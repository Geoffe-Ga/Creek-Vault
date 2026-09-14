"""Authenticated one-time HTTPS callback adapter for issue #1805."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import httpx

from creek_mcp.provisioning.driver import HandoffError
from creek_mcp.provisioning.production_secrets import read_owner_only_file

if TYPE_CHECKING:
    from pathlib import Path

_SUCCESS: Final[int] = 204
_TRANSIENT_STATUSES: Final[frozenset[int]] = frozenset({408, 425, 429})
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]+")
_COMPLETION_PATH: Final[str] = "/internal/vault-provisioning/completions"


class HttpOneTimeCredentialHandoff:
    """POST one credential to Adepthood without retaining its plaintext."""

    def __init__(
        self,
        completion_url: str,
        bearer_file: Path,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """Validate the fixed HTTPS destination and mounted bearer path."""
        try:
            url = httpx.URL(completion_url)
        except httpx.InvalidURL as exc:
            raise ValueError("handoff URL must use HTTPS") from exc
        if (
            url.scheme != "https"
            or url.host is None
            or url.path != _COMPLETION_PATH
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError("handoff URL must be the HTTPS internal completion route")
        self._completion_url = str(url)
        self._bearer = _read_bearer(bearer_file)
        self._client = client or httpx.Client(timeout=httpx.Timeout(10))

    def deliver(
        self,
        job_id: str,
        consumer_identity: str,
        vault_url: str,
        consumer_credential: str,
    ) -> None:
        """Deliver once; classify only the status and discard every response body."""
        try:
            with self._client.stream(
                "POST",
                self._completion_url,
                headers={"Authorization": f"Bearer {self._bearer}"},
                json={
                    "job_id": job_id,
                    "consumer_identity": consumer_identity,
                    "vault_url": vault_url,
                    "consumer_credential": consumer_credential,
                },
            ) as response:
                status = response.status_code
        except httpx.HTTPError:
            raise HandoffError(retryable=True) from None
        if status == _SUCCESS:
            return
        retryable = status in _TRANSIENT_STATUSES or status >= 500
        raise HandoffError(retryable=retryable)


def _read_bearer(path: Path) -> str:
    """Load a mounted callback bearer without preserving decoding detail."""
    try:
        bearer = read_owner_only_file(path).decode("utf-8").strip()
    except (UnicodeError, ValueError):
        raise ValueError("handoff bearer file is invalid") from None
    if _BEARER_RE.fullmatch(bearer) is None:
        raise ValueError("handoff bearer file is invalid")
    return bearer
