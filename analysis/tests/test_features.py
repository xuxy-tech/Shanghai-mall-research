from __future__ import annotations

import csv
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

ANALYSIS_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ANALYSIS_ROOT.parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


FEATURES = load("build_features", ANALYSIS_ROOT / "scripts" / "build_features.py")
FETCH = load("fetch_mall_metadata", ANALYSIS_ROOT / "scripts" / "fetch_mall_metadata.py")
ANALYSIS = load("run_analysis", ANALYSIS_ROOT / "scripts" / "run_analysis.py")
COMPOSITIONAL = load("compositional_analysis", ANALYSIS_ROOT / "scripts" / "compositional_analysis.py")
BRAND = load("brand_sensitivity", ANALYSIS_ROOT / "scripts" / "brand_sensitivity.py")


class AmapMatchTests(unittest.TestCase):
    def test_rejects_wrong_malls(self) -> None:
        wrong = [
            ("BFC外滩金融中心", "北外滩来福士"),
            ("世贸广场", "NX258"),
            ("浦东嘉里中心", "上海市静安区静安嘉里中心(出入口)"),
            ("新天地广场", "新邻天地"),
            ("虹桥新天地", "亿厘新天地"),
            ("长泰广场", "旖彩城(长泰国际商业广场店)"),
            ("五角场万达广场", "万达广场A区"),
            ("崇明万达广场", "万达广场(上海金山店)"),
        ]
        for mall, poi in wrong:
            with self.subTest(mall=mall, poi=poi):
                self.assertEqual(FETCH.match_quality(mall, poi), 0)

    def test_accepts_correct_malls(self) -> None:
        right = [
            ("K11购物艺术中心", "上海k11购物艺术中心"),
            ("ONE ITC", "上海市徐汇区One ITC(西北1门)"),
            ("国金ifc商场", "上海ifc商场"),
            ("大宁久光中心", "上海久光中心"),
            ("瑞虹天地太阳宫", "瑞虹新天地太阳宫"),
            ("崇明万达广场", "万达广场(上海崇明店)"),
            ("金山万达广场", "万达广场(上海金山店)"),
            ("青浦吾悦广场", "吾悦广场(上海青浦店)"),
            ("静安大悦城", "上海静安大悦城"),
        ]
        for mall, poi in right:
            with self.subTest(mall=mall, poi=poi):
                self.assertGreater(FETCH.match_quality(mall, poi), 0)

    def test_seed_merge_respects_active_flag(self) -> None:
        """active=FALSE 的低可信度年份必须保留在文件里但不进入分析。"""
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed.csv"
            with seed.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["active", "mall_name", "opening_year", "commercial_area_sqm"]
                )
                writer.writeheader()
                writer.writerow({"active": "TRUE", "mall_name": "可信商场", "opening_year": "2013",
                                 "commercial_area_sqm": "40000"})
                writer.writerow({"active": "FALSE", "mall_name": "存疑商场", "opening_year": "2006",
                                 "commercial_area_sqm": ""})
            records = [
                {"mall_name": "可信商场", "opening_year": "", "commercial_area_sqm": ""},
                {"mall_name": "存疑商场", "opening_year": "", "commercial_area_sqm": ""},
            ]
            merged = FETCH.merge_seed_years(records, seed)
            self.assertEqual(merged, 1)
            self.assertEqual(records[0]["opening_year"], "2013")
            self.assertEqual(records[1]["opening_year"], "")

    def test_seed_merge_rejects_malformed_year(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed.csv"
            with seed.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["active", "mall_name", "opening_year"])
                writer.writeheader()
                for bad in ("20133", "abc", "13", "2013年"):
                    writer.writerow({"active": "TRUE", "mall_name": bad, "opening_year": bad})
            records = [{"mall_name": bad, "opening_year": "", "commercial_area_sqm": ""}
                       for bad in ("20133", "abc", "13", "2013年")]
            self.assertEqual(FETCH.merge_seed_years(records, seed), 0)

    def test_concurrency_limit_is_three(self) -> None:
        # 高德对超频调用会封禁 Key，这个上限不允许被无意调高
        self.assertLessEqual(FETCH.MAX_CONCURRENCY, 3)
        self.assertGreater(FETCH.MIN_REQUEST_INTERVAL, 0)


class FeatureTests(unittest.TestCase):
    def test_entropy(self) -> None:
        self.assertAlmostEqual(FEATURES.shannon_entropy([1, 1]), math.log(2), places=6)
        self.assertEqual(FEATURES.shannon_entropy([5]), 0.0)
        self.assertEqual(FEATURES.shannon_entropy([]), 0.0)

    def test_leave_one_out_excludes_own_mall(self) -> None:
        """留一法的核心性质：只出现在本商场的品牌，其共现率必须为 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "master.csv"
            fields = [
                "mall_name", "brand_id", "canonical_brand_name",
                "category_l1", "category_l2", "avg_price_yuan", "rating",
            ]
            rows = [
                {"mall_name": "A", "brand_id": "B1", "canonical_brand_name": "独有A",
                 "category_l1": "购物", "category_l2": "服饰鞋包", "avg_price_yuan": "", "rating": "4.5"},
                {"mall_name": "A", "brand_id": "B2", "canonical_brand_name": "共有",
                 "category_l1": "美食", "category_l2": "粤菜", "avg_price_yuan": "100", "rating": "4.5"},
                {"mall_name": "B", "brand_id": "B2", "canonical_brand_name": "共有",
                 "category_l1": "美食", "category_l2": "粤菜", "avg_price_yuan": "100", "rating": "4.5"},
                {"mall_name": "B", "brand_id": "B3", "canonical_brand_name": "独有B",
                 "category_l1": "购物", "category_l2": "珠宝首饰", "avg_price_yuan": "", "rating": "4.5"},
            ]
            with master.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            config = json.loads((ANALYSIS_ROOT / "config" / "feature_config.json").read_text(encoding="utf-8-sig"))
            config["master_input"] = str(master)
            config["output_dir"] = str(Path(tmp) / "out")
            config["metadata_input"] = ""
            FEATURES.build(config)
            with (Path(tmp) / "out" / "mall_features.csv").open(encoding="utf-8-sig") as handle:
                result = {row["mall_name"]: row for row in csv.DictReader(handle)}
            # A 有 2 家店：1 家独有(共现0)、1 家在 B 也有(共现1/1) -> 均值 0.5
            self.assertAlmostEqual(float(result["A"]["standardization_loo"]), 0.5, places=6)
            self.assertAlmostEqual(float(result["A"]["local_only_rate"]), 0.5, places=6)

    def test_config_excludes_coffee_and_bakery_from_subculture(self) -> None:
        config = json.loads((ANALYSIS_ROOT / "config" / "feature_config.json").read_text(encoding="utf-8-sig"))
        subculture = set(config["subculture_categories"])
        self.assertNotIn("咖啡", subculture)
        self.assertNotIn("面包甜点", subculture)
        self.assertIn("演出活动", subculture)
        self.assertIn("DIY手工", subculture)
        # 正餐口径必须排除饮品与小吃，否则奶茶店多的商场客单价被系统性拉低
        sitdown = set(config["sitdown_dining_categories"])
        for excluded in ("茶饮果汁", "咖啡", "面包甜点", "小吃快餐"):
            self.assertNotIn(excluded, sitdown)


class AnalysisMethodTests(unittest.TestCase):
    def test_archetypes_remain_inside_observed_convex_hull(self) -> None:
        # 一维时凸包就是 [min, max]，旧的无约束最小二乘更新会越过这个区间。
        data = np.array([[-1.0], [-0.2], [0.0], [0.3], [1.0]])
        archetypes, weights, explained = ANALYSIS.archetypal(
            data, 2, iterations=500, restarts=4, seed=1
        )
        self.assertTrue(np.all(archetypes >= data.min(0) - 1e-9))
        self.assertTrue(np.all(archetypes <= data.max(0) + 1e-9))
        self.assertTrue(np.all(weights >= -1e-12))
        np.testing.assert_allclose(weights.sum(1), 1.0, atol=1e-9)
        self.assertGreater(explained, 0.95)

    def test_multinomial_holdout_partitions_counts(self) -> None:
        counts = np.array([[10, 0, 3], [2, 8, 1]])
        train, test = COMPOSITIONAL.split_multinomial_counts(counts, 0.25, seed=4)
        np.testing.assert_array_equal(train + test, counts)
        self.assertTrue(np.all(train >= 0))
        self.assertTrue(np.all(test >= 0))
        self.assertGreater(int(test.sum()), 0)

    def test_weak_singletons_use_each_identity_definition(self) -> None:
        rows = [
            {"mall_name": "A", "brand_id": "strict-a", "canonical_brand_name": "ABCD旗舰店", "observations": "1"},
            {"mall_name": "A", "brand_id": "strict-b", "canonical_brand_name": "ABCD专柜", "observations": "1"},
            {"mall_name": "A", "brand_id": "strict-c", "canonical_brand_name": "XYZ", "observations": "3"},
            {"mall_name": "B", "brand_id": "strict-c", "canonical_brand_name": "XYZ", "observations": "3"},
        ]
        strict = BRAND.weak_singletons_for_identity(rows, BRAND.identity_strict)
        merged = BRAND.weak_singletons_for_identity(rows, BRAND.identity_strip_suffix)
        self.assertEqual(strict, {"strict-a", "strict-b"})
        self.assertEqual(merged, {"LAT:abcd"})
        metrics = BRAND.evaluate(rows, ["A", "B"], BRAND.identity_strip_suffix, merged)
        self.assertEqual(metrics["brand_count"], 1)


if __name__ == "__main__":
    unittest.main()
