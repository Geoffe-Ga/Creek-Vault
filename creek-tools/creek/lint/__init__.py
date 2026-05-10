"""``creek lint`` — unified vault hygiene operation (FEAT-008).

This package wires the five emergence reports (paradox, unnamed,
synchronicity, compost, tags) into one named lint operation alongside
three deterministic checks (broken wiki-links, orphan compiled pages,
schema-skill size budgets). See ``docs/lint.md`` for the full spec.

Non-negotiable rules (pinned by regression tests in ``tests/test_lint.py``
and the lint skill at ``00-Creek-Meta/Skills/lint.SKILL.md``):

1. **Lint never resolves paradoxes.** Detected contradictions route to
   ``10-Liminal/Paradoxes/`` via the existing ``ParadoxDetector`` — they
   are data about a polygnostic experience, not defects to fix.
2. **Lint never auto-creates compiled pages.** Missing-compiled-page
   findings emit suggestions; the human (or ``creek compile``) decides.
3. **Lint never deletes orphan fragments.** Orphan fragments are normal;
   only orphan *compiled* pages are flagged.

The CLI surface, ``creek lint --check NAME --since DURATION``, is
implemented by :class:`LintRunner` in :mod:`creek.lint.runner`.
"""

from __future__ import annotations

from creek.lint._result import CheckResult
from creek.lint.runner import (
    ALL_CHECKS,
    DETERMINISTIC_CHECKS,
    SEMANTIC_CHECKS,
    LintReport,
    LintRunner,
    latest_lint_report,
    parse_since,
)

__all__ = [
    "ALL_CHECKS",
    "DETERMINISTIC_CHECKS",
    "SEMANTIC_CHECKS",
    "CheckResult",
    "LintReport",
    "LintRunner",
    "latest_lint_report",
    "parse_since",
]
