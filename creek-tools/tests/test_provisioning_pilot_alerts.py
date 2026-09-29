"""Closed, authenticated alert delivery for the managed-vault pilot."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from creek_mcp.provisioning.pilot_alerts import (
    AlertDeliveryError,
    HttpFleetAlertSink,
)

if TYPE_CHECKING:
    from pathlib import Path


def _bearer(tmp_path: Path) -> Path:
    path = tmp_path / "handoff-bearer"
    path.write_text("synthetic-mounted-bearer", encoding="ascii")
    path.chmod(0o600)
    return path


def test_alert_sink_retries_boundedly_and_sends_only_closed_kind_counts(
    tmp_path: Path,
) -> None:
    """No private alert subject, allocation id, or corpus value crosses the sink."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503 if len(requests) < 3 else 204)

    sink = HttpFleetAlertSink(
        "https://adepthood.example/internal/vault-provisioning/alerts",
        _bearer(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    sink.deliver(
        (
            "monthly_budget_departure",
            "duplicate_resource",
            "duplicate_resource",
        )
    )

    assert len(requests) == 3
    request = requests[-1]
    assert request.headers["Authorization"] == "Bearer synthetic-mounted-bearer"
    assert json.loads(request.content) == {
        "schema": "creek_fleet_alert_v1",
        "counts": {"duplicate_resource": 2, "monthly_budget_departure": 1},
    }
    assert "subject" not in request.content.decode()
    sink.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://adepthood.example/internal/vault-provisioning/alerts",
        "https://adepthood.example/other",
        "https://user@adepthood.example/internal/vault-provisioning/alerts",
    ],
)
def test_alert_sink_refuses_every_noncanonical_destination(
    tmp_path: Path,
    url: str,
) -> None:
    """The bearer is never sent to a mutable path or non-TLS authority."""
    with pytest.raises(ValueError, match="alert URL"):
        HttpFleetAlertSink(url, _bearer(tmp_path))


def test_alert_sink_rejects_unknown_kind_before_network(tmp_path: Path) -> None:
    """An injected subject cannot masquerade as a new operator alert kind."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    sink = HttpFleetAlertSink(
        "https://adepthood.example/internal/vault-provisioning/alerts",
        _bearer(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="alert kind"):
        sink.deliver(("private-allocation-id",))
    assert calls == 0


def test_alert_sink_failure_is_bounded_and_content_free(tmp_path: Path) -> None:
    """Three failed attempts make scheduler health fail without response echo."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="private-response-canary")

    sink = HttpFleetAlertSink(
        "https://adepthood.example/internal/vault-provisioning/alerts",
        _bearer(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AlertDeliveryError, match="delivery failed") as caught:
        sink.deliver(("orphan_resource",))
    assert calls == 3
    assert "private-response-canary" not in str(caught.value)
