"""Append-only log of whole-vault pipeline passes (#2124).

``GET /v1/pipeline/status`` answers "which stages has this document been
through?" from records that already exist rather than by inspecting fragment
frontmatter and guessing. The ingest ledger and the upload receipts beside it
(:mod:`creek.ingest.upload_receipts`) say when a document *landed*; this log
says when each later pass *ran*. Classify, its praxis pass, and every linker
stage append one line per pass, written by the MCP tools that every surface —
``/v1``, MCP and the durable job worker — calls.

**A pass is a whole-vault event, and that is what makes the join exact.**
Classify walks every fragment under ``01-Fragments`` and the linker loads every
fragment before it writes, so a pass whose ``started_at`` is at or after a
document's ingest instant ran over that document. ``started_at`` is taken
*before* the pass waits for the content-mutation lock, so within one clock
the comparison can only under-claim (a document that landed while the pass
queued is reported as not yet covered, and the next pass covers it).

That guarantee assumes the receipt and the pass were stamped by the same
clock. On one vault host they are — the upload, the synchronous pass and the
durable job worker all run on the machine that holds the vault — but a pass
and an upload stamped by two machines whose clocks disagree could be ordered
wrongly by up to that skew, and a pass that predates a document by less than
the skew could be shown as covering it. The stage trail is a status display,
never an enforcement boundary, so that bounded error is accepted rather than
engineered away.

The format mirrors :mod:`creek.audit.yield_summary`: one JSON object per line,
appended, never rewritten, not hash-chained — this is operational telemetry, and
a lost line costs a status row until the next pass, nothing more. Each line
holds a stage name, an outcome and two instants. It names no fragment, path or
title, so it needs no erasure pass of its own; ``creek purge vault`` sweeps it
with the rest of ``00-Creek-Meta/State``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_META_DIRNAME: Final[str] = "00-Creek-Meta"
"""The vault marker directory; a root without it is not a vault."""

_LOG_RELPARTS: Final[tuple[str, ...]] = (_META_DIRNAME, "State", "stage-log.jsonl")
"""Where the log lives, relative to the vault root."""

_LINK_STAGE_PREFIX: Final[str] = "linked:"
"""Every linker stage is named ``linked:<method>``."""


class PassStage(StrEnum):
    """A whole-vault pass this log records, in pipeline order.

    Attributes:
        CLASSIFIED: A classify pass (any method) ran.
        PRAXIS_SCANNED: The praxis passes inside classify: the keyword verdict
            on every fragment that pass processes, then the post-pass that
            files a ``04-Praxis`` page for every explicit fragment lacking one
            (#2122). A page that could not be written makes it ``partial``.
        LINKED_EMBEDDINGS: The ``embeddings`` linker stage ran.
        LINKED_TEMPORAL: The ``temporal`` linker stage ran.
        LINKED_EDDIES: The ``eddies`` linker stage ran.
        LINKED_THREADS: The ``threads`` linker stage ran.
    """

    CLASSIFIED = "classified"
    PRAXIS_SCANNED = "praxis_scanned"
    LINKED_EMBEDDINGS = "linked:embeddings"
    LINKED_TEMPORAL = "linked:temporal"
    LINKED_EDDIES = "linked:eddies"
    LINKED_THREADS = "linked:threads"


class StageOutcome(StrEnum):
    """How one pass ended.

    Attributes:
        OK: The pass completed with nothing left over.
        PARTIAL: The pass completed but left work for a retry (file errors,
            failed model calls, oversized clusters discarded).
        FAILED: The pass was refused before it did its work.
    """

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class StageRun:
    """One recorded pass.

    Attributes:
        stage: Which pass ran.
        outcome: How it ended.
        started_at: When it was asked to run, before it waited for any lock.
        finished_at: When it returned.
    """

    stage: PassStage
    outcome: StageOutcome
    started_at: datetime
    finished_at: datetime


def stage_log_path(vault_path: Path) -> Path:
    """Return ``<vault>/00-Creek-Meta/State/stage-log.jsonl``.

    Args:
        vault_path: Vault root.

    Returns:
        The log's path, whether or not it exists.
    """
    return vault_path.joinpath(*_LOG_RELPARTS)


def link_stage(method: str) -> PassStage | None:
    """Return the stage a linker *method* records, or ``None`` for no stage.

    Args:
        method: A linker method name.

    Returns:
        ``linked:<method>`` for a method the linker runs; ``None`` for one it
        would refuse, since a refused unknown method is not a pass.
    """
    try:
        return PassStage(f"{_LINK_STAGE_PREFIX}{method}")
    except ValueError:
        return None


def record_stage_run(
    vault_path: Path,
    *,
    stage: PassStage,
    outcome: StageOutcome,
    started_at: datetime,
    finished_at: datetime,
) -> bool:
    """Append one pass to the log, never failing the pass that ran.

    Writes only into an existing vault: a root without ``00-Creek-Meta`` is
    left untouched, so recording a refused pass against a missing vault cannot
    scaffold one.

    Args:
        vault_path: Vault root.
        stage: Which pass ran.
        outcome: How it ended.
        started_at: When it was asked to run.
        finished_at: When it returned.

    Returns:
        ``True`` when the line was appended; ``False`` when there is no vault
        or the write failed (logged as a warning, by exception type only).
    """
    if not (vault_path / _META_DIRNAME).is_dir():
        return False
    line = json.dumps(
        {
            "stage": stage.value,
            "outcome": outcome.value,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
        },
        separators=(",", ":"),
    )
    path = stage_log_path(vault_path)
    try:
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        logger.warning("stage log append failed: %s", type(exc).__name__)
        return False
    return True


def _aware(value: object) -> datetime | None:
    """Parse an ISO-8601 instant that carries an offset, or return ``None``."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parsed_run(line: str) -> StageRun | None:
    """Return one line as a run, or ``None`` when it is damaged."""
    try:
        data = json.loads(line)
        stage = PassStage(data["stage"])
        outcome = StageOutcome(data["outcome"])
    except (ValueError, KeyError, TypeError):
        return None
    started_at = _aware(data.get("started_at"))
    finished_at = _aware(data.get("finished_at"))
    if started_at is None or finished_at is None:
        return None
    return StageRun(
        stage=stage, outcome=outcome, started_at=started_at, finished_at=finished_at
    )


def read_stage_runs(vault_path: Path) -> list[StageRun]:
    """Return every readable pass in the log, in append order.

    Args:
        vault_path: Vault root.

    Returns:
        The runs. An absent or unreadable log is ``[]``; a damaged line is
        skipped and costs only itself.
    """
    try:
        text = stage_log_path(vault_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    runs = (_parsed_run(line) for line in text.splitlines() if line.strip())
    return [run for run in runs if run is not None]
