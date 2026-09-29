"""Closed authenticated alert delivery for the managed-vault pilot."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Final

import httpx

from creek_mcp.provisioning.budget import AlertKind
from creek_mcp.provisioning.handoff import read_bearer_file

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_ALERT_PATH: Final[str] = "/internal/vault-provisioning/alerts"
_SUCCESS: Final[int] = 204
_MAX_ATTEMPTS: Final[int] = 3
_TRANSIENT: Final[frozenset[int]] = frozenset({408, 425, 429})
_KINDS: Final[frozenset[str]] = frozenset(kind.value for kind in AlertKind)
_TRANSPORT_FAILURE: Final[int] = -1


class AlertDeliveryError(RuntimeError):
    """The content-free alert could not be accepted within its retry budget."""


class HttpFleetAlertSink:
    """Deliver closed alert-kind counts to one authenticated HTTPS endpoint."""

    def __init__(
        self,
        alert_url: str,
        bearer_file: Path,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """Validate the exact route and load the existing mounted handoff bearer."""
        try:
            url = httpx.URL(alert_url)
        except httpx.InvalidURL as exc:
            raise ValueError("alert URL must use the canonical HTTPS route") from exc
        if (
            url.scheme != "https"
            or url.host is None
            or url.path != _ALERT_PATH
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError("alert URL must use the canonical HTTPS route")
        self._url = str(url)
        self._bearer = read_bearer_file(bearer_file)
        self._client = client or httpx.Client(timeout=httpx.Timeout(5))

    def deliver(self, kinds: Iterable[str]) -> None:
        """Deliver only sorted closed kinds; retry transient failures twice."""
        counts = Counter(kinds)
        if not counts or not set(counts) <= _KINDS:
            raise ValueError("fleet alert kind is invalid")
        payload = {
            "schema": "creek_fleet_alert_v1",
            "counts": dict(sorted(counts.items())),
        }
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._client.stream(
                    "POST",
                    self._url,
                    headers={"Authorization": f"Bearer {self._bearer}"},
                    json=payload,
                ) as response:
                    status = response.status_code
            except httpx.HTTPError:
                status = _TRANSPORT_FAILURE
            if status == _SUCCESS:
                return
            retryable = (
                status == _TRANSPORT_FAILURE or status in _TRANSIENT or status >= 500
            )
            if not retryable or attempt == _MAX_ATTEMPTS - 1:
                break
        raise AlertDeliveryError("fleet alert delivery failed")

    def close(self) -> None:
        """Release the bounded HTTP transport."""
        self._client.close()
