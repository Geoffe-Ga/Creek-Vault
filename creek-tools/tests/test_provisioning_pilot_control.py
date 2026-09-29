"""Single-endpoint Fly control-plane composition for the bounded pilot."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from creek_mcp.httpapi.app import ROUTING_MISS_STATUS
from creek_mcp.provisioning.pilot_control import build_edge_application

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

_HOST = "creek-control-pilot.fly.dev"
_EDGE_HEADERS = {
    "Host": _HOST,
    "X-Forwarded-Proto": "https",
    "Fly-Forwarded-Port": "443",
}


def _recording_app(name: str) -> Starlette:
    async def endpoint(request: Request) -> Response:
        return JSONResponse(
            {
                "app": name,
                "authorization": request.headers.get("authorization"),
                "edge_headers": sum(
                    header in request.headers
                    for header in (
                        "x-forwarded-proto",
                        "fly-forwarded-port",
                        "fly-replay-src",
                    )
                ),
            }
        )

    return Starlette(routes=[Route("/{path:path}", endpoint)])


def test_exact_fly_edge_dispatches_control_and_vault_paths_without_auth_crossover() -> (
    None
):
    """One hostname preserves path and bearer while selecting exactly one app."""
    app = build_edge_application(
        _recording_app("control"),
        _recording_app("router"),
        expected_host=_HOST,
        fly_app_name="creek-control-pilot",
        fly_machine_id="machine-001",
        routing_miss_status=ROUTING_MISS_STATUS,
    )
    client = TestClient(app)

    control = client.get(
        "/control/v1/jobs/job-1",
        headers={**_EDGE_HEADERS, "Authorization": "Bearer control-token"},
    )
    routed = client.get(
        "/v1/health",
        headers={**_EDGE_HEADERS, "Authorization": "Bearer vault-token"},
    )

    assert control.json() == {
        "app": "control",
        "authorization": "Bearer control-token",
        "edge_headers": 0,
    }
    assert routed.json() == {
        "app": "router",
        "authorization": "Bearer vault-token",
        "edge_headers": 0,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Host", "wrong.fly.dev"),
        ("X-Forwarded-Proto", "http"),
        ("Fly-Forwarded-Port", "80"),
    ],
)
def test_fly_edge_refuses_missing_wrong_or_duplicate_attestation(
    name: str,
    value: str,
) -> None:
    """Only Fly's exact HTTPS edge assertion reaches either credential realm."""
    app = build_edge_application(
        _recording_app("control"),
        _recording_app("router"),
        expected_host=_HOST,
        fly_app_name="creek-control-pilot",
        fly_machine_id="machine-001",
        routing_miss_status=ROUTING_MISS_STATUS,
    )
    headers = {**_EDGE_HEADERS, name: value}

    wrong = TestClient(app).get("/v1/health", headers=headers)
    missing = TestClient(app).get(
        "/v1/health",
        headers={key: item for key, item in _EDGE_HEADERS.items() if key != name},
    )
    duplicate = TestClient(app).get(
        "/v1/health",
        headers=[*_EDGE_HEADERS.items(), (name, _EDGE_HEADERS[name])],
    )

    assert wrong.status_code == 400
    assert missing.status_code == 400
    assert duplicate.status_code == 400
    assert wrong.content == missing.content == duplicate.content == b""


def test_fly_health_is_content_free_and_unknown_paths_never_cross_apps() -> None:
    """Fly can probe liveness without bearer or edge headers and nothing else."""
    app = build_edge_application(
        _recording_app("control"),
        _recording_app("router"),
        expected_host=_HOST,
        fly_app_name="creek-control-pilot",
        fly_machine_id="machine-001",
        routing_miss_status=ROUTING_MISS_STATUS,
    )
    client = TestClient(app)

    health = client.get("/__fly/health")
    unknown = client.get("/not-published", headers=_EDGE_HEADERS)

    assert health.status_code == 204
    assert health.content == b""
    assert unknown.status_code == 404
    assert unknown.content == b""


def test_fly_health_fails_content_free_when_any_supervised_component_dies() -> None:
    """Fly removes the one Machine from service without exposing a cause."""
    app = build_edge_application(
        _recording_app("control"),
        _recording_app("router"),
        expected_host=_HOST,
        fly_app_name="creek-control-pilot",
        fly_machine_id="machine-001",
        routing_miss_status=ROUTING_MISS_STATUS,
        healthy=lambda: False,
    )

    response = TestClient(app).get("/__fly/health")

    assert response.status_code == 503
    assert response.content == b""
