"""从商户主表构建商场特征表。

主表只读。所有跨商场指标使用留一法定义，使其不随样本量系统漂移。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name
DEFAULT_CONFIG = PROJECT_ROOT / "analysis" / "config" / "feature_config.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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


def shannon_entropy(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    probabilities = [count / total for count in counts if count > 0]
    return -sum(p * math.log(p) for p in probabilities)


def build(config: dict[str, Any]) -> dict[str, Any]:
    master_path = resolve(config["master_input"])
    output_dir = resolve(config["output_dir"])
    rows = read_csv(master_path)
    if not rows:
        raise RuntimeError(f"主表为空：{master_path}")

    malls = sorted({row["mall_name"] for row in rows})
    total_malls = len(malls)
    by_mall: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_mall[row["mall_name"]].append(row)

    log(f"[1/4] 读取主表：{len(rows)} 行，{total_malls} 家商场")

    brand_malls: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        brand_malls[row["brand_id"]].add(row["mall_name"])
    category_malls: dict[str, set[str]] = defaultdict(set)
    for mall in malls:
        for category in {row["category_l2"] for row in by_mall[mall]}:
            category_malls[category].add(mall)

    subculture = set(config["subculture_categories"])
    light = set(config["light_consumption_categories"])
    sitdown = set(config["sitdown_dining_categories"])
    experiential = set(config["experiential_categories"])
    price_cfg = config["price"]
    head_min = int(config["chain"]["head_chain_min_malls"])

    metadata: dict[str, dict[str, str]] = {}
    metadata_setting = str(config.get("metadata_input", "")).strip()
    metadata_path = resolve(metadata_setting) if metadata_setting else None
    if metadata_path is not None and metadata_path.is_file():
        metadata = {row["mall_name"]: row for row in read_csv(metadata_path)}
        log(f"      合并区位元数据：{len(metadata)} 家")

    log("[2/4] 计算留一法跨商场指标")
    features: list[dict[str, Any]] = []
    for mall in malls:
        stores = by_mall[mall]
        count = len(stores)
        level2 = Counter(row["category_l2"] for row in stores)
        level1 = Counter(row["category_l1"] for row in stores)
        present = set(level2)

        # 留一法：品牌在"其余 N-1 家"中的出现比例，不含本商场自身
        others = total_malls - 1
        cooccurrence = statistics.mean(
            len(brand_malls[row["brand_id"]] - {mall}) / others for row in stores
        ) if others > 0 else 0.0
        local_only = sum(1 for row in stores if len(brand_malls[row["brand_id"]] - {mall}) == 0) / count
        head_chain = sum(
            1 for row in stores if len(brand_malls[row["brand_id"]] - {mall}) >= head_min - 1
        ) / count
        # 功能稀有度同样留一，避免本商场自身抬高其所含业态的普遍度
        rarity = statistics.mean(
            math.log(total_malls / max(len(category_malls[category] - {mall}), 1))
            for category in present
        )

        prices = []
        for row in stores:
            if row["category_l2"] not in sitdown:
                continue
            raw = row["avg_price_yuan"].strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if price_cfg["min_yuan"] <= value <= price_cfg["max_yuan"]:
                prices.append(value)
        price_median = statistics.median(prices) if len(prices) >= price_cfg["min_samples_per_mall"] else None
        price_dispersion = (
            statistics.pstdev(prices) / statistics.mean(prices)
            if len(prices) >= price_cfg["min_samples_per_mall"] and statistics.mean(prices) > 0
            else None
        )

        ratings = [float(row["rating"]) for row in stores if row["rating"].strip()]
        entropy = shannon_entropy(list(level1.values()))
        meta = metadata.get(mall, {})
        features.append(
            {
                "mall_name": mall,
                "store_count": count,
                "brand_count": len({row["brand_id"] for row in stores}),
                "l2_breadth": len(present),
                "standardization_loo": round(cooccurrence, 6),
                "head_chain_rate": round(head_chain, 6),
                "local_only_rate": round(local_only, 6),
                "function_rarity_loo": round(rarity, 6),
                "subculture_index": round(sum(level2.get(k, 0) for k in subculture) / count, 6),
                "light_consumption_index": round(sum(level2.get(k, 0) for k in light) / count, 6),
                "experiential_share": round(sum(level2.get(k, 0) for k in experiential) / count, 6),
                "dining_share": round(level1.get("美食", 0) / count, 6),
                "retail_share": round(level1.get("购物", 0) / count, 6),
                "sitdown_price_median": round(price_median, 2) if price_median else "",
                "sitdown_price_log": round(math.log(price_median), 6) if price_median else "",
                "sitdown_price_sample": len(prices),
                "price_dispersion": round(price_dispersion, 6) if price_dispersion else "",
                "mean_rating": round(statistics.mean(ratings), 4) if ratings else "",
                "category_entropy_l1": round(entropy, 6),
                "district": meta.get("district", ""),
                "business_area": meta.get("business_area", ""),
                "longitude": meta.get("longitude", ""),
                "latitude": meta.get("latitude", ""),
                "opening_year": meta.get("opening_year", ""),
                "result_status": "current",
            }
        )

    log("[3/4] 生成业态普遍度表")
    ubiquity = [
        {
            "category_l2": category,
            "mall_count": len(mall_set),
            "ubiquity_ratio": round(len(mall_set) / total_malls, 6),
            "store_count": sum(1 for row in rows if row["category_l2"] == category),
        }
        for category, mall_set in sorted(category_malls.items(), key=lambda item: -len(item[1]))
    ]

    log(f"[4/4] 写入：{output_dir}")
    feature_fields = list(features[0].keys())
    write_csv(output_dir / "mall_features.csv", features, feature_fields)
    write_csv(output_dir / "category_ubiquity.csv", ubiquity, list(ubiquity[0].keys()))

    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config_version": config["config_version"],
        "master_input": portable_path(master_path),
        "counts": {"malls": total_malls, "stores": len(rows), "brands": len(brand_malls)},
        "metadata_merged": len(metadata),
        "feature_columns": feature_fields,
    }
    (output_dir / "feature_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"      完成：{len(features)} 家商场 × {len(feature_fields)} 列")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建商场特征表")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir")
    parser.add_argument("--metadata-input")
    args = parser.parse_args(argv or sys.argv[1:])
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if args.output_dir:
        config["output_dir"] = args.output_dir
    if args.metadata_input:
        config["metadata_input"] = args.metadata_input
    try:
        build(config)
    except Exception as exc:  # noqa: BLE001
        log(f"[FAILED] {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
