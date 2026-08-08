"""Provider-neutral lifecycle that prevents profile sharing and VM residue.

This module deliberately imports nothing from Mojing or the Agent runtime.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, TypeVar

from .policy import SandboxPolicy


class ProfileBusyError(RuntimeError):
    """Raised when another task already owns the same browser profile."""


@dataclass(frozen=True)
class ProfileRef:
    site: str
    account_id: str

    @property
    def lock_key(self) -> str:
        return f"browser-profile:{self.site}:{self.account_id}"


@dataclass(frozen=True)
class ProfileCheckpoint:
    object_ref: str
    version: int
    sha256: str


@dataclass(frozen=True)
class SandboxHandle:
    sandbox_id: str


@dataclass(frozen=True)
class TaskOutcome:
    status: str
    # Fail closed: callers must opt in after an authenticated, healthy task.
    # Rate-limit, CAPTCHA, login-loss and parser failures must not overwrite the
    # last known-good browser state.
    persist_profile: bool = False


class ProfileRepository(Protocol):
    async def acquire(self, lock_key: str, ttl_seconds: int) -> bool: ...

    async def release(self, lock_key: str) -> None: ...

    async def current(self, profile: ProfileRef) -> ProfileCheckpoint | None: ...

    async def commit(
        self,
        profile: ProfileRef,
        checkpoint: ProfileCheckpoint,
        *,
        expected_version: int | None,
    ) -> None: ...


class SandboxProvider(Protocol):
    async def create_clean(self, policy: SandboxPolicy, profile: ProfileRef) -> SandboxHandle: ...

    async def restore_profile(
        self,
        sandbox: SandboxHandle,
        checkpoint: ProfileCheckpoint,
    ) -> None: ...

    async def stop_browser(self, sandbox: SandboxHandle) -> None: ...

    async def export_profile(
        self,
        sandbox: SandboxHandle,
        profile: ProfileRef,
        *,
        next_version: int,
    ) -> ProfileCheckpoint: ...

    async def kill(self, sandbox: SandboxHandle) -> None: ...


T = TypeVar("T", bound=TaskOutcome)
SandboxTask = Callable[[SandboxHandle], T | Awaitable[T]]


class BrowserSandboxCoordinator:
    """Run one task in a clean microVM and always destroy the runtime.

    ProfileRepository is expected to be backed by a distributed lock and an
    encrypted object store in production. SandboxProvider must never implement
    create_clean by cloning a snapshot that already contains login data.
    """

    def __init__(
        self,
        *,
        policy: SandboxPolicy,
        profiles: ProfileRepository,
        provider: SandboxProvider,
    ) -> None:
        self._policy = policy
        self._profiles = profiles
        self._provider = provider

    async def execute(self, profile: ProfileRef, task: SandboxTask[T]) -> T:
        lock_key = profile.lock_key
        acquired = await self._profiles.acquire(
            lock_key,
            ttl_seconds=self._policy.runtime.task_timeout_seconds + 60,
        )
        if not acquired:
            raise ProfileBusyError(f"profile already has an active writer: {lock_key}")

        sandbox: SandboxHandle | None = None
        try:
            current = await self._profiles.current(profile)
            sandbox = await self._provider.create_clean(self._policy, profile)
            if current is not None:
                await self._provider.restore_profile(sandbox, current)

            outcome = task(sandbox)
            if inspect.isawaitable(outcome):
                outcome = await outcome

            if outcome.persist_profile:
                # Chromium must be closed before copying its user-data directory;
                # otherwise SQLite-backed cookie/storage files can be inconsistent.
                await self._provider.stop_browser(sandbox)
                next_version = 1 if current is None else current.version + 1
                checkpoint = await self._provider.export_profile(
                    sandbox,
                    profile,
                    next_version=next_version,
                )
                await self._profiles.commit(
                    profile,
                    checkpoint,
                    expected_version=None if current is None else current.version,
                )
            return outcome
        finally:
            try:
                if sandbox is not None:
                    await self._provider.kill(sandbox)
            finally:
                await self._profiles.release(lock_key)
