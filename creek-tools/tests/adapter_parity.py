"""HTTP<->MCP behavioural parity harness for the ``/v1`` verticals (#1075).

Epic #1071 rests on one claim: ``/v1`` is an *adapter* over the same tools the
MCP surface calls, not a second implementation. A behavioural suite per adapter
cannot check that claim — two suites written from the same acceptance criteria
pass on the day they are written and then drift, which is the whole failure the
epic exists to stop.

So this module compares the two adapters against each other. It does **not**
compare envelopes: the wire response and the tool's return dict are deliberately
different shapes, and demanding byte-identity would only force one of them to
stop being canonical. What it compares is the *behavioural outcome* — did the
call succeed, which wire error code did the refusal carry, which facts came back
(action, fragment identity, tally), and what did the call write to
``MCPAuditLog``.

**The refusal comparison is not circular.** The MCP side is classified with the
adapter's own production reason-to-code mapping, and the HTTP side is read off
the wire. They agree only if the route actually routes the tool's refusal
through that mapping. A route that grew a privacy check of its own, refused
before entering the tool, or swallowed a refusal into a plausible success would
diverge here even though both adapters' own suites stayed green.

**Audit records are projected, not compared whole.** ``timestamp``,
``entry_hash`` and ``prev_hash`` differ between two runs of the identical call by
construction. :data:`AUDIT_FIELDS` names the fields that carry meaning — the
tool, the ceiling it ran at, who ran it, the tier it created and which fragments
it touched — and a divergence in any of those is a real divergence.

Nothing in this module asserts anything about *correctness*; it asserts only
that two adapters agree. The per-vertical modules own the scenario tables and
the correctness assertions.

Named ``tests/adapter_parity.py`` rather than the ``tests/helpers/`` package
#1075 asked for: ``tests/helpers.py`` already exists as a module, and a package
of that name shadows it, so ``tests/test_book_report_medium.py`` stops
importing. The flat spelling also matches ``tests/v1_api_support.py``, the
suite's other shared, deliberately-not-collected module.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

from creek_mcp.audit import MCP_AUDIT_RELPATH

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    import httpx

    from creek_mcp.api.models import ErrorCode

OK_STATUS: Final[int] = 200
"""The one success status ``/v1`` returns. Spelled, not imported.

The published status set is contract; a helper that read it back out of the
implementation could not notice the implementation changing it.
"""

TOOL_OK: Final[str] = "ok"
"""The ``status`` value an MCP tool returns on success."""

AUDIT_FIELDS: Final[tuple[str, ...]] = (
    "tool",
    "tier_ceiling",
    "consumer",
    "created_tier",
    "affected_fragment_ids",
)
"""The audit fields two adapters must agree on for the same scenario.

``timestamp``, ``entry_hash`` and ``prev_hash`` are excluded because they differ
between two runs of the *same* call, so including them would make every
comparison fail for a reason that is not a divergence. ``args_summary`` is
excluded because it carries the caller's own arguments, which the two adapters
legitimately spell differently (a path segment versus a keyword) — the
behavioural facts it would contribute are already carried by the projected
fields and by the success payload.
"""


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one adapter did, in vocabulary neither adapter owns.

    Attributes:
        kind: ``"ok"`` for a success, ``"error"`` for anything else.
        code: The wire :class:`~creek_mcp.api.models.ErrorCode` value for a
            refusal, and ``None`` for a success.
        payload: The success facts the scenario cares about, projected onto the
            keys it named. Empty for a refusal — a refusal that carried facts
            would be an oracle, and there is nothing to compare.
        audit: Every audit record the call appended, projected onto
            :data:`AUDIT_FIELDS`, in write order.
    """

    kind: str
    code: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    audit: tuple[Mapping[str, Any], ...] = ()


_FRAGMENT_ID: Final[re.Pattern[str]] = re.compile(r"\Afrag-[0-9a-f]{12}\Z")
"""A vault-side fragment id: the ``frag-`` prefix and twelve hex digits."""


def _alias_value(value: Any, aliases: dict[str, str]) -> Any:
    """Replace fragment ids in *value* with stable, first-seen-order aliases.

    Args:
        value: A projected field value.
        aliases: The alias map accumulated so far, mutated in place.

    Returns:
        *value* with every fragment id replaced by ``fragment#N``.
    """
    if isinstance(value, str) and _FRAGMENT_ID.match(value):
        return aliases.setdefault(value, f"fragment#{len(aliases)}")
    if isinstance(value, list):
        return [_alias_value(item, aliases) for item in value]
    return value


def alias_fragment_ids(outcome: Outcome) -> Outcome:
    """Return *outcome* with its fragment ids replaced by positional aliases.

    Two adapters running the identical scenario against two vaults mint two
    different fragment ids: the id is derived from where the fragment landed,
    and the two vaults are different directories. Comparing the raw digests
    would therefore make every success look like a divergence — and dropping
    the field would give up the criterion it exists to serve, that HTTP and MCP
    agree on *fragment identity*.

    So the ids are replaced by ``fragment#0``, ``fragment#1``, … in
    first-appearance order across the payload and then the audit trail. That
    preserves exactly the identity facts worth comparing — the id in the
    response is the id in the audit record; two calls returned the same id, or
    different ones — while discarding the vault-specific digest that carries no
    behavioural meaning.

    Args:
        outcome: The outcome to normalise.

    Returns:
        The normalised outcome.
    """
    aliases: dict[str, str] = {}
    payload = {
        name: _alias_value(value, aliases) for name, value in outcome.payload.items()
    }
    audit = tuple(
        {name: _alias_value(value, aliases) for name, value in record.items()}
        for record in outcome.audit
    )
    return replace(outcome, payload=payload, audit=audit)


def audit_trail(vault: Path) -> tuple[Mapping[str, Any], ...]:
    """Return every ``mcp.jsonl`` record under *vault*, projected and in order.

    Args:
        vault: The vault root.

    Returns:
        One projected record per appended entry. An absent log is an empty
        tuple rather than an error: "this call audited nothing" is a legitimate
        — and frequently asserted — outcome.
    """
    log = vault / MCP_AUDIT_RELPATH
    if not log.exists():
        return ()
    return tuple(
        {name: json.loads(line).get(name) for name in AUDIT_FIELDS}
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def http_outcome(
    response: httpx.Response, vault: Path, *, keys: Sequence[str]
) -> Outcome:
    """Return the :class:`Outcome` of one ``/v1`` call.

    Args:
        response: The response under test.
        vault: The vault the app was built over, read for its audit trail.
        keys: The success-body fields this scenario compares.

    Returns:
        The normalised outcome.
    """
    body: dict[str, Any] = response.json()
    audit = audit_trail(vault)
    if response.status_code != OK_STATUS:
        return Outcome(kind="error", code=str(body.get("code")), audit=audit)
    return alias_fragment_ids(
        Outcome(
            kind="ok",
            payload={name: body.get(name) for name in keys},
            audit=audit,
        )
    )


def mcp_outcome(
    result: Mapping[str, Any],
    vault: Path,
    *,
    keys: Sequence[str],
    classify: Callable[[str], ErrorCode],
    success_statuses: Sequence[str] = (TOOL_OK,),
) -> Outcome:
    """Return the :class:`Outcome` of one MCP tool call.

    Args:
        result: The tool's return dict.
        vault: The vault the tool ran against, read for its audit trail.
        keys: The success fields this scenario compares.
        classify: The adapter's own production reason-to-code mapping. Passed
            in rather than imported so this harness stays vertical-agnostic —
            and so a route that classified a refusal differently from the
            mapping it publishes shows up as a divergence here.
        success_statuses: Every tool ``status`` that is a success rather than a
            refusal. Defaults to ``("ok",)``; ``reflect_tool`` also answers
            ``empty`` and ``escalate``, both of which are ``200`` on the wire —
            an escalation in particular *must* not be an error, or a person in
            acute distress lands in a client's error path.

    Returns:
        The normalised outcome.
    """
    audit = audit_trail(vault)
    if result.get("status") not in success_statuses:
        return Outcome(
            kind="error",
            code=classify(str(result.get("reason", ""))).value,
            audit=audit,
        )
    return alias_fragment_ids(
        Outcome(
            kind="ok",
            payload={name: result.get(name) for name in keys},
            audit=audit,
        )
    )


def assert_parity(scenario: str, over_http: Outcome, over_mcp: Outcome) -> None:
    """Fail unless the two adapters behaved identically in *scenario*.

    Args:
        scenario: The scenario's name, so a failure names which row diverged
            rather than only which field.
        over_http: The outcome observed through ``/v1``.
        over_mcp: The outcome observed through the MCP tool.

    Raises:
        AssertionError: When the two outcomes differ in any compared field.
    """
    assert over_http == over_mcp, (
        f"HTTP<->MCP divergence in scenario {scenario!r}: "
        f"http={over_http!r} mcp={over_mcp!r}"
    )
