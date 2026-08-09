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

from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from creek_mcp.api.models import (
    OK_STATUS,
    ErrorCode,
    JournalAction,
    JournalUpsertRequest,
    JournalUpsertResponse,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.tools.journal import journal_ingest_tool

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

MAX_EXTERNAL_ID_CHARS: Final[int] = 512
"""Inclusive upper bound on an ``external_id`` path segment.

Generous enough for any namespaced consumer id — the published example is 38
characters — and small enough that the id cannot become a payload. There is no
bound below this one: ``safe_stem`` truncates only the *readable slug* to 80
characters and then appends a digest of the whole raw id, so an unbounded id
would be accepted, stored and echoed at full length.
"""

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

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The published :class:`~creek_mcp.api.models.ErrorCode`.
    """
    if reason == _BLANK_CALL_REASON or reason.startswith(_UNKNOWN_TIER_PREFIX):
        return ErrorCode.INVALID_REQUEST
    if reason == GENERIC_ABOVE_CEILING_REASON or reason.endswith(_ABOVE_CEILING_SUFFIX):
        return ErrorCode.PRIVACY_REFUSED
    if reason == _VAULT_UNAVAILABLE_REASON:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


def admissible_external_id(raw: str) -> bool:
    """Return whether *raw* can serve as an idempotency key.

    Args:
        raw: The decoded ``{external_id}`` path segment.

    Returns:
        ``True`` when the id is non-blank, within
        :data:`MAX_EXTERNAL_ID_CHARS`, free of the replacement character, and
        made entirely of printable characters — a control byte would reach both
        the staged frontmatter and the audit trail.
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
        )
    except (ValidationError, ValueError, KeyError):
        # A success in some *other* shape this contract cannot express: a key
        # the tool did not set, or a `tier_ceiling`/`action`/`tier` value the
        # wire enums cannot name. Nothing the caller can act on, so it lands in
        # the same server-fault bucket as the unresolved id above.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


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
    result = await run_in_threadpool(_upsert, request, external_id, parsed, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return _render(result, context)
