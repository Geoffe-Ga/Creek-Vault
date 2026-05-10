"""Semantic check: wrap :class:`creek.generate.paradox.ParadoxDetector`.

Lint **never** resolves paradoxes. Detected pairs are routed to
``10-Liminal/Paradoxes/`` via the existing
:meth:`~creek.generate.paradox.ParadoxDetector.create_paradox_note`
helper — the wrapper here only counts and summarises.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

import frontmatter
from pydantic import ValidationError

from creek.generate.paradox import ParadoxDetector
from creek.lint._result import CheckResult
from creek.models import Fragment

_FRAGMENT_DIRS: tuple[str, ...] = ("01-Fragments", "10-Liminal")


def _load_fragments(vault_path: Path) -> list[Fragment]:
    """Best-effort load of every parseable Fragment in the fragment dirs."""
    fragments: list[Fragment] = []
    for sub in _FRAGMENT_DIRS:
        root = vault_path / sub
        if not root.is_dir():
            continue
        for md_file in root.rglob("*.md"):
            try:
                post = frontmatter.load(str(md_file))
                fragments.append(Fragment.model_validate(post.metadata))
            except (OSError, ValueError, ValidationError):
                continue
    return fragments


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Detect contradiction pairs without resolving any of them."""
    del since  # ParadoxDetector does not support incremental scans today
    fragments = _load_fragments(vault_path)
    paradoxes = ParadoxDetector().detect_paradoxes(fragments)
    findings = [
        f"- {pair.contradiction_type}: "
        f"`{pair.fragment_ids[0]}` ↔ `{pair.fragment_ids[1]}` "
        f"(routed to `10-Liminal/Paradoxes/`)"
        for pair in paradoxes
    ]
    summary = (
        f"{len(paradoxes)} paradox(es) detected; "
        f"all routed to `10-Liminal/Paradoxes/` (never resolved)"
    )
    return CheckResult(name="paradox", summary=summary, findings=findings)
