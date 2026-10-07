"""成分数据分析：商场 × 二级业态计数矩阵的潜在因子结构。

门店计数是成分数据（各业态占比之和恒为 1），直接对占比做 PCA 或欧氏距离在数学上不成立。
本脚本从原始计数矩阵出发，让因子形状由数据决定；人工构造的指标（标准化程度、亚文化指数等）
只用于事后解释因子，不参与因子估计——避免"用假设推出假设"。

流程：
  1. 商场 × 业态计数矩阵
  2. CLR-PCA（透明基线，含零值替代敏感性检验）
  3. Aitchison 距离 + 零模型对照，检验是否存在自然分组
  4. 贝叶斯 logistic-normal multinomial，K=1..4
  5. 留出预测 / 后验预测检查 / bootstrap 载荷稳定性，选择因子数
  6. 因子载荷与命名
  7. 因子得分与外部变量（区位、开业年份、人工指标）的事后关联
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI_CENTRE = (121.4737, 31.2304)  # 人民广场

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    print(message, flush=True)


def configure_windows_compiler_paths() -> None:
    """补上 conda-forge UCRT 工具链在 Windows 下遗漏的链接搜索目录。"""
    if os.name != "nt":
        return
    sysroot_lib = (
        Path(sys.prefix) / "Library" / "x86_64-w64-mingw32" / "sysroot" / "usr" / "lib"
    )
    if not sysroot_lib.is_dir():
        return
    current = os.environ.get("LIBRARY_PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    if str(sysroot_lib) not in entries:
        os.environ["LIBRARY_PATH"] = os.pathsep.join([str(sysroot_lib), *entries])


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_matrix(rows: list[dict[str, str]]) -> tuple[np.ndarray, list[str], list[str]]:
    malls = sorted({row["mall_name"] for row in rows})
    categories = sorted({row["category_l2"] for row in rows})
    mall_index = {m: i for i, m in enumerate(malls)}
    category_index = {c: j for j, c in enumerate(categories)}
    matrix = np.zeros((len(malls), len(categories)))
    for row in rows:
        matrix[mall_index[row["mall_name"]], category_index[row["category_l2"]]] += 1
    return matrix, malls, categories


def clr(matrix: np.ndarray, eps: float = 0.5) -> np.ndarray:
    """中心对数比变换：除以各行几何平均后取对数，把成分数据送入欧氏空间。"""
    proportions = matrix + eps
    proportions = proportions / proportions.sum(1, keepdims=True)
    logs = np.log(proportions)
    return logs - logs.mean(1, keepdims=True)


def great_circle_km(lon: float, lat: float) -> float:
    dx = (lon - SHANGHAI_CENTRE[0]) * 111.32 * math.cos(math.radians(lat))
    dy = (lat - SHANGHAI_CENTRE[1]) * 110.57
    return math.sqrt(dx * dx + dy * dy)


def pearson(a: list[float], b: list[float]) -> float:
    x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(x) < 3 or x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


# ---------------------------------------------------------------- 步骤 3: CLR-PCA

def clr_pca(matrix: np.ndarray, categories: list[str]) -> dict[str, Any]:
    """CLR-PCA 基线，并检验结果是否依赖零值替代值的选择。"""
    reference = None
    sensitivity = []
    for eps in (1.0, 0.5, 0.3, 0.1, 0.05):
        centred = clr(matrix, eps)
        centred = centred - centred.mean(0)
        u, s, vt = np.linalg.svd(centred, full_matrices=False)
        explained = s**2 / np.sum(s**2)
        scores = u[:, :3] * s[:3]
        row = {
            "zero_replacement_eps": eps,
            "pc1_explained": round(float(explained[0]), 4),
            "pc2_explained": round(float(explained[1]), 4),
            "pc3_explained": round(float(explained[2]), 4),
            "cumulative_3": round(float(explained[:3].sum()), 4),
            "pc1_top_loadings": " | ".join(
                categories[j] for j in np.argsort(-np.abs(vt[0]))[:4]
            ),
        }
        if reference is None:
            reference = scores
            row["pc1_corr_vs_eps1"] = 1.0
            row["pc2_corr_vs_eps1"] = 1.0
        else:
            row["pc1_corr_vs_eps1"] = round(abs(pearson(reference[:, 0], scores[:, 0])), 4)
            row["pc2_corr_vs_eps1"] = round(abs(pearson(reference[:, 1], scores[:, 1])), 4)
        sensitivity.append(row)
    return {"sensitivity": sensitivity}


# ------------------------------------------------- 步骤 4: Aitchison 距离与分组检验

def kmedoids(distance: np.ndarray, k: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    medoids = list(rng.choice(len(distance), k, replace=False))
    for _ in range(100):
        labels = np.argmin(distance[:, medoids], 1)
        updated = []
        for cluster in range(k):
            members = np.where(labels == cluster)[0]
            if len(members) == 0:
                updated.append(medoids[cluster])
            else:
                inner = distance[np.ix_(members, members)].sum(1)
                updated.append(int(members[np.argmin(inner)]))
        if set(updated) == set(medoids):
            break
        medoids = updated
    return np.argmin(distance[:, medoids], 1)


def silhouette_from_distance(distance: np.ndarray, labels: np.ndarray) -> float:
    groups = set(labels.tolist())
    if len(groups) < 2:
        return 0.0
    scores = []
    for i in range(len(distance)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            continue
        a = distance[i][same].mean()
        b = min(distance[i][labels == g].mean() for g in groups if g != labels[i])
        scores.append((b - a) / max(a, b))
    return float(np.mean(scores)) if scores else 0.0


def aitchison_grouping(matrix: np.ndarray, reps: int = 30) -> dict[str, Any]:
    """检验商场间差异是否超过抽样噪声，以及是否形成离散分组。

    零模型：保持各商场门店总数与全局业态边际不变，重新多项抽样。
    若实测距离的变异系数显著高于零模型，说明商场间存在真实的结构差异。
    """
    centred = clr(matrix)
    distance = np.sqrt(((centred[:, None] - centred) ** 2).sum(2))
    n = len(matrix)
    upper = np.triu_indices(n, 1)
    observed_cv = float(distance[upper].std() / distance[upper].mean())

    rng = np.random.default_rng(0)
    marginal = matrix.sum(0) / matrix.sum()
    null_cv = []
    for _ in range(reps):
        sampled = np.array(
            [rng.multinomial(int(matrix[i].sum()), marginal) for i in range(n)], dtype=float
        )
        null_centred = clr(sampled)
        null_distance = np.sqrt(((null_centred[:, None] - null_centred) ** 2).sum(2))
        null_cv.append(null_distance[upper].std() / null_distance[upper].mean())
    null_mean, null_sd = float(np.mean(null_cv)), float(np.std(null_cv))

    silhouettes = {}
    for k in range(2, 7):
        silhouettes[k] = round(
            max(silhouette_from_distance(distance, kmedoids(distance, k, s)) for s in range(30)), 4
        )
    return {
        "distance_mean": round(float(distance[upper].mean()), 4),
        "distance_cv": round(observed_cv, 4),
        "null_cv_mean": round(null_mean, 4),
        "null_cv_sd": round(null_sd, 4),
        "cv_z_score": round((observed_cv - null_mean) / null_sd, 1) if null_sd else None,
        "structure_exceeds_sampling_noise": bool(observed_cv > null_mean + 3 * null_sd),
        "kmedoids_silhouette": silhouettes,
        "best_silhouette": max(silhouettes.values()),
        "discrete_groups_supported": bool(max(silhouettes.values()) >= 0.5),
    }, distance


# ------------------------------------------------------------ 步骤 5: 因子数选择

def parallel_analysis(matrix: np.ndarray, reps: int = 200) -> dict[str, Any]:
    """平行分析：与逐列置换的零模型比较特征值，判断哪些主成分超出偶然水平。"""
    centred = clr(matrix)
    centred = centred - centred.mean(0)
    singular = np.linalg.svd(centred, compute_uv=False)
    explained = singular**2 / np.sum(singular**2)
    rng = np.random.default_rng(1)
    null = []
    for _ in range(reps):
        permuted = np.column_stack(
            [rng.permutation(centred[:, j]) for j in range(centred.shape[1])]
        )
        s = np.linalg.svd(permuted, compute_uv=False)
        null.append(s**2 / np.sum(s**2))
    threshold = np.percentile(np.array(null), 95, axis=0)
    rows = []
    retained = 0
    for k in range(6):
        keep = bool(explained[k] > threshold[k])
        if keep:
            retained = k + 1
        rows.append(
            {
                "component": k + 1,
                "observed_explained": round(float(explained[k]), 4),
                "null_p95": round(float(threshold[k]), 4),
                "retained": keep,
            }
        )
    return {"components": rows, "suggested_factors": retained}


def holdout_reconstruction(matrix: np.ndarray, max_k: int = 6, mask_fraction: float = 0.10) -> list[dict[str, Any]]:
    """遮盖部分单元，用秩 k 重构填补，比较留出误差。误差最低的 k 即预测意义上的最优维数。"""
    centred = clr(matrix)
    centred = centred - centred.mean(0)
    rng = np.random.default_rng(2)
    mask = rng.random(centred.shape) < mask_fraction
    out = []
    for k in range(1, max_k + 1):
        working = centred.copy()
        working[mask] = 0.0
        reconstruction = working
        for _ in range(60):
            u, s, vt = np.linalg.svd(working, full_matrices=False)
            reconstruction = (u[:, :k] * s[:k]) @ vt[:k]
            working[mask] = reconstruction[mask]
        rmse = float(np.sqrt(((centred[mask] - reconstruction[mask]) ** 2).mean()))
        out.append({"k": k, "holdout_rmse": round(rmse, 4)})
    return out


def split_multinomial_counts(
    matrix: np.ndarray, test_fraction: float = 0.10, seed: int = 5
) -> tuple[np.ndarray, np.ndarray]:
    """把每个已观察门店随机分到训练集或测试集，保持计数数据的含义。

    不能把随机单元格直接改成零：在 Multinomial 似然中，零仍是一个已观察结果，
    会反过来迫使该类别的拟合概率下降，并不构成真正留出。
    """
    counts = np.asarray(matrix)
    if counts.ndim != 2 or np.any(counts < 0) or not np.all(counts == np.floor(counts)):
        raise ValueError("多项计数矩阵必须是非负整数二维矩阵")
    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction 必须在 0 与 1 之间")
    rng = np.random.default_rng(seed)
    test = rng.binomial(counts.astype(int), test_fraction)
    train = counts.astype(int) - test
    return train, test


def loading_stability(matrix: np.ndarray, max_k: int = 4, reps: int = 40) -> list[dict[str, Any]]:
    """bootstrap 重抽商场，看各因子的载荷方向是否稳定。不稳定的因子是噪声。"""
    centred = clr(matrix)
    centred = centred - centred.mean(0)
    _, _, base = np.linalg.svd(centred, full_matrices=False)
    rng = np.random.default_rng(3)
    n = len(centred)
    out = []
    for k in range(1, max_k + 1):
        correlations = []
        for _ in range(reps):
            index = rng.choice(n, n, replace=True)
            resampled = centred[index] - centred[index].mean(0)
            _, _, vt = np.linalg.svd(resampled, full_matrices=False)
            correlations.append(abs(pearson(base[k - 1], vt[k - 1])))
        out.append(
            {
                "factor": k,
                "loading_stability_median": round(float(np.median(correlations)), 4),
                "loading_stability_q25": round(float(np.percentile(correlations, 25)), 4),
                "stable": bool(np.median(correlations) >= 0.8),
            }
        )
    return out


# ----------------------------------------- 步骤 5b: 贝叶斯 logistic-normal multinomial

def fit_lnm(
    matrix: np.ndarray,
    n_factors: int,
    draws: int = 1000,
    tune: int = 1000,
    seed: int = 0,
    test_counts: np.ndarray | None = None,
    method: str = "advi",
    advi_iterations: int = 30000,
    progressbar: bool = True,
) -> dict[str, Any]:
    """贝叶斯 logistic-normal multinomial 因子模型。

    y_i ~ Multinomial(n_i, softmax(mu + Lambda @ f_i))

    相比 CLR-PCA 的两个好处：
      1. 零值被自然处理为"概率小但非零"，无需任意的零值替代；
      2. 计数的抽样误差进入模型，小规模商场的因子得分自动向均值收缩，
         而 PCA 会把 72 店与 484 店的商场当作同等可靠。

    为解决因子旋转不定性：Lambda 下三角约束 + f 标准正态先验。
    """
    configure_windows_compiler_paths()
    import pymc as pm
    import pytensor.tensor as pt

    counts = matrix.astype(int)
    n_malls, n_categories = counts.shape
    if test_counts is None:
        heldout = np.zeros_like(counts)
    else:
        heldout = np.asarray(test_counts, dtype=int)
        if heldout.shape != counts.shape or np.any(heldout < 0) or np.any(heldout > counts):
            raise ValueError("test_counts 必须与 matrix 同形，且逐项位于 0..matrix")
    observed = counts - heldout
    observed_totals = observed.sum(1)
    if np.any(observed_totals == 0):
        raise ValueError("每家商场的训练计数至少需要 1 条")

    with pm.Model() as model:
        intercept = pm.Normal("intercept", 0.0, 3.0, shape=n_categories)
        if n_factors > 0:
            # 下三角约束打破旋转不定性；正对角同时固定每个因子的符号，
            # 避免不同 MCMC 链落在正负镜像模式后，后验均值互相抵消。
            packed = pm.Normal("loadings_packed", 0.0, 1.0, shape=(n_categories, n_factors))
            diagonal = pm.HalfNormal("loadings_diagonal", 1.0, shape=n_factors)
            strict_lower_mask = np.tril(np.ones((n_categories, n_factors)), k=-1)
            identified = packed * strict_lower_mask
            identified = pt.set_subtensor(
                identified[np.arange(n_factors), np.arange(n_factors)], diagonal
            )
            loadings = pm.Deterministic("loadings", identified)
            scores = pm.Normal("scores", 0.0, 1.0, shape=(n_malls, n_factors))
            logits = intercept[None, :] + pt.dot(scores, loadings.T)
        else:
            logits = pt.tile(intercept[None, :], (n_malls, 1))
        probabilities = pm.Deterministic("probabilities", pm.math.softmax(logits, axis=1))
        pm.Multinomial("obs", n=observed_totals, p=probabilities, observed=observed)
        if method == "advi":
            # 61x69 的 softmax-multinomial 有约 330 个参数，NUTS 在本机需数小时；
            # ADVI 变分推断给出同样的后验均值量级，代价是低估后验方差。
            approximation = pm.fit(
                n=advi_iterations, method="advi", random_seed=seed, progressbar=progressbar
            )
            trace = approximation.sample(draws)
            converged = None
            # pm.fit().hist 保存的是要最小化的变分损失（即 negative ELBO），
            # 不能命名为 ELBO，否则模型比较的方向会被解释反。
            result_extra = {"final_advi_loss": round(float(approximation.hist[-1]), 1)}
        else:
            trace = pm.sample(
                draws=draws,
                tune=tune,
                chains=2,
                cores=1,
                random_seed=seed,
                progressbar=progressbar,
                target_accept=0.9,
                compute_convergence_checks=True,
            )
            converged = True
            result_extra = {}

    import arviz as az

    posterior = trace.posterior
    if converged:
        summary = az.summary(trace, var_names=["intercept"], kind="diagnostics")
    else:
        summary = None
    mean_probabilities = posterior["probabilities"].mean(("chain", "draw")).values
    expected = mean_probabilities * observed_totals[:, None]
    result: dict[str, Any] = {"n_factors": n_factors, "inference": method, **result_extra}
    if summary is not None:
        result["max_r_hat"] = round(float(summary["r_hat"].max()), 4)
        result["min_ess"] = int(summary["ess_bulk"].min())
    if n_factors > 0:
        result["loadings"] = posterior["loadings"].mean(("chain", "draw")).values
        result["scores"] = posterior["scores"].mean(("chain", "draw")).values
    # 后验预测检查：以卡方型偏差衡量拟合
    with np.errstate(divide="ignore", invalid="ignore"):
        deviance = np.where(expected > 0, (observed - expected) ** 2 / expected, 0.0)
    result["chi_square_discrepancy"] = round(float(deviance.sum()), 1)
    if test_counts is not None:
        heldout_totals = heldout.sum(1)
        usable_rows = heldout_totals > 0
        predicted = mean_probabilities[usable_rows] * heldout_totals[usable_rows, None]
        actual = heldout[usable_rows]
        result["holdout_mae"] = round(float(np.abs(actual - predicted).mean()), 4)
        probabilities_held = np.clip(mean_probabilities, 1e-12, 1.0)
        result["holdout_mean_log_score"] = round(
            float((heldout * np.log(probabilities_held)).sum() / max(heldout.sum(), 1)), 6
        )
        result["holdout_store_count"] = int(heldout.sum())
    return result


# --------------------------------------------------- 步骤 7-8: 因子载荷与事后解释

def factor_loadings_table(
    loadings: np.ndarray, categories: list[str], top: int = 8
) -> list[dict[str, Any]]:
    rows = []
    for k in range(loadings.shape[1]):
        column = loadings[:, k]
        order = np.argsort(column)
        rows.append(
            {
                "factor": k + 1,
                "negative_pole": " | ".join(f"{categories[j]}({column[j]:+.2f})" for j in order[:top]),
                "positive_pole": " | ".join(
                    f"{categories[j]}({column[j]:+.2f})" for j in order[-top:][::-1]
                ),
            }
        )
    return rows


def posthoc_associations(
    scores: np.ndarray,
    malls: list[str],
    feature_rows: list[dict[str, str]],
    metadata_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """因子得分与外部变量的关联。这些变量不参与因子估计，只用于命名与解释。"""
    features = {row["mall_name"]: row for row in feature_rows}
    metadata = {row["mall_name"]: row for row in metadata_rows}
    indicators = [
        "standardization_loo", "local_only_rate", "function_rarity_loo",
        "sitdown_price_log", "subculture_index", "light_consumption_index",
        "experiential_share", "l2_breadth", "store_count", "dining_share", "retail_share",
    ]
    out = []
    n_factors = scores.shape[1]
    for name in indicators:
        values, columns = [], [[] for _ in range(n_factors)]
        for i, mall in enumerate(malls):
            raw = features.get(mall, {}).get(name, "")
            if raw == "":
                continue
            values.append(float(raw))
            for k in range(n_factors):
                columns[k].append(scores[i, k])
        if len(values) < 5:
            continue
        row: dict[str, Any] = {"variable": name, "n": len(values), "kind": "constructed_indicator"}
        for k in range(n_factors):
            row[f"corr_factor{k + 1}"] = round(pearson(columns[k], values), 4)
        out.append(row)

    for label, extractor, kind in (
        (
            "distance_from_centre_km",
            lambda md: great_circle_km(float(md["longitude"]), float(md["latitude"]))
            if md.get("longitude") else None,
            "geography",
        ),
        (
            "opening_year",
            lambda md: float(md["opening_year"]) if md.get("opening_year") else None,
            "metadata",
        ),
    ):
        values, columns = [], [[] for _ in range(n_factors)]
        for i, mall in enumerate(malls):
            md = metadata.get(mall)
            if not md:
                continue
            try:
                value = extractor(md)
            except (ValueError, KeyError):
                value = None
            if value is None:
                continue
            values.append(value)
            for k in range(n_factors):
                columns[k].append(scores[i, k])
        if len(values) < 5:
            out.append({"variable": label, "n": len(values), "kind": kind, "note": "样本不足，未计算"})
            continue
        row = {"variable": label, "n": len(values), "kind": kind}
        for k in range(n_factors):
            row[f"corr_factor{k + 1}"] = round(pearson(columns[k], values), 4)
        out.append(row)
    return out


# ------------------------------------------------------------------------- 主流程

def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_factors < 1:
        raise ValueError("max_factors 必须至少为 1")
    master = PROJECT_ROOT / args.master
    output_dir = PROJECT_ROOT / args.output_dir
    rows = read_csv(master)
    matrix, malls, categories = build_matrix(rows)
    zeros = float((matrix == 0).mean())
    log(f"[1/7] 计数矩阵 {matrix.shape[0]} 商场 × {matrix.shape[1]} 业态，共 {int(matrix.sum())} 门店")
    log(f"      零值占比 {zeros:.1%}；行和 {int(matrix.sum(1).min())}–{int(matrix.sum(1).max())}")
    if matrix.shape[1] > matrix.shape[0]:
        log(f"      注意：业态数({matrix.shape[1]}) > 商场数({matrix.shape[0]})，协方差矩阵奇异")

    log("[2/7] CLR-PCA 基线与零值替代敏感性")
    pca = clr_pca(matrix, categories)
    worst = min(row["pc1_corr_vs_eps1"] for row in pca["sensitivity"])
    log(f"      PC1 得分在 eps 1.0→0.05 间最低相关 {worst:.3f}")

    log("[3/7] Aitchison 距离与自然分组检验")
    grouping, distance = aitchison_grouping(matrix)
    log(
        f"      距离 CV {grouping['distance_cv']} vs 零模型 {grouping['null_cv_mean']}"
        f" (z={grouping['cv_z_score']}) → 结构{'超出' if grouping['structure_exceeds_sampling_noise'] else '未超出'}抽样噪声"
    )
    log(
        f"      k-medoids 最佳轮廓 {grouping['best_silhouette']} →"
        f" {'支持' if grouping['discrete_groups_supported'] else '不支持'}离散分组"
    )

    log("[4/7] 因子数选择：平行分析 / 留出重构 / 载荷稳定性")
    parallel = parallel_analysis(matrix)
    holdout = holdout_reconstruction(matrix)
    stability = loading_stability(matrix)
    best_k_holdout = min(holdout, key=lambda row: row["holdout_rmse"])["k"]
    stable_k = sum(1 for row in stability if row["stable"])
    log(f"      平行分析建议 {parallel['suggested_factors']}；留出误差最优 k={best_k_holdout}；稳定因子数 {stable_k}")
    consensus = max(1, int(np.median([parallel["suggested_factors"], best_k_holdout, stable_k])))
    log(f"      三判据中位数 → K={consensus}")

    lnm_results: list[dict[str, Any]] = []
    lnm_fitted = None
    lnm_holdout_best_k = None
    selected_k = consensus
    if not args.skip_lnm:
        log(f"[5/7] 拟合贝叶斯 LNM，K=1..{args.max_factors}")
        _, test_counts = split_multinomial_counts(matrix, test_fraction=0.10, seed=5)
        fitted_by_k: dict[int, dict[str, Any]] = {}
        for k in range(1, args.max_factors + 1):
            log(f"      K={k} 拟合中（{args.inference}）…")
            started = datetime.now()
            try:
                fit = fit_lnm(
                    matrix, k, draws=args.draws, tune=args.tune,
                    test_counts=test_counts, method=args.inference,
                    advi_iterations=args.advi_iterations,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"      K={k} 拟合失败：{type(exc).__name__}: {exc}")
                continue
            fitted_by_k[k] = fit
            diagnostics = (
                f"r_hat_max={fit['max_r_hat']} ess_min={fit['min_ess']}"
                if "max_r_hat" in fit else f"inference={fit['inference']}"
            )
            elapsed = (datetime.now() - started).total_seconds()
            fit["elapsed_sec"] = round(elapsed, 1)
            lnm_results.append(
                {key: value for key, value in fit.items() if key not in ("loadings", "scores")}
            )
            log(
                f"      K={k} 完成（{elapsed:.0f}s）：{diagnostics}"
                f" holdout_MAE={fit.get('holdout_mae')} chi2={fit['chi_square_discrepancy']}"
            )
        if fitted_by_k:
            lnm_holdout_best_k = max(
                fitted_by_k,
                key=lambda k: fitted_by_k[k].get("holdout_mean_log_score", -math.inf),
            )
            selected_k = max(
                1,
                int(np.floor(np.median([
                    parallel["suggested_factors"], best_k_holdout, stable_k, lnm_holdout_best_k,
                ]))),
            )
            chosen_k = min(fitted_by_k, key=lambda k: (abs(k - selected_k), k))
            log(
                f"      LNM 留出预测最优 K={lnm_holdout_best_k}；"
                f"四判据合并后选择 K={selected_k}"
            )
            if chosen_k != selected_k:
                log(f"      K={selected_k} 未成功拟合，改用最近的成功结果 K={chosen_k}")
            # 模型比较使用同一训练/测试拆分；选定维数后必须用全量计数重拟合，
            # 否则最终载荷与因子得分只利用了约 90% 的门店。
            log(f"      K={chosen_k} 使用全量计数重拟合，用于最终解释…")
            try:
                lnm_fitted = fit_lnm(
                    matrix, chosen_k, draws=args.draws, tune=args.tune,
                    method=args.inference, advi_iterations=args.advi_iterations,
                    seed=17,
                )
                lnm_fitted["fit_scope"] = "full_data_refit"
            except Exception as exc:  # noqa: BLE001
                log(f"      全量重拟合失败：{type(exc).__name__}: {exc}；保留训练集拟合结果")
                lnm_fitted = fitted_by_k[chosen_k]
                lnm_fitted["fit_scope"] = "training_split_fallback"
    else:
        log("[5/7] 跳过 LNM（--skip-lnm）")

    log("[6/7] 因子载荷与命名")
    if lnm_fitted is not None:
        loadings = lnm_fitted["loadings"]
        scores = lnm_fitted["scores"]
        source = f"bayesian_lnm_K{lnm_fitted['n_factors']}"
    else:
        centred = clr(matrix)
        centred = centred - centred.mean(0)
        u, s, vt = np.linalg.svd(centred, full_matrices=False)
        loadings = vt[:consensus].T
        scores = u[:, :consensus] * s[:consensus]
        source = f"clr_pca_K{consensus}"
    log(f"      因子来源：{source}")
    loading_rows = factor_loadings_table(loadings, categories)
    for row in loading_rows:
        log(f"      因子{row['factor']} 正极 {row['positive_pole'][:60]}")

    log("[7/7] 事后关联：因子得分 vs 区位 / 开业年份 / 人工指标")
    feature_path = output_dir / "mall_features.csv"
    metadata_path = output_dir / "mall_metadata.csv"
    posthoc = posthoc_associations(
        scores,
        malls,
        read_csv(feature_path) if feature_path.exists() else [],
        read_csv(metadata_path) if metadata_path.exists() else [],
    )

    write_csv(output_dir / "compositional_pca_sensitivity.csv", pca["sensitivity"],
              list(pca["sensitivity"][0].keys()))
    write_csv(output_dir / "compositional_parallel_analysis.csv", parallel["components"],
              list(parallel["components"][0].keys()))
    stability_by_factor = {row["factor"]: row for row in stability}
    selection_rows = [
        {**row, **stability_by_factor.get(row["k"], {})}
        for row in holdout
    ]
    write_csv(output_dir / "compositional_factor_selection.csv", selection_rows,
              ["k", "holdout_rmse", "factor", "loading_stability_median", "loading_stability_q25", "stable"])
    write_csv(output_dir / "compositional_factor_loadings.csv", loading_rows, list(loading_rows[0].keys()))
    score_rows = [
        {"mall_name": malls[i], **{f"factor{k + 1}": round(float(scores[i, k]), 4) for k in range(scores.shape[1])}}
        for i in range(len(malls))
    ]
    write_csv(output_dir / "compositional_factor_scores.csv", score_rows, list(score_rows[0].keys()))
    if posthoc:
        keys: list[str] = []
        for row in posthoc:
            for key in row:
                if key not in keys:
                    keys.append(key)
        write_csv(output_dir / "compositional_posthoc.csv", posthoc, keys)
    if lnm_results:
        write_csv(output_dir / "compositional_lnm_comparison.csv", lnm_results,
                  list(max(lnm_results, key=len).keys()))
    counts_rows = [
        {"mall_name": malls[i], **{categories[j]: int(matrix[i, j]) for j in range(len(categories))}}
        for i in range(len(malls))
    ]
    write_csv(output_dir / "mall_category_counts.csv", counts_rows, ["mall_name"] + categories)

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "matrix": {
            "malls": len(malls), "categories": len(categories),
            "total_stores": int(matrix.sum()), "zero_share": round(zeros, 4),
        },
        "clr_pca_min_score_correlation": round(worst, 4),
        "aitchison": grouping,
        "factor_selection": {
            "parallel_analysis": parallel["suggested_factors"],
            "holdout_best_k": best_k_holdout,
            "stable_factor_count": stable_k,
            "consensus_k": consensus,
            "lnm_holdout_best_k": lnm_holdout_best_k,
            "selected_k": selected_k,
            "holdout_rmse": holdout,
            "loading_stability": stability,
        },
        "factor_source": source,
        "lnm_final_fitted_k": int(lnm_fitted["n_factors"]) if lnm_fitted is not None else None,
        "lnm_comparison": lnm_results,
        "posthoc": posthoc,
    }
    (output_dir / "compositional_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"      结果写入 {output_dir}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="商场×业态计数矩阵的成分数据因子分析")
    parser.add_argument("--master", default="data/master_stores.csv")
    parser.add_argument("--output-dir", default="analysis/outputs/current")
    parser.add_argument("--max-factors", type=int, default=4)
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--tune", type=int, default=1000)
    parser.add_argument("--skip-lnm", action="store_true", help="只跑 CLR-PCA 与因子数选择")
    parser.add_argument("--inference", choices=("advi", "nuts"), default="advi",
                        help="advi 为变分推断（秒级）；nuts 为完整 MCMC（本机需数小时）")
    parser.add_argument("--advi-iterations", type=int, default=30000)
    args = parser.parse_args(argv or sys.argv[1:])
    try:
        run(args)
    except Exception as exc:  # noqa: BLE001
        log(f"[FAILED] {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
