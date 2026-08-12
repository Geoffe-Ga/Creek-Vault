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
import shutil
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
# Engine tests — intimate stub pointer containment (#950)
# ---------------------------------------------------------------------------

_INTIMATE_STUB_DIR = "10-Liminal/Compost/intimate-stubs"
"""The only directory an ``intimate_body_pointer`` may delete from (#950)."""

_VAULT_CONFIG_RELPATH = "00-Creek-Meta/creek_config.yaml"
"""In-vault victim proving a hand-edited pointer is refused, not followed."""

_PURGE_AUDIT_RELPATH = "00-Creek-Meta/audit/purge.jsonl"
"""The audit log — a live in-vault file mid-purge, so a reachable victim."""

_NUL_YAML_ESCAPE = r"\0"
"""Two-character YAML escape that PyYAML decodes to a real NUL on load."""


def _write_note_with_nul_stub_pointer(
    vault: Path,
    frag_id: str,
    title: str,
) -> Path:
    """Hand-write an intimate note whose stub pointer holds a NUL byte.

    ``frontmatter.dumps`` cannot be used here: PyYAML's emitter refuses
    to serialise a control character, so a NUL put on a
    :class:`frontmatter.Post` never survives the round-trip. The
    markdown is therefore written by hand with the pointer as a
    double-quoted YAML scalar carrying :data:`_NUL_YAML_ESCAPE`, which
    PyYAML *decodes* back into a real NUL when the engine loads the
    note.

    Args:
        vault: Vault root.
        frag_id: Fragment ID for the note's frontmatter.
        title: Note title (also the note's filename stem).

    Returns:
        Path to the written note.
    """
    pointer = f"{_INTIMATE_STUB_DIR}/{title}{_NUL_YAML_ESCAPE}.md"
    note_path = vault / "01-Fragments" / "Conversations" / f"{title}.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        f"""---
id: {frag_id}
title: {title}
type: fragment
privacy_tier: intimate
saved_from:
  source_kind: answer
  intimate_body_pointer: "{pointer}"
---
(intimate body withheld)
""",
        encoding="utf-8",
    )
    return note_path


def _write_journal_fragment_with_nul_origin_key(
    vault: Path,
    frag_id: str,
    title: str,
) -> Path:
    """Hand-write a journal fragment whose ``origin_key`` holds a NUL byte.

    The ``source.origin_key`` twin of
    :func:`_write_note_with_nul_stub_pointer`, and hand-written for the
    same reason — PyYAML's emitter will not round-trip a NUL, so the
    escape has to go into the file as literal YAML text.

    Args:
        vault: Vault root.
        frag_id: Fragment ID for the fragment's frontmatter.
        title: Fragment title (also the fragment's filename stem).

    Returns:
        Path to the written fragment.
    """
    key = f"{_JOURNAL_STAGING_DIR}/{title}{_NUL_YAML_ESCAPE}.md"
    frag_path = vault / "01-Fragments" / "Journal" / f"{title}.md"
    frag_path.parent.mkdir(parents=True, exist_ok=True)
    frag_path.write_text(
        f"""---
id: {frag_id}
title: {title}
type: fragment
privacy_tier: intimate
source:
  platform: journal
  origin_key: "{key}"
---
A journal fragment summary.
""",
        encoding="utf-8",
    )
    return frag_path


def test_fragment_purge_refuses_stub_pointer_at_vault_config(
    tmp_path: Path,
) -> None:
    """A stub pointer aimed at the vault config never deletes it (#950).

    ``intimate_body_pointer`` is vault *content*: an operator (or an
    attacker who can write one note) can retarget it at any file. The
    only containment today is "inside the vault root", which every
    in-vault file trivially satisfies — so the pointer becomes an
    arbitrary in-vault delete primitive driven by a purge. Deleting
    ``creek_config.yaml`` also disarms the GAP-003 marker check that
    guards ``purge vault``. The stub sweep must be scoped to
    ``10-Liminal/Compost/intimate-stubs/`` and refuse anything else,
    while still purging the note that carried the bad pointer.
    """
    vault = _make_vault(tmp_path)
    config = vault / _VAULT_CONFIG_RELPATH
    config_bytes_before = config.read_bytes()
    note, _stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=_VAULT_CONFIG_RELPATH,
        write_stub=False,
    )
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-A")

    assert config.exists()
    assert config.read_bytes() == config_bytes_before
    assert result.intimate_stubs_removed == 0
    # The refusal is scoped to the stub sweep: the note itself is still
    # purged, so the RTBF request is honoured.
    assert not note.exists()


def test_fragment_purge_refuses_stub_pointer_at_sibling_fragment(
    tmp_path: Path,
) -> None:
    """Purging one note must not take a sibling fragment with it (#950).

    The worst form of the containment bug: purging fragment A silently
    destroys unrelated fragment B, so a single RTBF request becomes
    collateral data loss the operator never asked for and the audit log
    never names (B is not in ``affected_fragments``). Bravo must survive
    byte-identical and stay independently purgeable afterwards.
    """
    vault = _make_vault(tmp_path)
    bravo = _write_fragment(vault, "frag-B", "Bravo")
    bravo_bytes_before = bravo.read_bytes()
    note, _stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath="01-Fragments/Conversations/Bravo.md",
        write_stub=False,
    )

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0
    assert not note.exists()
    assert bravo.exists()
    assert bravo.read_bytes() == bravo_bytes_before

    # Bravo is untouched, not merely present: it is still a live
    # fragment the engine can find and purge on its own terms.
    second = PurgeEngine(vault).purge_fragment("frag-B")

    assert second.fragments_affected == 1
    assert not bravo.exists()


def test_fragment_purge_refuses_stub_pointer_at_audit_log(
    tmp_path: Path,
) -> None:
    """A stub pointer must not be able to erase the purge audit log (#950).

    The GAP-002 ``intent`` line is written *before* the destructive
    body, so ``00-Creek-Meta/audit/purge.jsonl`` is a live file by the
    time the stub sweep runs — a pointer aimed at it unlinks the
    forensic record of the very purge in flight, and the following
    ``outcome`` append silently restarts the chain from genesis. The
    surviving log must be the complete intent+outcome pair with an
    intact hash chain.
    """
    vault = _make_vault(tmp_path)
    note, _stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=_PURGE_AUDIT_RELPATH,
        write_stub=False,
    )

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0
    assert not note.exists()
    log = PurgeAuditLog(vault)
    entries = log.read()
    assert [entry.phase for entry in entries] == ["intent", "outcome"]
    assert entries[1].status == "complete"
    log.verify()


def test_fragment_purge_refuses_stub_pointer_escaping_stub_dir_via_relative_walk(
    tmp_path: Path,
) -> None:
    """``..`` inside the stub dir cannot walk back out to a vault file (#950).

    This is the test that proves the new guard is real containment
    rather than a string-prefix check: the pointer *starts* with the
    canonical stub directory and still lands on the vault config after
    resolution. It also passes the existing vault-root guard untouched,
    so only a guard applied to the **resolved** path stops it.
    """
    vault = _make_vault(tmp_path)
    config = vault / _VAULT_CONFIG_RELPATH
    config_bytes_before = config.read_bytes()
    note, _stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=f"{_INTIMATE_STUB_DIR}/../../../{_VAULT_CONFIG_RELPATH}",
        write_stub=False,
    )

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert config.exists()
    assert config.read_bytes() == config_bytes_before
    assert result.intimate_stubs_removed == 0
    assert not note.exists()


def test_fragment_purge_dry_run_refuses_out_of_scope_stub_pointer(
    tmp_path: Path,
) -> None:
    """A dry run must not *preview* an out-of-scope stub deletion (#950).

    ``--dry-run`` is the operator's decision aid before an irreversible
    purge. Counting a file the real run must refuse to touch both
    overstates the blast radius and, worse, tells the operator the
    engine considers that file in scope — so the containment guard has
    to run before the counter, not just before the ``unlink``.
    """
    vault = _make_vault(tmp_path)
    config = vault / _VAULT_CONFIG_RELPATH
    config_bytes_before = config.read_bytes()
    _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=_VAULT_CONFIG_RELPATH,
        write_stub=False,
    )

    result = PurgeEngine(vault, dry_run=True).purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0
    assert config.exists()
    assert config.read_bytes() == config_bytes_before


def test_refused_stub_pointer_warning_does_not_echo_resolved_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Refusing a pointer is logged, but without an existence oracle (#950).

    Two properties at once. The refusal must be operator-visible at
    WARNING — a silently ignored pointer is indistinguishable from a
    sweep that never ran. And the message must not echo the *resolved*
    victim path: the refusal is triggered by attacker-controlled
    frontmatter, so echoing the absolute path it resolved to turns the
    log into a filesystem oracle. Asserts on level and on the absence
    of the resolved path only — never on wording.
    """
    vault = _make_vault(tmp_path)
    _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=_VAULT_CONFIG_RELPATH,
        write_stub=False,
    )
    resolved_victim = str((vault / _VAULT_CONFIG_RELPATH).resolve())
    engine = PurgeEngine(vault)

    with caplog.at_level(logging.WARNING, logger="creek.purge.engine"):
        engine.purge_fragment("frag-A")

    refusals = [
        record
        for record in caplog.records
        if record.name == "creek.purge.engine" and record.levelno == logging.WARNING
    ]
    assert len(refusals) >= 1
    echoing = [
        record for record in caplog.records if resolved_victim in record.getMessage()
    ]
    assert echoing == []


def test_fragment_purge_removes_nested_intimate_stub(tmp_path: Path) -> None:
    """A stub nested under the stub dir is still swept (#950 no-regression).

    The containment guard must be a ``is_relative_to`` containment test,
    not an exact-parent-directory comparison: ``creek save`` is free to
    shard stubs into subdirectories, and a guard that only accepted the
    stub root would silently start leaking every nested intimate body
    past an RTBF request. Green before and after the fix.
    """
    vault = _make_vault(tmp_path)
    note, stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=f"{_INTIMATE_STUB_DIR}/2026/Alpha.md",
    )

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 1
    assert not note.exists()
    assert not stub.exists()


def test_fragment_purge_refuses_absolute_stub_pointer(tmp_path: Path) -> None:
    """An absolute stub pointer is refused, not followed (#950).

    Pins a pathlib footgun on a deletion path: ``Path("/vault") /
    "/etc/passwd"`` discards the left operand entirely and evaluates to
    ``Path("/etc/passwd")``, so an absolute pointer silently escapes the
    vault at the *join*, before any guard sees it. The vault-root
    containment check catches it today; this test stops a future
    refactor (e.g. swapping the join for ``os.path.join``-style
    concatenation, or guarding the raw pointer instead of the resolved
    path) from quietly reintroducing the escape.
    """
    vault = _make_vault(tmp_path)
    outsider = tmp_path / "absolute-outsider.md"
    outsider.write_text("not a stub\n", encoding="utf-8")
    outsider_bytes_before = outsider.read_bytes()
    note, _stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=str(outsider),
        write_stub=False,
    )

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert outsider.exists()
    assert outsider.read_bytes() == outsider_bytes_before
    assert result.intimate_stubs_removed == 0
    assert not note.exists()


def test_fragment_purge_stub_pointer_at_stub_root_itself_is_noop(
    tmp_path: Path,
) -> None:
    """A pointer naming the stub *directory* deletes nothing (#950).

    ``is_relative_to`` treats a path as relative to itself, so the
    canonical stub root passes the containment guard on its own. Nothing
    is removed only because the engine additionally requires the target
    to be a regular file. That distinction is what stops a directory
    pointer from ever reaching ``unlink``, so pin it explicitly rather
    than leaving the safe outcome incidental.
    """
    vault = _make_vault(tmp_path)
    stub_root = vault / _INTIMATE_STUB_DIR
    stub_root.mkdir(parents=True, exist_ok=True)
    bystander = stub_root / "Unrelated.md"
    bystander.write_text("another intimate body\n", encoding="utf-8")
    note, _stub = _write_intimate_note_with_stub(
        vault,
        "frag-A",
        "Alpha",
        stub_relpath=_INTIMATE_STUB_DIR,
        write_stub=False,
    )

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0
    assert stub_root.is_dir()
    assert bystander.exists()
    assert not note.exists()


def test_fragment_purge_nul_byte_stub_pointer_is_noop(tmp_path: Path) -> None:
    """A NUL byte in the stub pointer is a no-op, never an error (#950).

    ``Path.resolve()`` raises :class:`ValueError` (``embedded null
    byte``) rather than ``OSError`` for a NUL, so an unguarded resolve
    escapes the documented "a bad pointer is a no-op" contract: the
    exception aborts the purge body, the fragment is never unlinked,
    and the audit closes ``status="partial"``. One byte of hostile
    frontmatter therefore blocks the RTBF deletion it was attached to.
    """
    vault = _make_vault(tmp_path)
    note = _write_note_with_nul_stub_pointer(vault, "frag-A", "Alpha")
    loaded = frontmatter.load(str(note))
    # Guard against a vacuous pass: the escape must really have decoded
    # to a NUL, or this test proves nothing about NUL handling.
    assert "\x00" in loaded["saved_from"]["intimate_body_pointer"]

    result = PurgeEngine(vault).purge_fragment("frag-A")

    assert result.intimate_stubs_removed == 0
    assert not note.exists()
    entries = PurgeAuditLog(vault).read()
    assert [entry.phase for entry in entries] == ["intent", "outcome"]
    assert entries[1].status == "complete"


def test_fragment_purge_nul_byte_origin_key_is_noop(tmp_path: Path) -> None:
    """A NUL byte in ``source.origin_key`` is a no-op, never an error (#950).

    The journal-staging twin of the stub-pointer case: the same
    unguarded ``Path.resolve()`` sits in ``_purge_staged_source_entry``,
    so a NUL in the origin key aborts the purge of an intimate journal
    fragment with a bare :class:`ValueError` instead of skipping the
    unusable key and deleting the fragment.
    """
    vault = _make_vault(tmp_path)
    frag = _write_journal_fragment_with_nul_origin_key(vault, "frag-J", "entry-one")
    loaded = frontmatter.load(str(frag))
    assert "\x00" in loaded["source"]["origin_key"]

    result = PurgeEngine(vault).purge_fragment("frag-J")

    assert result.journal_staged_removed == 0
    assert not frag.exists()
    entries = PurgeAuditLog(vault).read()
    assert [entry.phase for entry in entries] == ["intent", "outcome"]
    assert entries[1].status == "complete"


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
# Engine tests — upload staged-source sweep (#1023)
# ---------------------------------------------------------------------------

_UPLOAD_STAGING_DIR = "00-Creek-Meta/adepthood/uploads"
"""Where ``upload_tool`` stages uploaded document bytes (the ledger key root)."""

_UPLOAD_SECRET = b"synthetic-upload-secret-1023"
"""Synthetic marker standing in for uploaded document content — never real data."""


def _write_upload_fragment_with_staged(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    origin_key: str | None = None,
    write_staged: bool = True,
) -> tuple[Path, Path]:
    """Write a document fragment plus the staged upload bytes it points at.

    Mirrors :func:`_write_journal_fragment_with_staged` for the upload
    surface (#1023), with two deliberate differences that decide whether
    these tests can catch a half-fix:

    * The staged file is a ``.pdf``, not a ``.md``. A markdown staged
      file would pass against a fix that widened the sweep but never
      gave a non-markdown source an ``origin_key``.
    * Its content is raw, non-UTF-8 bytes carrying
      :data:`_UPLOAD_SECRET`, exactly as ``creek.upload`` stages an
      uploaded document byte-for-byte.

    Args:
        vault: Vault root.
        frag_id: Fragment ID for the fragment's frontmatter.
        title: Fragment title (also the staged file's stem by default).
        origin_key: Vault-relative pointer recorded in the fragment's
            ``source.origin_key``. Defaults to the canonical uploads
            staging path for *title*.
        write_staged: When ``False``, record the pointer but do not
            create the staged file on disk.

    Returns:
        Tuple of ``(fragment_path, staged_path)``.
    """
    key = origin_key or f"{_UPLOAD_STAGING_DIR}/{title}.pdf"
    staged_path = vault / key
    if write_staged:
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        # 0xFF is invalid UTF-8: proof that a text-based sweep cannot
        # even read what an upload leaves behind.
        staged_path.write_bytes(b"%PDF-1.7\n\xff\x00" + _UPLOAD_SECRET + b"\n")

    frag_path = vault / "01-Fragments" / "Notes" / f"{title}.md"
    frag_post = frontmatter.Post(
        content="A document fragment summary.\n",
        id=frag_id,
        title=title,
        type="fragment",
        source={
            "platform": "document",
            "origin_key": key,
            "original_file": str(staged_path),
        },
        threads=[],
        eddies=[],
        privacy_tier="private",
    )
    frag_path.parent.mkdir(parents=True, exist_ok=True)
    frag_path.write_text(frontmatter.dumps(frag_post), encoding="utf-8")
    return frag_path, staged_path


def _vault_files_containing_upload_secret(vault: Path) -> list[Path]:
    """Return every vault file whose raw bytes still carry the upload secret.

    Deliberately byte-based and extension-agnostic:
    :func:`_vault_files_containing_secret` globs ``*.md`` and calls
    ``read_text``, so it would both miss a staged ``.pdf`` and raise
    :class:`UnicodeDecodeError` if it were pointed at one. The RTBF
    contract is vault-wide and format-blind — after a purge,
    :data:`_UPLOAD_SECRET` must appear in **no** file under the vault
    root, whatever its suffix.

    Args:
        vault: Vault root to walk.

    Returns:
        Offending paths — empty when the purge honored the RTBF request.
    """
    return [
        path
        for path in sorted(vault.rglob("*"))
        if path.is_file() and _UPLOAD_SECRET in path.read_bytes()
    ]


def test_fragment_purge_removes_an_upload_staged_source_file(tmp_path: Path) -> None:
    """Purging an upload-sourced fragment deletes its staged bytes (#1023)."""
    vault = _make_vault(tmp_path)
    frag, staged = _write_upload_fragment_with_staged(vault, "frag-U", "report-one")
    engine = PurgeEngine(vault)

    result = engine.purge_fragment("frag-U")

    assert result.journal_staged_removed == 1
    assert not frag.exists()
    assert not staged.exists()
    # The RTBF core: the uploaded document survives NOWHERE under the vault.
    assert _vault_files_containing_upload_secret(vault) == []


def test_source_purge_removes_an_upload_staged_source_file(tmp_path: Path) -> None:
    """``purge_source("document")`` sweeps the staged upload too (#1023).

    Pins the shared ``_purge_single`` path that serves both source-scoped
    and daterange purges, not only the single-fragment path.
    """
    vault = _make_vault(tmp_path)
    _frag, staged = _write_upload_fragment_with_staged(vault, "frag-U", "report-one")
    engine = PurgeEngine(vault)

    result = engine.purge_source("document")

    assert result.fragments_affected == 1
    assert result.journal_staged_removed == 1
    assert not staged.exists()
    assert _vault_files_containing_upload_secret(vault) == []


def test_vault_purge_wipes_the_uploads_staging_dir(tmp_path: Path) -> None:
    """``purge_vault`` wipes *every* Adepthood staging root (#1023).

    ``00-Creek-Meta/`` is exempt from the content-folder wipe, so both
    staging dirs need an explicit sweep. Staged files are source
    material, not fragments, so ``fragments_affected`` must still match
    a twin vault that never had them.
    """
    vault = _make_vault(tmp_path)
    _frag_a, staged_a = _write_upload_fragment_with_staged(
        vault,
        "frag-U1",
        "report-one",
    )
    _frag_b, staged_b = _write_upload_fragment_with_staged(
        vault,
        "frag-U2",
        "report-two",
    )
    _frag_j, staged_j = _write_journal_fragment_with_staged(
        vault,
        "frag-J1",
        "entry-one",
    )
    twin = _make_vault(tmp_path / "twin")
    for twin_id, twin_title in [("frag-U1", "report-one"), ("frag-U2", "report-two")]:
        _write_upload_fragment_with_staged(
            twin,
            twin_id,
            twin_title,
            write_staged=False,
        )
    _write_journal_fragment_with_staged(
        twin,
        "frag-J1",
        "entry-one",
        write_staged=False,
    )

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)
    twin_result = PurgeEngine(twin).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.journal_staged_removed == 3
    assert not staged_a.exists()
    assert not staged_b.exists()
    assert not staged_j.exists()
    assert _vault_files_containing_upload_secret(vault) == []
    assert _vault_files_containing_secret(vault) == []
    # Staged files never inflate the fragment count.
    assert result.fragments_affected == twin_result.fragments_affected


def test_upload_staged_dry_run_previews_without_deleting(tmp_path: Path) -> None:
    """Dry-run counts the staged upload it would delete but leaves it on disk.

    The counter increments only *after* the containment and ``is_file()``
    guards, so a dry run can never preview a deletion the real run would
    refuse.
    """
    vault = _make_vault(tmp_path)
    frag, staged = _write_upload_fragment_with_staged(vault, "frag-U", "report-one")

    frag_result = PurgeEngine(vault, dry_run=True).purge_fragment("frag-U")

    assert frag_result.journal_staged_removed == 1
    assert frag.exists()
    assert staged.exists()

    vault_result = PurgeEngine(vault, dry_run=True).purge_vault(
        VAULT_PURGE_CONFIRMATION,
    )

    assert vault_result.journal_staged_removed == 1
    assert staged.exists()


def test_an_origin_key_outside_every_staging_root_is_skipped(tmp_path: Path) -> None:
    """Widening the sweep ADDS a staging root — it never drops the guard.

    The twin of ``test_journal_staged_pointer_outside_vault_skipped`` for
    the upload fixture. A pointer escaping the vault root and a pointer
    at a vault file outside *every* staging root must both survive, each
    with a zero counter, while the fragment itself is still deleted.
    """
    vault = _make_vault(tmp_path)
    # Variant 1: the pointer escapes the vault root entirely.
    frag_out, outside = _write_upload_fragment_with_staged(
        vault,
        "frag-out",
        "escapee",
        origin_key="../outside-secret.pdf",
    )

    result_out = PurgeEngine(vault).purge_fragment("frag-out")

    assert result_out.journal_staged_removed == 0
    assert not frag_out.exists()
    assert outside.exists()
    assert _UPLOAD_SECRET in outside.read_bytes()

    # Variant 2: the pointer stays inside the vault but outside both
    # staging roots (staging-dir scope guard).
    frag_in, decoy = _write_upload_fragment_with_staged(
        vault,
        "frag-in",
        "decoyed",
        origin_key="01-Fragments/decoy.md",
    )

    result_in = PurgeEngine(vault).purge_fragment("frag-in")

    assert result_in.journal_staged_removed == 0
    assert not frag_in.exists()
    assert decoy.exists()
    assert _UPLOAD_SECRET in decoy.read_bytes()


def test_a_subdirectory_in_a_staging_dir_does_not_abort_a_vault_purge(
    tmp_path: Path,
) -> None:
    """A subdirectory under a staging dir must not abort the vault purge.

    The wipe loop iterates the staging dir non-recursively, so an
    ``unlink`` with no ``is_file()`` guard raises an ``OSError`` on any
    subdirectory (``IsADirectoryError`` on Linux, ``PermissionError`` on
    macOS) — and ``_run_audited`` then closes the audit
    ``status="partial"`` and re-raises, aborting the whole RTBF request.
    Verified load-bearing: deleting the ``is_file()`` guard fails this
    test.
    """
    vault = _make_vault(tmp_path)
    _frag, staged = _write_upload_fragment_with_staged(vault, "frag-U", "report-one")
    (staged.parent / "nested").mkdir()

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    # The directory is not a staged file, so it is never counted.
    assert result.journal_staged_removed == 1
    assert not staged.exists()
    entries = PurgeAuditLog(vault).read()
    assert [entry.phase for entry in entries] == ["intent", "outcome"]
    assert entries[1].status == "complete"


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

    # Exactly one fragment existed, so exactly one was destroyed. The
    # loose ``>= 1`` this replaces was satisfied by the pre-#1340 count
    # of 3 — the number of top-level *directories* the wipe walked, two
    # of them empty.
    assert result.fragments_affected == 1
    assert result.affected_fragment_ids == ["frag-A"]
    # Real files only. A directory path in an RTBF deletion record is
    # neither a fragment nor an auditable erasure of one.
    assert sorted(result.deleted_files) == sorted(
        [
            str(vault / "01-Fragments" / "Conversations" / "Alpha.md"),
            str(vault / "02-Threads" / "Active" / "Waves.md"),
        ],
    )
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
    assert result.fragments_affected == 1
    assert result.affected_fragment_ids == ["frag-A"]
    assert frag.exists()


# ---------------------------------------------------------------------------
# #1340 — a vault purge counts fragment FILES, never folders
#
# ``purge_vault`` reported ``fragments_affected = len(deleted_files)``,
# and ``deleted_files`` held the *top-level* entries of each content
# folder — directories, mostly. A vault holding 500 fragments in
# ``01-Fragments/Conversations`` therefore certified "3 fragments
# deleted, affected_fragments=[]" to the compliance log while destroying
# all 500. These tests pin the count, the ids, and the shape of
# ``deleted_files`` against the filesystem rather than against each
# other.
# ---------------------------------------------------------------------------

_VAULT_PURGE_CORPUS = 500
"""Fragment count for the #1340 acceptance criterion, verbatim from the issue.

Large enough that the defect is unmistakable (the pre-fix report said
``3``) and small enough to stay a unit test.
"""

_RECONCILIATION_CORPUS = 20
"""Smaller corpus for the dry/apply comparison — the 500 lives in one test."""


def _seed_fragment_corpus(vault: Path, count: int) -> list[str]:
    """Write *count* fragments spread across two nested subdirectories.

    Nesting is the point: the defect counted the top-level entries of
    ``01-Fragments``, so a corpus that lives two levels down is
    invisible to it no matter how large.

    Args:
        vault: Vault root.
        count: How many fragment files to write.

    Returns:
        The ids written, in creation order.
    """
    ids: list[str] = []
    for index in range(count):
        subfolder = "Conversations/2024" if index % 2 == 0 else "Messages/Archive/2025"
        frag_id = f"frag-{index:04d}"
        _write_fragment(vault, frag_id, f"Note-{index:04d}", subfolder=subfolder)
        ids.append(frag_id)
    return ids


def _relative_deleted_files(result: PurgeResult, vault: Path) -> list[str]:
    """Return ``deleted_files`` as sorted paths relative to their own vault.

    Two vaults built for a dry/apply comparison live under different
    ``tmp_path`` roots, so the absolute strings can never be equal even
    when the engine behaved identically. Stripping each result's own
    root is what makes the comparison meaningful.

    Args:
        result: The purge result whose ``deleted_files`` to normalise.
        vault: The vault root that result was produced against.

    Returns:
        Sorted root-relative path strings.
    """
    prefix = str(vault)
    return sorted(path.removeprefix(prefix) for path in result.deleted_files)


def _last_vault_outcome(vault: Path) -> PurgeAuditEntry:
    """Return the last ``vault``-operation ``outcome`` line from the audit log.

    Args:
        vault: Vault root whose ``purge.jsonl`` to read.

    Returns:
        The final outcome entry written by a ``purge_vault`` call.
    """
    outcomes = [
        entry
        for entry in PurgeAuditLog(vault).read()
        if entry.phase == "outcome" and entry.operation == "vault"
    ]
    assert outcomes, "purge_vault must write an outcome line"
    return outcomes[-1]


def test_vault_purge_counts_every_fragment_file_not_the_folders(
    tmp_path: Path,
) -> None:
    """500 fragments are reported as 500, and every id is named (#1340).

    The acceptance criterion of the issue, asserted three ways that can
    fail independently: against the number counted off the filesystem
    *before* the purge, against the literal 500, and against the audit
    log the compliance officer actually reads. An internal counter
    checked only against another internal counter can drift the same way
    twice.
    """
    vault = _make_vault(tmp_path)
    expected_ids = _seed_fragment_corpus(vault, _VAULT_PURGE_CORPUS)
    on_disk_before = sorted((vault / "01-Fragments").rglob("*.md"))
    assert len(on_disk_before) == _VAULT_PURGE_CORPUS

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.fragments_affected == len(on_disk_before)
    assert result.fragments_affected == _VAULT_PURGE_CORPUS
    assert len(result.affected_fragment_ids) == _VAULT_PURGE_CORPUS
    assert set(result.affected_fragment_ids) == set(expected_ids)
    outcome = _last_vault_outcome(vault)
    assert outcome.fragments_deleted == _VAULT_PURGE_CORPUS
    assert len(outcome.affected_fragments) == _VAULT_PURGE_CORPUS
    assert list((vault / "01-Fragments").rglob("*.md")) == []


def test_vault_purge_records_no_deleted_file_for_an_empty_directory(
    tmp_path: Path,
) -> None:
    """An empty folder is removed from disk but is not a deleted *file* (#1340).

    ``deleted_files`` is a record of destroyed content. An empty
    directory destroyed nothing, so it must contribute zero entries —
    while still disappearing, because the wipe's job is unchanged.
    """
    vault = _make_vault(tmp_path)
    empty = vault / "02-Threads" / "Dormant"
    empty.mkdir()
    _write_thread(vault, "thread-1", "Waves")

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.deleted_files == [str(vault / "02-Threads" / "Active" / "Waves.md")]
    assert result.fragments_affected == 0
    assert result.affected_fragment_ids == []
    assert not empty.exists()
    assert (vault / "02-Threads").is_dir()


def test_vault_purge_deleted_files_names_files_and_no_directory(
    tmp_path: Path,
) -> None:
    """``deleted_files`` is exactly the regular files, nested ones included.

    Asserted as an exact set rather than with a post-hoc ``is_dir()``
    sweep: after the purge everything named is gone, so ``is_dir()``
    answers ``False`` for a directory path too and would pass vacuously.
    The directory paths are collected *before* the purge instead, and
    the record is required to be disjoint from them.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha", subfolder="Conversations/2024/May")
    nested = vault / "01-Fragments" / "Conversations" / "2024" / "May" / "Alpha.md"
    _write_thread(vault, "thread-1", "Waves")
    (vault / "03-Eddies" / "Stale").mkdir()
    directories_before = {
        str(path)
        for folder in ("01-Fragments", "02-Threads", "03-Eddies", "04-Praxis")
        for path in (vault / folder).rglob("*")
        if path.is_dir()
    }

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert sorted(result.deleted_files) == sorted(
        [
            str(nested),
            str(vault / "02-Threads" / "Active" / "Waves.md"),
        ],
    )
    assert directories_before.isdisjoint(result.deleted_files)
    assert result.fragments_affected == 1
    assert result.affected_fragment_ids == ["frag-A"]


def test_vault_purge_counts_unidentifiable_fragments_but_names_no_id(
    tmp_path: Path,
) -> None:
    """The count and the id list are deliberately asymmetric (#1340).

    A vault purge destroys every file under ``01-Fragments`` whether or
    not the engine can read an id out of it, so the *count* must include
    a fragment with no ``id`` and one whose frontmatter will not parse.
    The id list cannot: naming an id nobody recorded would be a
    fabrication in a compliance record. Under-counting the destruction
    is the worse error, so the asymmetry resolves that way.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    no_id = vault / "01-Fragments" / "Conversations" / "no-id.md"
    no_id.write_text("---\ntitle: Untitled\n---\nA body.\n", encoding="utf-8")
    broken = vault / "01-Fragments" / "Messages" / "broken.md"
    broken.write_text("---\ntitle: [unclosed\n---\nA body.\n", encoding="utf-8")

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.fragments_affected == 3
    assert result.affected_fragment_ids == ["frag-A"]
    assert list((vault / "01-Fragments").rglob("*.md")) == []


def test_vault_purge_dry_run_predicts_its_own_apply(tmp_path: Path) -> None:
    """A dry vault purge and its apply twin agree on count, ids, and paths.

    Two vaults seeded identically, one previewed and one destroyed. The
    enumeration pass has to run *before* the wipe for this to hold: read
    the ids afterwards and the apply run would report none.
    """
    dry_vault = _make_vault(tmp_path / "dry")
    apply_vault = _make_vault(tmp_path / "apply")
    for target in (dry_vault, apply_vault):
        _seed_fragment_corpus(target, _RECONCILIATION_CORPUS)

    dry = PurgeEngine(dry_vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)
    applied = PurgeEngine(apply_vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert dry.fragments_affected == applied.fragments_affected
    assert dry.fragments_affected == _RECONCILIATION_CORPUS
    assert dry.affected_fragment_ids == applied.affected_fragment_ids
    assert _relative_deleted_files(dry, dry_vault) == _relative_deleted_files(
        applied,
        apply_vault,
    )


def test_vault_purge_never_counts_staged_adepthood_files_as_fragments(
    tmp_path: Path,
) -> None:
    """Staged source files count on their own counter only (#845/#1023/#1340).

    ``_wipe_adepthood_staging``'s contract is documented at
    ``engine.py:1343-1344``: staged files are source material, never
    appended to ``deleted_files`` and never inflating
    ``fragments_affected``. Until #1340 that held only because the
    ``fragments_affected`` assignment happened to run before the staging
    sweep. Statement order is not an invariant; this test is.
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

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.journal_staged_removed == 2
    assert result.fragments_affected == 2
    assert sorted(result.affected_fragment_ids) == ["frag-J1", "frag-J2"]
    assert sorted(result.deleted_files) == sorted(
        [
            str(vault / "01-Fragments" / "Journal" / "entry-one.md"),
            str(vault / "01-Fragments" / "Journal" / "entry-two.md"),
        ],
    )
    assert not staged_a.exists()
    assert not staged_b.exists()


def test_the_vault_deletion_record_never_walks_through_a_symlink(
    tmp_path: Path,
) -> None:
    """A symlinked folder does not put out-of-vault paths in the record (#1340).

    Recording recursive files instead of top-level entries means the
    walk meets whatever the vault contains, and ``rglob`` will scandir a
    symlinked directory it is anchored on. A vault holding
    ``01-Fragments/ext -> <somewhere else>`` therefore had every file
    behind that link enumerated into ``deleted_files`` — which is copied
    into the MCP tool payload and shown to the operator.

    Two things are wrong with that at once. It names paths outside the
    vault in the record of an erasure that never touched them, and it
    previews deletions the apply run cannot make: ``shutil.rmtree``
    refuses a symlinked directory outright.
    """
    outside = tmp_path / "outside_the_vault"
    (outside / "private").mkdir(parents=True)
    (outside / "secret.pdf").write_text("sensitive", encoding="utf-8")
    (outside / "private" / "deeper.txt").write_text("deeper", encoding="utf-8")
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    link = vault / "01-Fragments" / "ext"
    link.symlink_to(outside, target_is_directory=True)

    result = PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.deleted_files == [
        str(vault / "01-Fragments" / "Conversations" / "Alpha.md"),
    ]
    # Nothing behind the link is named, and nothing behind it is touched.
    assert (outside / "secret.pdf").exists()
    assert (outside / "private" / "deeper.txt").exists()


def test_an_aborted_vault_purge_certifies_no_deletion_it_did_not_make(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial vault purge must never over-claim in the audit log (#1340).

    The census that names the fragments has to run *before* the wipe,
    because afterwards the files are gone. Committing it to the result
    at that point, though, hands ``_run_audited``'s ``except`` clause a
    fully populated count: the very first ``rmtree`` fails, nothing is
    deleted, and the surviving hash-chained compliance record still
    reads ``fragments_deleted: 1`` and names ``frag-A`` as erased.

    Of the two ways to be wrong, an RTBF record that over-claims is the
    dangerous one — it tells an operator content is gone while it is
    sitting on disk. So the counts are committed only after the
    destructive section has run.

    The assertion is paired deliberately: the audit number *and* the
    filesystem. A counter checked against another counter can agree
    while both are wrong, which is the whole defect this issue exists
    to close.
    """
    import shutil as shutil_mod

    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")

    def exploding_rmtree(*_args: object, **_kwargs: object) -> None:
        """Fail the first wipe, before anything has been removed."""
        msg = "simulated mid-purge OSError"
        raise OSError(msg)

    monkeypatch.setattr(shutil_mod, "rmtree", exploding_rmtree)

    with pytest.raises(OSError, match="simulated mid-purge"):
        PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert frag.exists(), "nothing was deleted, so the audit must claim nothing"
    outcome = _last_vault_outcome(vault)
    assert outcome.status == "partial"
    assert outcome.fragments_deleted == 0
    assert outcome.affected_fragments == []


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

    ``failure_reason`` is the exception **type name only** (#950). The
    audit log is deliberately preserved by every purge, so anything
    written into it outlives the right-to-be-forgotten request that
    produced it — and an exception message raised mid-wipe routinely
    quotes vault-derived text (a path, a title, a YAML snippet). The
    type is the whole forensic value; the message is the leak.
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
    assert outcome.failure_reason == "OSError"
    assert "simulated mid-purge" not in outcome.failure_reason


_SYNTHETIC_EXCEPTION_MARKER = "synthetic-vault-title-950"
"""Stands in for vault-derived text riding inside an exception message."""


def test_partial_outcome_audit_never_writes_exception_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed purge must not copy exception text into the audit log (#950).

    ``00-Creek-Meta/`` is deliberately preserved by every purge —
    including ``purge vault`` — so the audit log is the one file a
    right-to-be-forgotten request cannot reach. An exception raised
    mid-wipe carries a message the vault supplied (``OSError`` on a
    delete quotes the path; a YAML error quotes the offending source
    line), and today that message is interpolated straight into
    ``failure_reason``. The marker must therefore reach neither the
    parsed entry nor the raw bytes on disk.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_thread(vault, "thread-1", "Waves")

    def leaky_rmtree(*_args: object, **_kwargs: object) -> None:
        """Abort the wipe with vault-derived text in the message."""
        msg = _SYNTHETIC_EXCEPTION_MARKER
        raise OSError(msg)

    monkeypatch.setattr("creek.purge.engine.shutil.rmtree", leaky_rmtree)

    with pytest.raises(OSError, match=_SYNTHETIC_EXCEPTION_MARKER):
        PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    entries = _entries_for_run(vault)
    assert len(entries) == 2
    outcome = entries[1]
    assert outcome.status == "partial"
    assert outcome.failure_reason == "OSError"
    audit_text = (vault / _PURGE_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert _SYNTHETIC_EXCEPTION_MARKER not in audit_text


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
    # Exact, not ``>=``: a count that can drift upward is exactly how a
    # dry run came to report six scrubs for an apply that did one.
    assert result.provenance_scrubbed == 2


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
    # YAML list entry + one body mention in the draft.
    assert outcome.provenance_scrubbed == 2


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

    # The same 2 the apply twin of this fixture reports: a dry run that
    # cannot be trusted to predict its apply is not a preview.
    assert result.provenance_scrubbed == 2
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

    result = PurgeEngine(vault).purge_source("claude")

    text = derived.read_text(encoding="utf-8")
    assert "frag-A" not in text
    assert "frag-B" not in text
    # 2 YAML list entries + 2 body mentions. Use >= rather than == because
    # the exact count depends on whether frontmatter.dumps serialises the
    # list in flow ([a, b]) or block style — robust today, brittle if the
    # serialiser ever switches.
    assert text.count("[purged]") >= 4
    # The *substitution* count has no such dependency: the engine counts
    # what it replaced, whatever shape the serialiser wrote. Pin it
    # exactly, because the file-text assertion above cannot tell a
    # correct 4 from a double-counted 8.
    assert result.provenance_scrubbed == 4


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
    ``purge_vault``, still reaches such files: it wipes folder contents
    wholesale rather than matching each file against criteria. Since
    #1340 it does read every fragment's frontmatter, but only to name
    the ids in the audit record — a file it cannot parse is still
    deleted, and is still counted, merely unnamed.
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


# ---------------------------------------------------------------------------
# #1340 — a dry run must predict its own apply
#
# Two independent causes, both pinned here. (a) A counted-only deletion
# stays on disk in a dry run, so a later fragment's pass re-counts a file
# the apply run had already destroyed. (b) A counted-only *rewrite* stays
# unwritten, so a later pass reads bytes the apply run had already
# scrubbed — no deletion involved anywhere, which is why a fix that only
# tracks removed paths does not close it.
# ---------------------------------------------------------------------------

_RECONCILIATION_BODY_A = "Alpha body: the first shared passage."
"""Body of the first reconciliation fragment, quoted verbatim by the profile."""

_RECONCILIATION_BODY_B = "Bravo body: the second shared passage."
"""Body of the second reconciliation fragment, quoted verbatim by the profile."""

_RECONCILIATION_STUB_POINTER = f"{_INTIMATE_STUB_DIR}/shared.md"
"""One stub, pointed at by *both* fragments — the double-count in miniature."""

_POISONED_LEDGER_TEXT = "ledger lie: this text must never reach the filesystem"
"""What a poisoned dry-run ledger claims a file says, in the safety test.

If this string ever lands on disk, an apply run consulted the dry-run
bookkeeping — the one thing the ledger is forbidden to influence.
"""


def _build_reconciliation_vault(root: Path) -> Path:
    """Build the shared dry/apply fixture: every coupling at once (#1340).

    Each ingredient is a way one fragment's purge changes what the next
    fragment's purge can still find, which is exactly what a dry run
    fails to model when it deletes and rewrites nothing:

    - two ``claude`` fragments that reference each other **by id** (the
      provenance scrub) and **by title wikilink** (the wiki scrub);
    - a single intimate stub both fragments point at, so the second pass
      re-counts a stub the first already unlinked;
    - one ``Register-Samples`` copy per fragment, plus one shared
      ``casual-profile.md`` quoting both bodies and one shared
      ``Lexicon/glossary.md`` naming both ids — shared voice artifacts
      the first pass deletes and the second must not re-count.

    Args:
        root: Directory to create the vault under.

    Returns:
        The vault root path.
    """
    vault = _make_vault(root)
    stub_path = vault / _RECONCILIATION_STUB_POINTER
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="The shared intimate body.\n",
                type="intimate-stub",
                privacy_tier="intimate",
            ),
        ),
        encoding="utf-8",
    )
    pairs = (
        ("frag-A", "Alpha", _RECONCILIATION_BODY_A, "frag-B", "Bravo"),
        ("frag-B", "Bravo", _RECONCILIATION_BODY_B, "frag-A", "Alpha"),
    )
    for frag_id, title, body, other_id, other_title in pairs:
        target = vault / "01-Fragments" / "Conversations" / f"{title}.md"
        target.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    content=f"{body} See {other_id} and [[{other_title}]].\n",
                    id=frag_id,
                    title=title,
                    type="fragment",
                    source={
                        "platform": "claude",
                        "original_file": f"{frag_id}.json",
                    },
                    threads=[],
                    eddies=[],
                    saved_from={
                        "source_kind": "answer",
                        "intimate_body_pointer": _RECONCILIATION_STUB_POINTER,
                    },
                ),
            ),
            encoding="utf-8",
        )
        sample = vault / "07-Voice" / "Register-Samples" / "casual" / f"{frag_id}.md"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    profile = vault / "07-Voice" / "casual-profile.md"
    profile.write_text(
        "### Sample Passages\n\n"
        f"{_RECONCILIATION_BODY_A} See frag-B and [[Bravo]].\n\n"
        f"{_RECONCILIATION_BODY_B} See frag-A and [[Alpha]].\n",
        encoding="utf-8",
    )
    glossary = vault / "07-Voice" / "Lexicon" / "glossary.md"
    glossary.parent.mkdir(parents=True, exist_ok=True)
    glossary.write_text("- [[frag-A]] — a\n- [[frag-B]] — b\n", encoding="utf-8")
    return vault


def _copy_reconciliation_vaults(tmp_path: Path) -> tuple[Path, Path]:
    """Return byte-identical dry and apply copies of the shared fixture.

    ``copytree`` rather than building twice: identical *inputs* are the
    whole premise of the comparison, and a copy cannot drift from the
    original the way two constructions can.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Tuple of ``(dry_vault, apply_vault)`` roots.
    """
    source = _build_reconciliation_vault(tmp_path / "source")
    dry_vault = tmp_path / "dry" / "vault"
    apply_vault = tmp_path / "apply" / "vault"
    shutil.copytree(source, dry_vault)
    shutil.copytree(source, apply_vault)
    return dry_vault, apply_vault


def test_a_dry_source_purge_predicts_its_apply_exactly(tmp_path: Path) -> None:
    """Dry and apply results are the *same object*, field for field (#1340).

    Full-object equality rather than a hand-picked field list: the
    divergence has already appeared on four different counters
    (``intimate_stubs_removed``, ``provenance_scrubbed``,
    ``voice_artifacts_removed``, ``wikilinks_removed``), and a field
    list has to be remembered while a model dump does not.

    ``dry_run`` is excluded because it is the one field that *must*
    differ. ``deleted_files`` is excluded because it holds absolute
    paths under two different ``tmp_path`` roots, so it can never be
    equal as written — it is compared separately, root-relative, rather
    than dropped.
    """
    dry_vault, apply_vault = _copy_reconciliation_vaults(tmp_path)

    dry = PurgeEngine(dry_vault, dry_run=True).purge_source("claude")
    applied = PurgeEngine(apply_vault).purge_source("claude")

    ignored = {"dry_run", "deleted_files"}
    assert dry.model_dump(exclude=ignored) == applied.model_dump(exclude=ignored)
    assert _relative_deleted_files(dry, dry_vault) == _relative_deleted_files(
        applied,
        apply_vault,
    )


def test_the_provenance_scrub_count_is_the_same_dry_or_applied(
    tmp_path: Path,
) -> None:
    """The narrow unit behind the full-object comparison (#1340).

    Equality alone would be satisfied by two runs that are wrong in the
    same direction, so the agreed value is asserted too: two id mentions
    survive to be scrubbed once the shared voice artifacts have gone —
    one in the sibling fragment, one in its ``Register-Samples`` copy.
    """
    dry_vault, apply_vault = _copy_reconciliation_vaults(tmp_path)

    dry = PurgeEngine(dry_vault, dry_run=True).purge_source("claude")
    applied = PurgeEngine(apply_vault).purge_source("claude")

    assert applied.provenance_scrubbed == 2
    assert dry.provenance_scrubbed == applied.provenance_scrubbed


def test_a_dry_run_predicts_its_apply_when_nothing_is_deleted(
    tmp_path: Path,
) -> None:
    """The rewrite-driven divergence, with no deletion anywhere (#1340).

    Two fragments share the title ``Alpha``, and a praxis note that
    **survives both purges** carries two ``[[Alpha]]`` wikilinks. The
    apply run scrubs them on the first fragment's pass and finds none on
    the second; the dry run, having written nothing, finds the same two
    twice and reports four.

    This is the case a removed-paths-only ledger cannot close: ``p1.md``
    is never deleted in either mode, so nothing about it is ever
    "removed". Only remembering the *text* the apply run would have
    written makes the second pass agree.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    twin = vault / "01-Fragments" / "Conversations" / "Alpha-2.md"
    twin.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="A second note that shares the Alpha title.\n",
                id="frag-B",
                title="Alpha",
                type="fragment",
                source={"platform": "claude", "original_file": "frag-B.json"},
                threads=[],
                eddies=[],
            ),
        ),
        encoding="utf-8",
    )
    note = _write_derived_note(
        vault,
        subfolder="04-Praxis",
        name="p1",
        body="See [[Alpha]] and again [[Alpha]].",
        metadata={"type": "praxis"},
    )
    dry_engine = PurgeEngine(vault, dry_run=True)

    dry = dry_engine.purge_source("claude")
    applied = PurgeEngine(vault).purge_source("claude")

    assert applied.wikilinks_removed == 2
    assert dry.wikilinks_removed == applied.wikilinks_removed
    # The praxis note is collateral neither run may delete.
    assert note.exists()


def test_the_voice_sweep_matches_the_same_way_dry_or_applied(
    tmp_path: Path,
) -> None:
    """The voice content match reads disk in both modes, and must (#1340).

    Every other counted sweep consults the dry-run ledger so a preview
    does not re-count work an apply run had already done. The voice
    content match must **not**, and this fixture is why.

    ``casual-profile.md`` quotes only Bravo's body, so purging Alpha
    does not delete it — but Alpha's reference scrub does rewrite it,
    because the quoted passage carries an ``[[Alpha]]`` wikilink. That
    same scrub rewrites Bravo's own file. Needle and haystack therefore
    move together: disk-vs-disk agrees between the two modes, while
    overlaying only the haystack compares an unscrubbed needle against
    a scrubbed one and reports a deletion the apply run makes and the
    preview does not.

    Measured on this fixture with a haystack-only overlay in place:
    ``voice_artifacts_removed`` dry 0 against apply 1. This test is the
    reason that overlay is not in the tree; it fails if anyone adds one.
    """
    body_b = "Bravo body quoting [[Alpha]] inside it."
    source = _make_vault(tmp_path / "source")
    for frag_id, title, body in (
        ("frag-A", "Alpha", "Alpha body all its own."),
        ("frag-B", "Bravo", body_b),
    ):
        _write_fragment(source, frag_id, title, body=body, platform="claude")
    profile = source / "07-Voice" / "casual-profile.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(f"### Sample Passages\n{body_b}\n", encoding="utf-8")
    dry_vault = tmp_path / "dry" / "vault"
    apply_vault = tmp_path / "apply" / "vault"
    shutil.copytree(source, dry_vault)
    shutil.copytree(source, apply_vault)

    dry = PurgeEngine(dry_vault, dry_run=True).purge_source("claude")
    applied = PurgeEngine(apply_vault).purge_source("claude")

    assert applied.voice_artifacts_removed == 1
    assert dry.voice_artifacts_removed == applied.voice_artifacts_removed


def test_an_apply_run_ignores_the_dry_run_ledger_entirely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger that lies about everything cannot change an apply run (#1340).

    The safety property of the whole change: the dry-run bookkeeping is
    *populated* on every run but *consulted* only under ``dry_run``, so
    nothing it holds can alter what is deleted or rewritten for real.
    Arguing that from the diff is not proof; poisoning the ledger and
    watching the filesystem is.

    The poison is applied twice over. The engine's own ledger instance is
    told the fragment and the note are already gone and that the note's
    text is a bogus constant. Then every :class:`DryRunLedger` in the
    process is made to answer the same way, so a ledger the engine
    rebuilds part-way through a run is a liar too — the invariant is that
    *no* query reaches an apply run, not that one particular object is
    ignored.

    Every assertion is against the filesystem, because a counter and a
    file can drift apart and it is the file that has to be erased.
    """
    from creek.purge.dryrun import DryRunLedger

    vault = _make_vault(tmp_path)
    frag = _write_fragment(vault, "frag-A", "Alpha")
    note = _write_derived_note(
        vault,
        subfolder="04-Praxis",
        name="p1",
        body="See [[Alpha]] and frag-A.",
        metadata={"source_fragments": ["frag-A"]},
    )
    engine = PurgeEngine(vault)
    ledgers = [
        value for value in vars(engine).values() if isinstance(value, DryRunLedger)
    ]
    assert len(ledgers) == 1, "the engine must hold exactly one DryRunLedger"
    ledgers[0].mark_removed(frag)
    ledgers[0].mark_removed(note)
    ledgers[0].set_text(note, _POISONED_LEDGER_TEXT)
    monkeypatch.setattr(DryRunLedger, "is_removed", lambda _self, _path: True)
    monkeypatch.setattr(
        DryRunLedger,
        "text_for",
        lambda _self, _path: _POISONED_LEDGER_TEXT,
    )

    result = engine.purge_fragment("frag-A")

    assert not frag.exists()
    text = note.read_text(encoding="utf-8")
    assert _POISONED_LEDGER_TEXT not in text
    assert "frag-A" not in text
    assert "[[Alpha]]" not in text
    assert text.count("[purged]") == 2
    assert result.wikilinks_removed == 1
    assert result.provenance_scrubbed == 2


def test_a_second_operation_on_one_engine_starts_from_a_clean_ledger(
    tmp_path: Path,
) -> None:
    """Sequential purges on one engine share no dry-run state (#1340).

    The glossary is swept as a voice artifact by the *first* purge (it
    carries ``[[frag-A]]``) but not by the second (it names ``frag-B``
    only in prose). So a ledger left over from the first operation would
    hide the glossary from the second operation's scrub, and the second
    result would under-report by exactly one — a stale preview of a file
    that is still sitting on disk.

    A dry engine is used deliberately: in apply mode the glossary really
    is gone by the second call, so only the dry lane can tell a reset
    ledger from a leaked one.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-A", "Alpha")
    _write_fragment(vault, "frag-B", "Bravo")
    glossary = vault / "07-Voice" / "Lexicon" / "glossary.md"
    glossary.parent.mkdir(parents=True, exist_ok=True)
    glossary_text = "- [[frag-A]] — a\n- see also frag-B here\n"
    glossary.write_text(glossary_text, encoding="utf-8")
    engine = PurgeEngine(vault, dry_run=True)

    first = engine.purge_fragment("frag-A")
    second = engine.purge_fragment("frag-B")

    # The glossary is swept for frag-A, so its frag-A mention is not also
    # counted as a scrub — the apply run would have no file left to scrub.
    assert first.voice_artifacts_removed == 1
    assert first.provenance_scrubbed == 0
    # ...but the second operation must see the glossary exactly as the
    # filesystem still holds it.
    assert second.voice_artifacts_removed == 0
    assert second.provenance_scrubbed == 1
    assert glossary.read_text(encoding="utf-8") == glossary_text


# ---------------------------------------------------------------------------
# #1340 — the CLI's "Deleted files" table is bounded
# ---------------------------------------------------------------------------


def test_the_deleted_files_table_is_capped_and_says_how_many_it_hid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An over-long deletion prints a bounded table and an honest remainder.

    Counting files instead of folders turns a three-row table into one
    row per destroyed fragment, which is a scrollback flood at vault
    scale. The corpus is sized off the cap itself, so the test cannot
    pass by agreeing with a magic number it also chose — and the hidden
    rows have to be *reported*, because a table that silently truncates
    an erasure record is worse than a long one.
    """
    from creek.cli import _MAX_DELETED_FILE_ROWS, _render_purge_result

    hidden = 3
    paths = [f"/v/f{index}.md" for index in range(_MAX_DELETED_FILE_ROWS + hidden)]
    result = PurgeResult(
        operation="vault",
        target="entire vault",
        deleted_files=paths,
        fragments_affected=len(paths),
    )

    _render_purge_result(result)

    out = capsys.readouterr().out
    assert paths[0] in out
    assert paths[-1] not in out
    assert sum(1 for path in paths if path in out) == _MAX_DELETED_FILE_ROWS
    assert f"{hidden} more" in out
