"""Unit tests for JD search candidate normalization."""

from __future__ import annotations

import unittest

from parity.collectors.jd_search import _candidate_from_browser, build_jd_search_url


class JdSearchDemoTest(unittest.TestCase):
    def test_build_search_url_normalizes_query(self) -> None:
        url = build_jd_search_url("  SK-II   神仙水  230ml ")
        self.assertIn("keyword=SK-II+%E7%A5%9E%E4%BB%99%E6%B0%B4+230ml", url)
        self.assertIn("enc=utf-8", url)

    def test_candidate_is_unverified_hint(self) -> None:
        candidate = _candidate_from_browser(
            {
                "sku": "10001",
                "title": "SK-II 神仙水 230ml",
                "url": "//item.jd.com/10001.html",
                "price": "￥699.9",
                "store": "SK-II京东自营旗舰店",
            }
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.sku, "10001")
        self.assertEqual(candidate.page_price, "699.90")
        self.assertTrue(candidate.jd_self_operated_hint)

    def test_non_item_link_is_rejected(self) -> None:
        self.assertIsNone(
            _candidate_from_browser(
                {"title": "广告", "url": "https://example.com/ad", "price": "1", "store": ""}
            )
        )
