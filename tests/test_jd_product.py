"""Unit tests for the evidence and stop-condition logic of the crawler demo."""

from __future__ import annotations

import unittest

from parity.collectors.jd_product import (
    JD_LOGIN_TARGET_URL,
    _classify_page,
    _collect_json_candidates,
    _normalize_price,
    _sanitize_observation_url,
    _sku_from_url,
)


class SkincarePriceCrawlerDemoTest(unittest.TestCase):
    def test_login_starts_from_a_retail_product_instead_of_the_corporate_site(self) -> None:
        self.assertEqual(JD_LOGIN_TARGET_URL, "https://item.jd.com/100179356947.html")
        self.assertNotIn("corporate.jd.com", JD_LOGIN_TARGET_URL)

    def test_extracts_sku_from_desktop_and_mobile_urls(self) -> None:
        self.assertEqual(_sku_from_url("https://item.jd.com/100179356947.html"), "100179356947")
        self.assertEqual(
            _sku_from_url("https://item.m.jd.com/product/100179356947.html?foo=1"),
            "100179356947",
        )

    def test_normalizes_only_plausible_positive_prices(self) -> None:
        self.assertEqual(_normalize_price("￥ 699.9"), "699.90")
        self.assertIsNone(_normalize_price("价格待登录后查看"))
        self.assertIsNone(_normalize_price(0))

    def test_collects_price_and_sku_from_nested_json(self) -> None:
        prices, skus = _collect_json_candidates(
            {"data": {"skuId": "100179356947", "price": "699.00", "items": [{"p": "688"}]}}
        )
        self.assertEqual(prices, ["699.00", "688.00"])
        self.assertEqual(skus, ["100179356947"])

    def test_frequency_page_is_not_reported_as_empty_inventory(self) -> None:
        self.assertEqual(
            _classify_page(
                "https://pc-frequent-pro.pf.jd.com/?reason=403",
                "暂时无法展示该商品的信息，请稍后重试",
            ),
            ("rate_limited", "JD redirected to or rendered a frequency-control page"),
        )

    def test_login_redirect_is_explicit(self) -> None:
        status = _classify_page(
            "https://plogin.m.jd.com/login/login",
            "京东登录注册 账号密码登录",
        )
        self.assertEqual(status, ("login_required", "JD requires an authenticated browser profile"))

    def test_network_evidence_redacts_browser_tokens(self) -> None:
        sanitized = _sanitize_observation_url(
            "https://api.m.jd.com/api?functionId=price&skuid=123&h5st=secret&x-api-token=private"
        )
        self.assertIn("functionId=price", sanitized)
        self.assertIn("skuid=123", sanitized)
        self.assertNotIn("secret", sanitized)
        self.assertNotIn("private", sanitized)
