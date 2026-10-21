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
  module produces is a closed-vocabulary code or a count: a hash would be a
  verifier for an offline guess, and a length would narrow one, so neither is
  emitted even in a refusal.

**In use, or not yet.** No vendor is chosen and no key exists, so the custody
path is **declared now and enforced on use**.  The control plane declares
:data:`POOL_ADMIN_KEY_IN_USE_ENV` from a closed two-value vocabulary, and it
defaults to not-in-use: a pilot deployed before the first key exists neither
carries the secret nor refuses to boot.  The alternative — refusing to boot
without a key nothing yet consumes — would force an operator to invent a
placeholder credential, which is the one thing this module exists to refuse,
and would make ``READY`` meaningless.  Once the key *is* declared in use every
refusal below is binding.

**Fail closed.** :func:`verify_pool_admin_key` answers one content-free
:class:`PoolAdminKeyStatus` and never the value.  :func:`load_pool_admin_key`
is the one way to the value and *raises* for every status but
:attr:`PoolAdminKeyStatus.READY` and :attr:`PoolAdminKeyStatus.NEAR_EXPIRY`,
so an unconfigured, malformed, undated or **expired** key can never be
presented, and no caller receives a default, placeholder or ambient
credential.  Each refusing status has its own exit code, disjoint from
:mod:`creek_mcp.container_health`'s probe codes — in particular from
``pool-unconfigured`` (30), which means "no pool at all" and must stay
distinguishable from "a pool, with no usable admin key".

**Alerting is registered, not yet emitted (#1896).** :meth:`
PoolAdminKeyCheck.alert` maps each status onto the closed
:class:`~creek_mcp.provisioning.budget.AlertKind` vocabulary, but **no
production path calls it yet**, so a ``NEAR_EXPIRY`` verdict today is
*recorded on stderr at startup, not delivered to anyone*.  Two things have to
land before it can be: the fleet-report emission belongs to the attesting
provider (#1896), which owns the only periodic pass that could re-check a
long-running control plane, and adepthood's alert receiver enforces **its own**
closed kind list, which today admits six kinds and not even
``pool_credential_orphan``.  Emitting an unknown kind would be answered 400,
which Creek's sink escalates to ``AlertDeliveryError``, which fails the
scheduler component and takes the Machine unhealthy — so wiring an emitter
before that list is widened would turn a warning into an outage.  Nothing here
pretends otherwise: near-expiry is not an early warning until #1896 wires it.

No spend is authorized (Decision 4): nothing here dials a vendor.
:func:`classify_admin_response` classifies a vendor admin response that
somebody else made, from its **status code alone** — the body is never read,
because a 401 body is exactly where a vendor echoes the credential back.

**Import surface, honestly bounded.** This module imports nothing first-party
at module scope, because it is reached from the root bootstrap before the
privilege drop.  That buys a bounded surface **in the not-in-use regime
only**: once a key is declared in use, :func:`_stored_status` runs and pulls
``production_secrets`` — and with it ``cryptography``'s x509/AEAD surface and
the ``provisioning`` modules that module imports — still as root.  Measured
on this tree, on top of what ``pilot_bootstrap`` already loads at module
scope: **2** modules in the not-in-use regime, **47** in the in-use one.  That
is a real cost, not a cancelled one, and both figures' shape is pinned by
``test_pool_admin_key_custody.py`` so neither claim can drift.  It is not inlined
away here: ``pilot_bootstrap`` already imports ``container_runtime`` at module
scope, so the "minimal bootstrap" premise is weak before this module is
reached, and duplicating an ``O_NOFOLLOW``/mode-checking reader to dodge one
import would trade a real DRY guarantee for a notional one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek_mcp.provisioning.budget import Alert

POOL_ADMIN_KEY_GUEST_PATH: Final[str] = "/run/secrets/creek_pool_admin_key"
"""The organization admin key's one declared home.

It is a *control-plane* file, never a vault one: no activation's Machine
carries it, because a vault that held it could re-cap or delete every other
vault's key.
"""

POOL_ADMIN_KEY_SECRET_NAME: Final[str] = "CREEK_POOL_ADMIN_KEY_B64"
"""The Fly app secret that injects :data:`POOL_ADMIN_KEY_GUEST_PATH`."""

POOL_ADMIN_KEY_IN_USE_ENV: Final[str] = "CREEK_POOL_ADMIN_KEY_IN_USE"
"""Whether this control plane consumes an admin key: ``"1"`` or ``"0"``.

A closed two-value vocabulary, absent meaning ``"0"``.  Declared rather than
inferred so the pilot's own config says which regime it is in, and defaulting
to not-in-use so declaring custody cannot brick a pilot that has no key yet.
Any other value is a garbled declaration and refuses
(:attr:`PoolAdminKeyStatus.UNDECLARED`).  An absent flag is **not** garbled:
it takes the permissive path, which is what makes declaring custody safe
before a key exists, and is also the one case no refusal catches.
"""

POOL_ADMIN_KEY_EXPIRES_AT_ENV: Final[str] = "CREEK_POOL_ADMIN_KEY_EXPIRES_AT"
"""The admin key's declared expiry: a non-secret ISO-8601 instant.

The vendor holds the real expiry and no vendor call is authorized (Decision
4), so the operator declares it beside the secret the way the Fly org token's
own expiry is declared.  While the key is in use an undeclared expiry is a
refusal rather than an assumption: a key nobody dated expires unnoticed and
takes the per-activation key lifecycle with it.
"""

POOL_ADMIN_KEY_IN_USE: Final[str] = "1"
"""The in-use declaration's only affirmative value."""

POOL_ADMIN_KEY_NOT_IN_USE: Final[str] = "0"
"""The in-use declaration's only negative value, and its default."""

POOL_ADMIN_KEY_RENEW_WITHIN: Final[timedelta] = timedelta(days=15)
"""Record a near-expiry verdict this far ahead of the declared expiry.

Decision 3.4 extends a *per-activation* key in place every 15 days for 30, so
one missed cycle is survivable.  The admin key is re-issued by hand, which
takes an owner action, so it gets the same notice period.
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
        NOT_IN_USE: This control plane declares that it consumes no admin
            key, so there is nothing to custody yet.  Not a refusal.
        NEAR_EXPIRY: Usable, but inside :data:`POOL_ADMIN_KEY_RENEW_WITHIN`
            of its declared expiry.  Not a refusal — the key still works —
            and today only *recorded*, never delivered (see the module
            docstring).
        MISSING: No declared secret file exists at all.
        MALFORMED: The file exists but is not a usable bearer credential, or
            its mode is not owner-only.
        REJECTED: The vendor refused it (401/403): present and well-formed,
            but no longer valid.  A **runtime** verdict only; no vendor call
            happens at startup.
        UNDECLARED: A **garbled** in-use flag — one outside
            :data:`POOL_ADMIN_KEY_IN_USE` / :data:`POOL_ADMIN_KEY_NOT_IN_USE`
            — or, while in use, a missing, unparseable or timezone-naive
            expiry.  An *absent* or empty in-use flag is **not** this: it is
            ``NOT_IN_USE``, which proceeds.  The distinction matters to an
            operator, so it is spelled out rather than summarised as "the
            declaration is absent or invalid".
        EXPIRED: The declared expiry has passed.
    """

    READY = "pool-admin-key-ready"
    NOT_IN_USE = "pool-admin-key-not-in-use"
    NEAR_EXPIRY = "pool-admin-key-near-expiry"
    MISSING = "pool-admin-key-missing"
    MALFORMED = "pool-admin-key-malformed"
    REJECTED = "pool-admin-key-rejected"
    UNDECLARED = "pool-admin-key-undeclared"
    EXPIRED = "pool-admin-key-expired"

    @property
    def exit_code(self) -> int:
        """Return this status's stable process exit code.

        Returns:
            ``0`` for every status that lets startup proceed, and otherwise a
            code unique to the refusal and disjoint from every probe code.
        """
        return _EXIT_CODES[self]

    @property
    def refused(self) -> bool:
        """Return whether this status must stop the operation.

        Returns:
            ``True`` for the five refusals.  ``READY``, ``NOT_IN_USE`` and
            ``NEAR_EXPIRY`` each let the control plane run.
        """
        return self in _REFUSING


_EXIT_CODES: Final[dict[PoolAdminKeyStatus, int]] = {
    PoolAdminKeyStatus.READY: 0,
    PoolAdminKeyStatus.NOT_IN_USE: 0,
    PoolAdminKeyStatus.NEAR_EXPIRY: 0,
    PoolAdminKeyStatus.MISSING: 40,
    PoolAdminKeyStatus.MALFORMED: 41,
    PoolAdminKeyStatus.REJECTED: 42,
    PoolAdminKeyStatus.UNDECLARED: 43,
    PoolAdminKeyStatus.EXPIRED: 44,
}
"""Codes start at 40 so no probe code (20-26, 30-33) is ever reused.

Every non-refusing status is ``0``: a working key must never stop the control
plane, so the distinguishing signal for those is the printed status string,
not the exit code.
"""

_REFUSING: Final[frozenset[PoolAdminKeyStatus]] = frozenset(
    {
        PoolAdminKeyStatus.MISSING,
        PoolAdminKeyStatus.MALFORMED,
        PoolAdminKeyStatus.REJECTED,
        PoolAdminKeyStatus.UNDECLARED,
        PoolAdminKeyStatus.EXPIRED,
    }
)
"""Every status that must stop the operation."""

_USABLE: Final[frozenset[PoolAdminKeyStatus]] = frozenset(
    {PoolAdminKeyStatus.READY, PoolAdminKeyStatus.NEAR_EXPIRY}
)
"""The only two statuses for which :func:`load_pool_admin_key` returns a value.

``NOT_IN_USE`` is deliberately absent: it is not a refusal for *startup*, but
a caller that asks for the value of a key its own config says it does not use
has a bug, and handing one back would be the fail-open this module exists to
prevent.
"""

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
            ``True`` for the five refusing statuses.
        """
        return self.status.refused

    def alert(self) -> Alert | None:
        """Return this verdict's closed operator alert, or ``None``.

        **No production path calls this yet** — see the module docstring for
        the two gates (#1896's fleet-report pass, and adepthood's own closed
        kind list) that have to clear before an alert can be delivered.

        Returns:
            The alert, or ``None`` when there is nothing to report.  The
            subject is the fleet, because the admin key belongs to no single
            activation.
        """
        # Imported here, not at module scope: the root bootstrap reaches this
        # module before the privilege drop, and ``budget`` pulls the bench
        # cost model with it.
        from creek_mcp.provisioning.budget import FLEET_SUBJECT, Alert, AlertKind

        kinds = {
            PoolAdminKeyStatus.MISSING: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
            PoolAdminKeyStatus.MALFORMED: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
            PoolAdminKeyStatus.UNDECLARED: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
            PoolAdminKeyStatus.EXPIRED: AlertKind.POOL_ADMIN_KEY_UNUSABLE,
            PoolAdminKeyStatus.REJECTED: AlertKind.POOL_ADMIN_KEY_REJECTED,
            PoolAdminKeyStatus.NEAR_EXPIRY: AlertKind.POOL_ADMIN_KEY_NEAR_EXPIRY,
        }
        kind = kinds.get(self.status)
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
    """The admin key may not be presented, so the operation refuses.

    The message is the status code and nothing else, so a traceback that
    escapes into a log or an error body carries no key material and no
    measurement of it.
    """

    def __init__(self, status: PoolAdminKeyStatus) -> None:
        """Carry only the closed-vocabulary status.

        Args:
            status: The status that forbade the value.
        """
        super().__init__(status.value)
        self.status = status

    @property
    def exit_code(self) -> int:
        """Return the status's exit code.

        Returns:
            The code.
        """
        return self.status.exit_code


def pool_admin_key_in_use(environ: Mapping[str, str]) -> bool | None:
    """Return the declared in-use regime, or ``None`` when it is garbled.

    Args:
        environ: The environment carrying :data:`POOL_ADMIN_KEY_IN_USE_ENV`.

    Returns:
        ``True`` while the control plane declares it consumes an admin key,
        ``False`` when it declares it does not or declares nothing at all, and
        ``None`` when the declaration is outside the closed vocabulary.
    """
    declared = environ.get(POOL_ADMIN_KEY_IN_USE_ENV, "").strip()
    if not declared or declared == POOL_ADMIN_KEY_NOT_IN_USE:
        return False
    if declared == POOL_ADMIN_KEY_IN_USE:
        return True
    return None


def pool_admin_secret_files(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """Return the admin-key files the root bootstrap must normalize.

    Mirrors :func:`~creek_mcp.provisioning.pool_bundle.pool_secret_files`: the
    bootstrap *validates listed files* and never enumerates ``/run/secrets``
    (Decision 6), and the list is derived from the Machine's own closed
    declaration.  A control plane with no admin key in use normalizes nothing,
    so declaring the custody path cannot make an existing pilot unbootable.

    Args:
        environ: The Machine's environment.

    Returns:
        The one admin-key path while it is in use, and otherwise empty.  A
        garbled declaration also returns empty, so the refusal comes from
        :func:`verify_pool_admin_key`'s content-free code rather than from the
        bootstrap's generic exit 2.
    """
    return (
        (Path(POOL_ADMIN_KEY_GUEST_PATH),)
        if pool_admin_key_in_use(environ) is True
        else ()
    )


def verify_pool_admin_key(
    path: Path,
    *,
    environ: Mapping[str, str],
    now: datetime,
) -> PoolAdminKeyCheck:
    """Verify the declared secret's custody without exposing its value.

    The declaration first, then storage, then the declared expiry.  The value
    is read into a local frame to be format-checked and is never returned,
    logged or measured from here.

    Args:
        path: The declared secret file.
        environ: The process environment to read the custody declaration
            from, injected rather than taken ambiently so a caller can verify
            a Machine's environment without being that Machine.
        now: The instant to judge the expiry against.

    Returns:
        The content-free verdict.
    """
    check, _value = _classify(path, environ=environ, now=now)
    return check


def load_pool_admin_key(
    path: Path,
    *,
    environ: Mapping[str, str],
    now: datetime,
) -> str:
    """Return the full admin key value, or refuse.

    The one way to the value.  There is no default, no placeholder and no
    environment fallback, and the expiry is enforced here rather than only in
    :func:`verify_pool_admin_key`: a loader that handed back an expired key
    would let a caller present a credential the vendor has already stopped
    honouring, which is a fail-open dressed as a check.

    Args:
        path: The declared secret file, normally
            :data:`POOL_ADMIN_KEY_GUEST_PATH`.
        environ: The environment carrying the custody declaration.
        now: The instant to judge the expiry against.

    Returns:
        The full key value, exactly as the vendor's ``update`` and ``delete``
        calls require it.

    Raises:
        PoolAdminKeyError: For every status but ``READY`` and
            ``NEAR_EXPIRY``, including ``NOT_IN_USE``.  The error names only
            its status.
    """
    check, value = _classify(path, environ=environ, now=now)
    if check.status not in _USABLE or value is None:
        raise PoolAdminKeyError(check.status)
    return value


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


def _classify(
    path: Path,
    *,
    environ: Mapping[str, str],
    now: datetime,
) -> tuple[PoolAdminKeyCheck, str | None]:
    """Produce the one verdict both public entry points share.

    Args:
        path: The declared secret file.
        environ: The environment carrying the custody declaration.
        now: The instant to judge the expiry against.

    Returns:
        The verdict, paired with the value only when it may be presented.
    """
    in_use = pool_admin_key_in_use(environ)
    if in_use is None:
        return (PoolAdminKeyCheck(PoolAdminKeyStatus.UNDECLARED), None)
    if not in_use:
        return (PoolAdminKeyCheck(PoolAdminKeyStatus.NOT_IN_USE), None)
    stored, value = _stored_status(path)
    if stored is not PoolAdminKeyStatus.READY or value is None:
        return (PoolAdminKeyCheck(stored), None)
    expires_at = _declared_expiry(environ)
    if expires_at is None:
        return (PoolAdminKeyCheck(PoolAdminKeyStatus.UNDECLARED), None)
    remaining = expires_at - now
    if remaining <= timedelta(0):
        return (PoolAdminKeyCheck(PoolAdminKeyStatus.EXPIRED), None)
    if remaining <= POOL_ADMIN_KEY_RENEW_WITHIN:
        return (
            PoolAdminKeyCheck(
                PoolAdminKeyStatus.NEAR_EXPIRY, int(remaining.total_seconds())
            ),
            value,
        )
    return (PoolAdminKeyCheck(PoolAdminKeyStatus.READY), value)


def _stored_status(path: Path) -> tuple[PoolAdminKeyStatus, str | None]:
    """Read and format-check the stored credential.

    Args:
        path: The declared secret file.

    Returns:
        ``(READY, value)`` when the file is a custodied bearer credential, and
        ``(MISSING | MALFORMED, None)`` otherwise.  No branch here reports the
        value's length, prefix or digest.
    """
    # Imported here, not at module scope, so a control plane in the
    # not-in-use regime never pays for it: this module is reached from the
    # root bootstrap before the privilege drop. Note what that does and does
    # not buy — reaching THIS function means a key is declared in use, so the
    # x509/AEAD surface ``production_secrets`` carries is loaded as root after
    # all. The deferral bounds the default regime, not every regime.
    from creek_mcp.provisioning.production_secrets import read_owner_only_file

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
        environ: The environment carrying :data:`POOL_ADMIN_KEY_EXPIRES_AT_ENV`.

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
