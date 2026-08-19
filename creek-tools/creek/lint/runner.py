"""Orchestrator for ``creek lint``: parse ``--since``, dispatch checks, render.

The runner is intentionally thin. Each check lives under
``creek/lint/checks/<name>.py`` and exposes a single ``run(vault_path,
*, since=None) -> CheckResult`` callable. The runner stitches the
results into a :class:`LintReport`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from creek.lint.checks import (
    broken_links,
    orphan_compiled,
    skill_size_budget,
)
from creek.lint.checks import (
    compost as compost_check,
)
from creek.lint.checks import (
    draft_grounding as draft_grounding_check,
)
from creek.lint.checks import (
    paradox as paradox_check,
)
from creek.lint.checks import (
    synchronicity as synchronicity_check,
)
from creek.lint.checks import (
    tags as tags_check,
)
from creek.lint.checks import (
    unnamed as unnamed_check,
)
from creek.lint.checks import (
    unparseable as unparseable_check,
)
from creek.lint.checks import (
    voice_fidelity as voice_fidelity_check,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.lint._result import CheckResult


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------


class _CheckCallable(Protocol):
    """Signature every check module must implement."""

    def __call__(
        self,
        vault_path: Path,
        *,
        since: datetime | None = None,
    ) -> CheckResult: ...


DETERMINISTIC_CHECKS: tuple[str, ...] = (
    "broken-links",
    "orphan-compiled",
    "skill-size",
    "tags",
    "compost",
    "draft-grounding",
    "voice-fidelity",
    "unparseable",
)
"""Cheap checks that always run by default (link graph + frontmatter only)."""

SEMANTIC_CHECKS: tuple[str, ...] = (
    "paradox",
    "synchronicity",
    "unnamed",
)
"""Embedding / LLM-driven checks; require ``--full`` or ``--since`` by default."""

ALL_CHECKS: tuple[str, ...] = DETERMINISTIC_CHECKS + SEMANTIC_CHECKS

_REGISTRY: dict[str, _CheckCallable] = {
    "broken-links": broken_links.run,
    "orphan-compiled": orphan_compiled.run,
    "skill-size": skill_size_budget.run,
    "tags": tags_check.run,
    "compost": compost_check.run,
    "draft-grounding": draft_grounding_check.run,
    "voice-fidelity": voice_fidelity_check.run,
    "paradox": paradox_check.run,
    "synchronicity": synchronicity_check.run,
    "unnamed": unnamed_check.run,
    "unparseable": unparseable_check.run,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LintReport:
    """Consolidated lint output.

    Attributes:
        results: One :class:`CheckResult` per executed check.
        since: Echo of the original ``--since`` flag (None when full).
        today: Date used to compose the report filename and header.
    """

    results: list[CheckResult]
    since: str | None
    today: date

    def render(self) -> str:
        """Render the report as a single markdown document."""
        lines: list[str] = [
            f"# Creek lint report — {self.today.isoformat()}",
            "",
            "## Summary",
            "",
            f"- Checks run: {', '.join(r.name for r in self.results) or 'none'}",
            f"- Window: {self.since or 'full vault'}",
            "",
        ]
        for result in self.results:
            lines.extend(
                (
                    f"## {result.name}",
                    "",
                    f"_{result.summary}_",
                    "",
                )
            )
            if result.findings:
                lines.extend(result.findings)
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^(\d+)(d|w|mo)$")
_DURATION_DAYS: dict[str, int] = {"d": 1, "w": 7, "mo": 30}


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """Parse a ``--since`` duration string into a cut-off datetime.

    Args:
        text: Duration spec (``7d``, ``1w``, ``1mo``, ``30d``).
        now: Reference "now" for the calculation. Defaults to the
            current UTC time.

    Returns:
        The cut-off datetime; events after this are considered "in window".

    Raises:
        ValueError: If *text* is not one of the documented forms.
    """
    match = _DURATION_RE.match(text.strip())
    if match is None:
        msg = f"Invalid --since duration {text!r}; expected e.g. 7d, 1w, 1mo, 30d."
        raise ValueError(msg)
    amount = int(match.group(1))
    unit = match.group(2)
    days = amount * _DURATION_DAYS[unit]
    reference = now or datetime.now(tz=UTC)
    return reference - timedelta(days=days)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_PROCESSING_LOG = ("00-Creek-Meta", "Processing-Log")


class LintRunner:
    """Dispatch lint checks against a vault and render a consolidated report.

    Attributes:
        vault_path: Root of the Obsidian vault.
        since: Optional cut-off datetime; passed through to each check.
        since_text: Original duration spec, kept for the report header.
        today: Date used to compose the report filename and header.
    """

    def __init__(
        self,
        vault_path: Path,
        *,
        since: datetime | None = None,
        since_text: str | None = None,
        today: date | None = None,
    ) -> None:
        """Initialise the runner.

        Args:
            vault_path: Root of the Obsidian vault.
            since: Pre-parsed cut-off datetime, or ``None`` for full vault.
            since_text: Original ``--since`` text for the report header.
            today: Date stamp used for the report filename. Defaults to
                today (UTC) so runs in the same day overwrite each other.
        """
        self.vault_path = vault_path
        self.since = since
        self.since_text = since_text
        self.today = today or datetime.now(tz=UTC).date()

    def run(self, checks: list[str] | None = None) -> LintReport:
        """Execute the requested checks and return a :class:`LintReport`.

        Args:
            checks: Explicit list of check names, or ``None`` to use the
                default set. The default runs all deterministic checks
                plus semantic checks whenever :attr:`since` is set.

        Returns:
            The consolidated :class:`LintReport`.

        Raises:
            ValueError: If *checks* contains an unknown name.
        """
        selected = self._resolve_checks(checks)
        results = [
            _REGISTRY[name](self.vault_path, since=self.since) for name in selected
        ]
        return LintReport(results=results, since=self.since_text, today=self.today)

    def write(self, report: LintReport) -> Path:
        """Write *report* to ``00-Creek-Meta/Processing-Log/lint-<date>.md``."""
        log_dir = self.vault_path.joinpath(*_PROCESSING_LOG)
        log_dir.mkdir(parents=True, exist_ok=True)
        target = log_dir / f"lint-{report.today.isoformat()}.md"
        target.write_text(report.render(), encoding="utf-8")
        return target

    def _resolve_checks(self, checks: list[str] | None) -> list[str]:
        """Decide which checks to run.

        Explicit ``--check`` overrides everything; otherwise the default
        set is all deterministic checks, augmented with semantic checks
        whenever :attr:`since` is set.
        """
        if checks is not None:
            unknown = [name for name in checks if name not in _REGISTRY]
            if unknown:
                msg = f"unknown check(s): {', '.join(unknown)}"
                raise ValueError(msg)
            ordering = {name: idx for idx, name in enumerate(ALL_CHECKS)}
            return sorted(set(checks), key=lambda c: ordering[c])
        default = list(DETERMINISTIC_CHECKS)
        if self.since is not None:
            default.extend(SEMANTIC_CHECKS)
        return default


# ---------------------------------------------------------------------------
# Read-back helper for `creek state` integration
# ---------------------------------------------------------------------------


def latest_lint_report(vault_path: Path) -> str | None:
    """Return the most recent ``lint-<date>.md`` body, if any.

    Used by :mod:`creek.generate.state` so the audit report can echo
    the latest lint findings (FEAT-008 acceptance criterion).
    """
    log_dir = vault_path.joinpath(*_PROCESSING_LOG)
    if not log_dir.is_dir():
        return None
    candidates = sorted(log_dir.glob("lint-*.md"))
    if not candidates:
        return None
    return candidates[-1].read_text(encoding="utf-8")
