"""The one definition of what a report-only pass must never do (#1769).

Three suites — reconciliation, telemetry and alarms — each ship an AST tripwire
over a module that is report-only by ruling, and each needs the same two sets:
the operations that mutate either seam, and the spellings that would reach one
without naming it. Three copies is how one of them quietly stops covering an
operation the others still do, and that is not hypothetical here: the
reconciliation copy had **already drifted**, carrying only the five provider
operations while the other two also carried the nine durable ones. A store
mutation reaches provider deletion through the worker, so the narrow copy left
a whole path uncovered in the module that reads the store most directly.

So the sets live here once and all three import them.

What these sets are *for* is worth stating, because three rounds of review have
now caught a tripwire docstring claiming more than its walk delivers. They are
an early warning over the common spellings. They are **not** a proof, and no
syntax-level check can be one — see the tripwires themselves for exactly what
each does and does not reach. The guarantee lives in behavioural assertions:
what the pass put on the wire, and whether the durable store changed at all.

This module is deliberately *not* named ``test_*``: pytest's ``python_files``
glob is ``test_*.py``, so it is imported, never collected.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

PROVIDER_MUTATIONS: Final[frozenset[str]] = frozenset(
    {"provision", "delete", "start", "stop", "delete_orphan"}
)
"""Every operation that changes something Creek is billed for."""

DURABLE_MUTATIONS: Final[frozenset[str]] = frozenset(
    {
        "submit",
        "request_delete",
        "retry",
        "record_failure",
        "complete_create",
        "complete_delete",
        "claim_next",
        "complete_key_ceremony",
        "expire_key_ceremonies",
    }
)
"""Every operation that writes the durable control plane.

Covering only the provider seam is not enough and that is the drift this
module exists to stop: ``ProvisioningWorker`` turns a durable state change into
a provider deletion, so a report-only module that reached one of these would
reach teardown by a longer route.
"""

MUTATING_OPERATIONS: Final[frozenset[str]] = PROVIDER_MUTATIONS | DURABLE_MUTATIONS
"""The union every report-only tripwire checks against."""

DYNAMIC_DISPATCH: Final[frozenset[str]] = frozenset(
    {"getattr", "setattr", "vars", "eval", "exec", "__import__", "globals"}
)
"""Builtins that would reach a mutating method without naming it.

Enumerating them is worth doing and is worth *not* overstating: a tripwire can
only see these where they are spelled as a call or an attribute. A bare name
bound to a variable first, or any other indirection, is outside every
syntax-level check, which is why the sets above are backed by the fingerprint
below rather than trusted on their own.
"""


def durable_fingerprint(database: Path) -> str:
    """Return a digest of the whole durable store, not merely its schema version.

    A schema pin (``PRAGMA user_version``) proves no *migration* ran. It says
    nothing about a row: an ``INSERT`` leaves the version untouched, names no
    method any allowlist enumerates, and puts nothing on the provider wire — so
    a raw write escaped all three guards at once. Hashing the file closes that
    by construction, whatever spelling reached it, because it asserts the
    outcome instead of the syntax.

    Returns the digest of an absent database as a fixed marker so a pass that
    creates one is caught as a change rather than as a crash.
    """
    if not database.exists():
        return "absent"
    return hashlib.sha256(database.read_bytes()).hexdigest()
