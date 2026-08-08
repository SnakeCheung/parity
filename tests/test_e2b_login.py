from __future__ import annotations

import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parity.e2b_login import _login_worker_command, _wait_for_login, run_e2b_login


class FakeCommands:
    def __init__(self):
        self.calls: list[str] = []

    def run(self, command: str, **_kwargs):
        self.calls.append(command)
        return object()


class FakeFiles:
    pass


class RemoteProtocolError(Exception):
    pass


class RecoveringFiles:
    def __init__(self):
        self.exit_reads = 0

    def read(self, path: str):
        if path.endswith("login.exit"):
            self.exit_reads += 1
            if self.exit_reads == 1:
                raise RemoteProtocolError("temporary disconnect")
            return "0"
        return "LOGIN_CONFIRMED_PROFILE_SAVED"


class FakeStream:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self, **_kwargs):
        self.started = True

    def get_auth_key(self):
        return "one-time-test-key"

    def get_url(self, **_kwargs):
        return "https://example.test/vnc.html?password=redacted"

    def stop(self):
        self.stopped = True


class FakeDesktop:
    sandbox_id = "login-sandbox"

    def __init__(self):
        self.commands = FakeCommands()
        self.files = FakeFiles()
        self.stream = FakeStream()
        self.killed = False

    def kill(self):
        self.killed = True


def test_login_worker_runs_inside_sandbox_with_persistent_profile():
    command = _login_worker_command(600)

    assert "parity.collectors.jd_product" in command
    assert "--sandbox-worker" in command
    assert "--login-only" in command
    assert "/home/user/state/profile-jd" in command


def test_login_success_exports_profile_and_destroys_sandbox():
    desktop = FakeDesktop()

    with TemporaryDirectory() as temp_dir, patch.dict(
        os.environ,
        {"E2B_API_KEY": "test-only", "PARITY_PROFILE_KEY": "test-only"},
        clear=False,
    ), patch("parity.e2b_login._upload_worker"), patch(
        "parity.e2b_login._install_remote_runtime"
    ), patch("parity.e2b_login._restore_profile", return_value=False), patch(
        "parity.e2b_login._wait_for_login", return_value=(True, "LOGIN_CONFIRMED_PROFILE_SAVED")
    ), patch("parity.e2b_login._export_profile") as export_profile, patch(
        "parity.e2b_login.time.sleep", return_value=None
    ):
        envelope = run_e2b_login(
            profile_archive=f"{temp_dir}/profile.fernet",
            login_timeout_seconds=60,
            desktop_factory=lambda **_kwargs: desktop,
        )

    assert envelope.status == "succeeded"
    assert envelope.profile_saved is True
    assert envelope.sandbox_destroyed is True
    assert desktop.stream.started is True
    assert desktop.stream.stopped is True
    assert desktop.killed is True
    export_profile.assert_called_once()


def test_login_poll_recovers_from_transient_control_plane_disconnect():
    desktop = FakeDesktop()
    desktop.files = RecoveringFiles()

    with patch("parity.e2b_login.time.sleep", return_value=None):
        logged_in, worker_log = _wait_for_login(desktop, 60)

    assert logged_in is True
    assert "LOGIN_CONFIRMED" in worker_log
