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
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
VAULT_TEMPLATE_DIR = TEMPLATES_DIR / "vault"
SKILLS_TEMPLATE_DIR = TEMPLATES_DIR / "skills"
AGENTS_TEMPLATE_FILE = TEMPLATES_DIR / "AGENTS.md"

MEDIUMS_DIRNAME: Final = "mediums"
"""Sub-directory of the skill tree holding the ``*.MEDIUM.md`` contracts."""

BACKUP_SUFFIX: Final = ".bak"
"""Suffix appended to a locally-edited skill file preserved by ``--force``.

Deliberately *appended* rather than substituted: ``essay.MEDIUM.md.bak``
still reads as the file it came from, and — because it no longer ends in
``.SKILL.md`` or ``.MEDIUM.md`` — it is invisible to every glob that
enumerates skills, including drift detection and the ``skill-size`` lint
check.
"""

# The ontology spec lives at repo-level ``docs/Ontology/`` per FEAT-019's
# pre-decided choice. We resolve it via the package's parents so editable
# installs (the project's default workflow) pick it up without packaging
# tricks.
REPO_ROOT = PACKAGE_DIR.parent.parent
ONTOLOGY_SPEC_FILE = REPO_ROOT / "docs" / "Ontology" / "creek_ontology_agent_prompt.md"


@dataclass(frozen=True)
class ScaffoldResult:
    """Summary of what a scaffold/refresh deployment touched."""

    folders_ensured: int
    skills_synced: int
    ontology_deployed: bool
    agents_deployed: bool


@dataclass(frozen=True)
class SkillDeployment:
    """Summary of one canonical skill-tree deployment.

    Attributes:
        written: How many canonical skill files were copied into the vault.
        preserved: The ``.bak`` sidecars created to hold the operator's
            pre-existing bytes. Empty unless ``force`` overrode drift.
    """

    written: int
    preserved: tuple[Path, ...]


class DriftedSkillsError(RuntimeError):
    """Raised when deploying canonical skills would clobber local edits.

    Carries the *deployed* paths (not the template ones) so a caller can
    hand the operator something they can open, and the skills root they
    hang off so :attr:`labels` can render them unambiguously.
    """

    def __init__(self, drifted: Sequence[Path], skills_root: Path) -> None:
        """Record the drifted deployed paths and the root they live under.

        Args:
            drifted: Deployed skill paths whose bytes diverge from canon,
                in the order :func:`detect_drifted_skills` returned them.
            skills_root: ``<vault>/00-Creek-Meta/Skills``.
        """
        self.drifted: tuple[Path, ...] = tuple(drifted)
        self.skills_root = skills_root
        joined = ", ".join(self.labels)
        super().__init__(
            f"{len(self.drifted)} canonical skill file(s) have local changes: {joined}",
        )

    @property
    def labels(self) -> tuple[str, ...]:
        """Return each drifted path rendered relative to the skills root.

        Returns:
            Vault-relative POSIX labels such as
            ``"mediums/essay.MEDIUM.md"``. The bare filename would be
            ambiguous — the two skill classes live at different depths,
            and only the relative path says which file to go and look at.
        """
        return tuple(
            path.relative_to(self.skills_root).as_posix() for path in self.drifted
        )


def _skills_root(vault: Path) -> Path:
    """Return the vault directory the canonical skill tree deploys into.

    Args:
        vault: Target vault root.

    Returns:
        ``<vault>/00-Creek-Meta/Skills`` — the single spelling of that
        location for this module.
    """
    return vault / "00-Creek-Meta" / "Skills"


def _canonical_skill_pairs(vault: Path) -> list[tuple[Path, Path]]:
    """Enumerate every canonical skill file and where it deploys to.

    This is the module's *only* answer to "what is a skill file". Drift
    detection and deployment both read it, so the two can no longer
    disagree — issue #1306 was exactly that disagreement: deployment
    wrote ``mediums/*.MEDIUM.md`` while the guard globbed only
    ``*.SKILL.md``, leaving hand-edited medium contracts unprotected.

    The two globs are deliberately explicit rather than an ``rglob``
    sweep: a stray ``.md`` file dropped into the template tree must not
    become something every user vault silently receives.

    Args:
        vault: Target vault root.

    Returns:
        ``(canonical_template_path, deployed_vault_path)`` pairs sorted
        once, globally, by the deployed path relative to the skills root
        — so ``lint.SKILL.md`` sorts before ``mediums/book-report.MEDIUM.md``
        sorts before ``paradox.SKILL.md``. Per-glob sorting would
        interleave wrongly and make the reported order a lie.
    """
    root = _skills_root(vault)
    pairs: list[tuple[Path, Path]] = [
        (canonical, root / canonical.name)
        for canonical in SKILLS_TEMPLATE_DIR.glob("*.SKILL.md")
    ]
    # Stripped-wheel installs ship no ``mediums/``; absence is not an error.
    mediums_src = SKILLS_TEMPLATE_DIR / MEDIUMS_DIRNAME
    if mediums_src.is_dir():
        pairs.extend(
            (canonical, root / MEDIUMS_DIRNAME / canonical.name)
            for canonical in mediums_src.glob("*.MEDIUM.md")
        )
    return sorted(pairs, key=lambda pair: pair[1].relative_to(root).as_posix())


def _preserve_local_edits(drifted: Sequence[Path]) -> tuple[Path, ...]:
    """Copy each drifted file aside before it is overwritten.

    Args:
        drifted: Deployed skill paths about to be replaced by canon.

    Returns:
        The ``.bak`` sidecars written, in the same order. *Whatever* sits
        at that path is replaced — normally a backup from an earlier
        ``--force``, but an operator's own unrelated ``<name>.bak`` would
        go with it. That is the accepted cost of a single guessable
        name: numbered sidecars would accumulate in a browsed Obsidian
        vault with nothing in creek to ever list or remove them. The
        CLI's refusal message discloses it before the operator reaches
        for ``--force``. The sidecar always ends up holding the edit
        this deploy is about to discard, never an older one.
    """
    backups: list[Path] = []
    for deployed in drifted:
        backup = deployed.with_name(deployed.name + BACKUP_SUFFIX)
        shutil.copy2(deployed, backup)
        backups.append(backup)
    return tuple(backups)


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
        The number of canonical directories ensured to exist. This is
        the count of directories defined in the template — a stable
        number that does not depend on whether the vault is fresh or
        being refreshed.
    """
    shutil.copytree(VAULT_TEMPLATE_DIR, vault, dirs_exist_ok=True)
    return sum(1 for entry in VAULT_TEMPLATE_DIR.rglob("*") if entry.is_dir())


def deploy_skills(vault: Path, *, force: bool = False) -> SkillDeployment:
    """Copy the canonical skill tree into ``<vault>/00-Creek-Meta/Skills/``.

    Deploys both the flat schema skills (``*.SKILL.md``) and the medium
    contracts under ``mediums/`` (``*.MEDIUM.md``, FEAT-041 §5).

    The drift guard lives *here*, in the destructive primitive, rather
    than in each caller: ``force`` defaults to ``False`` so a future
    caller that forgets to check is safe by construction (issue #1306,
    where ``creek init --refresh`` reached this function with no guard
    at all and destroyed hand-edited contracts).

    Args:
        vault: Target vault root.
        force: Overwrite locally-modified files instead of refusing.
            Each one is copied aside to ``<name>.bak`` first.

    Returns:
        A :class:`SkillDeployment` recording how many files were written
        and which ``.bak`` sidecars were created.

    Raises:
        DriftedSkillsError: If any deployed skill file has local changes
            and *force* is not set. Nothing is written in that case.
    """
    root = _skills_root(vault)
    drifted = detect_drifted_skills(vault)
    if drifted and not force:
        raise DriftedSkillsError(drifted, root)

    # The root is ensured even when the template tree is empty, so a vault
    # always has a Skills/ directory to drop operator-authored skills into.
    root.mkdir(parents=True, exist_ok=True)
    preserved = _preserve_local_edits(drifted)
    pairs = _canonical_skill_pairs(vault)
    for canonical, deployed in pairs:
        deployed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canonical, deployed)
    return SkillDeployment(written=len(pairs), preserved=preserved)


def detect_drifted_skills(vault: Path) -> list[Path]:
    """Return *deployed* skill paths whose bytes diverge from the canonical template.

    A skill file is considered *drifted* when it exists in the user's
    vault but its bytes differ from the canonical template. Missing
    files are not drift — they are simply pending sync. Files the
    operator authored themselves are not drift either: only the
    canonical set from :func:`_canonical_skill_pairs` is compared. The
    returned paths point at the *deployed* copies (under
    ``<vault>/00-Creek-Meta/Skills/``) so callers can surface them
    directly to the operator.

    Args:
        vault: Target vault root.

    Returns:
        The list of drifted *deployed* skill paths, in one global sort
        over the path each holds relative to
        ``<vault>/00-Creek-Meta/Skills/`` — so ``lint.SKILL.md`` precedes
        ``mediums/book-report.MEDIUM.md`` precedes ``paradox.SKILL.md``.
        Sorting each glob separately would interleave them wrongly.
    """
    root = _skills_root(vault)
    if not root.exists():
        return []
    return [
        deployed
        for canonical, deployed in _canonical_skill_pairs(vault)
        if deployed.exists() and deployed.read_bytes() != canonical.read_bytes()
    ]


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


def deploy_canonical(vault: Path, *, force: bool = False) -> ScaffoldResult:
    """Apply the full canonical deployment to *vault*.

    Wraps :func:`scaffold_vault`, :func:`deploy_skills`,
    :func:`deploy_ontology`, and :func:`deploy_agents_md` in one call.
    Safe to re-run; user data outside the canonical targets is never
    touched.

    The drift check is a *pre-flight*, not a step: refusing halfway
    through would leave the operator with a freshly-overwritten
    ``AGENTS.md`` and an error message, unable to tell what landed. So
    the check runs before the first write, which makes a *drift
    refusal* all-or-nothing — it deploys nothing at all.

    That guarantee is scoped to the refusal, and deliberately not
    claimed more broadly: once past the pre-flight the four deployment
    steps are sequential, so an I/O error partway through (a full disk,
    a permission denial) can still leave a partial deployment behind.
    Making that atomic would need a staging directory and is out of
    scope here; saying so is not.

    Args:
        vault: Target vault root.
        force: Overwrite locally-modified canonical skill files,
            preserving each as ``<name>.bak``.

    Returns:
        A :class:`ScaffoldResult` summarising the deployment.

    Raises:
        DriftedSkillsError: If any deployed skill file has local changes
            and *force* is not set. Nothing at all is deployed.
    """
    drifted = detect_drifted_skills(vault)
    if drifted and not force:
        raise DriftedSkillsError(drifted, _skills_root(vault))

    folders = scaffold_vault(vault)
    deployment = deploy_skills(vault, force=force)
    ontology = deploy_ontology(vault)
    agents = deploy_agents_md(vault)
    return ScaffoldResult(
        folders_ensured=folders,
        skills_synced=deployment.written,
        ontology_deployed=ontology,
        agents_deployed=agents,
    )
