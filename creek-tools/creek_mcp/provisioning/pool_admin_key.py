"""Custody of the organization pool admin key (ADR-0016 Decision 3.4, #1930).

**Why this credential is held in full, and why that is unavoidable.** The
vendor's Admin API identifies a per-activation key *by its own value*: the
``update`` call that resets a monthly cap, the ``update`` that extends an
expiry in place every 15 days, and the ``delete`` that revokes a key at
deletion all require the **full key value** as the presented credential.  A
hash cannot serve, because the vendor compares the bytes it was given against
its own stored secret rather than against a digest we chose; a truncated
handle cannot serve either, because the Admin API has no notion of a key
prefix or an opaque key id that a caller may substitute.  So the control plane
keeps the per-activation keys in full inside the encrypted bundle, and keeps
*this* key — the organization admin key that mints, re-caps and deletes them —
as a declared secret file of its own.

**Why it is more dangerous than the per-activation keys.** A per-activation
vendor key is an unbound bearer limited by its own caps: copied out of a
Machine config it spends up to that cap and no further, which is why the cap
is the brake (Decision 1 item 3).  The admin key has no cap and no activation
binding.  It can raise every cap, mint new keys, and delete the key a running
vault depends on.  Its blast radius is the whole organization, so:

* it is never carried by a vault Machine, only by the control plane;
* it travels as a mode-0400 file, never an environment value
  (:mod:`creek_mcp.provisioning.pilot_bootstrap` refuses the env form);
* nothing here logs, echoes, hashes or measures the value.  Every record this
  module produces is a closed-vocabulary code, a count or a latency: a hash
  would be a verifier for an offline guess, and a length would narrow one, so
  neither is emitted even in a refusal.

**Fail closed.** :func:`verify_pool_admin_key` answers one content-free
:class:`PoolAdminKeyStatus` and never the value.  :func:`load_pool_admin_key`
is the one way to the value and *raises* rather than returning a default,
placeholder or ambient credential, so an unconfigured or malformed key can
never fall through to an unauthenticated path.  Each status has its own exit
code, disjoint from :mod:`creek_mcp.container_health`'s probe codes — in
particular from ``pool-unconfigured`` (30), which means "no pool at all" and
must stay distinguishable from "a pool, with no usable admin key".

No spend is authorized (Decision 4): nothing here dials a vendor.
:func:`classify_admin_response` classifies a vendor admin response that
somebody else made, from its **status code alone** — the body is never read,
because a 401 body is exactly where a vendor echoes the credential back.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Final

from creek_mcp.provisioning.budget import FLEET_SUBJECT, Alert, AlertKind
from creek_mcp.provisioning.pool_bundle import POOL_ADMIN_KEY_EXPIRES_AT_ENV
from creek_mcp.provisioning.production_secrets import read_owner_only_file

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

POOL_ADMIN_KEY_RENEW_WITHIN: Final[timedelta] = timedelta(days=15)
"""Alert this far ahead of the declared expiry.

Decision 3.4 extends a *per-activation* key in place every 15 days for 30, so
one missed cycle is survivable.  The admin key is re-issued by hand, which
takes an owner action, so it gets the same notice period: a window long enough
that one ignored alert is not yet an outage.
"""

_MAX_ADMIN_KEY_BYTES: Final[int] = 4096
"""A bound, not a guess: no vendor bearer credential is larger than this."""

_ADMIN_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._~:+/=-]{20,512}")
"""Vendor-neutral bearer-credential shape.

Deliberately not a vendor's exact prefix or length — this work lands before
the vendor is chosen.  It is strict enough to catch the real mistakes: a
pasted PEM, a JSON blob, a second line, a shell-quoted value, a placeholder
too short to be a credential, or anything carrying whitespace or a control
character that would break the Authorization header it ends up in.
"""


@unique
class PoolAdminKeyStatus(StrEnum):
    """The closed custody vocabulary, one code per outcome.

    Attributes:
        READY: Present, owner-only, well-formed, and in date.
        MISSING: No declared secret file exists at all.
        MALFORMED: The file exists but is not a usable bearer credential, or
            its mode is not owner-only.
        REJECTED: The vendor refused it (401/403): present and well-formed,
            but no longer valid.
        NEAR_EXPIRY: Usable, but inside :data:`POOL_ADMIN_KEY_RENEW_WITHIN` of
            its declared expiry.  The only non-refusing non-ready status.
        EXPIRY_UNDECLARED: No parseable, timezone-aware expiry was declared,
            so near-expiry could never be detected.
        EXPIRED: The declared expiry has passed.
    """

    READY = "pool-admin-key-ready"
    MISSING = "pool-admin-key-missing"
    MALFORMED = "pool-admin-key-malformed"
    REJECTED = "pool-admin-key-rejected"
    NEAR_EXPIRY = "pool-admin-key-near-expiry"
    EXPIRY_UNDECLARED = "pool-admin-key-expiry-undeclared"
    EXPIRED = "pool-admin-key-expired"

    @property
    def exit_code(self) -> int:
        """Return this status's stable supervisor exit code.

        Returns:
            The code, disjoint from every ``--check`` probe code but ``0``.
        """
        return _EXIT_CODES[self]


_EXIT_CODES: Final[dict[PoolAdminKeyStatus, int]] = {
    PoolAdminKeyStatus.READY: 0,
    PoolAdminKeyStatus.MISSING: 40,
    PoolAdminKeyStatus.MALFORMED: 41,
    PoolAdminKeyStatus.REJECTED: 42,
    PoolAdminKeyStatus.NEAR_EXPIRY: 43,
    PoolAdminKeyStatus.EXPIRY_UNDECLARED: 44,
    PoolAdminKeyStatus.EXPIRED: 45,
}
"""Codes start at 40 so no probe code (20-26, 30-33) is ever reused."""

_ALERT_KINDS: Final[dict[PoolAdminKeyStatus, AlertKind]] = {
    PoolAdminKeyStatus.MISSING: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
    PoolAdminKeyStatus.MALFORMED: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
    PoolAdminKeyStatus.EXPIRY_UNDECLARED: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
    PoolAdminKeyStatus.EXPIRED: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
    PoolAdminKeyStatus.REJECTED: AlertKind.POOL_ADMIN_KEY_REJECTED,
    PoolAdminKeyStatus.NEAR_EXPIRY: AlertKind.POOL_ADMIN_KEY_NEAR_EXPIRY,
}
"""Three kinds, not seven: the operator's action differs, not the code."""

_REFUSING: Final[frozenset[PoolAdminKeyStatus]] = frozenset(
    {
        PoolAdminKeyStatus.MISSING,
        PoolAdminKeyStatus.MALFORMED,
        PoolAdminKeyStatus.REJECTED,
        PoolAdminKeyStatus.EXPIRY_UNDECLARED,
        PoolAdminKeyStatus.EXPIRED,
    }
)
"""Every status that must stop the operation. ``NEAR_EXPIRY`` still works."""

_UNAUTHORIZED: Final[frozenset[int]] = frozenset({401, 403})
"""The only vendor responses that say anything about the admin key itself."""


@dataclass(frozen=True, slots=True)
class PoolAdminKeyCheck:
    """One content-free custody verdict.

    Attributes:
        status: The closed-vocabulary outcome.
        remaining_seconds: Whole seconds left before the declared expiry, for
            a :attr:`PoolAdminKeyStatus.NEAR_EXPIRY` verdict only.  A count,
            so it is admissible in an operational record.
    """

    status: PoolAdminKeyStatus
    remaining_seconds: int | None = None

    @property
    def exit_code(self) -> int:
        """Return the status's stable exit code.

        Returns:
            The code.
        """
        return self.status.exit_code

    @property
    def refused(self) -> bool:
        """Return whether the admin-key operation must not proceed.

        Returns:
            ``True`` for every status but ``READY`` and ``NEAR_EXPIRY``.
        """
        return self.status in _REFUSING

    def alert(self) -> Alert | None:
        """Return this verdict's closed operator alert, or ``None``.

        Returns:
            The alert, or ``None`` when the key is ready.  The subject is the
            fleet, because the admin key belongs to no single activation.
        """
        kind = _ALERT_KINDS.get(self.status)
        if kind is None:
            return None
        measured = (
            None if self.remaining_seconds is None else str(self.remaining_seconds)
        )
        threshold = (
            None
            if measured is None
            else str(int(POOL_ADMIN_KEY_RENEW_WITHIN.total_seconds()))
        )
        return Alert(kind, FLEET_SUBJECT, None, measured, threshold)


class PoolAdminKeyError(RuntimeError):
    """The admin key is unusable, so the operation refuses.

    The message is the status code and nothing else, so a traceback that
    escapes into a log or an error body carries no key material and no
    measurement of it.
    """

    def __init__(self, status: PoolAdminKeyStatus) -> None:
        """Carry only the closed-vocabulary status.

        Args:
            status: The refusing status.
        """
        super().__init__(status.value)
        self.status = status

    @property
    def exit_code(self) -> int:
        """Return the refusing status's exit code.

        Returns:
            The code.
        """
        return self.status.exit_code


def load_pool_admin_key(path: Path) -> str:
    """Return the full admin key value, or refuse.

    The one way to the value.  There is no default, no placeholder and no
    environment fallback: a caller that cannot get the key here cannot make an
    admin call at all, which is what keeps an unconfigured control plane from
    silently proceeding unauthenticated.

    Args:
        path: The declared secret file, normally
            :data:`~creek_mcp.provisioning.pool_bundle.POOL_ADMIN_KEY_GUEST_PATH`.

    Returns:
        The full key value, exactly as the vendor's ``update`` and ``delete``
        calls require it.

    Raises:
        PoolAdminKeyError: When the file is absent, not owner-only, or not a
            well-formed bearer credential.  The error names only its status.
    """
    status, value = _stored_status(path)
    if value is None:
        raise PoolAdminKeyError(status)
    return value


def verify_pool_admin_key(
    path: Path,
    *,
    environ: Mapping[str, str],
    now: datetime,
) -> PoolAdminKeyCheck:
    """Verify the declared secret's custody without exposing its value.

    Storage first, then the declared expiry.  The value is read into a local
    frame to be format-checked and is never returned, logged or measured from
    here.

    Args:
        path: The declared secret file.
        environ: The process environment to read the declared expiry from,
            injected rather than taken ambiently so a caller can verify a
            Machine's environment without being that Machine.
        now: The instant to judge the expiry against.

    Returns:
        The content-free verdict.
    """
    stored, _value = _stored_status(path)
    if stored is not PoolAdminKeyStatus.READY:
        return PoolAdminKeyCheck(stored)
    expires_at = _declared_expiry(environ)
    if expires_at is None:
        return PoolAdminKeyCheck(PoolAdminKeyStatus.EXPIRY_UNDECLARED)
    remaining = expires_at - now
    if remaining <= timedelta(0):
        return PoolAdminKeyCheck(PoolAdminKeyStatus.EXPIRED)
    if remaining <= POOL_ADMIN_KEY_RENEW_WITHIN:
        return PoolAdminKeyCheck(
            PoolAdminKeyStatus.NEAR_EXPIRY, int(remaining.total_seconds())
        )
    return PoolAdminKeyCheck(PoolAdminKeyStatus.READY)


def classify_admin_response(status_code: int) -> PoolAdminKeyCheck | None:
    """Classify a vendor admin-API response from its status line alone.

    Takes an ``int`` on purpose: a 401 body is precisely where a vendor echoes
    the presented credential back, so there is no parameter here that a body
    could arrive in.  Mirrors ``_pool_status_error``'s documented "no body is
    read" discipline on the inference path.

    Args:
        status_code: The HTTP status the vendor answered with.

    Returns:
        A ``REJECTED`` verdict for 401 and 403, or ``None`` for every other
        code — a 500 or a timeout says nothing about the key's validity, and
        claiming otherwise would raise a false custody alert.
    """
    if status_code in _UNAUTHORIZED:
        return PoolAdminKeyCheck(PoolAdminKeyStatus.REJECTED)
    return None


def _stored_status(path: Path) -> tuple[PoolAdminKeyStatus, str | None]:
    """Read and format-check the stored credential.

    Args:
        path: The declared secret file.

    Returns:
        ``(READY, value)`` when the file is a custodied bearer credential, and
        ``(MISSING | MALFORMED, None)`` otherwise.  No branch here reports the
        value's length, prefix or digest.
    """
    if not os.path.lexists(path):
        return (PoolAdminKeyStatus.MISSING, None)
    try:
        raw = read_owner_only_file(path, maximum=_MAX_ADMIN_KEY_BYTES)
        value = raw.decode("ascii").strip()
    except (UnicodeError, ValueError):
        return (PoolAdminKeyStatus.MALFORMED, None)
    if _ADMIN_KEY_RE.fullmatch(value) is None:
        return (PoolAdminKeyStatus.MALFORMED, None)
    return (PoolAdminKeyStatus.READY, value)


def _declared_expiry(environ: Mapping[str, str]) -> datetime | None:
    """Return the declared, timezone-aware expiry, or ``None``.

    Args:
        environ: The environment carrying
            :data:`~creek_mcp.provisioning.pool_bundle.POOL_ADMIN_KEY_EXPIRES_AT_ENV`.

    Returns:
        The instant, or ``None`` when it is absent, unparseable or naive — all
        three are "nobody declared it", and all three refuse.
    """
    declared = environ.get(POOL_ADMIN_KEY_EXPIRES_AT_ENV, "").strip()
    if not declared:
        return None
    try:
        parsed = datetime.fromisoformat(declared)
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed
