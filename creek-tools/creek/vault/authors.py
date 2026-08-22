"""Loader for ``11-Other-Authors/<slug>/_author.md`` manifests (FEAT-041 #458).

Parses an author manifest's frontmatter into an
:class:`~creek.models.AuthorManifest`. Field-level fail-closed coercion lives on
the model; this module only handles file IO and the slug-authority rule (the
author's slug is its folder name, not the frontmatter value).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from creek.models import AuthorManifest
from creek.vault.reader import load_post_or_raise

logger = logging.getLogger(__name__)

OTHER_AUTHORS_DIR = "11-Other-Authors"
"""Top-level vault folder governing borrowed, non-owner author content."""


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
    post = load_post_or_raise(path)
    data: dict[str, object] = post.metadata.copy()
    # Slug authority: the folder name is the identity key (FEAT-041 §7.1).
    data["author_slug"] = path.parent.name
    return AuthorManifest.model_validate(data)


def load_author_manifest_or_default(vault_path: Path, slug: str) -> AuthorManifest:
    """Load ``<vault>/11-Other-Authors/<slug>/_author.md``, failing closed (#470).

    Resolves the manifest governing borrowed content under *slug*. When the
    ``_author.md`` file is missing or cannot be parsed/validated, this returns a
    default :class:`~creek.models.AuthorManifest` for *slug* — which already
    defaults to ``human_source`` / ``voice_weight=0.0`` /
    ``representativeness="reference"`` — so a borrowed fragment can never inherit
    voice influence from an absent or corrupt manifest (fail-closed to 0.0).

    Args:
        vault_path: Root of the Obsidian vault.
        slug: The ``11-Other-Authors/<slug>/`` folder name.

    Returns:
        The parsed manifest, or a safe default manifest for *slug*.
    """
    path = Path(vault_path) / OTHER_AUTHORS_DIR / slug / "_author.md"
    try:
        return load_author_manifest(path)
    except FileNotFoundError:
        logger.warning(
            "Author manifest missing for slug %r at %s; "
            "failing closed to voice_weight=0.0 / human_source.",
            slug,
            path,
        )
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        logger.warning(
            "Author manifest unreadable for slug %r at %s (%s); "
            "failing closed to voice_weight=0.0 / human_source.",
            slug,
            path,
            exc,
        )
    return AuthorManifest(author_slug=slug)
