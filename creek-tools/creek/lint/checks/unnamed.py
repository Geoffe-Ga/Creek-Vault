"""Semantic check: surface the size of ``10-Liminal/Unnamed/``.

The full UnnamedDigestGenerator runs as an ingestion step; lint only
reports how many fragments currently sit in the Unnamed folder so the
human notices when the backlog grows. It **never** auto-classifies
those fragments or moves them out — that would violate liminal
preservation (FEAT-008 non-negotiable rule).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.lint._result import CheckResult

_UNNAMED_DIR: tuple[str, ...] = ("10-Liminal", "Unnamed")
_DIGESTS_FOLDER: str = "Digests"


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Report fragment counts under ``10-Liminal/Unnamed/`` (no auto-classify)."""
    del since
    unnamed_dir = vault_path.joinpath(*_UNNAMED_DIR)
    if not unnamed_dir.is_dir():
        return CheckResult(
            name="unnamed",
            summary="0 unnamed fragments (no Unnamed folder yet)",
            findings=[],
        )
    fragments = [
        md
        for md in sorted(unnamed_dir.rglob("*.md"))
        if _DIGESTS_FOLDER not in md.relative_to(unnamed_dir).parts
    ]
    findings = [
        f"- `{md.relative_to(vault_path)}` (kept liminal; never auto-classified)"
        for md in fragments
    ]
    summary = f"{len(fragments)} fragment(s) sitting in `10-Liminal/Unnamed/`"
    return CheckResult(name="unnamed", summary=summary, findings=findings)
