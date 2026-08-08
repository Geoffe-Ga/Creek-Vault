"""Tests for the canonical vault scaffold under ``creek/templates/vault/``.

FEAT-019 moved the vault folder structure out of the repo root and into
a versioned template directory. This file pins the topology of that
template: every per-vault directory that ``creek init`` is expected to
materialise must have a ``.gitkeep`` marker so it round-trips through
git. The repo-level ``.obsidian/`` config also stays asserted here.

Topology is pinned from two directions, and the two must never overlap:

* **Derived** (issue #1025) — every directory production code *writes*
  into is computed from the production constants that name it, so a new
  ``SourcePlatform`` or a moved report target fails
  :func:`test_every_production_write_target_exists_in_the_scaffold`
  instead of silently creating a folder ``creek init`` never promised.
  Nothing in that expected set may be re-typed here; re-typing it is the
  exact defect #1025 fixed.
* **Declared** — the literal lists below cover only the folders the
  *scaffold itself* authors, which no production constant names
  (``06-Frequencies/F*``, ``09-Reference/*``, the meta workspace dirs).
  :func:`test_declared_topology_does_not_shadow_the_derived_guard` keeps
  them from regrowing into a second, drifting copy of the derived set.
"""

from __future__ import annotations

import json
from pathlib import Path

from creek.config import CompostConfig
from creek.generate.compost import CANONICAL_RELDIR
from creek.generate.drafts import DRAFTS_SUBDIR
from creek.generate.lexicon import _LEXICON_SUBPATH, _METAPHORS_SUBDIR
from creek.generate.state import _STATE_SUBPATH
from creek.generate.synchronicity import _SYNCHRONICITY_SUBPATH
from creek.generate.unnamed import _DIGESTS_SUBFOLDER, _UNNAMED_SUBPATH
from creek.generate.voice import _PROFILE_SUBDIR, _RHETORICAL_SUBDIR, _SAMPLES_SUBPATH
from creek.generate.wavelength import _MODE_PROFILE_SUBPATH, _PHASE_MAP_SUBPATH
from creek.models import ThreadStatus
from creek.save._constants import INTIMATE_STUB_RELPATH
from creek.save.router import TARGET_SUBDIRS
from creek.scaffold import VAULT_TEMPLATE_DIR, scaffold_vault
from creek.vault.authors import OTHER_AUTHORS_DIR
from creek.vault.writer import (
    _ACTIVE_DECISION_SUBFOLDER,
    _ARCHIVED_DECISION_SUBFOLDER,
    _DECISIONS_RELPART,
    _EDDIES_RELPART,
    _FRAGMENTS_RELPART,
    _ORPHANED_RELPARTS,
    _PLATFORM_SUBFOLDER,
    _PRAXIS_RELPART,
    _PRAXIS_SUBFOLDER,
    _PROCESSING_LOG_RELPARTS,
    _THREADS_RELPART,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VAULT_TEMPLATE_ROOT = VAULT_TEMPLATE_DIR

# ---------- expected structure ----------

TOP_LEVEL_FOLDERS: list[str] = [
    "00-Creek-Meta",
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "07-Voice",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
    "11-Other-Authors",
]

# Scaffold-authored subdirectories ONLY: folders the template ships that no
# production constant names, so nothing can derive them. Every folder
# production writes into is asserted by the derived guard below instead —
# adding one here as well would recreate the hand-copied list that drifted
# (#1025), and ``test_declared_topology_does_not_shadow_the_derived_guard``
# fails if anyone does.
SUBDIRECTORIES: dict[str, list[str]] = {
    "00-Creek-Meta": ["Templates", "Scripts", "Ontology", "Skills"],
    "06-Frequencies": [
        "F1-Agency",
        "F2-Receptivity",
        "F3-Self-Love-Power",
        "F4-Community-Love",
        "F5-Achievism",
        "F6-Pluralism",
        "F7-Integration",
        "F8-True-Self",
        "F9-Unity",
        "F10-Emptiness",
    ],
    # Frameworks/ is operator-authored: ``VaultWriter.write_decision`` only
    # ever routes to Active/ or Archive/, both of which the guard derives.
    "08-Decisions": ["Frameworks"],
    "09-Reference": ["APTITUDE-Course", "Published-Essays", "External-Sources"],
    # FEAT-041 §7.1: organized by author, then by work. ``ai-as-user`` is a
    # reserved author slug the save router derives; ``example-author`` is a
    # placeholder author folder nothing in production names.
    "11-Other-Authors": ["example-author"],
}


# ---------- derived expectations (issue #1025) ----------


def _scaffolded_relpaths() -> set[str]:
    """Return every directory the vault template ships, vault-relative.

    POSIX separators throughout, because some production constants embed
    one (``_PLATFORM_SUBFOLDER[SUBSTACK] == "Writing/Substack"``).
    """
    return {
        path.relative_to(VAULT_TEMPLATE_ROOT).as_posix()
        for path in VAULT_TEMPLATE_ROOT.rglob("*")
        if path.is_dir()
    }


def _writer_targets() -> set[str]:
    """Return every static directory :class:`VaultWriter` routes a model into."""
    return {
        _FRAGMENTS_RELPART,
        _EDDIES_RELPART,
        OTHER_AUTHORS_DIR,
        "/".join(_PROCESSING_LOG_RELPARTS),
        "/".join(_ORPHANED_RELPARTS),
        *(f"{_FRAGMENTS_RELPART}/{sub}" for sub in _PLATFORM_SUBFOLDER.values()),
        *(f"{_PRAXIS_RELPART}/{sub}" for sub in _PRAXIS_SUBFOLDER.values()),
        *(f"{_THREADS_RELPART}/{str(status).capitalize()}" for status in ThreadStatus),
        *(
            f"{_DECISIONS_RELPART}/{sub}"
            for sub in (_ACTIVE_DECISION_SUBFOLDER, _ARCHIVED_DECISION_SUBFOLDER)
        ),
    }


def _generator_targets() -> set[str]:
    """Return every static directory the ``creek generate`` writers emit into."""
    lexicon = "/".join(_LEXICON_SUBPATH)
    unnamed = "/".join(_UNNAMED_SUBPATH)
    return {
        DRAFTS_SUBDIR,
        CANONICAL_RELDIR,
        CompostConfig().review_queue_relpath,
        _PROFILE_SUBDIR,
        lexicon,
        f"{lexicon}/{_METAPHORS_SUBDIR}",
        unnamed,
        f"{unnamed}/{_DIGESTS_SUBFOLDER}",
        "/".join(_PHASE_MAP_SUBPATH),
        "/".join(_MODE_PROFILE_SUBPATH),
        "/".join(_RHETORICAL_SUBDIR),
        "/".join(_SAMPLES_SUBPATH),
        "/".join(_STATE_SUBPATH),
        "/".join(_SYNCHRONICITY_SUBPATH),
    }


def _save_targets() -> set[str]:
    """Return every directory ``creek save`` files a note into."""
    return {
        INTIMATE_STUB_RELPATH.as_posix(),
        *("/".join(parts) for parts in TARGET_SUBDIRS.values()),
    }


def _production_write_targets() -> set[str]:
    """Return every vault directory production code writes ontology content to.

    Computed from the constants the production code itself routes on, so
    the expected set cannot drift from the code the way the old
    hand-copied lists did. Deliberately excluded, because they hold
    machine state rather than vault content and their creators own them
    at runtime: ``00-Creek-Meta/{audit,Inbound,State/*,adepthood/*}``,
    the vault-root ``discord-export`` staging tree, and the generated
    ``creek-skills`` output tree. Also excluded are the content-derived
    segments no static list can name — ``11-Other-Authors/<slug>`` and
    ``07-Voice/Register-Samples/<register>`` — whose parents *are*
    covered here.
    """
    return _writer_targets() | _generator_targets() | _save_targets()


# FEAT-041 §7.1/§7.5: the Other-Authors category root ships two content files —
# a ``_README.md`` (attribution model + voice-training exclusion) and an
# ``_author.md`` manifest template (the shape issue #458 will parse).
OTHER_AUTHORS_ROOT_FILES: list[str] = ["_README.md", "_author.md"]

EXPECTED_PLUGINS: list[str] = [
    "dataview",
    "templater",
    "graph-analysis",
    "calendar",
    "obsidian-kanban",
    "tag-wrangler",
    "periodic-notes",
    "obsidian-git",
]


# ---------- tests ----------


def test_top_level_template_folders_exist() -> None:
    """Every top-level vault folder has a template directory."""
    for folder in TOP_LEVEL_FOLDERS:
        assert (VAULT_TEMPLATE_ROOT / folder).is_dir(), (
            f"Missing template folder: {folder}"
        )


def test_template_subdirectories_exist() -> None:
    """Every expected subdirectory is present in the template."""
    for parent, children in SUBDIRECTORIES.items():
        for child in children:
            assert (VAULT_TEMPLATE_ROOT / parent / child).is_dir(), (
                f"Missing template subdirectory: {parent}/{child}"
            )


def test_gitkeep_files_in_leaf_directories() -> None:
    """Every leaf directory in the template ships a ``.gitkeep`` marker.

    Walked from the template rather than from a declared list, so a
    directory added without a marker is caught: git does not track empty
    directories, so an unmarked folder never reaches a user's vault no
    matter what the packaging globs say.
    """
    missing = sorted(
        directory.relative_to(VAULT_TEMPLATE_ROOT).as_posix()
        for directory in VAULT_TEMPLATE_ROOT.rglob("*")
        if directory.is_dir()
        and not any(child.is_dir() for child in directory.iterdir())
        and not (directory / ".gitkeep").is_file()
    )
    assert not missing, f"Template leaf directories without .gitkeep: {missing}"


def test_every_production_write_target_exists_in_the_scaffold() -> None:
    """``creek init`` promises every directory production code writes into.

    The expected set is computed from the production constants (issue
    #1025). Adding a ``SourcePlatform`` or moving a report target without
    a matching scaffold folder fails here rather than silently creating a
    folder in the user's vault that ``creek init`` never made.
    """
    missing = sorted(_production_write_targets() - _scaffolded_relpaths())
    assert not missing, (
        f"Production writes into directories the scaffold never creates: {missing}"
    )


def test_scaffold_vault_materializes_every_production_write_target(
    tmp_path: Path,
) -> None:
    """The scaffold *as deployed* carries every production write target.

    The template on disk passing is not enough: only directories git
    tracks survive into the installed package, so this runs the real
    ``creek init`` code path and re-checks the derived set.
    """
    vault = tmp_path / "vault"
    scaffold_vault(vault)
    missing = sorted(
        rel for rel in _production_write_targets() if not (vault / rel).is_dir()
    )
    assert not missing, f"scaffold_vault did not create: {missing}"


def test_declared_topology_does_not_shadow_the_derived_guard() -> None:
    """The literal lists never restate a directory the guard already derives."""
    declared = {
        f"{parent}/{child}"
        for parent, children in SUBDIRECTORIES.items()
        for child in children
    }
    overlap = sorted(declared & _production_write_targets())
    assert not overlap, (
        "SUBDIRECTORIES re-types directories the production constants already "
        f"derive; delete them and let the guard own them: {overlap}"
    )


def test_other_authors_root_content_files_exist() -> None:
    """The ``11-Other-Authors/`` root ships ``_README.md`` and ``_author.md``."""
    root = VAULT_TEMPLATE_ROOT / "11-Other-Authors"
    for name in OTHER_AUTHORS_ROOT_FILES:
        assert (root / name).is_file(), f"Missing 11-Other-Authors/{name}"


def test_other_authors_readme_documents_exclusion() -> None:
    """``_README.md`` explains attribution and the voice-corpus exclusion."""
    readme = (VAULT_TEMPLATE_ROOT / "11-Other-Authors" / "_README.md").read_text(
        encoding="utf-8"
    )
    lowered = readme.lower()
    assert "attribution" in lowered, "README should explain the attribution model"
    assert "voice" in lowered, "README should explain the voice-training exclusion"
    assert "ai-as-user" in lowered, "README should document the reserved slug"


def test_other_authors_manifest_template_shape() -> None:
    """``_author.md`` carries the manifest frontmatter shape issue #458 parses."""
    manifest = (VAULT_TEMPLATE_ROOT / "11-Other-Authors" / "_author.md").read_text(
        encoding="utf-8"
    )
    for key in (
        "type: author_manifest",
        "author_slug:",
        "display_name:",
        "author_kind:",
        "voice_weight:",
        "representativeness:",
        "default_privacy_tier:",
        "attribution_required:",
    ):
        assert key in manifest, f"Manifest template missing `{key}`"


def test_creek_init_materializes_other_authors(tmp_path: Path) -> None:
    """``scaffold_vault`` creates the Other-Authors category in a fresh vault."""
    vault = tmp_path / "vault"
    scaffold_vault(vault)
    assert (vault / "11-Other-Authors").is_dir()
    assert (vault / "11-Other-Authors" / "ai-as-user").is_dir()
    assert (vault / "11-Other-Authors" / "_README.md").is_file()
    assert (vault / "11-Other-Authors" / "ai-as-user" / ".gitkeep").is_file()


def test_scaffold_is_idempotent_and_preserves_user_data(tmp_path: Path) -> None:
    """Re-scaffolding leaves 00-10 untouched and keeps user files (tracer invariant)."""
    vault = tmp_path / "vault"
    scaffold_vault(vault)
    user_note = vault / "01-Fragments" / "Journal" / "my-note.md"
    user_note.write_text("private", encoding="utf-8")

    scaffold_vault(vault)  # second run must not disturb existing content

    assert user_note.read_text(encoding="utf-8") == "private"
    assert (vault / "10-Liminal" / "Compost" / ".gitkeep").is_file()
    assert (vault / "11-Other-Authors" / "_author.md").is_file()


def test_ontology_spec_lives_in_repo() -> None:
    """The canonical ontology spec ships at ``docs/Ontology/``."""
    target = REPO_ROOT / "docs" / "Ontology" / "creek_ontology_agent_prompt.md"
    assert target.is_file(), "Ontology prompt not found in docs/Ontology/"


def test_obsidian_directory_exists() -> None:
    """The ``.obsidian/`` config directory remains at the repo root."""
    assert (REPO_ROOT / ".obsidian").is_dir(), "Missing .obsidian/ directory"


def test_community_plugins_json() -> None:
    """``community-plugins.json`` lists the required plugins."""
    plugins_file = REPO_ROOT / ".obsidian" / "community-plugins.json"
    assert plugins_file.is_file(), "Missing .obsidian/community-plugins.json"

    plugins: list[str] = json.loads(plugins_file.read_text(encoding="utf-8"))
    assert plugins == EXPECTED_PLUGINS


def test_gitignore_obsidian_entries() -> None:
    """Root ``.gitignore`` ignores Obsidian workspace state and cache."""
    gitignore = REPO_ROOT / ".gitignore"
    content = gitignore.read_text(encoding="utf-8")
    assert ".obsidian/workspace.json" in content
    assert ".obsidian/cache" in content
