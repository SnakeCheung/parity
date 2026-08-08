"""Interactive JD login inside E2B Desktop with encrypted Profile export."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .e2b_runner import (
    DEFAULT_TEMPLATE,
    REMOTE_APP_DIR,
    REMOTE_PROFILE_DIR,
    E2BConfigurationError,
    _Desktop,
    _default_desktop_factory,
    _export_profile,
    _install_remote_runtime,
    _require_api_key,
    _restore_profile,
    _upload_worker,
)
from .profile_store import DEFAULT_PROFILE_ARCHIVE, ProfileKeyError


REMOTE_LOGIN_EXIT = "/home/user/output/login.exit"
REMOTE_LOGIN_LOG = "/home/user/output/login.log"


def _write_status_file(status_file: str | Path, payload: dict[str, Any]) -> None:
    path = Path(status_file).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class E2BLoginEnvelope:
    runtime: str
    status: str
    reason: str
    template: str
    sandbox_id: str
    sandbox_destroyed: bool
    started_at: str
    elapsed_seconds: float
    profile_restored: bool
    profile_saved: bool
    profile_archive_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _login_worker_command(timeout_seconds: int) -> str:
    worker = " ".join(
        [
            "PYTHONPATH=" + shlex.quote(REMOTE_APP_DIR),
            "DISPLAY=:0",
            "python3",
            "-m",
            "parity.collectors.jd_product",
            "--sandbox-worker",
            "--headed",
            "--login-only",
            "--profile-dir",
            shlex.quote(REMOTE_PROFILE_DIR),
            "--browser-executable",
            "/usr/bin/google-chrome",
            "--login-timeout-seconds",
            str(timeout_seconds),
        ]
    )
    script = (
        f"{worker} > {REMOTE_LOGIN_LOG} 2>&1; "
        f"code=$?; printf '%s' \"$code\" > {REMOTE_LOGIN_EXIT}"
    )
    return "bash -lc " + shlex.quote(script)


def _wait_for_login(desktop: _Desktop, timeout_seconds: int) -> tuple[bool, str]:
    deadline = monotonic() + timeout_seconds + 15
    consecutive_transport_errors = 0
    while monotonic() < deadline:
        try:
            raw_exit = desktop.files.read(REMOTE_LOGIN_EXIT)
            if isinstance(raw_exit, bytes):
                raw_exit = raw_exit.decode("utf-8")
            exit_code = int(raw_exit.strip())
            try:
                worker_log = desktop.files.read(REMOTE_LOGIN_LOG)
                if isinstance(worker_log, bytes):
                    worker_log = worker_log.decode("utf-8", errors="replace")
            except Exception:
                worker_log = ""
            return exit_code == 0, worker_log[-1000:]
        except (FileNotFoundError, ValueError):
            consecutive_transport_errors = 0
            time.sleep(2)
        except Exception as exc:
            if type(exc).__name__ in {
                "FileNotFoundException",
                "NotFoundException",
                "NotFoundError",
            }:
                consecutive_transport_errors = 0
                time.sleep(2)
                continue
            if type(exc).__name__ in {
                "ConnectError",
                "ConnectTimeout",
                "ReadError",
                "ReadTimeout",
                "RemoteProtocolError",
                "WriteError",
            }:
                consecutive_transport_errors += 1
                if consecutive_transport_errors <= 20:
                    time.sleep(2)
                    continue
            raise
    return False, "LOGIN_TIMEOUT_PROFILE_CLOSED"


def run_e2b_login(
    *,
    profile_archive: str | Path = DEFAULT_PROFILE_ARCHIVE,
    template: str = DEFAULT_TEMPLATE,
    login_timeout_seconds: int = 600,
    status_file: str | Path | None = None,
    desktop_factory: Callable[..., _Desktop] | None = None,
) -> E2BLoginEnvelope:
    """Open an authenticated live desktop and save the resulting Profile encrypted."""

    _require_api_key()
    if not (60 <= login_timeout_seconds <= 780):
        raise ValueError("login_timeout_seconds must be between 60 and 780")

    factory = desktop_factory or _default_desktop_factory
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = monotonic()
    desktop: _Desktop | None = None
    sandbox_id = ""
    destroyed = False
    stream_started = False
    profile_restored = False
    profile_saved = False
    status = "failed"
    reason = "Login did not complete"
    archive_path = Path(profile_archive).resolve()

    try:
        desktop = factory(
            template=template,
            resolution=(1280, 900),
            timeout=min(900, login_timeout_seconds + 120),
            secure=True,
            allow_internet_access=True,
            metadata={"service": "parity-browser-sandbox", "operation": "login"},
        )
        sandbox_id = desktop.sandbox_id
        _upload_worker(desktop)
        _install_remote_runtime(desktop, install_cjk_fonts=True)
        profile_restored = _restore_profile(desktop, archive_path)

        desktop.commands.run(_login_worker_command(login_timeout_seconds), background=True, timeout=0)
        time.sleep(2)
        desktop.stream.start(require_auth=True)
        stream_started = True
        auth_key = desktop.stream.get_auth_key()
        live_view_url = desktop.stream.get_url(auth_key=auth_key)
        ready_event = {
            "event": "login_ready",
            "sandbox_id": sandbox_id,
            "live_view_url": live_view_url,
            "expires_in_seconds": login_timeout_seconds,
        }
        if status_file is not None:
            _write_status_file(status_file, ready_event)
        else:
            print(json.dumps(ready_event, ensure_ascii=False), flush=True)

        logged_in, worker_log = _wait_for_login(desktop, login_timeout_seconds)
        if logged_in:
            _export_profile(desktop, archive_path)
            profile_saved = True
            status = "succeeded"
            reason = "JD login confirmed and encrypted Profile saved"
        else:
            status = "login_timeout"
            reason = worker_log.strip() or "JD login was not confirmed"
    finally:
        if desktop is not None:
            if stream_started:
                try:
                    desktop.stream.stop()
                except Exception:
                    pass
            try:
                desktop.kill()
                destroyed = True
            except Exception:
                destroyed = False

    return E2BLoginEnvelope(
        runtime="e2b_desktop",
        status=status,
        reason=reason,
        template=template,
        sandbox_id=sandbox_id,
        sandbox_destroyed=destroyed,
        started_at=started_at,
        elapsed_seconds=round(monotonic() - started, 3),
        profile_restored=profile_restored,
        profile_saved=profile_saved,
        profile_archive_path=str(archive_path),
    )


def _main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Log in to JD inside E2B Desktop")
    parser.add_argument("--profile-archive", default=str(DEFAULT_PROFILE_ARCHIVE))
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--login-timeout-seconds", type=int, default=600)
    parser.add_argument("--status-file")
    args = parser.parse_args()

    try:
        envelope = run_e2b_login(
            profile_archive=args.profile_archive,
            template=args.template,
            login_timeout_seconds=args.login_timeout_seconds,
            status_file=args.status_file,
        )
    except (E2BConfigurationError, ProfileKeyError) as exc:
        failure = {"status": "configuration_error", "reason": str(exc)}
        if args.status_file:
            _write_status_file(args.status_file, failure)
        print(json.dumps(failure, ensure_ascii=False))
        return 2
    final = envelope.to_dict()
    if args.status_file:
        _write_status_file(args.status_file, final)
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if envelope.status == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(_main())
