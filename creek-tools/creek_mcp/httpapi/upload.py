"""``POST /v1/uploads`` — parse, delegate, project (#1524).

The document twin of :mod:`creek_mcp.httpapi.journal`, and built to the same
three rules: validate what arrived, hand it to the **existing**
:func:`creek_mcp.tools.upload.upload_tool`, and project that tool's return onto
the published wire model. There is no idempotency logic here, no privacy logic,
no staging, no ingest and no audit call of its own.

That is not tidiness, it is the whole reason this issue was small. ``creek
.upload`` already stages the bytes under a name derived from the ``external_id``,
dispatches by extension through :func:`creek.ingest.route_to_ingestor`, runs the
ledger-backed :func:`creek.ingest.pipeline.run_ingest`, refuses an upload whose
tier exceeds the caller's ceiling *before decoding a byte*, refuses an update
that would overwrite a fragment the caller could not have read (#970), refuses a
re-send that would fork one id onto two extensions, and audits each of those. A
route that reached ``run_ingest`` directly would lose every one of those
guarantees while still answering plausibly — the exact divergence epic #1071
exists to prevent. What was missing was never the behaviour; it was a door.

**No ``source_type`` on the wire.** The tool refuses that override
deliberately — the ``chatgpt`` / ``claude`` / ``discord`` / ``substack``
ingestors are directory-only, so naming one for a single staged file yields a
silent no-op reported as success — and this route does not reintroduce it.
Since #1525 an export **archive** is uploadable through this same route with
no new field: send the platform's ``.zip`` as ``content_base64``, and the tool
decides the export type from the archive's contents. That is why the override
is still absent — content-based detection is the guarantee the missing
override was standing in for.

**The unsupported-format refusal, and why it is translated rather than echoed.**
Since #1526 :func:`creek.ingest.route_to_ingestor` raises
:class:`~creek.ingest.UnsupportedSourceError` for a conversation export, an
archive or a legacy binary Office document instead of flattening it into one
``generic`` blob. ``upload_tool`` catches that above the staging write and
returns its message as a refusal *reason*; :func:`upload_refusal_code` maps that
reason to :attr:`~creek_mcp.api.models.ErrorCode.UNSUPPORTED_SOURCE`, whose
``415`` body carries the one published constant naming the remedy. The
exception's own text is **not** forwarded: it interpolates the caller's suffix,
and :func:`~creek_mcp.httpapi.errors.error_response` renders one constant per
code precisely so that no refusal on this surface is ever built from a string
something else composed.

**And the rest of the refusal projection.** Like ``journal_refusal_code``, the
mapping is total and fails closed to ``internal_error``: a reason this adapter
does not recognise is a reason it must not narrate. Two live reasons land there
on purpose — ``ingest failed: …``, which can carry a staged file path, and
``<source_type> ingest produced no fragment from this file``, which is the
backstop for content a reader could not extract anything from. Both are server
faults from the caller's point of view, and neither may reach a body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from creek.ingest import (
    ARCHIVE_GUIDANCE,
    LEGACY_OFFICE_GUIDANCE,
    STRUCTURED_EXPORT_GUIDANCE,
)
from creek.ingest.archive import (
    TOO_LARGE_REASON,
    TOO_MANY_ENTRIES_REASON,
    UNREADABLE_ARCHIVE_REASON,
    UNRECOGNISED_EXPORT_REASON,
    UNSAFE_ENTRY_REASON,
)
from creek_mcp.api.models import (
    OK_STATUS,
    ErrorCode,
    JournalAction,
    UploadRequest,
    UploadResponse,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import write_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.journal import admissible_external_id
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.tier_ceiling import TIER_REQUIRED_REASON
from creek_mcp.tools.upload import ARCHIVE_NO_FRAGMENTS_REASON, upload_tool

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

_BLANK_CALL_REASON: Final[str] = "filename, external_id and content_base64 are required"
"""``_validated_upload``'s malformed-call refusal, verbatim."""

_UNKNOWN_TIER_PREFIX: Final[str] = "unknown tier "
"""``_validated_upload``'s unparseable-tier refusal, by prefix."""

_ABOVE_CEILING_SUFFIX: Final[str] = " exceeds the ceiling"
"""The write-tier gate's refusal (``upload tier <tier> exceeds the ceiling``)."""

_NOT_BASE64_REASON: Final[str] = "content_base64 is not valid base64"
"""``_admit_payload``'s decode refusal, verbatim."""

_OVERSIZE_SUFFIX: Final[str] = "-byte cap"
"""Both size refusals end here (``encoded``/``decoded upload exceeds the N-byte cap``).

Matched by suffix rather than by two prefixes because the byte count is
interpolated into the middle of each: the constant part is the tail.
"""

_EXTENSION_CONFLICT_PREFIX: Final[str] = "external_id is already bound to "
"""The one-id-one-extension refusal, by prefix.

A caller error and not a server fault — the remedy, purging the existing
fragment or choosing a new id, is entirely in the caller's hands — so it earns
``invalid_request`` rather than the ``internal_error`` fallthrough.
"""

_VAULT_UNAVAILABLE_REASON: Final[str] = "vault unavailable"
"""The refusal a missing vault earns from the tool, verbatim.

Transient rather than terminal, exactly as on the journal route: a vault
directory reappearing is the kind of thing that clears on its own.
"""

_UNSUPPORTED_SOURCE_GUIDANCE: Final[tuple[str, ...]] = (
    STRUCTURED_EXPORT_GUIDANCE,
    ARCHIVE_GUIDANCE,
    LEGACY_OFFICE_GUIDANCE,
)
"""The three refusal remedies :func:`creek.ingest.route_to_ingestor` can raise.

**Imported, never copied.** ``UnsupportedSourceError``'s message is
``"Creek cannot ingest a '<suffix>' file as a single document. " + guidance``,
so the guidance is its stable tail and matching on it is what makes this
recognition survive a reworded refusal. A copied string would drift silently and
the whole family would start falling through to ``internal_error`` — a ``500``
where a ``415`` with a remedy belongs. A test sweeps every refused extension in
:data:`creek.ingest.gdrive._EXTENSION_ROUTES` through the real route to prove
the recognition is total rather than merely plausible.
"""


_MALFORMED_ARCHIVE_REASONS: Final[frozenset[str]] = frozenset(
    {
        UNREADABLE_ARCHIVE_REASON,
        TOO_MANY_ENTRIES_REASON,
        TOO_LARGE_REASON,
        UNSAFE_ENTRY_REASON,
    }
)
"""The #1525 archive refusals that are the CALLER's to fix, so ``400``.

Every one of them is decided from the caller's own bytes and names nothing
about the vault: bytes that are not a readable ZIP, an archive over the entry
or extraction-byte bound, and a member that would be written outside the
staging root. ``invalid_request`` rather than ``privacy_refused`` because none
of them is a tier decision, and rather than ``internal_error`` because a
caller that is told "server fault" retries the same crafted archive forever.

**Imported, never restated.** These are the exact strings
:mod:`creek.ingest.archive` raises, and a copied spelling here would silently
turn a zip-slip refusal into a ``500`` — the one refusal on this surface where
the caller most needs a definite answer.
"""

_UNSUPPORTED_ARCHIVE_REASONS: Final[frozenset[str]] = frozenset(
    {UNRECOGNISED_EXPORT_REASON, ARCHIVE_NO_FRAGMENTS_REASON}
)
"""The #1525 archive refusals that mean "readable, but not an export I know".

``415`` with the published remedy, joining the #1526 family: the archive
unpacked safely and Creek still could not name it as a chatgpt / claude /
discord / substack export, or named it and read no content out of it. Both are
answered by the same advice — unpack it and run ``creek ingest`` — which is
what :attr:`~creek_mcp.api.models.ErrorCode.UNSUPPORTED_SOURCE`'s body says.
"""


def upload_refusal_code(reason: str) -> ErrorCode:
    """Return the wire code for one of the upload tool's refusal reasons.

    Total by construction, and failing closed to ``internal_error`` rather than
    to a plausible refusal: a reason this adapter cannot classify is not a
    privacy decision it may claim to have made on the caller's behalf.

    The reasons are spelled as constants above rather than imported because
    :func:`creek_mcp.tools.upload.upload_tool` composes most of them inline as
    f-strings, so there is nothing to import — with two exceptions that *are*
    imported, for the two cases where a copy would be actively dangerous:
    :data:`~creek_mcp.tier_ceiling.TIER_REQUIRED_REASON`, one shared literal
    read by all three write verbs, and the three ``#1526`` guidance strings,
    whose drift would silently downgrade a ``415``-with-a-remedy into a ``500``.

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The published :class:`~creek_mcp.api.models.ErrorCode`.
    """
    if (
        any(reason.endswith(guidance) for guidance in _UNSUPPORTED_SOURCE_GUIDANCE)
        or reason in _UNSUPPORTED_ARCHIVE_REASONS
    ):
        return ErrorCode.UNSUPPORTED_SOURCE
    if reason in _MALFORMED_ARCHIVE_REASONS:
        return ErrorCode.INVALID_REQUEST
    if (
        reason in (_BLANK_CALL_REASON, TIER_REQUIRED_REASON, _NOT_BASE64_REASON)
        or reason.startswith((_UNKNOWN_TIER_PREFIX, _EXTENSION_CONFLICT_PREFIX))
        or reason.endswith(_OVERSIZE_SUFFIX)
    ):
        return ErrorCode.INVALID_REQUEST
    if reason == GENERIC_ABOVE_CEILING_REASON or reason.endswith(_ABOVE_CEILING_SUFFIX):
        return ErrorCode.PRIVACY_REFUSED
    if reason == _VAULT_UNAVAILABLE_REASON:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


async def _parsed_body(request: Request) -> UploadRequest | None:
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
        return UploadRequest.model_validate(raw)
    except (ValidationError, ValueError, UnicodeDecodeError):
        return None


def _upload(
    request: Request,
    parsed: UploadRequest,
    context: RequestContext,
) -> dict[str, Any] | None:
    """Resolve the vault and run the upload tool, both off the event loop.

    Every blocking call this route makes is reachable from here and nowhere
    else — the config read and YAML parse behind
    :func:`~creek_mcp.httpapi.vault.configured_vault`, the base64 decode of up
    to ten megabytes, the staging write, the ledger read, the whole ingest run
    and an audit append that holds an ``fcntl`` lock across an ``fsync``. This
    is the surface where that matters most: a document ingest is the longest
    unit of work ``/v1`` performs, and on the event loop it would stall every
    other connection the process is serving and leave
    :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware`
    unable to fire, since its cancel scope is evaluated on the loop.

    Args:
        request: The request in flight, which names the vault to resolve.
        parsed: The validated request body.
        context: The request's context, supplying the *admitted* ceiling and
            the authenticated consumer. Both come from the context rather than
            from anything this handler re-derives: the ceiling was decided
            once, at the adapter edge, and a second derivation here would be a
            second gate.

    Returns:
        The tool's return dict, success or refusal — or ``None`` when there is
        no readable vault to run against.
    """
    vault = configured_vault(request)
    if vault is None:
        return None
    return upload_tool(
        vault_path=vault,
        filename=parsed.filename,
        content_base64=parsed.content_base64,
        external_id=parsed.external_id,
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
        The ``200`` carrying :class:`~creek_mcp.api.models.UploadResponse`, or
        the published refusal for the tool's reason, or ``internal_error`` when
        the document was ingested but cannot be honestly described.
    """
    if result.get("status") != OK_STATUS:
        return error_response(
            upload_refusal_code(str(result.get("reason", ""))), context
        )
    if result.get("fragment_id") is None:
        # The tool reports `ok` with a null `fragment_id` when the ingest
        # succeeded and the ledger yielded no id for the staged file: the
        # writer and the ledger disagree about what was just written. Checked
        # before construction, never left to the guard below, because
        # `str(None)` raises nothing — it mints the literal id ``"None"``,
        # which a caller can store, quote back and never resolve.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    try:
        payload = UploadResponse(
            status=OK_STATUS,
            tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
            external_id=str(result["external_id"]),
            fragment_id=str(result["fragment_id"]),
            affected_fragment_ids=[
                str(identity) for identity in result["affected_fragment_ids"]
            ],
            action=JournalAction(result["action"]),
            source_type=str(result["source_type"]),
            # ``.get``, never ``result["warnings"]``: a missing key must not
            # land a *successful* ingest in the ``internal_error`` fallback.
            # ``or None`` collapses the no-advisory list to absent, so a quiet
            # upload does not put an unnegotiated key on the wire.
            warnings=result.get("warnings") or None,
        )
    except (ValidationError, ValueError, KeyError, TypeError):
        # A success in some *other* shape this contract cannot express: a key
        # the tool did not set, or a `tier_ceiling`/`action` value the wire
        # enums cannot name. Nothing the caller can act on, so it lands in the
        # same server-fault bucket as the unresolved id above.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json", exclude_none=True), HTTP_OK)


async def handle_upload(request: Request) -> Response:
    """Ingest one uploaded document as a vault fragment, idempotently.

    Three steps, in this order and for this reason: the body is validated
    before anything touches the disk, because a malformed request should not
    depend on the server's configuration to be refused; the ``external_id`` is
    checked against the same admissibility rule the journal route applies,
    because an id that cannot serve as an idempotency key mints a fragment the
    client can never address again; and everything blocking — resolving the
    vault included — happens last, in one worker thread.

    The id rule is *imported* from :mod:`creek_mcp.httpapi.journal` rather than
    restated. Both write surfaces mean the same thing by "an id that can serve
    as an idempotency key", they reach the same
    :func:`creek_mcp.staged_names.safe_stem`, and two copies of that rule would
    let the same id be addressable through one surface and not the other.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    parsed = await _parsed_body(request)
    if parsed is None or not admissible_external_id(parsed.external_id):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    result = await write_off_loop(_upload, request, parsed, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return _render(result, context)
