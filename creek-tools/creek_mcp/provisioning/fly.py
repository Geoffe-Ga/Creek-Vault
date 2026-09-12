"""Reconcile-first Fly Machines provider driver for issue #1770.

The adapter deliberately speaks the documented HTTP API instead of shelling
out to ``flyctl``.  Provider and runtime credentials are injected through
repr-safe values, while durable identity comes only from activation metadata.
That separation lets a retry recover resources created before the control
plane managed to persist the provider result.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Any, Final, Never, Protocol, cast

import httpx

from creek_mcp.provisioning.driver import ProviderAllocation, ProviderError
from creek_mcp.provisioning.inventory import ProviderResource
from creek_mcp.provisioning.models import (
    DeletionOutcome,
    FailureReason,
    ResourceClass,
    ResourceState,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from creek_mcp.provisioning.models import ProvisioningJob

_ALLOCATION_DIGEST_LENGTH: Final[int] = 24
_MAX_TOKEN_FILE_BYTES: Final[int] = 16 * 1024
_DEFAULT_API_BASE_URL: Final[str] = "https://api.machines.dev"
_DEFAULT_REGION: Final[str] = "iad"
_DEFAULT_CPU_KIND: Final[str] = "shared"
_DEFAULT_CPUS: Final[int] = 1
_DEFAULT_MEMORY_MB: Final[int] = 1024
_DEFAULT_ROOTFS_GB: Final[int] = 1
_DEFAULT_VOLUME_GB: Final[int] = 5
_DEFAULT_VAULT_PORT: Final[int] = 8823
_VAULT_MOUNT: Final[str] = "/vault"
_FLY_ABSENT_STATUS: Final[int] = 404
"""Fly's upstream resource-absence response; never exposed on Creek's wire."""
_IMMUTABLE_IMAGE_RE: Final[re.Pattern[str]] = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_FLY_PROVIDER: Final[str] = "fly"
_FLY_DELETED_CLASSES: Final[tuple[ResourceClass, ...]] = (
    ResourceClass.CREDENTIAL,
    ResourceClass.MACHINE,
    ResourceClass.VOLUME,
    ResourceClass.APP,
)
_MACHINE_STATES: Final[dict[str, ResourceState]] = {
    "started": ResourceState.RUNNING,
    "starting": ResourceState.RUNNING,
    "stopped": ResourceState.STOPPED,
    "stopping": ResourceState.STOPPED,
    "suspended": ResourceState.STOPPED,
    "destroyed": ResourceState.DESTROYED,
}


@unique
class FlyCredentialScope(StrEnum):
    """Credential kinds relevant to a multi-app provisioning controller."""

    ORG_DEPLOY = "org_deploy"
    PERSONAL = "personal"


@dataclass(frozen=True, slots=True)
class FlyCredential:
    """One short-lived, organization-scoped Fly deploy credential."""

    token: str = field(repr=False)
    organization: str
    scope: FlyCredentialScope
    expires_at: datetime

    def __post_init__(self) -> None:
        """Reject broad, blank, or time-unbounded provider credentials."""
        if not self.token.strip():
            raise ValueError("Fly credential token must not be blank")
        if not self.organization.strip():
            raise ValueError("Fly credential organization must not be blank")
        if self.scope is not FlyCredentialScope.ORG_DEPLOY:
            raise ValueError("Fly provisioning requires an org-scoped deploy token")
        if self.expires_at.tzinfo is None:
            raise ValueError("Fly credential expiry must be timezone-aware")

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        organization: str,
        scope: FlyCredentialScope,
        expires_at: datetime,
    ) -> FlyCredential:
        """Load a provider token from one owner-only regular file."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError("Fly credential file is unreadable") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("Fly credential file must be owner-only and regular")
            raw = os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_TOKEN_FILE_BYTES:
            raise ValueError("Fly credential file is too large")
        try:
            token = raw.decode("utf-8").strip()
        except UnicodeError as exc:
            raise ValueError("Fly credential file is not UTF-8") from exc
        return cls(token, organization, scope, expires_at)


@dataclass(frozen=True, slots=True)
class FlyRuntimeSecrets:
    """Secret-manager output installed as files in one Machine."""

    consumer_credential: str = field(repr=False)
    consumer_registry: bytes = field(repr=False)
    tls_certificate: bytes = field(repr=False)
    tls_private_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse incomplete bundles before creating a billable resource."""
        values = (
            self.consumer_credential.encode(),
            self.consumer_registry,
            self.tls_certificate,
            self.tls_private_key,
        )
        if any(not value for value in values):
            raise ValueError("Fly runtime secret bundle must be complete")


class FlySecretManager(Protocol):
    """Idempotent secret-manager boundary used by the provider lifecycle."""

    def issue(self, activation_id: str, consumer_identity: str) -> FlyRuntimeSecrets:
        """Return the same bundle for repeated calls for one activation."""

    def revoke(self, activation_id: str) -> None:
        """Idempotently make the activation's consumer credential unusable."""


class RefusingSecretManager:
    """A secret manager for fleet processes, which never provision or revoke.

    It lets the fleet CLI construct ``FlyProviderDriver`` for inventory and
    stop calls while making any accidental ``provision``/``delete`` fail
    closed with a non-retryable provider rejection.
    """

    def issue(self, activation_id: str, consumer_identity: str) -> Never:
        """Refuse to mint runtime secrets outside the provisioning worker."""
        del activation_id, consumer_identity
        raise ProviderError(FailureReason.PROVIDER_REJECTED, retryable=False)

    def revoke(self, activation_id: str) -> Never:
        """Refuse to revoke credentials outside the provisioning worker."""
        del activation_id
        raise ProviderError(FailureReason.PROVIDER_REJECTED, retryable=False)


@dataclass(frozen=True, slots=True)
class FlyProviderPolicy:
    """Configurable Fly allocation policy; values are not protocol constants."""

    organization: str
    image: str
    api_base_url: str = _DEFAULT_API_BASE_URL
    region: str = _DEFAULT_REGION
    app_prefix: str = "creek-vault"
    cpu_kind: str = _DEFAULT_CPU_KIND
    cpus: int = _DEFAULT_CPUS
    memory_mb: int = _DEFAULT_MEMORY_MB
    rootfs_size_gb: int = _DEFAULT_ROOTFS_GB
    volume_size_gb: int = _DEFAULT_VOLUME_GB
    vault_port: int = _DEFAULT_VAULT_PORT

    def __post_init__(self) -> None:
        """Validate configuration before any provider request is possible."""
        text_values = (
            self.organization,
            self.image,
            self.api_base_url,
            self.region,
            self.app_prefix,
            self.cpu_kind,
        )
        if any(not value.strip() for value in text_values):
            raise ValueError("Fly provider policy strings must not be blank")
        if _IMMUTABLE_IMAGE_RE.fullmatch(self.image) is None:
            raise ValueError("Fly provider image must use an immutable sha256 digest")
        try:
            api_url = httpx.URL(self.api_base_url)
        except httpx.InvalidURL as exc:
            raise ValueError("Fly provider API URL must use HTTPS") from exc
        if api_url.scheme != "https" or api_url.host is None:
            raise ValueError("Fly provider API URL must use HTTPS")
        positive = (
            self.cpus,
            self.memory_mb,
            self.rootfs_size_gb,
            self.volume_size_gb,
            self.vault_port,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("Fly provider numeric policy values must be positive")


@dataclass(frozen=True, slots=True)
class _AllocationRef:
    """Deterministic provider names derived from one activation."""

    activation_id: str
    allocation_id: str
    app_name: str
    volume_name: str
    machine_name: str


class FlyProviderDriver:
    """Idempotent Fly app, Machine, encrypted-volume, and shutdown lifecycle."""

    def __init__(
        self,
        policy: FlyProviderPolicy,
        credential: FlyCredential,
        secrets: FlySecretManager,
        client: httpx.Client | None = None,
    ) -> None:
        """Bind policy and secret boundaries without exposing their contents."""
        if credential.organization != policy.organization:
            raise ValueError("Fly credential organization does not match policy")
        self._policy = policy
        self._credential = credential
        self._secrets = secrets
        self._client = client or httpx.Client(
            base_url=policy.api_base_url,
            timeout=httpx.Timeout(30),
        )

    def provision(self, job: ProvisioningJob) -> ProviderAllocation:
        """Reconcile exactly one stopped allocation for *job*'s activation."""
        reference = self._reference(job.activation_id)
        runtime_secrets = self._secrets.issue(
            job.activation_id,
            job.consumer_identity,
        )
        self._ensure_app(reference)
        volume = self._ensure_volume(reference)
        machine = self._ensure_machine(
            reference,
            volume_id=self._identifier(volume, "volume"),
            runtime_secrets=runtime_secrets,
        )
        machine_id = self._identifier(machine, "Machine")
        return ProviderAllocation(
            allocation_id=reference.allocation_id,
            vault_url=(
                f"https://{machine_id}.vm.{reference.app_name}.internal:"
                f"{self._policy.vault_port}/v1"
            ),
            consumer_credential=runtime_secrets.consumer_credential,
        )

    def start(self, activation_id: str) -> None:
        """Idempotently start one allocation after caller authentication."""
        reference = self._reference(activation_id)
        machine = self._require_machine(reference)
        if str(machine.get("state")) in {"started", "starting"}:
            return
        self._change_machine_state(reference, machine, "start")

    def stop(self, activation_id: str) -> None:
        """Idempotently stop one allocation without detaching its volume."""
        reference = self._reference(activation_id)
        machine = self._require_machine(reference)
        if str(machine.get("state")) in {"stopped", "stopping", "suspended"}:
            return
        self._change_machine_state(reference, machine, "stop")

    def run_background_job(
        self,
        activation_id: str,
        *,
        drain: Callable[[], None],
        commit: Callable[[], None],
        close_vault: Callable[[], None],
    ) -> None:
        """Drain and commit durable work before closing and stopping the vault."""
        self.start(activation_id)
        try:
            drain()
            commit()
        finally:
            try:
                close_vault()
            finally:
                self.stop(activation_id)

    def delete(
        self,
        job: ProvisioningJob,
        provider_allocation_id: str | None,
    ) -> DeletionOutcome:
        """Revoke access and reconcile the activation to zero Fly resources.

        The outcome is returned only after absence is verified, so a caller can
        treat it as provider confirmation for the deletion receipt.
        """
        reference = self._reference(job.activation_id)
        if (
            provider_allocation_id is not None
            and provider_allocation_id != reference.allocation_id
        ):
            self._rejected("provider allocation identity does not match activation")
        self._secrets.revoke(job.activation_id)
        outcome = DeletionOutcome(_FLY_PROVIDER, _FLY_DELETED_CLASSES)
        if not self._app_exists(reference):
            return outcome
        for machine in self._matching_machines(reference):
            self._delete_machine(reference, machine)
        for volume in self._matching_volumes(reference):
            self._delete_volume(reference, volume)
        if self._matching_machines(reference) or self._matching_volumes(reference):
            self._unavailable("Fly resources remain after deletion")
        self._request(
            "DELETE",
            f"/v1/apps/{reference.app_name}",
            expected=(202, 204, _FLY_ABSENT_STATUS),
        )
        if self._app_exists(reference):
            self._unavailable("Fly app deletion has not converged")
        return outcome

    def list_resources(
        self,
        activation_ids: Sequence[str],
        *,
        app_names: Sequence[str] = (),
    ) -> tuple[ProviderResource, ...]:
        """Inventory the bounded known set with per-app GET calls only.

        Apps are derived from *activation_ids* and taken from *app_names* under
        the configured prefix; there is no org-wide listing.  Every resource is
        grouped by the allocation id derived from the app it lives in, never by
        the metadata it claims.
        """
        prefix = f"{self._policy.app_prefix}-"
        names = {self._reference(activation).app_name for activation in activation_ids}
        names.update(name for name in app_names if name.startswith(prefix))
        resources: list[ProviderResource] = []
        for name in sorted(names):
            app = self._app_by_name(name)
            if app is None:
                continue
            allocation_id = f"fly-{name.removeprefix(prefix)}"
            resources.append(
                ProviderResource(
                    allocation_id,
                    ResourceClass.APP,
                    ResourceState.OTHER,
                    None,
                    None,
                    self._identifier(app, "Fly app"),
                )
            )
            machines = self._request(
                "GET", f"/v1/apps/{name}/machines", expected=(200,)
            )
            volumes = self._request("GET", f"/v1/apps/{name}/volumes", expected=(200,))
            resources.extend(
                self._machine_resource(allocation_id, machine)
                for machine in self._objects(machines, "Fly Machine list")
            )
            resources.extend(
                self._volume_resource(allocation_id, volume)
                for volume in self._objects(volumes, "Fly volume list")
            )
        return tuple(sorted(resources, key=_resource_key))

    def expected_allocation_id(self, activation_id: str) -> str:
        """Return the allocation id a provisioned *activation_id* carries."""
        return self._reference(activation_id).allocation_id

    def _machine_resource(
        self,
        allocation_id: str,
        machine: Mapping[str, Any],
    ) -> ProviderResource:
        """Map one Machine to inventory; metadata is informational only."""
        config = machine.get("config")
        config = config if isinstance(config, dict) else {}
        metadata = config.get("metadata")
        activation = (
            metadata.get("creek_activation_id") if isinstance(metadata, dict) else None
        )
        rootfs = config.get("rootfs")
        size = rootfs.get("size_gb") if isinstance(rootfs, dict) else None
        return ProviderResource(
            allocation_id,
            ResourceClass.MACHINE,
            _MACHINE_STATES.get(str(machine.get("state")), ResourceState.OTHER),
            _size_gb(size),
            activation if isinstance(activation, str) else None,
            self._identifier(machine, "Fly Machine"),
        )

    def _volume_resource(
        self,
        allocation_id: str,
        volume: Mapping[str, Any],
    ) -> ProviderResource:
        """Map one volume to inventory."""
        destroyed = volume.get("state") == "destroyed"
        return ProviderResource(
            allocation_id,
            ResourceClass.VOLUME,
            ResourceState.DESTROYED if destroyed else ResourceState.OTHER,
            _size_gb(volume.get("size_gb")),
            None,
            self._identifier(volume, "Fly volume"),
        )

    def _ensure_app(self, reference: _AllocationRef) -> Mapping[str, Any]:
        response = self._request(
            "GET",
            f"/v1/apps/{reference.app_name}",
            expected=(200, _FLY_ABSENT_STATUS),
        )
        if response.status_code == _FLY_ABSENT_STATUS:
            self._request(
                "POST",
                "/v1/apps",
                expected=(200, 201, 422),
                payload={
                    "app_name": reference.app_name,
                    "org_slug": self._policy.organization,
                    "network": reference.allocation_id,
                },
            )
            response = self._request(
                "GET",
                f"/v1/apps/{reference.app_name}",
                expected=(200,),
            )
        app = self._object(response, "Fly app")
        organization = app.get("organization")
        if not isinstance(organization, dict) or organization.get("slug") != (
            self._policy.organization
        ):
            self._rejected("Fly app organization does not match policy")
        if app.get("network") not in {None, reference.allocation_id}:
            self._rejected("Fly app private network does not match allocation")
        return app

    def _ensure_volume(self, reference: _AllocationRef) -> Mapping[str, Any]:
        matches = self._matching_volumes(reference)
        if not matches:
            response = self._request(
                "POST",
                f"/v1/apps/{reference.app_name}/volumes",
                expected=(200, 201, 422),
                payload={
                    "name": reference.volume_name,
                    "region": self._policy.region,
                    "size_gb": self._policy.volume_size_gb,
                    "encrypted": True,
                    "require_unique_zone": False,
                },
            )
            matches = (
                self._matching_volumes(reference)
                if response.status_code == 422
                else [self._object(response, "Fly volume")]
            )
        volume = self._only(matches, "Fly volume")
        if (
            volume.get("region") != self._policy.region
            or volume.get("size_gb") != self._policy.volume_size_gb
            or volume.get("encrypted") is not True
            or volume.get("state") == "destroyed"
        ):
            self._rejected("Fly volume does not match encrypted allocation policy")
        return volume

    def _ensure_machine(
        self,
        reference: _AllocationRef,
        *,
        volume_id: str,
        runtime_secrets: FlyRuntimeSecrets,
    ) -> Mapping[str, Any]:
        matches = self._matching_machines(reference)
        if not matches:
            response = self._request(
                "POST",
                f"/v1/apps/{reference.app_name}/machines",
                expected=(200, 201, 422),
                payload=self._machine_request(
                    reference,
                    volume_id=volume_id,
                    runtime_secrets=runtime_secrets,
                ),
            )
            matches = (
                self._matching_machines(reference)
                if response.status_code == 422
                else [self._object(response, "Fly Machine")]
            )
        machine = self._only(matches, "Fly Machine")
        self._verify_machine(machine, reference, volume_id)
        return machine

    def _machine_request(
        self,
        reference: _AllocationRef,
        *,
        volume_id: str,
        runtime_secrets: FlyRuntimeSecrets,
    ) -> dict[str, Any]:
        encoded_files = (
            ("/run/secrets/creek_consumer_tokens", runtime_secrets.consumer_registry),
            ("/run/secrets/tls.crt", runtime_secrets.tls_certificate),
            ("/run/secrets/tls.key", runtime_secrets.tls_private_key),
        )
        return {
            "name": reference.machine_name,
            "region": self._policy.region,
            "skip_launch": True,
            "config": {
                "image": self._policy.image,
                "rootfs": {
                    "size_gb": self._policy.rootfs_size_gb,
                    "persist": "never",
                },
                "guest": {
                    "cpu_kind": self._policy.cpu_kind,
                    "cpus": self._policy.cpus,
                    "memory_mb": self._policy.memory_mb,
                },
                "mounts": [
                    {"volume": volume_id, "path": _VAULT_MOUNT, "encrypted": True}
                ],
                "metadata": {
                    "creek_allocation_id": reference.allocation_id,
                    "creek_activation_id": reference.activation_id,
                },
                "files": [
                    {
                        "guest_path": path,
                        "raw_value": base64.b64encode(value).decode("ascii"),
                    }
                    for path, value in encoded_files
                ],
                "restart": {"policy": "no"},
                "services": [
                    {
                        "protocol": "tcp",
                        "internal_port": self._policy.vault_port,
                        "ports": [],
                        "autostart": True,
                        "autostop": "stop",
                        "min_machines_running": 0,
                    }
                ],
            },
        }

    def _verify_machine(
        self,
        machine: Mapping[str, Any],
        reference: _AllocationRef,
        volume_id: str,
    ) -> None:
        config = machine.get("config")
        if not isinstance(config, dict):
            self._rejected("Fly Machine has no inspectable config")
        metadata = config.get("metadata")
        mount = {"volume": volume_id, "path": _VAULT_MOUNT, "encrypted": True}
        if (
            machine.get("region") != self._policy.region
            or config.get("image") != self._policy.image
            or metadata
            != {
                "creek_allocation_id": reference.allocation_id,
                "creek_activation_id": reference.activation_id,
            }
            or mount not in cast("list[object]", config.get("mounts", []))
        ):
            self._rejected("Fly Machine does not match allocation policy")

    def _require_machine(self, reference: _AllocationRef) -> Mapping[str, Any]:
        if not self._app_exists(reference):
            self._unavailable("Fly allocation app is missing")
        return self._only(self._matching_machines(reference), "Fly Machine")

    def _change_machine_state(
        self,
        reference: _AllocationRef,
        machine: Mapping[str, Any],
        operation: str,
    ) -> None:
        machine_id = self._identifier(machine, "Machine")
        self._request(
            "POST",
            f"/v1/apps/{reference.app_name}/machines/{machine_id}/{operation}",
            expected=(200, 201, 204),
        )

    def _delete_machine(
        self,
        reference: _AllocationRef,
        machine: Mapping[str, Any],
    ) -> None:
        machine_id = self._identifier(machine, "Machine")
        if machine.get("state") not in {"stopped", "stopping", "destroyed"}:
            self._request(
                "POST",
                f"/v1/apps/{reference.app_name}/machines/{machine_id}/stop",
                expected=(200, 201, 204, _FLY_ABSENT_STATUS),
            )
        self._request(
            "DELETE",
            f"/v1/apps/{reference.app_name}/machines/{machine_id}?force=true",
            expected=(200, 202, 204, _FLY_ABSENT_STATUS),
        )

    def _delete_volume(
        self,
        reference: _AllocationRef,
        volume: Mapping[str, Any],
    ) -> None:
        volume_id = self._identifier(volume, "volume")
        self._request(
            "DELETE",
            f"/v1/apps/{reference.app_name}/volumes/{volume_id}",
            expected=(200, 202, 204, _FLY_ABSENT_STATUS),
        )

    def _matching_machines(self, reference: _AllocationRef) -> list[Mapping[str, Any]]:
        response = self._request(
            "GET",
            f"/v1/apps/{reference.app_name}/machines",
            expected=(200,),
        )
        return [
            machine
            for machine in self._objects(response, "Fly Machine list")
            if self._machine_matches(machine, reference)
        ]

    @staticmethod
    def _machine_matches(
        machine: Mapping[str, Any],
        reference: _AllocationRef,
    ) -> bool:
        config = machine.get("config")
        metadata = config.get("metadata") if isinstance(config, dict) else None
        return machine.get("name") == reference.machine_name or (
            isinstance(metadata, dict)
            and metadata.get("creek_allocation_id") == reference.allocation_id
            and metadata.get("creek_activation_id") == reference.activation_id
        )

    def _matching_volumes(self, reference: _AllocationRef) -> list[Mapping[str, Any]]:
        response = self._request(
            "GET",
            f"/v1/apps/{reference.app_name}/volumes",
            expected=(200,),
        )
        return [
            volume
            for volume in self._objects(response, "Fly volume list")
            if volume.get("name") == reference.volume_name
            and volume.get("state") != "destroyed"
        ]

    def _app_exists(self, reference: _AllocationRef) -> bool:
        return self._app_by_name(reference.app_name) is not None

    def _app_by_name(self, app_name: str) -> Mapping[str, Any] | None:
        """Return the Fly app object for *app_name*, or None when it is absent."""
        response = self._request(
            "GET",
            f"/v1/apps/{app_name}",
            expected=(200, _FLY_ABSENT_STATUS),
        )
        if response.status_code == _FLY_ABSENT_STATUS:
            return None
        return self._object(response, "Fly app")

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: Sequence[int],
        payload: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._credential.token}"}
        try:
            response = (
                self._client.request(method, path, headers=headers)
                if payload is None
                else self._client.request(method, path, headers=headers, json=payload)
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                FailureReason.PROVIDER_UNAVAILABLE,
                retryable=True,
                private_detail=type(exc).__name__,
            ) from None
        if response.status_code not in expected:
            retryable = response.status_code == 429 or response.status_code >= 500
            reason = (
                FailureReason.PROVIDER_UNAVAILABLE
                if retryable
                else FailureReason.PROVIDER_REJECTED
            )
            raise ProviderError(reason, retryable=retryable) from None
        return response

    def _reference(self, activation_id: str) -> _AllocationRef:
        normalized = activation_id.strip()
        if not normalized:
            raise ValueError("activation_id must not be blank")
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        allocation_id = f"fly-{digest[:_ALLOCATION_DIGEST_LENGTH]}"
        app_name = f"{self._policy.app_prefix}-{digest[:_ALLOCATION_DIGEST_LENGTH]}"
        return _AllocationRef(
            activation_id=normalized,
            allocation_id=allocation_id,
            app_name=app_name,
            volume_name=f"{allocation_id}-vault",
            machine_name=f"{allocation_id}-machine",
        )

    @staticmethod
    def _object(response: httpx.Response, label: str) -> Mapping[str, Any]:
        try:
            value = response.json()
        except ValueError:
            FlyProviderDriver._unavailable(f"{label} response was invalid")
        if not isinstance(value, dict):
            FlyProviderDriver._unavailable(f"{label} response was invalid")
        return cast("Mapping[str, Any]", value)

    @staticmethod
    def _objects(response: httpx.Response, label: str) -> list[Mapping[str, Any]]:
        try:
            value = response.json()
        except ValueError:
            FlyProviderDriver._unavailable(f"{label} response was invalid")
        if not isinstance(value, list) or any(
            not isinstance(item, dict) for item in value
        ):
            FlyProviderDriver._unavailable(f"{label} response was invalid")
        return cast("list[Mapping[str, Any]]", value)

    @staticmethod
    def _identifier(resource: Mapping[str, Any], label: str) -> str:
        identifier = resource.get("id")
        if not isinstance(identifier, str) or not identifier:
            FlyProviderDriver._unavailable(f"{label} response omitted its id")
        return identifier

    @staticmethod
    def _only(
        resources: list[Mapping[str, Any]],
        label: str,
    ) -> Mapping[str, Any]:
        if not resources:
            FlyProviderDriver._unavailable(f"{label} is missing")
        if len(resources) != 1:
            FlyProviderDriver._rejected(f"duplicate {label} resources detected")
        return resources[0]

    @staticmethod
    def _unavailable(private_detail: str) -> Never:
        raise ProviderError(
            FailureReason.PROVIDER_UNAVAILABLE,
            retryable=True,
            private_detail=private_detail,
        )

    @staticmethod
    def _rejected(private_detail: str) -> Never:
        raise ProviderError(
            FailureReason.PROVIDER_REJECTED,
            retryable=False,
            private_detail=private_detail,
        )


def _size_gb(value: object) -> int | None:
    """Return a provider size in GB, or None when absent or not an integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _resource_key(resource: ProviderResource) -> tuple[str, str, str]:
    """Order inventory deterministically by allocation, class, and reference."""
    return (
        resource.provider_allocation_id,
        resource.resource_class.value,
        resource.provider_ref,
    )
