from __future__ import annotations

import http.client
import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from parity.bridge_server import (
    BridgeConfigurationError,
    build_server,
    ensure_bridge_token,
)


TOKEN = "x" * 32
ORIGIN = "chrome-extension://" + "a" * 32


def _payload() -> dict:
    return {
        "schema_version": 1,
        "site": "jd_search",
        "page_url": "https://search.jd.com/Search?keyword=%E6%AC%A7%E8%8E%B1%E9%9B%85",
        "page_title": "\u6b27\u83b1\u96c5 - \u5546\u54c1\u641c\u7d22",
        "query": "\u6b27\u83b1\u96c5",
        "collected_at": "2026-08-08T01:30:00+08:00",
        "cookie": "must-never-be-stored",
        "raw_html": "must-never-be-stored",
        "items": [
            {
                "position": 1,
                "sku": "100123456789",
                "title": "\u6b27\u83b1\u96c5\u9762\u971c 50ml",
                "price": "\uffe5699.9",
                "store_name": "\u6b27\u83b1\u96c5\u5b98\u65b9\u65d7\u8230\u5e97",
                "product_url": "https://item.jd.com/100123456789.html",
                "cookie": "must-never-be-stored",
                "raw_html": "must-never-be-stored",
            }
        ],
    }


def _request(server, method: str, path: str, *, token: str | None = None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Origin": ORIGIN}
    encoded = None
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-Parity-Token"] = token
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8")) if response.length != 0 else None
    connection.close()
    return response.status, payload


def test_bridge_accepts_allowlisted_fields_and_discards_sensitive_unknowns():
    with TemporaryDirectory() as temp_dir:
        server = build_server(port=0, token=TOKEN, output_dir=temp_dir)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, response = _request(
                server, "POST", "/v1/collect", token=TOKEN, body=_payload()
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        assert status == 202
        assert response["status"] == "accepted"
        assert response["item_count"] == 1
        saved = json.loads((Path(temp_dir) / "latest.json").read_text(encoding="utf-8"))
        assert saved["query"] == "\u6b27\u83b1\u96c5"
        assert saved["items"][0]["price"] == "699.90"
        assert "cookie" not in saved
        assert "raw_html" not in saved
        assert "cookie" not in saved["items"][0]
        assert "raw_html" not in saved["items"][0]


def test_bridge_rejects_missing_token_without_writing_collection():
    with TemporaryDirectory() as temp_dir:
        server = build_server(port=0, token=TOKEN, output_dir=temp_dir)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, response = _request(server, "POST", "/v1/collect", body=_payload())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        assert status == 401
        assert response["status"] == "unauthorized"
        assert not (Path(temp_dir) / "latest.json").exists()


def test_bridge_rejects_non_jd_search_page():
    payload = _payload()
    payload["page_url"] = "https://example.com/steal"
    with TemporaryDirectory() as temp_dir:
        server = build_server(port=0, token=TOKEN, output_dir=temp_dir)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, response = _request(
                server, "POST", "/v1/collect", token=TOKEN, body=payload
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        assert status == 400
        assert response["status"] == "invalid_payload"


def test_bridge_refuses_non_loopback_bind():
    with pytest.raises(BridgeConfigurationError, match="loopback"):
        build_server(host="0.0.0.0", port=0, token=TOKEN)


def test_init_token_writes_secret_without_returning_it():
    with TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
        env_path = Path(temp_dir) / ".env"
        created = ensure_bridge_token(env_path)

        assert created is True
        text = env_path.read_text(encoding="utf-8")
        assert "PARITY_BRIDGE_TOKEN=" in text
        assert len(text.split("=", 1)[1].strip()) >= 32
