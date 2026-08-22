"""What the ``/v1`` request deadline can and cannot bind (#1109).

Every ``/v1`` handler does its blocking work in a worker thread, and
:class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware` is an
``anyio.fail_after`` cancel scope evaluated on the event loop. Whether the
deadline is real therefore depends on one keyword nobody was passing.

``anyio.to_thread.run_sync`` defaults to ``abandon_on_cancel=False``, which
**defers** cancellation until the worker returns. Measured on this stack, with
a 0.25 s deadline against a 1.2 s tool::

    status  : 200
    elapsed : 1.223s
    body    : {"status":"ok", ..., "action":"created"}

Not a late ``503`` — the real answer, late. Ten of the eleven published
operations had that shape, so the published "per-request timeout: 30 s"
row was false for all of them, and the one green test that appeared to cover
it drove a pure-``async`` handler no production route resembles.

**The decision, split by route class rather than applied uniformly.**

* :func:`read_off_loop` — for a route that mutates no vault state.
  ``abandon_on_cancel=True``, so the deadline genuinely fires and the caller
  gets its ``503`` on time. The abandoned thread keeps running to completion,
  which is fine precisely because there is nothing for a half-finished read to
  tear.
* :func:`write_off_loop` — for a route that mutates the vault.
  ``abandon_on_cancel=False``, deliberately: abandoning a detached write
  thread while telling the client ``503`` is the torn-vault-plus-retry hazard
  #1109 was filed about, and it is a hazard that does **not** exist today.
  Introducing it in the name of fixing it would be the worst available trade.
  Consistency beats boundedness here, and ``docs/api.md`` says so rather than
  publishing a deadline these routes cannot keep.

Neither helper frees the worker thread early — nothing in Python can cancel a
running thread — so neither bounds resource use. What :func:`read_off_loop`
bounds is how long the *caller* is made to wait, which is the whole of what a
``503 temporarily_unavailable`` promises.

Named rather than passed as a bare keyword so the choice is visible and
greppable at every call site: ``tests/test_v1_api_hardening.py`` asserts that
no module in this package imports ``run_in_threadpool`` any more, which is what
stops the next route from inheriting a deadline decision by omission.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from anyio.to_thread import run_sync

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")

READ_ABANDONS_ON_CANCEL: bool = True
"""A read may be abandoned at the deadline: there is nothing to tear."""

WRITE_ABANDONS_ON_CANCEL: bool = False
"""A write may not: a half-applied vault mutation is worse than a late answer."""


async def read_off_loop(func: Callable[..., _T], *args: object) -> _T:
    """Run a **non-mutating** *func* in a worker thread, under a real deadline.

    Args:
        func: The blocking callable.
        *args: Positional arguments for *func*. Keyword arguments are
            deliberately unsupported — every call site here passes positionals,
            and a ``functools.partial`` shim would hide which callable is
            actually being dispatched from the reader and from the traceback.

    Returns:
        Whatever *func* returned.
    """
    return await run_sync(func, *args, abandon_on_cancel=READ_ABANDONS_ON_CANCEL)


async def write_off_loop(func: Callable[..., _T], *args: object) -> _T:
    """Run a **vault-mutating** *func* in a worker thread, to completion.

    The deadline does not bind this call, on purpose. See the module docstring:
    the caller waits, and gets the true answer, rather than being told ``503``
    about a write that in fact landed.

    Args:
        func: The blocking callable.
        *args: Positional arguments for *func*.

    Returns:
        Whatever *func* returned.
    """
    return await run_sync(func, *args, abandon_on_cancel=WRITE_ABANDONS_ON_CANCEL)
