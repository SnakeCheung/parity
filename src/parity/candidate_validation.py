"""Offline JD candidate planning and evidence merging; never launches a browser."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .collectors.jd_product import CrawlRequest, _sku_from_url
from .collectors.jd_search import SearchCandidate


def _identity(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"item.jd.com", "item.m.jd.com"}:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password or port not in {None, 80, 443}:
        return None
    return _sku_from_url(parsed.path)


def _spec_matches(expected: str, observed: str) -> bool:
    # Exact normalized phrase with numeric boundaries: 50ml must not match 150ml.
    normalize = lambda s: re.sub(r"\s+", "", s).casefold()
    return bool(re.search(r"(?<![\d.])" + re.escape(normalize(expected)) + r"(?![\d.])", normalize(observed)))


def _price(value: Any) -> str | None:
    from decimal import Decimal, InvalidOperation
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 < number < 1000000:
            rounded = number.quantize(Decimal("0.01"))
            return format(rounded, "f") if rounded > 0 else None
    except InvalidOperation:
        pass
    return None


def plan_candidates(document: dict[str, Any], expected_spec: str | None = None) -> dict[str, Any]:
    """Accept SearchResult or bridge JSON, dedupe valid identities, retain provenance."""
    if expected_spec is not None:
        expected_spec = expected_spec.strip()
        if not expected_spec:
            raise ValueError("expected_spec cannot be empty")
    rows = document.get("candidates", document.get("items"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Expected a candidates or items array of objects")
    entries: list[dict[str, Any]] = []
    by_sku: dict[str, dict[str, Any]] = {}
    tasks = []
    for index, raw in enumerate(rows):
        url = raw.get("url", raw.get("product_url"))
        url_sku = _identity(url)
        declared = raw.get("sku")
        sku = str(declared).strip() if declared is not None else url_sku
        valid = bool(url_sku and sku and sku.isascii() and sku.isdigit() and sku == url_sku)
        candidate = SearchCandidate(sku=sku, title=str(raw.get("title") or ""), url=url or "",
                                    page_price=raw.get("page_price", raw.get("price")),
                                    store_name_hint=raw.get("store_name_hint", raw.get("store_name")),
                                    jd_self_operated_hint=bool(raw.get("jd_self_operated_hint", False)))
        source = {"index": index, "raw": raw}
        if valid and sku in by_sku:
            by_sku[sku]["sources"].append(source)
            continue
        candidate_id = f"jd:{sku}" if valid else f"invalid:{index}"
        entry = {"candidate_id": candidate_id, "candidate": asdict(candidate), "sources": [source],
                 "status": "unverified" if valid else "sku_mismatch",
                 "reason": "Awaiting detail evidence" if valid else "Invalid JD URL or candidate SKU/URL conflict"}
        entries.append(entry)
        if valid:
            by_sku[sku] = entry
            tasks.append({"task_id": candidate_id, "candidate_id": candidate_id,
                          "request": asdict(CrawlRequest(f"https://item.jd.com/{sku}.html", expected_spec, sku))})
    return {"schema_version": 1, "source": {k: v for k, v in document.items() if k not in {"items", "candidates"}},
            "candidates": entries, "tasks": tasks}


def _evaluate(request: dict[str, Any], detail: dict[str, Any]) -> tuple[str, str, str | None]:
    status = detail.get("status")
    if status in {"rate_limited", "login_required", "captcha_required", "access_denied", "forbidden"} or detail.get("http_status") in {403, 429}:
        return "access_restricted", str(detail.get("reason") or status), None
    sku = request["expected_sku"]
    if (str(detail.get("sku")) != sku or _identity(detail.get("final_url")) != sku
            or detail.get("expected_sku") not in {None, sku} or status == "identity_changed"):
        return "sku_mismatch", "Detail SKU/final URL does not confirm candidate identity", None
    if status not in {"succeeded", "partial", "sku_ambiguous", "official_store_unverified"}:
        return "unverified", "Detail collection did not complete successfully", None
    expected = request.get("expected_spec")
    if expected and not _spec_matches(expected, str(detail.get("specification") or detail.get("title") or "")):
        return "spec_mismatch", "Expected specification not confirmed in detail evidence", None
    price = _price(detail.get("dom_price"))
    # Network lists alone have no price-to-SKU binding; use only successful single-SKU observations.
    if not price:
        prices = set()
        for observation in detail.get("network_observations", []):
            if observation.get("status") == 200 and observation.get("sku_candidates") == [sku]:
                prices.update(p for value in observation.get("price_candidates", []) if (p := _price(value)))
        if len(prices) == 1:
            price = prices.pop()
    if not price:
        return "price_evidence_insufficient", "No valid DOM price or uniquely SKU-bound network price", None
    if status != "succeeded" or detail.get("official_store_verified") is not True or detail.get("http_status") not in range(200, 300):
        return "unverified", "Collector success, successful HTTP and official store evidence required", None
    return "verified", "SKU, specification and detail price confirmed", price


def merge_results(plan: dict[str, Any], details: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge raw CrawlResult documents by requested URL, never by redirected SKU."""
    merged = json.loads(json.dumps(plan))
    grouped: dict[str, list[dict[str, Any]]] = {}
    unmatched = []
    task_ids = {task["request"]["expected_sku"] for task in merged["tasks"]}
    for detail in details:
        sku = _identity(detail.get("requested_url"))
        if sku not in task_ids:
            unmatched.append(detail)
        else:
            grouped.setdefault(sku, []).append(detail)
    entries = {entry["candidate_id"]: entry for entry in merged["candidates"]}
    for task in merged["tasks"]:
        entry = entries[task["candidate_id"]]
        evidence = grouped.get(task["request"]["expected_sku"], [])
        entry.pop("verified_price", None)
        entry["detail_results"] = evidence
        if not evidence:
            entry.update(status="unverified", reason="No detail result supplied")
        elif len(evidence) != 1:
            entry.update(status="unverified", reason="Multiple detail results; select one collection explicitly")
        else:
            status, reason, price = _evaluate(task["request"], evidence[0])
            entry.update(status=status, reason=reason)
            if status == "verified":
                entry["verified_price"] = price
    merged["unmatched_details"] = unmatched
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    prepare = sub.add_parser("plan")
    prepare.add_argument("--candidates", required=True, type=Path)
    prepare.add_argument("--expected-spec")
    merge = sub.add_parser("merge")
    merge.add_argument("--plan", required=True, type=Path)
    merge.add_argument("--details", required=True, nargs="+", type=Path)
    for command in (prepare, merge):
        command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    read = lambda path: json.loads(path.read_text(encoding="utf-8"))
    result = (plan_candidates(read(args.candidates), args.expected_spec) if args.operation == "plan"
              else merge_results(read(args.plan), [read(path) for path in args.details]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
