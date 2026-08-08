"""Minimal JD search-stage crawler for producing unverified product candidates."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

from .jd_product import (
    _classify_page,
    _discover_browser_executable,
    _normalize_price,
    _normalize_text,
    _sku_from_url,
)


@dataclass
class SearchCandidate:
    sku: str | None
    title: str
    url: str
    page_price: str | None = None
    store_name_hint: str | None = None
    jd_self_operated_hint: bool = False


@dataclass
class SearchResult:
    status: str
    reason: str
    query: str
    requested_url: str
    final_url: str
    collected_at: str
    http_status: int | None = None
    candidates: list[SearchCandidate] = field(default_factory=list)
    evidence_screenshot: str | None = None
    body_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_jd_search_url(query: str) -> str:
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("Search query cannot be empty")
    if len(normalized) > 120:
        raise ValueError("Search query is too long")
    return "https://search.jd.com/Search?" + urlencode({"keyword": normalized, "enc": "utf-8"})


def _candidate_from_browser(raw: dict[str, Any]) -> SearchCandidate | None:
    title = _normalize_text(str(raw.get("title") or ""))
    href = str(raw.get("url") or "").strip()
    if not title or not href:
        return None
    url = urljoin("https://search.jd.com/", href)
    if "item.jd.com" not in url:
        return None
    store = _normalize_text(str(raw.get("store") or ""))
    sku = str(raw.get("sku") or "").strip() or _sku_from_url(url)
    if sku and not sku.isdigit():
        sku = _sku_from_url(url)
    return SearchCandidate(
        sku=sku,
        title=title,
        url=url,
        page_price=_normalize_price(raw.get("price")),
        store_name_hint=store,
        jd_self_operated_hint=bool(store and "京东自营" in store),
    )


async def search_jd_products(
    query: str,
    *,
    profile_dir: str | Path,
    evidence_dir: str | Path,
    browser_executable: str | None = None,
    headless: bool = True,
    limit: int = 10,
    navigation_timeout_seconds: float = 45.0,
) -> SearchResult:
    """Search JD and return candidates; no candidate is considered verified."""

    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    requested_url = build_jd_search_url(query)
    profile_path = Path(profile_dir).resolve()
    evidence_path = Path(evidence_dir).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    evidence_path.mkdir(parents=True, exist_ok=True)
    screenshot_path = evidence_path / "search-page.png"
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e ./parity && playwright install chromium"
        ) from exc

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": headless,
            "chromium_sandbox": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1365, "height": 900},
        }
        executable = _discover_browser_executable(browser_executable)
        if executable:
            launch_options["executable_path"] = executable
        context = await playwright.chromium.launch_persistent_context(str(profile_path), **launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        response = None
        try:
            response = await page.goto(
                requested_url,
                wait_until="domcontentloaded",
                timeout=int(navigation_timeout_seconds * 1000),
            )
            await page.wait_for_timeout(6000)
            body_text = await page.locator("body").inner_text(timeout=5000)
            final_url = page.url
            classification = _classify_page(final_url, body_text)

            raw_candidates = await page.locator(
                "#J_goodsList li.gl-item, #J_goodsList [data-sku], li[data-sku].gl-item"
            ).evaluate_all(
                """
                (nodes, limit) => nodes.slice(0, limit).map((node) => {
                  const link = node.querySelector('.p-name a, a[href*="item.jd.com"]');
                  const titleNode = node.querySelector('.p-name, [class*="name"]');
                  const priceNode = node.querySelector('.p-price i, .p-price .price, [class*="price"]');
                  const storeNode = node.querySelector('.p-shop a, .p-shop, [class*="shop"]');
                  return {
                    sku: node.getAttribute('data-sku') || node.dataset?.sku || null,
                    title: link?.getAttribute('title') || titleNode?.textContent || link?.textContent || '',
                    url: link?.href || link?.getAttribute('href') || '',
                    price: priceNode?.textContent || '',
                    store: storeNode?.textContent || ''
                  };
                })
                """,
                limit,
            )
            candidates = []
            seen_skus: set[str] = set()
            for raw in raw_candidates:
                candidate = _candidate_from_browser(raw)
                if not candidate:
                    continue
                dedupe_key = candidate.sku or candidate.url
                if dedupe_key in seen_skus:
                    continue
                seen_skus.add(dedupe_key)
                candidates.append(candidate)

            if classification:
                status, reason = classification
                candidates = []
            elif candidates:
                status, reason = "succeeded", "JD returned unverified product candidates"
            else:
                status, reason = "partial", "Search page loaded, but no product cards were extracted"

            try:
                await page.screenshot(path=str(screenshot_path), full_page=False, timeout=10000)
                screenshot = str(screenshot_path)
            except Exception:
                screenshot = None

            result = SearchResult(
                status=status,
                reason=reason,
                query=" ".join(query.split()),
                requested_url=requested_url,
                final_url=final_url,
                collected_at=collected_at,
                http_status=response.status if response else None,
                candidates=candidates,
                evidence_screenshot=screenshot,
                body_excerpt=_normalize_text(body_text[:1200]),
            )
        except Exception as exc:
            result = SearchResult(
                status="page_changed",
                reason=f"Browser search failed: {type(exc).__name__}: {exc}",
                query=" ".join(query.split()),
                requested_url=requested_url,
                final_url=page.url,
                collected_at=collected_at,
            )
        finally:
            await context.close()

    (evidence_path / "search-result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search JD for unverified skincare candidates")
    parser.add_argument("--query", required=True, help="Exact brand, product name, and specification")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--profile-dir", default="artifacts/price-crawler/profile-jd")
    parser.add_argument("--evidence-dir", default="artifacts/price-crawler/search-latest")
    parser.add_argument("--browser-executable")
    parser.add_argument("--headed", action="store_true")
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
    result = await search_jd_products(
        args.query,
        profile_dir=args.profile_dir,
        evidence_dir=args.evidence_dir,
        browser_executable=args.browser_executable,
        headless=not args.headed,
        limit=args.limit,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status in {"succeeded", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
