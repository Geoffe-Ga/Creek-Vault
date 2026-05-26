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
from creek.purge.engine import VAULT_PURGE_CONFIRMATION, _str_list

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

# ---------------------------------------------------------------------------
# Vault fixtures
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    """Create a minimal vault with the directories purge needs.

    Also seeds ``00-Creek-Meta/creek_config.yaml`` — the marker file
    ``purge_vault`` checks for under GAP-003. Tests that want to
    simulate a non-Creek directory create their own decoy without
    going through this helper.

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
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# minimal marker for GAP-003\n",
        encoding="utf-8",
    )
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["thread-1", "thread-2"], ["thread-1", "thread-2"]),
        ([1, 2], ["1", "2"]),
        (None, []),
        ("thread-1", []),
        (7, []),
        ({"thread": "x"}, []),
    ],
)
def test_str_list_coerces_only_lists(value: object, expected: list[str]) -> None:
    """`_str_list` stringifies list members and yields [] for non-lists."""
    assert _str_list(value) == expected


def test_fragment_purge_zeroes_non_numeric_fragment_count(tmp_path: Path) -> None:
    """A thread whose fragment_count is non-numeric decrements to 0, not a crash."""
    vault = _make_vault(tmp_path)
    thread = vault / "02-Threads" / "Active" / "Weird.md"
    thread.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="",
                id="thread-1",
                title="Weird",
                type="thread",
                fragment_count=["not", "numeric"],
            ),
        ),
        encoding="utf-8",
    )
    _write_fragment(vault, "frag-A", "Alpha", threads=["thread-1"])

    PurgeEngine(vault).purge_fragment("frag-A")

    thread_post = frontmatter.load(str(thread))
    assert thread_post["fragment_count"] == 0


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
    """A purge_fragment call appends one intent + one outcome entry (GAP-002).

    ``embeddings_removed`` is *zero* on the outcome here because no
    embeddings cache was built (GAP-001). The intent line carries the
    planned scope; the outcome line carries the real counts.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    engine.purge_fragment("frag-A")

    entries = PurgeAuditLog(vault).read()
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert intent.operation == "fragment"
    assert intent.criteria == {"fragment_id": "frag-A"}
    assert outcome.phase == "outcome"
    assert outcome.status == "complete"
    assert outcome.operation == "fragment"
    assert outcome.criteria == {"fragment_id": "frag-A"}
    assert outcome.affected_fragments == ["frag-A"]
    assert outcome.fragments_deleted == 1
    assert outcome.embeddings_removed == 0
    assert outcome.operator == "human via CLI"
    assert outcome.dry_run is False


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
# Engine tests — purge_source_path with match modes (INC-008)
# ---------------------------------------------------------------------------


def _write_fragment_with_original(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    original_file: str,
) -> Path:
    """Write a fragment with an explicit ``source.original_file`` path."""
    target = vault / "01-Fragments" / "Conversations" / f"{title}.md"
    metadata: dict[str, object] = {
        "id": frag_id,
        "title": title,
        "type": "fragment",
        "source": {"platform": "claude", "original_file": original_file},
        "threads": [],
        "eddies": [],
        "frequency": {"primary": "F3", "secondary": []},
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
    target.write_text(frontmatter.dumps(frontmatter.Post(content="x", **metadata)))
    return target


def test_purge_source_path_exact_match(tmp_path: Path) -> None:
    """``--match exact`` deletes only the fragment with the exact path (INC-008)."""
    vault = _make_vault(tmp_path)
    _write_fragment_with_original(
        vault, "frag-A", "Alpha", original_file="/exports/claude/2026-04-28.json"
    )
    _write_fragment_with_original(
        vault, "frag-B", "Bravo", original_file="/exports/claude/2026-04-29.json"
    )
    engine = PurgeEngine(vault)

    result = engine.purge_source_path("/exports/claude/2026-04-28.json", match="exact")

    assert result.fragments_affected == 1
    remaining = list((vault / "01-Fragments").rglob("*.md"))
    assert len(remaining) == 1
    assert remaining[0].name == "Bravo.md"


def test_purge_source_path_substring_match(tmp_path: Path) -> None:
    """``--match substring`` deletes every fragment whose path contains the term."""
    vault = _make_vault(tmp_path)
    _write_fragment_with_original(
        vault, "frag-A", "Alpha", original_file="/exports/2026-04-28.json"
    )
    _write_fragment_with_original(
        vault, "frag-B", "Bravo", original_file="/exports/2026-04-29.json"
    )
    _write_fragment_with_original(
        vault, "frag-C", "Charlie", original_file="/notes/diary.md"
    )
    engine = PurgeEngine(vault)

    result = engine.purge_source_path("/exports/", match="substring")

    assert result.fragments_affected == 2
    remaining = list((vault / "01-Fragments").rglob("*.md"))
    assert len(remaining) == 1
    assert remaining[0].name == "Charlie.md"


def test_purge_source_path_regex_match(tmp_path: Path) -> None:
    """``--match regex`` accepts a regex pattern and deletes matching fragments."""
    vault = _make_vault(tmp_path)
    _write_fragment_with_original(
        vault, "frag-A", "Alpha", original_file="/exports/2026-04-28.json"
    )
    _write_fragment_with_original(
        vault, "frag-B", "Bravo", original_file="/exports/2026-04-29.json"
    )
    _write_fragment_with_original(
        vault, "frag-C", "Charlie", original_file="/notes/diary.md"
    )
    engine = PurgeEngine(vault)

    result = engine.purge_source_path(r"2026-04-2[89]\.json$", match="regex")

    assert result.fragments_affected == 2
    remaining = list((vault / "01-Fragments").rglob("*.md"))
    assert len(remaining) == 1
    assert remaining[0].name == "Charlie.md"


def test_purge_source_path_invalid_regex_raises(tmp_path: Path) -> None:
    """A bad regex fails fast with ``ValueError`` rather than silently mismatching."""
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)

    with pytest.raises(ValueError, match="Invalid regex"):
        engine.purge_source_path("[unclosed", match="regex")


def test_purge_source_path_unknown_match_mode_raises(tmp_path: Path) -> None:
    """Unknown match modes are rejected up-front."""
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)

    with pytest.raises(ValueError, match="Unknown match mode"):
        engine.purge_source_path("/anywhere", match="fuzzy")


def test_purge_source_path_audit_records_match_mode(tmp_path: Path) -> None:
    """The audit entry captures both ``source_path`` and ``match`` (INC-008)."""
    vault = _make_vault(tmp_path)
    _write_fragment_with_original(
        vault, "frag-A", "Alpha", original_file="/exports/2026-04-28.json"
    )
    engine = PurgeEngine(vault)

    engine.purge_source_path("/exports/", match="substring")

    audit_entries = PurgeAuditLog(vault).read()
    assert audit_entries[-1].operation == "source-path"
    assert audit_entries[-1].criteria["source_path"] == "/exports/"
    assert audit_entries[-1].criteria["match"] == "substring"


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
# GAP-003 — purge_vault refuses directories that are not Creek vaults
# ---------------------------------------------------------------------------


def test_vault_purge_refuses_directory_with_no_meta_folder(
    tmp_path: Path,
) -> None:
    """A directory missing ``00-Creek-Meta/`` is not a Creek vault (GAP-003).

    The decoy looks vault-ish (numeric-prefix folders, ``.md`` files
    inside) but was never ``creek init``-ed. Engine must refuse and the
    decoy files must survive.
    """
    decoy = tmp_path / "not-a-vault"
    (decoy / "01-Fragments").mkdir(parents=True)
    important = decoy / "01-Fragments" / "important.md"
    important.write_text("hello", encoding="utf-8")
    engine = PurgeEngine(decoy)

    with pytest.raises(ValueError, match="does not appear to be a Creek vault"):
        engine.purge_vault(VAULT_PURGE_CONFIRMATION)

    assert important.exists()


def test_vault_purge_refuses_directory_with_meta_but_no_config(
    tmp_path: Path,
) -> None:
    """``00-Creek-Meta/`` alone isn't enough — ``creek_config.yaml`` is the marker."""
    decoy = tmp_path / "half-vault"
    (decoy / "00-Creek-Meta").mkdir(parents=True)
    (decoy / "01-Fragments").mkdir(parents=True)
    important = decoy / "01-Fragments" / "important.md"
    important.write_text("hello", encoding="utf-8")
    engine = PurgeEngine(decoy)

    with pytest.raises(ValueError, match="does not appear to be a Creek vault"):
        engine.purge_vault(VAULT_PURGE_CONFIRMATION)

    assert important.exists()


def test_vault_purge_error_message_names_the_marker_file(
    tmp_path: Path,
) -> None:
    """The error names the marker file the engine looked for (criterion 2)."""
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    engine = PurgeEngine(decoy)

    with pytest.raises(ValueError, match=r"creek_config\.yaml"):
        engine.purge_vault(VAULT_PURGE_CONFIRMATION)


def test_vault_purge_accepts_directory_with_marker(tmp_path: Path) -> None:
    """A directory carrying the marker is recognised as a Creek vault."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")

    # No exception raised — the marker created by _make_vault suffices.
    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)


def test_vault_purge_writes_no_audit_when_marker_missing(
    tmp_path: Path,
) -> None:
    """Marker check runs *before* the intent audit entry (criterion 1).

    A refusal must leave the audit log untouched; otherwise an operator
    can't tell the marker-missing case apart from a successful purge by
    looking at the log alone.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    engine = PurgeEngine(decoy)

    with pytest.raises(ValueError, match="does not appear to be a Creek vault"):
        engine.purge_vault(VAULT_PURGE_CONFIRMATION)

    audit_path = decoy / "00-Creek-Meta" / "audit" / "purge.jsonl"
    assert not audit_path.exists()


def test_cli_purge_vault_refuses_non_creek_directory(tmp_path: Path) -> None:
    """``creek purge vault`` exits non-zero with a clear message (criterion 2)."""
    decoy = tmp_path / "decoy"
    (decoy / "01-Fragments").mkdir(parents=True)
    survivor = decoy / "01-Fragments" / "x.md"
    survivor.write_text("hi", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "purge",
            "vault",
            "--vault",
            str(decoy),
            "--confirm-text",
            VAULT_PURGE_CONFIRMATION,
            "--force-non-interactive",
        ],
    )

    assert result.exit_code != 0
    assert "does not appear to be a Creek vault" in result.output
    assert survivor.exists()


# ---------------------------------------------------------------------------
# Audit log tests
# ---------------------------------------------------------------------------


def test_audit_log_appends_entries(tmp_path: Path) -> None:
    """Multiple append() calls accumulate in the log file."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)

    log.append(
        PurgeAuditEntry(
            operation="fragment",
            criteria={"fragment_id": "frag-A"},
            affected_fragments=["frag-A"],
            fragments_deleted=1,
        ),
    )
    log.append(
        PurgeAuditEntry(
            operation="source",
            criteria={"source_type": "claude"},
            fragments_deleted=3,
        ),
    )

    entries = log.read()
    assert len(entries) == 2
    assert entries[0].criteria["fragment_id"] == "frag-A"
    assert entries[1].criteria["source_type"] == "claude"


def test_audit_log_path_location(tmp_path: Path) -> None:
    """Audit log lives at 00-Creek-Meta/audit/purge.jsonl."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    log.append(PurgeAuditEntry(operation="vault"))

    expected = vault / "00-Creek-Meta" / "audit" / "purge.jsonl"
    assert expected.exists()
    lines = expected.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    assert payload["operation"] == "vault"
    assert payload["prev_hash"] == "0" * 64


def test_audit_log_chain_detects_tampering(tmp_path: Path) -> None:
    """Removing the first entry breaks the chain on verify()."""
    from creek.audit import AuditChainBroken

    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    log.append(PurgeAuditEntry(operation="fragment", criteria={"id": "A"}))
    log.append(PurgeAuditEntry(operation="fragment", criteria={"id": "B"}))

    lines = log.log_path.read_text(encoding="utf-8").splitlines()
    log.log_path.write_text(lines[1] + "\n", encoding="utf-8")

    with pytest.raises(AuditChainBroken):
        log.verify()


def test_audit_log_read_missing_returns_empty(tmp_path: Path) -> None:
    """read() on a missing log returns an empty list."""
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    assert log.read() == []


def test_audit_log_migrates_legacy_purge_log(tmp_path: Path) -> None:
    """Pre-Batch-C purge-log.json is migrated to the new JSONL log."""
    vault = _make_vault(tmp_path)
    legacy_path = vault / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "operation": "fragment",
                    "target": "frag-X",
                    "count": 1,
                    "operator": "legacy",
                    "dry_run": False,
                },
            ],
        ),
        encoding="utf-8",
    )

    log = PurgeAuditLog(vault)
    entries = log.read()

    assert not legacy_path.exists()
    assert log.log_path.exists()
    assert any(
        (e.operation == "fragment" and "frag-X" in (e.target or ""))
        or e.criteria.get("target") == "frag-X"
        for e in entries
    )
    assert any(e.operation == "purge.audit.migration" for e in entries)


def test_audit_log_legacy_migration_strips_prev_hash(tmp_path: Path) -> None:
    """Legacy purge entries carrying ``prev_hash`` migrate cleanly.

    Mirrors the provenance migration regression test. Without the
    sanitiser in :meth:`PurgeAuditLog._migrate_legacy_if_needed`,
    :meth:`creek.audit.AuditLog.append` would raise ``ValueError`` and
    abort the entire migration with the legacy file still on disk.
    """
    vault = _make_vault(tmp_path)
    legacy_path = vault / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "operation": "fragment",
                    "target": "frag-with-prev",
                    "count": 1,
                    "operator": "legacy",
                    "dry_run": False,
                    "prev_hash": "deadbeef" * 8,
                },
            ],
        ),
        encoding="utf-8",
    )

    log = PurgeAuditLog(vault)
    entries = log.read()

    # Migration completed: legacy file removed, marker recorded.
    assert not legacy_path.exists()
    assert any(e.operation == "purge.audit.migration" for e in entries)
    # Verify the chain — confirms append() did not blow up midway.
    log.verify()
    # The migrated entry's prev_hash on disk is the chain hash, not the
    # legacy value the test seeded.
    raw = log.log_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(raw[0])
    assert first["prev_hash"] == "0" * 64


def test_audit_log_concurrent_appends_lose_nothing(tmp_path: Path) -> None:
    """Threaded appends produce N entries with no losses."""
    from concurrent.futures import ThreadPoolExecutor

    vault = _make_vault(tmp_path)

    def append_n(worker: int) -> None:
        log = PurgeAuditLog(vault)
        for j in range(20):
            log.append(
                PurgeAuditEntry(
                    operation="fragment",
                    criteria={"worker": worker, "j": j},
                ),
            )

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(append_n, range(4)))

    log = PurgeAuditLog(vault)
    entries = log.read()
    assert len(entries) == 80
    log.verify()


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


def test_audit_log_legacy_corrupt_json_is_skipped(tmp_path: Path) -> None:
    """A malformed legacy log is left alone but does not crash readers.

    The migration runs (the marker is written so an operator can see
    that the corrupt file was observed), and the legacy file is then
    unlinked so subsequent runs do not re-discover it. Asserting the
    cleanup explicitly closes the gap noted in the PR #193 review.
    """
    vault = _make_vault(tmp_path)
    legacy_path = vault / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{not json", encoding="utf-8")

    log = PurgeAuditLog(vault)
    entries = log.read()

    assert any(e.operation == "purge.audit.migration" for e in entries)
    assert not legacy_path.exists()


def test_audit_log_migration_with_empty_preexisting_log_does_not_double(
    tmp_path: Path,
) -> None:
    """Empty pre-created log_path + legacy file migrates exactly once.

    Reproduces the edge case flagged in review: an earlier operation
    may have opened the new JSONL log path in append mode without
    writing, leaving it on disk at zero bytes. The size guard in
    :meth:`PurgeAuditLog._migrate_legacy_if_needed` permits the
    migration to proceed in that case (size > 0 is the only short-
    circuit). A second construction afterwards must be a no-op,
    otherwise legacy entries would double on every fresh instance.
    """
    vault = _make_vault(tmp_path)
    legacy_path = vault / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "operation": "fragment",
                    "target": "frag-empty-precreate",
                    "count": 1,
                    "operator": "legacy",
                    "dry_run": False,
                },
            ],
        ),
        encoding="utf-8",
    )
    new_log_path = vault / "00-Creek-Meta" / "audit" / "purge.jsonl"
    new_log_path.parent.mkdir(parents=True, exist_ok=True)
    new_log_path.touch()
    assert new_log_path.stat().st_size == 0

    first = PurgeAuditLog(vault)
    first_entries = first.read()
    first.verify()

    # Migration ran exactly once: legacy fragment + migration marker.
    assert not legacy_path.exists()
    assert len(first_entries) == 2
    assert first_entries[0].operation == "fragment"
    assert first_entries[1].operation == "purge.audit.migration"

    # A fresh instance must not re-migrate (legacy is gone, log has size).
    second_entries = PurgeAuditLog(vault).read()
    assert len(second_entries) == 2
    assert [e.operation for e in second_entries] == [
        "fragment",
        "purge.audit.migration",
    ]


def test_audit_log_migration_oserror_preserves_legacy_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-migration OSError keeps the legacy file and logs the failure.

    Regression for PR #193 review (comment 4367360694 HIGH): without
    the explicit try/except, a failed ``AuditLog.append`` (disk full,
    permission flip) would leave the new JSONL with partial content
    while silently losing the rest of the legacy entries — and the
    next instance's size guard would treat the partial state as a
    clean prior migration. The new code re-raises so the caller sees
    the failure, leaves the legacy file intact, and emits an
    EXCEPTION-level log entry the operator can act on.
    """
    from creek.audit.log import AuditLog

    vault = _make_vault(tmp_path)
    legacy_path = vault / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "operation": "fragment",
                    "target": "frag-doomed",
                    "count": 1,
                    "operator": "legacy",
                    "dry_run": False,
                },
            ],
        ),
        encoding="utf-8",
    )

    real_append = AuditLog.append

    def failing_append(self: AuditLog, payload: dict[str, object]) -> None:
        if payload.get("operation") == "fragment":
            raise OSError("simulated disk full")
        real_append(self, payload)

    monkeypatch.setattr(AuditLog, "append", failing_append)

    with caplog.at_level("ERROR", logger="creek.purge.audit"), pytest.raises(OSError):
        PurgeAuditLog(vault).read()

    # Legacy file preserved so no entries are lost.
    assert legacy_path.exists()
    # Operator has a high-signal log line to act on.
    assert any(
        "failed mid-write" in record.message and "purge-log.json" in record.message
        for record in caplog.records
    )


def test_audit_log_orphaned_legacy_file_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Half-migrated state (both files present, JSONL non-empty) is logged.

    Regression for PR #193 review (comment 4367360694 HIGH): the size
    guard would silently skip migration when both the legacy and new
    log carried content, leaving an operator unaware that the legacy
    file was orphaned. The warning makes the inconsistency visible
    the first time it is encountered.
    """
    from creek.audit import AuditLog

    vault = _make_vault(tmp_path)
    legacy_path = vault / "00-Creek-Meta" / "Processing-Log" / "purge-log.json"
    new_path = vault / "00-Creek-Meta" / "audit" / "purge.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "operation": "fragment",
                    "target": "frag-orphan",
                    "count": 1,
                    "operator": "legacy",
                    "dry_run": False,
                },
            ],
        ),
        encoding="utf-8",
    )
    AuditLog(new_path).append(
        {
            "timestamp": "2025-02-01T00:00:00+00:00",
            "operation": "fragment",
            "criteria": {"fragment_id": "frag-already"},
        },
    )

    with caplog.at_level("WARNING", logger="creek.purge.audit"):
        PurgeAuditLog(vault).read()

    assert legacy_path.exists()
    assert any(
        "skipping migration" in record.message and "purge-log.json" in record.message
        for record in caplog.records
    )


def test_write_audit_known_operations_use_explicit_allowlist(tmp_path: Path) -> None:
    """Each known operation routes through the explicit allowlist.

    Pins the contract that ``classifications`` records zero deletions
    while file-deleting operations report ``fragments_affected`` as
    ``fragments_deleted``. Replaces the previous string-equality
    inference (``operation != "classifications"``), which would have
    silently classified any new operation as file-deleting.
    """
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault, dry_run=True)

    for operation in ("fragment", "source", "daterange", "vault"):
        engine._write_audit(
            PurgeResult(
                operation=operation,
                target=f"target-{operation}",
                fragments_affected=2,
                affected_fragment_ids=["frag-A", "frag-B"],
                dry_run=True,
            ),
        )
    engine._write_audit(
        PurgeResult(
            operation="classifications",
            target="target-classifications",
            fragments_affected=2,
            affected_fragment_ids=["frag-A", "frag-B"],
            dry_run=True,
        ),
    )

    entries = engine.audit_log.read()
    by_op = {e.operation: e for e in entries if e.operation != "purge.audit.migration"}
    assert by_op["fragment"].fragments_deleted == 2
    assert by_op["source"].fragments_deleted == 2
    assert by_op["daterange"].fragments_deleted == 2
    assert by_op["vault"].fragments_deleted == 2
    assert by_op["classifications"].fragments_deleted == 0


def test_write_audit_rejects_unknown_operation(tmp_path: Path) -> None:
    """An unknown operation name must raise rather than default-include.

    Defaulting unknown operations to ``deletes_files=True`` would silently
    over-count deletions whenever a new purge type is introduced. This
    test pins the explicit-rejection contract so any future operation
    name is forced through the allowlist.
    """
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)

    with pytest.raises(ValueError, match="Unknown purge operation"):
        engine._write_audit(
            PurgeResult(
                operation="brand-new-op",
                target="t",
                fragments_affected=99,
                dry_run=False,
            ),
        )


# ---------------------------------------------------------------------------
# GAP-001 — purge actually removes embedding cache rows
# ---------------------------------------------------------------------------


def _seed_embeddings_cache(vault: Path, fragment_ids: list[str]) -> Path:
    """Write a parquet cache with one row per supplied fragment ID.

    Uses :meth:`EmbeddingLinker.save_cache` so the layout matches the
    real one bit-for-bit; tests then assert on row presence via
    :meth:`EmbeddingLinker.load_cache`.
    """
    from datetime import UTC, datetime

    from creek.config import EmbeddingsConfig
    from creek.link.embeddings import (
        CachedEmbedding,
        EmbeddingLinker,
        embeddings_cache_path,
    )

    linker = EmbeddingLinker(config=EmbeddingsConfig())
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    entries = {
        fid: CachedEmbedding(
            fragment_id=fid,
            content_hash="hash-" + fid,
            model_name=linker.config.model,
            vector=[0.1, 0.2, 0.3],
            computed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for fid in fragment_ids
    }
    linker.save_cache(entries, cache_path)
    return cache_path


def _load_cache_ids(vault: Path) -> set[str]:
    """Return the set of fragment IDs currently in the embeddings cache."""
    from creek.config import EmbeddingsConfig
    from creek.link.embeddings import EmbeddingLinker, embeddings_cache_path

    linker = EmbeddingLinker(config=EmbeddingsConfig())
    return set(linker.load_cache(embeddings_cache_path(vault)).keys())


def test_fragment_purge_removes_embedding_row(tmp_path: Path) -> None:
    """``creek purge fragment <id>`` drops that fragment's row from the cache."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_fragment(vault, "frag-B", "Bravo")
    _seed_embeddings_cache(vault, ["frag-A", "frag-B"])

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert result.embeddings_removed == 1
    assert _load_cache_ids(vault) == {"frag-B"}


def test_source_purge_removes_embedding_rows_for_each_fragment(
    tmp_path: Path,
) -> None:
    """Every fragment from the purged source is scrubbed from the cache."""
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
    _seed_embeddings_cache(vault, ["frag-A", "frag-B", "frag-C"])

    result = PurgeEngine(vault).purge_source("claude")

    assert result.embeddings_removed == 2
    assert _load_cache_ids(vault) == {"frag-C"}


def test_source_path_purge_removes_embedding_rows(tmp_path: Path) -> None:
    """``purge_source_path`` scrubs cache rows for matched fragments (GAP-001).

    Closes the only file-deleting purge operation lacking an end-to-end
    GAP-001 smoke test. Uses ``--match substring`` so a single call
    catches more than one fragment, proving the shared
    ``_purge_cache_for`` helper is reached from this entry point too.
    """
    vault = _make_vault(tmp_path)
    _write_fragment_with_original(
        vault,
        "frag-A",
        "Alpha",
        original_file="/exports/2026-04-28.json",
    )
    _write_fragment_with_original(
        vault,
        "frag-B",
        "Bravo",
        original_file="/exports/2026-04-29.json",
    )
    _write_fragment_with_original(
        vault,
        "frag-C",
        "Charlie",
        original_file="/notes/diary.md",
    )
    _seed_embeddings_cache(vault, ["frag-A", "frag-B", "frag-C"])

    engine = PurgeEngine(vault)
    result = engine.purge_source_path("/exports/", match="substring")

    assert result.embeddings_removed == 2
    assert _load_cache_ids(vault) == {"frag-C"}
    entries = engine.audit_log.read()
    assert entries[-1].operation == "source-path"
    assert entries[-1].embeddings_removed == 2


def test_daterange_purge_removes_embedding_rows_in_range(tmp_path: Path) -> None:
    """Fragments inside the purged window lose their cache row; others stay."""
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
    _seed_embeddings_cache(vault, ["frag-old", "frag-mid", "frag-new"])

    result = PurgeEngine(vault).purge_daterange(
        date(2024, 5, 1),
        date(2024, 12, 31),
    )

    assert result.embeddings_removed == 1
    assert _load_cache_ids(vault) == {"frag-old", "frag-new"}


def test_vault_purge_deletes_embedding_cache_file(tmp_path: Path) -> None:
    """``creek purge vault`` removes the parquet cache outright (criterion 4)."""
    from creek.link.embeddings import embeddings_cache_path

    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    cache_path = _seed_embeddings_cache(vault, ["frag-A"])

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.embeddings_removed == 1
    assert not cache_path.exists()
    assert not embeddings_cache_path(vault).exists()


def test_fragment_purge_embeddings_removed_zero_when_cache_missing(
    tmp_path: Path,
) -> None:
    """Audit reports zero when the cache file has never been built (criterion 5)."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")

    engine = PurgeEngine(vault)
    result = engine.purge_fragment("frag-A")

    assert result.embeddings_removed == 0
    entries = engine.audit_log.read()
    assert entries[-1].embeddings_removed == 0


def test_fragment_purge_audit_reflects_real_cache_removal(tmp_path: Path) -> None:
    """The audit entry's ``embeddings_removed`` equals the real row delta."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _seed_embeddings_cache(vault, ["frag-A"])

    engine = PurgeEngine(vault)
    engine.purge_fragment("frag-A")

    entries = engine.audit_log.read()
    last = entries[-1]
    assert last.embeddings_removed == 1
    assert last.fragments_deleted == 1


def test_fragment_purge_audit_zero_when_id_not_in_cache(tmp_path: Path) -> None:
    """If the deleted fragment was never embedded, the audit is honest about it."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    # Cache contains other fragments but not frag-A.
    _seed_embeddings_cache(vault, ["frag-B", "frag-C"])

    engine = PurgeEngine(vault)
    engine.purge_fragment("frag-A")

    entries = engine.audit_log.read()
    assert entries[-1].embeddings_removed == 0
    # Untouched rows survive.
    assert _load_cache_ids(vault) == {"frag-B", "frag-C"}


def test_fragment_purge_dry_run_preserves_cache(tmp_path: Path) -> None:
    """Dry-run reports the would-remove count without rewriting the cache."""
    from creek.link.embeddings import embeddings_cache_path

    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    cache_path = _seed_embeddings_cache(vault, ["frag-A"])
    before_bytes = cache_path.read_bytes()

    result = PurgeEngine(vault, dry_run=True).purge_fragment("frag-A")

    assert result.embeddings_removed == 1
    assert embeddings_cache_path(vault).read_bytes() == before_bytes


def test_vault_purge_dry_run_preserves_cache_file(tmp_path: Path) -> None:
    """Dry-run vault purge counts cache rows but keeps the parquet on disk."""
    from creek.link.embeddings import embeddings_cache_path

    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _seed_embeddings_cache(vault, ["frag-A", "frag-B"])

    result = PurgeEngine(vault, dry_run=True).purge_vault(
        VAULT_PURGE_CONFIRMATION,
    )

    assert result.embeddings_removed == 2
    assert embeddings_cache_path(vault).exists()


def test_classifications_purge_does_not_touch_cache(tmp_path: Path) -> None:
    """Classifications reset is metadata-only; embeddings stay valid (still 0)."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _seed_embeddings_cache(vault, ["frag-A"])

    result = PurgeEngine(vault).purge_classifications()

    assert result.embeddings_removed == 0
    assert _load_cache_ids(vault) == {"frag-A"}


# ---------------------------------------------------------------------------
# GAP-002 — pre-destruction intent + post-destruction outcome audit pairs
# ---------------------------------------------------------------------------


def _entries_for_run(vault: Path) -> list[PurgeAuditEntry]:
    """Return non-migration purge entries from the audit log."""
    return [
        e for e in PurgeAuditLog(vault).read() if e.operation != "purge.audit.migration"
    ]


def test_purge_vault_writes_intent_before_destruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``purge_vault`` records an intent entry before unlinking anything.

    GAP-002 acceptance criterion #1: the intent entry must be on disk
    *before* the first destructive operation, so a SIGKILL mid-wipe
    still leaves a forensic trail naming what was being attempted.
    """
    import shutil as shutil_mod

    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    (vault / "01-Fragments" / "sub").mkdir(parents=True, exist_ok=True)
    (vault / "01-Fragments" / "sub" / "x.md").write_text("x", encoding="utf-8")

    captured_entries_at_first_rmtree: list[PurgeAuditEntry] = []
    real_rmtree = shutil_mod.rmtree

    def spy_rmtree(*args: object, **kwargs: object) -> None:
        # First time rmtree is invoked, snapshot the audit log so we
        # can prove the intent line was already there.
        if not captured_entries_at_first_rmtree:
            captured_entries_at_first_rmtree.extend(_entries_for_run(vault))
        real_rmtree(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("creek.purge.engine.shutil.rmtree", spy_rmtree)
    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert captured_entries_at_first_rmtree, "rmtree was never called"
    first_seen = captured_entries_at_first_rmtree[0]
    assert first_seen.phase == "intent"
    assert first_seen.operation == "vault"
    assert first_seen.operation_id


def test_purge_vault_emits_intent_then_outcome_pair(tmp_path: Path) -> None:
    """On success, two entries (intent + complete outcome) share operation_id."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.phase == "outcome"
    assert outcome.status == "complete"
    assert intent.operation_id == outcome.operation_id
    assert intent.operation == "vault" == outcome.operation


def test_purge_vault_outcome_status_partial_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the wipe loop raises, outcome records ``status="partial"``.

    GAP-002 acceptance criterion #3: the exception propagates *and* the
    log gets an outcome line saying what survived (intent already wrote
    what was being attempted; outcome closes the trail with the partial
    state).
    """
    import shutil as shutil_mod

    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_thread(vault, "thread-1", "Waves")
    # Add a subdirectory to 02-Threads so rmtree gets called there.
    (vault / "02-Threads" / "sub").mkdir(parents=True, exist_ok=True)
    (vault / "02-Threads" / "sub" / "x.md").write_text("x", encoding="utf-8")

    real_rmtree = shutil_mod.rmtree
    call_count = {"n": 0}

    def flaky_rmtree(*args: object, **kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            msg = "simulated mid-purge OSError"
            raise OSError(msg)
        real_rmtree(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("creek.purge.engine.shutil.rmtree", flaky_rmtree)

    with pytest.raises(OSError, match="simulated mid-purge"):
        PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.phase == "outcome"
    assert outcome.status == "partial"
    assert intent.operation_id == outcome.operation_id
    assert outcome.failure_reason
    assert "simulated mid-purge" in outcome.failure_reason


def test_purge_fragment_emits_intent_then_outcome_pair(tmp_path: Path) -> None:
    """Per-fragment purge follows the same intent/outcome contract."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")

    PurgeEngine(vault).purge_fragment("frag-A")

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.phase == "outcome"
    assert outcome.status == "complete"
    assert intent.operation_id == outcome.operation_id
    # Intent carries the scope; outcome carries the counts.
    assert intent.criteria == outcome.criteria == {"fragment_id": "frag-A"}
    assert outcome.fragments_deleted == 1


def test_purge_source_emits_intent_then_outcome_pair(tmp_path: Path) -> None:
    """``purge_source`` writes intent before, outcome after."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    _write_fragment(vault, "frag-B", "Bravo", platform="claude")

    PurgeEngine(vault).purge_source("claude")

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.status == "complete"
    assert intent.operation_id == outcome.operation_id
    assert outcome.fragments_deleted == 2


def test_purge_source_path_emits_intent_then_outcome_pair(tmp_path: Path) -> None:
    """``purge_source_path`` writes intent before, outcome after."""
    vault = _make_vault(tmp_path)
    _write_fragment_with_original(
        vault,
        "frag-A",
        "Alpha",
        original_file="/exports/2026-04-28.json",
    )

    PurgeEngine(vault).purge_source_path("/exports/", match="substring")

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.phase == "outcome"
    assert outcome.status == "complete"
    assert intent.operation_id == outcome.operation_id


def test_purge_daterange_emits_intent_then_outcome_pair(tmp_path: Path) -> None:
    """``purge_daterange`` writes intent before, outcome after."""
    vault = _make_vault(tmp_path)
    _write_fragment(
        vault,
        "frag-mid",
        "Mid",
        created=datetime(2024, 6, 1, tzinfo=UTC),
    )

    PurgeEngine(vault).purge_daterange(date(2024, 1, 1), date(2024, 12, 31))

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.status == "complete"
    assert intent.operation_id == outcome.operation_id


def test_purge_classifications_emits_intent_then_outcome_pair(
    tmp_path: Path,
) -> None:
    """Even metadata-only ops emit the pair; recovery still depends on intent."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")

    PurgeEngine(vault).purge_classifications()

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.phase == "outcome"
    assert outcome.status == "complete"
    assert intent.operation_id == outcome.operation_id


def test_audit_chain_intact_across_intent_outcome_pairs(tmp_path: Path) -> None:
    """Two pairs in a row leave a verifiable four-entry hash chain."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_fragment(vault, "frag-B", "Bravo")

    engine = PurgeEngine(vault)
    engine.purge_fragment("frag-A")
    engine.purge_fragment("frag-B")

    PurgeAuditLog(vault).verify()  # raises if chain is broken
    entries = _entries_for_run(vault)
    assert [e.phase for e in entries] == ["intent", "outcome", "intent", "outcome"]
    # Each pair's operation_ids match; the two pairs differ.
    assert entries[0].operation_id == entries[1].operation_id
    assert entries[2].operation_id == entries[3].operation_id
    assert entries[0].operation_id != entries[2].operation_id


def test_purge_fragment_intent_logged_even_when_target_missing(
    tmp_path: Path,
) -> None:
    """A no-op purge (target not found) still emits the intent/outcome pair.

    Forensic value: an operator who fat-fingers a fragment ID should
    still see *that* they tried to purge it. The outcome reports zero
    deletions but the intent records the attempt.
    """
    vault = _make_vault(tmp_path)

    PurgeEngine(vault).purge_fragment("frag-missing")

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.phase == "outcome"
    assert outcome.status == "complete"
    assert outcome.fragments_deleted == 0


def test_legacy_audit_entries_default_to_outcome_phase(tmp_path: Path) -> None:
    """Pre-GAP-002 entries (no phase/operation_id) read back as outcomes.

    Backward compatibility: an audit log that predates the schema change
    must keep reading cleanly; missing fields take sensible defaults
    (``phase="outcome"`` because that's what those entries always were
    de-facto, even though the field didn't exist).
    """
    vault = _make_vault(tmp_path)
    log = PurgeAuditLog(vault)
    # Append an entry the pre-GAP-002 way — directly through the
    # underlying AuditLog so it lands without phase/operation_id.
    from creek.audit import AuditLog

    AuditLog(log.log_path).append(
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "operation": "fragment",
            "criteria": {"fragment_id": "frag-old"},
            "affected_fragments": ["frag-old"],
            "fragments_deleted": 1,
        },
    )

    entries = PurgeAuditLog(vault).read()
    assert len(entries) == 1
    assert entries[0].phase == "outcome"
    assert entries[0].operation_id == ""
    assert entries[0].status is None
