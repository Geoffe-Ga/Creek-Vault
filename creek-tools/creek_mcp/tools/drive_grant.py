"""Authorising the Drive connector over the network (#1568), ADR-0012 option C.

The two verbs :mod:`creek_mcp.tools.drive` deliberately does not have: *begin*
an authorization, and *complete* one. Together they close the last unmet clause
of the seeding epic (#1523) — connecting Drive with no CLI and no shell access
on the vault host.

**The redirect never lands here.** The caller supplies its own ``redirect_uri``,
owns the browser leg, and relays the authorization code back over the bearer
credential it already holds. That is what keeps this feature from needing an
anonymous path through :class:`~creek_mcp.httpapi.auth.BearerAuthMiddleware`,
which today has no exemptions at all and is pinned that way by
``tests/test_v1_api_auth.py::test_no_path_is_exempt_from_the_bearer_gate``.

**Nothing here returns a credential.** :func:`drive_authorize_tool` returns a
URL and an opaque ``state``; :func:`drive_authorization_exchange_tool` returns
the connector's *state*, in the shape ``GET /v1/connectors/drive`` already
publishes. The client secret and the minted token stay on this host — the
former read from ``credentials.json``, the latter written by the same
:func:`creek.ingest.gdrive._write_token_file` the CLI path uses.

**Every refusal is one of two constants.** An OAuth failure names the client
id, the redirect URI and Google's own error code; a state lookup answers a
question about which authorizations this server has outstanding. Neither may be
narrated, so unknown / expired / consumed states and a refused exchange all
collapse to :data:`GRANT_REFUSED_REASON`, and every operator-actionable
condition to :data:`GRANT_UNAVAILABLE_REASON`. Neither tool reads the token
file, so no refusal can disclose whether a credential already exists.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from creek._fslock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    VaultLockTimeoutError,
    vault_lock,
)
from creek.config import GoogleDriveConfig
from creek.ingest.gdrive import GoogleApiUnavailableError, _write_token_file
from creek.ingest.gdrive_grant import (
    DriveGrantError,
    authorization_url,
    build_flow,
    exchange_code,
    new_code_verifier,
)
from creek.ingest.ledger import SourceLedger
from creek_mcp.tier_ceiling import refusal_response
from creek_mcp.tools.drive import (
    DRIVE_LEDGER_SOURCE,
    STATE_CONNECTED,
    _audit,
    _loaded_config,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from creek_mcp.tier_ceiling import TierCeiling

logger = logging.getLogger(__name__)

AUTHORIZE_TOOL_NAME: Final[str] = "creek.drive.authorize"
"""The audited name of the begin-authorization verb."""

EXCHANGE_TOOL_NAME: Final[str] = "creek.drive.authorize.complete"
"""The audited name of the complete-authorization verb."""

GRANT_UNAVAILABLE_REASON: Final[str] = "drive authorisation is unavailable"
"""No amount of retrying clears it; a human on the host must act.

An unreadable ``creek_config.yaml``, a missing or unusable
``credentials.json``, the optional Google libraries not installed, or a scope
list that would widen the grant. Published as ``unavailable`` rather than
``temporarily_unavailable`` precisely so
:data:`~creek_mcp.api.models.RETRY_POLICY` tells the client to stop backing off
and surface the problem.
"""

GRANT_REFUSED_REASON: Final[str] = "drive authorisation could not be completed"
"""The single refusal for every way an exchange can fail.

Unknown state, expired state, already-consumed state, and Google refusing the
code all return this one constant. A caller able to tell them apart would learn
which authorizations this server has outstanding, and — because Google's error
text names the client and the request — would be handed a description of this
deployment's wiring along with it.
"""

STATE_BYTES: Final[int] = 32
"""Entropy of the ``state`` value, in bytes, before url-safe encoding.

256 bits. ``state`` is the only thing binding a relayed code to an
authorization this server issued, so it is sized as a credential rather than as
an identifier.
"""

STATE_TTL_SECONDS: Final[float] = 900.0
"""How long an issued ``state`` remains exchangeable.

Fifteen minutes: long enough for a human to read a consent screen, short enough
that an authorization abandoned mid-flow does not stay live for a day. Google's
own authorization codes expire on a comparable scale, so a longer window here
would buy nothing.
"""

_STATE_FILENAME: Final[str] = "gdrive-authorizations.json"
"""The pending-authorization store, beside the Drive ledger and its sync lock."""

_REDIRECT_URI_KEY: Final[str] = "redirect_uri"
"""Store key: the URI the eventual exchange must be made against."""

_CODE_VERIFIER_KEY: Final[str] = "code_verifier"
"""Store key: the PKCE verifier whose challenge the authorization URL carries.

Persisted because the two legs of a remote grant are two requests, and the
challenge the first one published can only be answered by the value it was
derived from. It is why the store is written owner-only: on its own it buys
nothing, but paired with an intercepted code it is half of a redemption.
"""

_EXPIRES_AT_KEY: Final[str] = "expires_at"
"""Store key: the wall-clock second after which the entry stops being live."""


def _configured_scopes(drive: GoogleDriveConfig) -> list[str]:
    """Return the scope list this grant will request.

    A seam with one job: give the read-only guard below a single place to read
    from, so a test can hand the grant a widened list the way a bug would —
    without going through the pydantic validator that would have caught it.

    Args:
        drive: The validated Drive configuration.

    Returns:
        The configured scopes.
    """
    return list(drive.scopes)


def _readonly_scopes(drive: GoogleDriveConfig) -> list[str] | None:
    """Return the scopes to request, or ``None`` when they are not read-only.

    :meth:`creek.config.GoogleDriveConfig.validate_readonly_scopes` guards the
    *config* path. A remote connect button is the one place a write scope could
    reach an authorization URL without a config edit, so the same validator is
    re-run here on the list about to be sent rather than trusted to have run
    upstream. One call; the failure is a refusal, never a widened grant.

    Args:
        drive: The validated Drive configuration.

    Returns:
        The scope list, or ``None`` when anything in it is not read-only.
    """
    scopes = _configured_scopes(drive)
    try:
        return list(GoogleDriveConfig.validate_readonly_scopes(scopes))
    except ValueError:
        logger.warning(
            "Drive authorisation refused: configured scopes are not readonly"
        )
        return None


# --------------------------------------------------------------------------- #
# The pending-authorization store
# --------------------------------------------------------------------------- #


def _store_path(vault_path: Path) -> Path:
    """Return the pending-authorization store for *vault_path*.

    Beside the Drive ledger and the #1590 sync lock, so everything the
    connector keeps about one vault lives in one directory.

    Args:
        vault_path: Vault root.

    Returns:
        The store path. It need not exist yet.
    """
    ledger = SourceLedger.path_for(vault_path, DRIVE_LEDGER_SOURCE)
    return ledger.parent / _STATE_FILENAME


def _lock_path(vault_path: Path) -> Path:
    """Return the lock serialising store reads and writes.

    Args:
        vault_path: Vault root.

    Returns:
        The lock file path.
    """
    return _store_path(vault_path).with_suffix(".lock")


def _expiry_of(entry: object) -> float | None:
    """Return when *entry* stops being live, or ``None`` if it never was.

    Args:
        entry: One decoded store entry, of whatever shape the file held.

    Returns:
        The expiry as a float, or ``None`` when the entry is not a mapping or
        its ``expires_at`` is not a number. ``None`` drops the entry rather
        than raising: a value this module's writer never produces cannot be
        trusted to say when an authorization stops being exchangeable, and
        letting the conversion raise would turn one hand-edited line into a
        connector that can never be authorised again — the very outcome
        :func:`_read_store` reads defensively to avoid.
    """
    if not isinstance(entry, dict):
        return None
    try:
        return float(entry.get(_EXPIRES_AT_KEY, 0.0))
    except (TypeError, ValueError):
        return None


def _read_store(path: Path) -> dict[str, dict[str, Any]]:
    """Return the live entries in the store, dropping every expired one.

    Pruning on read rather than on a timer is what keeps the file from growing
    without bound when authorizations are begun and abandoned: every access
    forgets what has aged out, so the store's size tracks live flows only.

    Args:
        path: The store path.

    Returns:
        ``state -> {"redirect_uri", "code_verifier", "expires_at"}`` for
        unexpired entries. A missing, unreadable or malformed store reads as
        empty — the worst that costs is one refused exchange, whereas raising
        would turn a corrupt file into a permanently unauthorisable connector.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    live: dict[str, dict[str, Any]] = {}
    for state, entry in raw.items():
        expiry = _expiry_of(entry)
        if expiry is not None and expiry > now:
            live[str(state)] = entry
    return live


def _write_store(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Replace the store with *entries*, owner-only.

    ``state`` is credential-adjacent — it is what binds a relayed code to an
    authorization — so the file is written ``0o600`` from byte zero and moved
    into place, the same way
    :func:`creek.ingest.gdrive._write_token_file` writes the token.

    Args:
        path: The store path.
        entries: The entries to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    handle = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(entries, stream)
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _issue_state(vault_path: Path, redirect_uri: str, code_verifier: str) -> str:
    """Mint, persist and return one single-use ``state``.

    Args:
        vault_path: Vault root.
        redirect_uri: The URI the eventual exchange must be made against.
        code_verifier: The PKCE verifier the authorization URL's challenge is
            derived from, which the exchange leg has to present back.

    Returns:
        The issued state.

    Raises:
        VaultLockTimeoutError: When another authorization holds the store lock.
    """
    state = secrets.token_urlsafe(STATE_BYTES)
    path = _store_path(vault_path)
    with vault_lock(_lock_path(vault_path), timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
        entries = _read_store(path)
        entries[state] = {
            _REDIRECT_URI_KEY: redirect_uri,
            _CODE_VERIFIER_KEY: code_verifier,
            _EXPIRES_AT_KEY: time.time() + STATE_TTL_SECONDS,
        }
        _write_store(path, entries)
    return state


def _consume_state(vault_path: Path, state: str) -> tuple[str, str] | None:
    """Return what *state* was issued under, and forget it.

    Single-use by construction: the entry is removed inside the same locked
    window it is read in, so two concurrent exchanges of one state cannot both
    see it.

    Args:
        vault_path: Vault root.
        state: The state the caller presented.

    Returns:
        The stored ``(redirect_uri, code_verifier)`` pair, or ``None`` when the
        state is unknown, expired, already consumed, or stored without both
        halves — conditions the caller must not be able to tell apart, which is
        why they share a return value.

    Raises:
        VaultLockTimeoutError: When another exchange holds the store lock.
    """
    path = _store_path(vault_path)
    with vault_lock(_lock_path(vault_path), timeout=DEFAULT_LOCK_TIMEOUT_SECONDS):
        entries = _read_store(path)
        entry = entries.pop(state, None)
        if entry is None:
            return None
        _write_store(path, entries)
    redirect_uri = entry.get(_REDIRECT_URI_KEY)
    code_verifier = entry.get(_CODE_VERIFIER_KEY)
    if not isinstance(redirect_uri, str) or not isinstance(code_verifier, str):
        return None
    return redirect_uri, code_verifier


# --------------------------------------------------------------------------- #
# The two verbs
# --------------------------------------------------------------------------- #


def _unavailable(tool: str, ceiling: TierCeiling) -> dict[str, Any]:
    """Return the operator-actionable refusal.

    Args:
        tool: The tool refusing.
        ceiling: The caller's admitted ceiling.

    Returns:
        The structured refusal.
    """
    return dict(
        refusal_response(tool=tool, ceiling=ceiling, reason=GRANT_UNAVAILABLE_REASON)
    )


def _refused(tool: str, ceiling: TierCeiling) -> dict[str, Any]:
    """Return the one constant exchange refusal.

    Args:
        tool: The tool refusing.
        ceiling: The caller's admitted ceiling.

    Returns:
        The structured refusal.
    """
    return dict(
        refusal_response(tool=tool, ceiling=ceiling, reason=GRANT_REFUSED_REASON)
    )


def drive_authorize_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling,
    consumer: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Begin an authorization and return the URL the caller sends its user to.

    Makes no network call: building an authorization URL is a pure local
    computation over the client description and the scope list. The credential
    is not touched, and the connector's state is neither read nor reported —
    which is what keeps this verb from becoming an oracle on whether Drive is
    already connected.

    Args:
        vault_path: Vault root, for the configuration, the store and the audit
            trail.
        privacy_tier_ceiling: The caller's admitted ceiling.
        consumer: The authenticated consumer id.
        redirect_uri: The **caller's own** redirect URI, already validated by
            the wire model. Never a URI on this server.

    Returns:
        ``{"status": "ok", "authorization_url": ..., "state": ...}``, or a
        structured refusal.
    """
    config = _loaded_config(vault_path)
    if config is None:
        return _unavailable(AUTHORIZE_TOOL_NAME, privacy_tier_ceiling)
    scopes = _readonly_scopes(config.google_drive)
    if scopes is None:
        return _unavailable(AUTHORIZE_TOOL_NAME, privacy_tier_ceiling)
    code_verifier = new_code_verifier()
    try:
        state = _issue_state(vault_path, redirect_uri, code_verifier)
    except VaultLockTimeoutError:
        logger.info("Drive authorisation refused: the authorization store is busy")
        return _unavailable(AUTHORIZE_TOOL_NAME, privacy_tier_ceiling)
    try:
        flow = build_flow(
            credentials_file=config.google_drive.credentials_file,
            scopes=scopes,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        url = authorization_url(flow, state=state)
    except (DriveGrantError, GoogleApiUnavailableError) as exc:
        # The state stays in the store and ages out on its own. Unwinding it
        # here would be a second write for no gain: an unissued URL means no
        # code can ever be presented against it.
        logger.warning("Drive authorisation could not be begun: %s", exc)
        return _unavailable(AUTHORIZE_TOOL_NAME, privacy_tier_ceiling)
    _audit(
        tool=AUTHORIZE_TOOL_NAME,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
        consumer=consumer,
        args={"authorization": "begun"},
    )
    return {
        "status": "ok",
        "tier_ceiling": privacy_tier_ceiling.value,
        "authorization_url": url,
        "state": state,
    }


def drive_authorization_exchange_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling,
    consumer: str,
    state: str,
    code: str,
) -> dict[str, Any]:
    """Complete an authorization: exchange *code* and cache the credential.

    The state is consumed **before** the exchange is attempted, so a code that
    Google refuses cannot be retried against the same authorization. That costs
    the caller one extra round trip in the failure case and removes the retry
    loop an attacker would need to brute-force a code.

    Args:
        vault_path: Vault root.
        privacy_tier_ceiling: The caller's admitted ceiling.
        consumer: The authenticated consumer id.
        state: The state the authorization was issued under.
        code: The authorization code the caller relayed.

    Returns:
        ``{"status": "ok", "connection": "connected", ...}``, or a structured
        refusal. The success payload is the connector-status shape, so a client
        learns the outcome only as connector state.
    """
    config = _loaded_config(vault_path)
    if config is None:
        return _unavailable(EXCHANGE_TOOL_NAME, privacy_tier_ceiling)
    scopes = _readonly_scopes(config.google_drive)
    if scopes is None:
        return _unavailable(EXCHANGE_TOOL_NAME, privacy_tier_ceiling)
    try:
        issued = _consume_state(vault_path, state)
    except VaultLockTimeoutError:
        logger.info("Drive authorisation refused: the authorization store is busy")
        return _refused(EXCHANGE_TOOL_NAME, privacy_tier_ceiling)
    if issued is None:
        return _refused(EXCHANGE_TOOL_NAME, privacy_tier_ceiling)
    redirect_uri, code_verifier = issued
    serialised = _exchanged_credential(
        credentials_file=config.google_drive.credentials_file,
        scopes=scopes,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        code=code,
    )
    if serialised is None:
        return _refused(EXCHANGE_TOOL_NAME, privacy_tier_ceiling)
    _write_token_file(Path(config.google_drive.token_file), serialised)
    _audit(
        tool=EXCHANGE_TOOL_NAME,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
        consumer=consumer,
        args={"connection": STATE_CONNECTED},
    )
    return {
        "status": "ok",
        "tier_ceiling": privacy_tier_ceiling.value,
        "connection": STATE_CONNECTED,
        "scopes": scopes,
        "can_sync": True,
    }


def _exchanged_credential(
    *,
    credentials_file: str,
    scopes: list[str],
    redirect_uri: str,
    code_verifier: str,
    code: str,
) -> str | None:
    """Return the serialised credential *code* buys, or ``None`` on any failure.

    Split out so :func:`drive_authorization_exchange_tool` reads as its own
    decision table rather than as a nest of ``try`` blocks, and so the one
    place an OAuth exception can surface is one function wide.

    Args:
        credentials_file: Path to the web client secrets.
        scopes: The validated read-only scope list.
        redirect_uri: The URI the authorization was issued under.
        code_verifier: The PKCE verifier the authorization was issued under.
            Google checks it against the challenge the URL leg published, so a
            flow built without it is refused every time.
        code: The authorization code.

    Returns:
        The serialised credential, or ``None``. Nothing about *why* it is
        ``None`` reaches the caller; the reason goes to the server log, where
        the operator is.
    """
    try:
        flow = build_flow(
            credentials_file=credentials_file,
            scopes=scopes,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        return exchange_code(flow, code=code)
    except (DriveGrantError, GoogleApiUnavailableError) as exc:
        logger.warning("Drive authorisation exchange failed: %s", exc)
        return None
