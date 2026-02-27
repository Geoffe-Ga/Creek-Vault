"""Tests for Obsidian vault folder structure.

Verifies that all vault directories, .gitkeep files, the moved
ontology file, and the .obsidian configuration exist at the repo root.
"""

from __future__ import annotations

import json
from pathlib import Path

# Repo root is two levels up from this test file:
# <repo>/creek-tools/tests/test_vault_structure.py
REPO_ROOT = Path(__file__).resolve().parents[2]

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


def test_top_level_folders_exist() -> None:
    """All 11 top-level vault folders must exist at the repo root."""
    for folder in TOP_LEVEL_FOLDERS:
        path = REPO_ROOT / folder
        assert path.is_dir(), f"Missing top-level folder: {folder}"


def test_subdirectories_exist() -> None:
    """Every expected subdirectory must exist under its parent."""
    for parent, children in SUBDIRECTORIES.items():
        for child in children:
            path = REPO_ROOT / parent / child
            assert path.is_dir(), f"Missing subdirectory: {parent}/{child}"


def test_gitkeep_files_in_leaf_directories() -> None:
    """Leaf directories (and parents with no other tracked content) have .gitkeep."""
    for parent, children in SUBDIRECTORIES.items():
        if not children:
            # The parent itself is a leaf
            gitkeep = REPO_ROOT / parent / ".gitkeep"
            assert gitkeep.is_file(), f"Missing .gitkeep in {parent}"
        else:
            for child in children:
                gitkeep = REPO_ROOT / parent / child / ".gitkeep"
                assert gitkeep.is_file(), f"Missing .gitkeep in {parent}/{child}"


def test_ontology_file_moved() -> None:
    """The ontology prompt must reside in 00-Creek-Meta/Ontology/."""
    target = REPO_ROOT / "00-Creek-Meta" / "Ontology" / "creek_ontology_agent_prompt.md"
    assert target.is_file(), "Ontology prompt not found in 00-Creek-Meta/Ontology/"

    old_location = REPO_ROOT / "scripts" / "Ontology" / "creek_ontology_agent_prompt.md"
    assert not old_location.exists(), (
        "Ontology prompt still exists at old location scripts/Ontology/"
    )


def test_obsidian_directory_exists() -> None:
    """The .obsidian/ config directory must exist at the repo root."""
    obsidian_dir = REPO_ROOT / ".obsidian"
    assert obsidian_dir.is_dir(), "Missing .obsidian/ directory"


def test_community_plugins_json() -> None:
    """community-plugins.json must list the required plugins."""
    plugins_file = REPO_ROOT / ".obsidian" / "community-plugins.json"
    assert plugins_file.is_file(), "Missing .obsidian/community-plugins.json"

    plugins: list[str] = json.loads(plugins_file.read_text(encoding="utf-8"))
    assert plugins == EXPECTED_PLUGINS


def test_gitignore_obsidian_entries() -> None:
    """Root .gitignore must contain entries for Obsidian workspace and cache."""
    gitignore = REPO_ROOT / ".gitignore"
    content = gitignore.read_text(encoding="utf-8")
    assert ".obsidian/workspace.json" in content
    assert ".obsidian/cache" in content
