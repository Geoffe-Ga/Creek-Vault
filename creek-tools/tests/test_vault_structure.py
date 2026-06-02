"""Tests for the canonical vault scaffold under ``creek/templates/vault/``.

FEAT-019 moved the vault folder structure out of the repo root and into
a versioned template directory. This file pins the topology of that
template: every per-vault directory that ``creek init`` is expected to
materialise must have a ``.gitkeep`` marker so it round-trips through
git. The repo-level ``.obsidian/`` config also stays asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

from creek.scaffold import scaffold_vault

REPO_ROOT = Path(__file__).resolve().parents[2]
VAULT_TEMPLATE_ROOT = REPO_ROOT / "creek-tools" / "creek" / "templates" / "vault"

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

SUBDIRECTORIES: dict[str, list[str]] = {
    "00-Creek-Meta": ["Templates", "Scripts", "Ontology", "Processing-Log"],
    "01-Fragments": [
        "Conversations",
        "Messages",
        "Writing",
        "Journal",
        "Technical",
        "Unsorted",
    ],
    "02-Threads": ["Active", "Dormant", "Resolved"],
    "03-Eddies": [],
    "04-Praxis": ["Daily", "Seasonal", "Situational"],
    "05-Wavelength": ["Phase-Maps", "Mode-Profiles", "Observations"],
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
    "07-Voice": ["Register-Samples", "Rhetorical-Patterns", "Lexicon", "Drafts"],
    "08-Decisions": ["Active", "Archive", "Frameworks"],
    "09-Reference": ["APTITUDE-Course", "Published-Essays", "External-Sources"],
    "10-Liminal": ["Paradoxes", "Synchronicities", "Unnamed", "Compost"],
    # FEAT-041 §7.1: organized by author, then by work. ``ai-as-user`` is a
    # reserved author slug; ``example-author`` is a placeholder author folder.
    "11-Other-Authors": ["ai-as-user", "example-author"],
}

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
    """Every leaf directory in the template ships a ``.gitkeep`` marker."""
    for parent, children in SUBDIRECTORIES.items():
        if not children:
            keep = VAULT_TEMPLATE_ROOT / parent / ".gitkeep"
            assert keep.is_file(), f"Missing .gitkeep in {parent}"
        else:
            for child in children:
                keep = VAULT_TEMPLATE_ROOT / parent / child / ".gitkeep"
                assert keep.is_file(), f"Missing .gitkeep in {parent}/{child}"


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
