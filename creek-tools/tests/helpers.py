"""Shared helpers for fragment-driven test files.

Each engine test (classify, link, review) needs to seed a vault with a
real fragment file before the system under test runs. The helper used
to be copy-pasted across three test modules; centralising it here
keeps the on-disk shape of a fragment fixture in one place so
``Fragment.model_dump`` evolutions can't silently diverge between
suites.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  # no issue: runtime helper signature
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


_RAW_FRAGMENT_FM = (
    '---\ntype: fragment\nid: {id}\ntitle: "{title}"\n'
    "source:\n  platform: {platform}\n  author: {author}\n{extra}---\n{body}\n"
)


def write_raw_fragment_file(
    vault: Path,
    subdir: str,
    frag_id: str,
    title: str,
    *,
    body: str = "body",
    author: str = "self",
    author_slug: str | None = None,
    platform: str = "journal",
) -> None:
    """Write a minimal fragment markdown file under *subdir* of *vault*.

    Builds the frontmatter from a literal template (not ``Fragment.model_dump``)
    so ``author`` / ``author_slug`` / ``platform`` can be set to arbitrary raw
    values — used by the medium / agent suites to seed self and other-author
    corpora (an ``author_slug`` lands the fragment under ``11-Other-Authors/``).
    For a fragment built from a :class:`~creek.models.Fragment` model, use
    :func:`write_fragment_file` instead.

    Args:
        vault: The vault root to write under.
        subdir: Vault subdirectory (e.g. ``01-Fragments/Notes`` or
            ``11-Other-Authors/<slug>``).
        frag_id: Fragment id (also the file stem).
        title: Fragment title.
        body: Fragment body text.
        author: The authorship axis (``self`` / ``other`` / ...).
        author_slug: The ``11-Other-Authors/`` slug, or ``None`` for self.
        platform: The source platform string.
    """
    folder = vault / subdir
    folder.mkdir(parents=True, exist_ok=True)
    extra = f"  author_slug: {author_slug}\n" if author_slug else ""
    (folder / f"{frag_id}.md").write_text(
        _RAW_FRAGMENT_FM.format(
            id=frag_id,
            title=title,
            platform=platform,
            author=author,
            extra=extra,
            body=body,
        ),
        encoding="utf-8",
    )
