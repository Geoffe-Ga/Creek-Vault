"""Shared Fly Machines test doubles: an HTTP fake, a secret manager, builders.

Used by the Fly driver tests and the fleet CLI tests so both drive the same
documented endpoints.  Every value here is a synthetic canary, never a real
credential.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
    FlyRuntimeSecrets,
)
from creek_mcp.provisioning.models import JobOperation, JobState, ProvisioningJob

NOW = datetime(2026, 9, 7, 4, tzinfo=UTC)
PROVIDER_TOKEN = "fly-provider-secret-canary"
CONSUMER_TOKEN = "creek-consumer-secret-canary"
TLS_KEY = "tls-private-key-secret-canary"
ORGANIZATION = "creek-vaults"
IMAGE = "registry.example/creek@sha256:" + "a" * 64
API_BASE_URL = "https://fly.test"


@dataclass
class FakeSecretManager:
    """Return stable per-activation runtime secrets and record revocation."""

    revoked: set[str]

    def issue(self, activation_id: str, consumer_identity: str) -> FlyRuntimeSecrets:
        """Return the same secret bundle on every retry."""
        del activation_id
        return FlyRuntimeSecrets(
            consumer_credential=CONSUMER_TOKEN,
            consumer_registry=f"{consumer_identity}={CONSUMER_TOKEN}\n".encode(),
            tls_certificate=b"test-certificate",
            tls_private_key=TLS_KEY.encode(),
        )

    def revoke(self, activation_id: str) -> None:
        """Record an idempotent revocation."""
        self.revoked.add(activation_id)


class FakeFlyAPI:
    """Stateful HTTP fake for the documented Fly Machines endpoints."""

    def __init__(self) -> None:
        self.apps: dict[str, dict[str, Any]] = {}
        self.volumes: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        self.machines: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        self.requests: list[tuple[str, str]] = []
        self.failures: dict[tuple[str, str], int] = {}
        self.failure_body = "provider unavailable"
        self.malformed: dict[tuple[str, str], str] = {}

    def fail_once(self, method: str, path_suffix: str) -> None:
        """Return one 503 for a matching method and path suffix."""
        self.failures[(method, path_suffix)] = 1

    def malformed_once(self, method: str, path_suffix: str, body: str) -> None:
        """Return one 200 with a malformed *body* for a matching request."""
        self.malformed[(method, path_suffix)] = body

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Serve one authenticated request without a real network."""
        assert request.headers["Authorization"] == f"Bearer {PROVIDER_TOKEN}"
        method = request.method
        path = request.url.path
        self.requests.append((method, path))
        for key, remaining in self.failures.items():
            if remaining and method == key[0] and path.endswith(key[1]):
                self.failures[key] = remaining - 1
                return httpx.Response(503, text=self.failure_body, request=request)
        for key, body in list(self.malformed.items()):
            if method == key[0] and path.endswith(key[1]):
                del self.malformed[key]
                return httpx.Response(200, text=body, request=request)
        segments = path.strip("/").split("/")
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
            if self.volumes[app_name] or self.machines[app_name]:
                return httpx.Response(409, request=request)
            del self.apps[app_name]
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
                matched_machine["state"] = "started"
            elif segments[1] == "stop":
                matched_machine["state"] = "stopped"
            else:
                return httpx.Response(404, request=request)
            return httpx.Response(200, json=matched_machine, request=request)
        if len(segments) == 1 and request.method == "DELETE":
            self.machines[app_name].remove(matched_machine)
            return httpx.Response(200, request=request)
        return httpx.Response(404, request=request)

    @staticmethod
    def _json(request: httpx.Request) -> dict[str, Any]:
        """Decode a fake request body."""
        value = json.loads(request.content)
        assert isinstance(value, dict)
        return value


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


def fly_driver(
    api: FakeFlyAPI,
    secrets: FakeSecretManager | None = None,
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
        api_base_url=API_BASE_URL,
    )
    return FlyProviderDriver(
        policy, credential, secrets or FakeSecretManager(set()), fly_client(api)
    )
