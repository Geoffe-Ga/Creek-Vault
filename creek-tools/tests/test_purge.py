"""Tests for the creek purge module.

Covers both the :class:`creek.purge.PurgeEngine` directly (fragment,
source, classifications, daterange, vault) and the ``creek purge``
CLI subcommands. Uses fixture vaults seeded with small fragment
corpora per test.
"""

from __future__ import annotations

import json
import logging
import re
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


def _write_intimate_note_with_stub(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    stub_relpath: str | None = None,
    write_stub: bool = True,
) -> tuple[Path, Path]:
    """Write an intimate-tier vault note plus its stub body (GAP-012).

    Mirrors what ``creek save`` does for an ``intimate`` answer: a
    title-only note carrying a ``saved_from.intimate_body_pointer`` and
    a separate full-body stub under
    ``10-Liminal/Compost/intimate-stubs/``.

    Args:
        vault: Vault root.
        frag_id: Fragment ID for the note's frontmatter.
        title: Note title (also used for the stub filename).
        stub_relpath: Pointer value to record in the note. Defaults to
            ``10-Liminal/Compost/intimate-stubs/<title>.md``.
        write_stub: When ``False``, record the pointer but do not create
            the stub file on disk (simulates an already-deleted stub).

    Returns:
        Tuple of ``(note_path, stub_path)``.
    """
    pointer = stub_relpath or f"10-Liminal/Compost/intimate-stubs/{title}.md"
    stub_path = vault / pointer
    if write_stub:
        stub_path.parent.mkdir(parents=True, exist_ok=True)
        stub_post = frontmatter.Post(
            content="The full intimate body.\n",
            type="intimate-stub",
            title=title,
            privacy_tier="intimate",
        )
        stub_path.write_text(frontmatter.dumps(stub_post), encoding="utf-8")

    note_path = vault / "01-Fragments" / "Conversations" / f"{title}.md"
    note_post = frontmatter.Post(
        content="(intimate body withheld)\n",
        id=frag_id,
        title=title,
        type="fragment",
        source={"platform": "claude", "original_file": f"{frag_id}.json"},
        threads=[],
        eddies=[],
        privacy_tier="intimate",
        saved_from={
            "source_kind": "answer",
            "intimate_body_pointer": pointer,
        },
    )
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(frontmatter.dumps(note_post), encoding="utf-8")
    return note_path, stub_path


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
# Undecodable-body fixtures (#910)
# ---------------------------------------------------------------------------

_UNDECODABLE_BYTES = b"\xff\xfe"
"""Raw bytes that are not valid UTF-8 anywhere in a byte stream (#910).

Injected into fragment *bodies* only. A file carrying these bytes makes
``frontmatter.load`` raise ``UnicodeDecodeError`` at read time — before
the (perfectly well-formed, byte-clean ASCII) YAML frontmatter block is
ever parsed.
"""

_UNDECODABLE_SECRET = "synthetic-undecodable-secret-910"
"""Synthetic marker standing in for private body content — never real data."""


def _write_fragment_with_undecodable_body(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    platform: str = "claude",
    subfolder: str = "Conversations",
    created: datetime | None = None,
    threads: list[str] | None = None,
    eddies: list[str] | None = None,
    secret: str = _UNDECODABLE_SECRET,
) -> tuple[Path, bytes]:
    """Write a house-schema fragment whose *body* is not valid UTF-8 (#910).

    Builds the fragment through :func:`_write_fragment` so the
    frontmatter matches the house schema exactly, then re-writes the
    file with :data:`_UNDECODABLE_BYTES` appended to the body. Because
    the injection happens strictly after the closing ``---`` delimiter,
    the YAML frontmatter block stays byte-clean ASCII — that is
    precisely the class under test: well-formed metadata, undecodable
    body.

    Args:
        vault: Vault root.
        frag_id: Fragment ID.
        title: Fragment title (also the filename stem).
        platform: Source platform recorded in ``source.platform``.
        subfolder: Subfolder under ``01-Fragments/``.
        created: Optional creation datetime (for date-range matching).
        threads: Optional list of thread IDs the fragment belongs to.
        eddies: Optional list of eddy IDs.
        secret: Marker text embedded in the body so tests can prove the
            private content really left the vault.

    Returns:
        Tuple of ``(path, exact_bytes_written)`` so callers can assert
        byte-for-byte equality after a purge that must not touch it.
    """
    path = _write_fragment(
        vault,
        frag_id,
        title,
        platform=platform,
        subfolder=subfolder,
        created=created,
        threads=threads,
        eddies=eddies,
        body=f"Body carrying {secret} here.",
    )
    raw = path.read_bytes() + _UNDECODABLE_BYTES + b"\n"
    path.write_bytes(raw)
    return path, raw


def _vault_files_containing_bytes(vault: Path, needle: bytes) -> list[Path]:
    """Return every vault markdown file whose *bytes* still carry *needle*.

    The byte-level sibling of :func:`_vault_files_containing_secret`,
    which cannot be reused here: it decodes with
    ``read_text(encoding="utf-8")`` and would itself raise
    ``UnicodeDecodeError`` on the very fixtures under test (#910).

    Args:
        vault: Vault root to walk.
        needle: Byte sequence that must survive nowhere under the vault.

    Returns:
        Offending paths — empty when the purge honored the RTBF request.
    """
    return [
        md_file
        for md_file in sorted(vault.rglob("*.md"))
        if needle in md_file.read_bytes()
    ]


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


@pytest.mark.parametrize(
    "link",
    [
        "[[Alpha#Heading]]",
        "[[Alpha#Heading|alias]]",
        "[[Alpha#^blk01]]",
        "[[Alpha#]]",
    ],
)
def test_fragment_purge_removes_heading_wikilinks(tmp_path: Path, link: str) -> None:
    """Heading/block-suffixed wiki-links to the purged title are scrubbed (#833)."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    linker = _write_fragment(vault, "frag-B", "Beta", body=f"See {link} today.")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.wikilinks_removed == 1
    post = frontmatter.load(str(linker))
    assert link not in post.content
    assert "Alpha" not in post.content


def test_fragment_purge_removes_all_wikilink_variants(tmp_path: Path) -> None:
    """A mixed body loses every Alpha link variant while [[Beta]] survives (#833)."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    linker = _write_fragment(
        vault,
        "frag-B",
        "Beta",
        body=(
            "See [[Alpha]] and [[Alpha|a]] and [[Alpha#H]] "
            "and [[Alpha#H|a]] and [[Beta]]."
        ),
    )
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.wikilinks_removed == 4
    post = frontmatter.load(str(linker))
    assert "Alpha" not in post.content
    assert "[[Beta]]" in post.content


def test_fragment_purge_leaves_prefix_titled_wikilinks(tmp_path: Path) -> None:
    """Links to a longer title sharing the purged prefix are untouched (#833)."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    linker = _write_fragment(
        vault,
        "frag-B",
        "Beta",
        body="See [[AlphaBeta]] and [[AlphaBeta#H]].",
    )
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.wikilinks_removed == 0
    post = frontmatter.load(str(linker))
    assert "[[AlphaBeta]]" in post.content
    assert "[[AlphaBeta#H]]" in post.content


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
# Engine tests — intimate stub sweep (GAP-012)
# ---------------------------------------------------------------------------


def test_fragment_purge_removes_intimate_stub(tmp_path: Path) -> None:
    """Purging an intimate note also deletes its pointed-to stub (GAP-012)."""
    vault = _make_vault(tmp_path)
    note, stub = _write_intimate_note_with_stub(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 1
    assert not note.exists()
    assert not stub.exists()


def test_fragment_purge_intimate_stub_dry_run_preserves(tmp_path: Path) -> None:
    """Dry-run counts the stub it would delete but leaves it on disk."""
    vault = _make_vault(tmp_path)
    note, stub = _write_intimate_note_with_stub(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault, dry_run=True)

    result = engine.purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 1
    assert note.exists()
    assert stub.exists()


def test_fragment_purge_missing_stub_does_not_raise(tmp_path: Path) -> None:
    """A pointer at an already-deleted stub is tolerated, not an error."""
    vault = _make_vault(tmp_path)
    note, stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        write_stub=False,
    )
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0
    assert not note.exists()
    assert not stub.exists()


def test_fragment_purge_no_pointer_skips_stub_sweep(tmp_path: Path) -> None:
    """A plain note (no pointer) reports zero intimate stubs removed."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0


def test_source_purge_removes_intimate_stub(tmp_path: Path) -> None:
    """A scoped source purge sweeps the intimate stub too (GAP-012)."""
    vault = _make_vault(tmp_path)
    _note, stub = _write_intimate_note_with_stub(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.intimate_stubs_removed == 1
    assert not stub.exists()


def test_fragment_purge_intimate_stub_audit_records_count(
    tmp_path: Path,
) -> None:
    """The outcome audit entry reports ``intimate_stubs_removed`` (GAP-012)."""
    vault = _make_vault(tmp_path)
    _write_intimate_note_with_stub(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    engine.purge_fragment("frag-A")

    entries = PurgeAuditLog(vault).read()
    intent, outcome = entries
    assert intent.intimate_stubs_removed == 0
    assert outcome.intimate_stubs_removed == 1


def test_fragment_purge_intimate_stub_outside_vault_skipped(
    tmp_path: Path,
) -> None:
    """A pointer escaping the vault root is ignored, not followed (security)."""
    vault = _make_vault(tmp_path)
    outsider = tmp_path / "outside-secret.md"
    outsider.write_text("not a stub\n", encoding="utf-8")
    note, _stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath="../outside-secret.md",
        write_stub=False,
    )
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0
    assert not note.exists()
    assert outsider.exists()


# ---------------------------------------------------------------------------
# Engine tests — journal staged-entry sweep (#845)
# ---------------------------------------------------------------------------

_JOURNAL_STAGING_DIR = "00-Creek-Meta/adepthood/journal"
"""Where ``journal_ingest_tool`` stages full entry bodies (the ledger key root)."""

_JOURNAL_SECRET = "synthetic-intimate-secret-845"
"""Synthetic marker standing in for intimate body content — never real data."""


def _write_journal_fragment_with_staged(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    origin_key: str | None = None,
    write_staged: bool = True,
) -> tuple[Path, Path]:
    """Write a journal fragment plus the staged plaintext entry it points at.

    Mirrors what ``journal_ingest_tool`` leaves on disk (#845): the
    fragment lands under ``01-Fragments/Journal/`` with frontmatter
    ``source.origin_key`` naming the staged full-body markdown file
    under ``00-Creek-Meta/adepthood/journal/``. The staged body carries
    :data:`_JOURNAL_SECRET` so tests can assert the plaintext is really
    gone from the vault after a purge.

    Args:
        vault: Vault root.
        frag_id: Fragment ID for the fragment's frontmatter.
        title: Fragment title (also the staged file's stem by default).
        origin_key: Vault-relative pointer recorded in the fragment's
            ``source.origin_key``. Defaults to the canonical staging
            path for *title*.
        write_staged: When ``False``, record the pointer but do not
            create the staged file on disk.

    Returns:
        Tuple of ``(fragment_path, staged_path)``.
    """
    key = origin_key or f"{_JOURNAL_STAGING_DIR}/{title}.md"
    staged_path = vault / key
    if write_staged:
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_post = frontmatter.Post(
            content=f"The full journal body. {_JOURNAL_SECRET}\n",
            privacy_tier="intimate",
            source_id=title,
        )
        staged_path.write_text(frontmatter.dumps(staged_post), encoding="utf-8")

    frag_path = vault / "01-Fragments" / "Journal" / f"{title}.md"
    frag_post = frontmatter.Post(
        content="A journal fragment summary.\n",
        id=frag_id,
        title=title,
        type="fragment",
        source={
            "platform": "journal",
            "origin_key": key,
            "original_file": str(staged_path),
        },
        threads=[],
        eddies=[],
        privacy_tier="intimate",
    )
    frag_path.parent.mkdir(parents=True, exist_ok=True)
    frag_path.write_text(frontmatter.dumps(frag_post), encoding="utf-8")
    return frag_path, staged_path


def _vault_files_containing_secret(vault: Path) -> list[Path]:
    """Return every vault markdown file whose text still carries the secret.

    Walks the whole vault (not just ``01-Fragments``) because the RTBF
    contract is vault-wide: after a purge, :data:`_JOURNAL_SECRET` must
    appear in **no** file anywhere under the vault root.

    Args:
        vault: Vault root to walk.

    Returns:
        Offending paths — empty when the purge honored the RTBF request.
    """
    return [
        md_file
        for md_file in sorted(vault.rglob("*.md"))
        if _JOURNAL_SECRET in md_file.read_text(encoding="utf-8")
    ]


def test_fragment_purge_removes_journal_staged_entry(tmp_path: Path) -> None:
    """Purging a journal fragment deletes its staged plaintext body (#845)."""
    vault = _make_vault(tmp_path)
    frag, staged = _write_journal_fragment_with_staged(vault, "frag-J", "entry-one")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-J")

    assert result.journal_staged_removed == 1
    assert not frag.exists()
    assert not staged.exists()
    # The RTBF core: the intimate body survives NOWHERE under the vault.
    assert _vault_files_containing_secret(vault) == []


def test_source_purge_removes_journal_staged_entry(tmp_path: Path) -> None:
    """``purge_source("journal")`` sweeps the staged entry too (#845).

    Pins the ``_purge_single`` path — source-scoped purges must follow
    ``source.origin_key`` exactly like the single-fragment path does.
    """
    vault = _make_vault(tmp_path)
    _frag, staged = _write_journal_fragment_with_staged(vault, "frag-J", "entry-one")
    engine = PurgeEngine(vault)

    result = engine.purge_source("journal")

    assert result.fragments_affected == 1
    assert result.journal_staged_removed == 1
    assert not staged.exists()
    assert _vault_files_containing_secret(vault) == []


def test_vault_purge_removes_journal_staged_entries(tmp_path: Path) -> None:
    """``purge_vault`` wipes the staging dir; counters stay honest (#845).

    Both staged bodies must be gone and counted on
    ``journal_staged_removed`` — but staged files are *not* fragments,
    so ``fragments_affected`` must match a twin vault that never had
    them.
    """
    vault = _make_vault(tmp_path)
    _frag_a, staged_a = _write_journal_fragment_with_staged(
        vault,
        "frag-J1",
        "entry-one",
    )
    _frag_b, staged_b = _write_journal_fragment_with_staged(
        vault,
        "frag-J2",
        "entry-two",
    )
    twin = _make_vault(tmp_path / "twin")
    for twin_id, twin_title in [("frag-J1", "entry-one"), ("frag-J2", "entry-two")]:
        _write_journal_fragment_with_staged(
            twin,
            twin_id,
            twin_title,
            write_staged=False,
        )

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)
    twin_result = PurgeEngine(twin).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.journal_staged_removed == 2
    assert not staged_a.exists()
    assert not staged_b.exists()
    assert _vault_files_containing_secret(vault) == []
    # Staged files never inflate the fragment count.
    assert result.fragments_affected == twin_result.fragments_affected


def test_journal_staged_dry_run_preserves(tmp_path: Path) -> None:
    """Dry-run counts the staged entry it would delete but leaves it on disk."""
    vault = _make_vault(tmp_path)
    frag, staged = _write_journal_fragment_with_staged(vault, "frag-J", "entry-one")

    frag_result = PurgeEngine(vault, dry_run=True).purge_fragment("frag-J")

    assert frag_result.journal_staged_removed == 1
    assert frag.exists()
    assert staged.exists()

    vault_result = PurgeEngine(vault, dry_run=True).purge_vault(
        VAULT_PURGE_CONFIRMATION,
    )

    assert vault_result.journal_staged_removed == 1
    assert staged.exists()


def test_journal_staged_pointer_outside_vault_skipped(tmp_path: Path) -> None:
    """Escaping or out-of-scope ``origin_key`` pointers are never followed.

    Containment is two guards (#845): the pointer must resolve inside
    the vault root AND inside ``00-Creek-Meta/adepthood/journal/``. A
    pointer escaping the vault and a pointer at a vault file outside
    the staging dir must both survive, each with a zero counter.
    """
    vault = _make_vault(tmp_path)
    # Variant 1: the pointer escapes the vault root entirely.
    frag_out, outside = _write_journal_fragment_with_staged(
        vault,
        "frag-out",
        "escapee",
        origin_key="../outside-secret.md",
    )

    result_out = PurgeEngine(vault).purge_fragment("frag-out")

    assert result_out.journal_staged_removed == 0
    assert not frag_out.exists()
    assert outside.exists()
    assert _JOURNAL_SECRET in outside.read_text(encoding="utf-8")

    # Variant 2: the pointer stays inside the vault but outside the
    # staging dir (staging-dir scope guard).
    frag_in, decoy = _write_journal_fragment_with_staged(
        vault,
        "frag-in",
        "decoyed",
        origin_key="01-Fragments/other.md",
    )

    result_in = PurgeEngine(vault).purge_fragment("frag-in")

    assert result_in.journal_staged_removed == 0
    assert not frag_in.exists()
    assert decoy.exists()
    assert _JOURNAL_SECRET in decoy.read_text(encoding="utf-8")


def test_journal_purge_audit_records_count(tmp_path: Path) -> None:
    """The outcome audit entry reports ``journal_staged_removed`` (#845)."""
    vault = _make_vault(tmp_path)
    _write_journal_fragment_with_staged(vault, "frag-J", "entry-one")
    engine = PurgeEngine(vault)

    engine.purge_fragment("frag-J")

    entries = PurgeAuditLog(vault).read()
    intent, outcome = entries
    assert intent.journal_staged_removed == 0
    assert outcome.journal_staged_removed == 1


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


# ---------------------------------------------------------------------------
# GAP-004 — purge scrubs YAML provenance + bare-ID body mentions vault-wide
# ---------------------------------------------------------------------------


def _write_derived_note(
    vault: Path,
    *,
    subfolder: str,
    name: str,
    body: str,
    metadata: dict[str, object] | None = None,
) -> Path:
    """Write an arbitrary derived note (praxis / draft / etc.) under *subfolder*."""
    target_dir = vault / subfolder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.md"
    post = frontmatter.Post(content=body, **(metadata or {}))
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    "subfolder",
    [
        "04-Praxis",
        "05-Wavelength",
        "06-Frequencies",
        "07-Voice/Drafts",
        "08-Decisions",
        "09-Reference",
        "10-Liminal",
        "00-Creek-Meta/Skills",
    ],
)
def test_fragment_purge_scrubs_source_fragments_in_every_relevant_folder(
    tmp_path: Path,
    subfolder: str,
) -> None:
    """``source_fragments`` YAML list entries are scrubbed vault-wide (GAP-004).

    Verifies the new contract across every folder named in the GAP-004
    issue — derived content in 04-Praxis, weekly wavelength reports,
    Voice drafts, decisions/reference/liminal note trees, and the
    deployed skill tree.

    The contract is grep-based: the operator runs ``grep -r <id>`` and
    expects no match. We assert on the raw file text rather than the
    YAML-parsed structure because the literal placeholder ``[purged]``
    is itself bracket-flavoured and YAML re-parses it as a one-element
    nested list. That detail is downstream; the file content is the
    user-facing forensic surface.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    note = _write_derived_note(
        vault,
        subfolder=subfolder,
        name="derived",
        body="A derived note referencing the soon-deleted fragment.",
        metadata={"source_fragments": ["frag-A", "frag-B"]},
    )

    PurgeEngine(vault).purge_fragment("frag-A")

    text = note.read_text(encoding="utf-8")
    assert "frag-A" not in text
    assert "[purged]" in text
    assert "frag-B" in text


def test_fragment_purge_scrubs_bare_id_mentions_in_body(tmp_path: Path) -> None:
    """Body-text mentions of the fragment ID are replaced with ``[purged]``."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    draft = _write_derived_note(
        vault,
        subfolder="07-Voice/Drafts",
        name="d1",
        body="A draft built from frag-A. See also frag-A for details.",
        metadata={"type": "draft"},
    )

    PurgeEngine(vault).purge_fragment("frag-A")

    text = draft.read_text(encoding="utf-8")
    assert "frag-A" not in text
    assert text.count("[purged]") == 2  # two body mentions


def test_fragment_purge_does_not_match_id_as_substring(tmp_path: Path) -> None:
    """Word-boundary regex avoids over-matching ``frag-A`` inside ``frag-ABC``."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    sibling = _write_derived_note(
        vault,
        subfolder="07-Voice/Drafts",
        name="d2",
        body="The fragments frag-A and frag-ABC are different.",
        metadata={"source_fragments": ["frag-A", "frag-ABC"]},
    )

    PurgeEngine(vault).purge_fragment("frag-A")

    text = sibling.read_text(encoding="utf-8")
    assert "frag-ABC" in text  # the longer ID survives
    # Word-boundary regex matches frag-A but not the prefix of frag-ABC.
    # Two "frag-A" occurrences (YAML list + body) both get scrubbed.
    assert "[purged]" in text
    # Plain "frag-A" with a trailing word boundary should not appear.
    assert not re.search(r"\bfrag-A\b", text)


def test_fragment_purge_does_not_match_id_as_hyphen_suffix(tmp_path: Path) -> None:
    """ID scrub treats ``-`` as ID continuation so suffixed IDs survive.

    A bare ``\\b`` word boundary would match ``frag-01`` inside
    ``frag-01-extended`` (``1`` → ``-`` is a word/non-word boundary).
    The tightened lookaround treats a trailing hyphen as part of a
    longer ID and leaves the suffixed sibling untouched.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-01", "Alpha")
    sibling = _write_derived_note(
        vault,
        subfolder="07-Voice/Drafts",
        name="d3",
        body="The fragments frag-01 and frag-01-extended are different.",
        metadata={"source_fragments": ["frag-01", "frag-01-extended"]},
    )

    PurgeEngine(vault).purge_fragment("frag-01")

    text = sibling.read_text(encoding="utf-8")
    assert "frag-01-extended" in text  # hyphen-suffixed sibling survives
    assert "[purged]" in text  # the exact ID is still scrubbed
    # No standalone frag-01 (not followed by a hyphen-continuation) remains.
    assert not re.search(r"(?<![\w-])frag-01(?![\w-])", text)


def test_fragment_purge_leaves_audit_log_untouched(tmp_path: Path) -> None:
    """The compliance audit log (00-Creek-Meta/audit/purge.jsonl) is not scrubbed.

    JSONL files aren't ``.md`` so the recursive walk excludes them — but
    pin the invariant explicitly so a future refactor that broadens the
    scrubber surface cannot silently mutate the compliance record.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")

    PurgeEngine(vault).purge_fragment("frag-A")

    audit_path = vault / "00-Creek-Meta" / "audit" / "purge.jsonl"
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "frag-A" in audit_text  # affected_fragments retains the real ID


def test_fragment_purge_records_provenance_scrubs_in_result(tmp_path: Path) -> None:
    """``PurgeResult.provenance_scrubbed`` tallies the scrubbed mentions."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_derived_note(
        vault,
        subfolder="04-Praxis",
        name="p1",
        body="See frag-A.",
        metadata={"source_fragments": ["frag-A"]},
    )

    result = PurgeEngine(vault).purge_fragment("frag-A")

    # YAML list entry + one body mention = 2 substitutions in the praxis
    # file. The deleted fragment file itself is excluded from the scrub.
    assert result.provenance_scrubbed >= 2


def test_fragment_purge_audit_outcome_records_provenance_scrubs(
    tmp_path: Path,
) -> None:
    """The GAP-002 outcome line carries the provenance-scrub count."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_derived_note(
        vault,
        subfolder="07-Voice/Drafts",
        name="d1",
        body="From frag-A.",
        metadata={"source_fragments": ["frag-A"]},
    )

    PurgeEngine(vault).purge_fragment("frag-A")

    entries = [
        e for e in PurgeAuditLog(vault).read() if e.operation != "purge.audit.migration"
    ]
    outcome = entries[-1]
    assert outcome.phase == "outcome"
    assert outcome.provenance_scrubbed >= 1


def test_fragment_purge_dry_run_does_not_scrub(tmp_path: Path) -> None:
    """Dry-run reports the would-be count without writing to derived files."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    draft = _write_derived_note(
        vault,
        subfolder="07-Voice/Drafts",
        name="d1",
        body="From frag-A.",
        metadata={"source_fragments": ["frag-A"]},
    )
    before = draft.read_text(encoding="utf-8")

    result = PurgeEngine(vault, dry_run=True).purge_fragment("frag-A")

    assert result.provenance_scrubbed >= 1
    assert draft.read_text(encoding="utf-8") == before


def test_source_purge_scrubs_provenance_for_each_fragment(tmp_path: Path) -> None:
    """``purge_source`` scrubs every purged fragment's references."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    _write_fragment(vault, "frag-B", "Bravo", platform="claude")
    derived = _write_derived_note(
        vault,
        subfolder="04-Praxis",
        name="p1",
        body="Built from frag-A and frag-B.",
        metadata={"source_fragments": ["frag-A", "frag-B"]},
    )

    PurgeEngine(vault).purge_source("claude")

    text = derived.read_text(encoding="utf-8")
    assert "frag-A" not in text
    assert "frag-B" not in text
    # 2 YAML list entries + 2 body mentions. Use >= rather than == because
    # the exact count depends on whether frontmatter.dumps serialises the
    # list in flow ([a, b]) or block style — robust today, brittle if the
    # serialiser ever switches.
    assert text.count("[purged]") >= 4


def test_purged_marker_reparses_as_nested_list_in_yaml(tmp_path: Path) -> None:
    """Pin the documented YAML-flow-list side-effect of ``[purged]``.

    ``[purged]`` is bracket-flavoured, so a flow-sequence entry like
    ``source_fragments: [frag-A, frag-B]`` becomes
    ``[[purged], frag-B]`` after the text-level substitution. A YAML
    parser then re-reads the scrubbed entry as a one-element nested
    list (``["purged"]``) rather than a flat string. This test locks
    the trade-off in place: a future "quietly switch the marker" PR has
    to update both this assertion and the :data:`PURGED_MARKER`
    docstring together.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    note = _write_derived_note(
        vault,
        subfolder="04-Praxis",
        name="p1",
        body="A praxis note.",
        metadata={"source_fragments": ["frag-A", "frag-B"]},
    )

    PurgeEngine(vault).purge_fragment("frag-A")

    reloaded = frontmatter.load(str(note))
    source_fragments = reloaded.get("source_fragments")
    assert source_fragments == [["purged"], "frag-B"]
    # The surviving sibling stays a flat string; only the scrubbed entry nests.
    assert isinstance(source_fragments, list)
    assert source_fragments[0] == ["purged"]
    assert source_fragments[1] == "frag-B"


def test_purge_single_walks_vault_once_per_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiki-link + provenance scrubs share a single vault walk per fragment.

    ``_purge_single`` used to invoke ``_list_vault_md_files`` twice (once
    for wiki-links, once for provenance). The combined single-walk scrub
    must call it at most once per purged fragment.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    _write_fragment(vault, "frag-B", "Bravo", platform="claude")
    _write_derived_note(
        vault,
        subfolder="04-Praxis",
        name="p1",
        body="Built from frag-A and frag-B.",
        metadata={"source_fragments": ["frag-A", "frag-B"]},
    )

    engine = PurgeEngine(vault)
    # The test deliberately wraps the private walk helper to count calls.
    original = engine._list_vault_md_files
    calls = 0

    def _counting_walk() -> list[Path]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(engine, "_list_vault_md_files", _counting_walk)

    engine.purge_source("claude")

    # Two fragments purged → at most one walk each.
    assert calls <= 2


# ---------------------------------------------------------------------------
# Engine tests — fragments whose body is not valid UTF-8 (#910)
# ---------------------------------------------------------------------------


def test_source_purge_deletes_fragment_with_undecodable_body(
    tmp_path: Path,
) -> None:
    """A matching fragment with an undecodable body is really deleted (#910).

    The headline RTBF failure: ``frontmatter.load`` raises
    ``UnicodeDecodeError`` on the body even though the frontmatter is
    well-formed ASCII, the engine treats the unreadable load as "skip
    this file", and the matching fragment survives with its private
    body on disk.
    """
    vault = _make_vault(tmp_path)
    frag, _raw = _write_fragment_with_undecodable_body(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.fragments_affected == 1
    assert not frag.exists()
    # The RTBF core: the private body survives NOWHERE under the vault.
    assert _vault_files_containing_bytes(vault, _UNDECODABLE_SECRET.encode()) == []


def test_source_purge_audit_records_the_undecodable_deletion(
    tmp_path: Path,
) -> None:
    """The outcome entry counts the undecodable fragment it deleted (#910).

    The "reports success" half of the bug: today the outcome line says
    ``status="complete"`` with ``fragments_deleted=0`` and an empty
    ``affected_fragments``, so the compliance record claims a clean
    purge while the fragment is still on disk.
    """
    vault = _make_vault(tmp_path)
    _write_fragment_with_undecodable_body(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    engine.purge_source("claude")

    entries = PurgeAuditLog(vault).read()
    assert len(entries) == 2
    intent, outcome = entries
    assert intent.phase == "intent"
    assert outcome.phase == "outcome"
    assert outcome.status == "complete"
    assert outcome.operation == "source"
    assert outcome.criteria == {"source_type": "claude"}
    assert outcome.affected_fragments == ["frag-A"]
    assert outcome.fragments_deleted == 1


def test_fragment_purge_finds_fragment_with_undecodable_body(
    tmp_path: Path,
) -> None:
    """``purge_fragment`` locates a fragment whose body is undecodable (#910).

    Pins ``_find_fragment_by_id``: an unreadable load there makes the
    targeted single-fragment purge report "not found".
    """
    vault = _make_vault(tmp_path)
    frag, _raw = _write_fragment_with_undecodable_body(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.fragments_affected == 1
    assert not frag.exists()
    assert _vault_files_containing_bytes(vault, _UNDECODABLE_SECRET.encode()) == []


def test_source_path_purge_deletes_fragment_with_undecodable_body(
    tmp_path: Path,
) -> None:
    """``purge_source_path`` deletes an undecodable-bodied match (#910).

    ``_write_fragment`` records ``source.original_file`` as
    ``"<frag_id>.json"``, so ``"frag-A.json"`` is the exact path the
    seeded fragment carries. Pins ``_fragments_from_source_path``.
    """
    vault = _make_vault(tmp_path)
    frag, _raw = _write_fragment_with_undecodable_body(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_source_path("frag-A.json", match="exact")

    assert result.fragments_affected == 1
    assert not frag.exists()
    assert _vault_files_containing_bytes(vault, _UNDECODABLE_SECRET.encode()) == []


def test_daterange_purge_deletes_fragment_with_undecodable_body(
    tmp_path: Path,
) -> None:
    """A date-range purge deletes an in-range undecodable fragment (#910).

    Pins ``purge_daterange``'s inline frontmatter load: skipping the
    file means its ``created`` date is never even compared.
    """
    vault = _make_vault(tmp_path)
    frag, _raw = _write_fragment_with_undecodable_body(
        vault,
        "frag-A",
        "Alpha",
        created=datetime(2024, 6, 1, tzinfo=UTC),
    )
    engine = PurgeEngine(vault)

    result = engine.purge_daterange(date(2024, 5, 1), date(2024, 12, 31))

    assert result.fragments_affected == 1
    assert not frag.exists()
    assert _vault_files_containing_bytes(vault, _UNDECODABLE_SECRET.encode()) == []


def test_source_purge_leaves_undecodable_nonmatching_fragment_intact(
    tmp_path: Path,
) -> None:
    """Failing closed must not over-delete a non-matching fragment (#910).

    The undecodable fragment belongs to ``discord``, so a ``claude``
    purge must leave it byte-for-byte untouched — including through the
    vault-wide wiki-link/provenance scrub walk that visits it while
    deleting the matching fragment.
    """
    vault = _make_vault(tmp_path)
    keeper = _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    other, other_bytes = _write_fragment_with_undecodable_body(
        vault,
        "frag-B",
        "Bravo",
        platform="discord",
        subfolder="Messages",
    )
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.fragments_affected == 1
    assert not keeper.exists()
    assert other.exists()
    assert other.read_bytes() == other_bytes


def test_source_purge_skips_file_with_no_parseable_frontmatter(
    tmp_path: Path,
) -> None:
    """Unreadable garbage under ``01-Fragments/`` is skipped, not deleted (#910).

    "Fail closed" means *match* the fragments whose metadata is sound
    even when the body is undecodable — it must never mean "delete
    everything the parser cannot read". A delimiter-less file does not
    raise: ``python-frontmatter`` parses it as empty metadata, so it
    exposes none of the criteria surfaces (``source.platform`` here)
    and matches nothing. Either way it must survive intact.
    """
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    garbage = vault / "01-Fragments" / "Conversations" / "garbage.md"
    garbage_bytes = b"\xff\xfe not yaml at all"
    garbage.write_bytes(garbage_bytes)
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.fragments_affected == 1
    assert not frag.exists()
    assert garbage.exists()
    assert garbage.read_bytes() == garbage_bytes


def test_source_purge_skips_undecodable_file_with_malformed_yaml(
    tmp_path: Path,
) -> None:
    """A file that is neither decodable nor parseable is left on disk (#910).

    Pins the retry half of the frontmatter load: the invalid bytes make
    the strict read raise ``UnicodeDecodeError``, and the unterminated
    YAML flow sequence makes the lossy re-read raise as well, so *both*
    arms run and the engine ends up with no metadata at all. A file the
    engine cannot read matches no purge criteria, so it survives intact.

    This is the deliberate boundary of "fail closed". Failing closed
    resolves ambiguity *within* the criteria — that is exactly what the
    lossy re-read buys: a fragment whose metadata is sound still gets
    matched despite an undecodable body. It does not license deleting
    files by absence of evidence, which on a *scoped* purge would be
    unbounded collateral loss. The operator's total-RTBF hammer,
    ``purge_vault``, still reaches such files, because it wipes folder
    contents without ever parsing frontmatter.
    """
    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    unreadable = vault / "01-Fragments" / "Conversations" / "unreadable.md"
    raw = b"---\ntitle: [unclosed \xff\xfe\n---\nsecret body\n"
    unreadable.write_bytes(raw)
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.fragments_affected == 1
    assert not frag.exists()
    assert unreadable.exists()
    assert unreadable.read_bytes() == raw


def test_classifications_purge_does_not_corrupt_undecodable_fragment(
    tmp_path: Path,
) -> None:
    """The classification rewrite path must never re-encode a bad file (#910).

    This test is GREEN today **on purpose** — it is a regression guard,
    not a dead test. ``purge_classifications`` rewrites fragments in
    place via ``frontmatter.dumps`` + ``write_text``. If anyone makes
    the frontmatter helper this path uses lossy (``errors="replace"``),
    every undecodable byte would be silently rewritten as U+FFFD over
    the operator's original file — data loss. The byte-equality
    assertion fails the instant that happens.
    """
    vault = _make_vault(tmp_path)
    frag, raw = _write_fragment_with_undecodable_body(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_classifications()

    assert result.classifications_reset == 0
    assert frag.read_bytes() == raw


def test_decrement_counts_does_not_corrupt_undecodable_thread_file(
    tmp_path: Path,
) -> None:
    """A thread file with an undecodable body survives a purge intact (#910).

    ``_decrement_counts`` is the second in-place rewrite path. An
    unreadable thread file must be skipped (count not decremented)
    rather than crashing the purge or being re-encoded lossily, while
    the fragment itself is still deleted.
    """
    vault = _make_vault(tmp_path)
    thread = _write_thread(vault, "thread-1", "Waves", fragment_count=3)
    thread_bytes = thread.read_bytes() + _UNDECODABLE_BYTES + b"\n"
    thread.write_bytes(thread_bytes)
    frag = _write_fragment(vault, "frag-A", "Alpha", threads=["thread-1"])
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert result.fragments_affected == 1
    assert not frag.exists()
    assert result.threads_updated == 0
    assert thread.read_bytes() == thread_bytes


def test_source_purge_survives_two_undecodable_matching_fragments(
    tmp_path: Path,
) -> None:
    """Two undecodable matches are both deleted without raising (#910).

    Once matching stops skipping these files, deleting the first one
    walks the vault to scrub references — and that walk now reaches the
    *second* undecodable file. Pins that the scrub tolerates it instead
    of aborting the purge half-done.
    """
    vault = _make_vault(tmp_path)
    first, _first_raw = _write_fragment_with_undecodable_body(vault, "frag-A", "Alpha")
    second, _second_raw = _write_fragment_with_undecodable_body(
        vault,
        "frag-B",
        "Bravo",
    )
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.fragments_affected == 2
    assert not first.exists()
    assert not second.exists()
    assert _vault_files_containing_bytes(vault, _UNDECODABLE_SECRET.encode()) == []
    outcome = PurgeAuditLog(vault).read()[-1]
    assert outcome.status == "complete"
    assert outcome.fragments_deleted == 2
    assert sorted(outcome.affected_fragments) == ["frag-A", "frag-B"]


def test_purge_tolerates_bare_yaml_date_key_without_crashing(
    tmp_path: Path,
) -> None:
    """A bare YAML date key raises ``TypeError``, which must stay tolerated.

    ``2024-05-01: note`` parses to a ``datetime.date`` *key*, and
    ``frontmatter`` then splats the mapping as keyword arguments —
    ``TypeError: keywords must be strings``. GREEN today; it guards the
    narrowing of the engine's bare ``except Exception`` so nobody drops
    ``TypeError`` from the tolerated tuple (precedent: PR #927 /
    issue #847). The odd file contributes zero resets while a normal
    sibling still resets.
    """
    vault = _make_vault(tmp_path)
    odd = vault / "01-Fragments" / "Conversations" / "odd.md"
    odd_text = "---\n2024-05-01: note\n---\nbody\n"
    odd.write_text(odd_text, encoding="utf-8")
    _write_fragment(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    result = engine.purge_classifications()

    assert result.classifications_reset == 1
    assert result.affected_fragment_ids == ["frag-A"]
    assert odd.read_text(encoding="utf-8") == odd_text


def test_purge_tolerates_malformed_yaml_without_crashing(
    tmp_path: Path,
) -> None:
    """Genuinely invalid YAML frontmatter is skipped, not deleted (#910).

    An unterminated flow sequence raises ``yaml.YAMLError``. GREEN
    today; it pins ``yaml.YAMLError`` into the tolerated-error tuple
    that replaces the bare ``except Exception``, and pins that a file
    the engine cannot classify is left alone rather than purged.
    """
    vault = _make_vault(tmp_path)
    broken = vault / "01-Fragments" / "Conversations" / "broken.md"
    broken_text = "---\ntitle: [unclosed\n---\nA body with no links.\n"
    broken.write_text(broken_text, encoding="utf-8")
    frag = _write_fragment(vault, "frag-A", "Alpha", platform="claude")
    engine = PurgeEngine(vault)

    result = engine.purge_source("claude")

    assert result.fragments_affected == 1
    assert not frag.exists()
    assert broken.exists()
    assert broken.read_text(encoding="utf-8") == broken_text


def test_source_purge_warns_when_fragment_body_is_undecodable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An undecodable body is a WARNING, not a debug whisper (#910).

    Silently degrading to a lossy read on an RTBF-critical path is an
    operator-visible event: the log must name the offending file so it
    can be inspected. Asserts on level and path only — never on wording.
    """
    vault = _make_vault(tmp_path)
    frag, _raw = _write_fragment_with_undecodable_body(vault, "frag-A", "Alpha")
    engine = PurgeEngine(vault)

    with caplog.at_level(logging.WARNING, logger="creek.purge.engine"):
        engine.purge_source("claude")

    naming_the_file = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and str(frag) in record.getMessage()
    ]
    assert len(naming_the_file) >= 1
