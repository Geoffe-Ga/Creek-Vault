"""``creek.drive.*`` — the read-only Google Drive connector, over the wire (#1527).

The Drive connector has worked since long before this module: an OAuth2 flow
with a cached token (:class:`creek.ingest.gdrive.GoogleApiDriveClient`), an
incremental mirror keyed on Drive's ``modified_time``
(:class:`~creek.ingest.gdrive.GoogleDriveDownloader`), a read-only doctor
(:func:`~creek.ingest.gdrive.check_drive`) and a revoke-and-erase path
(:func:`~creek.ingest.gdrive.revoke_token`). What it never had was a door: all
five were reachable only from ``creek gdrive`` on the host. This module is that
door, and nothing else — every behaviour below is the existing one, delegated
to.

**Not MCP tools, despite the name and the neighbours.** ``creek.drive.status``,
``creek.drive.sync`` and ``creek.drive.disconnect`` are *audit* names: nothing
here is registered on the MCP server, because the agent surface already reaches
Drive the way an operator does. They live in this package, and are spelled in
this vocabulary, so that the audit trail reads uniformly and so that a later
MCP registration is a wiring change rather than a rewrite — the shape is
already the one every other tool here has.

**The credential stays here.** Three design points follow from that and are
worth stating before the code:

1. *The token is server-side, and no route accepts one.* The alternative —
   a caller supplying a token per request — was rejected: it would put a
   long-lived Drive credential into every request body, into the client's own
   storage, and within reach of every logging and tracing layer between the
   two, in exchange for nothing the server needs. ``/v1`` is already
   bearer-authenticated, so the consumer proves who it is with its own
   credential; a second one buys no authorisation this surface does not
   already have.
2. *No route begins, completes or carries an OAuth flow.* The cached
   credential is minted by an installed-app loopback flow, which needs a
   browser on the machine holding the client secret. Authorisation therefore
   stays a local operator action (``creek gdrive --download``), and
   :func:`drive_sync_tool` **refuses** rather than falling through to that
   flow — a network-triggered sync that opened a browser on the server, or
   blocked forever waiting for one nobody is standing at, is the worst
   available answer.
3. *Nothing the tools return is derived from the credential.* The status
   payload carries a state word, the granted scope set and a boolean; the sync
   payload carries seven integers; the disconnect payload carries a state word
   and a boolean. No token, no refresh token, no client secret, no path to any
   of them, and — because :func:`creek_mcp.audit.summarise_args` passes short
   strings through verbatim — nothing credential-derived is ever handed to the
   audit log either.

**What a sync ingests, and at what tier.** Every downloaded file goes through
:func:`creek.ingest.route_to_ingestor` and the ordinary ledger-backed
:func:`creek.ingest.pipeline.run_ingest`, one file at a time, exactly as
``creek ingest`` would. In particular ``privacy_tier`` is **not** passed:
the caller declares no tier here and has no standing to, because the content is
the vault owner's rather than the caller's. The tier is therefore whatever the
content and the classifier derive, which is the only arrangement under which
this path cannot make a tier less restrictive than the ordinary one would.

One file at a time is also load-bearing rather than stylistic. A *directory*
input is what makes :func:`~creek.ingest.pipeline.tomb_missing_units`
computable at all, and over a partial Drive listing a gone set is not a gone
set: every fragment the pass did not happen to see would look deleted. (This
note used to attribute the hazard to a forced ``ledger_source``. That is now
backwards — :func:`~creek.ingest.pipeline.tombing_is_authorised` requires
``ledger_source is None``, so a borrowed ledger *disarms* the sweep. The
single-file input is what this path actually relies on.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from yaml import YAMLError

from creek._fslock import VaultLockTimeoutError, vault_lock
from creek.config import load_vault_config
from creek.ingest import INGESTOR_REGISTRY, UnsupportedSourceError, route_to_ingestor
from creek.ingest.gdrive import (
    GoogleApiDriveClient,
    GoogleDriveDownloader,
    inspect_token,
    revoke_token,
)
from creek.ingest.ledger import SourceLedger
from creek.ingest.pipeline import run_ingest
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from creek.config import CreekConfig, GoogleDriveConfig
    from creek.ingest.gdrive import DriveClient, TokenInspection

logger = logging.getLogger(__name__)

STATUS_TOOL_NAME: Final[str] = "creek.drive.status"
"""Audit name of the connection-state read."""

SYNC_TOOL_NAME: Final[str] = "creek.drive.sync"
"""Audit name of the incremental sync."""

DISCONNECT_TOOL_NAME: Final[str] = "creek.drive.disconnect"
"""Audit name of the revoke-and-erase verb."""

_SYNC_LOCK_TIMEOUT_SECONDS: Final[float] = 10.0
"""How long a queued sync waits for the running one before refusing.

Comfortably under the ``/v1`` request timeout
(:data:`~creek_mcp.httpapi.middleware.limits.DEFAULT_TIMEOUT_SECONDS`, 30 s)
so a caller that loses the race is answered with :data:`SYNC_BUSY_REASON` —
which names what happened and is safe to retry — rather than with the
middleware's generic timeout, which names nothing.
"""

DRIVE_LEDGER_SOURCE: Final[str] = "gdrive"
"""The ingest ledger a synced Drive file is recorded under.

Its own name rather than ``upload``'s, so a right-to-be-forgotten sweep, a
re-sync and an ordinary upload of the same document cannot collide on one
ledger key — and so an operator reading the ledger can tell which door a
fragment came through.
"""

STATE_CONNECTED: Final[str] = "connected"
"""A usable cached credential: a sync will run with nobody present."""

STATE_NOT_CONNECTED: Final[str] = "not_connected"
"""No cached credential at all; the operator must authorise on the host."""

STATE_EXPIRED: Final[str] = "expired"
"""A credential that has lapsed and cannot be refreshed without a human."""

STATE_UNSUPPORTED: Final[str] = "unsupported"
"""The optional Google client libraries are not installed on this server."""

CONNECTION_STATES: Final[frozenset[str]] = frozenset(
    {STATE_CONNECTED, STATE_NOT_CONNECTED, STATE_EXPIRED, STATE_UNSUPPORTED}
)
"""Every state :func:`connection_state` can return.

Published so ``tests/test_v1_api_drive.py`` can pin it equal to
:class:`creek_mcp.api.models.DriveConnectionState`'s values. The two
vocabularies are declared separately — this module is the MCP side and must not
import the wire models, the same separation ``creek.upload`` keeps from
:class:`~creek_mcp.api.models.JournalAction` — so the guard against them
drifting has to be a test rather than an import.
"""

CONFIG_UNAVAILABLE_REASON: Final[str] = "drive configuration unavailable"
"""``creek_config.yaml`` could not be read or did not validate."""

NOT_CONNECTED_REASON: Final[str] = "drive connector is not connected"
"""The one refusal for every unusable connection state, deliberately.

``not_connected``, ``expired`` and ``unsupported`` collapse to a single reason
here so a *refusal* cannot be used to distinguish them. The distinction is real
and a client needs it, which is why :func:`drive_status_tool` publishes it
outright — a negotiated disclosure on a route whose whole purpose is to make
it, rather than a fact leaking out of the side of a verb that failed.
"""

SYNC_FAILED_REASON: Final[str] = "drive sync could not be completed"
"""A listing or download that raised, stated without naming what raised.

Deliberately carries no exception text and no interpolation. Drive's own
errors embed the request URI — file id and query parameters — and
:attr:`creek.ingest.gdrive.DownloadResult.failure_lines` lead with the file
*name*; both are the vault owner's content, and neither may cross to a remote
caller. The detail stays in the server log, where the operator is.
"""

VAULT_UNAVAILABLE_REASON: Final[str] = "vault unavailable"
"""The vault disappeared under the ingest. Transient, exactly as on upload."""

SYNC_BUSY_REASON: Final[str] = "another drive sync is already running"
"""A second sync arrived while the first still held the vault's Drive lock.

Transient by construction — the holder finishes — so the published code is
``temporarily_unavailable`` and the client's ordinary backoff clears it. Said
plainly rather than folded into :data:`SYNC_FAILED_REASON`, because "wait and
retry" and "the Drive read blew up" are different things for a caller to do,
and because a queued sync did nothing wrong.

Carries no path, no file name and no consumer id: like every reason here it
is a constant, not a composition.
"""

ERASE_FAILED_REASON: Final[str] = "cached credential could not be erased"
"""The local token file survived :func:`~creek.ingest.gdrive.revoke_token`.

Reported as a refusal rather than as a success carrying a flag, because a
disconnect that answers ``200`` while the credential is still on disk is the
one outcome a caller must never read as done.
"""


def build_drive_client(config: GoogleDriveConfig) -> DriveClient:
    """Return the Drive backend these tools run against.

    The single construction site, and therefore the single seam: the tools
    below take no ``client`` argument, so there is no alternative path a test
    could exercise instead of the production one. A test substitutes this
    function; everything downstream of it is the real code.

    Args:
        config: The read-only-validated Drive configuration.

    Returns:
        A :class:`~creek.ingest.gdrive.DriveClient` over the Google API.
    """
    return GoogleApiDriveClient(config)


def connection_state(token: TokenInspection, *, libs_available: bool) -> str:
    """Return which of :data:`CONNECTION_STATES` describes the connector.

    Ordered so the *unfixable-by-authorising* condition is reported first: with
    the optional libraries absent no credential could be used however fresh it
    is, and a client told ``not_connected`` would send its user round an
    authorisation loop that cannot terminate.

    A present-but-lapsed credential still reports ``connected`` when it carries
    a refresh token, because google-auth renews one of those without a human —
    which is the only question this state is really answering.

    Args:
        token: The local, secret-free inspection of the cached token file.
        libs_available: Whether the optional Google libraries import here.

    Returns:
        One of :data:`CONNECTION_STATES`.
    """
    if not libs_available:
        return STATE_UNSUPPORTED
    if not token.present:
        return STATE_NOT_CONNECTED
    if token.valid is True or token.refreshable:
        return STATE_CONNECTED
    return STATE_EXPIRED


def _loaded_config(vault_path: Path) -> CreekConfig | None:
    """Return *vault_path*'s Creek configuration, or ``None`` when unreadable.

    Until #1409 this resolved from the process's working directory, on the
    stated grounds that it matched how ``creek gdrive`` resolves. The parallel
    did not hold: the CLI's ``gdrive`` command declares no ``--vault`` flag and
    so is cwd-scoped by construction, whereas all three MCP drive tools take
    ``vault_path`` as a parameter. A remote caller naming vault A was therefore
    driving the Google Drive connector — staging directory included — that
    whichever vault the server was started in had declared.

    Args:
        vault_path: The vault the calling tool was asked to act on.

    Returns:
        The validated configuration, or ``None`` when it cannot be read. The
        exception groups are the three
        :data:`creek_mcp.httpapi.vault.UNREADABLE_CONFIG` names, restated
        rather than imported because this module is the MCP side and must not
        depend on the HTTP adapter.
    """
    try:
        return load_vault_config(vault_path)
    except (OSError, ValueError, YAMLError):
        return None


def _audit(
    *,
    tool: str,
    vault_path: Path,
    ceiling: TierCeiling,
    consumer: str,
    args: dict[str, Any],
) -> None:
    """Record one connector call in the vault's MCP audit trail.

    Args:
        tool: The dot-namespaced tool name.
        vault_path: Vault root holding the audit log.
        ceiling: The caller's admitted ceiling.
        consumer: The authenticated consumer id.
        args: The argument summary. **Never credential-derived**: the tools
            below pass state words and integers only, and
            :func:`creek_mcp.audit.summarise_args` copies a short string
            through verbatim, so anything token-shaped handed here would be
            persisted in the clear.
    """
    MCPAuditLog(vault_path).append(
        tool=tool,
        args=args,
        tier_ceiling=ceiling,
        consumer=consumer,
    )


def drive_status_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling,
    consumer: str,
) -> dict[str, Any]:
    """Report the connector's state, its granted scopes, and whether it can sync.

    Reads the cached token file only through
    :func:`creek.ingest.gdrive.inspect_token`, which by construction returns
    presence, expiry and refreshability and never the token itself. No network
    call is made: this is a local question about this server's configuration.

    Args:
        vault_path: Vault root, for the audit trail.
        privacy_tier_ceiling: The caller's admitted ceiling.
        consumer: The authenticated consumer id.

    Returns:
        The state payload, or a structured refusal when the configuration
        cannot be read.
    """
    config = _loaded_config(vault_path)
    if config is None:
        return refusal_response(
            tool=STATUS_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=CONFIG_UNAVAILABLE_REASON,
        )
    drive = config.google_drive
    state = connection_state(
        inspect_token(Path(drive.token_file)),
        libs_available=build_drive_client(drive).is_available(),
    )
    _audit(
        tool=STATUS_TOOL_NAME,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
        consumer=consumer,
        args={"connection": state},
    )
    return {
        "status": "ok",
        "tier_ceiling": privacy_tier_ceiling.value,
        "connection": state,
        "scopes": list(drive.scopes),
        "can_sync": state == STATE_CONNECTED,
    }


def _ingest_downloaded(
    downloaded: tuple[Path, ...],
    vault_path: Path,
) -> dict[str, int] | None:
    """Ingest each freshly downloaded file, one at a time.

    ``privacy_tier`` is deliberately not passed: see this module's docstring.
    ``ledger_source`` is, so a re-synced file is recognised as the same unit
    rather than written twice — and the input is always a single *file*, which
    is what keeps the forced ledger from arming the tomb sweep.

    Args:
        downloaded: The paths this run fetched, in listing order.
        vault_path: Vault root.

    Returns:
        The four tallies, or ``None`` when the vault vanished mid-run.
    """
    tally = {"created": 0, "updated": 0, "unchanged": 0, "unsupported": 0, "failed": 0}
    for path in downloaded:
        try:
            source_type = route_to_ingestor(path)
            ingestor_cls = INGESTOR_REGISTRY[source_type]
        except (UnsupportedSourceError, KeyError):
            # Refused with a remedy on the upload route; here there is no
            # caller to hand the remedy to, so it is counted and published as
            # a count. The bytes stay in staging for `creek ingest`.
            #
            # ``KeyError`` joins it rather than propagating: a routing table
            # naming an ingestor the registry does not hold is a Creek bug,
            # but letting it abort the whole run would lose every file after
            # it — and a sync that half-ran and reported nothing is strictly
            # worse than one that reports the file it could not read.
            tally["unsupported"] += 1
            continue
        try:
            result = run_ingest(
                ingestor_cls=ingestor_cls,
                source_type=source_type,
                input_path=path,
                vault_path=vault_path,
                ledger_source=DRIVE_LEDGER_SOURCE,
            )
        except FileNotFoundError:
            return None
        tally["created"] += result.created
        tally["updated"] += result.updated
        tally["unchanged"] += result.unchanged
        # run_ingest reports a per-unit failure in `errors` and never logs it
        # itself. Dropping that list made a file which downloaded cleanly but
        # whose fragment failed to assemble — or whose write raised OSError —
        # land in NONE of the three counts above: a 200 with silently fewer
        # fragments and no signal anywhere. `files_unsupported` does not cover
        # it; that is decided before the ingest even starts.
        #
        # Counted, never quoted: an error string carries the source path, and
        # a Drive file name is user content this response must not echo.
        tally["failed"] += len(result.errors)
        if result.errors:
            logger.warning(
                "%d file(s) downloaded from Drive but did not become "
                "fragments; this sync is INCOMPLETE",
                len(result.errors),
            )
    return tally


def _sync_payload(
    *,
    ceiling: TierCeiling,
    downloaded: int,
    skipped: int,
    failed: int,
    tally: dict[str, int],
) -> dict[str, Any]:
    """Assemble the sync response from the download and ingest tallies.

    Args:
        ceiling: The caller's admitted ceiling.
        downloaded: Files fetched this run.
        skipped: Files the incremental skip passed over.
        failed: Files whose download raised.
        tally: The ingest tallies from :func:`_ingest_downloaded`.

    Returns:
        The eight counts, and nothing derived from a file name or an id.

    ``files_failed`` and ``fragments_failed`` are different shortfalls and are
    published separately: the first is a download that never landed, the second
    a file that landed and then failed to become a fragment. Collapsing them
    would tell an operator a sync was clean when half its content is missing.
    """
    return {
        "status": "ok",
        "tier_ceiling": ceiling.value,
        "files_fetched": downloaded,
        "files_unchanged": skipped,
        "files_failed": failed,
        "files_unsupported": tally["unsupported"],
        "fragments_failed": tally["failed"],
        "fragments_created": tally["created"],
        "fragments_updated": tally["updated"],
        "fragments_unchanged": tally["unchanged"],
    }


def _download(
    client: DriveClient,
    drive: GoogleDriveConfig,
    staging: Path,
) -> tuple[tuple[Path, ...], int, int] | None:
    """Mirror the changed Drive files into *staging*.

    Args:
        client: The Drive backend.
        drive: The read-only Drive configuration.
        staging: Where the mirror lives.

    Returns:
        ``(downloaded paths, skipped count, failed count)``, or ``None`` when
        the run raised. Broad, and for the reason
        :func:`creek.ingest.gdrive.check_drive` documents: the Drive read
        surface spans ``HttpError``, network ``OSError`` and OAuth failures,
        none of them importable at module top level.
    """
    try:
        result = GoogleDriveDownloader(client=client, config=drive).download_all(
            staging
        )
    except Exception:
        logger.exception("Drive sync failed")
        return None
    return result.downloaded, len(result.skipped), len(result.errors)


def _sync_lock_path(vault_path: Path) -> Path:
    """Return the lock file that serialises Drive syncs of *vault_path*.

    Derived from the Drive ingest ledger's own path rather than spelled
    out, so the lock and the state it protects cannot drift apart: it is
    ``00-Creek-Meta/State/ingest/gdrive.lock`` beside
    ``…/gdrive.jsonl``.

    Naming the *connector* rather than the vault is the whole scoping
    decision. A vault-wide lock would make a journal write, an upload and
    a ``creek ingest`` wait on a Drive sync that has nothing to do with
    them; this one is contended only by two syncs of the same Drive into
    the same vault, which have no useful parallelism anyway —
    :meth:`~creek.ingest.gdrive.GoogleDriveDownloader.download_all` is a
    serial loop.

    Args:
        vault_path: Vault root.

    Returns:
        The lock file path. It need not exist yet.
    """
    return SourceLedger.path_for(vault_path, DRIVE_LEDGER_SOURCE).with_suffix(".lock")


def _locked_download_and_ingest(
    *,
    client: DriveClient,
    drive: GoogleDriveConfig,
    staging: Path,
    vault_path: Path,
) -> tuple[tuple[tuple[Path, ...], int, int] | None, dict[str, int] | None]:
    """Mirror Drive and ingest the result as **one** locked window.

    The download and the ingest are held together on purpose. Locking only
    the ingest would still let two runs both pass the downloader's
    incremental mtime check against a staging file neither had written
    yet, and locking only the download would still let two ingests race
    the fragment index. Measured before the lock existed, two overlapping
    syncs of one Drive file produced two notes carrying one fragment id
    and two ledger rows for one ``source_key`` (#1590).

    Args:
        client: The Drive backend.
        drive: The read-only Drive configuration.
        staging: Where the mirror lives.
        vault_path: Vault root — both the ingest target and the lock's home.

    Returns:
        ``(download outcome, ingest tally)``. The first is ``None`` when the
        Drive read raised, in which case no ingest was attempted and the
        second is ``None`` too; the second is ``None`` on its own when the
        vault vanished mid-ingest.

    Raises:
        VaultLockTimeoutError: If another sync of this vault held the lock
            for longer than :data:`_SYNC_LOCK_TIMEOUT_SECONDS`.
    """
    with vault_lock(
        _sync_lock_path(vault_path),
        timeout=_SYNC_LOCK_TIMEOUT_SECONDS,
    ):
        fetched = _download(client, drive, staging)
        if fetched is None:
            return None, None
        return fetched, _ingest_downloaded(fetched[0], vault_path)


def drive_sync_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling,
    consumer: str,
) -> dict[str, Any]:
    """Run one incremental Drive sync and ingest what it fetched.

    Refuses before touching the network unless a usable cached credential is
    already present. That guard is the whole of the "revocation actually
    invalidates" promise: :func:`~creek.ingest.gdrive.revoke_token` erases the
    token file, so the very next sync sees ``not_connected`` and stops — rather
    than reaching :class:`~creek.ingest.gdrive.GoogleApiDriveClient`, which
    would answer a missing token by opening an interactive OAuth flow on the
    *server*.

    Incremental by the mechanism that was already there: the downloader skips
    any file whose local mtime is at least as new as Drive's ``modified_time``,
    and only the files it actually fetched are ingested. A second sync over an
    unchanged Drive therefore fetches nothing and writes nothing — **provided
    it does not overlap the first**, which until #1590 nothing arranged. That
    skip is a check-then-act against a staging file the other run has not
    written yet, so two simultaneous syncs both passed it and both ingested;
    :func:`_locked_download_and_ingest` is what now makes the sentence true
    unconditionally, by serialising the two per vault.

    The lock is POSIX advisory (``fcntl``): on Windows, and on network mounts
    that do not implement ``flock``, it degrades to serialising the threads of
    one process — see :mod:`creek._fslock`. It is scoped to this connector, so
    a journal write, an upload or a ``creek ingest`` never waits on a sync;
    equally, those three still race *each other* through the same
    :func:`~creek.ingest.pipeline.run_ingest` seam, which #1590 does not
    address.

    Args:
        vault_path: Vault root — both the ingest target and the audit trail.
        privacy_tier_ceiling: The caller's admitted ceiling. It governs what
            this call may be *told*, not what it may ingest: the content is
            the vault owner's and its tier is derived from it.
        consumer: The authenticated consumer id.

    Returns:
        The eight counts :func:`_sync_payload` assembles, or a structured
        refusal.
    """
    config = _loaded_config(vault_path)
    if config is None:
        return refusal_response(
            tool=SYNC_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=CONFIG_UNAVAILABLE_REASON,
        )
    drive = config.google_drive
    client = build_drive_client(drive)
    state = connection_state(
        inspect_token(Path(drive.token_file)),
        libs_available=client.is_available(),
    )
    _audit(
        tool=SYNC_TOOL_NAME,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
        consumer=consumer,
        args={"connection": state},
    )
    if state != STATE_CONNECTED:
        return refusal_response(
            tool=SYNC_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=NOT_CONNECTED_REASON,
        )
    try:
        fetched, tally = _locked_download_and_ingest(
            client=client,
            drive=drive,
            staging=config.source_drive / drive.staging_dir,
            vault_path=vault_path,
        )
    except VaultLockTimeoutError:
        # Logged without the vault path: the refusal itself is constant, and
        # the operator-facing detail belongs in the server log, not the wire.
        logger.info("Drive sync refused: another sync of this vault holds the lock")
        return refusal_response(
            tool=SYNC_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=SYNC_BUSY_REASON,
        )
    if fetched is None:
        return refusal_response(
            tool=SYNC_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=SYNC_FAILED_REASON,
        )
    downloaded, skipped, failed = fetched
    if tally is None:
        return refusal_response(
            tool=SYNC_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=VAULT_UNAVAILABLE_REASON,
        )
    return _sync_payload(
        ceiling=privacy_tier_ceiling,
        downloaded=len(downloaded),
        skipped=skipped,
        failed=failed,
        tally=tally,
    )


def drive_disconnect_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling,
    consumer: str,
) -> dict[str, Any]:
    """Revoke the cached credential and erase it from disk.

    Delegates wholly to :func:`creek.ingest.gdrive.revoke_token`, which posts
    the refresh token to Google's revocation endpoint and then overwrites and
    unlinks the local file. That post is the one journey the credential makes,
    and it is back to its issuer; nothing about it reaches this function's
    return value beyond the boolean Google answered with.

    Idempotent: disconnecting an already-disconnected connector is a ``200``
    reporting the same state, because "was there a credential a moment ago" is
    a question about the past that this verb has no reason to answer.

    Args:
        vault_path: Vault root, for the audit trail.
        privacy_tier_ceiling: The caller's admitted ceiling.
        consumer: The authenticated consumer id.

    Returns:
        The disconnect payload, or a structured refusal when the configuration
        cannot be read or the local credential survived the erase.
    """
    config = _loaded_config(vault_path)
    if config is None:
        return refusal_response(
            tool=DISCONNECT_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=CONFIG_UNAVAILABLE_REASON,
        )
    outcome = revoke_token(config.google_drive)
    _audit(
        tool=DISCONNECT_TOOL_NAME,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
        consumer=consumer,
        args={"remote_revoked": outcome.remote_revoked},
    )
    if outcome.token_file_existed and not outcome.token_file_removed:
        return refusal_response(
            tool=DISCONNECT_TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=ERASE_FAILED_REASON,
        )
    return {
        "status": "ok",
        "tier_ceiling": privacy_tier_ceiling.value,
        "connection": STATE_NOT_CONNECTED,
        "remote_revoked": outcome.remote_revoked,
    }


__all__ = [
    "CONNECTION_STATES",
    "DRIVE_LEDGER_SOURCE",
    "build_drive_client",
    "connection_state",
    "drive_disconnect_tool",
    "drive_status_tool",
    "drive_sync_tool",
]
