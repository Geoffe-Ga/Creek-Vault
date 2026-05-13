"""Vault scaffolding helpers for ``creek init`` and ``creek skills sync``.

FEAT-019 separates the *repository* (toolchain + canonical material)
from the *user's vault* (personal content). The repo ships canonical
templates under :data:`TEMPLATES_DIR`; this module materialises them
into a user-chosen vault path while preserving any pre-existing user
data.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
VAULT_TEMPLATE_DIR = TEMPLATES_DIR / "vault"
SKILLS_TEMPLATE_DIR = TEMPLATES_DIR / "skills"
AGENTS_TEMPLATE_FILE = TEMPLATES_DIR / "AGENTS.md"

# The ontology spec lives at repo-level ``docs/Ontology/`` per FEAT-019's
# pre-decided choice. We resolve it via the package's parents so editable
# installs (the project's default workflow) pick it up without packaging
# tricks.
REPO_ROOT = PACKAGE_DIR.parent.parent
ONTOLOGY_SPEC_FILE = REPO_ROOT / "docs" / "Ontology" / "creek_ontology_agent_prompt.md"


@dataclass(frozen=True)
class ScaffoldResult:
    """Summary of what a scaffold/refresh deployment touched."""

    folders_created: int
    skills_synced: int
    ontology_deployed: bool
    agents_deployed: bool


def find_enclosing_git_repo(path: Path) -> Path | None:
    """Walk ancestors of *path* looking for a ``.git`` directory.

    Args:
        path: Candidate vault path. The path itself need not exist.

    Returns:
        The repository root if *path* sits inside a git repo, else
        ``None``. The function never spawns ``git``; it just inspects
        the filesystem.
    """
    start = path if path.exists() else path.parent
    candidate = start.resolve()
    while True:
        if (candidate / ".git").is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def scaffold_vault(vault: Path) -> int:
    """Materialise the canonical folder topology under *vault*.

    Copies :data:`VAULT_TEMPLATE_DIR` onto *vault* with
    ``dirs_exist_ok=True``. User files already present are left
    untouched; only ``.gitkeep`` markers and any missing directories
    are created.

    Args:
        vault: Target vault root.

    Returns:
        The number of directories that exist in the scaffold tree.
    """
    shutil.copytree(VAULT_TEMPLATE_DIR, vault, dirs_exist_ok=True)
    return sum(1 for _ in vault.rglob("*") if _.is_dir())


def deploy_skills(vault: Path) -> int:
    """Copy every canonical ``*.SKILL.md`` into ``<vault>/00-Creek-Meta/Skills/``.

    Args:
        vault: Target vault root.

    Returns:
        The number of skill files written.
    """
    target = vault / "00-Creek-Meta" / "Skills"
    target.mkdir(parents=True, exist_ok=True)
    written = 0
    for skill in sorted(SKILLS_TEMPLATE_DIR.glob("*.SKILL.md")):
        shutil.copy2(skill, target / skill.name)
        written += 1
    return written


def detect_drifted_skills(vault: Path) -> list[Path]:
    """Return deployed skill files whose contents diverge from canonical.

    A skill file is considered *drifted* when it exists in the user's
    vault but its bytes differ from the canonical template. Missing
    files are not drift — they are simply pending sync.

    Args:
        vault: Target vault root.

    Returns:
        The list of drifted deployed skill paths (sorted by name).
    """
    target = vault / "00-Creek-Meta" / "Skills"
    if not target.exists():
        return []
    drifted: list[Path] = []
    for canonical in sorted(SKILLS_TEMPLATE_DIR.glob("*.SKILL.md")):
        deployed = target / canonical.name
        if deployed.exists() and deployed.read_bytes() != canonical.read_bytes():
            drifted.append(deployed)
    return drifted


def deploy_ontology(vault: Path) -> bool:
    """Copy the canonical ontology spec into the vault.

    Args:
        vault: Target vault root.

    Returns:
        ``True`` when the spec was deployed, ``False`` when the
        canonical source is missing (rare; only happens in stripped
        wheel installs).
    """
    if not ONTOLOGY_SPEC_FILE.is_file():
        return False
    target = vault / "00-Creek-Meta" / "Ontology" / "creek_ontology_agent_prompt.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ONTOLOGY_SPEC_FILE, target)
    return True


def deploy_agents_md(vault: Path) -> bool:
    """Copy the canonical AGENTS.md to the vault root.

    Args:
        vault: Target vault root.

    Returns:
        ``True`` when the file was deployed, ``False`` if the template
        is missing.
    """
    if not AGENTS_TEMPLATE_FILE.is_file():
        return False
    vault.mkdir(parents=True, exist_ok=True)
    shutil.copy2(AGENTS_TEMPLATE_FILE, vault / "AGENTS.md")
    return True


def deploy_canonical(vault: Path) -> ScaffoldResult:
    """Apply the full canonical deployment to *vault*.

    Wraps :func:`scaffold_vault`, :func:`deploy_skills`,
    :func:`deploy_ontology`, and :func:`deploy_agents_md` in one call.
    Safe to re-run; user data outside the canonical targets is never
    touched.
    """
    folders = scaffold_vault(vault)
    synced = deploy_skills(vault)
    ontology = deploy_ontology(vault)
    agents = deploy_agents_md(vault)
    return ScaffoldResult(
        folders_created=folders,
        skills_synced=synced,
        ontology_deployed=ontology,
        agents_deployed=agents,
    )
