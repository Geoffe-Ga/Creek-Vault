"""Warn before the operator-recorded Fly org token expires (#1867, R2-2).

The pilot's provider credential is an org-scoped Fly token whose expiry the
owner records beside it as non-secret configuration
(``--fly-token-expires-at``, with ``--fly-token-id`` naming the token).  On
2026-10-13 such a token expired with no reminder and the pilot was down for
two days.  Owner decision R2-2 lengthens future tokens to 90 days, which makes
an advance warning mandatory rather than optional: a quarterly rotation is
easy to forget.

**What this module judges, and what it never touches.**  It reads the recorded
token ID and expiry and nothing else.  It never opens the token file, never
calls Fly's token API to discover the real expiry (the value is
operator-recorded by design), and never renders anything but the ID and the
expiry instant.  A token pasted into the ID field is refused by shape and is
never echoed back in the refusal.

**Verdicts.**  Each scheduled report re-judges the record, so a warning is
re-raised on every run until the recorded expiry changes:

* ``READY`` — more than the warning window remains; no alert.
* ``EXPIRING`` — the expiry is within the window (inclusive); alert.
* ``EXPIRED`` — the expiry is now or past; alert with "expired" wording, and
  the report refuses to run on a credential the provider no longer honours.
* ``UNRECORDED`` — no expiry was recorded; a configuration gap, alerted and
  refused, never treated as fine.

All four map onto the one closed kind
:attr:`~creek_mcp.provisioning.budget.AlertKind.PROVIDER_CREDENTIAL_EXPIRING`
(ADR-0013, amended by #1867).  The wire payload carries only that kind's
count; the wording lives in the operator report and the Machine log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import Final

from creek_mcp.provisioning.budget import FLEET_SUBJECT, Alert, AlertKind

PROVIDER_CREDENTIAL_WARN_WITHIN: Final[timedelta] = timedelta(hours=72)
"""The default warning window ahead of the recorded expiry (#1867)."""

_TOKEN_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
"""A Fly token's ID or name: short, printable, no whitespace.

Strict enough that a pasted token cannot pass: a ``FlyV1 ...`` header value
carries a space, and a bare macaroon is hundreds of characters long and carries
``,``, ``+``, ``/`` or ``=``.
"""

_UNRECORDED_ID: Final[str] = "(ID unrecorded)"


@unique
class ProviderCredentialStatus(StrEnum):
    """The closed outcome of judging one recorded expiry."""

    READY = "ready"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    UNRECORDED = "expiry_unrecorded"


_REFUSING: Final[frozenset[ProviderCredentialStatus]] = frozenset(
    {ProviderCredentialStatus.EXPIRED, ProviderCredentialStatus.UNRECORDED}
)


@dataclass(frozen=True, slots=True)
class ProviderCredentialCheck:
    """One content-free verdict on the recorded provider credential.

    Attributes:
        status: The closed outcome.
        token_id: The recorded non-secret token ID, or ``None``.
        expires_at: The recorded expiry, or ``None`` when unrecorded.
        warn_within: The warning window the verdict was judged against.
    """

    status: ProviderCredentialStatus
    token_id: str | None
    expires_at: datetime | None
    warn_within: timedelta

    @property
    def refused(self) -> bool:
        """Return whether the report must not run on this credential.

        Returns:
            ``True`` for an expired or unrecorded expiry.
        """
        return self.status in _REFUSING

    def alert(self) -> Alert | None:
        """Return the operator alert for this verdict, or ``None`` when ready.

        Returns:
            A ``provider_credential_expiring`` alert on the fleet whose
            ``measured`` names only the token ID and expiry, and whose
            ``threshold`` is the warning window in whole seconds.
        """
        if self.status is ProviderCredentialStatus.READY:
            return None
        return Alert(
            AlertKind.PROVIDER_CREDENTIAL_EXPIRING,
            FLEET_SUBJECT,
            None,
            self._wording(),
            str(int(self.warn_within.total_seconds())),
        )

    def _wording(self) -> str:
        """Return the human wording for a non-ready verdict."""
        token = f"token {self.token_id or _UNRECORDED_ID}"
        if self.expires_at is None:
            return f"{token} has no recorded expiry"
        verb = (
            "expired at"
            if self.status is ProviderCredentialStatus.EXPIRED
            else "expires at"
        )
        return f"{token} {verb} {self.expires_at.isoformat()}"


def validate_token_id(value: str) -> str:
    """Return *value* when it is shaped like a non-secret token ID.

    Args:
        value: The recorded token ID or name.

    Returns:
        The value unchanged.

    Raises:
        ValueError: When it is not ID-shaped.  The message never repeats the
            value, because a value that fails here may be the token itself.
    """
    if _TOKEN_ID_RE.fullmatch(value) is None:
        raise ValueError("Fly token ID must be a short non-secret token ID")
    return value


def parse_declared_expiry(value: str | None) -> datetime | None:
    """Parse the recorded expiry; absent means unrecorded.

    Args:
        value: The recorded ISO-8601 instant, or ``None``/empty.

    Returns:
        The timezone-aware instant, or ``None`` when nothing was recorded.

    Raises:
        ValueError: When a value is present but unparseable or naive; it is
            refused rather than read in whatever zone the reader is in.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Fly token expiry must be an ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        raise ValueError("Fly token expiry must carry a timezone")
    return parsed


def check_provider_credential(
    token_id: str | None,
    expires_at: datetime | None,
    *,
    now: datetime,
    warn_within: timedelta = PROVIDER_CREDENTIAL_WARN_WITHIN,
) -> ProviderCredentialCheck:
    """Judge the recorded expiry against *now* and the warning window.

    Args:
        token_id: The recorded non-secret token ID, or ``None``.
        expires_at: The recorded timezone-aware expiry, or ``None``.
        now: The instant to judge against.
        warn_within: How far ahead of the expiry to start warning.

    Returns:
        The verdict.  The window is inclusive: exactly *warn_within* remaining
        warns, and zero remaining is expired.
    """
    if expires_at is None:
        status = ProviderCredentialStatus.UNRECORDED
    elif expires_at <= now:
        status = ProviderCredentialStatus.EXPIRED
    elif expires_at - now <= warn_within:
        status = ProviderCredentialStatus.EXPIRING
    else:
        status = ProviderCredentialStatus.READY
    return ProviderCredentialCheck(status, token_id, expires_at, warn_within)
