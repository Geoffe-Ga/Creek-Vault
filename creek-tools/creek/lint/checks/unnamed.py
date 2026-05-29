"""Semantic check: surface fragments the classifier could not name.

The full UnnamedDigestGenerator runs as an ingestion step; lint only
reports which fragments are currently *unnamed* so the human notices
when the backlog grows or starts to cluster around a concept the
ontology is missing.

A fragment is *unnamed* when its primary frequency is
``unclassified`` — regardless of where it physically lives. Files
sitting directly under ``10-Liminal/Unnamed/`` are surfaced too, even
when they do not parse as Creek fragments, so the historical
folder-based behaviour never regresses. Results are de-duplicated.

The check **never** auto-classifies those fragments or moves them out
— that would violate liminal preservation (FEAT-008 non-negotiable
rule).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.lint._result import CheckResult
from creek.models import Frequency
from creek.vault.reader import iter_vault_fragments

_FRAGMENTS_DIR: str = "01-Fragments"
_UNNAMED_DIR: tuple[str, ...] = ("10-Liminal", "Unnamed")
_DIGESTS_FOLDER: str = "Digests"


def _unclassified_fragment_paths(vault_path: Path) -> list[Path]:
    """Collect fragments whose primary frequency is ``unclassified``.

    Walks both the fragments root and the Unnamed folder so an
    unclassified fragment is found wherever it lives.
    """
    roots = (
        vault_path / _FRAGMENTS_DIR,
        vault_path.joinpath(*_UNNAMED_DIR),
    )
    paths: list[Path] = []
    for root in roots:
        for path, fragment, _body, _raw in iter_vault_fragments(root):
            if fragment.frequency.primary == Frequency.UNCLASSIFIED:
                paths.append(path)
    return paths


def _unnamed_folder_paths(vault_path: Path) -> list[Path]:
    """Collect every markdown file physically under ``10-Liminal/Unnamed/``.

    Digest pages are excluded; everything else is surfaced even when it
    is not a parseable Creek fragment, preserving the legacy behaviour.
    """
    unnamed_dir = vault_path.joinpath(*_UNNAMED_DIR)
    if not unnamed_dir.is_dir():
        return []
    return [
        md
        for md in unnamed_dir.rglob("*.md")
        if _DIGESTS_FOLDER not in md.relative_to(unnamed_dir).parts
    ]


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Report fragments with ``unclassified`` frequency (no auto-classify).

    The ``since`` parameter is accepted for interface symmetry but
    ignored — the unnamed check is cheap and always scans the vault.
    """
    del since
    paths = set(_unclassified_fragment_paths(vault_path))
    paths.update(_unnamed_folder_paths(vault_path))
    findings = [
        f"- `{md.relative_to(vault_path)}` (kept liminal; never auto-classified)"
        for md in sorted(paths)
    ]
    summary = f"{len(findings)} fragment(s) with unclassified frequency"
    return CheckResult(name="unnamed", summary=summary, findings=findings)
