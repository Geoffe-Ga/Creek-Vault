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

import hashlib
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import creek.scaffold
from creek.cli import app
from creek.lint.checks import skill_size_budget
from creek.scaffold import DriftedSkillsError, deploy_skills, detect_drifted_skills

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "creek-tools" / "creek" / "templates"

runner = CliRunner()


def _digest_tree(root: Path) -> dict[Path, str]:
    """Map every file under *root* to the SHA-256 hex digest of its bytes.

    Used to prove a command wrote *nothing*: capture the mapping before
    the command, compare after. Keys are paths relative to *root* so the
    mapping is independent of the temporary directory it lives in.

    Args:
        root: Directory to walk recursively. A missing directory yields
            an empty mapping.

    Returns:
        A dict keyed by the file's path relative to *root*, valued by the
        hex SHA-256 digest of that file's bytes.
    """
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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


# ---------- issue #1306: mediums/*.MEDIUM.md are skills too ----------


def test_skills_sync_refuses_a_drifted_medium_contract_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A hand-edited medium contract blocks sync and survives byte-for-byte.

    Issue #1306: the drift guard only globbed ``*.SKILL.md``, so an
    operator's ``mediums/*.MEDIUM.md`` edits were silently overwritten by
    a plain ``creek skills sync``. The refusal must name the file, exit
    non-zero, and leave the whole Skills tree untouched.
    """
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    before = _digest_tree(skills_dir)

    essay = skills_dir / "mediums" / "essay.MEDIUM.md"
    essay.write_text("# my house style\n", encoding="utf-8")

    result = runner.invoke(app, ["skills", "sync", "--vault", str(vault)])

    assert result.exit_code == 1, result.output
    assert "mediums/essay.MEDIUM.md" in result.output
    assert essay.read_text(encoding="utf-8") == "# my house style\n"

    edited = Path("mediums/essay.MEDIUM.md")
    after = _digest_tree(skills_dir)
    assert {k: v for k, v in after.items() if k != edited} == {
        k: v for k, v in before.items() if k != edited
    }


def test_detect_drifted_skills_reports_a_drifted_medium_contract(
    tmp_path: Path,
) -> None:
    """A single edited ``mediums/*.MEDIUM.md`` is reported as drift."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    essay = skills_dir / "mediums" / "essay.MEDIUM.md"
    essay.write_text("# my house style\n", encoding="utf-8")

    assert detect_drifted_skills(vault) == [skills_dir / "mediums" / "essay.MEDIUM.md"]


def test_detect_drifted_skills_still_reports_a_drifted_schema_skill(
    tmp_path: Path,
) -> None:
    """The already-covered ``*.SKILL.md`` class must not regress."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    (skills_dir / "lint.SKILL.md").write_text("# my lint rules\n", encoding="utf-8")

    assert detect_drifted_skills(vault) == [skills_dir / "lint.SKILL.md"]


def test_detect_drifted_skills_sorts_globally_not_per_glob(tmp_path: Path) -> None:
    """Drifted paths come back in one global sort over vault-relative posix paths.

    ``lint.SKILL.md`` < ``mediums/book-report.MEDIUM.md`` < ``paradox.SKILL.md``.
    A naive ``[*sorted(skill_glob), *sorted(medium_glob)]`` concatenation
    yields ``lint, paradox, mediums/book-report`` instead, so this must be a
    list equality — not a set, not a length.
    """
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    (skills_dir / "lint.SKILL.md").write_text("# my lint rules\n", encoding="utf-8")
    (skills_dir / "paradox.SKILL.md").write_text("# my paradoxes\n", encoding="utf-8")
    (skills_dir / "mediums" / "book-report.MEDIUM.md").write_text(
        "# my book reports\n",
        encoding="utf-8",
    )

    assert detect_drifted_skills(vault) == [
        skills_dir / "lint.SKILL.md",
        skills_dir / "mediums" / "book-report.MEDIUM.md",
        skills_dir / "paradox.SKILL.md",
    ]


def test_detect_drifted_skills_returns_deployed_paths_never_template_paths(
    tmp_path: Path,
) -> None:
    """Reported paths live in the vault so the operator can open them."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    (skills_dir / "lint.SKILL.md").write_text("# my lint rules\n", encoding="utf-8")
    (skills_dir / "mediums" / "essay.MEDIUM.md").write_text(
        "# my house style\n",
        encoding="utf-8",
    )

    drifted = detect_drifted_skills(vault)
    assert len(drifted) == 2, drifted
    template_skills = TEMPLATES_DIR / "skills"
    for path in drifted:
        assert path.is_relative_to(skills_dir), path
        assert not path.is_relative_to(template_skills), path


def test_detect_drifted_skills_tolerates_missing_directories(tmp_path: Path) -> None:
    """A vault missing ``mediums/`` — or ``Skills/`` entirely — is not an error."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    shutil.rmtree(skills_dir / "mediums")
    assert detect_drifted_skills(vault) == []

    shutil.rmtree(skills_dir)
    assert detect_drifted_skills(vault) == []


def test_deploy_skills_handles_a_template_tree_with_no_mediums_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stripped wheel whose template tree has no ``mediums/`` still deploys.

    Neither deployment nor drift detection may assume the medium
    contracts exist on disk.
    """
    stripped = tmp_path / "stripped-templates"
    stripped.mkdir()
    (stripped / "solo.SKILL.md").write_text("# solo\n", encoding="utf-8")
    monkeypatch.setattr(creek.scaffold, "SKILLS_TEMPLATE_DIR", stripped)

    vault = tmp_path / "vault"
    assert deploy_skills(vault, force=True).written == 1
    assert detect_drifted_skills(vault) == []


def test_deploy_skills_preserves_operator_authored_extra_files(tmp_path: Path) -> None:
    """Operator-authored skills the templates never shipped survive a deploy.

    Pins against a ``copytree``-style "fix" that would mirror the template
    tree and delete everything the operator added.
    """
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    mine = skills_dir / "mine.SKILL.md"
    mine.write_text("# my own schema skill\n", encoding="utf-8")
    custom = skills_dir / "mediums" / "custom.MEDIUM.md"
    custom.write_text("# my own medium contract\n", encoding="utf-8")

    assert detect_drifted_skills(vault) == []

    deploy_skills(vault, force=True)

    assert mine.read_text(encoding="utf-8") == "# my own schema skill\n"
    assert custom.read_text(encoding="utf-8") == "# my own medium contract\n"
    assert detect_drifted_skills(vault) == []


def test_deploy_skills_refuses_drift_and_labels_paths_under_the_skills_root(
    tmp_path: Path,
) -> None:
    """Without ``force`` the API raises, labelling both drifted classes."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    (skills_dir / "lint.SKILL.md").write_text("# my lint rules\n", encoding="utf-8")
    essay = skills_dir / "mediums" / "essay.MEDIUM.md"
    essay.write_text("# my house style\n", encoding="utf-8")

    with pytest.raises(DriftedSkillsError) as excinfo:
        deploy_skills(vault)

    assert list(excinfo.value.labels) == [
        "lint.SKILL.md",
        "mediums/essay.MEDIUM.md",
    ]
    assert essay.read_text(encoding="utf-8") == "# my house style\n"


def test_skills_sync_force_restores_both_classes_and_backs_up_operator_bytes(
    tmp_path: Path,
) -> None:
    """``--force`` re-deploys both skill classes, keeping ``.bak`` sidecars."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    skills_dir = vault / "00-Creek-Meta" / "Skills"
    lint = skills_dir / "lint.SKILL.md"
    lint.write_text("# my lint rules\n", encoding="utf-8")
    essay = skills_dir / "mediums" / "essay.MEDIUM.md"
    essay.write_text("# my house style\n", encoding="utf-8")

    result = runner.invoke(app, ["skills", "sync", "--vault", str(vault), "--force"])
    assert result.exit_code == 0, result.output

    canonical_skills = TEMPLATES_DIR / "skills"
    assert lint.read_bytes() == (canonical_skills / "lint.SKILL.md").read_bytes()
    assert (
        essay.read_bytes()
        == (canonical_skills / "mediums" / "essay.MEDIUM.md").read_bytes()
    )

    lint_backup = skills_dir / "lint.SKILL.md.bak"
    essay_backup = skills_dir / "mediums" / "essay.MEDIUM.md.bak"
    assert lint_backup.read_text(encoding="utf-8") == "# my lint rules\n"
    assert essay_backup.read_text(encoding="utf-8") == "# my house style\n"


def test_backup_sidecars_are_invisible_to_drift_detection_and_lint(
    tmp_path: Path,
) -> None:
    """``.bak`` sidecars must not be mistaken for skill files by anything.

    The operator's saved versions are deliberately over the size budget so
    a glob that accidentally swept them in (``*.md``, ``*.SKILL.md*``)
    would surface as a ``skill-size`` finding.
    """
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    bloated = "word " * (skill_size_budget.SKILL_BUDGET_WORDS + 100)
    skills_dir = vault / "00-Creek-Meta" / "Skills"
    (skills_dir / "lint.SKILL.md").write_text(bloated, encoding="utf-8")
    (skills_dir / "mediums" / "essay.MEDIUM.md").write_text(bloated, encoding="utf-8")

    result = runner.invoke(app, ["skills", "sync", "--vault", str(vault), "--force"])
    assert result.exit_code == 0, result.output

    lint_backup = skills_dir / "lint.SKILL.md.bak"
    essay_backup = skills_dir / "mediums" / "essay.MEDIUM.md.bak"
    assert lint_backup.read_text(encoding="utf-8") == bloated
    assert essay_backup.read_text(encoding="utf-8") == bloated

    assert detect_drifted_skills(vault) == []

    globbed = [
        *sorted(skills_dir.glob("*.SKILL.md")),
        *sorted(skills_dir.glob("mediums/*.MEDIUM.md")),
    ]
    assert [p for p in globbed if p.name.endswith(".bak")] == []

    findings = skill_size_budget.run(vault).findings
    assert [f for f in findings if ".bak" in f] == []


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


def test_init_refresh_refuses_a_drifted_medium_and_deploys_nothing(
    tmp_path: Path,
) -> None:
    """``init --refresh`` pre-flights the drift check and aborts atomically.

    Issue #1306: ``--refresh`` overwrote hand-edited medium contracts. The
    refusal has to happen *before* any deployment, so not one byte of the
    vault — folders, ontology, AGENTS.md included — may change.
    """
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    user_note = vault / "01-Fragments" / "Journal" / "mine.md"
    user_note.write_text("dear diary\n", encoding="utf-8")
    essay = vault / "00-Creek-Meta" / "Skills" / "mediums" / "essay.MEDIUM.md"
    essay.write_text("# my house style\n", encoding="utf-8")

    before = _digest_tree(vault)

    refresh = runner.invoke(app, ["init", "--vault", str(vault), "--refresh"])

    assert refresh.exit_code == 1, refresh.output
    assert "mediums/essay.MEDIUM.md" in refresh.output
    assert essay.read_text(encoding="utf-8") == "# my house style\n"
    assert user_note.read_text(encoding="utf-8") == "dear diary\n"
    assert _digest_tree(vault) == before


def test_init_refresh_refusal_precedes_the_folder_scaffold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drift pre-flight must precede ``scaffold_vault``, not just the copy.

    Its sibling test above cannot see the difference: re-scaffolding an
    already-initialised vault rewrites byte-identical content, so a
    whole-vault digest stays equal even if the folder step ran. Deleting
    the pre-flight in ``deploy_canonical`` as "redundant" — the guard
    inside ``deploy_skills`` would still protect the skill bytes — would
    therefore break nothing visible, while quietly letting a refused
    refresh create canonical folders. Pin it with a template tree that
    ships a directory the vault does not have yet.
    """
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    upgraded_templates = tmp_path / "upgraded-vault-template"
    (upgraded_templates / "99-Brand-New").mkdir(parents=True)
    (upgraded_templates / "99-Brand-New" / ".gitkeep").write_text(
        "",
        encoding="utf-8",
    )
    monkeypatch.setattr(creek.scaffold, "VAULT_TEMPLATE_DIR", upgraded_templates)

    essay = vault / "00-Creek-Meta" / "Skills" / "mediums" / "essay.MEDIUM.md"
    essay.write_text("# my house style\n", encoding="utf-8")

    refresh = runner.invoke(app, ["init", "--vault", str(vault), "--refresh"])
    assert refresh.exit_code == 1, refresh.output
    assert not (vault / "99-Brand-New").exists(), (
        "a refused refresh deployed the new canonical folder: the drift "
        "pre-flight ran after scaffold_vault instead of before it"
    )

    forced = runner.invoke(
        app,
        ["init", "--vault", str(vault), "--refresh", "--force"],
    )
    assert forced.exit_code == 0, forced.output
    assert (vault / "99-Brand-New").is_dir(), (
        "the control half failed: --force did not deploy the folder either, "
        "so the assertion above proves nothing"
    )


def test_init_refresh_force_restores_canonical_bytes_and_keeps_a_backup(
    tmp_path: Path,
) -> None:
    """``init --refresh --force`` overwrites drift but preserves a ``.bak``."""
    vault = tmp_path / "vault"
    init_result = runner.invoke(app, ["init", "--vault", str(vault)])
    assert init_result.exit_code == 0, init_result.output

    essay = vault / "00-Creek-Meta" / "Skills" / "mediums" / "essay.MEDIUM.md"
    essay.write_text("# my house style\n", encoding="utf-8")

    refresh = runner.invoke(
        app,
        ["init", "--vault", str(vault), "--refresh", "--force"],
    )
    assert refresh.exit_code == 0, refresh.output

    canonical = TEMPLATES_DIR / "skills" / "mediums" / "essay.MEDIUM.md"
    assert essay.read_bytes() == canonical.read_bytes()

    backup = essay.with_name("essay.MEDIUM.md.bak")
    assert backup.read_text(encoding="utf-8") == "# my house style\n"
