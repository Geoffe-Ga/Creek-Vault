"""Turn a classify or link tool result into stage-log lines (#2124).

The tools, not the engines, record: every surface that triggers a pass —
``/v1`` synchronously, the durable job worker, and MCP — goes through
:func:`creek_mcp.tools.classify.classify_tool` or
:func:`creek_mcp.tools.link.link_tool`, so recording there covers each of them
once. The outcome is read off the tool's own return dict, so it says exactly
what the caller was told: ``ok`` for a clean pass, ``partial`` for one that left
work for a retry, ``failed`` for a refusal met once the pass was under way.
An unknown method names no pass, and a vault config that cannot load is refused
before the vault is read, so neither records anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from creek.audit.stage_log import StageOutcome, record_stage_run

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from creek.audit.stage_log import PassStage

UNKNOWN_METHOD_PREFIX: Final[str] = "unknown method "
"""Both tools' unknown-method refusal begins with this; it records nothing."""

_LEFTOVER_KEYS: Final[tuple[str, ...]] = (
    "errors",
    "llm_call_failed",
    "oversized_discarded",
)
"""Result keys whose truthy value means the pass left work for a retry."""


def utc_now() -> datetime:
    """Return the current instant in UTC.

    Returns:
        An aware ``datetime``.
    """
    return datetime.now(UTC)


def pass_outcome(
    result: Mapping[str, Any], unrecorded_prefixes: tuple[str, ...] = ()
) -> StageOutcome | None:
    """Return how the pass behind *result* ended, or ``None`` for no pass.

    Args:
        result: A classify or link tool return dict.
        unrecorded_prefixes: Refusal-reason prefixes, beyond the unknown
            method, that are answered before the vault is read and so leave
            no trace — a vault config that cannot load (#2096) is one.

    Returns:
        ``OK``, ``PARTIAL`` or ``FAILED``; ``None`` for a refusal that names
        no pass or must leave the vault untouched.
    """
    if result.get("status") != "ok":
        reason = result.get("reason")
        skipped = (UNKNOWN_METHOD_PREFIX, *unrecorded_prefixes)
        if isinstance(reason, str) and reason.startswith(skipped):
            return None
        return StageOutcome.FAILED
    if any(result.get(key) for key in _LEFTOVER_KEYS):
        return StageOutcome.PARTIAL
    return StageOutcome.OK


def record_pass(
    vault_path: Path,
    stages: Iterable[PassStage],
    result: Mapping[str, Any],
    started_at: datetime,
    unrecorded_prefixes: tuple[str, ...] = (),
) -> None:
    """Append one stage-log line per stage the pass behind *result* covered.

    Args:
        vault_path: The vault the pass ran over.
        stages: The stages that pass constitutes.
        result: The tool's return dict.
        started_at: When the tool was entered, before any lock wait.
        unrecorded_prefixes: Refusal prefixes that record nothing; see
            :func:`pass_outcome`.
    """
    outcome = pass_outcome(result, unrecorded_prefixes)
    if outcome is None:
        return
    finished_at = utc_now()
    for stage in stages:
        record_stage_run(
            vault_path,
            stage=stage,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
        )
