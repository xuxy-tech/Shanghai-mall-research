"""Check the table contract consumed by the static map exporter."""

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "package_results.py"
spec = importlib.util.spec_from_file_location("package_results", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


class PackageResultsTests(unittest.TestCase):
    def test_profiles_join_by_mall_name_and_keep_map_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_csv(root / "mall_features.csv", [
                {"mall_name": "A", "store_count": "20", "longitude": "121.5"},
                {"mall_name": "B", "store_count": "10", "longitude": "121.6"},
            ])
            write_csv(root / "compositional_factor_scores.csv", [
                {"mall_name": "B", "factor1": "2"},
                {"mall_name": "A", "factor1": "1"},
            ])
            write_csv(root / "archetype_mixtures.csv", [
                {"mall_name": "A", "dominant": "A0"},
                {"mall_name": "B", "dominant": "A1"},
            ])
            target = module.package(root)
            with target.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["mall_name"] for row in rows], ["A", "B"])
            self.assertEqual([(row["store_count"], row["factor1"], row["dominant"]) for row in rows],
                             [("20", "1", "A0"), ("10", "2", "A1")])
            self.assertTrue((root / "intermediate" / "mall_features.csv").exists())

    def test_mismatched_mall_sets_fail_before_writing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_csv(root / "mall_features.csv", [{"mall_name": "A"}])
            write_csv(root / "compositional_factor_scores.csv", [{"mall_name": "B"}])
            write_csv(root / "archetype_mixtures.csv", [{"mall_name": "A"}])
            with self.assertRaisesRegex(ValueError, "Mall names differ"):
                module.package(root)
            self.assertFalse((root / "tables" / "core" / "mall_profiles.csv").exists())

    def test_repackages_new_scores_with_existing_intermediate_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            intermediate = root / "intermediate"
            intermediate.mkdir()
            write_csv(intermediate / "mall_features.csv", [{"mall_name": "A", "store_count": "20"}])
            write_csv(intermediate / "archetype_mixtures.csv", [{"mall_name": "A", "dominant": "A0"}])
            write_csv(root / "compositional_factor_scores.csv", [{"mall_name": "A", "factor1": "3"}])
            target = module.package(root)
            with target.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle))["factor1"], "3")
            self.assertTrue((intermediate / "compositional_factor_scores.csv").exists())


if __name__ == "__main__":
    unittest.main()
