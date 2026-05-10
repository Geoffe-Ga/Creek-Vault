"""Shared :class:`CheckResult` dataclass — lives in its own module to
keep the import graph acyclic (the checks under ``creek.lint.checks``
import this, and ``creek.lint.runner`` imports both).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CheckResult:
    """A single lint check's output.

    Attributes:
        name: Registry key (e.g. ``"broken-links"``).
        summary: One-line human-readable headline.
        findings: Pre-formatted markdown bullet lines.
    """

    name: str
    summary: str
    findings: list[str] = field(default_factory=list)
