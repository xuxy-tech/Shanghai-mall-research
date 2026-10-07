from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def portable_path(path: Path) -> str:
    """Store generated paths relative to the repository."""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def portable_text_path(value: str) -> str:
    """Normalize paths read from older OCR CSVs without retaining local roots."""
    normalized = value.replace("\\", "/")
    for marker, prefix in (
        ("/video_ocr_pipeline/", "ocr/"),
        ("/merchant_data_pipeline/", "data/"),
    ):
        if marker in normalized:
            return prefix + normalized.split(marker, 1)[1]
    return normalized
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "pipeline_config.json"
OUTPUT_CSV_FILES = (
    "excluded_records.csv",
    "duplicate_audit.csv",
    "name_correction_candidates.csv",
    "name_corrections_applied.csv",
    "category_unmapped.csv",
    "category_summary.csv",
    "mall_metrics.csv",
    "mall_similarity.csv",
    "brand_summary.csv",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    print(message, flush=True)


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def compact_token(value: Any) -> str:
    return re.sub(r"\s+", "", text(value))


def brand_key(value: Any) -> str:
    return "".join(ch.lower() for ch in text(value) if ch.isalnum())


def stable_id(prefix: str, value: str, length: int = 12) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:length].upper()
    return f"{prefix}-{digest}"


def resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def capture_date_from_folder(folder_name: str) -> str:
    match = re.search(r"__(\d{8})$", folder_name)
    if not match:
        return ""
    raw = match.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def normalize_floor(value: Any) -> str:
    value = compact_token(value).upper()
    if not value:
        return ""
    match = re.fullmatch(r"(?:B\d+|LG\d+|L\d+|F\d+|\d+F)", value)
    return match.group(0) if match else ""


def metadata_has_exact_token(metadata: str, accepted: set[str]) -> bool:
    tokens = [compact_token(token) for token in str(metadata or "").split("|")]
    return any(token in accepted for token in tokens)


def rule_matches(value: str, match_type: str, pattern: str) -> bool:
    value = text(value)
    if match_type == "exact":
        return value.casefold() == text(pattern).casefold()
    if match_type == "contains":
        return text(pattern).casefold() in value.casefold()
    if match_type == "regex":
        return bool(re.search(pattern, value, flags=re.IGNORECASE))
    raise ValueError(f"Unsupported match_type: {match_type}")


class CategoryMapper:
    def __init__(self, rules: list[dict[str, str]]) -> None:
        self.rules = sorted(
            [rule for rule in rules if as_bool(rule.get("active", True))],
            key=lambda item: int(item.get("priority") or 9999),
        )
        self.level1_values = sorted({rule["category_l1"] for rule in self.rules if rule.get("category_l1")})

    def map(self, raw_category: str) -> tuple[str, str, str, str]:
        for rule in self.rules:
            if rule_matches(raw_category, rule["match_type"], rule["pattern"]):
                return rule["category_l1"], rule["category_l2"], "mapped", rule["pattern"]
        return "未映射", "未映射", "unmapped", ""


def is_category_noise(raw_category: Any) -> bool:
    value = compact_token(raw_category)
    if not value:
        return True
    return bool(
        re.fullmatch(
            r"(?:B\d+|LG\d+|L\d+|F\d+|\d+F|\d+/(?:B\d+|LG\d+|L\d+|F\d+|\d+F|商场内)|月|\.*)",
            value,
            flags=re.IGNORECASE,
        )
    )


def build_unmapped_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("mapping_status") == "unmapped":
            grouped[text(row.get("category_raw"))].append(row)
    output = []
    for raw_category, group in grouped.items():
        output.append(
            {
                "category_raw": raw_category,
                "source_record_count": len(group),
                "mall_count": len({row["mall_id"] for row in group}),
                "sample_merchant_names": " | ".join(
                    sorted({row["canonical_brand_name"] for row in group})[:8]
                ),
                "suggested_action": (
                    "discard_ocr_category_noise"
                    if is_category_noise(raw_category)
                    else "review_or_edit_category_mapping"
                ),
            }
        )
    return sorted(output, key=lambda item: (-item["source_record_count"], item["category_raw"]))


def load_manual_exclusions(path: Path) -> set[tuple[str, str]]:
    """人工逐条确认的排除项，按 (商场, 商户名) 精确匹配。

    大众点评会把剧目、限时展览和活动作为独立条目收录，与常驻场地重复计数。
    这类判断依赖逐条阅读，无法用正则表达，因此单独维护并在此按精确键匹配。
    """
    if not path.exists():
        return set()
    return {
        (compact_token(row.get("mall_name")), compact_token(row.get("merchant_name")))
        for row in read_csv(path)
        if as_bool(row.get("active", True))
    }


def apply_exclusion_rules(row: dict[str, Any], rules: list[dict[str, str]]) -> list[str]:
    reasons: list[str] = []
    keep_override = False
    for rule in sorted(rules, key=lambda item: int(item.get("priority") or 9999)):
        if not as_bool(rule.get("active", True)):
            continue
        field = rule.get("field", "")
        if field not in row:
            continue
        if rule_matches(str(row.get(field, "")), rule.get("match_type", "exact"), rule.get("pattern", "")):
            if rule.get("action") == "keep":
                keep_override = True
            elif rule.get("action") == "exclude":
                reasons.append(rule.get("reason") or rule.get("rule_id") or "configured_exclusion")
    return [] if keep_override else sorted(set(reasons))


def levenshtein(a: str, b: str, cutoff: int | None = None) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > (cutoff if cutoff is not None else max(len(a), len(b))):
        return (cutoff + 1) if cutoff is not None else abs(len(a) - len(b))
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        row_min = i
        for j, char_b in enumerate(b, start=1):
            value = min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char_a != char_b))
            current.append(value)
            row_min = min(row_min, value)
        if cutoff is not None and row_min > cutoff:
            return cutoff + 1
        previous = current
    return previous[-1]


def category_compatible(a: dict[str, set[str]], b: dict[str, set[str]]) -> bool:
    a_l2 = {value for value in a["l2"] if value and value != "未映射"}
    b_l2 = {value for value in b["l2"] if value and value != "未映射"}
    if a_l2 and b_l2:
        return bool(a_l2 & b_l2)
    a_l1 = {value for value in a["l1"] if value and value != "未映射"}
    b_l1 = {value for value in b["l1"] if value and value != "未映射"}
    return bool(a_l1 & b_l1)


def protected_difference(name_a: str, name_b: str, protected_terms: Iterable[str]) -> bool:
    for term in protected_terms:
        if (term in name_a) != (term in name_b):
            return True
    return False


def quality_score(row: dict[str, Any]) -> tuple[float, ...]:
    return (
        1.0 if row.get("rating") else 0.0,
        1.0 if row.get("mapping_status") == "mapped" else 0.0,
        1.0 if row.get("category_raw") else 0.0,
        numeric(row.get("title_score")),
        numeric(row.get("ocr_confidence")),
        numeric(row.get("observations")),
    )


def load_inputs(input_root: Path, input_filename: str, include_malls: set[str]) -> tuple[list[dict[str, Any]], list[Path]]:
    files = sorted(input_root.glob(f"*/{input_filename}"))
    rows: list[dict[str, Any]] = []
    used_files: list[Path] = []
    for source_file in files:
        source_rows = read_csv(source_file)
        if not source_rows:
            continue
        mall_name = text(source_rows[0].get("mall_name"))
        if include_malls and mall_name not in include_malls:
            continue
        used_files.append(source_file)
        source_folder = source_file.parent.name
        capture_date = capture_date_from_folder(source_folder)
        for source in source_rows:
            row: dict[str, Any] = dict(source)
            row["mall_name"] = text(row.get("mall_name"))
            row["mall_id"] = stable_id("MALL", row["mall_name"], 10)
            row["capture_date"] = capture_date
            row["source_folder"] = source_folder
            row["source_file"] = portable_path(source_file)
            row["source_record_id"] = f"{source_folder}/{row.get('card_id', '')}"
            evidence = text(row.get("evidence_image"))
            row["evidence_image_absolute"] = portable_path(source_file.parent / evidence) if evidence else ""
            if row.get("source_video"):
                row["source_video"] = portable_text_path(row["source_video"])
            row["normalized_floor"] = normalize_floor(row.get("floor_location"))
            rows.append(row)
    return rows, used_files


def preprocess_rows(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    category_mapper: CategoryMapper,
    exclusion_rules: list[dict[str, str]],
    aliases: dict[str, str],
    manual_exclusions: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manual_exclusions = manual_exclusions or set()
    accepted_new_tokens = {compact_token(value) for value in config["exclusion"]["new_store_exact_tokens"]}
    excluded: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for row in rows:
        category_l1, category_l2, mapping_status, mapping_rule = category_mapper.map(text(row.get("category_raw")))
        row["category_l1"] = category_l1
        row["category_l2"] = category_l2
        row["mapping_status"] = mapping_status
        row["mapping_rule"] = mapping_rule
        original_name = text(row.get("merchant_name_normalized") or row.get("merchant_name_raw"))
        original_key = brand_key(original_name)
        row["canonical_brand_name"] = aliases.get(original_key, original_name)
        row["correction_method"] = "manual_alias" if original_key in aliases else "none"
        reasons: list[str] = []
        if config["exclusion"].get("exclude_exact_new_store_badge") and metadata_has_exact_token(
            str(row.get("metadata_text", "")), accepted_new_tokens
        ):
            reasons.append("exact_new_store_badge")
        if config["exclusion"].get("exclude_explicit_unrated") and (
            row.get("rating_status") == "unrated" or row.get("card_type") == "unrated"
        ):
            reasons.append("explicitly_unrated")
        reasons.extend(apply_exclusion_rules(row, exclusion_rules))
        if (compact_token(row.get("mall_name")), compact_token(row["canonical_brand_name"])) in manual_exclusions:
            reasons.append("manual_review_exclusion")
        if config.get("category", {}).get("exclude_unmapped", True) and mapping_status == "unmapped":
            reasons.append("unmapped_category")
        # 门店状态作为变量保留，不决定门店是否存在。
        # 评分缺失是数据属性而非商户属性；新店是经营阶段而非排除理由。
        # 二者的缺失都不随机（游戏电竞 24.3% vs 奢侈品 2.6%），排除会系统性低估新兴业态。
        row["is_new_store"] = metadata_has_exact_token(
            str(row.get("metadata_text", "")), accepted_new_tokens
        )
        row["rating_state"] = (
            "explicitly_unrated"
            if (row.get("rating_status") == "unrated" or row.get("card_type") == "unrated")
            else ("rated" if text(row.get("rating")) else "ocr_missing")
        )
        row["analysis_eligible"] = not reasons
        row["processing_exclusion_reason"] = ";".join(sorted(set(reasons)))
        (eligible if row["analysis_eligible"] else excluded).append(row)
    return eligible, excluded


def apply_name_consistency(
    rows: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    settings = config["name_consistency"]
    stats: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[brand_key(row["canonical_brand_name"])].append(row)
    for key, group in grouped.items():
        display = Counter(text(row["canonical_brand_name"]) for row in group).most_common(1)[0][0]
        stats[key] = {
            "display": display,
            "malls": {row["mall_id"] for row in group},
            "rows": len(group),
            "l1": {row["category_l1"] for row in group},
            "l2": {row["category_l2"] for row in group},
        }
    candidates: list[dict[str, Any]] = []
    corrections: dict[str, str] = {}
    if settings.get("enabled"):
        keys = list(stats)
        for variant_key in keys:
            variant = stats[variant_key]
            if len(variant["malls"]) > int(settings["max_variant_malls"]):
                continue
            best: tuple[float, int, str] | None = None
            for reference_key in keys:
                if reference_key == variant_key:
                    continue
                reference = stats[reference_key]
                reference_malls = len(reference["malls"])
                if reference_malls < int(settings["candidate_min_reference_malls"]):
                    continue
                if abs(len(variant_key) - len(reference_key)) > int(settings["max_edit_distance"]):
                    continue
                distance = levenshtein(variant_key, reference_key, int(settings["max_edit_distance"]))
                if distance > int(settings["max_edit_distance"]):
                    continue
                similarity = 1.0 - distance / max(len(variant_key), len(reference_key), 1)
                if similarity < float(settings["min_similarity"]):
                    continue
                if not category_compatible(variant, reference):
                    continue
                if protected_difference(variant["display"], reference["display"], settings["protected_terms"]):
                    continue
                rank = (similarity, reference_malls, reference_key)
                if best is None or rank > best:
                    best = rank
            if best is None:
                continue
            similarity, reference_malls, reference_key = best
            reference = stats[reference_key]
            auto_eligible = (
                bool(settings.get("auto_correct"))
                and reference_malls >= int(settings["min_reference_malls"])
                and reference["rows"] > variant["rows"]
            )
            candidates.append(
                {
                    "variant_name": variant["display"],
                    "reference_name": reference["display"],
                    "edit_distance": levenshtein(variant_key, reference_key),
                    "similarity": round(similarity, 4),
                    "variant_mall_count": len(variant["malls"]),
                    "reference_mall_count": reference_malls,
                    "variant_row_count": variant["rows"],
                    "reference_row_count": reference["rows"],
                    "category_compatible": True,
                    "auto_eligible": auto_eligible,
                    "action": "auto_corrected" if auto_eligible else "review_candidate",
                }
            )
            if auto_eligible:
                corrections[variant_key] = reference["display"]
    for row in rows:
        key = brand_key(row["canonical_brand_name"])
        if key in corrections:
            row["canonical_brand_name"] = corrections[key]
            row["correction_method"] = "cross_mall_auto"
    final_display: dict[str, str] = {}
    final_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        final_groups[brand_key(row["canonical_brand_name"])].append(row)
    for key, group in final_groups.items():
        corrected = [row["canonical_brand_name"] for row in group if row["correction_method"] != "none"]
        final_display[key] = corrected[0] if corrected else Counter(row["canonical_brand_name"] for row in group).most_common(1)[0][0]
    for row in rows:
        key = brand_key(row["canonical_brand_name"])
        row["canonical_brand_name"] = final_display[key]
        row["brand_id"] = stable_id("BRAND", key)
    return rows, sorted(candidates, key=lambda item: (-item["reference_mall_count"], -item["similarity"], item["variant_name"]))


def deduplicate(rows: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    settings = config["deduplication"]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["mall_id"], row["brand_id"])].append(row)
    instances: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for (_, _), group in grouped.items():
        known_floors = sorted({row["normalized_floor"] for row in group if row["normalized_floor"]})
        if not settings.get("enabled"):
            buckets = {f"row-{index}": [row] for index, row in enumerate(group)}
            policy = "deduplication_disabled"
        elif settings.get("keep_confirmed_different_floors") and len(known_floors) > 1:
            buckets = {floor: [row for row in group if row["normalized_floor"] == floor] for floor in known_floors}
            unknown = [row for row in group if not row["normalized_floor"]]
            if unknown:
                best_floor = max(known_floors, key=lambda floor: max(quality_score(row) for row in buckets[floor]))
                buckets[best_floor].extend(unknown)
            policy = "keep_confirmed_different_floors"
        else:
            bucket_name = known_floors[0] if known_floors else "UNKNOWN"
            buckets = {bucket_name: group}
            policy = "merge_same_or_missing_floor"
        instance_ids: list[str] = []
        for bucket_name, bucket_rows in buckets.items():
            representative = max(bucket_rows, key=quality_score)
            effective_floor = representative["normalized_floor"] or (bucket_name if bucket_name != "UNKNOWN" else "")
            instance_id = stable_id(
                "STORE", f"{representative['mall_id']}|{representative['brand_id']}|{effective_floor or 'UNKNOWN'}"
            )
            instance_ids.append(instance_id)
            instance = dict(representative)
            instance.update(
                {
                    "store_instance_id": instance_id,
                    "floor_location": effective_floor or text(representative.get("floor_location")),
                    "source_record_count": len(bucket_rows),
                    "merged_source_record_ids": " | ".join(sorted(row["source_record_id"] for row in bucket_rows)),
                    "merged_card_ids": " | ".join(sorted({text(row.get("card_id")) for row in bucket_rows if row.get("card_id")})),
                    "merged_name_variants": " | ".join(sorted({text(row.get("merchant_name_normalized")) for row in bucket_rows})),
                    "merged_floor_candidates": " | ".join(sorted({row["normalized_floor"] for row in bucket_rows if row["normalized_floor"]})),
                    "evidence_images": " | ".join(sorted({row["evidence_image_absolute"] for row in bucket_rows if row["evidence_image_absolute"]})),
                    "dedup_policy": policy,
                }
            )
            instances.append(instance)
        if len(group) > 1:
            audit.append(
                {
                    "mall_id": group[0]["mall_id"],
                    "mall_name": group[0]["mall_name"],
                    "brand_id": group[0]["brand_id"],
                    "canonical_brand_name": group[0]["canonical_brand_name"],
                    "rows_before": len(group),
                    "instances_after": len(buckets),
                    "duplicates_removed": len(group) - len(buckets),
                    "known_floors": " | ".join(known_floors),
                    "dedup_policy": policy,
                    "store_instance_ids": " | ".join(instance_ids),
                    "source_record_ids": " | ".join(sorted(row["source_record_id"] for row in group)),
                }
            )
    return instances, sorted(audit, key=lambda item: (-item["duplicates_removed"], item["mall_name"], item["canonical_brand_name"]))


def add_brand_metrics(instances: list[dict[str, Any]], known_chains: set[str]) -> list[dict[str, Any]]:
    coverage: dict[str, set[str]] = defaultdict(set)
    counts: Counter[str] = Counter()
    for row in instances:
        coverage[row["brand_id"]].add(row["mall_id"])
        counts[row["brand_id"]] += 1
    for row in instances:
        row["mall_coverage_count"] = len(coverage[row["brand_id"]])
        row["shared_across_malls"] = row["mall_coverage_count"] >= 2
        row["known_chain"] = brand_key(row["canonical_brand_name"]) in known_chains
        row["brand_store_instance_count"] = counts[row["brand_id"]]
    return instances


def build_brand_summary(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in instances:
        grouped[row["brand_id"]].append(row)
    output = []
    for brand_id, group in grouped.items():
        output.append(
            {
                "brand_id": brand_id,
                "canonical_brand_name": group[0]["canonical_brand_name"],
                "mall_coverage_count": len({row["mall_id"] for row in group}),
                "store_instance_count": len(group),
                "known_chain": group[0]["known_chain"],
                "category_l1": Counter(row["category_l1"] for row in group).most_common(1)[0][0],
                "category_l2": Counter(row["category_l2"] for row in group).most_common(1)[0][0],
                "mall_names": " | ".join(sorted({row["mall_name"] for row in group})),
            }
        )
    return sorted(output, key=lambda item: (-item["mall_coverage_count"], item["canonical_brand_name"]))


def build_metrics(
    source_rows: list[dict[str, Any]],
    excluded_rows: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    duplicate_audit: list[dict[str, Any]],
    category_level1_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    malls = sorted({row["mall_name"] for row in source_rows})
    source_by_mall = Counter(row["mall_name"] for row in source_rows)
    excluded_by_mall = Counter(row["mall_name"] for row in excluded_rows)
    removed_by_mall = Counter()
    for row in duplicate_audit:
        removed_by_mall[row["mall_name"]] += int(row["duplicates_removed"])
    instances_by_mall: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in instances:
        instances_by_mall[row["mall_name"]].append(row)
    mall_metrics = []
    category_summary = []
    for mall in malls:
        current = instances_by_mall[mall]
        total = len(current)
        category_counts = Counter(row["category_l1"] for row in current)
        mapped = total - category_counts.get("未映射", 0)
        probabilities = [
            count / mapped
            for category, count in category_counts.items()
            if mapped and category != "未映射"
        ]
        entropy = -sum(probability * math.log(probability) for probability in probabilities if probability > 0)
        normalized_entropy = entropy / math.log(category_level1_count) if category_level1_count > 1 else 0.0
        hhi = sum(probability * probability for probability in probabilities)
        shared = sum(as_bool(row["shared_across_malls"]) for row in current)
        chain = sum(as_bool(row["known_chain"]) for row in current)
        unmapped_source = sum(
            row["mall_name"] == mall and row.get("mapping_status") == "unmapped"
            for row in source_rows
        )
        mall_metrics.append(
            {
                "mall_id": current[0]["mall_id"] if current else stable_id("MALL", mall, 10),
                "mall_name": mall,
                "source_record_count": source_by_mall[mall],
                "excluded_record_count": excluded_by_mall[mall],
                "eligible_before_dedup": source_by_mall[mall] - excluded_by_mall[mall],
                "duplicates_removed": removed_by_mall[mall],
                "final_store_instance_count": total,
                "unique_brand_count": len({row["brand_id"] for row in current}),
                "shared_store_count": shared,
                "shared_store_ratio": round(shared / total, 6) if total else 0,
                "known_chain_store_count": chain,
                "known_chain_rate": round(chain / total, 6) if total else 0,
                "mapped_category_count": mapped,
                "category_mapping_rate": round(mapped / total, 6) if total else 0,
                "unmapped_source_record_count": unmapped_source,
                "source_category_mapping_rate": round(
                    (source_by_mall[mall] - unmapped_source) / source_by_mall[mall], 6
                ) if source_by_mall[mall] else 0,
                "missing_floor_count": sum(not text(row.get("floor_location")) for row in current),
                "missing_rating_count": sum(not text(row.get("rating")) for row in current),
                "category_entropy": round(entropy, 6),
                "normalized_category_entropy": round(normalized_entropy, 6),
                "category_hhi": round(hhi, 6),
                "result_status": "current",
            }
        )
        for category, count in sorted(category_counts.items()):
            category_summary.append(
                {
                    "mall_name": mall,
                    "category_l1": category,
                    "store_instance_count": count,
                    "category_ratio": round(count / total, 6) if total else 0,
                    "result_status": "current",
                }
            )
    similarity = []
    brand_sets = {mall: {row["brand_id"] for row in instances_by_mall[mall]} for mall in malls}
    for mall_a, mall_b in itertools.combinations(malls, 2):
        intersection = len(brand_sets[mall_a] & brand_sets[mall_b])
        union = len(brand_sets[mall_a] | brand_sets[mall_b])
        similarity.append(
            {
                "mall_a": mall_a,
                "mall_b": mall_b,
                "shared_brand_count": intersection,
                "union_brand_count": union,
                "jaccard_similarity": round(intersection / union, 6) if union else 0,
                "result_status": "current",
            }
        )
    return mall_metrics, category_summary, sorted(similarity, key=lambda item: -item["jaccard_similarity"])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_report(
    output_dir: Path,
    source_rows: list[dict[str, Any]],
    excluded_rows: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    duplicate_audit: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    unmapped: list[dict[str, Any]],
    mall_metrics: list[dict[str, Any]],
) -> None:
    exclusion_counts = Counter()
    for row in excluded_rows:
        for reason in str(row.get("processing_exclusion_reason", "")).split(";"):
            if reason:
                exclusion_counts[reason] += 1
    auto_corrected = sum(item["action"] == "auto_corrected" for item in candidates)
    report = [
        "# 商场商户主表构建报告",
        "",
        "> 本报告记录当前主表的构建范围、处理规则和质量审计；研究解释仍需结合商场样本覆盖范围。",
        "",
        "## 构建范围",
        "",
        f"- 商场数：{len({row['mall_id'] for row in source_rows})}",
        f"- OCR 候选记录：{len(source_rows)}",
        f"- 汇总期排除记录：{len(excluded_rows)}",
        f"- 去重后店铺实例：{len(instances)}",
        f"- 同商场重复审计组：{len(duplicate_audit)}",
        f"- 名称一致性候选：{len(candidates)}，其中自动修正 {auto_corrected}",
        f"- 未映射原始分类值：{len(unmapped)}",
        f"- 因分类未映射而不进入主表的源记录：{sum(row['source_record_count'] for row in unmapped)}",
        "",
        "## 处理规则与验证结果",
        "",
        "- 原始 OCR 文件未修改；所有输出均可由配置和原始文件重建。",
        "- 新店排除只检查卡片固定信息文本中独立且精确的“新店”token；英文 New 和其它开业文案不触发。",
        "- 同商场同品牌仅在明确抓到不同楼层时保留多个实例；同层或楼层缺失按重复合并，并保留来源记录与证据路径。",
        "- 跨商场名称校正仅处理高频规范名对应的孤立一字符差异；其它相似名称只进入候选表。",
        "- 连锁率只读取人工维护的连锁品牌表，与样本内跨商场重复率分开计算。",
        "- 未映射分类只进入分类审计和排除表，不进入分析主表、品牌统计及相似度指标。",
        "",
        "## 本次发现的问题",
        "",
        f"- 分类映射仍有 {len(unmapped)} 个原始分类值未覆盖；楼层、空值和不明截断值直接丢弃，其它明确类别可继续编辑映射表。",
        "- 快闪店和临时展览只有在店名或分类明确标注时才能自动排除；未标注的临时项目仍需人工名单。",
        "- 楼层缺失时执行偏激进合并，可能误杀少量同商场多铺位品牌；重复审计表保留了可回溯证据。",
        "- 当前连锁品牌表仍是人工维护名单，连锁率口径会随名单更新而变化。",
        "- 相似度、熵和连锁率会随新增商场自动重算；解释这些指标时仍需考虑样本选址和类型覆盖。",
        "",
        "## 排除原因计数",
        "",
    ]
    report.extend(f"- {reason}: {count}" for reason, count in sorted(exclusion_counts.items()))
    report.extend(["", "## 商场级摘要", "", "| 商场 | 最终店铺实例 | 跨商场重复比例 | 源记录分类命中率 |", "|---|---:|---:|---:|"])
    for row in mall_metrics:
        report.append(
            f"| {row['mall_name']} | {row['final_store_instance_count']} | {float(row['shared_store_ratio']):.1%} | {float(row['source_category_mapping_rate']):.1%} |"
        )
    report.extend(["", "## 说明", "", "当前主表是本项目统一使用的可重建数据集。分类映射、临时项目识别和楼层缺失去重仍可随新增样本继续校准；对指标作研究解释时需结合样本覆盖范围。", ""])
    (output_dir / "BUILD_REPORT.md").write_text("\n".join(report), encoding="utf-8")


def build(config: dict[str, Any]) -> dict[str, Any]:
    config_dir = PROJECT_ROOT / "data" / "config"
    input_root = resolve_project_path(config["input_root"])
    output_dir = resolve_project_path(config["output_dir"])
    master_output = resolve_project_path(config.get("master_output", "data/master_stores.csv"))
    include_malls = {text(value) for value in config.get("include_malls", [])}
    log(f"[1/7] 读取 OCR 候选表：{input_root}")
    source_rows, input_files = load_inputs(input_root, config["input_filename"], include_malls)
    if not source_rows:
        raise RuntimeError("没有找到可处理的 merchant_cards.csv")
    log(f"      找到 {len(input_files)} 家商场，{len(source_rows)} 行候选记录")

    category_rules = read_csv(config_dir / "category_mapping.csv")
    category_mapper = CategoryMapper(category_rules)
    exclusion_rules = read_csv(config_dir / "exclusion_rules.csv")
    alias_rows = read_csv(config_dir / "brand_aliases.csv")
    aliases = {
        brand_key(row["alias"]): text(row["canonical_brand_name"])
        for row in alias_rows
        if as_bool(row.get("active", True))
    }
    chain_rows = read_csv(config_dir / "chain_brands.csv")
    known_chains = {
        brand_key(row["canonical_brand_name"])
        for row in chain_rows
        if as_bool(row.get("active", True))
    }

    manual_exclusions = load_manual_exclusions(config_dir / "manual_exclusions.csv")

    log("[2/7] 应用汇总期排除规则和分类映射")
    if manual_exclusions:
        log(f"      载入人工确认排除项 {len(manual_exclusions)} 条")
    eligible, excluded = preprocess_rows(
        source_rows, config, category_mapper, exclusion_rules, aliases, manual_exclusions
    )
    log(f"      保留 {len(eligible)} 行，排除 {len(excluded)} 行")
    unmapped_source_rows = [row for row in source_rows if row.get("mapping_status") == "unmapped"]
    unmapped = build_unmapped_summary(unmapped_source_rows)
    if config.get("category", {}).get("exclude_unmapped", True):
        log(f"      其中 {len(unmapped_source_rows)} 行分类未映射，仅保留在审计输出")

    log("[3/7] 执行跨商场内部一致性校验")
    eligible, correction_candidates = apply_name_consistency(eligible, config)
    auto_count = sum(item["action"] == "auto_corrected" for item in correction_candidates)
    log(f"      候选 {len(correction_candidates)} 组，自动修正 {auto_count} 组")

    log("[4/7] 执行同商场去重")
    instances, duplicate_audit = deduplicate(eligible, config)
    instances = add_brand_metrics(instances, known_chains)
    log(f"      得到 {len(instances)} 个店铺实例，移除 {len(eligible) - len(instances)} 行重复")

    log("[5/7] 计算汇总指标和质量审计")
    brand_summary = build_brand_summary(instances)
    mall_metrics, category_summary, mall_similarity = build_metrics(
        source_rows, excluded, instances, duplicate_audit, len(category_mapper.level1_values)
    )
    applied_groups: dict[tuple[str, str, str], int] = Counter()
    for row in eligible:
        if row["correction_method"] != "none":
            applied_groups[(text(row.get("merchant_name_normalized")), row["canonical_brand_name"], row["correction_method"])] += 1
    applied = [
        {"source_name": key[0], "canonical_brand_name": key[1], "correction_method": key[2], "affected_row_count": count}
        for key, count in sorted(applied_groups.items())
    ]

    log(f"[6/7] 写入主表：{master_output}")
    log(f"      写入审计结果：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    master_fields = [
        "store_instance_id", "mall_id", "mall_name", "capture_date", "brand_id", "canonical_brand_name",
        "merchant_name_raw", "merchant_name_normalized", "category_raw", "category_l1", "category_l2",
        "mapping_status", "mapping_rule", "rating", "rating_state", "is_new_store",
        "avg_price_yuan", "floor_location", "known_chain",
        "mall_coverage_count", "shared_across_malls", "brand_store_instance_count", "source_record_count",
        "merged_source_record_ids", "merged_card_ids", "merged_name_variants", "merged_floor_candidates",
        "correction_method", "dedup_policy", "ocr_confidence", "title_score", "observations", "review_status",
        "review_reason", "source_video", "evidence_images", "pipeline_version", "rule_version", "result_status"
    ]
    for row in instances:
        row["pipeline_version"] = config["pipeline_version"]
        row["rule_version"] = config["rule_version"]
        row["result_status"] = "current"
    write_csv(master_output, instances, master_fields)
    excluded_fields = [
        "source_record_id", "mall_id", "mall_name", "capture_date", "card_id", "merchant_name_raw",
        "merchant_name_normalized", "category_raw", "category_l1", "category_l2", "mapping_status",
        "rating", "floor_location", "card_type", "rating_status",
        "metadata_text", "processing_exclusion_reason", "source_file", "evidence_image_absolute"
    ]
    write_csv(output_dir / "excluded_records.csv", excluded, excluded_fields)
    duplicate_fields = [
        "mall_id", "mall_name", "brand_id", "canonical_brand_name", "rows_before", "instances_after",
        "duplicates_removed", "known_floors", "dedup_policy", "store_instance_ids", "source_record_ids"
    ]
    write_csv(output_dir / "duplicate_audit.csv", duplicate_audit, duplicate_fields)
    candidate_fields = [
        "variant_name", "reference_name", "edit_distance", "similarity", "variant_mall_count",
        "reference_mall_count", "variant_row_count", "reference_row_count", "category_compatible",
        "auto_eligible", "action"
    ]
    write_csv(output_dir / "name_correction_candidates.csv", correction_candidates, candidate_fields)
    write_csv(output_dir / "name_corrections_applied.csv", applied, ["source_name", "canonical_brand_name", "correction_method", "affected_row_count"])
    write_csv(output_dir / "category_unmapped.csv", unmapped, ["category_raw", "source_record_count", "mall_count", "sample_merchant_names", "suggested_action"])
    write_csv(output_dir / "category_summary.csv", category_summary, ["mall_name", "category_l1", "store_instance_count", "category_ratio", "result_status"])
    write_csv(output_dir / "mall_metrics.csv", mall_metrics, list(mall_metrics[0].keys()))
    write_csv(output_dir / "mall_similarity.csv", mall_similarity, list(mall_similarity[0].keys()))
    write_csv(output_dir / "brand_summary.csv", brand_summary, list(brand_summary[0].keys()))

    manifest = {
        "run_status": "current",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pipeline_name": config["pipeline_name"],
        "pipeline_version": config["pipeline_version"],
        "rule_version": config["rule_version"],
        "config": config,
        "input_files": [{"path": portable_path(path), "sha256": sha256_file(path)} for path in input_files],
        "counts": {
            "malls": len(input_files),
            "source_records": len(source_rows),
            "excluded_records": len(excluded),
            "eligible_before_dedup": len(eligible),
            "final_store_instances": len(instances),
            "duplicate_groups": len(duplicate_audit),
            "name_correction_candidates": len(correction_candidates),
            "name_corrections_applied": len(applied),
            "unmapped_categories": len(unmapped),
            "unmapped_source_records": len(unmapped_source_rows),
        },
        "master_output": str(master_output),
        "outputs": list(OUTPUT_CSV_FILES) + ["BUILD_REPORT.md", "run_manifest.json"],
    }
    create_report(output_dir, source_rows, excluded, instances, duplicate_audit, correction_candidates, unmapped, mall_metrics)
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log("[7/7] 完成：统一主表与审计结果已更新")
    return manifest


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 OCR 商场候选表重建可审计的商户主表")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="JSON 参数配置路径")
    parser.add_argument("--input-root", help="覆盖输入目录")
    parser.add_argument("--output-dir", help="覆盖输出目录")
    parser.add_argument("--master-output", help="覆盖主表输出路径")
    parser.add_argument("--no-deduplicate", action="store_true", help="关闭同商场去重，用于敏感性试验")
    parser.add_argument("--no-auto-correct", action="store_true", help="关闭跨商场名称自动修正")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if args.input_root:
        config["input_root"] = args.input_root
    if args.output_dir:
        config["output_dir"] = args.output_dir
    if args.master_output:
        config["master_output"] = args.master_output
    if args.no_deduplicate:
        config["deduplication"]["enabled"] = False
    if args.no_auto_correct:
        config["name_consistency"]["auto_correct"] = False
    try:
        build(config)
    except Exception as exc:
        log(f"[FAILED] {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
