"""Run JD collectors inside an actual E2B Desktop microVM.

The local process is only a control plane: it creates the sandbox, uploads the
collector source, downloads structured evidence, and always destroys the VM.
Chromium and Playwright execute inside E2B.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Protocol

from .profile_store import DEFAULT_PROFILE_ARCHIVE, decrypt_archive, encrypt_archive


REMOTE_APP_DIR = "/home/user/parity"
REMOTE_OUTPUT_DIR = "/home/user/output"
REMOTE_PROFILE_DIR = "/home/user/state/profile-jd"
REMOTE_PROFILE_ARCHIVE = "/home/user/output/profile-jd.tar.gz"
DEFAULT_TEMPLATE = "desktop"


class E2BConfigurationError(RuntimeError):
    """Raised before a cloud sandbox is created when credentials are absent."""


class _Files(Protocol):
    def write(self, path: str, data: str | bytes, **kwargs: Any) -> Any: ...

    def read(self, path: str, **kwargs: Any) -> str | bytes: ...


class _Commands(Protocol):
    def run(self, command: str, **kwargs: Any) -> Any: ...


class _Desktop(Protocol):
    sandbox_id: str
    files: _Files
    commands: _Commands

    def kill(self) -> Any: ...


@dataclass(frozen=True)
class E2BRunEnvelope:
    runtime: str
    template: str
    sandbox_id: str
    sandbox_destroyed: bool
    started_at: str
    elapsed_seconds: float
    result: dict[str, Any]
    local_result_path: str
    local_screenshot_path: str | None
    profile_restored: bool
    profile_saved: bool
    profile_archive_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_local_env() -> None:
    """Load only local .env files; never log credential values."""

    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    package_dir = Path(__file__).resolve().parent
    candidates = (
        Path.cwd() / ".env",
        package_dir / ".env",
        package_dir.parent / ".env",
        package_dir.parent.parent / ".env",
    )
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _require_api_key() -> None:
    _load_local_env()
    if not os.getenv("E2B_API_KEY"):
        raise E2BConfigurationError(
            "E2B_API_KEY is missing. Add it to parity/.env or the current process environment."
        )


def _default_desktop_factory(**kwargs: Any) -> _Desktop:
    try:
        from e2b_desktop import Sandbox
    except ImportError as exc:
        raise E2BConfigurationError(
            "e2b-desktop is not installed. Run: pip install -e ./parity"
        ) from exc
    return Sandbox.create(**kwargs)


def _collector_sources() -> dict[str, bytes]:
    package_dir = Path(__file__).resolve().parent
    relative_paths = (
        Path("__init__.py"),
        Path("collectors/__init__.py"),
        Path("collectors/jd_product.py"),
        Path("collectors/jd_search.py"),
    )
    return {
        relative.as_posix(): (package_dir / relative).read_bytes()
        for relative in relative_paths
    }


def _checked_command(
    desktop: _Desktop,
    command: str,
    *,
    timeout: float,
    allowed_exit_codes: frozenset[int] = frozenset({0}),
) -> Any:
    try:
        result = desktop.commands.run(command, timeout=timeout)
    except Exception as exc:
        # Current E2B SDK versions raise CommandExitException instead of
        # returning CommandResult for every non-zero exit. Collectors reserve
        # exit code 2 for an expected stop state while still writing evidence.
        exit_code = getattr(exc, "exit_code", None)
        if exit_code in allowed_exit_codes:
            return exc
        raise
    exit_code = getattr(result, "exit_code", 0)
    if exit_code not in allowed_exit_codes:
        stderr = str(getattr(result, "stderr", ""))[-2000:]
        raise RuntimeError(f"E2B command failed with exit code {exit_code}: {stderr}")
    return result


def _upload_worker(desktop: _Desktop) -> None:
    _checked_command(
        desktop,
        f"mkdir -p {REMOTE_APP_DIR}/parity/collectors {REMOTE_OUTPUT_DIR} {REMOTE_PROFILE_DIR}",
        timeout=30,
    )
    for relative, content in _collector_sources().items():
        desktop.files.write(f"{REMOTE_APP_DIR}/parity/{relative}", content)


def _install_remote_runtime(desktop: _Desktop, *, install_cjk_fonts: bool = False) -> None:
    _checked_command(
        desktop,
        "python3 -m pip install --user --disable-pip-version-check "
        "'playwright>=1.45.0,<2.0.0'",
        timeout=180,
    )
    if install_cjk_fonts:
        _checked_command(
            desktop,
            "if ! fc-list :lang=zh | grep -q .; then "
            "sudo apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fonts-wqy-zenhei; "
            "fi; fc-cache -f",
            timeout=240,
        )


def _restore_profile(desktop: _Desktop, profile_archive: str | Path | None) -> bool:
    if profile_archive is None:
        return False
    archive_path = Path(profile_archive).resolve()
    if not archive_path.is_file():
        return False

    desktop.files.write(REMOTE_PROFILE_ARCHIVE, decrypt_archive(archive_path))
    _checked_command(
        desktop,
        f"mkdir -p {REMOTE_PROFILE_DIR} && "
        f"tar -xzf {REMOTE_PROFILE_ARCHIVE} -C {REMOTE_PROFILE_DIR}",
        timeout=90,
    )
    return True


def _export_profile(desktop: _Desktop, profile_archive: str | Path) -> Path:
    _checked_command(
        desktop,
        f"tar --exclude='Singleton*' --exclude='*/Singleton*' "
        f"-czf {REMOTE_PROFILE_ARCHIVE} -C {REMOTE_PROFILE_DIR} .",
        timeout=120,
    )
    archive = desktop.files.read(REMOTE_PROFILE_ARCHIVE, format="bytes")
    if isinstance(archive, str):
        archive = archive.encode("utf-8")
    return encrypt_archive(archive, profile_archive)


def _remote_collector_command(
    *,
    operation: str,
    query: str | None,
    url: str | None,
    expected_spec: str | None,
) -> tuple[str, str, str]:
    if operation == "search":
        if not query:
            raise ValueError("query is required for search")
        module = "parity.collectors.jd_search"
        result_name = "search-result.json"
        screenshot_name = "search-page.png"
        arguments = ["--query", query]
    elif operation == "product":
        if not url:
            raise ValueError("url is required for product")
        module = "parity.collectors.jd_product"
        result_name = "result.json"
        screenshot_name = "page.png"
        arguments = ["--url", url]
        if expected_spec:
            arguments.extend(["--expected-spec", expected_spec])
    else:
        raise ValueError(f"unsupported operation: {operation}")

    arguments.extend(
        [
            "--sandbox-worker",
            "--headed",
            "--profile-dir",
            REMOTE_PROFILE_DIR,
            "--evidence-dir",
            REMOTE_OUTPUT_DIR,
            "--browser-executable",
            "/usr/bin/google-chrome",
        ]
    )
    command = " ".join(
        [
            "PYTHONPATH=" + shlex.quote(REMOTE_APP_DIR),
            "DISPLAY=:0",
            "python3",
            "-m",
            module,
            *(shlex.quote(value) for value in arguments),
        ]
    )
    return command, result_name, screenshot_name


def run_e2b_collector(
    *,
    operation: str,
    query: str | None = None,
    url: str | None = None,
    expected_spec: str | None = None,
    evidence_dir: str | Path = "artifacts/price-crawler/e2b-latest",
    template: str = DEFAULT_TEMPLATE,
    timeout_seconds: int = 300,
    profile_archive: str | Path | None = None,
    desktop_factory: Callable[..., _Desktop] | None = None,
) -> E2BRunEnvelope:
    """Run exactly one headed browser collector in an E2B Desktop sandbox."""

    _require_api_key()
    if not (60 <= timeout_seconds <= 900):
        raise ValueError("timeout_seconds must be between 60 and 900")

    factory = desktop_factory or _default_desktop_factory
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = monotonic()
    desktop: _Desktop | None = None
    sandbox_id = ""
    destroyed = False
    result: dict[str, Any] | None = None
    output_path = Path(evidence_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    local_result_path = output_path / "result.json"
    local_screenshot_path: Path | None = None
    profile_restored = False
    profile_saved = False

    try:
        desktop = factory(
            template=template,
            resolution=(1024, 768),
            timeout=timeout_seconds,
            secure=True,
            allow_internet_access=True,
            metadata={"service": "parity-browser-sandbox", "operation": operation},
        )
        sandbox_id = desktop.sandbox_id
        _upload_worker(desktop)
        _install_remote_runtime(desktop)
        profile_restored = _restore_profile(desktop, profile_archive)
        command, result_name, screenshot_name = _remote_collector_command(
            operation=operation,
            query=query,
            url=url,
            expected_spec=expected_spec,
        )
        # Collectors use exit code 2 for explicit stop states such as
        # rate_limited or login_required, but still write valid evidence.
        _checked_command(
            desktop,
            command,
            timeout=timeout_seconds,
            allowed_exit_codes=frozenset({0, 2}),
        )

        raw_result = desktop.files.read(f"{REMOTE_OUTPUT_DIR}/{result_name}")
        if isinstance(raw_result, bytes):
            raw_result = raw_result.decode("utf-8")
        result = json.loads(raw_result)
        local_result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        try:
            screenshot = desktop.files.read(
                f"{REMOTE_OUTPUT_DIR}/{screenshot_name}", format="bytes"
            )
            if isinstance(screenshot, str):
                screenshot = screenshot.encode("utf-8")
            local_screenshot_path = output_path / screenshot_name
            local_screenshot_path.write_bytes(screenshot)
        except Exception:
            local_screenshot_path = None

        if profile_archive is not None and result.get("status") in {"succeeded", "partial"}:
            _export_profile(desktop, profile_archive)
            profile_saved = True
    finally:
        if desktop is not None:
            try:
                desktop.kill()
                destroyed = True
            except Exception:
                destroyed = False

    if result is None:
        raise RuntimeError("E2B collector finished without a result")

    envelope = E2BRunEnvelope(
        runtime="e2b_desktop",
        template=template,
        sandbox_id=sandbox_id,
        sandbox_destroyed=destroyed,
        started_at=started_at,
        elapsed_seconds=round(monotonic() - started, 3),
        result=result,
        local_result_path=str(local_result_path),
        local_screenshot_path=str(local_screenshot_path) if local_screenshot_path else None,
        profile_restored=profile_restored,
        profile_saved=profile_saved,
        profile_archive_path=(
            str(Path(profile_archive).resolve()) if profile_archive is not None else None
        ),
    )
    (output_path / "e2b-run.json").write_text(
        json.dumps(envelope.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return envelope


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a JD collector in E2B Desktop")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--evidence-dir", default="artifacts/price-crawler/e2b-latest")
    parser.add_argument("--profile-archive", default=str(DEFAULT_PROFILE_ARCHIVE))
    subparsers = parser.add_subparsers(dest="operation", required=True)

    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)

    product = subparsers.add_parser("product")
    product.add_argument("--url", required=True)
    product.add_argument("--expected-spec")
    return parser


def _main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    try:
        envelope = run_e2b_collector(
            operation=args.operation,
            query=getattr(args, "query", None),
            url=getattr(args, "url", None),
            expected_spec=getattr(args, "expected_spec", None),
            evidence_dir=args.evidence_dir,
            template=args.template,
            timeout_seconds=args.timeout_seconds,
            profile_archive=args.profile_archive,
        )
    except E2BConfigurationError as exc:
        print(json.dumps({"status": "configuration_error", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(envelope.to_dict(), ensure_ascii=False, indent=2))
    return 0 if envelope.result.get("status") in {"succeeded", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(_main())
