from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Windows 下先加载 Paddle，避免 OpenCV 的运行库抢先加载后导致 mklml.dll 冲突。
import paddle  # noqa: F401
import cv2
import numpy as np
from opencc import OpenCC
from rapidfuzz import fuzz


PIPELINE_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = PIPELINE_ROOT.parent


def portable_path(path: Path) -> str:
    """Store paths relative to the repository so generated reports are portable."""
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.name
DEFAULT_CACHE_ALIAS = Path(tempfile.gettempdir()) / "codex_dianping_video_ocr_cache"
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(DEFAULT_CACHE_ALIAS))
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
T2S_CONVERTER = OpenCC("t2s")


@dataclass
class OCRToken:
    text: str
    score: float
    box: tuple[int, int, int, int]

    @property
    def x1(self) -> int:
        return self.box[0]

    @property
    def y1(self) -> int:
        return self.box[1]

    @property
    def x2(self) -> int:
        return self.box[2]

    @property
    def y2(self) -> int:
        return self.box[3]

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2


@dataclass
class CardObservation:
    merchant_name_raw: str
    category_raw: str
    rating: str
    avg_price_yuan: str
    floor_location: str
    ocr_confidence: float
    frame_index: int
    timestamp_sec: float
    crop_top: int
    crop_bottom: int
    all_text: str
    detection_method: str = "rating_anchor"
    rating_status: str = "rated"
    category_status: str = "parsed"
    title_height: int = 0
    title_score: float = 0.0
    metadata_text: str = ""
    card_type: str = "unknown"


@dataclass
class FrameRecallAudit:
    frame_index: int
    timestamp_sec: float
    ocr_tokens: int = 0
    title_candidates: int = 0
    rating_anchors: int = 0
    unrated_anchors: int = 0
    metadata_anchors: int = 0
    raw_anchors: int = 0
    matched_anchors: int = 0
    matched_anchor_titles: int = 0
    unmatched_anchors: int = 0
    accepted_cards: int = 0
    rated_cards: int = 0
    unrated_cards: int = 0
    missing_rating_cards: int = 0
    missing_category_cards: int = 0
    missing_floor_cards: int = 0


@dataclass
class MerchantRecord:
    best: CardObservation
    first_seen_sec: float
    last_seen_sec: float
    observations: int = 1
    variants: set[str] = field(default_factory=set)
    card_type_counts: dict[str, int] = field(default_factory=dict)


def imwrite_unicode(path: Path, image: np.ndarray, quality: int = 92) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower() or ".jpg"
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if suffix in {".jpg", ".jpeg"} else []
    ok, encoded = cv2.imencode(suffix, image, params)
    if not ok:
        raise RuntimeError(f"无法编码图像：{path}")
    encoded.tofile(str(path))


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = T2S_CONVERTER.convert(value)
    value = re.sub(r"[·•・∙]", "·", value)
    value = re.sub(r"[，,、]", "·", value)
    value = re.sub(r"[\s\u3000]+", "", value)
    value = re.sub(r"[…\.]{2,}$", "", value)
    value = re.sub(r"[\(（]商场内[\)）]$", "", value)
    return value.strip("-—_·,，。;；:：")


def has_name_chars(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fffA-Za-z]", value))


def is_title_token(token: OCRToken, image_width: int) -> bool:
    text = token.text.strip()
    x_ratio = token.x1 / image_width
    if not (0.265 <= x_ratio <= 0.32):
        return False
    if token.height < 46 or token.width < 70 or token.score < 0.55:
        return False
    if len(normalize_name(text)) < 2 or not has_name_chars(text):
        return False
    if re.fullmatch(r"[★☆*\d.￥¥折元\s]+", text):
        return False
    blocked = ("点评榜", "优惠", "代金券", "休息", "人均", "热门榜", "好评榜")
    return not any(word in text for word in blocked)


def is_unrated_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(
        marker in compact
        for marker in (
            "暂无评分",
            "暂无星级",
            "暂无评价",
            "暂无点评",
            "尚无评分",
            "尚无评价",
            "未评分",
        )
    )


def is_new_store_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if compact in {"新店", "新店开业", "即将开业"}:
        return True
    return bool(re.fullmatch(r"\d{1,2}(?:[./月]\d{1,2})?新店开业", compact))


def is_new_store_badge(
    token: OCRToken,
    title: OCRToken,
    image_width: int,
    image_height: int,
) -> bool:
    if not is_new_store_text(token.text):
        return False
    max_vertical_gap = max(80, int(round(image_height * 0.06)))
    return (
        title.y1 - 4 <= token.cy <= title.cy + max_vertical_gap
        and title.x1 - 25 <= token.x1 <= title.x1 + image_width * 0.20
    )


def is_structural_title_candidate(
    token: OCRToken,
    image_width: int,
    image_height: int,
) -> bool:
    text = token.text.strip()
    x_ratio = token.x1 / image_width
    min_height = max(24, int(round(image_height * 0.015)))
    min_width = max(45, int(round(image_width * 0.05)))
    if not (0.23 <= x_ratio <= 0.36):
        return False
    if token.height < min_height or token.width < min_width or token.score < 0.45:
        return False
    if len(normalize_name(text)) < 2 or not has_name_chars(text):
        return False
    if extract_rating(text) or extract_price(text) or is_unrated_text(text):
        return False
    if text.startswith(("“", "\"", "'", "￥", "¥")):
        return False
    if len(text) >= 12 and re.search(r"[，,、。！!]", text):
        return False
    blocked = (
        "点评榜",
        "点评单",
        "点评清单",
        "优惠",
        "代金券",
        "热门榜",
        "好评榜",
        "销量榜",
        "口味榜",
        "回头客榜",
        "已售",
    )
    if re.search(r"点评.{0,2}[单榜]", text):
        return False
    return not any(word in text for word in blocked)


# 大众点评楼层标记同时存在“字母在前”（L2、B1、LG2）和“数字在前”（1F、4F）两种写法。
# 只认前者会让 4F 这类 token 落不进楼层字段，转而被当作分类，因此两种都要覆盖。
# 嵌在长文本里时要求 LG 带层号，避免把 LG 电子等品牌名误判为楼层；
# 整个 token 就是楼层标记时（FLOOR_FULL）才允许裸 LG。
_FLOOR_LETTER_FIRST = r"LG[1-9]|B[1-9]|L[1-9]|F[1-9]"
_FLOOR_DIGIT_FIRST = r"[1-9][0-9]?F"
FLOOR_SEARCH = re.compile(
    rf"(?<![A-Z0-9.])({_FLOOR_LETTER_FIRST}|{_FLOOR_DIGIT_FIRST})(?:层)?(?![A-Z0-9.])"
)
FLOOR_FULL = re.compile(rf"(?:LG|{_FLOOR_LETTER_FIRST}|{_FLOOR_DIGIT_FIRST})(?:层)?")


def extract_rating(text: str) -> str:
    if "已售" in text or "折" in text:
        return ""
    match = re.search(r"(?<![\d￥¥])([0-5][.·]\d)(?!\d)", text)
    return match.group(1).replace("·", ".") if match else ""


def extract_price(text: str) -> str:
    match = re.search(r"[￥¥]\s*(\d+(?:\.\d+)?)\s*/\s*人", text)
    return match.group(1) if match else ""


def extract_floor(text: str) -> str:
    upper = unicodedata.normalize("NFKC", text).upper()
    match = FLOOR_SEARCH.search(upper)
    if match:
        return match.group(1)
    compact = re.sub(r"\s+", "", upper)
    if FLOOR_FULL.fullmatch(compact):
        return compact.removesuffix("层")
    return "商场内" if "商场内" in text else ""


def is_pure_floor_token(text: str) -> bool:
    """整个 token 就是一个楼层标记时为真；这类 token 不参与分类竞争。"""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).upper()
    return bool(FLOOR_FULL.fullmatch(compact))


def extract_category(text: str) -> str:
    if is_pure_floor_token(text):
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"^[^0-9A-Za-z\u3400-\u9fff]*", "", value)
    value = re.sub(
        r"^(?:暂无评分|暂无星级|暂无评价|暂无点评|尚无评分|尚无评价|未评分)\s*",
        "",
        value,
    )
    # 楼层前缀必须在数字噪声规则之前剥掉：否则 "10F" 会被 ^[0-5]\d 规则吃掉数字、只剩下 "F"
    value = re.sub(
        rf"^(?:{_FLOOR_LETTER_FIRST}|{_FLOOR_DIGIT_FIRST})(?:层)?[\s:：|、,，]*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"^[A-Za-z日口□国0-9._-]{0,10}L?[0-5][.·]\d\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"^[0-5]\d(?=[\u3400-\u9fffA-Za-z])", "", value)
    value = re.sub(r"[￥¥].*$", "", value)
    value = re.sub(r"\d+(?:\.\d+)?\s*/\s*人.*$", "", value)
    value = re.sub(
        rf"[:：]?(?:{_FLOOR_LETTER_FIRST}|{_FLOOR_DIGIT_FIRST})(?:层)?$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = value.strip(" -—_·,，。;；:：")
    if value in {"商场内", "店内", "附近"}:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?\s*/\s*人", value):
        return ""
    if not has_name_chars(value):
        return ""
    if any(word in value for word in ("点评榜", "热门榜", "好评榜", "休息")):
        return ""
    return value if len(value) <= 24 else ""


def is_plausible_category(value: str) -> bool:
    if not value or len(value) > 18:
        return False
    if is_pure_floor_token(value):
        return False
    if re.search(r"[“”\"，,。！!]", value):
        return False
    blocked = (
        "点评",
        "榜第",
        "热门榜",
        "好评榜",
        "销量榜",
        "口味榜",
        "回头客榜",
        "服务",
        "方便",
        "划算",
        "值得",
        "推荐",
        "体验",
        "超赞",
        "好看",
        "好用",
    )
    return not any(word in value for word in blocked)


def parse_ocr_result(result: object) -> list[OCRToken]:
    payload = getattr(result, "json", result)
    if isinstance(payload, dict) and "res" in payload:
        payload = payload["res"]
    texts = payload.get("rec_texts", [])
    scores = payload.get("rec_scores", [])
    boxes = payload.get("rec_boxes", [])
    tokens: list[OCRToken] = []
    for text, score, box in zip(texts, scores, boxes):
        clean_text = str(text).strip()
        if not clean_text:
            continue
        tokens.append(
            OCRToken(
                text=clean_text,
                score=float(score),
                box=tuple(int(value) for value in box),
            )
        )
    return sorted(tokens, key=lambda token: (token.y1, token.x1))


def load_ocr_tokens(path: Path) -> list[OCRToken]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return sorted(
        [
            OCRToken(
                text=str(item["text"]),
                score=float(item["score"]),
                box=tuple(int(value) for value in item["box"]),
            )
            for item in payload
        ],
        key=lambda token: (token.y1, token.x1),
    )


def parse_cards(
    tokens: list[OCRToken],
    image_width: int,
    image_height: int,
    scale: float,
    crop_y: int,
    frame_height: int,
    frame_index: int,
    timestamp_sec: float,
) -> tuple[list[CardObservation], FrameRecallAudit]:
    audit = FrameRecallAudit(
        frame_index=frame_index,
        timestamp_sec=timestamp_sec,
        ocr_tokens=len(tokens),
    )
    title_candidates = [
        token
        for token in tokens
        if is_structural_title_candidate(token, image_width, image_height)
    ]
    audit.title_candidates = len(title_candidates)

    anchor_candidates: list[tuple[int, str, str, OCRToken]] = []
    for token in tokens:
        rating = extract_rating(token.text)
        if rating:
            audit.rating_anchors += 1
            anchor_candidates.append((3, "rating_anchor", "rated", token))
            continue
        if is_unrated_text(token.text):
            audit.unrated_anchors += 1
            anchor_candidates.append((3, "unrated_anchor", "unrated", token))
            continue
        has_price = bool(extract_price(token.text))
        has_floor = bool(extract_floor(token.text))
        if has_price or has_floor:
            audit.metadata_anchors += 1
            priority = 2 if has_price else 1
            anchor_candidates.append((priority, "metadata_anchor", "ocr_missing", token))
    audit.raw_anchors = len(anchor_candidates)

    min_gap = max(6, int(round(image_height * 0.004)))
    max_gap = max(90, int(round(image_height * 0.07)))
    matched_by_title: dict[int, tuple[int, float, str, str, OCRToken, OCRToken]] = {}
    for priority, detection_method, rating_status, anchor in anchor_candidates:
        possible_titles = [
            title
            for title in title_candidates
            if min_gap <= anchor.cy - title.cy <= max_gap
            and title.y2 < anchor.cy
        ]
        if not possible_titles:
            audit.unmatched_anchors += 1
            continue
        audit.matched_anchors += 1
        title = min(possible_titles, key=lambda item: anchor.cy - item.cy)
        gap = anchor.cy - title.cy
        existing = matched_by_title.get(id(title))
        candidate = (priority, gap, detection_method, rating_status, anchor, title)
        if existing is None or priority > existing[0] or (
            priority == existing[0] and gap < existing[1]
        ):
            matched_by_title[id(title)] = candidate

    matches = sorted(matched_by_title.values(), key=lambda item: item[5].y1)
    audit.matched_anchor_titles = len(matches)
    observations: list[CardObservation] = []
    for title_index, (_, _, detection_method, rating_status, anchor, title) in enumerate(matches):
        next_title_y = matches[title_index + 1][5].y1 if title_index + 1 < len(matches) else image_height
        row_tolerance = max(24, int(round(image_height * 0.018)))
        metadata_row = [
            token
            for token in tokens
            if abs(token.cy - anchor.cy) <= row_tolerance
            and token.y1 < next_title_y
            and token.x1 >= title.x1 - 20
        ]
        rating = ""
        avg_price = ""
        floor = ""
        category = ""
        category_score = 0.0
        category_rank = -1.0
        # 楼层先单独认领：只由“整个 token 就是楼层标记”的 token 提供，
        # 并按 x 位置取最靠右的一个（点评卡片里楼层固定在这一行的右端）。
        # 这样楼层 token 不会再落空、也不会转而占用分类字段。
        floor_tokens = [token for token in metadata_row if is_pure_floor_token(token.text)]
        if floor_tokens:
            floor = extract_floor(max(floor_tokens, key=lambda item: item.x1).text)
        claimed_floor = {id(token) for token in floor_tokens}
        for token in metadata_row:
            rating = rating or extract_rating(token.text)
            avg_price = avg_price or extract_price(token.text)
            if not floor:
                floor = extract_floor(token.text)
            if id(token) in claimed_floor:
                continue
            candidate = extract_category(token.text)
            if is_new_store_text(token.text):
                continue
            if not is_plausible_category(candidate):
                continue
            rank = token.score
            if token is anchor:
                rank += 0.35
            if extract_rating(token.text) or extract_price(token.text):
                rank += 0.20
            rank -= min(len(candidate), 18) * 0.003
            if rank > category_rank:
                category = candidate
                category_score = token.score
                category_rank = rank

        if rating_status == "rated" and not rating:
            rating_status = "ocr_missing"
        category_status = "parsed" if category else "missing"
        region_bottom = min(
            image_height,
            next_title_y - 4,
            title.y1 + max(260, int(round(image_height * 0.20))),
        )
        card_region_tokens = [
            token
            for token in tokens
            if title.y1 - 8 <= token.cy < region_bottom
        ]
        card_tokens = [token.text for token in card_region_tokens]
        new_store_detected = any(
            is_new_store_badge(token, title, image_width, image_height)
            for token in card_region_tokens
        )
        if new_store_detected:
            card_type = "new_store"
        elif rating_status == "unrated":
            card_type = "unrated"
        elif rating_status == "rated" and rating:
            card_type = "rated"
        else:
            card_type = "unknown"
        confidence_parts = [title.score]
        if category:
            confidence_parts.append(category_score)
        confidence = sum(confidence_parts) / len(confidence_parts)

        crop_top = max(0, int(crop_y + title.y1 / scale - 18))
        if title_index + 1 < len(matches):
            crop_bottom = int(crop_y + next_title_y / scale - 18)
        else:
            crop_bottom = int(crop_y + title.y1 / scale + 235)
        crop_bottom = min(frame_height, max(crop_top + 120, crop_bottom))

        observations.append(
            CardObservation(
                merchant_name_raw=title.text,
                category_raw=category,
                rating=rating,
                avg_price_yuan=avg_price,
                floor_location=floor,
                ocr_confidence=confidence,
                frame_index=frame_index,
                timestamp_sec=timestamp_sec,
                crop_top=crop_top,
                crop_bottom=crop_bottom,
                all_text=" | ".join(card_tokens),
                detection_method=detection_method,
                rating_status=rating_status,
                category_status=category_status,
                title_height=title.height,
                title_score=title.score,
                metadata_text=" | ".join(token.text for token in metadata_row),
                card_type=card_type,
            )
        )
        audit.accepted_cards += 1
        if rating_status == "rated":
            audit.rated_cards += 1
        elif rating_status == "unrated":
            audit.unrated_cards += 1
        else:
            audit.missing_rating_cards += 1
        if not category:
            audit.missing_category_cards += 1
        if not floor:
            audit.missing_floor_cards += 1
    return observations, audit


def observation_quality(observation: CardObservation) -> float:
    name = observation.merchant_name_raw
    return (
        observation.ocr_confidence
        + (0.12 if observation.category_raw else 0.0)
        + (0.07 if observation.floor_location else 0.0)
        + (0.05 if observation.rating else 0.0)
        + (0.04 if observation.detection_method == "rating_anchor" else 0.0)
        + (0.04 if observation.card_type in {"new_store", "unrated"} else 0.0)
        + min(len(name), 24) * 0.003
        - (0.12 if "..." in name or "…" in name else 0.0)
    )


def same_merchant(left: CardObservation, right: CardObservation) -> bool:
    left_name = normalize_name(left.merchant_name_raw).casefold()
    right_name = normalize_name(right.merchant_name_raw).casefold()
    if not left_name or not right_name:
        return False
    if left.floor_location and right.floor_location and left.floor_location != right.floor_location:
        return False
    if left_name == right_name:
        return True
    if min(len(left_name), len(right_name)) >= 4 and (
        left_name.startswith(right_name) or right_name.startswith(left_name)
    ):
        return True
    ratio = fuzz.ratio(left_name, right_name)
    left_category = extract_category(left.category_raw).casefold()
    right_category = extract_category(right.category_raw).casefold()
    category_ratio = fuzz.ratio(left_category, right_category)
    category_compatible = (
        not left_category
        or not right_category
        or category_ratio >= 60
        or left_category in right_category
        or right_category in left_category
    )
    if ratio >= 91 and category_compatible:
        return True
    rating_compatible = not left.rating or not right.rating or left.rating == right.rating
    floor_compatible = (
        left.floor_location == right.floor_location
        or not left.floor_location
        or not right.floor_location
    )
    price_compatible = (
        not left.avg_price_yuan
        or not right.avg_price_yuan
        or left.avg_price_yuan == right.avg_price_yuan
    )
    strong_metadata_match = (
        category_compatible
        and rating_compatible
        and floor_compatible
        and price_compatible
    )
    return (
        strong_metadata_match
        and ratio >= 75
        and abs(len(left_name) - len(right_name)) <= 2
    )


def merge_observations(observations: Iterable[CardObservation]) -> list[MerchantRecord]:
    records: list[MerchantRecord] = []
    for observation in sorted(observations, key=lambda item: item.timestamp_sec):
        matched: MerchantRecord | None = None
        for record in records:
            if same_merchant(record.best, observation):
                matched = record
                break
        if matched is None:
            records.append(
                MerchantRecord(
                    best=observation,
                    first_seen_sec=observation.timestamp_sec,
                    last_seen_sec=observation.timestamp_sec,
                    variants={observation.merchant_name_raw},
                    card_type_counts={observation.card_type: 1},
                )
            )
            continue
        matched.observations += 1
        matched.last_seen_sec = observation.timestamp_sec
        matched.variants.add(observation.merchant_name_raw)
        matched.card_type_counts[observation.card_type] = (
            matched.card_type_counts.get(observation.card_type, 0) + 1
        )
        if observation_quality(observation) > observation_quality(matched.best):
            matched.best = observation
    return records


def frame_difference(left: np.ndarray, right: np.ndarray) -> float:
    left_small = cv2.resize(left, (96, 160), interpolation=cv2.INTER_AREA)
    right_small = cv2.resize(right, (96, 160), interpolation=cv2.INTER_AREA)
    return float(np.mean(cv2.absdiff(left_small, right_small)))


def select_keyframes(
    video_path: Path,
    interval_sec: float,
    probe_fps: float,
    min_difference: float,
    start_sec: float,
    max_seconds: float | None,
) -> tuple[list[tuple[int, float, np.ndarray]], dict[str, float]]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = frame_count / fps if fps else 0.0
    end_sec = min(duration, start_sec + max_seconds) if max_seconds else duration
    probe_step = max(1, int(round(fps / probe_fps)))
    bucket_frames = max(probe_step, int(round(interval_sec * fps)))
    start_frame = max(0, int(start_sec * fps))
    end_frame = min(frame_count, int(end_sec * fps))

    capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    best: tuple[float, int, np.ndarray] | None = None
    current_bucket_end = start_frame + bucket_frames
    selected: list[tuple[int, float, np.ndarray]] = []
    last_roi: np.ndarray | None = None
    frame_index = start_frame
    while frame_index < end_frame:
        ok, frame = capture.read()
        if not ok:
            break
        if (frame_index - start_frame) % probe_step == 0:
            roi = frame[int(height * 0.18) : int(height * 0.99)]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            if best is None or sharpness > best[0]:
                best = (sharpness, frame_index, frame.copy())
        if frame_index >= current_bucket_end:
            if best is not None:
                _, best_index, best_frame = best
                best_roi = cv2.cvtColor(
                    best_frame[int(height * 0.18) : int(height * 0.99)],
                    cv2.COLOR_BGR2GRAY,
                )
                if last_roi is None or frame_difference(last_roi, best_roi) >= min_difference:
                    selected.append((best_index, best_index / fps, best_frame))
                    last_roi = best_roi
            best = None
            current_bucket_end += bucket_frames
        frame_index += 1
    if best is not None:
        _, best_index, best_frame = best
        selected.append((best_index, best_index / fps, best_frame))
    capture.release()
    return selected, {
        "fps": fps,
        "frame_count": frame_count,
        "duration_sec": duration,
        "width": width,
        "height": height,
    }


def save_evidence(
    video_path: Path,
    records: list[MerchantRecord],
    output_dir: Path,
) -> None:
    capture = cv2.VideoCapture(str(video_path))
    evidence_dir = output_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    by_frame: dict[int, list[tuple[int, MerchantRecord]]] = {}
    for index, record in enumerate(records, start=1):
        by_frame.setdefault(record.best.frame_index, []).append((index, record))
    for frame_index, frame_records in by_frame.items():
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok:
            continue
        for index, record in frame_records:
            crop = frame[record.best.crop_top : record.best.crop_bottom]
            imwrite_unicode(evidence_dir / f"card_{index:04d}.jpg", crop)
    capture.release()


def safe_csv_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def record_card_type(record: MerchantRecord) -> str:
    counts = record.card_type_counts or {record.best.card_type: record.observations}
    if counts.get("new_store", 0):
        return "new_store"
    if counts.get("rated", 0):
        return "rated"
    if counts.get("unrated", 0):
        return "unrated"
    return "unknown"


def record_exclusion_reason(record: MerchantRecord) -> str:
    card_type = record_card_type(record)
    if card_type == "new_store":
        return "new_store"
    if card_type == "unrated":
        return "explicitly_unrated"
    return ""


def record_research_eligible(record: MerchantRecord) -> bool:
    return not record_exclusion_reason(record)


def record_review_reasons(record: MerchantRecord) -> list[str]:
    if not record_research_eligible(record):
        return []
    best = record.best
    reasons: list[str] = []
    if record.observations == 1:
        reasons.append("single_observation")
    if best.rating_status == "unrated":
        reasons.append("unrated")
    elif best.rating_status != "rated":
        reasons.append("rating_missing")
    if not best.category_raw:
        reasons.append("category_missing")
    # Floor is optional research metadata. Keep it for deduplication when OCR
    # succeeds, but do not send otherwise usable records to manual review when
    # the low-contrast floor label is missed.
    if best.detection_method != "rating_anchor":
        reasons.append(best.detection_method)
    if "..." in best.merchant_name_raw or "…" in best.merchant_name_raw:
        reasons.append("name_truncated")
    if best.title_score < 0.80:
        reasons.append("low_title_confidence")
    return reasons


def write_csv(
    records: list[MerchantRecord],
    output_path: Path,
    mall_name: str,
    video_path: Path,
    research_only: bool = False,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "card_id",
        "mall_name",
        "merchant_name_raw",
        "merchant_name_normalized",
        "category_raw",
        "rating",
        "avg_price_yuan",
        "floor_location",
        "card_type",
        "research_eligible",
        "exclusion_reason",
        "rating_status",
        "category_status",
        "detection_method",
        "ocr_confidence",
        "title_score",
        "title_height",
        "observations",
        "name_variants",
        "first_seen_sec",
        "last_seen_sec",
        "evidence_image",
        "source_video",
        "all_text",
        "metadata_text",
        "review_status",
        "review_reason",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=columns)
        writer.writeheader()
        for index, record in enumerate(records, start=1):
            if research_only and not record_research_eligible(record):
                continue
            best = record.best
            card_type = record_card_type(record)
            eligible = record_research_eligible(record)
            row = {
                "card_id": f"CARD-{index:04d}",
                "mall_name": mall_name,
                "merchant_name_raw": best.merchant_name_raw,
                "merchant_name_normalized": normalize_name(best.merchant_name_raw),
                "category_raw": best.category_raw,
                "rating": best.rating,
                "avg_price_yuan": best.avg_price_yuan,
                "floor_location": best.floor_location,
                "card_type": card_type,
                "research_eligible": eligible,
                "exclusion_reason": record_exclusion_reason(record),
                "rating_status": best.rating_status,
                "category_status": best.category_status,
                "detection_method": best.detection_method,
                "ocr_confidence": round(best.ocr_confidence, 4),
                "title_score": round(best.title_score, 4),
                "title_height": best.title_height,
                "observations": record.observations,
                "name_variants": " | ".join(sorted(record.variants)),
                "first_seen_sec": round(record.first_seen_sec, 2),
                "last_seen_sec": round(record.last_seen_sec, 2),
                "evidence_image": f"evidence/card_{index:04d}.jpg",
                "source_video": portable_path(video_path),
                "all_text": best.all_text,
                "metadata_text": best.metadata_text,
                "review_status": (
                    "研究排除"
                    if not eligible
                    else "建议复核"
                    if record_review_reasons(record)
                    else "结构完整"
                ),
                "review_reason": " | ".join(record_review_reasons(record)),
            }
            writer.writerow({key: safe_csv_text(value) for key, value in row.items()})


def write_observations_csv(
    observations: list[CardObservation],
    output_path: Path,
    mall_name: str,
    video_path: Path,
) -> None:
    columns = [
        "observation_id",
        "mall_name",
        "merchant_name_raw",
        "merchant_name_normalized",
        "category_raw",
        "rating",
        "avg_price_yuan",
        "floor_location",
        "card_type",
        "research_eligible",
        "exclusion_reason",
        "rating_status",
        "category_status",
        "detection_method",
        "ocr_confidence",
        "title_score",
        "title_height",
        "frame_index",
        "timestamp_sec",
        "metadata_text",
        "all_text",
        "source_video",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=columns)
        writer.writeheader()
        for index, observation in enumerate(observations, start=1):
            row = {
                "observation_id": f"OBS-{index:06d}",
                "mall_name": mall_name,
                "merchant_name_raw": observation.merchant_name_raw,
                "merchant_name_normalized": normalize_name(observation.merchant_name_raw),
                "category_raw": observation.category_raw,
                "rating": observation.rating,
                "avg_price_yuan": observation.avg_price_yuan,
                "floor_location": observation.floor_location,
                "card_type": observation.card_type,
                "research_eligible": observation.card_type not in {"new_store", "unrated"},
                "exclusion_reason": (
                    "new_store"
                    if observation.card_type == "new_store"
                    else "explicitly_unrated"
                    if observation.card_type == "unrated"
                    else ""
                ),
                "rating_status": observation.rating_status,
                "category_status": observation.category_status,
                "detection_method": observation.detection_method,
                "ocr_confidence": round(observation.ocr_confidence, 4),
                "title_score": round(observation.title_score, 4),
                "title_height": observation.title_height,
                "frame_index": observation.frame_index,
                "timestamp_sec": round(observation.timestamp_sec, 3),
                "metadata_text": observation.metadata_text,
                "all_text": observation.all_text,
                "source_video": portable_path(video_path),
            }
            writer.writerow({key: safe_csv_text(value) for key, value in row.items()})


def aggregate_recall_audits(audits: list[FrameRecallAudit]) -> dict[str, object]:
    metric_names = (
        "ocr_tokens",
        "title_candidates",
        "rating_anchors",
        "unrated_anchors",
        "metadata_anchors",
        "raw_anchors",
        "matched_anchors",
        "matched_anchor_titles",
        "unmatched_anchors",
        "accepted_cards",
        "rated_cards",
        "unrated_cards",
        "missing_rating_cards",
        "missing_category_cards",
        "missing_floor_cards",
    )
    totals = {name: sum(getattr(audit, name) for audit in audits) for name in metric_names}
    return {
        "totals": totals,
        "frames_with_cards": sum(1 for audit in audits if audit.accepted_cards),
        "frames_without_cards": sum(1 for audit in audits if not audit.accepted_cards),
        "per_frame": [audit.__dict__ for audit in audits],
    }


def build_ocr(recognition_model: str) -> object:
    from paddleocr import PaddleOCR

    return PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name=recognition_model,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def analyze(args: argparse.Namespace) -> Path:
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else PIPELINE_ROOT / "outputs" / video_path.stem
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    keyframes, metadata = select_keyframes(
        video_path=video_path,
        interval_sec=args.keyframe_interval,
        probe_fps=args.probe_fps,
        min_difference=args.min_frame_difference,
        start_sec=args.start_sec,
        max_seconds=args.max_seconds,
    )
    print(f"视频：{metadata['duration_sec']:.1f} 秒，候选关键帧：{len(keyframes)}")

    reuse_raw_ocr_dir = Path(args.reuse_raw_ocr_dir).resolve() if args.reuse_raw_ocr_dir else None
    if reuse_raw_ocr_dir and not reuse_raw_ocr_dir.is_dir():
        raise FileNotFoundError(f"原始 OCR 目录不存在：{reuse_raw_ocr_dir}")
    ocr = None if reuse_raw_ocr_dir else build_ocr(args.recognition_model)
    all_observations: list[CardObservation] = []
    frame_audits: list[FrameRecallAudit] = []
    raw_ocr_dir = output_dir / "raw_ocr"
    if args.save_debug:
        raw_ocr_dir.mkdir(parents=True, exist_ok=True)

    frame_height = int(metadata["height"])
    crop_y = int(frame_height * args.crop_top_ratio)
    crop_bottom = int(frame_height * args.crop_bottom_ratio)
    for position, (frame_index, timestamp_sec, frame) in enumerate(keyframes, start=1):
        raw_ocr_path = (
            reuse_raw_ocr_dir / f"frame_{frame_index:06d}.json"
            if reuse_raw_ocr_dir
            else None
        )
        if raw_ocr_path:
            if not raw_ocr_path.is_file():
                raise FileNotFoundError(f"缺少关键帧 OCR：{raw_ocr_path}")
            tokens = load_ocr_tokens(raw_ocr_path)
            scaled_height = int(round((crop_bottom - crop_y) * args.upscale))
            scaled_width = int(round(metadata["width"] * args.upscale))
        else:
            crop = frame[crop_y:crop_bottom]
            scaled = cv2.resize(
                crop,
                None,
                fx=args.upscale,
                fy=args.upscale,
                interpolation=cv2.INTER_CUBIC,
            )
            results = list(ocr.predict(input=scaled))
            if not results:
                frame_audits.append(
                    FrameRecallAudit(frame_index=frame_index, timestamp_sec=timestamp_sec)
                )
                continue
            tokens = parse_ocr_result(results[0])
            scaled_height, scaled_width = scaled.shape[:2]
        observations, frame_audit = parse_cards(
            tokens=tokens,
            image_width=scaled_width,
            image_height=scaled_height,
            scale=args.upscale,
            crop_y=crop_y,
            frame_height=frame_height,
            frame_index=frame_index,
            timestamp_sec=timestamp_sec,
        )
        all_observations.extend(observations)
        frame_audits.append(frame_audit)
        if args.save_debug:
            payload = [
                {"text": token.text, "score": token.score, "box": token.box}
                for token in tokens
            ]
            (raw_ocr_dir / f"frame_{frame_index:06d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if position % 10 == 0 or position == len(keyframes):
            print(
                f"OCR {position}/{len(keyframes)}，累计卡片观察：{len(all_observations)}，"
                f"累计锚点：{sum(audit.raw_anchors for audit in frame_audits)}",
                flush=True,
            )

    records = merge_observations(all_observations)
    save_evidence(video_path, records, output_dir)
    csv_path = output_dir / "merchant_cards.csv"
    research_csv_path = output_dir / "research_merchants.csv"
    observations_csv_path = output_dir / "card_observations.csv"
    recall_audit_path = output_dir / "recall_audit.json"
    write_csv(records, csv_path, args.mall_name, video_path)
    write_csv(
        records,
        research_csv_path,
        args.mall_name,
        video_path,
        research_only=True,
    )
    write_observations_csv(
        all_observations,
        observations_csv_path,
        args.mall_name,
        video_path,
    )
    recall_audit = aggregate_recall_audits(frame_audits)
    recall_audit_path.write_text(
        json.dumps(recall_audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        "mall_name": args.mall_name,
        "source_video": portable_path(video_path),
        "video_metadata": metadata,
        "keyframes": len(keyframes),
        "card_observations": len(all_observations),
        "unique_cards": len(records),
        "csv": portable_path(csv_path),
        "research_csv": portable_path(research_csv_path),
        "observations_csv": portable_path(observations_csv_path),
        "recall_audit": portable_path(recall_audit_path),
        "recall_totals": recall_audit["totals"],
        "records_requiring_review": sum(
            1 for record in records if record_review_reasons(record)
        ),
        "research_eligible_records": sum(
            1 for record in records if record_research_eligible(record)
        ),
        "excluded_new_store_records": sum(
            1 for record in records if record_card_type(record) == "new_store"
        ),
        "excluded_unrated_records": sum(
            1 for record in records if record_card_type(record) == "unrated"
        ),
        "rated_records": sum(1 for record in records if record.best.rating_status == "rated"),
        "unrated_records": sum(1 for record in records if record.best.rating_status == "unrated"),
        "missing_rating_records": sum(
            1 for record in records if record.best.rating_status == "ocr_missing"
        ),
    }
    (output_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"完成：{len(records)} 张去重卡片，{len(all_observations)} 次观察，CSV：{csv_path}"
    )
    return csv_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从大众点评商场列表录屏中提取商户卡片")
    parser.add_argument("--video", required=True, help="输入 MP4 路径")
    parser.add_argument("--mall-name", required=True, help="商场名称")
    parser.add_argument("--output-dir", help="输出文件夹；默认写入 outputs/<视频文件名>")
    parser.add_argument("--start-sec", type=float, default=0.0)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--keyframe-interval", type=float, default=0.5)
    parser.add_argument("--probe-fps", type=float, default=5.0)
    parser.add_argument("--min-frame-difference", type=float, default=1.0)
    parser.add_argument("--crop-top-ratio", type=float, default=0.18)
    parser.add_argument("--crop-bottom-ratio", type=float, default=0.99)
    parser.add_argument("--upscale", type=float, default=2.0)
    parser.add_argument(
        "--recognition-model",
        default="PP-OCRv5_mobile_rec",
        choices=("PP-OCRv5_server_rec", "PP-OCRv5_mobile_rec"),
        help="文字识别模型；默认移动端模型适合整屏批量处理，服务端模型仅建议小样本复核。",
    )
    parser.add_argument("--save-debug", action="store_true")
    parser.add_argument(
        "--reuse-raw-ocr-dir",
        default="",
        help="复用 -SaveDebug 生成的 raw_ocr 目录，跳过 OCR 模型推理",
    )
    return parser


if __name__ == "__main__":
    analyze(build_parser().parse_args())
