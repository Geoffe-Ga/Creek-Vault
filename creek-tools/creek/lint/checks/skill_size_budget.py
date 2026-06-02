"""Deterministic check: schema-skill files must stay under their token budget.

The lint skill (``00-Creek-Meta/Skills/lint.SKILL.md``) declares
``Budget: ≤1500 tokens``. We approximate token count as word count
(roughly 1 token per word for English prose — close enough for a
deterministic budget check that does not need an LLM tokenizer in the
hot path). The root ``AGENTS.md`` is also budgeted at ≤3000 tokens
per the same skill.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.lint._result import CheckResult

SKILL_BUDGET_WORDS: int = 1500
"""Per-skill word budget (proxy for ≤1500 tokens)."""

AGENTS_BUDGET_WORDS: int = 3000
"""Root ``AGENTS.md`` word budget (proxy for ≤3000 tokens)."""

_SKILLS_DIR: tuple[str, ...] = ("00-Creek-Meta", "Skills")


def _word_count(path: Path) -> int:
    """Count whitespace-delimited words in *path*."""
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").split())
    except OSError:
        return 0


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Flag schema-skill files that exceed their declared budget."""
    del since
    findings: list[str] = []
    skills_dir = vault_path.joinpath(*_SKILLS_DIR)
    if skills_dir.is_dir():
        # Schema skills (``*.SKILL.md``) and medium contracts
        # (``mediums/*.MEDIUM.md``) share the same per-file budget (FEAT-041 §5).
        skill_files = [
            *skills_dir.glob("*.SKILL.md"),
            *skills_dir.glob("mediums/*.MEDIUM.md"),
        ]
        for skill in sorted(skill_files):
            words = _word_count(skill)
            if words > SKILL_BUDGET_WORDS:
                findings.append(
                    f"- `{skill.relative_to(vault_path)}`: "
                    f"{words} words (budget: {SKILL_BUDGET_WORDS})",
                )

    agents_md = vault_path / "AGENTS.md"
    if agents_md.exists():
        words = _word_count(agents_md)
        if words > AGENTS_BUDGET_WORDS:
            findings.append(
                f"- `AGENTS.md`: {words} words (budget: {AGENTS_BUDGET_WORDS})",
            )

    summary = f"{len(findings)} skill file(s) over budget"
    return CheckResult(name="skill-size", summary=summary, findings=findings)
