"""抓取商场区位元数据（高德 Web 服务）。

并发严格限制为 3，且每个请求之间保持最小间隔——高德对超频调用会封禁 Key。
结果可增量重建：已抓到的商场默认跳过，用 --force 重抓。

高德 POI 接口不返回开业年份与商业面积，这两列留空，由 seed_opening_years.csv 补充。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AMAP_ENDPOINT = "https://restapi.amap.com/v3/place/text"
MAX_CONCURRENCY = 3          # 高德并发上限，不要调高
MIN_REQUEST_INTERVAL = 0.35  # 秒；配合并发 3 约等于 8-9 QPS 以下

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def log(message: str) -> None:
    print(message, flush=True)


def resolve(value: str) -> Path:
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
        writer.writerows(rows)


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def throttled_get(url: str, timeout: float = 20.0) -> dict[str, Any]:
    """全局节流：无论哪个线程发起请求，都保证最小间隔。"""
    global _last_request_at
    with _throttle_lock:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_for_match(value: str) -> str:
    """归一化后比较：去掉城市前缀、括号补注、出入口/门等后缀和所有非字母数字字符。"""
    text = str(value)
    # "万达广场(上海崇明店)" 这类括号里带的是地名限定，要提到前面而不是丢弃，
    # 否则 61 家里的"崇明万达广场""金山万达广场"会全部退化成同一个"万达广场"。
    text = re.sub(
        r"^(.*?)[（(]上海([一-鿿]{2,3})[店馆][）)]",
        lambda m: f"{m.group(2)}{m.group(1)}",
        text,
    )
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"^上海市?", "", text)
    text = re.sub(r"(上海市)?[一-鿿]{2,3}区", "", text)
    text = re.sub(r"(出入口|[东南西北]?\d*门|[A-Z]区|停车场|购物中心|商场|广场)$", "", text)
    return "".join(ch.lower() for ch in text if ch.isalnum())


def match_quality(mall_name: str, poi_name: str) -> int:
    """0=不可接受 1=弱匹配 2=包含匹配 3=完全一致。"""
    a, b = normalize_for_match(mall_name), normalize_for_match(poi_name)
    if not a or not b:
        return 0
    if a == b:
        return 3
    if a in b or b in a:
        # 过短的包含容易误配（"新天地"匹配到"新邻天地"），要求有足够重叠长度
        return 2 if min(len(a), len(b)) >= 3 else 0
    # 弱匹配：允许少量插入/删除（"瑞虹天地"vs"瑞虹新天地"、"崇明万达广场"vs"万达广场上海崇明店"）
    # 要求较短串的字符几乎全部出现在较长串中且顺序一致
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4:
        position = 0
        matched = 0
        for char in short:
            found = long.find(char, position)
            if found >= 0:
                matched += 1
                position = found + 1
        if matched / len(short) >= 0.9:
            return 1
    return 0


def pick_best_poi(pois: list[dict[str, Any]], mall_name: str) -> dict[str, Any] | None:
    """只接受名称可匹配的购物中心类 POI；宁可返回 None 也不接受错配。"""
    candidates = []
    for poi in pois:
        quality = match_quality(mall_name, str(poi.get("name", "")))
        if quality == 0:
            continue
        is_mall = 1 if str(poi.get("typecode", "")).startswith("0601") else 0
        has_district = 1 if str(poi.get("adname", "")) not in ("", "[]") else 0
        candidates.append(((quality, is_mall, has_district), poi))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def fetch_one(
    mall_name: str,
    key: str,
    city: str,
    aliases: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    alias = (aliases or {}).get(mall_name, {})
    keyword = alias.get("search_keyword") or mall_name
    expected_district = alias.get("expected_district", "").strip()
    query = urllib.parse.urlencode(
        {
            "key": key,
            "keywords": keyword,
            "city": city,
            "citylimit": "true",
            "types": "060100",  # 商场类，含购物中心子类
            "offset": "10",
            "page": "1",
            "extensions": "all",
        }
    )
    record: dict[str, Any] = {
        "mall_name": mall_name,
        "amap_status": "",
        "amap_name": "",
        "amap_id": "",
        "district": "",
        "business_area": "",
        "address": "",
        "longitude": "",
        "latitude": "",
        "typecode": "",
        "amap_rating": "",
        "opening_year": "",
        "commercial_area_sqm": "",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        payload = throttled_get(f"{AMAP_ENDPOINT}?{query}")
    except Exception as exc:  # noqa: BLE001
        record["amap_status"] = f"request_error:{type(exc).__name__}"
        return record
    if str(payload.get("status")) != "1":
        record["amap_status"] = f"api_error:{payload.get('info', 'unknown')}"
        return record
    pois = payload.get("pois") or []
    if expected_district:
        # 限定行政区，避免匹配到同名的其它项目（如宝山区亦有"新天地"）
        filtered = [p for p in pois if str(p.get("adname", "")) == expected_district]
        pois = filtered or pois
    poi = pick_best_poi(pois, keyword)
    if not poi:
        record["amap_status"] = "no_match"
        return record
    if expected_district and str(poi.get("adname", "")) not in ("", "[]", expected_district):
        record["amap_status"] = f"district_mismatch:{poi.get('adname')}"
        return record
    location = str(poi.get("location", ""))
    longitude, _, latitude = location.partition(",")
    business = poi.get("business_area")
    biz_ext = poi.get("biz_ext") or {}
    rating = biz_ext.get("rating") if isinstance(biz_ext, dict) else ""
    record.update(
        {
            "amap_status": "ok",
            "amap_name": str(poi.get("name", "")),
            "amap_id": str(poi.get("id", "")),
            "district": str(poi.get("adname", "")),
            "business_area": business if isinstance(business, str) else "",
            "address": str(poi.get("address", "")),
            "longitude": longitude,
            "latitude": latitude,
            "typecode": str(poi.get("typecode", "")),
            "amap_rating": rating if isinstance(rating, str) else "",
        }
    )
    return record


def merge_seed_years(records: list[dict[str, Any]], seed_path: Path) -> int:
    """合并人工/检索整理的开业年份。高德接口不提供该字段。"""
    if not seed_path.exists():
        return 0
    seed = {}
    for row in read_csv(seed_path):
        name = row.get("mall_name", "").strip()
        if not name:
            continue
        # active=FALSE 用于保留低可信度的检索结果供人工复核，但不参与分析
        if str(row.get("active", "TRUE")).strip().upper() not in {"1", "TRUE", "YES", "Y"}:
            continue
        seed[name] = row
    merged = 0
    for record in records:
        row = seed.get(record["mall_name"])
        if not row:
            continue
        year = (row.get("opening_year") or "").strip()
        area = (row.get("commercial_area_sqm") or "").strip()
        if year and re.fullmatch(r"(19|20)\d{2}", year):
            record["opening_year"] = year
            merged += 1
        if area:
            record["commercial_area_sqm"] = area
    return merged


def run(args: argparse.Namespace) -> int:
    env = load_env(PROJECT_ROOT / ".env")
    key = (env.get("AMAP_WEB_SERVICE_KEY") or "").strip()
    if not key:
        log("[FAILED] .env 中缺少 AMAP_WEB_SERVICE_KEY；请复制 .env.example 为 .env 并填入")
        return 1

    master = read_csv(resolve(args.master))
    malls = sorted({row["mall_name"] for row in master})
    output_path = resolve(args.output)

    existing: dict[str, dict[str, Any]] = {}
    if output_path.exists() and not args.force:
        existing = {
            row["mall_name"]: row
            for row in read_csv(output_path)
            if row.get("amap_status") == "ok"
        }
    pending = [mall for mall in malls if mall not in existing]
    log(f"共 {len(malls)} 家商场；已有 {len(existing)} 家，待抓取 {len(pending)} 家")
    log(f"并发上限 {MAX_CONCURRENCY}，最小请求间隔 {MIN_REQUEST_INTERVAL}s（高德防封限制）")

    alias_path = resolve(args.aliases)
    aliases: dict[str, dict[str, str]] = {}
    if alias_path.exists():
        aliases = {
            row["mall_name"]: row
            for row in read_csv(alias_path)
            if str(row.get("active", "")).strip().upper() in {"1", "TRUE", "YES", "Y"}
        }
        log(f"载入检索别名 {len(aliases)} 条")

    results: list[dict[str, Any]] = []
    if pending:
        done = 0
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
            for record in pool.map(lambda name: fetch_one(name, key, args.city, aliases), pending):
                results.append(record)
                done += 1
                flag = "ok" if record["amap_status"] == "ok" else record["amap_status"]
                log(f"  [{done}/{len(pending)}] {record['mall_name']} -> {flag}")

    combined = list(existing.values()) + results
    order = {mall: index for index, mall in enumerate(malls)}
    combined.sort(key=lambda row: order.get(row["mall_name"], 9999))

    merged_years = merge_seed_years(combined, resolve(args.seed))
    fields = [
        "mall_name", "amap_status", "amap_name", "amap_id", "district", "business_area",
        "address", "longitude", "latitude", "typecode", "amap_rating",
        "opening_year", "commercial_area_sqm", "fetched_at",
    ]
    write_csv(output_path, combined, fields)
    ok = sum(1 for row in combined if row.get("amap_status") == "ok")
    log("")
    log(f"写入 {output_path}")
    log(f"  成功 {ok}/{len(combined)}；合并开业年份 {merged_years} 家")
    failed = [row["mall_name"] for row in combined if row.get("amap_status") != "ok"]
    if failed:
        log(f"  未命中：{', '.join(failed)}")
    if merged_years < len(combined):
        log(f"  提示：高德不提供开业年份，可在 {resolve(args.seed)} 中补充")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取商场区位元数据（高德，并发上限 3）")
    parser.add_argument("--master", default="data/master_stores.csv")
    parser.add_argument("--output", default="analysis/outputs/current/tables/core/mall_metadata.csv")
    parser.add_argument("--seed", default="analysis/config/seed_opening_years.csv")
    parser.add_argument("--aliases", default="analysis/config/mall_search_aliases.csv")
    parser.add_argument("--city", default="上海")
    parser.add_argument("--force", action="store_true", help="忽略已有结果，全部重抓")
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[FAILED] {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
