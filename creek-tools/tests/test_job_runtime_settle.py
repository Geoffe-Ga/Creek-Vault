"""``JobRuntime.settle_vault_pass``: a consumer's passes keep their order (#2132)."""

from __future__ import annotations

import asyncio
from typing import Final

from creek_mcp.httpapi.job_runtime import JobRuntime

_CONSUMER: Final[str] = "adepthood"
_LONG: Final[float] = 5.0
_SHORT: Final[float] = 0.01


def test_settling_frees_the_admission_slot_before_returning() -> None:
    """Once the job is done, ``busy`` is already false when the wait returns."""

    async def _scenario() -> bool:
        runtime = JobRuntime()
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        runtime.track(_CONSUMER, "job-1", task, vault_pass=True)
        asyncio.get_running_loop().call_soon(gate.set)
        await runtime.settle_vault_pass(_CONSUMER, timeout=_LONG)
        return runtime.busy(_CONSUMER)

    assert asyncio.run(_scenario()) is False


def test_a_timed_out_wait_leaves_the_job_running() -> None:
    """The bound returns control without cancelling the job."""

    async def _scenario() -> tuple[bool, bool]:
        runtime = JobRuntime()
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        runtime.track(_CONSUMER, "job-1", task, vault_pass=True)
        await runtime.settle_vault_pass(_CONSUMER, timeout=_SHORT)
        still_running = not task.done() and runtime.busy(_CONSUMER)
        gate.set()
        await task
        return still_running, runtime.busy(_CONSUMER)

    assert asyncio.run(_scenario()) == (True, False)


def test_a_job_that_is_not_a_vault_pass_is_not_waited_for() -> None:
    """A composition job shares the admission slot but never orders a pass."""

    async def _scenario() -> bool:
        runtime = JobRuntime()
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        runtime.track(_CONSUMER, "draft-1", task)
        await asyncio.wait_for(
            runtime.settle_vault_pass(_CONSUMER, timeout=_LONG), timeout=_LONG / 5
        )
        done = task.done()
        gate.set()
        await task
        return done

    assert asyncio.run(_scenario()) is False


def test_settling_with_no_job_returns_at_once() -> None:
    """No active job means nothing to wait for."""
    runtime = JobRuntime()

    asyncio.run(
        asyncio.wait_for(
            runtime.settle_vault_pass(_CONSUMER, timeout=_LONG), _SHORT * 50
        )
    )

    assert runtime.vault_pass_by_consumer == {}
