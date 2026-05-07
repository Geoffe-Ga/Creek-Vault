"""Pre-LLM yield summary writer (FEAT-005).

After every ``creek process`` run the pipeline reports how much work the
deterministic and local-model passes (Pass 1 + Pass 2) accomplished
without ever invoking the network LLM (Pass 3). This module owns the
on-disk format: one JSONL line per run, appended to
``<vault>/00-Creek-Meta/Processing-Log/run-summary.jsonl``.

The format is deliberately not chained (unlike :class:`creek.audit.AuditLog`)
because run summaries are operational telemetry, not compliance evidence —
losing or skipping a line is recoverable (just re-run), and a hash chain
would force a global write lock on what is otherwise an append-only
stream that downstream tools want to ``tail``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pathlib import Path


_LOG_SUBPATH = ("00-Creek-Meta", "Processing-Log", "run-summary.jsonl")


class PreLLMYieldSummary(BaseModel):
    """One pipeline run's deterministic / local-model / residue counts.

    Attributes:
        run_id: Stable identifier for the pipeline run; used to correlate
            the summary line with provenance and audit entries.
        deterministic_classified: Fragments confidently classified by
            Pass 1 (rules) — including ``human_review_sources`` whose
            policy is to skip the LLM entirely.
        local_model_processed: Fragments that went through Pass 2
            (embeddings / OCR). On a normal run this equals the count of
            fragments handed to the linker; ``--no-llm`` does not change
            this number.
        residue: Fragments that the rule classifier left uncertain —
            i.e. would have been routed to the LLM if Pass 3 were
            enabled. Reported even when Pass 3 ran, so audit consumers
            can see the deterministic-pass yield for any run.
        no_llm: Whether the run was launched with ``--no-llm``. False
            for normal runs that did dispatch residue to the LLM.
        timestamp: UTC timestamp of summary emission, populated
            automatically when omitted.
    """

    run_id: str
    deterministic_classified: int
    local_model_processed: int
    residue: int
    no_llm: bool
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


def format_yield_line(summary: PreLLMYieldSummary) -> str:
    """Return the human-readable stdout line documented in FEAT-005.

    The exact wording is pinned by the FEAT and consumed verbatim by the
    audit report (FEAT-006), so the format string is intentionally
    rigid — do not localise or reorder the segments.

    Args:
        summary: The yield counts to render.

    Returns:
        A single-line string suitable for ``console.print`` or stdout.
    """
    return (
        f"Deterministic: {summary.deterministic_classified} classified | "
        f"Local-model: {summary.local_model_processed} embedded/OCR'd | "
        f"Residue: {summary.residue} (would go to LLM if Pass-3 enabled)"
    )


def yield_summary_path(vault_path: Path) -> Path:
    """Resolve the canonical path for ``run-summary.jsonl`` under *vault_path*.

    Args:
        vault_path: Vault root.

    Returns:
        ``<vault>/00-Creek-Meta/Processing-Log/run-summary.jsonl``.
    """
    return vault_path.joinpath(*_LOG_SUBPATH)


def write_yield_summary(*, vault_path: Path, summary: PreLLMYieldSummary) -> Path:
    """Append *summary* to ``run-summary.jsonl`` under *vault_path*.

    Creates the parent directory tree on demand. Each call appends one
    JSONL line — the file is read by FEAT-006's audit report by streaming
    line-by-line, so callers MUST NOT rewrite or reorder existing lines.

    Args:
        vault_path: Vault root the run was processed into.
        summary: The yield counts to persist.

    Returns:
        The absolute path of the written log file.
    """
    log_path = yield_summary_path(vault_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = summary.model_dump_json()
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return log_path
