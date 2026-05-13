"""Tests for FEAT-019: vault sovereignty (separate user vault from repo).

The repo carries the *toolchain* and *canonical templates* under
``creek-tools/creek/templates/``. ``creek init --vault <path>``
materialises a fresh vault at the user's chosen path, copying the
ontology spec, AGENTS.md, the schema-skill tree, and a starter config.

These tests pin the privacy/sovereignty guarantees the FEAT promises:
the repo no longer contains user-vault folders, ``--vault`` is required,
inside-a-git-repo paths are refused by default, and ``creek skills
sync`` re-deploys canonical skills.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from creek.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "creek-tools" / "creek" / "templates"

runner = CliRunner()


# ---------- repo topology ----------


REMOVED_TOP_LEVEL_FOLDERS = (
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
)


def test_repo_root_has_no_vault_folders() -> None:
    """The repo no longer ships any empty vault placeholders (FEAT-019)."""
    for folder in REMOVED_TOP_LEVEL_FOLDERS:
        path = REPO_ROOT / folder
        assert not path.exists(), (
            f"{folder}/ is a per-vault directory and must not exist at repo root"
        )


def test_canonical_templates_directory_exists() -> None:
    """``creek-tools/creek/templates/`` is the scaffolding source of truth."""
    assert (TEMPLATES_DIR / "vault").is_dir()
    assert (TEMPLATES_DIR / "skills").is_dir()
    assert (TEMPLATES_DIR / "AGENTS.md").is_file()


def test_template_vault_has_full_topology() -> None:
    """Every documented vault subdirectory has a ``.gitkeep`` in the template."""
    expected_leaves = (
        "00-Creek-Meta/Ontology",
        "00-Creek-Meta/Scripts",
        "00-Creek-Meta/Templates",
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis/Daily",
        "05-Wavelength/Phase-Maps",
        "06-Frequencies/F1-Agency",
        "07-Voice/Drafts",
        "08-Decisions/Active",
        "09-Reference/External-Sources",
        "10-Liminal/Paradoxes",
    )
    vault_template = TEMPLATES_DIR / "vault"
    for relpath in expected_leaves:
        keep = vault_template / relpath / ".gitkeep"
        assert keep.is_file(), f"Missing .gitkeep in template: {relpath}"


def test_canonical_skill_files_are_present() -> None:
    """All eight schema-skill files live under the canonical skills/ dir."""
    expected = {
        "compile.SKILL.md",
        "liminal.SKILL.md",
        "lint.SKILL.md",
        "paradox.SKILL.md",
        "privacy-tier.SKILL.md",
        "query.SKILL.md",
        "save.SKILL.md",
        "wavelength-aware.SKILL.md",
    }
    actual = {p.name for p in (TEMPLATES_DIR / "skills").glob("*.SKILL.md")}
    assert expected.issubset(actual), f"Missing skills: {expected - actual}"


def test_ontology_spec_at_repo_level() -> None:
    """The canonical ontology spec lives at ``docs/Ontology/`` in the repo."""
    spec = REPO_ROOT / "docs" / "Ontology" / "creek_ontology_agent_prompt.md"
    assert spec.is_file()


# ---------- creek init: required --vault flag ----------


def test_init_requires_vault_flag() -> None:
    """``creek init`` without ``--vault`` exits non-zero (sovereignty guard)."""
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0


def test_init_scaffolds_full_vault_topology(tmp_path: Path) -> None:
    """``creek init --vault <path>`` materialises the canonical folder tree."""
    vault = tmp_path / "my-vault"
    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    for folder in (
        "00-Creek-Meta/Ontology",
        "00-Creek-Meta/Skills",
        "01-Fragments/Journal",
        "02-Threads/Active",
        "06-Frequencies/F1-Agency",
        "10-Liminal/Paradoxes",
    ):
        assert (vault / folder).is_dir(), f"init did not create {folder}"


def test_init_copies_ontology_spec_byte_for_byte(tmp_path: Path) -> None:
    """The deployed ontology matches the canonical source byte-for-byte."""
    vault = tmp_path / "vault"
    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    deployed = vault / "00-Creek-Meta" / "Ontology" / "creek_ontology_agent_prompt.md"
    canonical = REPO_ROOT / "docs" / "Ontology" / "creek_ontology_agent_prompt.md"
    assert deployed.read_bytes() == canonical.read_bytes()


def test_init_copies_agents_md(tmp_path: Path) -> None:
    """``creek init`` writes the canonical AGENTS.md to the vault root."""
    vault = tmp_path / "vault"
    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    deployed = vault / "AGENTS.md"
    canonical = TEMPLATES_DIR / "AGENTS.md"
    assert deployed.read_bytes() == canonical.read_bytes()


def test_init_copies_full_skill_tree(tmp_path: Path) -> None:
    """``creek init`` copies every canonical skill into the vault."""
    vault = tmp_path / "vault"
    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    skills_glob = "*.SKILL.md"
    canonical_names = {p.name for p in (TEMPLATES_DIR / "skills").glob(skills_glob)}
    deployed_dir = vault / "00-Creek-Meta" / "Skills"
    deployed_names = {p.name for p in deployed_dir.glob(skills_glob)}
    assert canonical_names == deployed_names


def test_init_writes_starter_config(tmp_path: Path) -> None:
    """A fresh init still seeds ``creek_config.yaml`` (ARCH-002 regression)."""
    vault = tmp_path / "vault"
    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert (vault / "00-Creek-Meta" / "creek_config.yaml").is_file()


# ---------- inside-a-git-repo guard ----------


def _mark_as_git_repo(path: Path) -> None:
    """Create a ``.git`` marker so the sovereignty guard fires."""
    (path / ".git").mkdir(exist_ok=True)


def test_init_refuses_inside_git_repo(tmp_path: Path) -> None:
    """Targeting a path inside a git repo is refused (FEAT-019 sovereignty)."""
    _mark_as_git_repo(tmp_path)
    vault = tmp_path / "would-be-vault"
    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code != 0
    assert "inside a git repository" in result.output
    assert "--allow-in-repo" in result.output
    # Sovereignty guard must fail BEFORE any scaffold writes occur.
    assert not vault.exists()


def test_init_allow_in_repo_overrides_guard(tmp_path: Path) -> None:
    """``--allow-in-repo`` succeeds with a warning even inside a git repo."""
    _mark_as_git_repo(tmp_path)
    vault = tmp_path / "in-repo-vault"
    result = runner.invoke(
        app,
        ["init", "--vault", str(vault), "--allow-in-repo"],
    )
    assert result.exit_code == 0, result.output
    assert (vault / "AGENTS.md").is_file()


# ---------- creek skills sync ----------


def test_skills_sync_redeploys_canonical_tree(tmp_path: Path) -> None:
    """``creek skills sync`` re-copies the canonical tree into a vault."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    drifted = skills_dir / "compile.SKILL.md"
    drifted.write_text("stale local edit\n", encoding="utf-8")

    sync_result = runner.invoke(
        app, ["skills", "sync", "--vault", str(vault), "--force"]
    )
    assert sync_result.exit_code == 0, sync_result.output

    canonical = (TEMPLATES_DIR / "skills" / "compile.SKILL.md").read_bytes()
    assert drifted.read_bytes() == canonical


def test_skills_sync_refuses_local_edits_without_force(tmp_path: Path) -> None:
    """Local skill edits block ``skills sync`` unless ``--force`` is set."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    drifted = vault / "00-Creek-Meta" / "Skills" / "compile.SKILL.md"
    drifted.write_text("stale local edit\n", encoding="utf-8")

    sync_result = runner.invoke(app, ["skills", "sync", "--vault", str(vault)])
    assert sync_result.exit_code != 0
    assert "local changes" in sync_result.output.lower()
    assert drifted.read_text(encoding="utf-8") == "stale local edit\n"


def test_skills_sync_requires_vault_flag() -> None:
    """``creek skills sync`` without ``--vault`` exits non-zero."""
    result = runner.invoke(app, ["skills", "sync"])
    assert result.exit_code != 0


def test_skills_sync_is_idempotent(tmp_path: Path) -> None:
    """Running ``skills sync`` twice on a clean vault is a no-op."""
    vault = tmp_path / "vault"
    runner.invoke(app, ["init", "--vault", str(vault)])

    first = runner.invoke(app, ["skills", "sync", "--vault", str(vault)])
    second = runner.invoke(app, ["skills", "sync", "--vault", str(vault)])
    assert first.exit_code == 0
    assert second.exit_code == 0


# ---------- init idempotency / refresh ----------


def test_init_refresh_overwrites_canonical_material(tmp_path: Path) -> None:
    """``creek init --refresh`` re-copies templates without nuking user data."""
    vault = tmp_path / "vault"
    runner.invoke(app, ["init", "--vault", str(vault)])

    user_note = vault / "01-Fragments" / "Journal" / "my-private-note.md"
    user_note.write_text("dear diary\n", encoding="utf-8")

    agents = vault / "AGENTS.md"
    agents.write_text("# local edit\n", encoding="utf-8")

    refresh = runner.invoke(app, ["init", "--vault", str(vault), "--refresh"])
    assert refresh.exit_code == 0, refresh.output

    canonical_agents = (TEMPLATES_DIR / "AGENTS.md").read_bytes()
    assert agents.read_bytes() == canonical_agents
    assert user_note.read_text(encoding="utf-8") == "dear diary\n"


def test_init_refresh_preserves_user_content_across_topology(tmp_path: Path) -> None:
    """``--refresh`` leaves user-authored files alone across every vault subtree."""
    vault = tmp_path / "vault"
    runner.invoke(app, ["init", "--vault", str(vault)])

    user_files = {
        vault / "01-Fragments" / "Journal" / "2026-05-13.md": "morning pages\n",
        vault / "02-Threads" / "Active" / "thread-a.md": "active narrative\n",
        vault / "04-Praxis" / "Daily" / "ritual.md": "rise and shine\n",
        vault / "07-Voice" / "Drafts" / "essay.md": "first sentence\n",
    }
    for path, body in user_files.items():
        path.write_text(body, encoding="utf-8")

    refresh = runner.invoke(app, ["init", "--vault", str(vault), "--refresh"])
    assert refresh.exit_code == 0, refresh.output

    for path, body in user_files.items():
        assert path.read_text(encoding="utf-8") == body, f"refresh mutated {path}"


def test_init_does_not_overwrite_user_config_without_force(tmp_path: Path) -> None:
    """The existing config-preservation contract still holds."""
    vault = tmp_path / "vault"
    runner.invoke(app, ["init", "--vault", str(vault)])

    config = vault / "00-Creek-Meta" / "creek_config.yaml"
    config.write_text("# operator-edited\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert result.exit_code != 0
    assert config.read_text(encoding="utf-8") == "# operator-edited\n"
