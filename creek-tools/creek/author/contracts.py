"""Loader for medium contracts (``<medium>.MEDIUM.md``) (FEAT-041 #459).

A medium contract is a small skill file — YAML frontmatter declaring the
structure, specialist weights, citation norm, default privacy tier, and
reflection rubric, plus a budgeted markdown body of human guidance. The
Conductor loads one to drive an authoring run. Contracts deploy to
``<vault>/00-Creek-Meta/Skills/mediums/`` from the canonical templates; the
loader reads the deployed copy when present and falls back to the template so
the desk runs on a freshly scaffolded vault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter

from creek.models import MediumContract
from creek.scaffold import SKILLS_TEMPLATE_DIR

if TYPE_CHECKING:
    from pathlib import Path

#: Canonical medium-contract templates shipped with the package.
MEDIUMS_TEMPLATE_DIR = SKILLS_TEMPLATE_DIR / "mediums"

#: Deployed location of medium contracts within a vault.
_VAULT_MEDIUMS_SUBDIR = ("00-Creek-Meta", "Skills", "mediums")


def _resolve_contract_path(medium: str, vault: Path) -> Path | None:
    """Return the contract path for *medium*, preferring the vault copy.

    Args:
        medium: The medium slug.
        vault: The vault root.

    Returns:
        The deployed contract path, the canonical template path, or ``None``
        when neither exists.
    """
    deployed = vault.joinpath(*_VAULT_MEDIUMS_SUBDIR, f"{medium}.MEDIUM.md")
    if deployed.is_file():
        return deployed
    template = MEDIUMS_TEMPLATE_DIR / f"{medium}.MEDIUM.md"
    if template.is_file():
        return template
    return None


def load_medium_contract(medium: str, vault: Path) -> MediumContract:
    """Load and validate the contract for *medium*.

    Args:
        medium: The medium slug (e.g. ``research``).
        vault: The vault to look in (falls back to the canonical template).

    Returns:
        The parsed :class:`~creek.models.MediumContract`.

    Raises:
        FileNotFoundError: When no contract exists for *medium*.
    """
    path = _resolve_contract_path(medium, vault)
    if path is None:
        msg = f"No medium contract found for {medium!r}."
        raise FileNotFoundError(msg)
    post = frontmatter.load(str(path))
    data: dict[str, object] = post.metadata.copy()
    data.setdefault("medium", medium)
    return MediumContract.model_validate(data)
