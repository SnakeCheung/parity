"""Loopback-only receiver for user-triggered browser extension collections."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BRIDGE_TOKEN_ENV = "PARITY_BRIDGE_TOKEN"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_OUTPUT_DIR = Path("artifacts/bridge/inbox")
MAX_BODY_BYTES = 1_000_000
MAX_ITEMS = 200
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")
_SKU = re.compile(r"^\d{5,30}$")


class BridgeConfigurationError(RuntimeError):
    """Raised before binding a socket when bridge configuration is unsafe."""


class PayloadError(ValueError):
    """Raised when extension input fails the narrow collection schema."""


def _load_local_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    package_dir = Path(__file__).resolve().parent
    for candidate in (
        Path.cwd() / ".env",
        package_dir / ".env",
        package_dir.parent / ".env",
        package_dir.parent.parent / ".env",
    ):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _valid_token(value: str) -> bool:
    return len(value) >= 32 and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def require_bridge_token() -> str:
    _load_local_env()
    token = os.getenv(BRIDGE_TOKEN_ENV, "").strip()
    if not _valid_token(token):
        raise BridgeConfigurationError(
            f"{BRIDGE_TOKEN_ENV} is missing or invalid. Run: "
            "python -m parity.bridge_server init-token --env-file .env"
        )
    return token


def ensure_bridge_token(env_file: str | Path = ".env") -> bool:
    """Create a random bridge token without returning or logging its value."""

    current = os.getenv(BRIDGE_TOKEN_ENV, "").strip()
    if current:
        if not _valid_token(current):
            raise BridgeConfigurationError(f"Existing {BRIDGE_TOKEN_ENV} is invalid")
        return False

    env_path = Path(env_file).resolve()
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    for line in existing.splitlines():
        if line.strip().startswith(f"{BRIDGE_TOKEN_ENV}="):
            value = line.split("=", 1)[1].strip()
            if not _valid_token(value):
                raise BridgeConfigurationError(f"Existing {BRIDGE_TOKEN_ENV} is invalid")
            os.environ[BRIDGE_TOKEN_ENV] = value
            return False

    token = secrets.token_urlsafe(32)
    separator = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    env_path.write_text(
        f"{existing}{separator}{BRIDGE_TOKEN_ENV}={token}\n", encoding="utf-8"
    )
    os.environ[BRIDGE_TOKEN_ENV] = token
    return True


def _text(value: Any, *, maximum: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise PayloadError("required text field is missing")
        return None
    normalized = " ".join(str(value).split()).strip()
    if required and not normalized:
        raise PayloadError("required text field is empty")
    return normalized[:maximum] or None


def _jd_url(value: Any, *, search_page: bool = False) -> str | None:
    text = _text(value, maximum=2000, required=search_page)
    if not text:
        return None
    parsed = urlparse(text)
    allowed_hosts = {"search.jd.com"} if search_page else {"item.jd.com", "item.m.jd.com"}
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts:
        if search_page:
            raise PayloadError("page_url must be an HTTPS search.jd.com URL")
        return None
    return text


def _price(value: Any) -> str | None:
    text = _text(value, maximum=40)
    if not text:
        return None
    text = text.replace("¥", "").replace("￥", "").replace(",", "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number <= 0 or number >= Decimal("10000000"):
        return None
    return f"{number:.2f}"


def validate_collection(payload: Any) -> dict[str, Any]:
    """Return a strict allowlisted representation; discard all unknown input fields."""

    if not isinstance(payload, dict):
        raise PayloadError("request body must be a JSON object")
    if payload.get("schema_version") != 1 or payload.get("site") != "jd_search":
        raise PayloadError("unsupported collection schema or site")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise PayloadError("items must be a JSON array")
    if len(raw_items) > MAX_ITEMS:
        raise PayloadError(f"items exceeds the limit of {MAX_ITEMS}")

    clean_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            continue
        sku = _text(raw.get("sku"), maximum=30)
        if sku and not _SKU.fullmatch(sku):
            sku = None
        product_url = _jd_url(raw.get("product_url"))
        title = _text(raw.get("title"), maximum=500, required=True)
        identity = sku or product_url
        if not identity or identity in seen:
            continue
        seen.add(identity)
        position = raw.get("position", index)
        if not isinstance(position, int) or not 1 <= position <= 1000:
            position = index
        clean_items.append(
            {
                "position": position,
                "sku": sku,
                "title": title,
                "price": _price(raw.get("price")),
                "currency": "CNY",
                "store_name": _text(raw.get("store_name"), maximum=200),
                "product_url": product_url,
            }
        )

    return {
        "schema_version": 1,
        "site": "jd_search",
        "page_url": _jd_url(payload.get("page_url"), search_page=True),
        "page_title": _text(payload.get("page_title"), maximum=500),
        "query": _text(payload.get("query"), maximum=200),
        "collected_at": _text(payload.get("collected_at"), maximum=50),
        "items": clean_items,
    }


def save_collection(collection: dict[str, Any], output_dir: str | Path) -> tuple[str, Path]:
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    collection_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
    document = {
        "collection_id": collection_id,
        "received_at": now.isoformat(timespec="seconds"),
        **collection,
    }
    encoded = json.dumps(document, ensure_ascii=False, indent=2)
    destination = output_path / f"{collection_id}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(destination)

    latest = output_path / "latest.json"
    latest_temporary = output_path / "latest.json.tmp"
    latest_temporary.write_text(encoded, encoding="utf-8")
    latest_temporary.replace(latest)
    return collection_id, destination


class BridgeServer(ThreadingHTTPServer):
    token: str
    output_dir: Path


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def _allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin", "")
        return origin if _EXTENSION_ORIGIN.fullmatch(origin) else None

    def _security_headers(self) -> None:
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path != "/v1/collect" or not self._allowed_origin():
            self._json_response(403, {"status": "forbidden"})
            return
        self.send_response(204)
        self._security_headers()
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Parity-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json_response(200, {"status": "ok", "service": "parity-bridge"})
            return
        self._json_response(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/collect":
            self._json_response(404, {"status": "not_found"})
            return
        if not self._allowed_origin():
            self._json_response(403, {"status": "forbidden", "reason": "extension origin required"})
            return
        supplied = self.headers.get("X-Parity-Token", "")
        if not hmac.compare_digest(supplied, self.server.token):
            self._json_response(401, {"status": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY_BYTES:
            self._json_response(413, {"status": "payload_too_large"})
            return
        try:
            raw = self.rfile.read(length)
            collection = validate_collection(json.loads(raw.decode("utf-8")))
            collection_id, _ = save_collection(collection, self.server.output_dir)
        except (UnicodeDecodeError, json.JSONDecodeError, PayloadError) as exc:
            self._json_response(400, {"status": "invalid_payload", "reason": str(exc)})
            return
        self._json_response(
            202,
            {
                "status": "accepted",
                "collection_id": collection_id,
                "item_count": len(collection["items"]),
            },
        )

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(
            f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
            f"{self.client_address[0]} {format % args}\n"
        )


def build_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> BridgeServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise BridgeConfigurationError("The bridge must bind to loopback only")
    if not _valid_token(token):
        raise BridgeConfigurationError("The bridge token is invalid")
    server = BridgeServer((host, port), BridgeHandler)
    server.token = token
    server.output_dir = Path(output_dir).resolve()
    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parity's loopback browser-extension bridge")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    init_token = subparsers.add_parser("init-token")
    init_token.add_argument("--env-file", default=".env")
    subparsers.add_parser("show-token")
    serve = subparsers.add_parser("serve")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser


def _main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    try:
        if args.operation == "init-token":
            created = ensure_bridge_token(args.env_file)
            print(json.dumps({"status": "ok", "token_created": created}))
            return 0
        if args.operation == "show-token":
            print(require_bridge_token())
            return 0

        token = require_bridge_token()
        server = build_server(port=args.port, token=token, output_dir=args.output_dir)
        print(f"PARITY_BRIDGE_READY=http://{DEFAULT_HOST}:{server.server_port}", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except BridgeConfigurationError as exc:
        print(json.dumps({"status": "configuration_error", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
