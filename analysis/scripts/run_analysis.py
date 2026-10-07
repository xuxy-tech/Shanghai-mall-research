"""商场消费生态结构分析。

四条互相印证的证据线：
  1. 嵌套性检验（含等规模稀释控制）——结构是连续还是分化
  2. 原型分析——连续结构下商场是几种极端型的混合
  3. 品牌共址网络——连续性的微观来源
  4. K-Means 对照——用于证明离散分型不成立，而非作为主结论
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "analysis" / "config" / "feature_config.json"

FEATURE_COLUMNS = [
    "standardization_loo",
    "local_only_rate",
    "function_rarity_loo",
    "sitdown_price_log",
    "subculture_index",
    "experiential_share",
]

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


def standardize(matrix: np.ndarray) -> np.ndarray:
    std = matrix.std(0)
    std[std == 0] = 1.0
    return (matrix - matrix.mean(0)) / std


def project_rows_to_simplex(matrix: np.ndarray) -> np.ndarray:
    """把每一行精确投影到概率单纯形（非负且行和为 1）。"""
    values = np.asarray(matrix, dtype=float)
    if values.ndim != 2 or values.shape[1] == 0:
        raise ValueError("单纯形投影需要至少一列的二维矩阵")
    ordered = np.sort(values, axis=1)[:, ::-1]
    cumulative = np.cumsum(ordered, axis=1) - 1.0
    ranks = np.arange(1, values.shape[1] + 1)
    positive = ordered - cumulative / ranks > 0
    rho = positive.sum(axis=1) - 1
    theta = cumulative[np.arange(len(values)), rho] / (rho + 1)
    return np.maximum(values - theta[:, None], 0.0)


def partial_correlations(z: np.ndarray, names: list[str]) -> list[dict[str, Any]]:
    corr = np.corrcoef(z.T)
    precision = np.linalg.inv(corr)
    diag = np.sqrt(np.diag(precision))
    partial = -precision / np.outer(diag, diag)
    np.fill_diagonal(partial, 1.0)
    out = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            marginal = float(corr[i, j])
            direct = float(partial[i, j])
            if abs(direct) > 0.3:
                verdict = "direct"
            elif abs(marginal) > 0.4:
                verdict = "spurious"
            else:
                verdict = "weak"
            out.append(
                {
                    "feature_a": names[i],
                    "feature_b": names[j],
                    "marginal_corr": round(marginal, 4),
                    "partial_corr": round(direct, 4),
                    "verdict": verdict,
                }
            )
    return sorted(out, key=lambda row: -abs(row["partial_corr"]))


def nestedness(
    presence: dict[str, set[str]], malls: list[str], stores: dict[str, list[dict[str, str]]], reps: int = 30
) -> dict[str, Any]:
    """业态构成是嵌套的还是分化的。

    corr(多样性, 所含业态平均普遍度) 强负 = 嵌套：业态多的商场在共有核心上叠加稀有业态。
    但门店数与多样性天然正相关，因此必须做等规模稀释控制，排除规模假象。
    """
    ubiquity: Counter[str] = Counter()
    for mall in malls:
        for category in presence[mall]:
            ubiquity[category] += 1
    diversity = [len(presence[mall]) for mall in malls]
    mean_ubiquity = [statistics.mean(ubiquity[c] for c in presence[mall]) for mall in malls]
    sizes = [len(stores[mall]) for mall in malls]

    smallest = min(sizes)
    rng = random.Random(7)
    controlled = []
    for _ in range(reps):
        sampled = {
            mall: {row["category_l2"] for row in rng.sample(stores[mall], smallest)} for mall in malls
        }
        sub_ubiquity: Counter[str] = Counter()
        for mall in malls:
            for category in sampled[mall]:
                sub_ubiquity[category] += 1
        controlled.append(
            statistics.correlation(
                [len(sampled[mall]) for mall in malls],
                [statistics.mean(sub_ubiquity[c] for c in sampled[mall]) for mall in malls],
            )
        )
    return {
        "corr_diversity_ubiquity": round(statistics.correlation(diversity, mean_ubiquity), 4),
        "corr_size_diversity": round(statistics.correlation(sizes, diversity), 4),
        "rarefied_corr_mean": round(statistics.mean(controlled), 4),
        "rarefied_corr_sd": round(statistics.pstdev(controlled), 4),
        "rarefied_to_n_stores": smallest,
        "rarefaction_reps": reps,
        "diversity_min": min(diversity),
        "diversity_max": max(diversity),
    }


def archetypal(
    z: np.ndarray,
    k: int,
    iterations: int = 1000,
    seed: int = 0,
    restarts: int = 8,
) -> tuple[np.ndarray, np.ndarray, float]:
    """原型分析：找数据云的凸包顶点，把每个样本表示为顶点的凸组合。

    与 K-Means 的区别是原型位于数据边缘而非中心，且样本是混合而非硬分配，
    因此适合连续（嵌套）结构——这类结构里不存在离散簇。
    """
    n = len(z)
    if z.ndim != 2 or not 1 <= k <= n:
        raise ValueError(f"原型数必须在 1..{n} 之间")
    if iterations < 1 or restarts < 1:
        raise ValueError("iterations 与 restarts 必须为正整数")

    # 标准原型分析要求 Z_hat = alpha @ beta @ Z，其中 alpha 与 beta 的每一行
    # 都在概率单纯形上。因此每个原型 beta @ Z 必然位于观测数据的凸包内。
    rng = np.random.default_rng(seed)
    z_spectral_sq = max(float(np.linalg.norm(z, 2) ** 2), 1e-12)
    best: tuple[float, np.ndarray, np.ndarray] | None = None
    for _ in range(restarts):
        selected = rng.choice(n, k, replace=False)
        beta = np.zeros((k, n), dtype=float)
        beta[np.arange(k), selected] = 1.0
        archetypes = beta @ z
        nearest = np.argmin(((z[:, None, :] - archetypes[None, :, :]) ** 2).sum(2), axis=1)
        weights = np.eye(k)[nearest]
        previous = math.inf

        for _ in range(iterations):
            archetypes = beta @ z
            # alpha 更新：给定原型，以凸组合重构每家商场。
            step_alpha = 1.0 / max(2.0 * float(np.linalg.norm(archetypes, 2) ** 2), 1e-12)
            gradient_alpha = 2.0 * (weights @ archetypes - z) @ archetypes.T
            weights = project_rows_to_simplex(weights - step_alpha * gradient_alpha)

            # beta 更新：每个原型本身也必须是所有观测商场的凸组合。
            residual_matrix = weights @ beta @ z - z
            step_beta = 1.0 / max(
                2.0 * float(np.linalg.norm(weights, 2) ** 2) * z_spectral_sq,
                1e-12,
            )
            gradient_beta = 2.0 * weights.T @ residual_matrix @ z.T
            beta = project_rows_to_simplex(beta - step_beta * gradient_beta)

            residual = float(((z - weights @ beta @ z) ** 2).sum())
            if math.isfinite(previous) and abs(previous - residual) <= 1e-9 * max(previous, 1.0):
                break
            previous = residual

        archetypes = beta @ z
        residual = float(((z - weights @ archetypes) ** 2).sum())
        if best is None or residual < best[0]:
            best = (residual, archetypes.copy(), weights.copy())

    assert best is not None
    residual, archetypes, weights = best
    total = ((z - z.mean(0)) ** 2).sum()
    return archetypes, weights, 1 - residual / total if total > 0 else 1.0


def kmeans(z: np.ndarray, k: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centroids = z[rng.choice(len(z), k, replace=False)]
    labels = np.zeros(len(z), dtype=int)
    for _ in range(300):
        labels = np.argmin(((z[:, None] - centroids) ** 2).sum(2), 1)
        updated = np.array(
            [z[labels == i].mean(0) if (labels == i).any() else centroids[i] for i in range(k)]
        )
        if np.allclose(updated, centroids):
            break
        centroids = updated
    return labels


def silhouette(z: np.ndarray, labels: np.ndarray) -> float:
    distances = np.sqrt(((z[:, None] - z) ** 2).sum(2))
    scores = []
    groups = set(labels.tolist())
    if len(groups) < 2:
        return 0.0
    for i in range(len(z)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            continue
        a = distances[i][same].mean()
        b = min(distances[i][labels == g].mean() for g in groups if g != labels[i])
        scores.append((b - a) / max(a, b))
    return float(np.mean(scores)) if scores else 0.0


def colocation_network(
    rows: list[dict[str, str]], malls: list[str], min_malls: int, max_malls: int, min_cooccurrence: int
) -> list[dict[str, Any]]:
    """品牌共址提升度：分析单元下移到品牌对，绕开商场样本量限制。"""
    brand_malls: dict[str, set[str]] = defaultdict(set)
    names: dict[str, str] = {}
    for row in rows:
        brand_malls[row["brand_id"]].add(row["mall_name"])
        names[row["brand_id"]] = row["canonical_brand_name"]
    selected = [b for b, v in brand_malls.items() if min_malls <= len(v) <= max_malls]
    total = len(malls)
    index = {mall: i for i, mall in enumerate(malls)}
    matrix = np.zeros((total, len(selected)))
    for j, brand in enumerate(selected):
        for mall in brand_malls[brand]:
            matrix[index[mall], j] = 1
    cooccurrence = matrix.T @ matrix
    prevalence = matrix.mean(0)
    expected = np.outer(prevalence, prevalence) * total
    out = []
    for i in range(len(selected)):
        for j in range(i + 1, len(selected)):
            observed = cooccurrence[i, j]
            if observed < min_cooccurrence or expected[i, j] <= 0:
                continue
            out.append(
                {
                    "brand_a": names[selected[i]],
                    "brand_b": names[selected[j]],
                    "malls_a": int(matrix[:, i].sum()),
                    "malls_b": int(matrix[:, j].sum()),
                    "cooccurrence": int(observed),
                    "lift": round(float(observed / expected[i, j]), 4),
                }
            )
    return sorted(out, key=lambda row: -row["lift"])


def run(config: dict[str, Any], archetype_k: int) -> dict[str, Any]:
    output_dir = resolve(config["output_dir"])
    features = read_csv(output_dir / "mall_features.csv")
    rows = read_csv(resolve(config["master_input"]))
    malls = [row["mall_name"] for row in features]
    by_mall: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_mall[row["mall_name"]].append(row)
    presence = {mall: {row["category_l2"] for row in by_mall[mall]} for mall in malls}

    usable = [c for c in FEATURE_COLUMNS if all(row.get(c, "") != "" for row in features)]
    if len(usable) < len(FEATURE_COLUMNS):
        log(f"      注意：{set(FEATURE_COLUMNS) - set(usable)} 存在缺失值，已排除")
    matrix = np.array([[float(row[c]) for c in usable] for row in features])
    z = standardize(matrix)
    log(f"[1/5] 特征矩阵 {z.shape[0]} 商场 × {z.shape[1]} 维：{', '.join(usable)}")

    log("[2/5] 嵌套性检验（含等规模稀释控制）")
    nested = nestedness(presence, malls, by_mall)
    log(f"      corr(多样性, 平均普遍度) = {nested['corr_diversity_ubiquity']}")
    log(
        f"      等规模稀释至 {nested['rarefied_to_n_stores']} 店后 = "
        f"{nested['rarefied_corr_mean']} (sd {nested['rarefied_corr_sd']})"
    )

    log("[3/5] 相关结构：边际 vs 偏相关")
    partial = partial_correlations(z, usable)
    spurious = sum(1 for row in partial if row["verdict"] == "spurious")
    log(f"      {len(partial)} 对中 {spurious} 对为伪相关（控制其余维度后消失）")

    log(f"[4/5] 原型分析 k={archetype_k}")
    archetypes, weights, explained = archetypal(z, archetype_k, seed=1)
    log(f"      解释方差 {explained:.1%}")
    pure = int((weights.max(1) > 0.6).sum())
    log(f"      接近纯型 {pure}/{len(malls)}，混合型 {len(malls) - pure}")

    archetype_rows = []
    for a in range(archetype_k):
        profile = {usable[d]: round(float(archetypes[a, d]), 4) for d in range(len(usable))}
        top = [malls[i] for i in np.argsort(-weights[:, a])[:6]]
        archetype_rows.append(
            {"archetype": f"A{a}", "purest_malls": " | ".join(top), **profile}
        )
    mixture_rows = [
        {
            "mall_name": malls[i],
            **{f"w_A{a}": round(float(weights[i, a]), 4) for a in range(archetype_k)},
            "dominant": f"A{int(np.argmax(weights[i]))}",
            "max_weight": round(float(weights[i].max()), 4),
            "is_mixed": bool(weights[i].max() <= 0.6),
        }
        for i in range(len(malls))
    ]

    log("[5/5] K-Means 对照与品牌共址网络")
    cluster_rows = []
    for k in range(2, 7):
        best = max(((silhouette(z, kmeans(z, k, s)), s) for s in range(25)), key=lambda t: t[0])
        labels = kmeans(z, k, best[1])
        cluster_rows.append(
            {
                "k": k,
                "silhouette": round(best[0], 4),
                "cluster_sizes": " | ".join(str(v) for v in sorted(Counter(labels.tolist()).values(), reverse=True)),
            }
        )
    best_sil = max(row["silhouette"] for row in cluster_rows)
    log(f"      K-Means 最佳轮廓系数 {best_sil}（<0.5 表示无清晰离散分型）")
    network = colocation_network(rows, malls, 4, 30, 5)
    log(f"      品牌共址对 {len(network)} 组")

    write_csv(output_dir / "partial_correlations.csv", partial, list(partial[0].keys()))
    write_csv(output_dir / "archetype_profiles.csv", archetype_rows, list(archetype_rows[0].keys()))
    write_csv(output_dir / "archetype_mixtures.csv", mixture_rows, list(mixture_rows[0].keys()))
    write_csv(output_dir / "kmeans_reference.csv", cluster_rows, list(cluster_rows[0].keys()))
    write_csv(output_dir / "brand_colocation.csv", network[:400], list(network[0].keys()))

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "features_used": usable,
        "mall_count": len(malls),
        "nestedness": nested,
        "archetype": {
            "k": archetype_k,
            "explained_variance": round(explained, 4),
            "pure_malls": pure,
            "mixed_malls": len(malls) - pure,
        },
        "kmeans_reference": cluster_rows,
        "spurious_pairs": spurious,
        "colocation_pairs": len(network),
    }
    (output_dir / "analysis_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"      结果写入 {output_dir}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="商场消费生态结构分析")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--archetypes", type=int, default=3)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv or sys.argv[1:])
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if args.output_dir:
        config["output_dir"] = args.output_dir
    try:
        run(config, args.archetypes)
    except Exception as exc:  # noqa: BLE001
        log(f"[FAILED] {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
