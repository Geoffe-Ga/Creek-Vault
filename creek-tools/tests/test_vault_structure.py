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
}

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
