"""Progress, a server deadline and a job-handle fallback for compositions (#1966).

``creek.draft`` and ``creek.author`` compose on the vault's own model stack,
and on a pinned local model that can take minutes. MCP clients own their own
request timeouts — OpenClaw's documented example is 20 s, the Fly edge drops an
idle connection well before a cold model finishes — so a synchronous tool call
that simply runs to completion dies to somebody else's clock. This module is
the one place those two tools run through instead:

1. **Admission.** One active composition per consumer, the #1605 rule ``/v1``
   already applies to its detached pipeline jobs, decided on the same
   :class:`~creek_mcp.httpapi.job_runtime.JobRuntime`. A second long call
   while one runs gets :func:`~creek_mcp.tools.jobs.unavailable_refusal`.
2. **A durable record from the first moment.** The call is written as a
   ``draft`` / ``author`` :class:`~creek_mcp.httpapi.jobs.StoredJob` — the
   ``/v1`` record type, in the ``/v1`` job directory — and claimed
   ``running`` before any composing starts, so a server that dies mid-call
   leaves a record the restart rule marks ``failed`` rather than nothing.
3. **Progress.** The tool runs in a worker thread and reports each
   :class:`~creek.progress.CompositionStage` it crosses; each is forwarded as
   ``ctx.report_progress(n, total, stage)`` — the stage *name* and its
   position, never a title or a line of prose.
4. **The deadline.** :data:`DEFAULT_SYNC_DEADLINE_SECONDS`, overridable through
   :data:`SYNC_DEADLINE_ENV`. A composition that beats it answers exactly as
   the synchronous tool always has. One that does not is *detached*: the call
   answers ``{status: "accepted", job_id, state: "running"}`` and the same
   worker keeps composing, writing the draft to ``07-Voice/Drafts/`` and then
   the job's content-free result, which ``creek.jobs.read`` serves.

**Who wins a near-tie is decided once, under one lock.** The worker finishing
and the deadline firing can race. :class:`_Handoff` settles it: whichever
takes its lock first decides whether the caller gets the synchronous answer or
a handle, and the loser defers. The ``accepted`` audit entry is written inside
that same critical section, so the ``completed`` entry the worker writes can
never precede it; both carry the job id.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import threading
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TypeAlias

from creek_mcp.api.models import JobState
from creek_mcp.audit import (
    AUDIT_OUTCOME_ACCEPTED,
    AUDIT_OUTCOME_COMPLETED,
    MCPAuditLog,
)
from creek_mcp.httpapi.jobs import claim_job, create_job, finish_job
from creek_mcp.tools.author import AUTHOR_STAGES, author_job_result, save_authored_draft
from creek_mcp.tools.author import TOOL_NAME as AUTHOR_TOOL_NAME
from creek_mcp.tools.draft import DRAFT_STAGES, draft_job_result
from creek_mcp.tools.draft import TOOL_NAME as DRAFT_TOOL_NAME
from creek_mcp.tools.jobs import jobs_read_tool, unavailable_refusal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from mcp.server.fastmcp import Context

    from creek.progress import CompositionStage, StageCallback
    from creek_mcp.httpapi.job_runtime import JobRuntime
    from creek_mcp.httpapi.jobs import StoredJob
    from creek_mcp.tier_ceiling import TierCeiling

logger = logging.getLogger(__name__)

SYNC_DEADLINE_ENV: Final[str] = "CREEK_MCP_SYNC_DEADLINE_SECONDS"
"""The environment variable an operator sets to move the synchronous deadline."""

DEFAULT_SYNC_DEADLINE_SECONDS: Final[float] = 90.0
"""How long a composition may run before the call answers with a job handle.

90 s because Claude's hosted client tolerates a long tool call while the Fly
edge's idle timeout and OpenClaw's defaults do not: long enough that a warm
model's draft comes back in one call, short enough that the call itself is
never the thing that times out.
"""

CompositionKind: TypeAlias = Literal["draft", "author"]
"""The two :class:`~creek_mcp.httpapi.jobs.StoredJob` kinds this module writes."""


class _Work(Protocol):
    """The synchronous tool call, with every argument but the sink bound."""

    def __call__(self, *, on_stage: StageCallback) -> dict[str, Any]:
        """Run the composition, reporting each stage to *on_stage*."""


class _Settle(Protocol):
    """Turn the tool's response into the job's content-free result."""

    def __call__(
        self, response: dict[str, Any], *, detached: bool
    ) -> dict[str, Any] | None:
        """Return the result to store, or ``None`` for ``failed``."""


_ProgressSink: TypeAlias = (
    "Callable[[float, float | None, str | None], Awaitable[None]]"
)
"""``Context.report_progress``'s shape: ``(progress, total, message)``."""


@dataclass(frozen=True)
class CompositionCall:
    """One ``creek.draft`` / ``creek.author`` call, ready to run.

    Attributes:
        tool: The tool's MCP name.
        kind: The job-record kind.
        consumer: The caller, from ``_effective_consumer`` — the identity the
            job record is bound to and every audit entry is written under.
        ceiling: The caller's declared ceiling, echoed and audited.
        stages: The boundaries this tool reports, in order; their count is
            the progress ``total``.
        work: The tool call itself.
        settle: Its response's projection onto the job result.
    """

    tool: str
    kind: CompositionKind
    consumer: str
    ceiling: TierCeiling
    stages: tuple[CompositionStage, ...]
    work: _Work
    settle: _Settle


def sync_deadline_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Return the configured synchronous deadline, failing loudly on nonsense.

    An unset or blank variable is the default. Anything else must parse as a
    finite, positive number of seconds: a typo here would otherwise either
    detach every call at once or never detach any, and an operator should hear
    about it at start-up rather than discover it from a client timeout.

    Args:
        environ: The environment to read; ``os.environ`` when ``None``.

    Returns:
        The deadline in seconds.

    Raises:
        ValueError: When the variable is set to anything but a positive,
            finite number.
    """
    source = os.environ if environ is None else environ
    raw = source.get(SYNC_DEADLINE_ENV, "").strip()
    if not raw:
        return DEFAULT_SYNC_DEADLINE_SECONDS
    msg = f"{SYNC_DEADLINE_ENV} must be a positive number of seconds, got {raw!r}"
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(msg) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(msg)
    return value


def draft_call(work: _Work, *, consumer: str, ceiling: TierCeiling) -> CompositionCall:
    """Return the :class:`CompositionCall` for one ``creek.draft`` invocation.

    The draft tool saves its own draft whichever way the call ends, so its
    settlement ignores *detached* and keeps the content-free projection.

    Args:
        work: ``draft_tool`` with everything but ``on_stage`` bound.
        consumer: The caller's identity.
        ceiling: The caller's ceiling.

    Returns:
        The call.
    """
    return CompositionCall(
        tool=DRAFT_TOOL_NAME,
        kind="draft",
        consumer=consumer,
        ceiling=ceiling,
        stages=DRAFT_STAGES,
        work=work,
        settle=_settle_draft,
    )


def author_call(
    work: _Work, *, vault: Path, consumer: str, ceiling: TierCeiling
) -> CompositionCall:
    """Return the :class:`CompositionCall` for one ``creek.author`` invocation.

    Unlike ``creek.draft``, the desk returns its body rather than saving it,
    so a detached ``ok`` run is written to ``07-Voice/Drafts/`` by its
    settlement — the body would otherwise have nowhere to go.

    Args:
        work: ``author_tool`` with everything but ``on_stage`` bound.
        vault: The vault a detached draft is written into.
        consumer: The caller's identity.
        ceiling: The caller's ceiling.

    Returns:
        The call.
    """
    return CompositionCall(
        tool=AUTHOR_TOOL_NAME,
        kind="author",
        consumer=consumer,
        ceiling=ceiling,
        stages=AUTHOR_STAGES,
        work=work,
        settle=partial(_settle_author, vault),
    )


def _settle_draft(response: dict[str, Any], *, detached: bool) -> dict[str, Any] | None:
    """Project a draft response; the draft is already on disk either way."""
    del detached
    return draft_job_result(response)


def _settle_author(
    vault: Path, response: dict[str, Any], *, detached: bool
) -> dict[str, Any] | None:
    """Project an author response, first saving a detached run's body."""
    if response.get("status") != "ok" or not detached:
        return author_job_result(response, None)
    saved = save_authored_draft(vault, response)
    return author_job_result(response, str(saved.relative_to(vault)))


def _post(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[CompositionStage | None],
    stage: CompositionStage,
) -> None:
    """Hand *stage* from the worker thread to the event loop's queue.

    A closed loop — the server shutting down under a detached job — must not
    fail the composition, so its ``RuntimeError`` is swallowed: the stage had
    nobody left to reach.
    """
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(queue.put_nowait, stage)


class _Handoff:
    """The single, thread-safe decision between "answer" and "detach".

    The worker thread calls :meth:`report` and then :meth:`finish`; the event
    loop calls :meth:`detach` (through a thread, so the lock is never taken on
    the loop) once the deadline passes. All three share one lock, so exactly
    one of "the worker finished first" and "the call detached first" is ever
    true.
    """

    def __init__(self, emit: Callable[[CompositionStage], None]) -> None:
        """Build an undecided handoff forwarding stages through *emit*."""
        self._lock = threading.Lock()
        self._emit = emit
        self._finished = False
        self._detached = False

    def report(self, stage: CompositionStage) -> None:
        """Forward *stage* to the caller, unless the call has detached."""
        with self._lock:
            if not self._detached:
                self._emit(stage)

    def finish(self) -> bool:
        """Mark the composition finished; return whether it had detached."""
        with self._lock:
            self._finished = True
            return self._detached

    def detach(self, record_acceptance: Callable[[], None]) -> bool:
        """Detach unless the worker already finished; return whether it did.

        *record_acceptance* runs inside the lock, before the detach is
        visible, so the worker's ``completed`` audit entry cannot precede the
        ``accepted`` one.
        """
        with self._lock:
            if self._finished:
                return False
            record_acceptance()
            self._detached = True
            return True


def _progress_sink(ctx: Context) -> _ProgressSink | None:
    """Return the request's progress sender, or ``None`` when there is none.

    ``None`` both outside a request (an in-process ``call_tool``) and when the
    client sent no ``progressToken``, which is how MCP clients decline
    progress.
    """
    try:
        meta = ctx.request_context.meta
    except ValueError:
        return None
    if meta is None or meta.progressToken is None:
        return None
    return ctx.report_progress


async def _forward(
    queue: asyncio.Queue[CompositionStage | None],
    sink: _ProgressSink | None,
    stages: tuple[CompositionStage, ...],
) -> None:
    """Send each queued stage as one progress notification until ``None``.

    Positions only ever increase, as MCP requires of ``progress``. Progress is
    best-effort: a notification that cannot be sent (the client went away)
    stops the forwarding, never the composition.
    """
    last = 0
    total = float(len(stages))
    while (stage := await queue.get()) is not None:
        if sink is None or stage not in stages:
            continue
        position = stages.index(stage) + 1
        if position <= last:
            continue
        last = position
        try:
            await sink(float(position), total, stage.value)
        except Exception:
            logger.warning("progress notification not delivered", exc_info=True)
            return


@dataclass(frozen=True)
class CompositionJobs:
    """Run compositions under admission, progress and the deadline.

    One per served MCP server, built by ``build_server``.

    Attributes:
        vault: The served vault; its job directory and audit log.
        runtime: The process's job runtime — shareable with a ``/v1`` app in
            the same process, so one consumer holds one slot across both.
        deadline_seconds: How long a call may compose synchronously.
    """

    vault: Path
    runtime: JobRuntime
    deadline_seconds: float

    async def run(self, call: CompositionCall, ctx: Context) -> dict[str, Any]:
        """Run *call*, answering synchronously or with a job handle.

        Args:
            call: The composition to run.
            ctx: The FastMCP request context progress is reported through.

        Returns:
            The tool's own response when it beat the deadline, the accepted
            handle when it did not, or the ``unavailable`` refusal when the
            consumer already has a composition running.
        """
        queue: asyncio.Queue[CompositionStage | None] = asyncio.Queue()
        handoff = _Handoff(partial(_post, asyncio.get_running_loop(), queue))
        async with self.runtime.admission_lock:
            if self.runtime.busy(call.consumer):
                return unavailable_refusal(call.tool, call.ceiling)
            record = await asyncio.to_thread(self._open, call)
            if record is None:
                return unavailable_refusal(call.tool, call.ceiling)
            task = asyncio.create_task(
                asyncio.to_thread(self._execute, call, record, handoff)
            )
            self.runtime.track(call.consumer, record.job_id, task)
        forwarder = asyncio.create_task(
            _forward(queue, _progress_sink(ctx), call.stages)
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=self.deadline_seconds)
            if not done and await asyncio.to_thread(
                handoff.detach,
                partial(self._audit, call, record.job_id, AUDIT_OUTCOME_ACCEPTED),
            ):
                return self._accepted(call, record.job_id)
            response = await task
            queue.put_nowait(None)
            await forwarder
        finally:
            forwarder.cancel()
        return response

    async def read(
        self, *, job_id: str, ceiling: TierCeiling, consumer: str
    ) -> dict[str, Any]:
        """Resolve one of *consumer*'s job handles through ``creek.jobs.read``.

        Args:
            job_id: The handle.
            ceiling: The caller's ceiling, audited and echoed.
            consumer: The caller's identity.

        Returns:
            The tool's response.
        """
        # Snapshot on the loop: the worker thread reading the record gets an
        # immutable value, never the loop-owned set.
        active = frozenset(self.runtime.active_job_ids)
        return await asyncio.to_thread(
            partial(
                jobs_read_tool,
                vault_path=self.vault,
                job_id=job_id,
                worker_id=self.runtime.worker_id,
                active_job_ids=active,
                privacy_tier_ceiling=ceiling,
                consumer=consumer,
            )
        )

    def _open(self, call: CompositionCall) -> StoredJob | None:
        """Durably create *call*'s job record and claim it ``running``."""
        record = create_job(
            self.vault,
            consumer=call.consumer,
            kind=call.kind,
            method=call.tool,
            retier=False,
            ceiling=call.ceiling.value,
            worker_id=self.runtime.worker_id,
        )
        return claim_job(self.vault, record.job_id, record.worker_id)

    def _execute(
        self, call: CompositionCall, record: StoredJob, handoff: _Handoff
    ) -> dict[str, Any]:
        """Compose in the worker thread, then record the terminal state.

        The record is finished whichever way the call ends. A failure is
        re-raised so a synchronous caller sees it exactly as before; a
        detached one is already answered and sees only the ``failed`` state.
        """
        try:
            response = call.work(on_stage=handoff.report)
        except Exception:
            self._close(call, record, None, detached=handoff.finish())
            raise
        detached = handoff.finish()
        self._close(call, record, self._settled(call, response, detached), detached)
        return response

    @staticmethod
    def _settled(
        call: CompositionCall, response: dict[str, Any], detached: bool
    ) -> dict[str, Any] | None:
        """Project *response*, recording a projection failure as ``failed``."""
        try:
            return call.settle(response, detached=detached)
        except Exception:
            logger.exception("%s job result could not be settled", call.tool)
            return None

    def _close(
        self,
        call: CompositionCall,
        record: StoredJob,
        result: dict[str, Any] | None,
        detached: bool,
    ) -> None:
        """Write the terminal record and, for a detached call, its audit entry."""
        try:
            finish_job(self.vault, record.job_id, record.worker_id, result)
        except Exception:
            logger.exception(
                "%s job %s could not be finished", call.tool, record.job_id
            )
        if detached:
            state = JobState.SUCCEEDED if result is not None else JobState.FAILED
            self._audit(call, record.job_id, AUDIT_OUTCOME_COMPLETED, state.value)

    def _audit(
        self,
        call: CompositionCall,
        job_id: str,
        outcome: str,
        state: str | None = None,
    ) -> None:
        """Append one job-lifecycle entry: the job id, and the state if known."""
        args: dict[str, Any] = {"job_id": job_id}
        if state is not None:
            args["state"] = state
        MCPAuditLog(self.vault).append(
            tool=call.tool,
            args=args,
            tier_ceiling=call.ceiling,
            consumer=call.consumer,
            outcome=outcome,
        )

    @staticmethod
    def _accepted(call: CompositionCall, job_id: str) -> dict[str, Any]:
        """Return the handle a detached call answers with."""
        return {
            "status": "accepted",
            "tool": call.tool,
            "tier_ceiling": call.ceiling.value,
            "job_id": job_id,
            "state": JobState.RUNNING.value,
        }
