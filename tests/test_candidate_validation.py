"""Deterministic tests: no browser, JD access or E2B required."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from parity.candidate_validation import merge_results, plan_candidates


@pytest.fixture
def plan():
    return plan_candidates({"collection_id": "test", "items": [
        {"sku": "10001", "title": "cream 50ml", "product_url": "https://item.jd.com/10001.html", "price": "1"},
        {"sku": None, "title": "duplicate", "product_url": "https://item.m.jd.com/product/10001.html?x=1"},
    ]}, "50ml")


@pytest.fixture
def detail():
    return {"status": "succeeded", "reason": "ok", "requested_url": "https://item.jd.com/10001.html",
            "final_url": "https://item.jd.com/10001.html", "sku": "10001", "expected_sku": "10001",
            "title": "cream 50ml", "http_status": 200, "official_store_verified": True,
            "dom_price": "20.00"}


def test_bridge_dedupe_and_provenance(plan):
    assert len(plan["candidates"]) == len(plan["tasks"]) == 1
    assert len(plan["candidates"][0]["sources"]) == 2
    assert plan["source"]["collection_id"] == "test"
    assert plan["tasks"][0]["request"]["expected_spec"] == "50ml"
    assert plan["candidates"][0]["status"] == "unverified"


def test_search_schema():
    from dataclasses import asdict
    from parity.collectors.jd_search import SearchCandidate
    candidate = SearchCandidate("10001", "cream", "https://item.jd.com/10001.html", "1")
    plan = plan_candidates({"candidates": [asdict(candidate)]})
    assert plan["candidates"][0]["candidate"] == asdict(candidate)


@pytest.mark.parametrize("url,sku", [
    ("https://item.jd.com/10002.html", "10001"),
    ("https://item.jd.com.evil.test/10001.html", "10001"),
    ("https://example.com/10001.html", "10001"),
    ("https://item.jd.com/10001.html", "oops"),
    (None, "10001"),
])
def test_invalid_candidate_not_scheduled(url, sku):
    plan = plan_candidates({"items": [{"sku": sku, "product_url": url}]})
    assert not plan["tasks"]
    assert plan["candidates"][0]["status"] == "sku_mismatch"


def test_verified_and_no_mutation(plan, detail):
    original = deepcopy(plan)
    merged = merge_results(plan, [detail])
    assert plan == original
    entry = merged["candidates"][0]
    assert entry["status"] == "verified"
    assert entry["verified_price"] == "20.00"
    assert entry["detail_results"] == [detail]
    assert entry["candidate"]["page_price"] == "1"


@pytest.mark.parametrize("change,status", [
    ({"sku": "10002", "final_url": "https://item.jd.com/10002.html"}, "sku_mismatch"),
    ({"expected_sku": "10002"}, "sku_mismatch"),
    ({"title": "cream 150ml"}, "spec_mismatch"),
    ({"title": None}, "spec_mismatch"),
    ({"dom_price": None, "network_price_candidates": ["20"]}, "price_evidence_insufficient"),
    ({"dom_price": "NaN"}, "price_evidence_insufficient"),
    ({"dom_price": "-20"}, "price_evidence_insufficient"),
    ({"dom_price": "20-30"}, "price_evidence_insufficient"),
    ({"http_status": 403}, "access_restricted"),
    ({"http_status": 429}, "access_restricted"),
    ({"status": "captcha_required"}, "access_restricted"),
    ({"status": "login_required"}, "access_restricted"),
    ({"status": "rate_limited"}, "access_restricted"),
    ({"status": "page_changed"}, "unverified"),
    ({"official_store_verified": False}, "unverified"),
    ({"http_status": None}, "unverified"),
    ({"status": "partial"}, "unverified"),
])
def test_statuses(plan, detail, change, status):
    detail.update(change)
    entry = merge_results(plan, [detail])["candidates"][0]
    assert entry["status"] == status
    assert "verified_price" not in entry


def test_missing_ambiguous_and_unrelated_details(plan, detail):
    for details in ([], [detail, detail]):
        assert merge_results(plan, details)["candidates"][0]["status"] == "unverified"
    detail["requested_url"] = "https://item.jd.com/99999.html"
    result = merge_results(plan, [detail])
    assert result["candidates"][0]["status"] == "unverified"
    assert result["unmatched_details"] == [detail]


def test_sku_bound_network_price(plan, detail):
    detail["dom_price"] = None
    observation = {"status": 200, "sku_candidates": ["10001"], "price_candidates": ["20"]}
    detail["network_observations"] = [observation]
    assert merge_results(plan, [detail])["candidates"][0]["status"] == "verified"
    for change in ({"sku_candidates": ["10002"]}, {"price_candidates": ["20", "30"]}, {"status": 403}):
        detail["network_observations"] = [{**observation, **change}]
        assert merge_results(plan, [detail])["candidates"][0]["status"] == "price_evidence_insufficient"


def test_removing_evidence_clears_previous_success(plan, detail):
    verified = merge_results(plan, [detail])
    entry = merge_results(verified, [])["candidates"][0]
    assert entry["status"] == "unverified"
    assert "verified_price" not in entry


def test_cli_round_trip(tmp_path, detail):
    candidates = tmp_path / "latest.json"
    candidates.write_text(json.dumps({"items": [{"sku": "10001", "product_url": detail["requested_url"]}]}))
    evidence = tmp_path / "result.json"
    evidence.write_text(json.dumps(detail))
    plan_path, merged_path = tmp_path / "plan.json", tmp_path / "merged.json"
    command = [sys.executable, "-m", "parity.candidate_validation"]
    subprocess.run(command + ["plan", "--candidates", str(candidates), "--expected-spec", "50ml", "--output", str(plan_path)], check=True)
    subprocess.run(command + ["merge", "--plan", str(plan_path), "--details", str(evidence), "--output", str(merged_path)], check=True)
    assert json.loads(merged_path.read_text())["candidates"][0]["status"] == "verified"
