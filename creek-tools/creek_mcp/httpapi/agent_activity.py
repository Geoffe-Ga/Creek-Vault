"""``GET /v1/agent-activity`` — what each agent asked for, never what it read (#1968).

A person who connected an agent to their vault needs to tell a quiet helper
from a noisy one, and to revoke with a reason. The only record of what agents
asked for is the hash-chained MCP audit log,
``00-Creek-Meta/audit/mcp.jsonl`` (:mod:`creek_mcp.audit`), and until this
route the owner's only reader of it was ``cat``. This module projects that log
onto :class:`~creek_mcp.api.models.AgentActivityResponse`: the most recent
calls, newest first, each reduced to ``{consumer, tool, at, outcome}``.

**Content-free, by projection rather than by redaction.** An audit entry also
carries ``args_summary`` (short argument strings verbatim, long ones as
lengths), the caller's ceiling, write-side paths and fragment ids, and two
hashes. None of it is copied and then scrubbed: each row is *built* from four
named fields, each validated against a closed published pattern, so a field
this module never names cannot cross however an entry is shaped. A ``tool``
that is not a dotted tool name and a ``consumer`` carrying a control character
are not rendered at all; they count as skipped.

**What ``outcome`` can and cannot say.** ``refused`` is an entry the MCP
adapter wrote for a pre-dispatch refusal — an out-of-profile tool (#1964) or a
remote ceiling above ``personal`` (#1968). Every other entry is ``admitted``:
the call reached its tool. How the tool then answered is deliberately not in
the chain — the read-gate discipline (:mod:`creek_mcp.read_gate`) logs the
attempt before resolving the target and never the result — so ``admitted``
does not mean "content was returned", and this route cannot make it mean that.

**Job lifecycle entries are not calls.** A composition that outlives the
synchronous deadline (#1966) writes its tool's ordinary entry, then an
``accepted`` entry and a ``completed`` one carrying the same job id. The owner
asked for one draft, so the card shows one row: the lifecycle entries are
neither rendered nor counted as skipped.

**Nothing is hidden by consumer.** The app's own ``adepthood`` calls through
``/v1`` are audited into the same log under their own stamp and are listed
beside every agent's; there is no consumer filter, and an unknown query
parameter asking for one is ``invalid_request``.

**The log is read, never written.** The route opens the file read-only and
appends no entry of its own, so an owner refreshing the card cannot grow the
trail it is reading. The cost is one linear pass over the log per request,
holding at most ``limit`` rows; it runs in a worker thread under the request
deadline, through :func:`creek_mcp.httpapi.deadline.read_off_loop`.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple

from pydantic import ValidationError

from creek_mcp.api.models import (
    AGENT_ACTIVITY_MAX_LIMIT,
    OK_STATUS,
    AgentActivityOutcome,
    AgentActivityQuery,
    AgentActivityResponse,
    AgentActivityRow,
    ErrorCode,
    WireTierCeiling,
)
from creek_mcp.audit import (
    AUDIT_OUTCOME_ACCEPTED,
    AUDIT_OUTCOME_COMPLETED,
    AUDIT_OUTCOME_REFUSED,
    MCP_AUDIT_RELPATH,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import read_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tools.handshake import vault_available

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

_SKIPPED: Final[Literal["skipped"]] = "skipped"
"""A line the projection could not read: counted, never rendered."""

_NOT_A_CALL: Final[Literal["not-a-call"]] = "not-a-call"
"""A job-lifecycle entry: a follow-up of a call already listed, not a call."""

_LIFECYCLE_OUTCOMES: Final[frozenset[str]] = frozenset(
    {AUDIT_OUTCOME_ACCEPTED, AUDIT_OUTCOME_COMPLETED}
)
"""The audit outcomes a detached composition writes after its call (#1966)."""

_OUTCOMES: Final[dict[str | None, AgentActivityOutcome]] = {
    None: AgentActivityOutcome.ADMITTED,
    AUDIT_OUTCOME_REFUSED: AgentActivityOutcome.REFUSED,
}
"""Audit ``outcome`` to wire outcome. Absent means the tool ran and wrote it."""


class _Projected(NamedTuple):
    """One readable call: its row, and the full-precision instant behind it.

    The row's ``at`` is truncated to the second for display; ``since`` is
    compared against the untruncated instant so a call later in the same
    second as ``since`` is not dropped.
    """

    row: AgentActivityRow
    instant: datetime


class ActivityPage(NamedTuple):
    """The projected page: the rows, newest first, and the unreadable count.

    Attributes:
        rows: At most the requested number of calls, newest first.
        skipped: Lines of the whole log that could not be projected.
    """

    rows: list[AgentActivityRow]
    skipped: int


def _decoded(raw: bytes) -> dict[str, Any] | None:
    """Return the JSON object on one log line, or ``None`` when there is none.

    Args:
        raw: One line of the log, as bytes, newline included or not.

    Returns:
        The decoded object, or ``None`` for undecodable bytes, malformed JSON
        or a JSON value that is not an object.
    """
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _instant(entry: dict[str, Any]) -> datetime | None:
    """Return *entry*'s audited instant in UTC, or ``None`` when unreadable.

    Args:
        entry: One decoded audit entry.

    Returns:
        The ``timestamp`` as an aware UTC datetime, or ``None`` when it is
        missing, not a string, not ISO 8601, or carries no offset — a naive
        instant cannot be placed on the timeline the card renders.
    """
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _outcome(
    entry: dict[str, Any],
) -> AgentActivityOutcome | Literal["not-a-call"] | None:
    """Return the wire outcome *entry* records, or why it has none.

    Args:
        entry: One decoded audit entry.

    Returns:
        ``admitted`` for an entry without ``outcome``, ``refused`` for a
        pre-dispatch refusal, :data:`_NOT_A_CALL` for a job-lifecycle entry,
        and ``None`` for any other value — an outcome the closed enum cannot
        name is not guessed at.
    """
    recorded = entry.get("outcome")
    if recorded is not None and not isinstance(recorded, str):
        return None
    if recorded in _LIFECYCLE_OUTCOMES:
        return _NOT_A_CALL
    return _OUTCOMES.get(recorded)


def _project(raw: bytes) -> _Projected | Literal["skipped", "not-a-call"]:
    """Project one log line onto a row, or say why it is not one.

    Args:
        raw: One non-blank line of the log.

    Returns:
        The projected call; :data:`_NOT_A_CALL` for a job-lifecycle entry; or
        :data:`_SKIPPED` for anything the closed row shape cannot carry.
    """
    entry = _decoded(raw)
    if entry is None:
        return _SKIPPED
    outcome = _outcome(entry)
    if outcome == _NOT_A_CALL:
        return _NOT_A_CALL
    instant = _instant(entry)
    if outcome is None or instant is None:
        return _SKIPPED
    try:
        row = AgentActivityRow.model_validate(
            {
                "consumer": entry.get("consumer"),
                "tool": entry.get("tool"),
                "at": instant.replace(microsecond=0),
                "outcome": outcome,
            }
        )
    except ValidationError:
        return _SKIPPED
    return _Projected(row=row, instant=instant)


def read_activity(
    vault: Path, *, limit: int, since: datetime | None
) -> ActivityPage | None:
    """Return the newest *limit* calls in *vault*'s MCP audit log.

    One forward pass in chain order — which is append order — keeping the
    most recent *limit* matches, so memory is bounded by the page and not by
    the log. Order is the chain's, not a sort by timestamp: the chain is the
    record of what happened in what order.

    Args:
        vault: The vault root.
        limit: The most rows to return; clamped to
            :data:`~creek_mcp.api.models.AGENT_ACTIVITY_MAX_LIMIT`.
        since: An inclusive lower bound on the audited instant, or ``None``.

    Returns:
        The page, newest first — empty when the log does not exist yet — or
        ``None`` when the log exists but cannot be read.
    """
    log_path = vault / MCP_AUDIT_RELPATH
    if not log_path.exists():
        return ActivityPage(rows=[], skipped=0)
    kept: deque[AgentActivityRow] = deque(maxlen=min(limit, AGENT_ACTIVITY_MAX_LIMIT))
    skipped = 0
    try:
        with log_path.open("rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                projected = _project(raw)
                if projected == _SKIPPED:
                    skipped += 1
                elif isinstance(projected, _Projected) and (
                    since is None or projected.instant >= since
                ):
                    kept.append(projected.row)
    except OSError:
        return None
    return ActivityPage(rows=list(reversed(kept)), skipped=skipped)


def parsed_query(request: Request) -> AgentActivityQuery | None:
    """Return the validated query string, or ``None`` when it is not admissible.

    A parameter given twice is refused rather than resolved, because "last one
    wins" and "first one wins" are two readings two clients would disagree
    about.

    Args:
        request: The request in flight.

    Returns:
        The validated query, or ``None`` for a repeated, unknown or malformed
        parameter.
    """
    items = request.query_params.multi_items()
    names = [name for name, _value in items]
    if len(names) != len(set(names)):
        return None
    try:
        return AgentActivityQuery.model_validate(dict(items))
    except ValidationError:
        return None


def _read(request: Request, query: AgentActivityQuery) -> ActivityPage | None:
    """Resolve the vault and project its log, both off the event loop.

    Args:
        request: The request in flight, which names the vault to resolve.
        query: The validated query.

    Returns:
        The page, or ``None`` when there is no readable vault or log.
    """
    vault = configured_vault(request)
    if vault is None or not vault_available(vault):
        return None
    return read_activity(vault, limit=query.limit, since=query.since)


async def handle_agent_activity(request: Request) -> Response:
    """Return the most recent MCP calls against this vault, content-free.

    **The chain is read, not verified.** :func:`creek_mcp.audit.verify_mcp_audit_chain`
    re-hashes every line of the log and is deliberately not run per request:
    its cost grows with the log, and this route is fetched each time the owner
    opens a settings screen. So the response carries **no**
    ``chain_verified_at`` field, on purpose. A timestamp of the last
    verification would have to come from somewhere other than this request,
    and stamping the time of a check the request did not perform would tell
    the owner the rows were verified when they were not. The rows are a
    display of what the log says; tamper-evidence stays with the verifier and
    the operator who runs it.

    Args:
        request: The request in flight.

    Returns:
        The published page; ``invalid_request`` for a query the published
        schema does not admit; ``unavailable`` when there is no readable vault
        or log.
    """
    context = context_of(request.scope)
    query = parsed_query(request)
    if query is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    page = await read_off_loop(_read, request, query)
    if page is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    payload = AgentActivityResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(context.ceiling.value),
        skipped=page.skipped,
        rows=page.rows,
    )
    return json_response(payload.model_dump(mode="json"), HTTP_OK)
