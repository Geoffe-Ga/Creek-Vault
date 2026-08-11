"""Vault-side fragment reader — single source of truth for loading fragments.

The classify engine, review runner, and link engine all walk
``<vault>/01-Fragments/`` looking for files that parse as Creek
fragments. They differ only in what they want back:

- the classify engine needs ``(fragment, body, raw_metadata)`` so it
  can rewrite the file with updated frontmatter while preserving any
  non-Fragment keys;
- the review runner builds a :class:`ReviewEntry` around the same
  triple plus the source path;
- the link engine just needs the :class:`Fragment` itself.

Before this module existed the three sites carried independent copies
of the same three-step validation chain (``frontmatter.load`` → check
``type == "fragment"`` → :meth:`Fragment.model_validate`). A schema
change to :class:`Fragment` or a rename of the ``type`` sentinel
would have required keeping three implementations in lock-step. The
helpers here remove that drift risk by exposing one validated load
path; callers project the result into whatever shape they need.
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003  # no issue: runtime use in type hints
from typing import Final

import frontmatter
import yaml
from pydantic import ValidationError

from creek.models import Fragment

logger = logging.getLogger(__name__)

CORPUS_SUBDIRS: Final[tuple[str, ...]] = (
    "01-Fragments",
    "09-Reference",
    "11-Other-Authors",
)
"""The vault subtrees that hold Creek fragments, in walk order.

One definition with two consumers that must never disagree:

* the Writing Desk specialists gather their evidence from these subtrees
  (:func:`creek.author.agents._load_corpus`), and
* the HARD ``privacy_compliance`` leak gate resolves each cited fragment's tier
  by walking exactly the same ones
  (:func:`creek.author.checks._resolve_cited_tiers`).

While those were two separate literals the gate fell a subtree behind the
specialists it polices, and a draft could reproduce the protected body of an
``intimate`` fragment filed under ``09-Reference`` or ``11-Other-Authors`` and
still review as ``PASS`` (#1341). Anything that reads part of the corpus reads
this tuple; a second list is a second thing to forget to update.

Index 2 is the folder :data:`creek.vault.authors.OTHER_AUTHORS_DIR` already
names. That module is deliberately *not* imported here — the dependency inside
:mod:`creek.vault` runs toward the reader, not away from it — so the equality is
pinned by ``tests/test_vault_reader.py`` rather than by an import.
"""


def try_load_fragment(
    md_file: Path,
) -> tuple[Fragment, str, dict[str, object]] | None:
    """Load a single fragment file, returning ``None`` for non-fragments.

    The function distinguishes two failure modes:

    * **Real I/O / parse failures** propagate as ``OSError``,
      ``ValueError``, or :class:`yaml.YAMLError` so the caller can
      record them on its summary's ``errors`` list.
    * **Non-fragment markdown** (no ``type: fragment`` field, or a
      schema mismatch) returns ``None`` so the caller silently skips
      it. Markdown notes coexist with fragments in a vault and aren't
      errors.

    Args:
        md_file: Path to a markdown file inside ``<vault>/01-Fragments``.

    Returns:
        ``(fragment, body, raw_metadata)`` for a valid Creek fragment,
        or ``None`` when the file is well-formed YAML but not a
        fragment.

    Raises:
        OSError: When the file cannot be read.
        ValueError: When the YAML cannot be parsed.
        yaml.YAMLError: When the YAML parser rejects the document.
    """
    post = frontmatter.load(str(md_file))
    metadata = post.metadata.copy()
    if metadata.get("type") != "fragment":
        return None
    try:
        fragment = Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None
    return fragment, post.content, metadata


def iter_vault_fragments(
    fragments_root: Path,
) -> list[tuple[Path, Fragment, str, dict[str, object]]]:
    """Walk ``fragments_root`` and yield every valid Creek fragment.

    Convenience wrapper for callers that want every loadable fragment
    without manually distinguishing real I/O failures from
    non-fragment skips. I/O failures are logged at DEBUG level and
    skipped — callers that need to surface them to the operator
    should iterate manually with :func:`try_load_fragment`.

    Args:
        fragments_root: Path to ``<vault>/01-Fragments`` (or any
            directory tree containing fragment files).

    Returns:
        Sorted list of ``(path, fragment, body, raw_metadata)``
        tuples — one per valid fragment.
    """
    if not fragments_root.exists():
        return []

    out: list[tuple[Path, Fragment, str, dict[str, object]]] = []
    for md_file in sorted(fragments_root.rglob("*.md")):
        try:
            record = try_load_fragment(md_file)
        except (OSError, ValueError, yaml.YAMLError):
            logger.debug("Skipping unreadable markdown file: %s", md_file)
            continue
        if record is None:
            continue
        fragment, body, raw = record
        out.append((md_file, fragment, body, raw))
    return out
