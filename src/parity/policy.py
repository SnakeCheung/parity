"""Fail-closed configuration for the standalone browser sandbox service.

The policy is intentionally provider-neutral. An E2B/BYOC/Firecracker adapter
must translate it to the provider's runtime controls and refuse to launch when
one of the required controls cannot be enforced.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


class PolicyViolation(ValueError):
    """Raised when a sandbox policy weakens a required isolation boundary."""


@dataclass(frozen=True)
class IdentityPolicy:
    tenant_boundary: str
    profile_scope: str
    one_writer_per_profile: bool


@dataclass(frozen=True)
class RuntimePolicy:
    provider: str
    runtime_class: str
    template_id: str
    rootfs_read_only: bool
    allow_host_mounts: bool
    vcpu: int
    memory_mb: int
    disk_mb: int
    task_timeout_seconds: int


@dataclass(frozen=True)
class ControlPlanePolicy:
    authenticated: bool
    encrypted_transport: bool
    public_envd: bool
    public_debug_ports: bool


@dataclass(frozen=True)
class EgressPolicy:
    mode: str
    allowed_domains: tuple[str, ...]
    block_private_networks: bool
    block_cloud_metadata: bool
    stable_identity_per_profile: bool
    rotate_on_rate_limit: bool


@dataclass(frozen=True)
class LiveViewPolicy:
    enabled_by_default: bool
    requires_one_time_token: bool
    audit_access: bool
    max_seconds: int
    viewport_width: int
    viewport_height: int


@dataclass(frozen=True)
class PersistencePolicy:
    mode: str
    encrypted_at_rest: bool
    profile_scope: str
    allow_authenticated_template_snapshots: bool
    close_browser_before_export: bool
    max_versions: int
    checkpoint_ttl_hours: int


@dataclass(frozen=True)
class SandboxPolicy:
    identity: IdentityPolicy
    runtime: RuntimePolicy
    control_plane: ControlPlanePolicy
    egress: EgressPolicy
    live_view: LiveViewPolicy
    persistence: PersistencePolicy

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SandboxPolicy":
        try:
            runtime_raw = dict(raw["runtime"])
            runtime_raw["runtime_class"] = runtime_raw.pop("class")
            egress_raw = dict(raw["egress"])
            egress_raw["allowed_domains"] = tuple(egress_raw["allowed_domains"])
            policy = cls(
                identity=IdentityPolicy(**raw["identity"]),
                runtime=RuntimePolicy(**runtime_raw),
                control_plane=ControlPlanePolicy(**raw["control_plane"]),
                egress=EgressPolicy(**egress_raw),
                live_view=LiveViewPolicy(**raw["live_view"]),
                persistence=PersistencePolicy(**raw["persistence"]),
            )
        except (KeyError, TypeError) as exc:
            raise PolicyViolation(f"invalid or missing sandbox policy field: {exc}") from exc
        policy.validate()
        return policy

    def validate(self) -> None:
        failures: list[str] = []

        if self.identity.tenant_boundary != "channel_account":
            failures.append("identity.tenant_boundary must be 'channel_account'")
        if self.identity.profile_scope != "one_site_one_login":
            failures.append("identity.profile_scope must be 'one_site_one_login'")
        if not self.identity.one_writer_per_profile:
            failures.append("identity.one_writer_per_profile must be true")

        if self.runtime.runtime_class != "microvm":
            failures.append("runtime.class must be 'microvm'")
        if self.runtime.provider not in {"e2b_managed", "e2b_byoc", "firecracker_self_hosted"}:
            failures.append("runtime.provider is not an approved microVM provider")
        if not self.runtime.template_id.strip():
            failures.append("runtime.template_id must name an immutable clean template")
        if not self.runtime.rootfs_read_only:
            failures.append("runtime.rootfs_read_only must be true")
        if self.runtime.allow_host_mounts:
            failures.append("runtime.allow_host_mounts must be false")
        if not (1 <= self.runtime.vcpu <= 8):
            failures.append("runtime.vcpu must be between 1 and 8")
        if not (512 <= self.runtime.memory_mb <= 16384):
            failures.append("runtime.memory_mb must be between 512 and 16384")
        if not (1024 <= self.runtime.disk_mb <= 65536):
            failures.append("runtime.disk_mb must be between 1024 and 65536")
        if not (30 <= self.runtime.task_timeout_seconds <= 3600):
            failures.append("runtime.task_timeout_seconds must be between 30 and 3600")

        if not self.control_plane.authenticated:
            failures.append("control_plane.authenticated must be true")
        if not self.control_plane.encrypted_transport:
            failures.append("control_plane.encrypted_transport must be true")
        if self.control_plane.public_envd:
            failures.append("control_plane.public_envd must be false")
        if self.control_plane.public_debug_ports:
            failures.append("control_plane.public_debug_ports must be false")

        broad_domains = {"*", "*.*", "0.0.0.0/0"}
        if self.egress.mode != "allowlist":
            failures.append("egress.mode must be 'allowlist'")
        if not self.egress.allowed_domains or broad_domains.intersection(self.egress.allowed_domains):
            failures.append("egress.allowed_domains must be a non-empty, bounded allowlist")
        if not self.egress.block_private_networks:
            failures.append("egress.block_private_networks must be true")
        if not self.egress.block_cloud_metadata:
            failures.append("egress.block_cloud_metadata must be true")
        if not self.egress.stable_identity_per_profile:
            failures.append("egress.stable_identity_per_profile must be true")
        if self.egress.rotate_on_rate_limit:
            failures.append("egress.rotate_on_rate_limit must be false")

        if self.live_view.enabled_by_default:
            failures.append("live_view.enabled_by_default must be false")
        if not self.live_view.requires_one_time_token:
            failures.append("live_view.requires_one_time_token must be true")
        if not self.live_view.audit_access:
            failures.append("live_view.audit_access must be true")
        if not (60 <= self.live_view.max_seconds <= 900):
            failures.append("live_view.max_seconds must be between 60 and 900")
        if self.live_view.viewport_width > 1280 or self.live_view.viewport_height > 900:
            failures.append("live_view viewport must not exceed 1280x900")

        if self.persistence.mode != "encrypted_browser_context":
            failures.append("persistence.mode must be 'encrypted_browser_context'")
        if not self.persistence.encrypted_at_rest:
            failures.append("persistence.encrypted_at_rest must be true")
        if self.persistence.profile_scope != "channel_account":
            failures.append("persistence.profile_scope must be 'channel_account'")
        if self.persistence.allow_authenticated_template_snapshots:
            failures.append("authenticated template snapshots are forbidden")
        if not self.persistence.close_browser_before_export:
            failures.append("persistence.close_browser_before_export must be true")
        if not (1 <= self.persistence.max_versions <= 10):
            failures.append("persistence.max_versions must be between 1 and 10")
        if not (1 <= self.persistence.checkpoint_ttl_hours <= 720):
            failures.append("persistence.checkpoint_ttl_hours must be between 1 and 720")

        if failures:
            raise PolicyViolation("; ".join(failures))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_policy(path: str | Path) -> SandboxPolicy:
    with Path(path).open("rb") as stream:
        return SandboxPolicy.from_mapping(tomllib.load(stream))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate a browser sandbox isolation policy")
    parser.add_argument("policy", type=Path)
    args = parser.parse_args()
    try:
        policy = load_policy(args.policy)
    except (OSError, tomllib.TOMLDecodeError, PolicyViolation) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"valid": True, "policy": policy.to_dict()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
