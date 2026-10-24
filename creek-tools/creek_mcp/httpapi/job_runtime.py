"""The in-process half of the durable job machinery: who is running what (#1966).

:mod:`creek_mcp.httpapi.jobs` is the durable half — records on disk that
outlive a process. This is the half that must *not* outlive one: the worker id
stamped on every record this process accepts, the detached tasks it is
executing, and the one-active-job-per-consumer admission rule of #1605.

It was five loose attributes on the ``/v1`` app's ``state`` until the MCP
server needed the same rule for ``creek.draft`` and ``creek.author``. Lifting
them into one object is what lets both surfaces share them rather than each
growing a copy: the ``/v1`` app builds one, and an MCP server mounted in the
same process can be handed that same instance, so a consumer running an
expensive pass through one surface is refused a second through the other.

**Loop confinement.** Every collection here is read and written on the event
loop only — admission runs under :attr:`JobRuntime.admission_lock`, and the
release callback is a task done-callback, which the loop runs. Detached worker
threads never touch them, so correctness does not lean on cross-thread ``set``
operations being atomic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from uuid import uuid4


def _new_worker_id() -> str:
    """Return a fresh, process-unique worker id."""
    return uuid4().hex


@dataclass(eq=False)
class JobRuntime:
    """One process's job-execution state, shared by every surface it serves.

    Attributes:
        worker_id: Stamped on each record this process accepts. A record whose
            worker id is not this one belongs to a process that has since
            died, which is how a restart is recognised (#1605).
        admission_lock: Serialises admission so the busy check and the
            registration it guards cannot interleave.
        tasks: The detached tasks still executing. Held so they are not
            garbage-collected mid-run.
        active_job_ids: Every job id this process is executing.
        active_job_by_consumer: The one job each consumer is running.
    """

    worker_id: str = field(default_factory=_new_worker_id)
    admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    active_job_ids: set[str] = field(default_factory=set)
    active_job_by_consumer: dict[str, str] = field(default_factory=dict)

    def busy(self, consumer: str) -> bool:
        """Return whether *consumer* already has a job running here.

        Args:
            consumer: The caller's identity.

        Returns:
            ``True`` when a second long call must be refused.
        """
        return consumer in self.active_job_by_consumer

    def track(self, consumer: str, job_id: str, task: asyncio.Task[Any]) -> None:
        """Register *task* as *consumer*'s active job until it finishes.

        Call under :attr:`admission_lock`, after :meth:`busy` said no.

        Args:
            consumer: The caller the job is bound to.
            job_id: The durable record's id.
            task: The task executing the job.
        """
        self.tasks.add(task)
        self.active_job_ids.add(job_id)
        self.active_job_by_consumer[consumer] = job_id
        task.add_done_callback(partial(self._release, consumer=consumer, job_id=job_id))

    def _release(self, task: asyncio.Task[Any], *, consumer: str, job_id: str) -> None:
        """Forget a finished task and free its consumer's admission slot.

        A detached task's outcome has already been recorded on disk by the
        worker, and nobody awaits it, so its exception is retrieved here —
        otherwise the loop would log "exception was never retrieved" for a
        failure that is already reported as the job's ``failed`` state.
        """
        if not task.cancelled():
            task.exception()
        self.tasks.discard(task)
        self.active_job_ids.discard(job_id)
        if self.active_job_by_consumer.get(consumer) == job_id:
            del self.active_job_by_consumer[consumer]
