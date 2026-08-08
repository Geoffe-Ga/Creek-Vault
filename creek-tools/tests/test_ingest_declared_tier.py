"""Out-of-band declared tier and ledger override on ``run_ingest`` (#1023).

Two seams the ``creek.upload`` MCP tool needs and the ingest pipeline did
not have.

**The tier channel.** A staged binary document (``.docx`` / ``.pdf`` /
``.xlsx``) cannot carry frontmatter, so the trick ``creek.journal`` uses
for text — writing ``privacy_tier`` into the staged markdown's own
frontmatter and letting ``MarkdownIngestor._merge_frontmatter`` pick it
up — is unavailable. ``grep -rn privacy_tier creek/ingest/`` returns zero
hits: no ingestor emits the key, so every non-markdown fragment lands at
:attr:`~creek.models.PrivacyTier.UNCLASSIFIED`.
:func:`~creek.ingest.pipeline.stamp_declared_tier` is the one out-of-band
channel, and it merges with
:func:`creek.classify.privacy_pass.escalate` rather than assigning, so a
source that already declares a *higher* tier can never be lowered by the
caller's declaration.

**The ledger channel.** ``ledger_for_source`` hard-codes ``if source_type
!= "markdown": return None``, so ``attach_origin_key`` returns early and
a staged ``.pdf`` gets no ``source.origin_key`` — which is exactly the
field the RTBF purge sweep keys on, so the sweep would silently no-op.
:func:`~creek.ingest.pipeline.resolve_ledger` lets a caller that owns a
stable staging path opt a non-markdown source type into ledger-backed
identity without changing what ``creek ingest --type document`` does on
the CLI; ``ledger_for_source`` itself is deliberately untouched.

The end-to-end case uses ``DocumentIngestor`` over a ``.txt``: it is the
cheapest supported document format with no optional binary dependency
(no tesseract, no openpyxl), so the test exercises the real pipeline
without becoming environment-dependent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter

from creek.ingest.documents import DocumentIngestor
from creek.ingest.journal_staging import UPLOAD_LEDGER_SOURCE
from creek.ingest.ledger import SourceLedger
from creek.ingest.pipeline import resolve_ledger, run_ingest, stamp_declared_tier
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path

_DOCUMENT_BODY = "An uploaded document body.\n"


# ---- Helpers ----


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault :class:`VaultWriter` will accept."""
    vault = tmp_path / "vault"
    for rel in ("00-Creek-Meta", "01-Fragments/Unsorted"):
        (vault / rel).mkdir(parents=True, exist_ok=True)
    return vault


def _fragment(tier: PrivacyTier) -> Fragment:
    """Build a bare fragment already sitting at *tier*."""
    return Fragment(
        id="frag-declared01",
        title="Declared",
        source=FragmentSource(platform=SourcePlatform.OTHER),
        privacy_tier=tier,
    )


def _fragment_files(vault: Path) -> list[Path]:
    """Return every fragment markdown file written under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


# ---- stamp_declared_tier ----


class TestStampDeclaredTier:
    """The only out-of-band tier channel into a binary fragment (#1023)."""

    def test_stamp_declared_tier_lifts_an_unclassified_tier(self) -> None:
        """A declared tier supersedes the ingestors' ``unclassified`` default.

        Pins ``_ESCALATION_RANK[UNCLASSIFIED] == -1``: ``unclassified`` is
        the absence of a decision, so *any* real declared tier — even the
        least restrictive ``open`` — outranks it.
        """
        fragment = _fragment(PrivacyTier.UNCLASSIFIED)

        stamp_declared_tier(fragment, PrivacyTier.OPEN)

        assert fragment.privacy_tier == PrivacyTier.OPEN

    def test_stamp_declared_tier_never_lowers_an_existing_tier(self) -> None:
        """An already-intimate fragment is not downgraded to a declared ``open``.

        Proves the merge is :func:`escalate`, not assignment: an uploaded
        ``.md`` whose own frontmatter says ``privacy_tier: intimate`` must
        survive a caller that passed ``tier="open"``.
        """
        fragment = _fragment(PrivacyTier.INTIMATE)

        stamp_declared_tier(fragment, PrivacyTier.OPEN)

        assert fragment.privacy_tier == PrivacyTier.INTIMATE

    def test_stamp_declared_tier_with_no_declaration_is_a_no_op(self) -> None:
        """``None`` means "the caller declared nothing" — leave the tier alone."""
        fragment = _fragment(PrivacyTier.PERSONAL)

        stamp_declared_tier(fragment, None)

        assert fragment.privacy_tier == PrivacyTier.PERSONAL


# ---- resolve_ledger ----


class TestResolveLedger:
    """An explicit override ledgers a source type the CLI leaves unledgered."""

    def test_resolve_ledger_override_ledgers_a_non_markdown_source(
        self,
        tmp_path: Path,
    ) -> None:
        """``document`` is unledgered by default and ledgered on override.

        The ``None`` case preserves ``creek ingest --type document``
        semantics exactly; the override case must land on the dedicated
        ``00-Creek-Meta/State/ingest/upload.jsonl`` that
        :meth:`SourceLedger.path_for` names, because relocating it would
        orphan every staged upload.
        """
        vault = _make_vault(tmp_path)

        assert resolve_ledger("document", vault, None) is None

        ledger = resolve_ledger("document", vault, UPLOAD_LEDGER_SOURCE)

        assert ledger is not None
        ledger.record("00-Creek-Meta/adepthood/uploads/note.txt", "frag-x", "hash")
        assert SourceLedger.path_for(vault, UPLOAD_LEDGER_SOURCE).is_file()
        assert (
            SourceLedger.path_for(vault, UPLOAD_LEDGER_SOURCE)
            == vault / "00-Creek-Meta" / "State" / "ingest" / "upload.jsonl"
        )


# ---- run_ingest end-to-end ----


class TestRunIngestDeclaredTier:
    """Both seams together, through the real pipeline, onto disk."""

    def test_run_ingest_stamps_the_declared_tier_and_the_origin_key(
        self,
        tmp_path: Path,
    ) -> None:
        """A ledgered document ingest writes the tier AND the origin key.

        Without ``stamp_declared_tier`` the frontmatter would read
        ``privacy_tier: unclassified``; without the ``ledger_source``
        override ``attach_origin_key`` returns early and
        ``source.origin_key`` stays ``None`` — which is precisely what
        would make the RTBF purge sweep no-op on an uploaded document.
        """
        vault = _make_vault(tmp_path)
        staged = vault / "00-Creek-Meta" / "adepthood" / "uploads" / "note.txt"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(_DOCUMENT_BODY, encoding="utf-8")

        result = run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=staged,
            vault_path=vault,
            ledger_source=UPLOAD_LEDGER_SOURCE,
            privacy_tier=PrivacyTier.PERSONAL,
        )

        assert result.errors == []
        assert result.written == 1
        written = _fragment_files(vault)
        assert len(written) == 1
        post = frontmatter.load(str(written[0]))
        assert post["privacy_tier"] == PrivacyTier.PERSONAL.value
        source = post["source"]
        assert isinstance(source, dict)
        assert source["origin_key"] == "00-Creek-Meta/adepthood/uploads/note.txt"
