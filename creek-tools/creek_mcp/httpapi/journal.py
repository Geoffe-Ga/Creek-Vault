"""``PUT /v1/journal-entries/{external_id}`` — parse, delegate, project (#1075).

The first tracer stub replaced by real behaviour, and the shape every later
vertical copies: this module validates what arrived, hands it to the **existing**
:func:`creek_mcp.tools.journal.journal_ingest_tool`, and projects that tool's
return onto the published wire model. It contains no idempotency logic, no
privacy logic and no audit call of its own.

That is not a style preference. ``journal_ingest_tool`` refuses an entry whose
tier exceeds the caller's ceiling *before* staging it, refuses an update that
would destroy a fragment the caller could not have read (#970), and audits both
the refusal and the success. A route that reached ``run_ingest`` directly, or
that passed a ceiling the adapter policy had not admitted, would lose every one
of those guarantees while still answering plausibly — which is exactly the
divergence epic #1071 exists to prevent.

**What this module *does* own is the path segment.** ``external_id`` arrives as
URL text rather than as a validated field, and
:func:`creek_mcp.staged_names.safe_stem` accepts literally any string: it would
happily mint a stable staged name for a 20 KB id, or for one whose bytes did not
survive URL decoding. Either mints a fragment under a key the client can never
address again, so :func:`admissible_external_id` refuses them here, above the
tool, before anything is written.

**And the refusal projection.** The tool answers in its own structured
vocabulary — ``{"status": "refused", ..., "reason": ...}`` — which #1072 does not
publish. :func:`journal_refusal_code` is the single translation into the wire
taxonomy, and it is deliberately total: an unrecognised reason becomes
``internal_error`` rather than a plausible refusal, because a reason this adapter
does not understand is a reason it must not narrate. ``ingest failed: …`` in
particular can carry a staged file path, and :func:`error_response` renders a
constant message per code, so nothing of it reaches the wire.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import frontmatter
from pydantic import ValidationError

from creek._fsio import atomic_write_text
from creek._fslock import VaultLockTimeoutError, vault_lock
from creek.audit import AuditLog
from creek.ingest.ledger import forget_fragment_ids, ledger_dir
from creek.ingest.pipeline import derive_source_key, ledger_for_source
from creek.link.embeddings import (
    cache_contains_fragment_ids,
    embeddings_cache_path,
    purge_fragment_ids_from_cache,
)
from creek.purge.engine import PurgeEngine
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS
from creek.vault.writer import INDEX_FILENAME, PROVENANCE_FILENAME, VaultWriter
from creek_mcp.api.models import (
    MAX_EXTERNAL_ID_CHARS,
    OK_STATUS,
    ErrorCode,
    JournalAction,
    JournalUpsertRequest,
    JournalUpsertResponse,
    JournalWithdrawResponse,
    WireTierCeiling,
)
from creek_mcp.api.routes import CEILING_HEADER
from creek_mcp.audit import MCPAuditLog
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import write_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.tier_ceiling import TIER_REQUIRED_REASON
from creek_mcp.tools.journal import (
    journal_ingest_tool,
    journal_mutation_lock_path,
    journal_staged_path,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

REPLACEMENT_CHARACTER: Final[str] = "�"
"""U+FFFD, the marker that a path segment did not survive URL decoding.

Its presence means the client's bytes and the server's string already differ, so
the id the client would address the entry by is not the id the entry was written
under. That is an idempotency key that silently does not work, which is worse
than a refusal.
"""

_BLANK_CALL_REASON: Final[str] = "content and external_id are required"
"""``_validated_entry_tier``'s malformed-call refusal, verbatim."""

_UNKNOWN_TIER_PREFIX: Final[str] = "unknown tier "
"""``_validated_entry_tier``'s unparseable-tier refusal, by prefix."""

_ABOVE_CEILING_SUFFIX: Final[str] = " exceeds the ceiling"
"""Gate 1's refusal (``entry tier <tier> exceeds the ceiling``), by suffix."""

_VAULT_UNAVAILABLE_REASON: Final[str] = "vault unavailable"
"""The refusal a missing vault earns from the ingest runner, verbatim.

Transient rather than terminal: the vault directory reappearing is exactly the
kind of thing that clears on its own, which is why it maps to
``temporarily_unavailable`` and not to ``unavailable``.
"""

_MUTATION_BUSY_REASON: Final[str] = "journal mutation busy"
"""The shared upsert/withdrawal lock could not be acquired in time."""

_WITHDRAW_AUDIT_TOOL: Final[str] = "creek.journal.withdraw"
"""Body-free audit event for one withdrawal attempt."""

_WITHDRAW_PENDING_RELDIR: Final[Path] = Path(
    "00-Creek-Meta/adepthood/journal-withdrawals"
)
"""Crash-recovery records keyed by a digest, never a concrete external id."""

_SOURCE_TYPE: Final[str] = "markdown"
"""The ingest ledger journal entries use."""


def journal_refusal_code(reason: str) -> ErrorCode:
    """Return the wire code for one of the journal tool's refusal reasons.

    Total by construction. Two reasons share the ``internal_error`` fallthrough
    and neither gets a branch of its own, because a branch that returned what
    the default already returns is a branch nothing can go wrong in:

    * ``ingest failed: <message>`` — the entry was staged and tier-allowed and
      the write failed anyway. The message can name a staged file path, which is
      why this must not reach a body that echoes anything; it does not, because
      :func:`~creek_mcp.httpapi.errors.error_response` renders one constant per
      code.
    * anything this adapter does not recognise. Failing closed to
      ``internal_error`` rather than to ``privacy_refused`` matters: a refusal
      this adapter cannot classify is not a privacy decision it may claim to
      have made on the caller's behalf, and ``internal_error`` is the one code
      that asserts nothing about the vault.

    The four matched reasons are spelled as constants above rather than imported
    because :func:`creek_mcp.tools.journal.journal_ingest_tool` composes three of
    them inline as f-strings, so there is nothing to import. Each live branch is
    pinned by a behavioural test that drives the real tool through the route, so
    a reworded reason surfaces as a wrong status code rather than as silence.

    :data:`~creek_mcp.tier_ceiling.TIER_REQUIRED_REASON` is the exception on
    both counts. It *is* imported — it is one shared literal with a name of its
    own, read by all three write verbs — and it is pinned by a direct unit test
    on this function rather than through the route, because the route cannot
    produce it today: :class:`~creek_mcp.api.models.JournalUpsertRequest`'s
    ``tier`` (``creek_mcp/api/models.py:614``) has no default and is typed
    :class:`~creek_mcp.api.models.WireTierCeiling`, so a body omitting it fails
    schema validation and never reaches the tool. The mapping is therefore
    defence in depth: this function is total and fails closed to
    ``internal_error``, so the day that reason does reach it — a new caller
    path, a relaxed field — it is published as the caller error it is rather
    than as a server fault (#1494).

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The published :class:`~creek_mcp.api.models.ErrorCode`.
    """
    if reason in (_BLANK_CALL_REASON, TIER_REQUIRED_REASON) or reason.startswith(
        _UNKNOWN_TIER_PREFIX
    ):
        return ErrorCode.INVALID_REQUEST
    if reason == GENERIC_ABOVE_CEILING_REASON or reason.endswith(_ABOVE_CEILING_SUFFIX):
        return ErrorCode.PRIVACY_REFUSED
    if reason == _VAULT_UNAVAILABLE_REASON:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    if reason == _MUTATION_BUSY_REASON:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


def admissible_external_id(raw: str) -> bool:
    """Return whether *raw* can serve as an idempotency key.

    Args:
        raw: The decoded ``{external_id}`` path segment.

    Returns:
        ``True`` when the id is non-blank, within
        :data:`creek_mcp.api.models.MAX_EXTERNAL_ID_CHARS` — the one bound both
        write surfaces share — free of the replacement character, and made
        entirely of printable characters: a control byte would reach both the
        staged frontmatter and the audit trail.
    """
    if not raw.strip() or len(raw) > MAX_EXTERNAL_ID_CHARS:
        return False
    if REPLACEMENT_CHARACTER in raw:
        return False
    return raw.isprintable()


async def _parsed_body(request: Request) -> JournalUpsertRequest | None:
    """Return the validated request body, or ``None`` when it does not validate.

    Args:
        request: The request in flight.

    Returns:
        The parsed model, or ``None``. The caller renders one
        ``invalid_request`` for both an undecodable body and a schema failure:
        a caller able to tell those apart learns which half of its request the
        server got far enough to look at.
    """
    try:
        raw = await request.json()
        return JournalUpsertRequest.model_validate(raw)
    except (ValidationError, ValueError, UnicodeDecodeError):
        return None


def _upsert(
    request: Request,
    external_id: str,
    parsed: JournalUpsertRequest,
    context: RequestContext,
) -> dict[str, Any] | None:
    """Resolve the vault and run the journal tool, both off the event loop.

    Every blocking call the route makes is reachable from here and nowhere
    else — the config read and YAML parse behind
    :func:`~creek_mcp.httpapi.vault.configured_vault`, the staging write, the
    ledger read, the whole ingest run and an audit append that holds an
    ``fcntl`` lock across an ``fsync`` — so one caller's slow write cannot
    stall every other connection this process is serving.

    Resolution belongs *inside* this seam rather than at the call site because
    the app is built without a ``vault_path`` in the production entry point, so
    ``configured_vault`` reads and parses ``creek_config.yaml`` on every
    request. Hoisting only the tool would leave that file read on the loop —
    the same narrowed hoist :mod:`creek_mcp.httpapi.capabilities` documents and
    avoids.

    Args:
        request: The request in flight, which names the vault to resolve.
        external_id: The already-validated path segment.
        parsed: The validated request body.
        context: The request's context, supplying the *admitted* ceiling and the
            authenticated consumer. Both come from the context rather than from
            anything the handler re-derives: the ceiling was decided once, at
            the adapter edge, and a second derivation here would be a second
            gate.

    Returns:
        The tool's return dict, success or refusal — or ``None`` when there is
        no readable vault to run against, which the caller renders as the
        ``unavailable`` refusal.
    """
    vault = configured_vault(request)
    if vault is None:
        return None
    return journal_ingest_tool(
        vault_path=vault,
        content=parsed.content,
        external_id=external_id,
        timestamp=parsed.timestamp,
        tier=parsed.tier.value,
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
        storage_scope=context.consumer,
        allow_legacy=len(request.app.state.consumer_ids) == 1,
    )


def _render(result: dict[str, Any], context: RequestContext) -> Response:
    """Project the tool's return onto the published response, or a refusal.

    Args:
        result: The tool's return dict.
        context: The request's context.

    Returns:
        The ``200`` carrying :class:`~creek_mcp.api.models.JournalUpsertResponse`,
        or the published refusal for the tool's reason, or ``internal_error``
        when the entry was written but cannot be honestly described.
    """
    if result.get("status") != OK_STATUS:
        reason = str(result.get("reason", ""))
        return error_response(journal_refusal_code(reason), context)
    if result.get("fragment_id") is None:
        # `journal_ingest_tool` reports `ok` with a null `fragment_id` when
        # `_resolve_fragment_id` comes back empty right after a successful
        # write: the ledger and the writer disagree about what was just
        # written. This must be checked *before* construction and cannot be
        # left to the guard below, because `str(None)` raises nothing — it
        # mints the literal id ``"None"``, which the caller can store, quote
        # back and never resolve. Fabricating an id is precisely what this
        # module's docstring forbids, and there is no honest success to render,
        # so it is a server fault by the taxonomy's own definition.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    try:
        payload = JournalUpsertResponse(
            status=OK_STATUS,
            tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
            external_id=str(result["external_id"]),
            fragment_id=str(result["fragment_id"]),
            action=JournalAction(result["action"]),
            tier=WireTierCeiling(result["tier"]),
            # ``.get``, never ``result["warnings"]``: a missing key here would
            # land a *successful* write in the ``internal_error`` fallback
            # below, and that branch must not become newly reachable because
            # an advisory field was added. ``or None`` collapses the
            # no-advisory list to absent, so a quiet write dumps exactly the
            # bytes it did before this field existed (#1372).
            warnings=result.get("warnings") or None,
        )
    except (ValidationError, ValueError, KeyError):
        # A success in some *other* shape this contract cannot express: a key
        # the tool did not set, or a `tier_ceiling`/`action`/`tier` value the
        # wire enums cannot name. Nothing the caller can act on, so it lands in
        # the same server-fault bucket as the unresolved id above.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    # ``exclude_none`` so the optional ``warnings`` is absent rather than null
    # when the write was quiet. Every other field on the response is required,
    # so this cannot drop a key an existing consumer reads (#1372).
    return json_response(payload.model_dump(mode="json", exclude_none=True), HTTP_OK)


async def handle_journal_upsert(request: Request) -> Response:
    """Create or update one journal entry, idempotently.

    Three steps, in this order and for this reason: the path segment is checked
    before the body, because an id that cannot be a key makes the body moot; the
    body is validated before anything touches the disk, because a malformed
    request should not depend on the server's configuration to be refused; and
    everything blocking — resolving the vault included — happens last, in one
    worker thread.

    **Nothing here reads the filesystem.** Both checks above are pure, and
    resolving the vault is not: with no ``vault_path`` on the app — the
    production default, since :func:`creek_mcp.httpapi.cli.main` never passes
    one — it reads and parses ``creek_config.yaml`` per request. Done on the
    loop it would stall every other connection this process is serving and
    leave :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware`
    unable to fire for that window, since its cancel scope is evaluated on the
    loop. So it sits inside :func:`_upsert` with the rest of the blocking work,
    matching :mod:`creek_mcp.httpapi.capabilities`. The refusal is unchanged —
    an unreadable configuration is still ``unavailable``; only the thread that
    decides it moved.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    external_id = str(request.path_params["external_id"])
    if not admissible_external_id(external_id):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    parsed = await _parsed_body(request)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    result = await write_off_loop(_upsert, request, external_id, parsed, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return _render(result, context)


def _pending_path(vault: Path, consumer: str, external_id: str) -> Path:
    """Return a consumer-scoped recovery path that reveals neither input."""
    digest = sha256(f"{consumer}\0{external_id}".encode()).hexdigest()
    return vault / _WITHDRAW_PENDING_RELDIR / f"{digest}.json"


def _read_pending(path: Path) -> tuple[list[str], bool] | None:
    """Read one valid recovery record, or ``None`` when none exists.

    Raises:
        ValueError: When a present record cannot be trusted. Treating a corrupt
            record as absence could lose the fragment ids needed to finish a
            partial cache or ledger cleanup.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid journal withdrawal recovery record") from exc
    if not isinstance(raw, dict):
        raise ValueError("invalid journal withdrawal recovery record")
    raw_ids = raw.get("fragment_ids")
    legacy = raw.get("legacy")
    if not isinstance(raw_ids, list) or not isinstance(legacy, bool):
        raise ValueError("invalid journal withdrawal recovery record")
    fragment_ids = [item for item in raw_ids if isinstance(item, str) and item]
    if len(fragment_ids) != len(raw_ids):
        raise ValueError("invalid journal withdrawal recovery record")
    return fragment_ids, legacy


def _write_pending(path: Path, fragment_ids: list[str], *, legacy: bool) -> None:
    """Atomically persist the minimum state a post-crash retry needs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "fragment_ids": sorted(set(fragment_ids)),
                "legacy": legacy,
                "version": 1,
            },
            sort_keys=True,
        )
        + "\n",
    )


def _origin_key(post: frontmatter.Post) -> str | None:
    """Return one fragment's staged source key, when structurally valid."""
    source = post.get("source")
    if not isinstance(source, dict):
        return None
    origin = source.get("origin_key")
    return origin if isinstance(origin, str) and origin else None


def _fragment_ids_for_source(vault: Path, staged: Path) -> tuple[list[str], bool]:
    """Discover every fragment backed by *staged* and whether the scan was total."""
    source_key = derive_source_key(str(staged), vault)
    found: set[str] = set()
    ledger = ledger_for_source(_SOURCE_TYPE, vault)
    if ledger is not None:
        found.update(record.fragment_id for _, record in ledger.records_for(source_key))

    for path in sorted((vault / "01-Fragments").rglob("*.md")):
        try:
            post = frontmatter.load(path)
        except FRONTMATTER_LOAD_ERRORS:
            return sorted(found), False
        if _origin_key(post) != source_key:
            continue
        fragment_id = post.get("id")
        if not isinstance(fragment_id, str) or not fragment_id:
            return sorted(found), False
        found.add(fragment_id)
    return sorted(found), True


def _has_ledger_residue(vault: Path, fragment_ids: list[str], source_key: str) -> bool:
    """Return whether an ingest ledger still names the withdrawn source or ids."""
    directory = ledger_dir(vault)
    if not directory.is_dir():
        return False
    needles = [source_key, *fragment_ids]
    for path in sorted(directory.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            return True
        if any(needle in text for needle in needles):
            return True
    return False


def _has_markdown_residue(vault: Path, fragment_ids: list[str]) -> bool:
    """Return whether a live markdown artifact still names a withdrawn id."""
    encoded = [fragment_id.encode() for fragment_id in fragment_ids]
    for path in sorted(vault.rglob("*.md")):
        try:
            data = path.read_bytes()
        except OSError:
            return True
        if any(fragment_id in data for fragment_id in encoded):
            return True
    return False


def _has_index_residue(vault: Path, fragment_ids: list[str]) -> bool:
    """Return whether a persistent fragment index still names a removed id."""
    encoded = [fragment_id.encode() for fragment_id in fragment_ids]
    for path in sorted(vault.rglob(INDEX_FILENAME)):
        try:
            data = path.read_bytes()
        except OSError:
            return True
        if any(fragment_id in data for fragment_id in encoded):
            return True
    return False


def _scrub_fragment_indexes(vault: Path, fragment_ids: list[str]) -> None:
    """Compact every index that still carries a withdrawn fragment id.

    Vault indexes are append-only during ordinary writes, so unlinking a
    fragment leaves its old mapping on disk even though readers treat that
    mapping as stale. ``compact_index`` is the writer's locked, concurrent-safe
    maintenance primitive: it rebuilds from the live directory and atomically
    removes dead records without losing another process's append.
    """
    encoded = [fragment_id.encode() for fragment_id in fragment_ids]
    writer = VaultWriter(vault_path=vault)
    for path in sorted(vault.rglob(INDEX_FILENAME)):
        if any(fragment_id in path.read_bytes() for fragment_id in encoded):
            writer.compact_index(path.parent)


def _verified_absent(vault: Path, staged: Path, fragment_ids: list[str]) -> bool:
    """Verify every primary and retrieval-facing membership is absent."""
    if staged.exists():
        return False
    remaining, complete_scan = _fragment_ids_for_source(vault, staged)
    if not complete_scan or remaining:
        return False
    source_key = derive_source_key(str(staged), vault)
    if _has_ledger_residue(vault, fragment_ids, source_key):
        return False
    if _has_index_residue(vault, fragment_ids):
        return False
    if cache_contains_fragment_ids(embeddings_cache_path(vault), fragment_ids):
        return False
    return not _has_markdown_residue(vault, fragment_ids)


def _selected_withdrawal_source(
    request: Request,
    vault: Path,
    external_id: str,
    pending: tuple[list[str], bool] | None,
) -> tuple[Path, bool]:
    """Select the caller scope, or an unambiguous pre-0.16 legacy source."""
    scoped = journal_staged_path(
        vault,
        external_id,
        consumer_scope=context_of(request.scope).consumer,
    )
    legacy = journal_staged_path(vault, external_id)
    if pending is not None:
        return (legacy, True) if pending[1] else (scoped, False)
    if len(request.app.state.consumer_ids) != 1:
        return scoped, False
    legacy_ids, complete_scan = _fragment_ids_for_source(vault, legacy)
    if legacy.exists() or (complete_scan and legacy_ids):
        return legacy, True
    return scoped, False


def _audit_withdrawal(vault: Path, context: RequestContext) -> None:
    """Persist one bounded attempt without the concrete external identity."""
    MCPAuditLog(vault).append(
        tool=_WITHDRAW_AUDIT_TOOL,
        args={"has_external_id": True},
        tier_ceiling=context.ceiling,
        consumer=context.consumer,
    )


def _purge_ids(vault: Path, fragment_ids: list[str]) -> bool:
    """Purge all ids, returning false on any dry-run or apply shortfall."""
    for fragment_id in fragment_ids:
        preview = PurgeEngine(vault, dry_run=True).purge_fragment(fragment_id)
        if preview.outcome_status != "complete":
            return False
    for fragment_id in fragment_ids:
        applied = PurgeEngine(vault).purge_fragment(fragment_id)
        if applied.outcome_status != "complete":
            return False
    forget_fragment_ids(vault, fragment_ids)
    purge_fragment_ids_from_cache(embeddings_cache_path(vault), fragment_ids)
    return True


def _tomb_provenance(vault: Path, fragment_ids: list[str]) -> None:
    """Append durable, content-free withdrawal events for *fragment_ids*.

    The operational provenance log is hash-chained and append-only. Rewriting
    its historical write record would invalidate that chain, while leaving the
    write as its final event would keep the fragment an active member. A final
    ``withdrawal`` event preserves both properties: readers can resolve
    membership from the last event for an id, and no path, external id, or body
    enters the tombstone.
    """
    log = AuditLog(vault / "00-Creek-Meta" / "Processing-Log" / PROVENANCE_FILENAME)
    withdrawn_at = datetime.now(tz=UTC).isoformat()
    for fragment_id in fragment_ids:
        log.append(
            {
                "id": fragment_id,
                "type": "withdrawal",
                "withdrawn_at": withdrawn_at,
            }
        )


def _withdraw(
    request: Request,
    external_id: str,
    context: RequestContext,
) -> bool | ErrorCode | None:
    """Resolve and erase one consumer-owned journal identity off-loop."""
    vault = configured_vault(request)
    if vault is None:
        return None
    _audit_withdrawal(vault, context)
    marker = _pending_path(vault, context.consumer, external_id)
    try:
        with vault_lock(journal_mutation_lock_path(vault)):
            pending = _read_pending(marker)
            staged, legacy = _selected_withdrawal_source(
                request,
                vault,
                external_id,
                pending,
            )
            discovered, complete_scan = _fragment_ids_for_source(vault, staged)
            if not complete_scan:
                return ErrorCode.TEMPORARILY_UNAVAILABLE
            fragment_ids = sorted(set(discovered) | set(pending[0] if pending else []))
            if not fragment_ids and not staged.exists() and pending is None:
                return True
            _write_pending(marker, fragment_ids, legacy=legacy)
            if not _purge_ids(vault, fragment_ids):
                return ErrorCode.TEMPORARILY_UNAVAILABLE
            staged.unlink(missing_ok=True)
            _scrub_fragment_indexes(vault, fragment_ids)
            # This tombstone precedes the final cache check deliberately. A
            # linker holding a pre-withdraw snapshot consults the latest
            # provenance event under the cache lock before saving, so once
            # this append lands it cannot restore the withdrawn row. A crash
            # or failed postcondition remains retryable through ``marker``;
            # duplicate withdrawal events are content-free and idempotent.
            _tomb_provenance(vault, fragment_ids)
            if not _verified_absent(vault, staged, fragment_ids):
                return ErrorCode.TEMPORARILY_UNAVAILABLE
            marker.unlink()
            return True
    except (OSError, ValueError, VaultLockTimeoutError):
        return ErrorCode.TEMPORARILY_UNAVAILABLE


async def handle_journal_withdraw(request: Request) -> Response:
    """Withdraw one journal entry without accepting or returning prose."""
    context = context_of(request.scope)
    external_id = str(request.path_params["external_id"])
    if not admissible_external_id(external_id):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    if request.headers.get(CEILING_HEADER) is None or await request.body():
        return error_response(ErrorCode.INVALID_REQUEST, context)
    result = await write_off_loop(_withdraw, request, external_id, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    if isinstance(result, ErrorCode):
        return error_response(result, context)
    payload = JournalWithdrawResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(context.ceiling.value),
        action="withdrawn",
    )
    return json_response(payload.model_dump(mode="json"), HTTP_OK)
