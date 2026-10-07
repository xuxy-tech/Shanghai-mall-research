"""Merge per-mall outputs and arrange analysis tables for the map and report."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


CORE = {"mall_metadata.csv", "archetype_profiles.csv", "compositional_factor_loadings.csv"}
DIAGNOSTICS = {
    "brand_colocation.csv", "brand_sensitivity.csv", "category_ubiquity.csv",
    "compositional_factor_selection.csv", "compositional_lnm_comparison.csv",
    "compositional_parallel_analysis.csv", "compositional_pca_sensitivity.csv",
    "compositional_posthoc.csv", "kmeans_reference.csv", "partial_correlations.csv",
}
INTERMEDIATE = {
    "archetype_mixtures.csv", "compositional_factor_scores.csv",
    "mall_category_counts.csv", "mall_features.csv",
}
MACHINE = {
    "analysis_results.json", "brand_sensitivity.json",
    "compositional_results.json", "feature_manifest.json", "run_manifest.json",
}


def read_rows(root: Path, name: str) -> list[dict[str, str]]:
    source = root / name
    if not source.exists():
        source = root / "intermediate" / name
    with source.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def package(root: Path) -> Path:
    base = read_rows(root, "mall_features.csv")
    by_name = {row["mall_name"]: dict(row) for row in base}
    for name in ("compositional_factor_scores.csv", "archetype_mixtures.csv"):
        rows = read_rows(root, name)
        if {row["mall_name"] for row in rows} != set(by_name):
            raise ValueError(f"Mall names differ between mall_features.csv and {name}")
        for row in rows:
            by_name[row["mall_name"]].update({key: value for key, value in row.items() if key != "mall_name"})

    fields = list(dict.fromkeys(key for row in by_name.values() for key in row))
    target = root / "tables" / "core" / "mall_profiles.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(by_name.values())

    for folder, names in (("tables/core", CORE), ("tables/diagnostics", DIAGNOSTICS),
                          ("intermediate", INTERMEDIATE), ("machine", MACHINE)):
        destination = root / folder
        for name in names:
            source = root / name
            if source.exists():
                destination.mkdir(parents=True, exist_ok=True)
                source.replace(destination / name)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Package analysis tables for the map")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(f"[DONE] {package(args.output_dir.resolve())}", flush=True)


if __name__ == "__main__":
    main()
