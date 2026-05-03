"""Tests for the creek purge module.

Covers both the :class:`creek.purge.PurgeEngine` directly (fragment,
source, classifications, daterange, vault) and the ``creek purge``
CLI subcommands. Uses fixture vaults seeded with small fragment
corpora per test.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.purge import PurgeAuditEntry, PurgeAuditLog, PurgeEngine, PurgeResult
from creek.purge.engine import VAULT_PURGE_CONFIRMATION

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

# ---------------------------------------------------------------------------
# Vault fixtures
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    """Create a minimal vault with the directories purge needs.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root path.
    """
    vault = tmp_path / "vault"
    for d in [
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
        "01-Fragments/Messages",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis",
    ]:
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _write_fragment(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    platform: str = "claude",
    subfolder: str = "Conversations",
    created: datetime | None = None,
    threads: list[str] | None = None,
    eddies: list[str] | None = None,
    body: str = "Some body content.",
    frequency_primary: str = "F3",
) -> Path:
    """Write a fragment markdown file.

    Args:
        vault: Vault root.
        frag_id: Fragment ID.
        title: Fragment title.
        platform: Source platform.
        subfolder: Subfolder under 01-Fragments/.
        created: Optional creation datetime.
        threads: Optional list of thread IDs the fragment belongs to.
        eddies: Optional list of eddy IDs.
        body: Markdown body content.
        frequency_primary: Primary frequency classification value.

    Returns:
        Path to the written fragment file.
    """
    target = vault / "01-Fragments" / subfolder / f"{title}.md"
    metadata: dict[str, object] = {
        "id": frag_id,
        "title": title,
        "type": "fragment",
        "source": {"platform": platform, "original_file": f"{frag_id}.json"},
        "threads": threads or [],
        "eddies": eddies or [],
        "frequency": {"primary": frequency_primary, "secondary": []},
        "wavelength": {
            "phase": "rising",
            "mode": "inhabit",
            "orientation": "do",
            "dosage": "medicine",
            "color": "orange",
            "descriptor": "bright",
        },
        "voice": {"voice_register": "analytical", "confidence": "settled"},
    }
    if created is not None:
        metadata["created"] = created.isoformat()
    post = frontmatter.Post(content=body, **metadata)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_thread(
    vault: Path,
    thread_id: str,
    title: str,
    *,
    fragment_count: int = 1,
) -> Path:
    """Write a thread markdown file.

    Args:
        vault: Vault root.
        thread_id: Thread ID.
        title: Thread title.
        fragment_count: Initial fragment count.

    Returns:
        Path to the thread file.
    """
    target = vault / "02-Threads" / "Active" / f"{title}.md"
    post = frontmatter.Post(
        content="",
        id=thread_id,
        title=title,
        type="thread",
        status="active",
        fragment_count=fragment_count,
    )
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _write_eddy(
    vault: Path,
    eddy_id: str,
    title: str,
    *,
    fragment_count: int = 1,
) -> Path:
    """Write an eddy markdown file.

    Args:
        vault: Vault root.
        eddy_id: Eddy ID.
        title: Eddy title.
        fragment_count: Initial fragment count.

    Returns:
        Path to the eddy file.
    """
    target = vault / "03-Eddies" / f"{title}.md"
    post = frontmatter.Post(
        content="",
        id=eddy_id,
        title=title,
        type="eddy",
        fragment_count=fragment_count,
    )
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Engine tests — purge_fragment
# ---------------------------------------------------------------------------


def test_fragment_purge_removes_file(tmp_path: Path) -> None:
    """Purging a fragment deletes the underlying markdown file."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.fragments_affected == 1
    assert str(frag) in result.deleted_files
    assert not frag.exists()


def test_fragment_purge_missing_returns_empty(tmp_path: Path) -> None:
    """Purging a nonexistent fragment is a no-op with a clean result."""
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-missing")

    assert result.fragments_affected == 0
    assert result.deleted_files == []


def test_fragment_purge_removes_wikilinks(tmp_path: Path) -> None:
    """Wiki-links pointing at the purged title are scrubbed from other files."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    linker = _write_fragment(
        vault,
        "frag-B",
        "Beta",
        body="See [[Alpha]] and [[Alpha|alias]] and [[Beta]].",
    )
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.wikilinks_removed == 2
    post = frontmatter.load(str(linker))
    assert "[[Alpha]]" not in post.content
    assert "[[Alpha|alias]]" not in post.content
    assert "[[Beta]]" in post.content


def test_fragment_purge_decrements_thread_counts(tmp_path: Path) -> None:
    """Threads referenced in the fragment have fragment_count decremented."""
    vault = _make_vault(tmp_path)
    _write_thread(vault, "thread-1", "Waves", fragment_count=3)
    _write_fragment(vault, "frag-A", "Alpha", threads=["thread-1"])
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.threads_updated == 1
    thread_post = frontmatter.load(str(vault / "02-Threads/Active/Waves.md"))
    assert thread_post["fragment_count"] == 2


def test_fragment_purge_decrements_eddy_counts(tmp_path: Path) -> None:
    """Eddies referenced in the fragment have fragment_count decremented."""
    vault = _make_vault(tmp_path)
    _write_eddy(vault, "eddy-1", "Whirl", fragment_count=2)
    _write_fragment(vault, "frag-A", "Alpha", eddies=["eddy-1"])
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.eddies_updated == 1
    eddy_post = frontmatter.load(str(vault / "03-Eddies/Whirl.md"))
    assert eddy_post["fragment_count"] == 1


def test_fragment_purge_dry_run_preserves_everything(tmp_path: Path) -> None:
    """Dry-run records intended actions without touching disk."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")
    linker = _write_fragment(vault, "frag-B", "Beta", body="[[Alpha]]")
    engine = PurgeEngine(vault, dry_run=True)

    result = engine.purge_fragment("frag-A")

    assert result.dry_run is True
    assert result.fragments_affected == 1
    assert result.wikilinks_removed == 1
    assert frag.exists()
    assert "[[Alpha]]" in linker.read_text(encoding="utf-8")


def test_fragment_purge_writes_audit(tmp_path: Path) -> None:
    """A purge_fragment call appends exactly one audit entry."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    engine.purge_fragment("frag-A")

    entries = PurgeAuditLog(vault).read()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.operation == "fragment"
    assert entry.target == "frag-A"
    assert entry.count == 1
    assert entry.operator == "human via CLI"
    assert entry.dry_run is False


# ---------------------------------------------------------------------------
# Engine tests — purge_source
# ---------------------------------------------------------------------------


def test_source_purge_removes_matching_fragments(tmp_path: Path) -> None:
    """Every fragment from the named source is deleted."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    _write_fragment(
        vault,
        "frag-B",
        "Bravo",
        platform="discord",
        subfolder="Messages",
    )
    _write_fragment(vault, "frag-C", "Charlie", platform="claude")
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.fragments_affected == 2
    remaining = list((vault / "01-Fragments").rglob("*.md"))
    assert len(remaining) == 1
    assert remaining[0].name == "Bravo.md"


def test_source_count_matches(tmp_path: Path) -> None:
    """count_fragments_from_source returns the expected number."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    _write_fragment(vault, "frag-B", "Bravo", platform="claude")
    _write_fragment(
        vault,
        "frag-C",
        "Charlie",
        platform="discord",
        subfolder="Messages",
    )
    engine = PurgeEngine(vault)

    assert engine.count_fragments_from_source("claude") == 2
    assert engine.count_fragments_from_source("discord") == 1
    assert engine.count_fragments_from_source("gdrive") == 0


# ---------------------------------------------------------------------------
# Engine tests — purge_classifications
# ---------------------------------------------------------------------------


def test_classifications_purge_resets_fields(tmp_path: Path) -> None:
    """Classification fields reset to unclassified; metadata preserved."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_classifications()

    assert result.classifications_reset == 1
    post = frontmatter.load(str(frag))
    assert post["frequency"] == {"primary": "unclassified", "secondary": []}
    assert post["wavelength"]["phase"] == "unclassified"
    assert post["voice"]["voice_register"] is None
    # Source + timestamps preserved
    assert post["source"]["platform"] == "claude"
    assert post["title"] == "Alpha"


def test_classifications_purge_skips_already_clean(tmp_path: Path) -> None:
    """Fragments already at defaults are not reported as reset."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", frequency_primary="unclassified")
    # Manually overwrite with all defaults so reset is a no-op
    path = vault / "01-Fragments/Conversations/Alpha.md"
    post = frontmatter.load(str(path))
    post["frequency"] = {"primary": "unclassified", "secondary": []}
    post["wavelength"] = {
        "phase": "unclassified",
        "mode": "unclassified",
        "orientation": "unclassified",
        "dosage": "unclassified",
        "color": "unclassified",
        "descriptor": "",
    }
    post["voice"] = {"voice_register": None, "confidence": None}
    path.write_text(frontmatter.dumps(post), encoding="utf-8")

    engine = PurgeEngine(vault)
    result = engine.purge_classifications()

    assert result.classifications_reset == 0


def test_classifications_dry_run_no_writes(tmp_path: Path) -> None:
    """Dry-run reports resets without modifying fragment files."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")
    before = frag.read_text(encoding="utf-8")
    engine = PurgeEngine(vault, dry_run=True)

    result = engine.purge_classifications()

    assert result.classifications_reset == 1
    assert frag.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Engine tests — purge_daterange
# ---------------------------------------------------------------------------


def test_daterange_purge_deletes_in_range(tmp_path: Path) -> None:
    """Only fragments within [start, end] are deleted."""
    vault = _make_vault(tmp_path)
    _write_fragment(
        vault,
        "frag-old",
        "Old",
        created=datetime(2024, 1, 1, tzinfo=UTC),
    )
    _write_fragment(
        vault,
        "frag-mid",
        "Mid",
        created=datetime(2024, 6, 1, tzinfo=UTC),
    )
    _write_fragment(
        vault,
        "frag-new",
        "New",
        created=datetime(2025, 1, 1, tzinfo=UTC),
    )
    engine = PurgeEngine(vault)

    result = engine.purge_daterange(date(2024, 5, 1), date(2024, 12, 31))

    assert result.fragments_affected == 1
    remaining = {p.name for p in (vault / "01-Fragments").rglob("*.md")}
    assert remaining == {"Old.md", "New.md"}


def test_daterange_purge_invalid_range(tmp_path: Path) -> None:
    """End before start raises ValueError."""
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)

    with pytest.raises(ValueError, match="before start date"):
        engine.purge_daterange(date(2024, 6, 1), date(2024, 1, 1))


# ---------------------------------------------------------------------------
# Engine tests — purge_vault
# ---------------------------------------------------------------------------


def test_vault_purge_requires_confirmation(tmp_path: Path) -> None:
    """Missing/incorrect confirmation raises ValueError."""
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)

    with pytest.raises(ValueError, match="explicit confirmation"):
        engine.purge_vault("wrong phrase")


def test_vault_purge_deletes_content_preserves_structure(
    tmp_path: Path,
) -> None:
    """Vault contents are removed but folder structure stays intact."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_thread(vault, "thread-1", "Waves")
    engine = PurgeEngine(vault)

    result = engine.purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.fragments_affected >= 1
    # Top-level content folders still exist
    assert (vault / "01-Fragments").is_dir()
    assert (vault / "02-Threads").is_dir()
    assert (vault / "03-Eddies").is_dir()
    # But are empty
    assert list((vault / "01-Fragments").iterdir()) == []
    assert list((vault / "02-Threads").iterdir()) == []


def test_vault_purge_dry_run_preserves(tmp_path: Path) -> None:
    """Dry-run vault purge removes nothing."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault, dry_run=True)

    result = engine.purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.dry_run is True
    assert result.fragments_affected >= 1
    assert frag.exists()


# ---------------------------------------------------------------------------
# Audit log tests
# ---------------------------------------------------------------------------


def test_audit_log_appends_entries(tmp_path: Path) -> None:
    """Multiple append() calls accumulate in the log file."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)

    log.append(
        PurgeAuditEntry(operation="fragment", target="frag-A", count=1),
    )
    log.append(
        PurgeAuditEntry(operation="source", target="claude", count=3),
    )

    entries = log.read()
    assert len(entries) == 2
    assert entries[0].target == "frag-A"
    assert entries[1].target == "claude"


def test_audit_log_path_location(tmp_path: Path) -> None:
    """Audit log lives at 00-Creek-Meta/Processing-Log/purge-log.json."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    log.append(PurgeAuditEntry(operation="vault", target="x", count=0))

    expected = vault / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
    assert expected.exists()
    data = json.loads(expected.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert data[0]["operation"] == "vault"


def test_audit_log_recovers_from_corruption(tmp_path: Path) -> None:
    """Malformed log is rebuilt when appending a new entry."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    log.log_path.parent.mkdir(parents=True, exist_ok=True)
    log.log_path.write_text("{not valid json", encoding="utf-8")

    log.append(
        PurgeAuditEntry(operation="fragment", target="frag-A", count=1),
    )

    entries = log.read()
    assert len(entries) == 1


def test_audit_log_read_missing_returns_empty(tmp_path: Path) -> None:
    """read() on a missing log returns an empty list."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    assert log.read() == []


# ---------------------------------------------------------------------------
# PurgeResult model
# ---------------------------------------------------------------------------


def test_purge_result_defaults() -> None:
    """Default PurgeResult has zero counts and empty lists."""
    result = PurgeResult(operation="fragment", target="frag-A")
    assert result.deleted_files == []
    assert result.fragments_affected == 0
    assert result.wikilinks_removed == 0
    assert result.dry_run is False


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_purge_help() -> None:
    """`creek purge --help` lists the five subcommands."""
    result = runner.invoke(app, ["purge", "--help"])
    assert result.exit_code == 0
    assert "fragment" in result.output
    assert "source" in result.output
    assert "classifications" in result.output
    assert "daterange" in result.output
    assert "vault" in result.output


def test_cli_purge_fragment_dry_run(tmp_path: Path) -> None:
    """`creek purge fragment --dry-run` previews but preserves."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        [
            "purge",
            "fragment",
            "frag-A",
            "--vault",
            str(vault),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    assert frag.exists()


def test_cli_purge_fragment_apply(tmp_path: Path) -> None:
    """`creek purge fragment --yes` deletes the fragment."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        ["purge", "fragment", "frag-A", "--vault", str(vault), "--yes"],
    )

    assert result.exit_code == 0
    assert "APPLY" in result.output
    assert not frag.exists()


def test_cli_purge_fragment_interactive_decline(tmp_path: Path) -> None:
    """Declining the interactive prompt aborts the purge."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        ["purge", "fragment", "frag-A", "--vault", str(vault)],
        input="n\n",
    )

    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert frag.exists()


def test_cli_purge_source_shows_count(tmp_path: Path) -> None:
    """`creek purge source` prints the count and purges on confirm."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    _write_fragment(vault, "frag-B", "Bravo", platform="claude")

    result = runner.invoke(
        app,
        ["purge", "source", "claude", "--vault", str(vault), "--yes"],
    )

    assert result.exit_code == 0
    assert "2" in result.output
    assert not (vault / "01-Fragments/Conversations/Alpha.md").exists()
    assert not (vault / "01-Fragments/Conversations/Bravo.md").exists()


def test_cli_purge_classifications_runs(tmp_path: Path) -> None:
    """`creek purge classifications --yes` resets fields."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        ["purge", "classifications", "--vault", str(vault), "--yes"],
    )

    assert result.exit_code == 0
    post = frontmatter.load(str(frag))
    assert post["frequency"]["primary"] == "unclassified"


def test_cli_purge_daterange_invalid(tmp_path: Path) -> None:
    """Invalid ISO date exits with code 2."""
    vault = _make_vault(tmp_path)

    result = runner.invoke(
        app,
        [
            "purge",
            "daterange",
            "not-a-date",
            "2024-12-31",
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 2


def test_cli_purge_daterange_applies(tmp_path: Path) -> None:
    """`creek purge daterange` removes fragments in range."""
    vault = _make_vault(tmp_path)
    _write_fragment(
        vault,
        "frag-old",
        "Old",
        created=datetime.now(tz=UTC) - timedelta(days=400),
    )
    keeper = _write_fragment(
        vault,
        "frag-new",
        "New",
        created=datetime.now(tz=UTC),
    )
    start = (datetime.now(tz=UTC) - timedelta(days=500)).date()
    end = (datetime.now(tz=UTC) - timedelta(days=300)).date()

    result = runner.invoke(
        app,
        [
            "purge",
            "daterange",
            start.isoformat(),
            end.isoformat(),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert not (vault / "01-Fragments/Conversations/Old.md").exists()
    assert keeper.exists()


def test_cli_purge_vault_rejects_bad_confirm(tmp_path: Path) -> None:
    """Wrong confirmation text aborts at the CLI boundary, not in the engine.

    Reviewer flagged that letting :class:`PurgeEngine` raise
    ``ValueError`` for a bad ``--confirm-text`` puts the validation
    deep inside the engine. The CLI should detect the mismatch up
    front and surface a clear, user-facing message that names the
    flag involved.
    """
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        [
            "purge",
            "vault",
            "--vault",
            str(vault),
            "--confirm-text",
            "maybe",
            "--force-non-interactive",
        ],
    )

    assert result.exit_code != 0
    assert frag.exists()
    assert "--confirm-text" in result.output


def test_cli_purge_vault_accepts_exact_confirm(tmp_path: Path) -> None:
    """Correct confirmation text purges the vault (with explicit non-tty opt-in)."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        [
            "purge",
            "vault",
            "--vault",
            str(vault),
            "--confirm-text",
            VAULT_PURGE_CONFIRMATION,
            "--force-non-interactive",
        ],
    )

    assert result.exit_code == 0
    assert not frag.exists()


def test_cli_purge_vault_dry_run(tmp_path: Path) -> None:
    """Dry-run vault purge preserves the vault contents."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        ["purge", "vault", "--vault", str(vault), "--dry-run"],
    )

    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    assert frag.exists()


# ---------------------------------------------------------------------------
# OPS-002: non-interactive purge refusal
# ---------------------------------------------------------------------------


def test_cli_purge_vault_refuses_non_tty_without_force(tmp_path: Path) -> None:
    """Piped stdin is rejected unless --force-non-interactive is set."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        [
            "purge",
            "vault",
            "--vault",
            str(vault),
            "--confirm-text",
            VAULT_PURGE_CONFIRMATION,
        ],
    )

    assert result.exit_code != 0
    assert "non-interactive" in result.output.lower()
    assert frag.exists()


def test_cli_purge_vault_refuses_when_only_yes_piped(tmp_path: Path) -> None:
    """Even with piped 'y' confirmation, non-tty must abort without the flag."""
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        ["purge", "vault", "--vault", str(vault)],
        input="y\n",
    )

    assert result.exit_code != 0
    assert "non-interactive" in result.output.lower()
    assert frag.exists()


def test_cli_purge_vault_force_non_interactive_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`--force-non-interactive` succeeds but emits a WARNING audit entry."""
    import logging

    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    with caplog.at_level(logging.WARNING, logger="creek.cli"):
        result = runner.invoke(
            app,
            [
                "purge",
                "vault",
                "--vault",
                str(vault),
                "--confirm-text",
                VAULT_PURGE_CONFIRMATION,
                "--force-non-interactive",
            ],
        )

    assert result.exit_code == 0
    assert not frag.exists()
    assert any(
        "non-interactive" in record.message.lower()
        for record in caplog.records
        if record.levelno == logging.WARNING
    )


def test_cli_purge_vault_interactive_wrong_path_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interactive run that types the wrong vault path aborts."""
    monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    result = runner.invoke(
        app,
        ["purge", "vault", "--vault", str(vault)],
        input="not-the-right-path\n",
    )

    assert result.exit_code != 0
    assert frag.exists()


def test_cli_purge_vault_interactive_correct_path_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing the absolute vault path interactively confirms the purge."""
    monkeypatch.setattr("creek.cli._is_interactive", lambda: True)

    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")
    abs_vault = str(vault.resolve())

    result = runner.invoke(
        app,
        ["purge", "vault", "--vault", str(vault)],
        input=f"{abs_vault}\n",
    )

    assert result.exit_code == 0, result.output
    assert not frag.exists()


# ---------------------------------------------------------------------------
# Edge case coverage
# ---------------------------------------------------------------------------


def test_fragment_purge_empty_title_skips_scrub(tmp_path: Path) -> None:
    """A fragment with an empty title skips wiki-link scrubbing."""
    vault = _make_vault(tmp_path)
    frag = vault / "01-Fragments" / "Conversations" / "notitle.md"
    post = frontmatter.Post(
        content="body",
        id="frag-NT",
        title="",
        type="fragment",
        source={"platform": "claude"},
        threads=[],
        eddies=[],
    )
    frag.write_text(frontmatter.dumps(post), encoding="utf-8")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-NT")

    assert result.fragments_affected == 1
    assert result.wikilinks_removed == 0


def test_fragment_purge_missing_vault_dir(tmp_path: Path) -> None:
    """Purge against a vault with no 01-Fragments directory is a no-op."""
    vault = tmp_path / "empty"
    vault.mkdir()
    (vault / "00-Creek-Meta" / "Processing-Log").mkdir(parents=True)
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-X")

    assert result.fragments_affected == 0


def test_decrement_counts_skips_missing_folder(tmp_path: Path) -> None:
    """Decrement is a no-op when the thread folder doesn't exist."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta" / "Processing-Log").mkdir(parents=True)
    (vault / "01-Fragments").mkdir()
    engine = PurgeEngine(vault)
    frag = vault / "01-Fragments" / "solo.md"
    frag.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="",
                id="frag-S",
                title="Solo",
                threads=["thread-missing"],
                eddies=[],
                source={"platform": "claude"},
            ),
        ),
        encoding="utf-8",
    )

    result = engine.purge_fragment("frag-S")

    assert result.fragments_affected == 1
    assert result.threads_updated == 0


def test_load_frontmatter_handles_unreadable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When frontmatter.load raises, the file is silently skipped."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    def _raise(_path: str) -> None:
        raise OSError("simulated")

    monkeypatch.setattr("creek.purge.engine.frontmatter.load", _raise)

    result = engine.purge_classifications()
    assert result.classifications_reset == 0


def test_source_platform_helper_missing_source() -> None:
    """_extract_source_platform returns None for non-dict source."""
    from creek.purge.engine import _extract_source_platform

    post = frontmatter.Post(content="", source="string-not-dict")
    assert _extract_source_platform(post) is None


def test_coerce_date_accepts_date_instance() -> None:
    """Plain date values are returned as-is."""
    from creek.purge.engine import _coerce_date

    today = date(2024, 3, 14)
    assert _coerce_date(today) == today


def test_coerce_date_rejects_invalid_string() -> None:
    """Unparseable strings produce None."""
    from creek.purge.engine import _coerce_date

    assert _coerce_date("never") is None
    assert _coerce_date(42) is None


def test_audit_log_empty_file_is_treated_as_empty(tmp_path: Path) -> None:
    """An empty audit log file is treated as zero entries."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    log.log_path.parent.mkdir(parents=True, exist_ok=True)
    log.log_path.write_text("", encoding="utf-8")

    assert log.read() == []


def test_audit_log_non_list_json_is_discarded(tmp_path: Path) -> None:
    """A top-level non-list JSON value is discarded on read."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    log.log_path.parent.mkdir(parents=True, exist_ok=True)
    log.log_path.write_text('{"not": "a list"}', encoding="utf-8")

    assert log.read() == []
