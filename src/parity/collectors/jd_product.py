"""Minimal real-browser collector for one explicit JD product URL.

This is intentionally independent from the Agent and RuntimeTask wiring. It
proves the browser collection boundary first: persistent profile, DOM plus
network observations, identity checks, evidence, and explicit stop statuses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit


_JD_HOSTS = {"item.jd.com", "item.m.jd.com"}
_PRICE_KEYS = {"p", "price", "jdprice", "saleprice", "finalprice", "payprice"}
_SKU_KEYS = {"sku", "skuid", "wareid", "productid"}
_NETWORK_URL_HINT = re.compile(r"price|sku|ware|item|product", re.IGNORECASE)
_PRICE_PATTERN = re.compile(r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)")
_SKU_PATH_PATTERN = re.compile(r"/(?:product/)?(\d+)\.html(?:$|[?#])")
JD_LOGIN_TARGET_URL = "https://item.jd.com/100179356947.html"


@dataclass(frozen=True)
class CrawlRequest:
    url: str
    expected_spec: str | None = None
    expected_sku: str | None = None


@dataclass
class NetworkObservation:
    url: str
    status: int
    price_candidates: list[str] = field(default_factory=list)
    sku_candidates: list[str] = field(default_factory=list)


@dataclass
class CrawlResult:
    status: str
    reason: str
    requested_url: str
    final_url: str
    collected_at: str
    http_status: int | None = None
    title: str | None = None
    sku: str | None = None
    expected_sku: str | None = None
    specification: str | None = None
    expected_spec: str | None = None
    store_name: str | None = None
    official_store_verified: bool = False
    dom_price: str | None = None
    network_price_candidates: list[str] = field(default_factory=list)
    network_observations: list[NetworkObservation] = field(default_factory=list)
    evidence_screenshot: str | None = None
    body_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _sku_from_url(url: str) -> str | None:
    match = _SKU_PATH_PATTERN.search(url)
    return match.group(1) if match else None


def _normalize_price(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    match = _PRICE_PATTERN.search(str(value).replace(",", ""))
    if not match:
        return None
    try:
        price = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if price <= 0 or price >= Decimal("1000000"):
        return None
    return format(price.quantize(Decimal("0.01")), "f")


def _collect_json_candidates(payload: Any) -> tuple[list[str], list[str]]:
    prices: list[str] = []
    skus: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key).replace("_", "").lower()
                if key in _PRICE_KEYS:
                    price = _normalize_price(child)
                    if price and price not in prices:
                        prices.append(price)
                if key in _SKU_KEYS and child is not None:
                    sku = str(child).strip()
                    if sku.isdigit() and sku not in skus:
                        skus.append(sku)
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:200]:
                visit(child, depth + 1)

    visit(payload)
    return prices[:20], skus[:20]


def _classify_page(final_url: str, body_text: str) -> tuple[str, str] | None:
    lowered_url = final_url.lower()
    compact_body = " ".join(body_text.split())
    if "pc-frequent" in lowered_url or any(
        marker in compact_body
        for marker in ("访问频繁", "暂时无法展示该商品的信息", "无法搜索，请稍后再试")
    ):
        return "rate_limited", "JD redirected to or rendered a frequency-control page"
    if any(marker in lowered_url for marker in ("passport.jd.com", "plogin.m.jd.com")) or any(
        marker in compact_body for marker in ("京东登录注册", "账号密码登录")
    ):
        return "login_required", "JD requires an authenticated browser profile"
    if any(marker in compact_body.lower() for marker in ("验证码", "captcha", "安全验证")):
        return "captcha_required", "JD requires human verification"
    return None


def _sanitize_observation_url(url: str) -> str:
    """Keep endpoint evidence without persisting browser signatures or tokens."""

    parts = urlsplit(url)
    safe_keys = {"appid", "client", "clientVersion", "functionId", "skuid"}
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        safe_query.append((key, value if key in safe_keys else "<redacted>"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


async def _first_text(page: Any, selectors: list[str]) -> str | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            text = _normalize_text(await locator.text_content(timeout=1200))
        except Exception:
            continue
        if text:
            return text
    return None


def _discover_browser_executable(explicit: str | None) -> str | None:
    configured = explicit or os.getenv("PRICE_CRAWLER_BROWSER_EXECUTABLE")
    if configured:
        return configured
    if os.name != "nt":
        return None
    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    )
    return str(next((path for path in candidates if path.exists()), "")) or None


async def crawl_jd_product(
    request: CrawlRequest,
    *,
    profile_dir: str | Path,
    evidence_dir: str | Path,
    browser_executable: str | None = None,
    headless: bool = True,
    navigation_timeout_seconds: float = 45.0,
) -> CrawlResult:
    """Visit one explicit JD item URL and return only evidence-backed fields."""

    parsed = urlparse(request.url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _JD_HOSTS:
        raise ValueError("The demo accepts only explicit item.jd.com/item.m.jd.com product URLs")

    expected_sku = request.expected_sku or _sku_from_url(request.url)
    profile_path = Path(profile_dir).resolve()
    evidence_path = Path(evidence_dir).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    evidence_path.mkdir(parents=True, exist_ok=True)
    screenshot_path = evidence_path / "page.png"
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e ./parity && playwright install chromium"
        ) from exc

    observations: list[NetworkObservation] = []
    response_tasks: set[asyncio.Task[None]] = set()

    async def capture_response(response: Any) -> None:
        if len(observations) >= 30 or not _NETWORK_URL_HINT.search(response.url):
            return
        content_type = (response.headers.get("content-type") or "").lower()
        response_host = urlparse(response.url).hostname or ""
        if "json" not in content_type and response_host != "api.m.jd.com":
            return
        prices: list[str] = []
        skus: list[str] = []
        try:
            payload = await asyncio.wait_for(response.json(), timeout=2.0)
            prices, skus = _collect_json_candidates(payload)
        except Exception:
            pass
        observations.append(
            NetworkObservation(
                url=_sanitize_observation_url(response.url)[:1000],
                status=response.status,
                price_candidates=prices,
                sku_candidates=skus,
            )
        )

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": headless,
            "chromium_sandbox": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1280, "height": 900},
        }
        executable = _discover_browser_executable(browser_executable)
        if executable:
            launch_options["executable_path"] = executable
        context = await playwright.chromium.launch_persistent_context(str(profile_path), **launch_options)
        page = context.pages[0] if context.pages else await context.new_page()

        def on_response(response: Any) -> None:
            task = asyncio.create_task(capture_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", on_response)
        response = None
        try:
            for attempt in range(2):
                try:
                    response = await page.goto(
                        request.url,
                        wait_until="domcontentloaded",
                        timeout=int(navigation_timeout_seconds * 1000),
                    )
                    break
                except Exception:
                    if attempt:
                        raise
                    await page.wait_for_timeout(1500)
            await page.wait_for_timeout(6000)
            body_text = await page.locator("body").inner_text(timeout=5000)
            final_url = page.url
            page_title = _normalize_text(await page.title())
            classification = _classify_page(final_url, body_text)
            final_sku = _sku_from_url(final_url)

            dom_name = await _first_text(page, [".sku-name", ".itemInfo-wrap .sku-name"])
            product_title = dom_name or page_title
            store_name = await _first_text(
                page,
                ["#crumb-wrap .contact .name a", ".J-hove-wrap .name a", ".shopName"],
            )
            dom_price = _normalize_price(
                await _first_text(page, [".summary-price .p-price", ".p-price .price"])
            )

            pending_responses = list(response_tasks)
            if pending_responses:
                await asyncio.wait(pending_responses, timeout=3.0)
            network_prices = list(
                dict.fromkeys(
                    price
                    for observation in observations
                    for price in observation.price_candidates
                )
            )
            official_verified = bool(store_name and "京东自营" in store_name)
            spec = (
                request.expected_spec
                if request.expected_spec
                and product_title
                and request.expected_spec.lower() in product_title.lower()
                else None
            )

            if classification:
                status, reason = classification
            elif expected_sku and final_sku != expected_sku:
                status, reason = "identity_changed", "Final page SKU differs from the requested SKU"
            elif request.expected_spec and not spec:
                status, reason = "sku_ambiguous", "The expected specification was not confirmed on the page"
            elif not official_verified:
                status, reason = "official_store_unverified", "JD self-operated store identity was not confirmed"
            elif not (dom_price or network_prices):
                status, reason = "partial", "Product identity loaded, but no current price was confirmed"
            else:
                status, reason = "succeeded", "Product identity, official store, and a current price were confirmed"

            try:
                await page.screenshot(path=str(screenshot_path), full_page=False, timeout=10000)
                screenshot = str(screenshot_path)
            except Exception:
                screenshot = None

            result = CrawlResult(
                status=status,
                reason=reason,
                requested_url=request.url,
                final_url=final_url,
                collected_at=collected_at,
                http_status=response.status if response else None,
                title=product_title,
                sku=final_sku,
                expected_sku=expected_sku,
                specification=spec,
                expected_spec=request.expected_spec,
                store_name=store_name,
                official_store_verified=official_verified,
                dom_price=dom_price,
                network_price_candidates=network_prices,
                network_observations=observations,
                evidence_screenshot=screenshot,
                body_excerpt=_normalize_text(body_text[:1200]),
            )
        except Exception as exc:
            result = CrawlResult(
                status="page_changed",
                reason=f"Browser collection failed: {type(exc).__name__}: {exc}",
                requested_url=request.url,
                final_url=page.url,
                collected_at=collected_at,
                expected_sku=expected_sku,
                expected_spec=request.expected_spec,
                network_observations=observations,
            )
        finally:
            await context.close()

    (evidence_path / "result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


async def login_jd_profile(
    *,
    profile_dir: str | Path,
    browser_executable: str | None = None,
    timeout_seconds: float = 600.0,
) -> bool:
    """Open a visible JD QR login page and persist its authenticated profile."""

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e ./parity && playwright install chromium"
        ) from exc

    profile_path = Path(profile_dir).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": False,
            "chromium_sandbox": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1280, "height": 900},
        }
        executable = _discover_browser_executable(browser_executable)
        if executable:
            launch_options["executable_path"] = executable
        context = await playwright.chromium.launch_persistent_context(str(profile_path), **launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(
                JD_LOGIN_TARGET_URL,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            print("LOGIN_WINDOW_OPENED", flush=True)
            while time.monotonic() < deadline:
                if page.is_closed():
                    print("LOGIN_WINDOW_CLOSED_BEFORE_CONFIRMATION", flush=True)
                    return False
                cookie_names = {cookie["name"] for cookie in await context.cookies()}
                login_cookie_present = {"pt_key", "pt_pin"}.issubset(cookie_names)
                still_on_login_page = "passport.jd.com" in page.url or "plogin.m.jd.com" in page.url
                if login_cookie_present and not still_on_login_page:
                    print("LOGIN_CONFIRMED_PROFILE_SAVED", flush=True)
                    return True
                await page.wait_for_timeout(1000)
            print("LOGIN_TIMEOUT_PROFILE_CLOSED", flush=True)
            return False
        finally:
            await context.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl one explicit JD skincare product page")
    parser.add_argument("--url", help="item.jd.com or item.m.jd.com product URL")
    parser.add_argument("--expected-spec", help="Exact specification expected in the title, e.g. 50ml")
    parser.add_argument("--expected-sku", help="Expected JD SKU; defaults to the URL SKU")
    parser.add_argument("--profile-dir", default="artifacts/price-crawler/profile-jd")
    parser.add_argument("--evidence-dir", default="artifacts/price-crawler/latest")
    parser.add_argument("--browser-executable")
    parser.add_argument("--headed", action="store_true", help="Show the browser for manual login")
    parser.add_argument("--login-only", action="store_true", help="Open QR login and save the profile")
    parser.add_argument("--login-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--local-dev", action="store_true", help="Explicitly allow host execution")
    parser.add_argument("--sandbox-worker", action="store_true", help=argparse.SUPPRESS)
    return parser


async def _main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    if not (args.local_dev or args.sandbox_worker):
        print(
            json.dumps(
                {
                    "status": "configuration_error",
                    "reason": "Direct host execution is disabled; use e2b_runner or pass --local-dev explicitly",
                },
                ensure_ascii=False,
            )
        )
        return 2
    if args.login_only:
        logged_in = await login_jd_profile(
            profile_dir=args.profile_dir,
            browser_executable=args.browser_executable,
            timeout_seconds=args.login_timeout_seconds,
        )
        return 0 if logged_in else 2
    if not args.url:
        raise SystemExit("--url is required unless --login-only is used")
    result = await crawl_jd_product(
        CrawlRequest(url=args.url, expected_spec=args.expected_spec, expected_sku=args.expected_sku),
        profile_dir=args.profile_dir,
        evidence_dir=args.evidence_dir,
        browser_executable=args.browser_executable,
        headless=not args.headed,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status in {"succeeded", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
