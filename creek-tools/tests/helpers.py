"""Shared helpers for fragment-driven test files.

Each engine test (classify, link, review) needs to seed a vault with a
real fragment file before the system under test runs. The helper used
to be copy-pasted across three test modules; centralising it here
keeps the on-disk shape of a fragment fixture in one place so
``Fragment.model_dump`` evolutions can't silently diverge between
suites.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — runtime use in type hints
from typing import TYPE_CHECKING

import frontmatter

if TYPE_CHECKING:
    from creek.models import Fragment


def write_fragment_file(
    *,
    vault: Path,
    fragment: Fragment,
    body: str,
    method: str | None = None,
    extras: dict[str, object] | None = None,
    subfolder: str = "Notes",
) -> Path:
    """Persist *fragment* under ``<vault>/01-Fragments/<subfolder>``.

    Args:
        vault: Vault root.
        fragment: Fragment metadata to persist.
        body: Markdown body the file should carry below the frontmatter.
        method: Optional ``classification_method`` to stamp (typically
            one of ``"rules" | "llm" | "manual"``).
        extras: Extra frontmatter keys to merge in alongside the
            Fragment fields.
        subfolder: Vault subfolder under ``01-Fragments``; defaults to
            ``"Notes"`` so tests don't need to know the canonical
            ``SourcePlatform`` mapping.

    Returns:
        Path to the freshly-written file.
    """
    fragments_dir = vault / "01-Fragments" / subfolder
    fragments_dir.mkdir(parents=True, exist_ok=True)

    metadata = fragment.model_dump(mode="json")
    if method is not None:
        metadata["classification_method"] = method
    if extras:
        metadata.update(extras)

    path = fragments_dir / f"{fragment.id}.md"
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return path
