"""品牌身份定义的敏感性分析。

74.8% 的品牌只出现在一家商场。这个比例既包含真实独有品牌，也包含中英文名未统一、
OCR 名称碎片、样本只覆盖到一个门店、以及点评收录差异。因此"品牌 Jaccard 均值 0.040"
不能全部解释为商业独特性。

本脚本在三种品牌身份定义和两种单例处理下重算相似度，给出结论的稳健区间。
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUFFIX_PATTERN = re.compile(
    r"(专卖店|专柜|旗舰店|体验店|授权体验店|官方授权体验店|服务体验中心|指定店"
    r"|时尚店|传承|女装店|男装店|集合店|概念店|臻选|店)$"
)


def log(message: str) -> None:
    print(message, flush=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def identity_strict(row: dict[str, str]) -> str:
    """现行口径：去符号后的完整名称。"""
    return row["brand_id"]


def identity_latin_core(row: dict[str, str]) -> str:
    """拉丁字母主干相同即视为同一品牌，用于合并中英文名并存的情况。"""
    latin = "".join(re.findall(r"[A-Za-z0-9]+", row["canonical_brand_name"])).lower()
    return f"LAT:{latin}" if len(latin) >= 4 else row["brand_id"]


def identity_strip_suffix(row: dict[str, str]) -> str:
    """再去掉店型后缀与括号补注，最激进的合并。"""
    name = re.sub(r"[（(].*?[）)]", "", row["canonical_brand_name"])
    name = SUFFIX_PATTERN.sub("", name)
    latin = "".join(re.findall(r"[A-Za-z0-9]+", name)).lower()
    if len(latin) >= 4:
        return f"LAT:{latin}"
    key = "".join(ch for ch in name if ch.isalnum()).lower()
    return f"K:{key}" if key else row["brand_id"]


DEFINITIONS: list[tuple[str, Callable[[dict[str, str]], str]]] = [
    ("strict_current", identity_strict),
    ("latin_core_merged", identity_latin_core),
    ("strip_suffix_merged", identity_strip_suffix),
]


def weak_singletons_for_identity(
    rows: list[dict[str, str]], identity: Callable[[dict[str, str]], str]
) -> set[str]:
    """按当前品牌身份口径识别证据薄弱的单例，而不是复用严格 ID。"""
    brand_malls: dict[str, set[str]] = defaultdict(set)
    observations: dict[str, int] = defaultdict(int)
    for row in rows:
        key = identity(row)
        brand_malls[key].add(row["mall_name"])
        try:
            value = int(row.get("observations") or 0)
        except ValueError:
            value = 0
        observations[key] = max(observations[key], value)
    return {
        brand for brand, mall_set in brand_malls.items()
        if len(mall_set) == 1 and observations[brand] <= 1
    }


def evaluate(
    rows: list[dict[str, str]], malls: list[str], identity: Callable[[dict[str, str]], str], drop: set[str]
) -> dict[str, Any]:
    brand_malls: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        key = identity(row)
        if key in drop:
            continue
        brand_malls[key].add(row["mall_name"])
    by_mall = {
        mall: {identity(row) for row in rows if row["mall_name"] == mall and identity(row) not in drop}
        for mall in malls
    }
    jaccard = [
        len(by_mall[a] & by_mall[b]) / len(by_mall[a] | by_mall[b])
        for a, b in itertools.combinations(malls, 2)
        if by_mall[a] and by_mall[b]
    ]
    singletons = sum(1 for v in brand_malls.values() if len(v) == 1)
    return {
        "brand_count": len(brand_malls),
        "singleton_count": singletons,
        "singleton_share": round(singletons / len(brand_malls), 4),
        "jaccard_mean": round(statistics.mean(jaccard), 4),
        "jaccard_median": round(statistics.median(jaccard), 4),
        "jaccard_max": round(max(jaccard), 4),
    }


def structural_similarity(rows: list[dict[str, str]], malls: list[str]) -> dict[str, float]:
    categories = sorted({row["category_l2"] for row in rows})
    vectors = {}
    for mall in malls:
        counts = Counter(row["category_l2"] for row in rows if row["mall_name"] == mall)
        total = sum(counts.values())
        vectors[mall] = [counts.get(c, 0) / total for c in categories]

    def cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
        return dot / norm if norm else 0.0

    values = [cosine(vectors[a], vectors[b]) for a, b in itertools.combinations(malls, 2)]
    return {"cosine_mean": round(statistics.mean(values), 4), "cosine_max": round(max(values), 4)}


def run(master: Path, output_dir: Path) -> dict[str, Any]:
    rows = read_csv(master)
    malls = sorted({row["mall_name"] for row in rows})
    log(f"读取 {len(rows)} 行、{len(malls)} 家商场")

    results = []
    weak_counts: dict[str, int] = {}
    for label, identity in DEFINITIONS:
        weak_singletons = weak_singletons_for_identity(rows, identity)
        weak_counts[label] = len(weak_singletons)
        log(f"{label} 口径的证据薄弱单例：{len(weak_singletons)}")
        for drop_label, drop in (("keep_all", set()), ("drop_weak_singletons", weak_singletons)):
            metrics = evaluate(rows, malls, identity, drop)
            results.append(
                {
                    "brand_identity": label,
                    "singleton_policy": drop_label,
                    "weak_singletons_in_definition": len(weak_singletons),
                    **metrics,
                }
            )
            log(
                f"  {label:<20}{drop_label:<22}"
                f"brands={metrics['brand_count']:<6}singleton={metrics['singleton_share']:.1%}  "
                f"jaccard={metrics['jaccard_mean']:.4f}"
            )

    structural = structural_similarity(rows, malls)
    jaccards = [row["jaccard_mean"] for row in results]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "master_input": portable_path(master),
        "mall_count": len(malls),
        "weak_singleton_count_by_identity": weak_counts,
        "jaccard_range": [min(jaccards), max(jaccards)],
        "structural_cosine": structural,
        "structural_to_brand_ratio": round(structural["cosine_mean"] / max(jaccards), 1),
        "conclusion": (
            "品牌 Jaccard 在所有归一化与单例处理组合下均落在 "
            f"{min(jaccards)}–{max(jaccards)}，而业态结构余弦为 {structural['cosine_mean']}。"
            "'品牌层面不重叠、结构层面雷同'的结论对品牌身份定义不敏感；"
            "但单例品牌比例本身受命名与覆盖度影响，不应单独解读为商业独特性。"
        ),
    }
    write_csv(output_dir / "brand_sensitivity.csv", results, list(results[0].keys()))
    (output_dir / "brand_sensitivity.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log("")
    log(f"品牌 Jaccard 区间 {min(jaccards)}–{max(jaccards)}；业态结构余弦 {structural['cosine_mean']}")
    log(f"结构/品牌 相似度倍数 {summary['structural_to_brand_ratio']}x")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="品牌身份定义敏感性分析")
    parser.add_argument("--master", default="data/master_stores.csv")
    parser.add_argument("--output-dir", default="analysis/outputs/current")
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        run(PROJECT_ROOT / args.master, PROJECT_ROOT / args.output_dir)
    except Exception as exc:  # noqa: BLE001
        log(f"[FAILED] {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
