"""Semantic check: wrap :class:`creek.generate.synchronicity.SynchronicityDetector`.

Today's :class:`SynchronicityDetector` is fed by resonance pairs that
the linking pass has already computed. Re-running embeddings inside
lint would be expensive and produce no extra signal, so the wrapper
walks any pre-computed synchronicities already written to
``10-Liminal/Synchronicities/`` and reports the count. Future work
(when the linker exposes an incremental API) can replace this with a
live scan.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

import frontmatter

from creek.lint._result import CheckResult

_SYNC_DIR: tuple[str, ...] = ("10-Liminal", "Synchronicities")


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Report recorded synchronicities (no new resonances are computed)."""
    del since
    sync_dir = vault_path.joinpath(*_SYNC_DIR)
    findings: list[str] = []
    if sync_dir.is_dir():
        for note in sorted(sync_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(note))
            except (OSError, ValueError):
                continue
            if post.get("type") != "synchronicity":
                continue
            sync_id = str(post.get("id") or note.stem)
            findings.append(
                f"- `{sync_id}` (recorded in `10-Liminal/Synchronicities/`)"
            )
    summary = f"{len(findings)} recorded synchronicity note(s)"
    return CheckResult(name="synchronicity", summary=summary, findings=findings)
