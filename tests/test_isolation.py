from __future__ import annotations

import asyncio
import ast
from dataclasses import replace
from pathlib import Path

import pytest

from parity.lifecycle import (
    BrowserSandboxCoordinator,
    ProfileBusyError,
    ProfileCheckpoint,
    ProfileRef,
    SandboxHandle,
    TaskOutcome,
)
from parity.policy import PolicyViolation, load_policy


POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "sandbox-policy.example.toml"
PACKAGE_PATH = Path(__file__).resolve().parents[1] / "src" / "parity"


def test_package_does_not_import_agent_runtime():
    forbidden_roots = {"Mojing", "simpleclaw", "admin", "confluent_kafka"}
    imported_roots: set[str] = set()

    for source_path in PACKAGE_PATH.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint(forbidden_roots)


class FakeProfiles:
    def __init__(self, *, acquire: bool = True, current: ProfileCheckpoint | None = None):
        self.can_acquire = acquire
        self.checkpoint = current
        self.calls: list[str] = []

    async def acquire(self, lock_key: str, ttl_seconds: int) -> bool:
        self.calls.append(f"acquire:{lock_key}:{ttl_seconds}")
        return self.can_acquire

    async def release(self, lock_key: str) -> None:
        self.calls.append(f"release:{lock_key}")

    async def current(self, profile: ProfileRef) -> ProfileCheckpoint | None:
        self.calls.append("current")
        return self.checkpoint

    async def commit(
        self,
        profile: ProfileRef,
        checkpoint: ProfileCheckpoint,
        *,
        expected_version: int | None,
    ) -> None:
        self.calls.append(f"commit:{checkpoint.version}:{expected_version}")
        self.checkpoint = checkpoint


class FakeProvider:
    def __init__(self):
        self.calls: list[str] = []

    async def create_clean(self, policy, profile: ProfileRef) -> SandboxHandle:
        self.calls.append(f"create_clean:{profile.site}:{profile.account_id}")
        return SandboxHandle("sandbox-1")

    async def restore_profile(self, sandbox, checkpoint: ProfileCheckpoint) -> None:
        self.calls.append(f"restore:{checkpoint.version}")

    async def stop_browser(self, sandbox) -> None:
        self.calls.append("stop_browser")

    async def export_profile(self, sandbox, profile, *, next_version: int) -> ProfileCheckpoint:
        self.calls.append(f"export:{next_version}")
        return ProfileCheckpoint(f"profiles/{profile.site}/{profile.account_id}", next_version, "abc")

    async def kill(self, sandbox) -> None:
        self.calls.append("kill")


def test_example_policy_is_strict_and_valid():
    policy = load_policy(POLICY_PATH)

    assert policy.runtime.runtime_class == "microvm"
    assert policy.egress.mode == "allowlist"
    assert policy.persistence.allow_authenticated_template_snapshots is False


def test_policy_rejects_authenticated_template_snapshots():
    policy = load_policy(POLICY_PATH)
    unsafe = replace(
        policy,
        persistence=replace(policy.persistence, allow_authenticated_template_snapshots=True),
    )

    with pytest.raises(PolicyViolation, match="authenticated template snapshots are forbidden"):
        unsafe.validate()


def test_coordinator_serializes_profile_and_destroys_runtime():
    profiles = FakeProfiles(
        current=ProfileCheckpoint("profiles/jd/account-1", version=4, sha256="old")
    )
    provider = FakeProvider()
    coordinator = BrowserSandboxCoordinator(
        policy=load_policy(POLICY_PATH),
        profiles=profiles,
        provider=provider,
    )

    async def task(sandbox: SandboxHandle) -> TaskOutcome:
        assert sandbox.sandbox_id == "sandbox-1"
        return TaskOutcome(status="succeeded", persist_profile=True)

    result = asyncio.run(coordinator.execute(ProfileRef("jd", "account-1"), task))

    assert result.status == "succeeded"
    assert provider.calls == [
        "create_clean:jd:account-1",
        "restore:4",
        "stop_browser",
        "export:5",
        "kill",
    ]
    assert "commit:5:4" in profiles.calls
    assert profiles.calls[-1] == "release:browser-profile:jd:account-1"


def test_coordinator_kills_runtime_when_task_fails():
    profiles = FakeProfiles()
    provider = FakeProvider()
    coordinator = BrowserSandboxCoordinator(
        policy=load_policy(POLICY_PATH),
        profiles=profiles,
        provider=provider,
    )

    async def task(_sandbox: SandboxHandle) -> TaskOutcome:
        raise RuntimeError("collector failed")

    with pytest.raises(RuntimeError, match="collector failed"):
        asyncio.run(coordinator.execute(ProfileRef("jd", "account-1"), task))

    assert provider.calls[-1] == "kill"
    assert not any(call.startswith("export") for call in provider.calls)
    assert profiles.calls[-1] == "release:browser-profile:jd:account-1"


def test_coordinator_does_not_persist_rate_limited_state_by_default():
    profiles = FakeProfiles(
        current=ProfileCheckpoint("profiles/jd/account-1", version=4, sha256="old")
    )
    provider = FakeProvider()
    coordinator = BrowserSandboxCoordinator(
        policy=load_policy(POLICY_PATH),
        profiles=profiles,
        provider=provider,
    )

    result = asyncio.run(
        coordinator.execute(
            ProfileRef("jd", "account-1"),
            lambda _sandbox: TaskOutcome(status="rate_limited"),
        )
    )

    assert result.status == "rate_limited"
    assert provider.calls == ["create_clean:jd:account-1", "restore:4", "kill"]
    assert not any(call.startswith("commit") for call in profiles.calls)


def test_coordinator_rejects_concurrent_profile_writer():
    profiles = FakeProfiles(acquire=False)
    provider = FakeProvider()
    coordinator = BrowserSandboxCoordinator(
        policy=load_policy(POLICY_PATH),
        profiles=profiles,
        provider=provider,
    )

    with pytest.raises(ProfileBusyError):
        asyncio.run(
            coordinator.execute(
                ProfileRef("jd", "account-1"),
                lambda _sandbox: TaskOutcome(status="succeeded"),
            )
        )

    assert provider.calls == []
