import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analyze_video import (  # noqa: E402
    CardObservation,
    OCRToken,
    extract_category,
    extract_floor,
    extract_price,
    extract_rating,
    is_plausible_category,
    is_pure_floor_token,
    normalize_name,
    parse_cards,
    record_card_type,
    record_review_reasons,
    record_research_eligible,
    same_merchant,
    MerchantRecord,
)


def observation(
    name: str,
    category: str = "粤菜馆",
    floor: str = "L2",
    rating: str = "4.7",
) -> CardObservation:
    return CardObservation(
        merchant_name_raw=name,
        category_raw=category,
        rating=rating,
        avg_price_yuan="120",
        floor_location=floor,
        ocr_confidence=0.95,
        frame_index=1,
        timestamp_sec=1.0,
        crop_top=0,
        crop_bottom=200,
        all_text=name,
    )


class ParserTests(unittest.TestCase):
    def test_metadata_extractors(self) -> None:
        text = "★★★★★ 4.7 粤菜馆￥120/人 L2"
        self.assertEqual(extract_rating(text), "4.7")
        self.assertEqual(extract_price(text), "120")
        self.assertEqual(extract_floor("L2"), "L2")
        self.assertEqual(extract_floor("饮品：B2层"), "B2")
        self.assertEqual(extract_floor("L3.9甜品"), "")
        self.assertEqual(extract_floor("B74S店"), "")
        self.assertEqual(extract_floor("B99"), "")
        self.assertEqual(extract_category("4.7粤菜馆￥120/人"), "粤菜馆")
        self.assertEqual(extract_category("商场内"), "")
        self.assertEqual(extract_category("95/人"), "")
        self.assertEqual(extract_category("□4.4 打边炉/港式..."), "打边炉/港式...")
        self.assertEqual(extract_category("日4.8私房菜"), "私房菜")
        self.assertEqual(extract_category("46川菜"), "川菜")
        self.assertEqual(extract_category("小火锅50/人"), "小火锅")
        self.assertEqual(extract_category("OL3.9日本料理"), "日本料理")
        self.assertEqual(extract_category("日日日4.8数码产品"), "数码产品")
        self.assertEqual(extract_rating("已售4.4万"), "")
        self.assertEqual(extract_rating("￥4.8"), "")

    def test_digit_first_floor_is_recognized(self) -> None:
        # 点评实际使用 4F 这类写法，旧正则只认 F4，导致楼层落空并被当成分类
        for raw, expected in (
            ("4F", "4F"),
            ("1F", "1F"),
            ("10F", "10F"),
            ("LG2", "LG2"),
            ("LG", "LG"),
            ("B1", "B1"),
            ("L5", "L5"),
            ("餐饮：3F层", "3F"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(extract_floor(raw), expected)

    def test_punctuation_prefixed_floor_leaves_no_category_residue(self) -> None:
        # 真实样本 "、10F"：前导标点使整串不被判为纯楼层，数字噪声规则会只留下 "F"
        for raw in ("、10F", "，1F", "10F", " 4F"):
            with self.subTest(raw=raw):
                self.assertFalse(is_plausible_category(extract_category(raw)))

    def test_floor_token_never_becomes_category(self) -> None:
        for raw in ("4F", "1F", "10F", "LG2", "B1", "L5", "LG"):
            with self.subTest(raw=raw):
                self.assertTrue(is_pure_floor_token(raw))
                self.assertFalse(is_plausible_category(extract_category(raw)))

    def test_brand_names_are_not_mistaken_for_floors(self) -> None:
        # 楼层判定不能吃掉正常品牌名与含字母数字的分类
        for raw in ("LG电子", "F1赛车主题", "B站周边", "L'OCCITANE", "B74S店", "B99"):
            with self.subTest(raw=raw):
                self.assertFalse(is_pure_floor_token(raw))
        self.assertEqual(extract_category("服饰鞋包"), "服饰鞋包")

    def test_name_normalization(self) -> None:
        self.assertEqual(normalize_name(" 醉庐 · 新上海菜 "), "醉庐·新上海菜")

    def test_dedup_respects_floor(self) -> None:
        self.assertTrue(same_merchant(observation("点都德"), observation("點都德")))
        self.assertTrue(same_merchant(observation("H's", "服装", "L2"), observation("H'S", "服装", "L2")))
        self.assertTrue(same_merchant(observation("省士道寿司", "寿司", "B2"), observation("鮨士道寿司", "寿司", "B2")))
        self.assertTrue(
            same_merchant(
                observation("味千拉面", "日式拉面", "B2"),
                observation("味干拉面", "日式拉面", "B2", rating=""),
            )
        )
        self.assertTrue(
            same_merchant(
                observation("眉州东坡", "川菜馆", "L5"),
                observation("眉州东妆", "日4.8川菜馆", "L5"),
            )
        )
        self.assertFalse(same_merchant(observation("点都德", floor="L2"), observation("点都德", floor="L6")))

    def test_structural_parser_accepts_low_resolution_title(self) -> None:
        tokens = [
            OCRToken("锦府园·新台州菜", 0.99, (252, 100, 520, 138)),
            OCRToken("4.8浙菜￥156/人", 0.96, (252, 150, 650, 184)),
            OCRToken("L5", 0.98, (760, 150, 810, 184)),
            OCRToken("杨浦区浙菜口味榜第4名", 0.99, (252, 210, 600, 240)),
        ]
        cards, audit = parse_cards(
            tokens=tokens,
            image_width=864,
            image_height=1556,
            scale=2.0,
            crop_y=172,
            frame_height=960,
            frame_index=1,
            timestamp_sec=1.0,
        )
        self.assertEqual(audit.accepted_cards, 1)
        self.assertEqual(cards[0].merchant_name_raw, "锦府园·新台州菜")
        self.assertEqual(cards[0].category_raw, "浙菜")
        self.assertEqual(cards[0].rating, "4.8")
        self.assertEqual(cards[0].floor_location, "L5")

    def test_structural_parser_accepts_unrated_card(self) -> None:
        tokens = [
            OCRToken("NEW BRAND", 0.98, (252, 100, 490, 138)),
            OCRToken("暂无评分 服装", 0.94, (252, 150, 540, 184)),
            OCRToken("L2", 0.98, (760, 150, 810, 184)),
        ]
        cards, audit = parse_cards(
            tokens=tokens,
            image_width=864,
            image_height=1556,
            scale=2.0,
            crop_y=172,
            frame_height=960,
            frame_index=1,
            timestamp_sec=1.0,
        )
        self.assertEqual(audit.unrated_cards, 1)
        self.assertEqual(cards[0].rating_status, "unrated")
        self.assertEqual(cards[0].card_type, "unrated")
        self.assertEqual(cards[0].category_raw, "服装")

    def test_structural_parser_accepts_no_star_card(self) -> None:
        tokens = [
            OCRToken("NEW BRAND", 0.98, (252, 100, 490, 138)),
            OCRToken("暂无星级", 0.94, (252, 150, 430, 184)),
            OCRToken("服装", 0.96, (470, 150, 550, 184)),
            OCRToken("L2", 0.98, (760, 150, 810, 184)),
        ]
        cards, audit = parse_cards(
            tokens=tokens,
            image_width=864,
            image_height=1556,
            scale=2.0,
            crop_y=172,
            frame_height=960,
            frame_index=1,
            timestamp_sec=1.0,
        )
        self.assertEqual(audit.unrated_cards, 1)
        self.assertEqual(cards[0].rating_status, "unrated")
        self.assertEqual(cards[0].card_type, "unrated")
        self.assertEqual(cards[0].category_raw, "服装")

    def test_new_store_badge_sets_scope_without_becoming_category(self) -> None:
        tokens = [
            OCRToken("文兴烧腊", 0.99, (252, 100, 500, 138)),
            OCRToken("新店", 0.99, (252, 150, 335, 184)),
            OCRToken("烧腊￥70/人", 0.96, (350, 150, 620, 184)),
            OCRToken("B1", 0.98, (760, 150, 810, 184)),
        ]
        cards, _ = parse_cards(
            tokens=tokens,
            image_width=864,
            image_height=1556,
            scale=2.0,
            crop_y=172,
            frame_height=960,
            frame_index=1,
            timestamp_sec=1.0,
        )
        self.assertEqual(cards[0].card_type, "new_store")
        self.assertEqual(cards[0].category_raw, "烧腊")
        record = MerchantRecord(
            best=cards[0],
            first_seen_sec=1.0,
            last_seen_sec=1.0,
            card_type_counts={"new_store": 1},
        )
        self.assertEqual(record_card_type(record), "new_store")
        self.assertFalse(record_research_eligible(record))

    def test_coupon_new_store_text_does_not_set_new_store_type(self) -> None:
        tokens = [
            OCRToken("正常餐厅", 0.99, (252, 100, 500, 138)),
            OCRToken("4.8粤菜￥120/人", 0.96, (252, 150, 650, 184)),
            OCRToken("L2", 0.98, (760, 150, 810, 184)),
            OCRToken("【新店特惠】100元代金券", 0.96, (252, 310, 700, 344)),
        ]
        cards, _ = parse_cards(
            tokens=tokens,
            image_width=864,
            image_height=1556,
            scale=2.0,
            crop_y=172,
            frame_height=960,
            frame_index=1,
            timestamp_sec=1.0,
        )
        self.assertEqual(cards[0].card_type, "rated")

    def test_digit_first_floor_fills_floor_slot_not_category(self) -> None:
        # 复刻真实失败样本：4F 出现在评分之后，旧逻辑把 4F 写进分类、楼层留空
        tokens = [
            OCRToken("清晨家烤肉", 0.99, (252, 100, 500, 138)),
            OCRToken("4.9", 0.96, (252, 150, 330, 184)),
            OCRToken("4F", 0.98, (760, 150, 810, 184)),
            OCRToken("韩式烤肉￥210/人", 0.96, (350, 150, 700, 184)),
        ]
        cards, _ = parse_cards(
            tokens=tokens,
            image_width=864,
            image_height=1556,
            scale=2.0,
            crop_y=172,
            frame_height=960,
            frame_index=1,
            timestamp_sec=1.0,
        )
        self.assertEqual(cards[0].floor_location, "4F")
        self.assertEqual(cards[0].category_raw, "韩式烤肉")
        self.assertEqual(cards[0].avg_price_yuan, "210")
        self.assertEqual(cards[0].rating, "4.9")

    def test_floor_leading_token_order_is_handled(self) -> None:
        # 另一种真实排列：楼层在最前，分类和评分顺序颠倒
        tokens = [
            OCRToken("OUTCiRCS", 0.98, (252, 100, 490, 138)),
            OCRToken("2F", 0.98, (760, 150, 810, 184)),
            OCRToken("服饰鞋包", 0.96, (350, 150, 520, 184)),
            OCRToken("3.7", 0.95, (252, 150, 330, 184)),
        ]
        cards, _ = parse_cards(
            tokens=tokens,
            image_width=864,
            image_height=1556,
            scale=2.0,
            crop_y=172,
            frame_height=960,
            frame_index=1,
            timestamp_sec=1.0,
        )
        self.assertEqual(cards[0].floor_location, "2F")
        self.assertEqual(cards[0].category_raw, "服饰鞋包")
        self.assertEqual(cards[0].rating, "3.7")

    def test_missing_floor_does_not_require_review(self) -> None:
        item = observation("楼层灰字测试店", floor="")
        record = MerchantRecord(
            best=item,
            first_seen_sec=1.0,
            last_seen_sec=2.0,
            observations=2,
            variants={item.merchant_name_raw},
            card_type_counts={"rated": 2},
        )
        self.assertNotIn("floor_missing", record_review_reasons(record))


if __name__ == "__main__":
    unittest.main()
