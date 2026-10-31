"""Shared Fly Machines test doubles: an HTTP fake, a secret manager, builders.

Used by the Fly driver tests and the fleet CLI tests so both drive the same
documented endpoints.  Every value here is a synthetic canary, never a real
credential.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
    FlyRuntimeSecrets,
)
from creek_mcp.provisioning.models import (
    JobOperation,
    JobState,
    McpOAuthSettings,
    ProvisioningJob,
)
from creek_mcp.provisioning.pool_bundle import (
    PoolCredential,
    PoolCredentialInventory,
    PoolRuntimeBundle,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 9, 7, 4, tzinfo=UTC)
PROVIDER_TOKEN = "fly-provider-secret-canary"
CONSUMER_TOKEN = "creek-consumer-secret-canary"
TLS_KEY = "tls-private-key-secret-canary"
ORGANIZATION = "creek-vaults"
IMAGE = "registry.example/creek@sha256:" + "a" * 64
API_BASE_URL = "https://fly.test"
ROUTING_PUBLIC_URL = "https://vault-router.example.com"
MCP_ISSUER = "https://adepthood.example"
MCP_RESOURCE_URL = f"{ROUTING_PUBLIC_URL}/mcp"
_FLY_VOLUME_NAME = re.compile(r"[a-z0-9_]{1,30}")


POOL_URL = "https://pool.vendor.example"
POOL_VENDOR_KEY = "vendor-pool-key-secret-canary"
POOL_MANIFEST_JSON = json.dumps(
    {
        "schema_version": 1,
        "runtime_name": "vendor-enclave",
        "runtime_version": "1.4.0",
        "runtime_digest": "b" * 64,
        "model_name": "pool-model-a",
        "model_blob_sha256": "c" * 64,
        "runtime_inventory_digest": "d" * 64,
        "quantization": "mxfp4",
        "parameter_count": 120_000_000_000,
        "size_bytes": 65_000_000_000,
        "license_spdx": "Apache-2.0",
        "license_url": "https://example.invalid/license",
    },
    sort_keys=True,
)


def pool_bundle(
    *,
    bundle_version: int = 1,
    pool_url: str = POOL_URL,
    model_name: str = "pool-model-a",
    key: str = POOL_VENDOR_KEY,
) -> PoolRuntimeBundle:
    """Return one synthetic per-activation pool bundle."""
    return PoolRuntimeBundle(
        bundle_version=bundle_version,
        pool_url=pool_url,
        manifest_json=POOL_MANIFEST_JSON.replace("pool-model-a", model_name),
        credential=PoolCredential.vendor_key(key),
    )


@dataclass
class FakeSecretManager:
    """Return stable per-activation runtime secrets and record revocation.

    With an *issuer* the first issuance for an activation mints that
    activation's pool bundle and every later issuance returns the stored one,
    which is what makes ``_verify_machine`` per-activation (ADR-0016 D1.1).
    *mcp_oauth* is stored the same way (#1974): an activation first issued
    without it keeps none until :meth:`roll_mcp_oauth` writes it.  With
    *per_allocation_mcp_resource* the roll moves a legacy or shared record to
    its own per-allocation resource (#2004), exactly as the encrypted manager.
    """

    revoked: set[str]
    issuer: Any | None = None
    issued: dict[str, FlyRuntimeSecrets] = field(default_factory=dict)
    call_log: list[tuple[str, str]] | None = None
    mcp_oauth: McpOAuthSettings | None = None
    per_allocation_mcp_resource: bool = False

    def issue(
        self,
        activation_id: str,
        consumer_identity: str,
        *,
        requester_identity: str,
    ) -> FlyRuntimeSecrets:
        """Return the same secret bundle on every retry."""
        del requester_identity
        stored = self.issued.get(activation_id)
        if stored is not None:
            return stored
        pool = None if self.issuer is None else self.issuer.issue(activation_id)
        bundle = FlyRuntimeSecrets(
            consumer_credential=CONSUMER_TOKEN,
            replay_state="r" * 64,
            consumer_registry=f"{consumer_identity}={CONSUMER_TOKEN}\n".encode(),
            tls_certificate=b"test-certificate",
            tls_private_key=TLS_KEY.encode(),
            pool=pool,
            mcp_oauth=self.mcp_oauth,
        )
        self.issued[activation_id] = bundle
        return bundle

    def roll_mcp_oauth(self, activation_id: str) -> McpOAuthSettings:
        """Write the rolled MCP OAuth settings into one stored activation."""
        if self.mcp_oauth is None:
            raise ValueError("no MCP OAuth settings are configured")
        stored = self.issued[activation_id]
        target = self.mcp_oauth.roll_target(
            stored.mcp_oauth, per_allocation=self.per_allocation_mcp_resource
        )
        self.issued[activation_id] = dataclasses.replace(stored, mcp_oauth=target)
        return target

    def mcp_oauth_is_current(self, settings: McpOAuthSettings) -> bool:
        """Return whether a record holding *settings* needs no roll."""
        if self.mcp_oauth is None:
            return True
        return self.mcp_oauth.is_rolled(
            settings, per_allocation=self.per_allocation_mcp_resource
        )

    def revoke(self, activation_id: str) -> None:
        """Record an idempotent revocation, confirming the pool refusal first.

        When a *call_log* is shared with :class:`FakeFlyAPI`, the revocation is
        appended to it, so a test can prove the pool refused the credential
        before any Machine was destroyed (T11).
        """
        stored = self.issued.get(activation_id)
        if self.issuer is not None and stored is not None and stored.pool is not None:
            self.issuer.revoke(stored.pool)
        self.revoked.add(activation_id)
        if self.call_log is not None:
            self.call_log.append(("REVOKE", "pool-credential"))


class FakeFlyAPI:
    """Stateful HTTP fake for the documented Fly Machines endpoints."""

    def __init__(self) -> None:
        self.provider_token = PROVIDER_TOKEN
        self.apps: dict[str, dict[str, Any]] = {}
        self.volumes: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        self.machines: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        self.requests: list[tuple[str, str]] = []
        self.failures: dict[tuple[str, str], list[int]] = {}
        self.failure_body = "provider unavailable"
        self.malformed: dict[tuple[str, str], str] = {}
        self.replace_machine_on_wait = False
        self.replacement_drops_mount = False
        self.volume_loses_encryption_on_wait = False
        self.start_stays_stopped = False
        self.deleted_volume_state: str | None = None
        self.updates: list[dict[str, Any]] = []

    def fail_once(self, method: str, path_suffix: str, *, status: int = 503) -> None:
        """Return *status* once for a matching method and path suffix."""
        self.failures[(method, path_suffix)] = [status]

    def malformed_once(self, method: str, path_suffix: str, body: str) -> None:
        """Return one 200 with a malformed *body* for a matching request."""
        self.malformed[(method, path_suffix)] = body

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Serve one authenticated request without a real network."""
        assert request.headers["Authorization"] == f"Bearer {self.provider_token}"
        method = request.method
        path = request.url.path
        self.requests.append((method, path))
        for key, statuses in self.failures.items():
            if statuses and method == key[0] and path.endswith(key[1]):
                return httpx.Response(
                    statuses.pop(0), text=self.failure_body, request=request
                )
        for key, body in list(self.malformed.items()):
            if method == key[0] and path.endswith(key[1]):
                del self.malformed[key]
                return httpx.Response(200, text=body, request=request)
        segments = path.strip("/").split("/")
        if segments == ["v1", "apps"] and method == "GET":
            if request.url.params.get("org_slug") != ORGANIZATION:
                return httpx.Response(404, request=request)
            return httpx.Response(
                200,
                json={"total_apps": len(self.apps), "apps": list(self.apps.values())},
                request=request,
            )
        if segments == ["v1", "apps"] and method == "POST":
            return self._create_app(request)
        if len(segments) >= 3 and segments[:2] == ["v1", "apps"]:
            return self._app_request(request, segments[2:])
        return httpx.Response(404, request=request)

    def _create_app(self, request: httpx.Request) -> httpx.Response:
        body = self._json(request)
        app_name = str(body["app_name"])
        if app_name in self.apps:
            return httpx.Response(422, request=request)
        self.apps[app_name] = {
            "id": f"app-{len(self.apps) + 1}",
            "name": app_name,
            "organization": {"slug": body["org_slug"]},
            "network": body["network"],
        }
        return httpx.Response(201, json=self.apps[app_name], request=request)

    def _app_request(
        self,
        request: httpx.Request,
        segments: list[str],
    ) -> httpx.Response:
        app_name = segments[0]
        if len(segments) == 1:
            return self._app_resource(request, app_name)
        if app_name not in self.apps:
            return httpx.Response(404, request=request)
        if segments[1] == "volumes":
            return self._volume_request(request, app_name, segments[2:])
        if segments[1] == "machines":
            return self._machine_request(request, app_name, segments[2:])
        return httpx.Response(404, request=request)

    def _app_resource(self, request: httpx.Request, app_name: str) -> httpx.Response:
        if request.method == "GET":
            app = self.apps.get(app_name)
            return httpx.Response(
                404 if app is None else 200,
                json=None if app is None else app,
                request=request,
            )
        if request.method == "DELETE":
            if app_name not in self.apps:
                return httpx.Response(404, request=request)
            live_volumes = [
                volume
                for volume in self.volumes[app_name]
                if volume.get("state") != self.deleted_volume_state
            ]
            if live_volumes or self.machines[app_name]:
                return httpx.Response(409, request=request)
            del self.apps[app_name]
            self.volumes.pop(app_name, None)
            return httpx.Response(202, request=request)
        return httpx.Response(405, request=request)

    def _volume_request(
        self,
        request: httpx.Request,
        app_name: str,
        segments: list[str],
    ) -> httpx.Response:
        if not segments and request.method == "GET":
            return httpx.Response(200, json=self.volumes[app_name], request=request)
        if not segments and request.method == "POST":
            body = self._json(request)
            if _FLY_VOLUME_NAME.fullmatch(str(body["name"])) is None:
                return httpx.Response(
                    400,
                    json={"error": "invalid volume name"},
                    request=request,
                )
            volume = {
                "id": f"vol-{len(self.volumes[app_name]) + 1}",
                "name": body["name"],
                "region": body["region"],
                "size_gb": body["size_gb"],
                "encrypted": body["encrypted"],
                "state": "created",
            }
            self.volumes[app_name].append(volume)
            return httpx.Response(200, json=volume, request=request)
        if len(segments) == 1 and request.method == "DELETE":
            volume_id = segments[0]
            if self.deleted_volume_state is not None:
                for volume in self.volumes[app_name]:
                    if volume["id"] == volume_id:
                        volume["state"] = self.deleted_volume_state
                        return httpx.Response(200, json=volume, request=request)
            before = len(self.volumes[app_name])
            self.volumes[app_name] = [
                volume for volume in self.volumes[app_name] if volume["id"] != volume_id
            ]
            status = 200 if len(self.volumes[app_name]) < before else 404
            return httpx.Response(status, request=request)
        return httpx.Response(404, request=request)

    def _machine_request(
        self,
        request: httpx.Request,
        app_name: str,
        segments: list[str],
    ) -> httpx.Response:
        if not segments and request.method == "GET":
            return httpx.Response(200, json=self.machines[app_name], request=request)
        if not segments and request.method == "POST":
            body = self._json(request)
            machine = {
                "id": f"machine-{len(self.machines[app_name]) + 1}",
                "name": body["name"],
                "region": body["region"],
                "state": "stopped" if body["skip_launch"] else "started",
                "config": body["config"],
            }
            self.machines[app_name].append(machine)
            return httpx.Response(200, json=machine, request=request)
        if not segments:
            return httpx.Response(404, request=request)
        matched_machine: dict[str, Any] | None = None
        for candidate in self.machines[app_name]:
            if candidate["id"] == segments[0]:
                matched_machine = candidate
                break
        if matched_machine is None:
            return httpx.Response(404, request=request)
        if len(segments) == 2 and request.method == "POST":
            if segments[1] == "start":
                if not self.start_stays_stopped:
                    matched_machine["state"] = "started"
            elif segments[1] == "stop":
                matched_machine["state"] = "stopped"
            else:
                return httpx.Response(404, request=request)
            return httpx.Response(200, json=matched_machine, request=request)
        if len(segments) == 2 and segments[1] == "wait" and request.method == "GET":
            if self.volume_loses_encryption_on_wait:
                self.volumes[app_name][0]["encrypted"] = False
                self.volume_loses_encryption_on_wait = False
            if self.replace_machine_on_wait:
                replacement = dict(matched_machine)
                if self.replacement_drops_mount:
                    replacement["config"] = dict(matched_machine["config"])
                    replacement["config"]["mounts"] = []
                replacement["id"] = "machine-replacement"
                replacement["state"] = "started"
                self.machines[app_name] = [replacement]
                self.replace_machine_on_wait = False
                return httpx.Response(200, request=request)
            status = 200 if matched_machine["state"] == "started" else 408
            return httpx.Response(status, request=request)
        if len(segments) == 1 and request.method == "DELETE":
            self.machines[app_name].remove(matched_machine)
            return httpx.Response(200, request=request)
        if len(segments) == 1 and request.method == "POST":
            return self._update_machine(request, matched_machine)
        return httpx.Response(404, request=request)

    def _update_machine(
        self,
        request: httpx.Request,
        machine: dict[str, Any],
    ) -> httpx.Response:
        """Replace one Machine's config the way Fly's update endpoint does."""
        body = self._json(request)
        self.updates.append(body)
        machine["config"] = body["config"]
        if not body["skip_launch"]:
            machine["state"] = "started"
        return httpx.Response(200, json=machine, request=request)

    @staticmethod
    def _json(request: httpx.Request) -> dict[str, Any]:
        """Decode a fake request body."""
        value = json.loads(request.content)
        assert isinstance(value, dict)
        return value


class ProviderNormalizedFlyAPI(FakeFlyAPI):
    """Return the exact vault Machine shape observed from Fly's live API."""

    def _machine_request(
        self,
        request: httpx.Request,
        app_name: str,
        segments: list[str],
    ) -> httpx.Response:
        """Normalize newly created Machines before returning provider state."""
        response = super()._machine_request(request, app_name, segments)
        if not segments and request.method == "POST" and response.status_code == 200:
            machine = self.machines[app_name][-1]
            config = machine["config"]
            config.pop("user", None)
            mount = config["mounts"][0]
            volume = next(
                volume
                for volume in self.volumes[app_name]
                if volume["id"] == mount["volume"]
            )
            mount.update(name=volume["name"], size_gb=volume["size_gb"])
            service = config["services"][0]
            service["autostop"] = True
            service["force_instance_key"] = None
            return httpx.Response(200, json=machine, request=request)
        return response


def fly_job(activation_id: str = "activation-fly-001") -> ProvisioningJob:
    """Return one provisioning-state job for *activation_id*."""
    return ProvisioningJob(
        job_id="job-fly-001",
        activation_id=activation_id,
        requester_identity="adepthood",
        consumer_identity="adepthood-user-001",
        state=JobState.PROVISIONING,
        operation=JobOperation.CREATE,
        attempts=1,
        retryable=False,
        failure_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def fly_client(api: FakeFlyAPI) -> httpx.Client:
    """Return an HTTP client routed into *api* without a network."""
    return httpx.Client(
        base_url=API_BASE_URL, transport=httpx.MockTransport(api.handle)
    )


def mcp_oauth_settings() -> McpOAuthSettings:
    """Return the synthetic settings a router at ``ROUTING_PUBLIC_URL`` writes."""
    return McpOAuthSettings(MCP_RESOURCE_URL, MCP_ISSUER)


def fly_driver(
    api: FakeFlyAPI,
    secrets: FakeSecretManager | None = None,
    *,
    discover_organization_apps: bool = False,
    fly_replay_enabled: bool = False,
    credentials: PoolCredentialInventory | None = None,
) -> FlyProviderDriver:
    """Return a driver over *api* with the synthetic org-scoped credential."""
    credential = FlyCredential(
        token=PROVIDER_TOKEN,
        organization=ORGANIZATION,
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=NOW + timedelta(days=7),
    )
    policy = FlyProviderPolicy(
        organization=ORGANIZATION,
        image=IMAGE,
        routing_public_url=ROUTING_PUBLIC_URL,
        api_base_url=API_BASE_URL,
        discover_organization_apps=discover_organization_apps,
        fly_replay_enabled=fly_replay_enabled,
    )
    return FlyProviderDriver(
        policy,
        credential,
        secrets or FakeSecretManager(set()),
        fly_client(api),
        pool_credentials=credentials,
    )


def fly_driver_from_file(
    api: FakeFlyAPI,
    token_file: Path,
    secrets: FakeSecretManager,
) -> FlyProviderDriver:
    """Return a driver that reloads its credential from an owner-only mount."""
    credential = FlyCredential.from_file(
        token_file,
        organization=ORGANIZATION,
        scope=FlyCredentialScope.ORG_DEPLOY,
        expires_at=NOW + timedelta(days=7),
    )
    policy = FlyProviderPolicy(
        organization=ORGANIZATION,
        image=IMAGE,
        routing_public_url=ROUTING_PUBLIC_URL,
        api_base_url=API_BASE_URL,
    )
    return FlyProviderDriver(policy, credential, secrets, fly_client(api))
