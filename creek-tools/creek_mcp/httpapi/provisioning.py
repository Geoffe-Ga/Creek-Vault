"""Starlette adapter for the authenticated provisioning control plane (#1768)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from creek_mcp.httpapi.auth import BearerAuthMiddleware
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import read_off_loop, write_off_loop
from creek_mcp.httpapi.middleware.access_log import AccessLogMiddleware
from creek_mcp.httpapi.middleware.boundary import ErrorBoundaryMiddleware
from creek_mcp.provisioning.api import CONTRACT_VERSION, ActivationRequest
from creek_mcp.provisioning.store import (
    ActivationConflictError,
    InvalidJobTransitionError,
    ProvisioningStore,
)

if TYPE_CHECKING:
    from starlette.requests import Request

    from creek_mcp.provisioning.models import ProvisioningJob
    from creek_mcp.remote_auth import ConsumerTokenVerifier

_VERSION_HEADER: Final[str] = "Creek-Provisioning-Version"
_NO_STORE: Final[str] = "no-store"


def _response(payload: dict[str, object], *, status_code: int) -> JSONResponse:
    """Return one non-cacheable, versioned control-plane response."""
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            _VERSION_HEADER: CONTRACT_VERSION,
            "Cache-Control": _NO_STORE,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _error(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    """Return a bounded error with a correlation id and no exception detail."""
    return _response(
        {
            "code": code,
            "message": message,
            "request_id": context_of(request.scope).request_id,
        },
        status_code=status_code,
    )


def _job_payload(job: ProvisioningJob) -> dict[str, object]:
    """Serialize exactly the public, secret-free job contract."""
    return {
        "job_id": job.job_id,
        "activation_id": job.activation_id,
        "state": job.state.value,
        "attempts": job.attempts,
        "retryable": job.retryable,
        "failure_reason": (
            None if job.failure_reason is None else job.failure_reason.value
        ),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "status_url": f"/control/v1/jobs/{job.job_id}",
    }


class ProvisioningAPI:
    """HTTP handlers that mutate only durable queue state, never provider state."""

    def __init__(self, store: ProvisioningStore) -> None:
        """Bind handlers to the injected durable store."""
        self._store = store

    async def activate(self, request: Request) -> Response:
        """Submit an activation and immediately return its durable handle."""
        payload = await self._activation_payload(request)
        if isinstance(payload, Response):
            return payload
        consumer = context_of(request.scope).consumer
        if payload.consumer_identity != consumer:
            return _error(
                request,
                "consumer_mismatch",
                "consumer identity does not match the authenticated caller",
                403,
            )
        try:
            job = await write_off_loop(
                self._store.submit,
                payload.activation_id,
                payload.consumer_identity,
            )
        except ActivationConflictError:
            return _error(
                request,
                "activation_conflict",
                "activation cannot be accepted",
                409,
            )
        return _response(_job_payload(job), status_code=202)

    async def status(self, request: Request) -> Response:
        """Return one consumer-owned job without provider or credential fields."""
        job = await self._owned_job(request)
        if job is None:
            return _error(request, "job_unavailable", "job unavailable", 403)
        return _response(_job_payload(job), status_code=200)

    async def retry(self, request: Request) -> Response:
        """Requeue one consumer-owned retryable failure."""
        job_id = request.path_params["job_id"]
        consumer = context_of(request.scope).consumer
        assert consumer is not None
        try:
            job = await write_off_loop(self._store.retry, job_id, consumer)
        except InvalidJobTransitionError as error:
            if await read_off_loop(self._store.get, job_id, consumer) is None:
                return _error(request, "job_unavailable", "job unavailable", 403)
            del error
            return _error(
                request,
                "invalid_transition",
                "job is not retryable",
                409,
            )
        return _response(_job_payload(job), status_code=202)

    async def delete(self, request: Request) -> Response:
        """Idempotently enqueue provider teardown for one owned job."""
        job_id = request.path_params["job_id"]
        consumer = context_of(request.scope).consumer
        assert consumer is not None
        try:
            job = await write_off_loop(
                self._store.request_delete,
                job_id,
                consumer,
            )
        except InvalidJobTransitionError:
            return _error(request, "job_unavailable", "job unavailable", 403)
        return _response(_job_payload(job), status_code=202)

    async def _owned_job(self, request: Request) -> ProvisioningJob | None:
        """Load the path job under the authenticated consumer boundary."""
        consumer = context_of(request.scope).consumer
        assert consumer is not None
        return await read_off_loop(
            self._store.get,
            request.path_params["job_id"],
            consumer,
        )

    @staticmethod
    async def _activation_payload(request: Request) -> ActivationRequest | Response:
        """Parse one strict activation body or return the stable request refusal."""
        try:
            raw = await request.json()
            return ActivationRequest.model_validate(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            return _error(
                request,
                "invalid_request",
                "request body does not match the activation schema",
                400,
            )


def build_provisioning_app(
    store: ProvisioningStore,
    verifier: ConsumerTokenVerifier,
) -> Starlette:
    """Build the authenticated control plane without injecting a provider driver."""
    api = ProvisioningAPI(store)
    routes = [
        Route("/control/v1/activations", api.activate, methods=["POST"]),
        Route("/control/v1/jobs/{job_id}", api.status, methods=["GET"]),
        Route("/control/v1/jobs/{job_id}", api.delete, methods=["DELETE"]),
        Route("/control/v1/jobs/{job_id}/retry", api.retry, methods=["POST"]),
    ]
    middleware = [
        Middleware(AccessLogMiddleware),
        Middleware(ErrorBoundaryMiddleware),
        Middleware(BearerAuthMiddleware, verifier=verifier),
    ]
    return Starlette(routes=routes, middleware=middleware)
