from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from parity.e2b_runner import (
    E2BConfigurationError,
    _remote_collector_command,
    run_e2b_collector,
)


class FakeCommandResult:
    exit_code = 0
    stderr = ""
    stdout = ""


class FakeCommands:
    def __init__(self):
        self.calls: list[str] = []

    def run(self, command: str, **_kwargs):
        self.calls.append(command)
        return FakeCommandResult()


class FakeFiles:
    def __init__(self):
        self.writes: dict[str, str | bytes] = {}

    def write(self, path: str, data: str | bytes, **_kwargs):
        self.writes[path] = data

    def read(self, path: str, **_kwargs):
        if path.endswith("search-result.json"):
            return json.dumps(
                {
                    "status": "succeeded",
                    "reason": "remote browser returned candidates",
                    "candidates": [{"sku": "123", "title": "demo"}],
                }
            )
        if path.endswith(".png"):
            return b"PNG"
        raise FileNotFoundError(path)


class FakeDesktop:
    sandbox_id = "e2b-sandbox-1"

    def __init__(self):
        self.commands = FakeCommands()
        self.files = FakeFiles()
        self.killed = False

    def kill(self):
        self.killed = True


class ExpectedStop(Exception):
    exit_code = 2


class ExpectedStopCommands(FakeCommands):
    def run(self, command: str, **_kwargs):
        self.calls.append(command)
        if "--sandbox-worker" in command:
            raise ExpectedStop("collector stopped after writing evidence")
        return FakeCommandResult()


def test_remote_command_runs_headed_chrome_inside_worker():
    command, result_name, screenshot_name = _remote_collector_command(
        operation="search",
        query="SK-II 神仙水 230ml",
        url=None,
        expected_spec=None,
    )

    assert "parity.collectors.jd_search" in command
    assert "--sandbox-worker" in command
    assert "--headed" in command
    assert "/usr/bin/google-chrome" in command
    assert result_name == "search-result.json"
    assert screenshot_name == "search-page.png"


def test_e2b_runner_uploads_worker_downloads_result_and_kills_vm():
    desktop = FakeDesktop()
    create_kwargs: dict[str, object] = {}

    def factory(**kwargs):
        create_kwargs.update(kwargs)
        return desktop

    with TemporaryDirectory() as temp_dir, patch.dict(
        os.environ, {"E2B_API_KEY": "test-only"}, clear=False
    ):
        envelope = run_e2b_collector(
            operation="search",
            query="SK-II 神仙水 230ml",
            evidence_dir=temp_dir,
            desktop_factory=factory,
        )

        assert envelope.runtime == "e2b_desktop"
        assert envelope.result["status"] == "succeeded"
        assert envelope.sandbox_destroyed is True
        assert Path(envelope.local_result_path).is_file()
        assert Path(envelope.local_screenshot_path or "").read_bytes() == b"PNG"

    assert create_kwargs["template"] == "desktop"
    assert create_kwargs["resolution"] == (1024, 768)
    assert create_kwargs["secure"] is True
    assert desktop.killed is True
    assert any(path.endswith("collectors/jd_search.py") for path in desktop.files.writes)
    assert any("pip install" in command for command in desktop.commands.calls)
    assert any("--sandbox-worker" in command for command in desktop.commands.calls)


def test_e2b_runner_downloads_evidence_after_expected_exit_code_two():
    desktop = FakeDesktop()
    desktop.commands = ExpectedStopCommands()

    with TemporaryDirectory() as temp_dir, patch.dict(
        os.environ, {"E2B_API_KEY": "test-only"}, clear=False
    ):
        envelope = run_e2b_collector(
            operation="search",
            query="demo",
            evidence_dir=temp_dir,
            desktop_factory=lambda **_kwargs: desktop,
        )

    assert envelope.result["status"] == "succeeded"
    assert envelope.sandbox_destroyed is True


def test_e2b_runner_rejects_missing_key_before_creating_vm():
    with patch.dict(os.environ, {}, clear=True), patch(
        "parity.e2b_runner._load_local_env", return_value=None
    ):
        with pytest.raises(E2BConfigurationError, match="E2B_API_KEY is missing"):
            run_e2b_collector(
                operation="search",
                query="demo",
                desktop_factory=lambda **_kwargs: pytest.fail("must not create sandbox"),
            )


def test_direct_collector_cli_requires_explicit_local_dev():
    from parity.collectors import jd_search

    with patch.object(sys, "argv", ["jd_search", "--query", "demo"]):
        assert __import__("asyncio").run(jd_search._main()) == 2
