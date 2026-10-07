from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_master.py"
SPEC = importlib.util.spec_from_file_location("build_master", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def base_row(name: str, floor: str = "", metadata: str = "", rating_status: str = "rated") -> dict[str, str]:
    return {
        "card_id": name + floor,
        "mall_name": "测试商场",
        "merchant_name_raw": name,
        "merchant_name_normalized": name,
        "category_raw": "咖啡",
        "rating": "4.5" if rating_status == "rated" else "",
        "floor_location": floor,
        "rating_status": rating_status,
        "card_type": rating_status,
        "metadata_text": metadata,
        "ocr_confidence": "0.9",
        "title_score": "0.9",
        "observations": "1",
        "mall_id": "MALL-TEST",
        "source_record_id": name + floor,
        "evidence_image_absolute": "",
        "normalized_floor": MODULE.normalize_floor(floor),
    }


class ProcessingRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "category": {"exclude_unmapped": True},
            # 与 pipeline_config.json 一致：新店与无评分作为状态变量保留，不排除
            "exclusion": {
                "exclude_exact_new_store_badge": False,
                "new_store_exact_tokens": ["新店"],
                "exclude_explicit_unrated": False,
            },
            "deduplication": {
                "enabled": True,
                "keep_confirmed_different_floors": True,
                "merge_when_floor_missing": True,
            },
        }
        self.mapper = MODULE.CategoryMapper(
            [{"priority": "1", "active": "TRUE", "match_type": "exact", "pattern": "咖啡", "category_l1": "美食", "category_l2": "咖啡"}]
        )

    def test_new_store_becomes_status_variable_not_exclusion(self) -> None:
        """新店是经营阶段而非排除理由；标记保留为变量，门店必须进入主表。

        排除新店会系统性低估新兴业态：游戏电竞 24.3% 被排除，奢侈品仅 2.6%。
        """
        rows = [
            base_row("精确新店", metadata="F1 | 新店 | 咖啡"),
            base_row("英文品牌", metadata="F1 | New | 咖啡"),
            base_row("开业文案", metadata="F1 | 新店开业 | 咖啡"),
        ]
        eligible, excluded = MODULE.preprocess_rows(rows, self.config, self.mapper, [], {})
        self.assertEqual(len(eligible), 3)
        self.assertEqual(excluded, [])
        flags = {row["merchant_name_normalized"]: row["is_new_store"] for row in eligible}
        self.assertTrue(flags["精确新店"])
        self.assertFalse(flags["英文品牌"])
        self.assertFalse(flags["开业文案"])

    def test_rating_state_recorded_without_dropping_stores(self) -> None:
        """评分缺失是数据属性而非商户属性，不能决定商户是否存在。"""
        rows = [
            base_row("明确无评分", rating_status="unrated"),
            base_row("漏识别评分", rating_status="ocr_missing"),
            base_row("正常有评分"),
        ]
        eligible, excluded = MODULE.preprocess_rows(rows, self.config, self.mapper, [], {})
        self.assertEqual(len(eligible), 3)
        self.assertEqual(excluded, [])
        states = {row["merchant_name_normalized"]: row["rating_state"] for row in eligible}
        self.assertEqual(states["明确无评分"], "explicitly_unrated")
        self.assertEqual(states["漏识别评分"], "ocr_missing")
        self.assertEqual(states["正常有评分"], "rated")

    def test_exclusion_switches_remain_available_for_sensitivity(self) -> None:
        """开关保留：仍可开启排除以做敏感性对比，但默认关闭。"""
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "config" / "pipeline_config.json").read_text(
                encoding="utf-8-sig"
            )
        )
        self.assertFalse(config["exclusion"]["exclude_exact_new_store_badge"])
        self.assertFalse(config["exclusion"]["exclude_explicit_unrated"])
        self.config["exclusion"]["exclude_explicit_unrated"] = True
        eligible, excluded = MODULE.preprocess_rows(
            [base_row("明确无评分", rating_status="unrated")], self.config, self.mapper, [], {}
        )
        self.assertEqual(eligible, [])
        self.assertEqual(len(excluded), 1)

    def test_same_brand_different_known_floors_is_kept(self) -> None:
        rows = [base_row("同品牌", "F1"), base_row("同品牌", "F2"), base_row("同品牌", "")]
        for row in rows:
            row.update({"brand_id": "BRAND-X", "canonical_brand_name": "同品牌", "mapping_status": "mapped"})
        instances, audit = MODULE.deduplicate(rows, self.config)
        self.assertEqual(len(instances), 2)
        self.assertEqual(audit[0]["duplicates_removed"], 1)

    def test_missing_floor_duplicates_are_merged(self) -> None:
        rows = [base_row("同品牌", ""), base_row("同品牌", "")]
        for row in rows:
            row.update({"brand_id": "BRAND-X", "canonical_brand_name": "同品牌", "mapping_status": "mapped"})
        instances, audit = MODULE.deduplicate(rows, self.config)
        self.assertEqual(len(instances), 1)
        self.assertEqual(audit[0]["duplicates_removed"], 1)

    def test_category_mapping_is_editable_rule_lookup(self) -> None:
        self.assertEqual(self.mapper.map("咖啡")[:2], ("美食", "咖啡"))
        self.assertEqual(self.mapper.map("未知长尾")[:2], ("未映射", "未映射"))

    def test_unmapped_category_is_excluded_from_analysis_rows(self) -> None:
        row = base_row("未知类别店")
        row["category_raw"] = "未知长尾"
        eligible, excluded = MODULE.preprocess_rows([row], self.config, self.mapper, [], {})
        self.assertEqual(eligible, [])
        self.assertEqual(excluded[0]["processing_exclusion_reason"], "unmapped_category")

    def test_unmapped_category_filter_can_be_disabled(self) -> None:
        row = base_row("未知类别店")
        row["category_raw"] = "未知长尾"
        self.config["category"]["exclude_unmapped"] = False
        eligible, excluded = MODULE.preprocess_rows([row], self.config, self.mapper, [], {})
        self.assertEqual(len(eligible), 1)
        self.assertEqual(excluded, [])

    def test_strengthened_mapping_examples(self) -> None:
        rules = MODULE.read_csv(Path(__file__).resolve().parents[1] / "config" / "category_mapping.csv")
        mapper = MODULE.CategoryMapper(rules)
        expected = {
            "西服定制": ("购物", "服饰鞋包"),
            "米粉": ("美食", "小吃快餐"),
            "锅贴": ("美食", "小吃快餐"),
            "鲁菜": ("美食", "其它地方菜"),
            "美胸丰胸": ("丽人美发", "美容医美"),
        }
        for raw, categories in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(mapper.map(raw)[:2], categories)

    def _live_rules(self):
        config_dir = Path(__file__).resolve().parents[1] / "config"
        mapper = MODULE.CategoryMapper(MODULE.read_csv(config_dir / "category_mapping.csv"))
        exclusions = MODULE.read_csv(config_dir / "exclusion_rules.csv")
        return mapper, exclusions

    def _resolve(self, category_raw: str, name: str = "") -> str:
        mapper, exclusions = self._live_rules()
        row = {
            "category_raw": category_raw,
            "merchant_name_normalized": name,
            "category_l1": "",
            "category_l2": "",
        }
        if MODULE.apply_exclusion_rules(row, exclusions):
            return "excluded"
        return "/".join(mapper.map(category_raw)[:2])

    def test_bank_networks_excluded_but_telecom_and_forex_kept(self) -> None:
        for raw in ("营业网点", "银行", "ATM自助网点"):
            with self.subTest(raw=raw):
                self.assertEqual(self._resolve(raw, "招商银行"), "excluded")
        self.assertEqual(self._resolve("通信营业厅", "中国移动"), "生活服务/营业网点")
        self.assertEqual(self._resolve("换汇/汇款", "携程外币兑换"), "生活服务/营业网点")

    def test_non_tenant_and_out_of_scope_categories_excluded(self) -> None:
        for raw in (
            "停车场", "充电站/桩", "充电站.", "智能自助设施", "景区设施",
            "智能按摩空间", "智能.", "证券投资", "保险公司", "房产中介",
            "人力资源", "酒店", "豪华型", "经济型", "综合经销商",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self._resolve(raw), "excluded")

    def test_office_tenants_excluded_by_name_without_catching_retailers(self) -> None:
        for name in ("北京市两高(上海)律师事务所", "上海东亚期货有限公司", "某会计师事务所"):
            with self.subTest(name=name):
                self.assertEqual(self._resolve("数码家电", name), "excluded")
        # 普通商户名里的“有限公司”不应被误排除
        self.assertNotEqual(self._resolve("数码家电", "上海金城制冷设备有限公司"), "excluded")

    def test_newly_added_category_mappings(self) -> None:
        expected = {
            "文化艺术": ("休闲玩乐", "其它"),
            "美术馆": ("休闲玩乐", "其它"),
            "LiveHouse": ("休闲玩乐", "演出活动"),
            "集市": ("休闲玩乐", "其它"),
            "机器人编程": ("教育培训", "教育培训"),
            "绘本馆": ("亲子", "亲子活动"),
            "美容/SPA": ("丽人美发", "美容医美"),
            "眼科诊所/门诊": ("丽人美发", "美容医美"),
            "小儿推拿": ("丽人美发", "美容医美"),
            "运动健身": ("运动健身", "健身中心"),
            "建材": ("购物", "其它"),
            "家用": ("购物", "数码家电"),
        }
        mapper, _ = self._live_rules()
        for raw, categories in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(mapper.map(raw)[:2], categories)

    def test_entropy_uses_mapped_categories_as_denominator(self) -> None:
        source_rows = [
            {"mall_name": "测试商场", "mapping_status": "mapped"},
            {"mall_name": "测试商场", "mapping_status": "mapped"},
            {"mall_name": "测试商场", "mapping_status": "unmapped"},
        ]
        instances = [
            {
                "mall_name": "测试商场", "mall_id": "MALL-TEST", "brand_id": "A",
                "category_l1": "美食", "shared_across_malls": False, "known_chain": False,
                "floor_location": "", "rating": "4.5",
            },
            {
                "mall_name": "测试商场", "mall_id": "MALL-TEST", "brand_id": "B",
                "category_l1": "购物", "shared_across_malls": False, "known_chain": False,
                "floor_location": "", "rating": "4.5",
            },
            {
                "mall_name": "测试商场", "mall_id": "MALL-TEST", "brand_id": "C",
                "category_l1": "未映射", "shared_across_malls": False, "known_chain": False,
                "floor_location": "", "rating": "4.5",
            },
        ]
        mall_metrics, _, _ = MODULE.build_metrics(source_rows, [], instances, [], 8)
        self.assertAlmostEqual(mall_metrics[0]["category_entropy"], math.log(2), places=6)
        self.assertAlmostEqual(mall_metrics[0]["category_hhi"], 0.5, places=6)


if __name__ == "__main__":
    unittest.main()
