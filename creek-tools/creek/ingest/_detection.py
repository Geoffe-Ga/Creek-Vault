"""Format-detection helpers shared by the ingestors.

Lives in its own module so neither :mod:`creek.ingest.documents` nor
:mod:`creek.ingest.substack` needs to import from the other to ask
"does this directory look like a Substack export?". Keeping detection
helpers cycle-free here means new format detectors can be added without
having to think about import order.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

POSTS_CSV_FILENAME = "posts.csv"
"""The canonical Substack-export metadata sidecar."""

_LEADING_DIGITS = re.compile(r"^(\d+)")
"""Capture the leading numeric component of a Substack HTML filename."""


def extract_post_id(filename: str) -> str | None:
    """Return the leading numeric component of a Substack HTML filename.

    Substack names post files ``<post_id>.<slug>.html``. We split on the
    leading digit run rather than ``"."`` because some slugs themselves
    contain ``.``, and a regex anchored at the start is the simplest
    way to read past those without misclassifying.
    """
    match = _LEADING_DIGITS.match(filename)
    if match is None:
        return None
    return match.group(1)


def is_substack_export(path: Path) -> bool:
    """Return whether *path* looks like a Substack export directory.

    A Substack export root holds a ``posts.csv`` sidecar plus at least
    one ``<post_id>.<slug>.html`` post file (Substack drops the HTML
    files either next to ``posts.csv`` or one level down inside a
    ``posts/`` subdirectory). Files and missing paths short-circuit to
    ``False`` so the check is safe to call against arbitrary CLI input.
    """
    if not path.exists() or not path.is_dir():
        return False
    if not (path / POSTS_CSV_FILENAME).is_file():
        return False
    return any(
        extract_post_id(p.name) is not None for p in path.rglob("*.html") if p.is_file()
    )
