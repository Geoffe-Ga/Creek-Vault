"""No-migration guards for ingest source identity (#953).

**Every test in this file is expected GREEN at HEAD and must stay GREEN after
the #953/#1363/#1367/#1467 change lands.** That is the whole point of the
file: it is not a red-first specification, it is the tripwire that proves the
change did not re-mint the corpus.

A fragment's identity is ledger-backed, keyed by
:func:`creek.ingest.pipeline.derive_source_key`. Every markdown fragment in
every already-synced vault carries a key that function produced. Change how
the *in-vault* branch spells a key and the next ingest fails to match anything
already on disk, mints fresh ids, and leaves the predecessors behind as
orphans — the #1304/#1329/#1384 scar, three times over. So the in-vault branch
of ``derive_source_key`` must stay byte-identical, and the way that is enforced
is by pinning its exact output as a string literal here.

The literals below were **measured** against HEAD, not guessed:

    derive_source_key("<vault>/00-Inbox/2024-01-05.md", vault)
        == "00-Inbox/2024-01-05.md"
    derive_source_key("00-Inbox/2024-01-05.md", vault)      # relative record
        == "00-Inbox/2024-01-05.md"

Any prefix, digest, normalisation or reordering added to the in-vault arm kills
these tests. Only the out-of-vault ``except ValueError`` arm may move, and its
new behaviour is specified red-first in
``tests/test_ingest_out_of_vault_identity.py``.

The two out-of-vault *migration* guards at the bottom are green at HEAD for a
different reason — at HEAD the legacy key IS the current key, so there is
nothing to adopt and nothing to mis-tomb. They exist to catch the regression
the fix itself could introduce: an adopted legacy record whose stale key is
left in ``live_keys()`` and is then read as a vanished unit, soft-tombing every
journal fragment in the vault in one pass. Their non-vacuousness is proven by
mutation *after* the fix (drop the legacy key from ``seen_keys`` and they go
red), which is recorded in the PR.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.config import OCRConfig
from creek.ingest import INGESTOR_REGISTRY, assemble_ingested_fragment
from creek.ingest.documents import DocumentIngestor
from creek.ingest.images import OCR_ENGINES, OcrResult
from creek.ingest.ledger import SourceLedger
from creek.ingest.pipeline import IngestRunResult, derive_source_key, run_ingest
from creek.vault.writer import VaultWriter
from tests.scanned_pdf_support import SCAN_PAGES, scanned_pdf

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# The exact key an in-vault markdown source has been getting since #672. This
# literal is the migration guard; it may not be "updated to match" a change.
_PINNED_IN_VAULT_KEY = "00-Inbox/2024-01-05.md"
_PINNED_IN_VAULT_RELPATH = "00-Inbox/2024-01-05.md"
_PINNED_NESTED_KEY = "01-Fragments/Unsorted/sub/dir/note.md"

# A directory name that is *literally* a 16-char hex digest, sitting inside the
# vault under the prefix the out-of-vault scheme reserves. The in-vault branch
# must still win for it, because it is reached first and is not being edited.
_LOOKALIKE_DIGEST_DIR = "external/0123456789abcdef"

# The id a pre-existing vault already holds on disk. It is seeded, not derived,
# precisely so that it can be pinned as a literal: the derivation hashes the
# absolute source path, which is a tmp path and cannot be pinned.
_PREEXISTING_ID = "frag-preexisting01"

_ORIGINAL_SOURCE = "---\ndate: 2024-01-05\n---\n\n# Morning\n\nOriginal body.\n"
_EDITED_SOURCE = (
    "---\ndate: 2024-01-05\n---\n\n# Morning\n\nEdited body, MARKER-EDIT.\n"
)
_PINNED_MTIME = datetime(2024, 1, 5, 8, 30, tzinfo=UTC)


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree ``VaultWriter`` and the ledger need."""
    vault = tmp_path / "vault"
    for relative in (
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/State/ingest",
        "00-Inbox",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "01-Fragments/Unsorted",
        "10-Liminal/Orphaned",
    ):
        (vault / relative).mkdir(parents=True, exist_ok=True)
    return vault


def _pin_mtime(path: Path) -> None:
    """Pin *path*'s mtime so the ingestor's timestamp is deterministic."""
    epoch = _PINNED_MTIME.timestamp()
    os.utime(path, (epoch, epoch))


def _live_fragments(vault: Path) -> list[Path]:
    """Return every live fragment file under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _orphans(vault: Path) -> list[Path]:
    """Return every soft-tombed fragment file under ``10-Liminal/Orphaned``."""
    return sorted((vault / "10-Liminal" / "Orphaned").rglob("*.md"))


def _raw_ledger_keys(vault: Path, source: str) -> list[str]:
    """Read ``source_key`` off the ledger's raw JSONL lines, in file order.

    Deliberately *not* via :class:`SourceLedger`: a reader that normalises or
    repairs what it loads would hide exactly the drift this file exists to
    catch. The bytes on disk are the assertion.
    """
    path = vault / "00-Creek-Meta" / "State" / "ingest" / f"{source}.jsonl"
    if not path.exists():
        return []
    return [
        str(json.loads(line)["source_key"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _seed_preexisting_fragment(
    vault: Path,
    source_file: Path,
    *,
    source_key: str,
    fragment_id: str,
) -> None:
    """Seed the vault state a pre-#953 ingest of *source_file* left behind.

    The fragment is assembled by the real ingestor and only then has its id
    overwritten, so it lands in exactly the directory a re-ingest will look in
    — the routing is the ingestor's, not this test's guess at it.
    """
    ingested = INGESTOR_REGISTRY["markdown"]().ingest(source_file)
    assembled = assemble_ingested_fragment(ingested.fragments[0])
    assembled.fragment.id = fragment_id
    assembled.fragment.source.origin_key = source_key
    VaultWriter(vault_path=vault).write_fragment(
        assembled.fragment, body=assembled.body
    )
    ledger = SourceLedger.load(vault, source="markdown")
    # A hash that is deliberately not the current content's, so the re-ingest
    # takes the *changed* branch and has to prove the id survives a rewrite.
    ledger.record(source_key, fragment_id, "0" * 64)


# ---- The in-vault branch, pinned character-for-character ----------------


def test_an_absolute_in_vault_source_keys_as_its_exact_relative_posix_path(
    tmp_path: Path,
) -> None:
    """An in-vault source keys as its vault-relative POSIX path, verbatim."""
    vault = _make_vault(tmp_path)
    source = vault / "00-Inbox" / "2024-01-05.md"
    source.write_text(_ORIGINAL_SOURCE, encoding="utf-8")

    assert derive_source_key(str(source), vault) == _PINNED_IN_VAULT_KEY


def test_a_relative_in_vault_record_keys_identically_to_the_absolute_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded spelling does not change the key (#1329 anchoring).

    Run from a foreign working directory so the relative record can only
    resolve by being anchored to the vault, which is the behaviour
    ``resolve_recorded_source`` promises and which the key must not lose.
    """
    vault = _make_vault(tmp_path)
    source = vault / "00-Inbox" / "2024-01-05.md"
    source.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    assert derive_source_key(_PINNED_IN_VAULT_RELPATH, vault) == _PINNED_IN_VAULT_KEY


def test_a_nested_in_vault_source_keys_as_its_full_relative_path(
    tmp_path: Path,
) -> None:
    """Nesting is preserved in full; no truncation to a basename."""
    vault = _make_vault(tmp_path)
    nested = vault / "01-Fragments" / "Unsorted" / "sub" / "dir"
    nested.mkdir(parents=True)
    source = nested / "note.md"
    source.write_text("body\n", encoding="utf-8")

    assert derive_source_key(str(source), vault) == _PINNED_NESTED_KEY


def test_the_reserved_prefix_does_not_shadow_a_real_in_vault_directory(
    tmp_path: Path,
) -> None:
    """A vault directory that *looks* like an out-of-vault key still keys plainly.

    The out-of-vault scheme reserves a prefix; an operator could hand-create a
    directory with that shape inside the vault. The in-vault arm is tried
    first and is not being edited, so it must keep winning here.
    """
    vault = _make_vault(tmp_path)
    lookalike = vault / _LOOKALIKE_DIGEST_DIR
    lookalike.mkdir(parents=True)
    source = lookalike / "x.md"
    source.write_text("body\n", encoding="utf-8")

    assert derive_source_key(str(source), vault) == f"{_LOOKALIKE_DIGEST_DIR}/x.md"


# ---- The corpus does not migrate: exact key AND exact id ----------------


def test_an_existing_in_vault_markdown_keeps_its_exact_key_and_its_exact_id(
    tmp_path: Path,
) -> None:
    """A pre-existing ledgered in-vault fragment survives a re-ingest intact.

    This is the guard the whole change is measured against. A vault seeded as
    a pre-#953 ingest left it — one fragment on disk under
    ``frag-preexisting01``, one ledger record keyed
    ``00-Inbox/2024-01-05.md`` — is re-ingested after its source is edited.

    Three things must hold, and each is a distinct failure mode:

    * the ledger writes back the **same key string**, so no second record is
      minted under a migrated spelling;
    * the run reports the **same fragment id**, so nothing is re-minted;
    * exactly **one** fragment exists afterwards, holding the new body, so the
      edit updated in place rather than orphaning its predecessor.
    """
    vault = _make_vault(tmp_path)
    source = vault / "00-Inbox" / "2024-01-05.md"
    source.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    _pin_mtime(source)
    _seed_preexisting_fragment(
        vault,
        source,
        source_key=_PINNED_IN_VAULT_KEY,
        fragment_id=_PREEXISTING_ID,
    )
    assert len(_live_fragments(vault)) == 1

    source.write_text(_EDITED_SOURCE, encoding="utf-8")
    _pin_mtime(source)
    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=vault / "00-Inbox",
        vault_path=vault,
    )

    assert result.errors == []
    assert set(_raw_ledger_keys(vault, "markdown")) == {_PINNED_IN_VAULT_KEY}, (
        "the in-vault source key migrated. Every already-synced vault keys its "
        "markdown fragments this way; a new spelling orphans all of them."
    )
    assert result.fragment_ids == [_PREEXISTING_ID], (
        f"the fragment id was re-minted: {result.fragment_ids} != "
        f"[{_PREEXISTING_ID!r}]. The ledger is the authority on identity."
    )
    assert (result.created, result.updated) == (0, 1)
    live = _live_fragments(vault)
    assert len(live) == 1, f"the edit duplicated the corpus: {live}"
    assert "MARKER-EDIT" in live[0].read_text(encoding="utf-8")
    assert frontmatter.load(live[0])["id"] == _PREEXISTING_ID


# ---- The migration must not destroy an out-of-vault journal vault -------


def test_a_present_out_of_vault_source_is_never_tombed_by_its_legacy_key(
    tmp_path: Path,
) -> None:
    """A directory pass whose files are all present tombs nothing.

    ``creek sync`` ingests ``source_drive/personal/journal/`` as source_type
    ``markdown`` — a *directory* input, so tombing is armed — and every record
    it has ever written is keyed by the out-of-vault spelling. Once that
    spelling changes, the stale legacy key stays in ``live_keys()``; if the run
    does not also mark it seen, every journal fragment in the vault is
    soft-tombed in a single tick.

    Green at HEAD (the legacy key *is* the current key). It goes red under a
    fix that adopts the legacy record without widening ``seen_keys``.
    """
    vault = _make_vault(tmp_path)
    journal = tmp_path / "drive" / "personal" / "journal"
    journal.mkdir(parents=True)
    source = journal / "2024-01-05.md"
    source.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    _pin_mtime(source)
    _seed_preexisting_fragment(
        vault, source, source_key="2024-01-05.md", fragment_id=_PREEXISTING_ID
    )

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=journal,
        vault_path=vault,
    )

    assert result.errors == []
    assert result.tombed == 0, (
        "a source file that is still on disk was soft-tombed. Its legacy "
        "ledger key was read as a vanished unit."
    )
    assert _orphans(vault) == []
    assert len(_live_fragments(vault)) == 1


def test_a_genuinely_deleted_out_of_vault_unit_tombs_exactly_once(
    tmp_path: Path,
) -> None:
    """Deletion still tombs, and the count is not doubled by a second key.

    The other half of the guard above: widening ``seen_keys`` must not
    *disarm* tombing, and adopting a legacy record must not make one vanished
    fragment count as two relocations. The number is printed to the operator,
    so it may only ever name work that happened.
    """
    vault = _make_vault(tmp_path)
    journal = tmp_path / "drive" / "personal" / "journal"
    journal.mkdir(parents=True)
    gone = journal / "2024-01-05.md"
    gone.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    _pin_mtime(gone)
    _seed_preexisting_fragment(
        vault, gone, source_key="2024-01-05.md", fragment_id=_PREEXISTING_ID
    )
    gone.unlink()
    survivor = journal / "2024-02-01.md"
    survivor.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    _pin_mtime(survivor)

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=journal,
        vault_path=vault,
    )

    assert result.errors == []
    assert result.tombed == 1, (
        f"expected exactly one relocation, got {result.tombed}. A legacy key "
        "and its successor must not tomb the same fragment twice."
    )
    assert len(_orphans(vault)) == 1


# ---- The scanned-PDF route's sub-unit identity (#1639) -----------------


class _PageOcrEngine:
    """Stub OCR backend returning one canned page per :data:`SCAN_PAGES`.

    Registered through the production ``OCR_ENGINES`` registry rather than
    injected past it, so these tests exercise the same resolution path
    ``ocr.engine`` takes in a real vault.
    """

    def __init__(self, language: str = "eng") -> None:
        """Accept the language string the production factory passes.

        Args:
            language: Tesseract language code(s), already joined.
        """
        self.language = language

    def is_available(self) -> bool:
        """Report availability; the stub needs no system binary."""
        return True

    def extract_text(self, image_path: Path) -> OcrResult:
        """Unused on this route; present to satisfy the protocol.

        Args:
            image_path: Image the ingestor asked about.

        Returns:
            An empty result.
        """
        return OcrResult(text="", confidence=0.0)

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Return one canned result per page.

        Args:
            pdf_path: PDF the ingestor asked about.

        Returns:
            One :class:`OcrResult` per entry in :data:`SCAN_PAGES`.
        """
        return [
            OcrResult(
                text=text,
                confidence=0.9,
                page=number,
                image_type="scanned_pdf_page",
            )
            for number, text in enumerate(SCAN_PAGES, start=1)
        ]


def _make_scan_source(tmp_path: Path) -> Path:
    """Write a pinned-mtime three-page image-only PDF into a source dir."""
    source = tmp_path / "scans"
    source.mkdir(parents=True, exist_ok=True)
    pdf = scanned_pdf(source / "Scan.pdf", 3)
    _pin_mtime(pdf)
    return source


def _ingest_scan(source: Path, vault: Path) -> IngestRunResult:
    """Run the document ingest over *source* with the stub OCR engine wired."""
    return run_ingest(
        ingestor_cls=DocumentIngestor,
        source_type="document",
        input_path=source,
        vault_path=vault,
        ocr=OCRConfig(enabled=True, engine="pageocr"),
    )


def _origin_keys(vault: Path) -> list[str]:
    """Read ``source.origin_key`` off each written fragment's bytes."""
    return sorted(
        str(frontmatter.loads(path.read_text(encoding="utf-8"))["source"]["origin_key"])
        for path in _live_fragments(vault)
    )


def test_each_ocrd_pdf_page_gets_its_own_ledger_key_and_its_own_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed scan must not collapse three pages into one fragment.

    ``document`` is in ``LEDGERED_SOURCES`` **because**
    ``DocumentIngestor.parse`` emitted exactly one fragment per file. The
    #1639 OCR route breaks that invariant, and without the ``source_unit``
    ``ingest_pdf`` mints, every page of one PDF derives the same
    ``source.origin_key``, resolves to the same ledger record, and
    ``write_fragment_idempotent`` reassigns each page's id to that record's —
    overwriting its predecessor while reporting ``updated``.

    Measured with the discriminator removed: **one** surviving file holding
    page 3's body under page 1's filename, and the run reporting success.
    That is silent data loss, so this assertion is the point of the change
    rather than a detail of it.

    Note the ``set(...)``: the raw ledger JSONL is append-only and keeps three
    lines under the collapsing mutation, all with the *same* key, so a bare
    ``len(_raw_ledger_keys(...)) == 3`` reads ``3 == 3`` and passes against
    exactly the defect it is billed to catch.
    """
    monkeypatch.setitem(OCR_ENGINES, "pageocr", _PageOcrEngine)
    vault = _make_vault(tmp_path)
    source = _make_scan_source(tmp_path)

    _ingest_scan(source, vault)

    keys = set(_raw_ledger_keys(vault, "document"))
    assert len(keys) == 3
    assert sorted(key.rsplit("#", 1)[-1] for key in keys) == [
        "page-1",
        "page-2",
        "page-3",
    ]
    assert len(_live_fragments(vault)) == 3
    assert len(set(_origin_keys(vault))) == 3
    # Every page keys off the *same* file: the discriminator rides
    # ``source_unit``, never ``source_path``, because ``routing.arbitrate``
    # groups fragments on that exact string to decide which ingestor owns a
    # file. A ``path#page=N`` source path would split one PDF across three
    # imagined files.
    assert {key.rsplit("#", 1)[0] for key in keys} == {
        derive_source_key(str(source / "Scan.pdf"), vault)
    }


def test_re_ingesting_the_same_scan_updates_nothing_and_orphans_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second run of an unchanged scan is a no-op, page by page.

    The identity half of the fix is only proven by a second pass: minting
    three distinct keys once is compatible with minting three *new* ones next
    time, which would duplicate the corpus on every ingest.
    """
    monkeypatch.setitem(OCR_ENGINES, "pageocr", _PageOcrEngine)
    vault = _make_vault(tmp_path)
    source = _make_scan_source(tmp_path)

    first = _ingest_scan(source, vault)
    assert first.created == 3
    ids_after_first = sorted(
        str(frontmatter.loads(path.read_text(encoding="utf-8"))["id"])
        for path in _live_fragments(vault)
    )

    second = _ingest_scan(source, vault)

    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == 3
    assert _orphans(vault) == []
    assert len(_live_fragments(vault)) == 3
    assert (
        sorted(
            str(frontmatter.loads(path.read_text(encoding="utf-8"))["id"])
            for path in _live_fragments(vault)
        )
        == ids_after_first
    )
