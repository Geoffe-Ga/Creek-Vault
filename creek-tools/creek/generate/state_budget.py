"""Size-budget gate for the ``creek state`` audit report (FEAT-007).

The audit report at ``00-Creek-Meta/State/latest.md`` is the session-start
context for CrawDad and Claude Code: it must fit in a single Claude
context window. FEAT-007 pins that contract as a budget that
``./scripts/check-all.sh`` enforces.

The budget is **50,000 tokens** (~200KB at four characters per token, a
conservative English approximation that needs no model download).

A budget failure is *not* a cap to raise. It is a signal that the
compiled layer is fragmenting and the fix is consolidation — ``creek
lint``'s synchronicity and tag-cluster checks point at the work.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

SIZE_BUDGET_TOKENS: int = 50_000
"""Maximum allowed token count for ``latest.md`` (FEAT-007)."""

CHARS_PER_TOKEN: int = 4
"""Heuristic English-text characters per token.

Anthropic's published rule of thumb for English text is roughly 3.5-4
characters per token. Four keeps the gate forgiving: a 50K-token budget
under this estimator corresponds to roughly 200KB on disk, which is
well under any current Claude context window.
"""

_TOP_SECTION_REPORT_LIMIT: int = 3
"""Number of largest sections reported in the failure summary."""

_CONSOLIDATE_HINT: str = (
    "A failing budget means the compiled layer is fragmenting; "
    "consolidate via `creek lint` rather than raising the cap."
)


def estimate_tokens(text: str) -> int:
    """Estimate the token count of *text* under :data:`CHARS_PER_TOKEN`.

    Returns zero for empty input. The estimator is intentionally simple:
    the budget gate only needs to catch order-of-magnitude regressions,
    not tokenize precisely.
    """
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class BudgetResult:
    """Outcome of a size-budget check against ``latest.md``.

    Attributes:
        ok: ``True`` when the file is under :data:`SIZE_BUDGET_TOKENS`.
        tokens: Estimated token count of the file.
        budget: The active budget (always :data:`SIZE_BUDGET_TOKENS`).
        largest_sections: Up to three ``(header, tokens)`` pairs, ordered
            by descending size. Used by failure messages to name the
            sections that grew. An immutable tuple so ``frozen=True``
            actually means frozen — a ``list`` here would still permit
            mutation via ``.append``.
        message: Human-readable summary suitable for shell or CI output.
    """

    ok: bool
    tokens: int
    budget: int = SIZE_BUDGET_TOKENS
    largest_sections: tuple[tuple[str, int], ...] = ()
    message: str = ""


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split *text* into ``(header, body)`` pairs at ``## `` boundaries.

    Content before the first ``## `` header is grouped under the
    ``"(preamble)"`` synthetic header so it still contributes to the
    largest-sections ranking.
    """
    sections: list[tuple[str, str]] = []
    current_header = "(preamble)"
    current_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            sections.append((current_header, "".join(current_lines)))
            current_header = line.rstrip("\n")
            current_lines = []
        else:
            current_lines.append(line)
    sections.append((current_header, "".join(current_lines)))
    return sections


def _rank_sections(text: str) -> list[tuple[str, int]]:
    """Return ``(header, tokens)`` pairs ordered by descending token count."""
    sized = [(header, estimate_tokens(body)) for header, body in _split_sections(text)]
    sized.sort(key=lambda pair: (-pair[1], pair[0]))
    return sized


def check_budget(latest_path: Path) -> BudgetResult:
    """Return a :class:`BudgetResult` for the ``latest.md`` at *latest_path*.

    A missing file is treated as a pass with zero tokens — CI runs do
    not always have a vault, and the gate must not block them.
    """
    if not latest_path.exists():
        return BudgetResult(
            ok=True,
            tokens=0,
            message=f"No state report at {latest_path}; budget gate skipped.",
        )
    text = latest_path.read_text(encoding="utf-8")
    tokens = estimate_tokens(text)
    ranked = tuple(_rank_sections(text)[:_TOP_SECTION_REPORT_LIMIT])
    if tokens <= SIZE_BUDGET_TOKENS:
        return BudgetResult(
            ok=True,
            tokens=tokens,
            largest_sections=ranked,
            message=(
                f"State report {tokens} tokens / budget {SIZE_BUDGET_TOKENS} (ok)."
            ),
        )
    breakdown = ", ".join(f"{header}={count}" for header, count in ranked)
    overage = tokens - SIZE_BUDGET_TOKENS
    return BudgetResult(
        ok=False,
        tokens=tokens,
        largest_sections=ranked,
        message=(
            f"State report exceeds budget by {overage} tokens "
            f"({tokens} / {SIZE_BUDGET_TOKENS}). Largest sections: "
            f"{breakdown}. {_CONSOLIDATE_HINT}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """Console entry point: ``python -m creek.generate.state_budget <path>``.

    Prints the message and returns 0 when the file is within budget or
    missing, 1 otherwise.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(
            "usage: python -m creek.generate.state_budget <path-to-latest.md>\n",
        )
        return 2
    result = check_budget(Path(args[0]))
    stream = sys.stdout if result.ok else sys.stderr
    stream.write(result.message + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised by the shell wrapper
    raise SystemExit(main())
