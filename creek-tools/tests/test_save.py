"""Tests for ``creek save`` (FEAT-009).

Covers the destination router, the frontmatter writer, the
``pre_save_filter`` helper added to ``creek/classify/privacy_filter.py``,
and the ``creek save`` CLI command itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.classify.privacy_filter import pre_save_filter
from creek.cli import app
from creek.models import PrivacyTier
from creek.save import (
    INTIMATE_STUB_RELPATH,
    SaveRequest,
    SaveTarget,
    save_to_vault,
    target_directory,
)
from creek.save.router import _TARGET_SUBDIRS

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Provide a minimal vault scaffold for the save module to write under."""
    for relparts in {
        ("00-Creek-Meta",),
        ("01-Fragments",),
        *_TARGET_SUBDIRS.values(),
        ("10-Liminal", "Compost", "intimate-stubs"),
    }:
        (tmp_path.joinpath(*relparts)).mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _make_request(
    target: SaveTarget,
    *,
    body: str = "An answer worth keeping.",
    title: str | None = "Why creeks compound",
    tier: PrivacyTier = PrivacyTier.OPEN,
    full_body: bool = False,
    provenance: tuple[str, ...] = ("frag-aaa", "frag-bbb"),
) -> SaveRequest:
    return SaveRequest(
        target=target,
        body=body,
        title=title,
        tier=tier,
        full_body=full_body,
        provenance=provenance,
        source_kind="manual",
        source_id="conv-001",
        saved_by="tester",
    )


# ---- Router ----


@pytest.mark.parametrize(("target", "parts"), list(_TARGET_SUBDIRS.items()))
def test_router_maps_each_target_to_canonical_subdir(
    vault: Path,
    target: SaveTarget,
    parts: tuple[str, ...],
) -> None:
    """Every target type resolves to its documented vault subdirectory."""
    assert target_directory(vault, target) == vault.joinpath(*parts)


# ---- Writer: each target produces a model-conformant note at the right path ----


@pytest.mark.parametrize("target", list(SaveTarget))
def test_save_writes_note_under_correct_directory(
    vault: Path,
    target: SaveTarget,
) -> None:
    """Each target lands inside the directory the router promised."""
    path = save_to_vault(_make_request(target), vault_path=vault)
    expected_dir = target_directory(vault, target)
    assert path.parent == expected_dir
    assert path.suffix == ".md"
    post = frontmatter.load(str(path))
    assert post["type"] == target.value
    assert post["title"]
    saved_from = post["saved_from"]
    assert saved_from["source_kind"] == "manual"
    assert saved_from["source_id"] == "conv-001"
    assert saved_from["contributing_fragments"] == ["frag-aaa", "frag-bbb"]
    assert saved_from["saved_by"] == "tester"
    assert saved_from["saved_at"].endswith("Z")


def test_thread_save_carries_thread_model_fields(vault: Path) -> None:
    """A ``thread`` save serialises Thread-compatible frontmatter keys."""
    path = save_to_vault(
        _make_request(SaveTarget.THREAD, title="Compounding wikis"),
        vault_path=vault,
    )
    post = frontmatter.load(str(path))
    assert post["type"] == "thread"
    assert post["status"] == "active"


def test_eddy_save_carries_eddy_model_fields(vault: Path) -> None:
    """An ``eddy`` save serialises Eddy-compatible frontmatter keys."""
    path = save_to_vault(_make_request(SaveTarget.EDDY), vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["type"] == "eddy"
    assert "formed" in post.metadata


def test_praxis_save_carries_praxis_model_fields(vault: Path) -> None:
    """A ``praxis`` save serialises Praxis-compatible frontmatter keys."""
    path = save_to_vault(_make_request(SaveTarget.PRAXIS), vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["type"] == "praxis"
    assert post["praxis_type"] == "insight"
    assert post["status"] == "proposed"


# ---- Paradox routing regression ----


def test_paradox_always_lands_in_liminal_paradoxes(vault: Path) -> None:
    """``--target paradox`` is the routing rule that cannot be overridden."""
    request = _make_request(
        SaveTarget.PARADOX,
        body="Two contradictory claims that need preserving, not resolving.",
        title="Both true at once",
    )
    path = save_to_vault(request, vault_path=vault)
    assert path.parent == vault / "10-Liminal" / "Paradoxes"


def test_paradox_tier_is_open_even_if_caller_passes_intimate(vault: Path) -> None:
    """Per the FEAT, a paradox save records *the fact* of the contradiction.

    Tier-filtering for the paradox target is forced to ``open`` so the
    full title and pointer survive into the vault.
    """
    request = _make_request(
        SaveTarget.PARADOX,
        body="The body of the contradiction itself stays out of the vault.",
        tier=PrivacyTier.INTIMATE,
    )
    path = save_to_vault(request, vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["type"] == "paradox"
    assert post["privacy_tier"] == "open"


# ---- pre_save_filter ----


def test_pre_save_filter_open_returns_full_body() -> None:
    """``open`` tier passes the body through unchanged."""
    result = pre_save_filter(
        "Full open answer.",
        tier=PrivacyTier.OPEN,
        title="Open question",
    )
    assert result.vault_body == "Full open answer."
    assert result.stub_body is None
    assert result.stub_relpath is None


def test_pre_save_filter_personal_summarises_by_default() -> None:
    """``personal`` tier writes title + summary, no full body."""
    result = pre_save_filter(
        "A sensitive personal reflection.",
        tier=PrivacyTier.PERSONAL,
        title="Personal moment",
    )
    assert "Personal moment" in result.vault_body
    assert "A sensitive personal reflection." not in result.vault_body
    assert result.stub_body is None
    assert result.stub_relpath is None


def test_pre_save_filter_personal_full_body_when_requested() -> None:
    """Explicit ``--full-body`` lets personal bodies into the vault."""
    result = pre_save_filter(
        "A personal reflection.",
        tier=PrivacyTier.PERSONAL,
        title="Personal moment",
        full_body=True,
    )
    assert result.vault_body == "A personal reflection."
    assert result.stub_body is None


def test_pre_save_filter_intimate_redirects_body_to_gitignored_stubs() -> None:
    """Intimate bodies never reach the vault; they go to the compost stub."""
    result = pre_save_filter(
        "Confessional intimate body.",
        tier=PrivacyTier.INTIMATE,
        title="Intimate moment",
    )
    assert "Intimate moment" in result.vault_body
    assert "Confessional intimate body." not in result.vault_body
    assert result.stub_body == "Confessional intimate body."
    assert result.stub_relpath is not None
    assert result.stub_relpath.parts[:3] == (
        "10-Liminal",
        "Compost",
        "intimate-stubs",
    )


def test_intimate_stub_relpath_constant_matches_gitignored_dir() -> None:
    """The published constant is the canonical gitignored stubs path."""
    assert Path("10-Liminal/Compost/intimate-stubs") == INTIMATE_STUB_RELPATH


# ---- Intimate full-body never lands in the vault tree ----


def test_intimate_save_never_writes_full_body_into_vault(vault: Path) -> None:
    """File-system inspection: vault note has no intimate body content."""
    sensitive = "INTIMATE BODY MARKER 7f3a"
    request = _make_request(
        SaveTarget.UNNAMED,
        body=sensitive,
        title="Confession",
        tier=PrivacyTier.INTIMATE,
    )
    written = save_to_vault(request, vault_path=vault)
    post = frontmatter.load(str(written))
    assert sensitive not in post.content
    saved_from = post["saved_from"]
    assert saved_from.get("intimate_body_pointer", "").startswith(
        "10-Liminal/Compost/intimate-stubs/",
    )
    # The body lives only inside the compost stubs directory.
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    stub_files = list(stubs_dir.glob("*.md"))
    assert stub_files, "intimate save must drop a stub"
    assert any(sensitive in f.read_text(encoding="utf-8") for f in stub_files)
    # And nowhere else under the tracked vault tree.
    leakages = [
        p
        for p in vault.rglob("*.md")
        if "intimate-stubs" not in p.parts
        and sensitive in p.read_text(encoding="utf-8")
    ]
    assert leakages == []


# ---- CLI ----


runner = CliRunner()


def _scaffold_vault(root: Path) -> Path:
    """Create a vault layout sufficient for ``creek save``."""
    for parts in {
        ("00-Creek-Meta",),
        ("01-Fragments",),
        *_TARGET_SUBDIRS.values(),
        ("10-Liminal", "Compost", "intimate-stubs"),
    }:
        root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    return root


def test_cli_save_help_lists_targets() -> None:
    """``creek save --help`` advertises the six target types."""
    result = runner.invoke(app, ["save", "--help"])
    assert result.exit_code == 0
    for target in SaveTarget:
        assert target.value in result.output


def test_cli_save_refuses_without_tier_or_provenance(tmp_path: Path) -> None:
    """No tier + no provenance is the regression case: must refuse loudly."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("Hello", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(body_file),
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "tier" in result.output.lower()


def test_cli_save_thread_round_trip(tmp_path: Path) -> None:
    """Integration: --target thread + --body + --provenance + --tier open works."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("A thoughtful synthesis.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(body_file),
            "--title",
            "How creeks compound",
            "--provenance",
            "frag-001,frag-002",
            "--source",
            "claude-session-xyz",
            "--source-kind",
            "claude-session",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    written = list((vault / "02-Threads" / "Active").glob("*.md"))
    assert len(written) == 1
    post = frontmatter.load(str(written[0]))
    assert post["type"] == "thread"
    assert post["title"] == "How creeks compound"
    assert post["saved_from"]["contributing_fragments"] == ["frag-001", "frag-002"]
    assert post["saved_from"]["source_kind"] == "claude-session"
    assert "A thoughtful synthesis." in post.content


def test_cli_save_paradox_routing(tmp_path: Path) -> None:
    """A paradox save lands in 10-Liminal/Paradoxes via the CLI too."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("Two things cannot both be true.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "paradox",
            "--body",
            str(body_file),
            "--title",
            "Contradiction X",
            "--provenance",
            "frag-001",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    written = list((vault / "10-Liminal" / "Paradoxes").glob("*.md"))
    assert len(written) == 1


def test_save_falls_back_to_first_body_line_when_title_missing(vault: Path) -> None:
    """When ``--title`` is omitted, the first non-empty body line becomes it."""
    request = SaveRequest(
        target=SaveTarget.UNNAMED,
        body="\n\n# Derived from body\n\nThe rest of the answer.",
        title=None,
        tier=PrivacyTier.OPEN,
        provenance=("frag-001",),
        source_kind="manual",
        source_id="conv-1",
        saved_by="tester",
    )
    path = save_to_vault(request, vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["title"] == "Derived from body"


def test_save_retries_on_filename_collision(vault: Path) -> None:
    """Two saves with the same title produce two files with counter suffixes."""
    first = save_to_vault(_make_request(SaveTarget.EDDY), vault_path=vault)
    second = save_to_vault(_make_request(SaveTarget.EDDY), vault_path=vault)
    assert first != second
    assert second.parent == first.parent


def test_intimate_stub_collision_increments_suffix(vault: Path) -> None:
    """A second intimate save with the same title gets a -1 stub suffix."""
    request = _make_request(
        SaveTarget.UNNAMED,
        body="First intimate body",
        title="repeat me",
        tier=PrivacyTier.INTIMATE,
    )
    save_to_vault(request, vault_path=vault)
    save_to_vault(
        SaveRequest(
            target=SaveTarget.UNNAMED,
            body="Second intimate body",
            title="repeat me",
            tier=PrivacyTier.INTIMATE,
            provenance=("frag-aaa",),
            source_kind="manual",
            source_id="conv-001",
            saved_by="tester",
        ),
        vault_path=vault,
    )
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    stubs = sorted(stubs_dir.glob("*.md"))
    assert len(stubs) == 2
    assert any(s.stem.endswith("-1") for s in stubs)


def test_intimate_stub_records_saved_at_and_saved_by(vault: Path) -> None:
    """Stub frontmatter carries the full ``saved_from`` block.

    The stub directory is gitignored — without a ``saved_at`` field on
    the stub itself, operators recovering from disk would have no
    timestamp to reason about. The block also identifies who saved it.
    """
    request = _make_request(
        SaveTarget.UNNAMED,
        body="Intimate confession.",
        title="With timestamp",
        tier=PrivacyTier.INTIMATE,
    )
    save_to_vault(request, vault_path=vault)
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    stub = next(iter(stubs_dir.glob("*.md")))
    post = frontmatter.load(str(stub))
    saved_from = post["saved_from"]
    assert saved_from.get("saved_at")
    assert saved_from["saved_by"] == "tester"
    # The stub *is* the body, so it must not point back at itself or
    # leak an intimate_body_pointer.
    assert "intimate_body_pointer" not in saved_from


def test_cli_save_unknown_source_kind_exits_two(tmp_path: Path) -> None:
    """Reject free-form ``--source-kind`` values with a clear error."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("body", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(body_file),
            "--provenance",
            "frag-001",
            "--source-kind",
            "smoke-signal",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "source-kind" in result.output.lower()


def test_cli_save_missing_body_path_exits_two(tmp_path: Path) -> None:
    """A nonexistent ``--body`` path is a hard error, not a silent inline body.

    Regression for the bug where ``--body /typo.md`` would file the
    path string itself as the note body. For a privacy-sensitive
    primitive that silent fallback is dangerous: the operator believes
    they filed their answer when they actually filed a path.
    """
    vault = _scaffold_vault(tmp_path / "vault")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(tmp_path / "does-not-exist.md"),
            "--provenance",
            "frag-001",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output.lower()
    # And nothing got written into the vault — no thread file.
    assert not list((vault / "02-Threads" / "Active").glob("*.md"))


def test_cli_save_unknown_target_exits_two(tmp_path: Path) -> None:
    """An unknown --target value exits 2 with a hint listing valid options."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("body", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "nope",
            "--body",
            str(body_file),
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "target" in result.output.lower()
