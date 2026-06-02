"""Loader for ``11-Other-Authors/<slug>/_author.md`` manifests (FEAT-041 #458).

Parses an author manifest's frontmatter into an
:class:`~creek.models.AuthorManifest`. Field-level fail-closed coercion lives on
the model; this module only handles file IO and the slug-authority rule (the
author's slug is its folder name, not the frontmatter value).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter

from creek.models import AuthorManifest

if TYPE_CHECKING:
    from pathlib import Path


def load_author_manifest(path: Path) -> AuthorManifest:
    """Load and validate the ``_author.md`` manifest at *path*.

    The author slug is taken from the parent folder name (FEAT-041 §7.1's
    slug-authority rule), overriding any ``author_slug`` in the frontmatter.
    Malformed attribution fields fail closed via the model's validators rather
    than raising.

    Args:
        path: Path to a ``11-Other-Authors/<slug>/_author.md`` file.

    Returns:
        The parsed :class:`~creek.models.AuthorManifest`.

    Raises:
        FileNotFoundError: When *path* does not point at an existing file.
    """
    if not path.is_file():
        msg = f"Author manifest not found: {path}"
        raise FileNotFoundError(msg)
    post = frontmatter.load(str(path))
    data: dict[str, object] = post.metadata.copy()
    # Slug authority: the folder name is the identity key (FEAT-041 §7.1).
    data["author_slug"] = path.parent.name
    return AuthorManifest.model_validate(data)
