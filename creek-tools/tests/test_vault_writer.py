"""Tests for the vault writer module.

Verifies that VaultWriter correctly writes Creek ontological primitives
(Fragment, Thread, Eddy, Praxis, Decision) as markdown files with YAML
frontmatter to the appropriate vault directories, handles duplicate
detection, provenance logging, filename sanitization, and dispatching
via write_any.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
from pydantic import BaseModel

from creek._fslock import VaultLockTimeoutError, vault_lock
from creek.models import (
    Decision,
    DecisionStatus,
    Eddy,
    Fragment,
    FragmentSource,
    Praxis,
    PraxisStatus,
    PraxisType,
    PrivacyTier,
    SourcePlatform,
    Thread,
    ThreadStatus,
)
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS
from creek.vault.writer import INDEX_FILENAME, INDEX_LOCK_FILENAME, VaultWriter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from conftest import ShortWriteController


_POLL_SECONDS: Final[float] = 0.02
"""Granularity of the overlap test's wait-for-an-effect loop."""

_WEDGE_TIMEOUT_SECONDS: Final[float] = 5.0
"""Ceiling on any wait in the overlap test, so a regression fails instead of hanging."""

_OVERLAP_CEILING_SECONDS: Final[float] = 2.0
"""Longest the overlap test waits for an unserialised second caller to act.

Not a sleep. The wait ends the instant the second caller produces its
side effect, so the unserialised case — the one that must go red — finishes
in milliseconds. Only the serialised case pays the ceiling, and it pays it
because there is nothing to see: the second caller is blocked on the lock.
A fixed sleep would have had the opposite bias, passing vacuously on a
loaded CI box where the second caller had simply not finished yet.
"""

_NO_STALL_CEILING_SECONDS: Final[float] = 5.0
"""Longest the anti-inversion test lets a lookup and a write take together.

Well under :data:`creek._fslock.DEFAULT_LOCK_TIMEOUT_SECONDS` (10 s) on
purpose. The naive answer to #1621 — take ``vault_lock`` *inside*
``self._lock`` — does not hang: it inverts the pair, burns the full
lock timeout and then raises ``VaultLockTimeoutError`` out of a
read-only lookup. A test that only waited for both callers to *finish*
would pass on that implementation, so the assertion has to be a clock
bound between the two.
"""


# ---- Fixtures ----


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal vault structure under tmp_path for testing."""
    dirs = [
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
        "01-Fragments/Messages",
        "01-Fragments/Writing",
        "01-Fragments/Journal",
        "01-Fragments/Technical",
        "01-Fragments/Notes",
        "01-Fragments/Documents",
        "01-Fragments/Data",
        "01-Fragments/Decks",
        "01-Fragments/Images",
        "01-Fragments/Unsorted",
        "02-Threads/Active",
        "02-Threads/Dormant",
        "02-Threads/Resolved",
        "03-Eddies",
        "04-Praxis/Daily",
        "04-Praxis/Seasonal",
        "04-Praxis/Situational",
        "08-Decisions/Active",
        "08-Decisions/Archive",
        "08-Decisions/Frameworks",
    ]
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def writer(vault_path: Path) -> VaultWriter:
    """Return a VaultWriter configured for the test vault."""
    return VaultWriter(vault_path=vault_path)


@pytest.fixture()
def sample_fragment() -> Fragment:
    """Return a sample Fragment for testing."""
    return Fragment(
        id="frag-test0001",
        title="Test Conversation Fragment",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=datetime(2025, 1, 15, 10, 30, 0),
    )


@pytest.fixture()
def sample_thread() -> Thread:
    """Return a sample Thread for testing."""
    return Thread(
        id="thread-test001",
        title="Test Active Thread",
        status=ThreadStatus.ACTIVE,
        first_seen=date(2025, 1, 10),
        last_seen=date(2025, 1, 15),
    )


@pytest.fixture()
def sample_eddy() -> Eddy:
    """Return a sample Eddy for testing."""
    return Eddy(
        id="eddy-test0001",
        title="Test Eddy Cluster",
        formed=date(2025, 1, 12),
        fragment_count=5,
        threads=["thread-a", "thread-b"],
    )


@pytest.fixture()
def sample_praxis() -> Praxis:
    """Return a sample Praxis for testing."""
    return Praxis(
        id="praxis-test01",
        title="Test Praxis Habit",
        praxis_type=PraxisType.HABIT,
        status=PraxisStatus.ACTIVE,
    )


@pytest.fixture()
def sample_decision() -> Decision:
    """Return a sample Decision for testing."""
    return Decision(
        id="decision-test",
        title="Test Decision",
        status=DecisionStatus.SENSING,
        opened=date(2025, 1, 14),
    )


# ---- VaultWriter Init ----


class TestVaultWriterInit:
    """Tests for VaultWriter initialization and vault validation."""

    def test_init_valid_vault(self, vault_path: Path) -> None:
        """VaultWriter accepts a valid vault path."""
        w = VaultWriter(vault_path=vault_path)
        assert w.vault_path == vault_path

    def test_init_nonexistent_path(self, tmp_path: Path) -> None:
        """VaultWriter raises FileNotFoundError for nonexistent path."""
        bad_path = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError, match="Vault path does not exist"):
            VaultWriter(vault_path=bad_path)

    def test_init_missing_fragments_dir(self, tmp_path: Path) -> None:
        """VaultWriter raises FileNotFoundError when 01-Fragments/ is missing."""
        # Create a vault-like dir but omit 01-Fragments
        (tmp_path / "00-Creek-Meta/Processing-Log").mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="01-Fragments"):
            VaultWriter(vault_path=tmp_path)

    def test_init_missing_meta_dir(self, tmp_path: Path) -> None:
        """VaultWriter raises FileNotFoundError when 00-Creek-Meta/ is missing."""
        (tmp_path / "01-Fragments/Unsorted").mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="00-Creek-Meta"):
            VaultWriter(vault_path=tmp_path)


# ---- write_fragment ----


class TestWriteFragment:
    """Tests for writing Fragment models to the vault."""

    def test_write_fragment_creates_file(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """write_fragment creates a markdown file in the correct subfolder."""
        result = writer.write_fragment(sample_fragment)
        assert result.exists()
        assert result.suffix == ".md"
        assert "01-Fragments/Conversations" in str(result)

    def test_write_fragment_content_has_frontmatter(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """Written file contains YAML frontmatter with correct fields."""
        result = writer.write_fragment(sample_fragment)
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "id: frag-test0001" in content
        assert "title: Test Conversation Fragment" in content
        assert "type: fragment" in content

    def test_write_fragment_persists_body(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """Provided body text is written below the frontmatter block."""
        import frontmatter as fm_mod

        body = "# A Note\n\nReal body content goes here.\n"
        result = writer.write_fragment(sample_fragment, body=body)
        post = fm_mod.load(str(result))
        assert "Real body content goes here." in post.content
        assert post["id"] == "frag-test0001"

    def test_write_fragment_body_round_trips_through_pydantic(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """Frontmatter survives round-trip back into a Fragment model."""
        import frontmatter as fm_mod

        body = "Body that must survive serialisation."
        result = writer.write_fragment(sample_fragment, body=body)
        post = fm_mod.load(str(result))
        round_tripped = Fragment.model_validate(dict(post.metadata))
        assert round_tripped.id == sample_fragment.id
        assert round_tripped.title == sample_fragment.title
        assert body.strip() in post.content.strip()
        assert round_tripped.type == "fragment"

    def test_write_fragment_chatgpt_goes_to_conversations(
        self,
        writer: VaultWriter,
    ) -> None:
        """ChatGPT fragments go to the Conversations subfolder."""
        frag = Fragment(
            id="frag-chatgpt01",
            title="ChatGPT Talk",
            source=FragmentSource(platform=SourcePlatform.CHATGPT),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Conversations" in str(result)

    def test_write_fragment_discord_goes_to_messages(
        self,
        writer: VaultWriter,
    ) -> None:
        """Discord fragments go to the Messages subfolder."""
        frag = Fragment(
            id="frag-discord01",
            title="Discord Message",
            source=FragmentSource(platform=SourcePlatform.DISCORD),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Messages" in str(result)

    def test_write_fragment_essay_goes_to_writing(
        self,
        writer: VaultWriter,
    ) -> None:
        """Essay fragments go to the Writing subfolder."""
        frag = Fragment(
            id="frag-essay0001",
            title="Essay Piece",
            source=FragmentSource(platform=SourcePlatform.ESSAY),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Writing" in str(result)

    def test_write_fragment_journal_goes_to_journal(
        self,
        writer: VaultWriter,
    ) -> None:
        """Journal fragments go to the Journal subfolder."""
        frag = Fragment(
            id="frag-journal01",
            title="Journal Entry",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Journal" in str(result)

    def test_write_fragment_code_goes_to_technical(
        self,
        writer: VaultWriter,
    ) -> None:
        """Code fragments go to the Technical subfolder."""
        frag = Fragment(
            id="frag-code0001",
            title="Code Snippet",
            source=FragmentSource(platform=SourcePlatform.CODE),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Technical" in str(result)

    def test_write_fragment_other_goes_to_unsorted(
        self,
        writer: VaultWriter,
    ) -> None:
        """Other/unknown platform fragments go to the Unsorted subfolder."""
        frag = Fragment(
            id="frag-other001",
            title="Misc Fragment",
            source=FragmentSource(platform=SourcePlatform.OTHER),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Unsorted" in str(result)

    def test_write_fragment_email_goes_to_messages(
        self,
        writer: VaultWriter,
    ) -> None:
        """Email fragments share the Messages subfolder with chat platforms."""
        frag = Fragment(
            id="frag-email001",
            title="Email Fragment",
            source=FragmentSource(platform=SourcePlatform.EMAIL),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Messages" in str(result)

    def test_write_fragment_image_ocr_goes_to_images(
        self,
        writer: VaultWriter,
    ) -> None:
        """OCR'd image fragments land in the Images subfolder."""
        frag = Fragment(
            id="frag-ocr00001",
            title="OCR Fragment",
            source=FragmentSource(platform=SourcePlatform.IMAGE_OCR),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Images" in str(result)

    def test_write_fragment_document_goes_to_documents(
        self,
        writer: VaultWriter,
    ) -> None:
        """Document fragments land in the Documents subfolder."""
        frag = Fragment(
            id="frag-doc00001",
            title="Doc Fragment",
            source=FragmentSource(platform=SourcePlatform.DOCUMENT),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Documents" in str(result)

    def test_write_fragment_spreadsheet_goes_to_data(
        self,
        writer: VaultWriter,
    ) -> None:
        """Spreadsheet fragments land in the Data subfolder."""
        frag = Fragment(
            id="frag-xls00001",
            title="Sheet Fragment",
            source=FragmentSource(platform=SourcePlatform.SPREADSHEET),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Data" in str(result)

    def test_write_fragment_presentation_goes_to_decks(
        self,
        writer: VaultWriter,
    ) -> None:
        """Presentation fragments land in the Decks subfolder."""
        frag = Fragment(
            id="frag-ppt00001",
            title="Deck Fragment",
            source=FragmentSource(platform=SourcePlatform.PRESENTATION),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Decks" in str(result)

    def test_write_fragment_markdown_goes_to_notes(
        self,
        writer: VaultWriter,
    ) -> None:
        """Generic markdown fragments land in the Notes subfolder."""
        frag = Fragment(
            id="frag-md0000001",
            title="Markdown Note",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        )
        result = writer.write_fragment(frag)
        assert "01-Fragments/Notes" in str(result)

    def test_write_fragment_persists_hierarchy_fields(
        self,
        writer: VaultWriter,
    ) -> None:
        """FEAT-020 hierarchy fields appear in frontmatter and re-validate.

        Acceptance criterion: "``VaultWriter`` serialises hierarchy
        fields to frontmatter; ``VaultReader`` parses them back."
        """
        import frontmatter as fm_mod

        frag = Fragment(
            id="frag-hier-write1",
            title="Hierarchy Write",
            source=FragmentSource(platform=SourcePlatform.ESSAY),
            created=datetime(2025, 1, 15, 10, 30, 0),
            parent_id="frag-hier-root00",
            child_ids=["frag-hier-childa", "frag-hier-childb"],
            level="subsection",
            structural_path=["The Capricorn Moon", "On grief"],
        )
        result = writer.write_fragment(frag)
        text = result.read_text(encoding="utf-8")
        assert "parent_id: frag-hier-root00" in text
        assert "level: subsection" in text

        post = fm_mod.load(str(result))
        reloaded = Fragment.model_validate(dict(post.metadata))
        assert reloaded.parent_id == "frag-hier-root00"
        assert reloaded.child_ids == ["frag-hier-childa", "frag-hier-childb"]
        assert reloaded.level == "subsection"
        assert reloaded.structural_path == ["The Capricorn Moon", "On grief"]

    def test_write_fragment_defaults_serialise_as_root_document(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """A flat fragment's frontmatter records the documented defaults.

        Pre-FEAT-020 callers that never set the hierarchy fields must
        produce vault files whose ``parent_id``/``child_ids``/``level``/
        ``structural_path`` round-trip back to the documented defaults
        — so downstream loads of those files re-create root documents.
        """
        import frontmatter as fm_mod

        result = writer.write_fragment(sample_fragment)
        post = fm_mod.load(str(result))
        assert post["parent_id"] is None
        assert post["child_ids"] == []
        assert post["level"] == "document"
        assert post["structural_path"] == []

    def test_platform_subfolder_mapping_is_total(self) -> None:
        """Every SourcePlatform must have an explicit subfolder mapping.

        Guards against regressions where a new platform silently routes
        to ``Unsorted/`` because a developer forgot to extend the map.
        """
        from creek.vault.writer import _PLATFORM_SUBFOLDER

        unmapped = {p for p in SourcePlatform if p not in _PLATFORM_SUBFOLDER}
        assert unmapped == set(), (
            f"Missing _PLATFORM_SUBFOLDER entries for: {sorted(unmapped)}"
        )


# ---- write_thread ----


class TestWriteThread:
    """Tests for writing Thread models to the vault."""

    def test_write_thread_active(
        self,
        writer: VaultWriter,
        sample_thread: Thread,
    ) -> None:
        """Active thread is written to 02-Threads/Active/."""
        result = writer.write_thread(sample_thread)
        assert result.exists()
        assert "02-Threads/Active" in str(result)

    def test_write_thread_dormant(self, writer: VaultWriter) -> None:
        """Dormant thread is written to 02-Threads/Dormant/."""
        thread = Thread(
            id="thread-dormant",
            title="Dormant Thread",
            status=ThreadStatus.DORMANT,
        )
        result = writer.write_thread(thread)
        assert "02-Threads/Dormant" in str(result)

    def test_write_thread_resolved(self, writer: VaultWriter) -> None:
        """Resolved thread is written to 02-Threads/Resolved/."""
        thread = Thread(
            id="thread-resolv",
            title="Resolved Thread",
            status=ThreadStatus.RESOLVED,
        )
        result = writer.write_thread(thread)
        assert "02-Threads/Resolved" in str(result)

    def test_write_thread_content(
        self,
        writer: VaultWriter,
        sample_thread: Thread,
    ) -> None:
        """Thread file contains expected YAML frontmatter fields."""
        result = writer.write_thread(sample_thread)
        content = result.read_text(encoding="utf-8")
        assert "id: thread-test001" in content
        assert "type: thread" in content

    def test_write_thread_aliases_title_for_link_resolution(
        self,
        writer: VaultWriter,
        sample_thread: Thread,
    ) -> None:
        """The page aliases its bare title so ``[[<title>]]`` resolves.

        The bulk threads linker writes ``[[Test Active Thread]]`` onto member
        fragments, while the page lands at ``{date}-Test-Active-Thread.md``; the
        ``aliases`` entry bridges that gap for stock-Obsidian link resolution.
        """
        import frontmatter

        result = writer.write_thread(sample_thread)
        post = frontmatter.loads(result.read_text(encoding="utf-8"))
        assert sample_thread.title in post.get("aliases", [])


# ---- write_eddy ----


class TestWriteEddy:
    """Tests for writing Eddy models to the vault."""

    def test_write_eddy_creates_file(
        self,
        writer: VaultWriter,
        sample_eddy: Eddy,
    ) -> None:
        """Eddy is written to 03-Eddies/."""
        result = writer.write_eddy(sample_eddy)
        assert result.exists()
        assert "03-Eddies" in str(result)

    def test_write_eddy_content(
        self,
        writer: VaultWriter,
        sample_eddy: Eddy,
    ) -> None:
        """Eddy file contains expected YAML frontmatter fields."""
        result = writer.write_eddy(sample_eddy)
        content = result.read_text(encoding="utf-8")
        assert "id: eddy-test0001" in content
        assert "type: eddy" in content

    def test_write_eddy_aliases_title_for_link_resolution(
        self,
        writer: VaultWriter,
        sample_eddy: Eddy,
    ) -> None:
        """The page aliases its bare title so ``[[<title>]]`` resolves.

        Symmetric with threads: the eddies linker writes ``[[Test Eddy
        Cluster]]`` onto fragments while the file is ``{date}-Test-Eddy-
        Cluster.md``; the ``aliases`` entry makes the link resolve.
        """
        import frontmatter

        result = writer.write_eddy(sample_eddy)
        post = frontmatter.loads(result.read_text(encoding="utf-8"))
        assert sample_eddy.title in post.get("aliases", [])


# ---- write_praxis ----


class TestWritePraxis:
    """Tests for writing Praxis models to the vault."""

    def test_write_praxis_habit(
        self,
        writer: VaultWriter,
        sample_praxis: Praxis,
    ) -> None:
        """Praxis with type=habit goes to 04-Praxis/Daily/."""
        result = writer.write_praxis(sample_praxis)
        assert result.exists()
        assert "04-Praxis" in str(result)

    def test_write_praxis_practice(self, writer: VaultWriter) -> None:
        """Praxis with type=practice goes to 04-Praxis/Daily/."""
        praxis = Praxis(
            id="praxis-pract1",
            title="Practice Praxis",
            praxis_type=PraxisType.PRACTICE,
        )
        result = writer.write_praxis(praxis)
        assert "04-Praxis/Daily" in str(result)

    def test_write_praxis_framework(self, writer: VaultWriter) -> None:
        """Praxis with type=framework goes to 04-Praxis/Seasonal/."""
        praxis = Praxis(
            id="praxis-frame1",
            title="Framework Praxis",
            praxis_type=PraxisType.FRAMEWORK,
        )
        result = writer.write_praxis(praxis)
        assert "04-Praxis/Seasonal" in str(result)

    def test_write_praxis_insight(self, writer: VaultWriter) -> None:
        """Praxis with type=insight goes to 04-Praxis/Situational/."""
        praxis = Praxis(
            id="praxis-insig1",
            title="Insight Praxis",
            praxis_type=PraxisType.INSIGHT,
        )
        result = writer.write_praxis(praxis)
        assert "04-Praxis/Situational" in str(result)

    def test_write_praxis_commitment(self, writer: VaultWriter) -> None:
        """Praxis with type=commitment goes to 04-Praxis/Seasonal/."""
        praxis = Praxis(
            id="praxis-commi1",
            title="Commitment Praxis",
            praxis_type=PraxisType.COMMITMENT,
        )
        result = writer.write_praxis(praxis)
        assert "04-Praxis/Seasonal" in str(result)

    def test_write_praxis_content(
        self,
        writer: VaultWriter,
        sample_praxis: Praxis,
    ) -> None:
        """Praxis file contains expected YAML frontmatter fields."""
        result = writer.write_praxis(sample_praxis)
        content = result.read_text(encoding="utf-8")
        assert "id: praxis-test01" in content
        assert "type: praxis" in content


# ---- write_decision ----


class TestWriteDecision:
    """Tests for writing Decision models to the vault."""

    def test_write_decision_sensing(
        self,
        writer: VaultWriter,
        sample_decision: Decision,
    ) -> None:
        """Decision with status=sensing goes to 08-Decisions/Active/."""
        result = writer.write_decision(sample_decision)
        assert result.exists()
        assert "08-Decisions/Active" in str(result)

    def test_write_decision_deliberating(self, writer: VaultWriter) -> None:
        """Decision with status=deliberating goes to 08-Decisions/Active/."""
        dec = Decision(
            id="decision-del1",
            title="Deliberating Decision",
            status=DecisionStatus.DELIBERATING,
        )
        result = writer.write_decision(dec)
        assert "08-Decisions/Active" in str(result)

    def test_write_decision_enacted(self, writer: VaultWriter) -> None:
        """Decision with status=enacted goes to 08-Decisions/Archive/."""
        dec = Decision(
            id="decision-ena1",
            title="Enacted Decision",
            status=DecisionStatus.ENACTED,
        )
        result = writer.write_decision(dec)
        assert "08-Decisions/Archive" in str(result)

    def test_write_decision_reflecting(self, writer: VaultWriter) -> None:
        """Decision with status=reflecting goes to 08-Decisions/Archive/."""
        dec = Decision(
            id="decision-ref1",
            title="Reflecting Decision",
            status=DecisionStatus.REFLECTING,
        )
        result = writer.write_decision(dec)
        assert "08-Decisions/Archive" in str(result)

    def test_write_decision_content(
        self,
        writer: VaultWriter,
        sample_decision: Decision,
    ) -> None:
        """Decision file contains expected YAML frontmatter fields."""
        result = writer.write_decision(sample_decision)
        content = result.read_text(encoding="utf-8")
        assert "id: decision-test" in content
        assert "type: decision" in content


# ---- write_any dispatch ----


class TestWriteAny:
    """Tests for the write_any dispatch method."""

    def test_write_any_fragment(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """write_any dispatches Fragment to write_fragment."""
        result = writer.write_any(sample_fragment)
        assert result.exists()
        assert "01-Fragments" in str(result)

    def test_write_any_thread(
        self,
        writer: VaultWriter,
        sample_thread: Thread,
    ) -> None:
        """write_any dispatches Thread to write_thread."""
        result = writer.write_any(sample_thread)
        assert result.exists()
        assert "02-Threads" in str(result)

    def test_write_any_eddy(
        self,
        writer: VaultWriter,
        sample_eddy: Eddy,
    ) -> None:
        """write_any dispatches Eddy to write_eddy."""
        result = writer.write_any(sample_eddy)
        assert result.exists()
        assert "03-Eddies" in str(result)

    def test_write_any_praxis(
        self,
        writer: VaultWriter,
        sample_praxis: Praxis,
    ) -> None:
        """write_any dispatches Praxis to write_praxis."""
        result = writer.write_any(sample_praxis)
        assert result.exists()
        assert "04-Praxis" in str(result)

    def test_write_any_decision(
        self,
        writer: VaultWriter,
        sample_decision: Decision,
    ) -> None:
        """write_any dispatches Decision to write_decision."""
        result = writer.write_any(sample_decision)
        assert result.exists()
        assert "08-Decisions" in str(result)

    def test_write_any_unknown_type_raises(
        self,
        writer: VaultWriter,
    ) -> None:
        """write_any raises ValueError for unsupported model types."""

        class Unknown(BaseModel):
            """An unknown model type for testing."""

            type: str = "unknown"

        with pytest.raises(ValueError, match="Unsupported model type"):
            writer.write_any(Unknown())


# ---- Filename Sanitization ----


class TestFilenameSanitization:
    """Tests for filename sanitization and edge cases."""

    def test_long_title_truncated(self, writer: VaultWriter) -> None:
        """Titles longer than 80 characters are truncated."""
        long_title = "A" * 200
        frag = Fragment(
            id="frag-longttl1",
            title=long_title,
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        result = writer.write_fragment(frag)
        # Filename (minus date prefix and .md) should not exceed 80 chars
        name_part = result.stem  # e.g. "2025-01-15-AAAA..."
        # The sanitized title portion (after date prefix) should be <= 80 chars
        assert len(name_part) <= 80 + 11  # 11 for "YYYY-MM-DD-"

    def test_special_characters_removed(self, writer: VaultWriter) -> None:
        """Special characters are stripped from filenames."""
        frag = Fragment(
            id="frag-special1",
            title="Hello! @World #2025: A Test/Case",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        result = writer.write_fragment(frag)
        name = result.stem
        # Should not contain special chars
        assert "!" not in name
        assert "@" not in name
        assert "#" not in name
        assert ":" not in name
        assert "/" not in name

    def test_unicode_characters_in_title(self, writer: VaultWriter) -> None:
        """Unicode characters are handled gracefully in filenames."""
        frag = Fragment(
            id="frag-unicode1",
            title="Caf\u00e9 R\u00e9sum\u00e9 Na\u00efve",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        result = writer.write_fragment(frag)
        assert result.exists()

    def test_empty_title_produces_valid_filename(
        self,
        writer: VaultWriter,
    ) -> None:
        """An empty title still produces a valid filename."""
        frag = Fragment(
            id="frag-empty001",
            title="",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        result = writer.write_fragment(frag)
        assert result.exists()
        assert result.name.endswith(".md")

    def test_date_prefix_in_filename(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """Filenames are prefixed with a date."""
        result = writer.write_fragment(sample_fragment)
        # Should start with a date pattern like "2025-01-15-"
        assert result.name[:4].isdigit()
        assert result.name[4] == "-"


# ---- Duplicate Detection ----


class TestDuplicateDetection:
    """Tests for duplicate detection based on fragment ID."""

    def test_duplicate_fragment_skipped(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """Writing the same fragment twice returns the same path (no overwrite)."""
        first = writer.write_fragment(sample_fragment)
        second = writer.write_fragment(sample_fragment)
        assert first == second

    def test_duplicate_thread_skipped(
        self,
        writer: VaultWriter,
        sample_thread: Thread,
    ) -> None:
        """Writing the same thread twice returns the same path."""
        first = writer.write_thread(sample_thread)
        second = writer.write_thread(sample_thread)
        assert first == second

    def test_duplicate_eddy_skipped(
        self,
        writer: VaultWriter,
        sample_eddy: Eddy,
    ) -> None:
        """Writing the same eddy twice returns the same path."""
        first = writer.write_eddy(sample_eddy)
        second = writer.write_eddy(sample_eddy)
        assert first == second

    def test_duplicate_praxis_skipped(
        self,
        writer: VaultWriter,
        sample_praxis: Praxis,
    ) -> None:
        """Writing the same praxis twice returns the same path."""
        first = writer.write_praxis(sample_praxis)
        second = writer.write_praxis(sample_praxis)
        assert first == second

    def test_duplicate_decision_skipped(
        self,
        writer: VaultWriter,
        sample_decision: Decision,
    ) -> None:
        """Writing the same decision twice returns the same path."""
        first = writer.write_decision(sample_decision)
        second = writer.write_decision(sample_decision)
        assert first == second


# ---- Uniqueness: Same Title Different ID ----


class TestFilenameUniqueness:
    """Tests for filename uniqueness when titles collide."""

    def test_same_title_different_id_produces_unique_files(
        self,
        writer: VaultWriter,
    ) -> None:
        """Two fragments with the same title but different IDs get unique files."""
        frag1 = Fragment(
            id="frag-aaaa0001",
            title="Same Title",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 0, 0),
        )
        frag2 = Fragment(
            id="frag-bbbb0001",
            title="Same Title",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 0, 0),
        )
        path1 = writer.write_fragment(frag1)
        path2 = writer.write_fragment(frag2)
        assert path1 != path2
        assert path1.exists()
        assert path2.exists()


# ---- Provenance Logging ----


def _read_provenance(vault_path: Path) -> list[dict[str, Any]]:
    """Read the JSONL provenance log into a list of dicts (test helper)."""
    log_path = vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
    text = log_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestProvenanceLogging:
    """Tests for provenance log file creation and appending."""

    @staticmethod
    def _provenance_path(vault_path: Path) -> Path:
        """Return the canonical JSONL provenance log path."""
        return vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"

    @staticmethod
    def _read_entries(log_path: Path) -> list[dict[str, Any]]:
        """Parse a JSONL provenance log into a list of dict entries."""
        entries: list[dict[str, Any]] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line:
                entries.append(json.loads(line))
        return entries

    def test_provenance_log_created(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Writing a fragment creates/updates the provenance log."""
        writer.write_fragment(sample_fragment)
        assert self._provenance_path(vault_path).exists()

    def test_provenance_log_contains_entry(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Provenance log contains an entry for the written fragment."""
        writer.write_fragment(sample_fragment)
        entries = self._read_entries(self._provenance_path(vault_path))
        assert len(entries) == 1
        assert entries[0]["id"] == "frag-test0001"
        assert entries[0]["type"] == "fragment"

    def test_provenance_log_appends(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        sample_thread: Thread,
        vault_path: Path,
    ) -> None:
        """Multiple writes append to the provenance log."""
        writer.write_fragment(sample_fragment)
        writer.write_thread(sample_thread)
        entries = self._read_entries(self._provenance_path(vault_path))
        assert len(entries) == 2
        ids = {e["id"] for e in entries}
        assert "frag-test0001" in ids
        assert "thread-test001" in ids

    def test_provenance_log_has_path_field(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Provenance log entry includes the written file path."""
        result = writer.write_fragment(sample_fragment)
        entries = self._read_entries(self._provenance_path(vault_path))
        assert entries[0]["path"] == str(result)

    def test_provenance_log_has_timestamp(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Provenance log entry includes a timestamp."""
        writer.write_fragment(sample_fragment)
        entries = self._read_entries(self._provenance_path(vault_path))
        assert "written_at" in entries[0]

    def test_duplicate_not_logged_again(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Writing a duplicate does not add a second provenance entry."""
        writer.write_fragment(sample_fragment)
        writer.write_fragment(sample_fragment)
        entries = self._read_entries(self._provenance_path(vault_path))
        assert len(entries) == 1

    def test_legacy_provenance_json_migrated_on_first_writer(
        self,
        vault_path: Path,
    ) -> None:
        """Pre-Batch-C provenance.json is replayed into provenance.jsonl.

        Regression for PR #193 review (comment 4365568699 BLOCKING #2):
        operators upgrading from a vault that already has the legacy
        JSON-array log must not lose those entries when the new
        :class:`creek.audit.AuditLog`-backed JSONL log is constructed.
        """
        from creek.audit import AuditLog

        legacy_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.json"
        )
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(
                [
                    {
                        "id": "frag-old-001",
                        "type": "fragment",
                        "path": "/tmp/old-frag.md",
                        "written_at": "2025-12-31T23:59:59",
                    },
                    {
                        "id": "frag-old-002",
                        "type": "fragment",
                        "path": "/tmp/old-frag-2.md",
                        "written_at": "2026-01-01T00:00:00",
                    },
                ],
            ),
            encoding="utf-8",
        )

        VaultWriter(vault_path=vault_path)

        new_path = vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
        assert new_path.exists()
        assert not legacy_path.exists()
        entries = self._read_entries(new_path)
        ids = [e.get("id") for e in entries]
        assert "frag-old-001" in ids
        assert "frag-old-002" in ids
        marker = next(e for e in entries if e.get("type") == "provenance.migration")
        assert marker["migrated_entries"] == 2
        assert marker["migration_status"] == "ok"
        AuditLog(new_path).verify()

    def test_legacy_provenance_migration_marks_corrupt_input(
        self,
        vault_path: Path,
    ) -> None:
        """Malformed legacy JSON is recorded as ``parse_failed``.

        Distinguishes a clean migration of an empty legacy file from a
        transient parse failure that lost data — the operator can audit
        which case occurred.
        """
        legacy_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.json"
        )
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("{not valid json", encoding="utf-8")

        VaultWriter(vault_path=vault_path)

        new_path = vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
        entries = self._read_entries(new_path)
        marker = next(e for e in entries if e.get("type") == "provenance.migration")
        assert marker["migration_status"] == "parse_failed"
        assert marker["migrated_entries"] == 0

    def test_legacy_provenance_migration_strips_prev_hash(
        self,
        vault_path: Path,
    ) -> None:
        """Legacy entries carrying ``prev_hash`` are sanitised before append.

        :meth:`AuditLog.append` rejects payloads that try to forge the
        chain key. Migration must be tolerant of older logs that may
        have stamped ``prev_hash`` themselves rather than blow up at
        startup.
        """
        legacy_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.json"
        )
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(
                [
                    {
                        "id": "frag-with-prev",
                        "type": "fragment",
                        "prev_hash": "deadbeef" * 8,
                    },
                ],
            ),
            encoding="utf-8",
        )

        VaultWriter(vault_path=vault_path)

        new_path = vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
        entries = self._read_entries(new_path)
        migrated = next(e for e in entries if e.get("id") == "frag-with-prev")
        # The line's prev_hash field is the chain hash, not the legacy
        # value. Genesis hash of an empty chain is 64 zeros.
        assert migrated["prev_hash"] == "0" * 64

    def test_legacy_provenance_migration_skipped_when_jsonl_already_populated(
        self,
        vault_path: Path,
    ) -> None:
        """If the JSONL log already has entries, the legacy JSON is left alone.

        Prevents a half-migrated state from being silently re-migrated;
        the operator must resolve the inconsistency by inspection.
        """
        legacy_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.json"
        )
        new_path = vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps([{"id": "old", "type": "fragment"}]))
        # Pre-populate the new log via a fresh AuditLog to simulate a
        # prior partial migration.
        from creek.audit import AuditLog

        AuditLog(new_path).append({"id": "already-here", "type": "fragment"})

        VaultWriter(vault_path=vault_path)

        assert legacy_path.exists()  # Untouched.
        ids = [e.get("id") for e in self._read_entries(new_path)]
        assert ids == ["already-here"]

    def test_legacy_provenance_migration_with_empty_preexisting_jsonl(
        self,
        vault_path: Path,
    ) -> None:
        """Empty pre-created provenance.jsonl + legacy JSON migrates once.

        Regression for PR #193 review (comment 4367110538 BLOCKING #3):
        the size guard short-circuits only when the new log has size > 0.
        An earlier code path may have opened ``provenance.jsonl`` for
        append (creating it as a 0-byte file) without writing. This test
        pins that the migration still runs, replays the legacy entries,
        and a second writer constructed afterwards is a no-op so legacy
        entries cannot double across instances.
        """
        legacy_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.json"
        )
        new_path = vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(
                [
                    {"id": "frag-empty-pre", "type": "fragment"},
                ],
            ),
            encoding="utf-8",
        )
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.touch()
        assert new_path.stat().st_size == 0

        VaultWriter(vault_path=vault_path)

        # First instance migrated cleanly.
        assert not legacy_path.exists()
        entries = self._read_entries(new_path)
        ids = [e.get("id") for e in entries]
        types = [e.get("type") for e in entries]
        assert ids == ["frag-empty-pre", "_migration_"]
        assert types == ["fragment", "provenance.migration"]

        # A second writer must not re-migrate anything.
        VaultWriter(vault_path=vault_path)
        ids_after = [e.get("id") for e in self._read_entries(new_path)]
        assert ids_after == ["frag-empty-pre", "_migration_"]

    def test_legacy_provenance_orphaned_file_logs_warning(
        self,
        vault_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Half-migrated state (both files present, JSONL non-empty) is logged.

        Regression for PR #193 review (comment 4367360694 HIGH): the
        size guard would silently skip migration when both the legacy
        and the new log carried content, leaving an operator unaware
        that the legacy file was orphaned. The warning makes the
        inconsistency visible the first time it is encountered.
        """
        from creek.audit import AuditLog

        legacy_path = (
            vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.json"
        )
        new_path = vault_path / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps([{"id": "orphan-frag", "type": "fragment"}]),
            encoding="utf-8",
        )
        AuditLog(new_path).append({"id": "already-here", "type": "fragment"})

        with caplog.at_level("WARNING", logger="creek.vault.writer"):
            VaultWriter(vault_path=vault_path)

        assert legacy_path.exists()
        assert any(
            "skipping migration" in record.message
            and "provenance.json" in record.message
            for record in caplog.records
        )

    def test_provenance_chain_intact_after_many_writes(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """Many writes share a single AuditLog and produce a valid chain.

        Regression for PR #193 review (comment 4365147477): a transient
        ``AuditLog`` per provenance write would defeat the per-instance
        hash cache, re-reading the entire log every append. This test
        writes ten fragments through ``write_fragment`` and asserts the
        resulting chain still verifies — caching cannot have broken the
        chain semantics.
        """
        from creek.audit import AuditLog

        for i in range(10):
            frag = Fragment(
                id=f"frag-prov-{i:04d}",
                title=f"Provenance Chain {i}",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=datetime(2025, 1, 15, 10, 30, 0),
            )
            writer.write_fragment(frag)

        log_path = self._provenance_path(vault_path)
        entries = self._read_entries(log_path)
        assert len(entries) == 10
        AuditLog(log_path).verify()
        # Same VaultWriter writes share one AuditLog instance per the
        # __init__ change. We assert the cache was reused at least once
        # by checking the cache fields are populated post-run.
        assert writer._provenance_log._cached_last_hash is not None
        assert writer._provenance_log._cached_size is not None


# Note: Batch E (PR #194) introduced TestProvenanceLegacyMigration covering
# the same migration scenarios. Those tests have been merged into the four
# `test_legacy_provenance_*` methods on TestProvenanceLogging above, which
# additionally assert the migration marker fields, migration_status, and
# chain integrity that Batch C's AuditLog-backed migration provides.
# `test_migration_drops_oversized_entry_without_blocking_writes` from main
# is dropped because Batch C's implementation routes legacy entries through
# AuditLog.append (which uses Python's text I/O, not raw os.write with a
# PIPE_BUF cap), so the oversized-entry failure mode the test pinned does
# not apply to the merged code path.


# ---- ID Index (PERF-001) ----


class TestIdIndex:
    """Tests for the per-directory ``.id-index.jsonl`` index file."""

    def test_index_file_created_on_first_write(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """A first write appends a JSON line to ``.id-index.jsonl``."""
        result = writer.write_fragment(sample_fragment)
        index_path = result.parent / ".id-index.jsonl"
        assert index_path.exists()
        lines = [
            line for line in index_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry == {"id": sample_fragment.id, "filename": result.name}

    def test_index_lookup_avoids_directory_scan(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """Re-writing an existing fragment uses the index, not a scan."""
        result = writer.write_fragment(sample_fragment)
        # If the index path is consulted, removing the markdown but
        # leaving a stale index entry should make the second write
        # re-create the file at a new path (proof the lookup goes
        # through the index, then verifies file existence on disk).
        result.unlink()
        new_path = writer.write_fragment(sample_fragment)
        assert new_path.exists()
        assert new_path.parent == result.parent

    def test_index_rebuilds_when_missing(
        self,
        vault_path: Path,
        sample_fragment: Fragment,
    ) -> None:
        """A vault with existing fragments but no index rebuilds it on demand."""
        # Seed the directory using one writer instance.
        seed_writer = VaultWriter(vault_path=vault_path)
        original = seed_writer.write_fragment(sample_fragment)
        # Remove the index file as if migrating from an older vault.
        index_path = original.parent / ".id-index.jsonl"
        index_path.unlink()
        # A fresh writer should detect the existing fragment without
        # re-creating it.
        fresh_writer = VaultWriter(vault_path=vault_path)
        result = fresh_writer.write_fragment(sample_fragment)
        assert result == original
        assert index_path.exists()

    def test_corrupt_index_lines_are_skipped(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Malformed lines in the JSONL index are skipped, not fatal."""
        original = writer.write_fragment(sample_fragment)
        index_path = original.parent / ".id-index.jsonl"
        # Append a malformed line; the well-formed first line should
        # still be parsed by a fresh writer.
        with index_path.open("a", encoding="utf-8") as fp:
            fp.write("{not json}\n")
        fresh_writer = VaultWriter(vault_path=vault_path)
        result = fresh_writer.write_fragment(sample_fragment)
        assert result == original

    def test_index_skips_blank_and_non_dict_lines(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Blank lines and non-object JSON lines are tolerated."""
        original = writer.write_fragment(sample_fragment)
        index_path = original.parent / ".id-index.jsonl"
        with index_path.open("a", encoding="utf-8") as fp:
            fp.write("\n")  # blank line
            fp.write("[1, 2, 3]\n")  # non-dict line
            fp.write('{"id": 7, "filename": "x.md"}\n')  # wrong types
        fresh_writer = VaultWriter(vault_path=vault_path)
        result = fresh_writer.write_fragment(sample_fragment)
        assert result == original

    def test_public_find_existing_uses_lock(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
    ) -> None:
        """``_find_existing`` (the public-style shim) returns the indexed path."""
        original = writer.write_fragment(sample_fragment)
        target_dir = original.parent
        # Direct call to the locked-shim variant should report the same
        # path the in-memory index holds.
        assert writer._find_existing(sample_fragment.id, target_dir) == original
        # Unknown ID returns ``None`` via the same path.
        assert writer._find_existing("frag-unknown", target_dir) is None


# ---- ID index verification (#1083) ----


def _seed_victim(writer: VaultWriter) -> Path:
    """Write one distinctive bystander fragment and return its path."""
    victim = Fragment(
        id="frag-victim001",
        title="Victim",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=datetime(2025, 1, 15, 10, 30, 0),
        privacy_tier=PrivacyTier.INTIMATE,
    )
    return writer.write_fragment(victim, body="victim body")


def _edited_fragment() -> Fragment:
    """Return the fragment whose id a poisoned index will mis-resolve."""
    return Fragment(
        id="frag-edited001",
        title="Edited",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=datetime(2025, 1, 15, 10, 30, 0),
    )


def _files_declaring(target_dir: Path, model_id: str) -> list[Path]:
    """Return the ``.md`` files in *target_dir* declaring *model_id* on disk."""
    return sorted(
        path
        for path in target_dir.glob("*.md")
        if str(frontmatter.load(str(path)).get("id")) == model_id
    )


class TestIdIndexVerification:
    """The located file must itself declare the id the index claimed (#1083).

    A stale or poisoned ``.id-index.jsonl`` entry names a file belonging to a
    *different* fragment. Every locator caller — ``update_fragment``, the
    ``_write_model`` duplicate check, ``tomb_fragment`` and
    ``restore_fragment`` — then acts destructively on that foreign file.
    """

    def test_update_fragment_does_not_clobber_foreign_id_file(
        self,
        vault_path: Path,
    ) -> None:
        """A stale index entry must not redirect an update onto a foreign file."""
        seed = VaultWriter(vault_path=vault_path)
        victim = Fragment(
            id="frag-victim001",
            title="Victim",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 30, 0),
            privacy_tier=PrivacyTier.INTIMATE,
        )
        victim_path = seed.write_fragment(victim, body="victim body")
        before = victim_path.read_bytes()

        # Poison the on-disk index: a DIFFERENT id now names the victim's file.
        seed._append_index_entry(
            victim_path.parent,
            "frag-edited001",
            victim_path.name,
        )

        # Fresh writer, so the poisoned mapping is loaded from the JSONL
        # rather than an in-process dict.
        fresh = VaultWriter(vault_path=vault_path)
        edited = Fragment(
            id="frag-edited001",
            title="Edited",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 30, 0),
        )
        result = fresh.update_fragment(edited, "therapy session notes")

        assert victim_path.read_bytes() == before
        assert "therapy session notes" not in victim_path.read_text(encoding="utf-8")
        assert frontmatter.load(str(victim_path))["privacy_tier"] == "intimate"
        assert result is None

    def test_update_fragment_reresolves_to_the_real_file(
        self,
        vault_path: Path,
    ) -> None:
        """A mismatch re-resolves to the file that genuinely declares the id."""
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        edited = _edited_fragment()
        owner_path = seed.write_fragment(edited, body="owner body")
        before = victim_path.read_bytes()
        seed._append_index_entry(victim_path.parent, edited.id, victim_path.name)

        fresh = VaultWriter(vault_path=vault_path)
        result = fresh.update_fragment(edited, "revised owner body")

        assert result == owner_path
        assert "revised owner body" in frontmatter.load(str(owner_path)).content
        assert victim_path.read_bytes() == before

    def test_index_repair_is_persisted(
        self,
        vault_path: Path,
    ) -> None:
        """The corrected mapping survives into a fresh writer's index load."""
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        edited = _edited_fragment()
        owner_path = seed.write_fragment(edited, body="owner body")
        target_dir = victim_path.parent
        seed._append_index_entry(target_dir, edited.id, victim_path.name)

        VaultWriter(vault_path=vault_path).update_fragment(edited, "revised body")

        third = VaultWriter(vault_path=vault_path)
        assert third._find_existing(edited.id, target_dir) == owner_path
        assert _files_declaring(target_dir, edited.id) == [owner_path]

    def test_write_model_does_not_return_foreign_file_as_duplicate(
        self,
        vault_path: Path,
    ) -> None:
        """A poisoned entry must not turn a brand-new fragment into a silent no-op."""
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        before = victim_path.read_bytes()
        newcomer = _edited_fragment()
        seed._append_index_entry(victim_path.parent, newcomer.id, victim_path.name)

        fresh = VaultWriter(vault_path=vault_path)
        result = fresh.write_fragment(newcomer, body="newcomer body")

        assert result != victim_path
        assert result.exists()
        assert frontmatter.load(str(result))["id"] == newcomer.id
        assert victim_path.read_bytes() == before

    def test_tomb_fragment_does_not_move_and_unlink_foreign_file(
        self,
        vault_path: Path,
    ) -> None:
        """Tombing a ghost id must not relocate and delete a bystander fragment."""
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        before = victim_path.read_bytes()
        seed._append_index_entry(
            victim_path.parent,
            "frag-ghost0001",
            victim_path.name,
        )
        orphan_dir = vault_path / "10-Liminal" / "Orphaned"

        fresh = VaultWriter(vault_path=vault_path)
        result = fresh.tomb_fragment("frag-ghost0001")

        assert result is None
        assert victim_path.exists()
        assert victim_path.read_bytes() == before
        assert list(orphan_dir.glob("*.md")) == []

    def test_restore_fragment_does_not_move_and_unlink_foreign_file(
        self,
        vault_path: Path,
    ) -> None:
        """Restoring a ghost id must not drag a real tomb out of Orphaned."""
        orphan_dir = vault_path / "10-Liminal" / "Orphaned"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        seed = VaultWriter(vault_path=vault_path)
        _seed_victim(seed)
        tombed_path = seed.tomb_fragment("frag-victim001")
        assert tombed_path is not None  # setup guard
        before = tombed_path.read_bytes()
        seed._append_index_entry(orphan_dir, "frag-ghost0001", tombed_path.name)

        ghost = Fragment(
            id="frag-ghost0001",
            title="Ghost",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 30, 0),
        )
        fresh = VaultWriter(vault_path=vault_path)
        result = fresh.restore_fragment(ghost)

        assert result is None
        assert tombed_path.exists()
        assert tombed_path.read_bytes() == before

    def test_unparseable_located_file_is_a_mismatch_not_a_crash(
        self,
        vault_path: Path,
    ) -> None:
        """Broken YAML in the located file resolves to no match, not an exception."""
        edited = _edited_fragment()
        seed = VaultWriter(vault_path=vault_path)
        target_dir = seed._fragment_target_dir(edited)
        target_dir.mkdir(parents=True, exist_ok=True)
        broken = target_dir / "broken.md"
        broken.write_text(
            "---\nid: [unterminated\ntitle: Broken\n---\nbroken body\n",
            encoding="utf-8",
        )
        seed._append_index_entry(target_dir, edited.id, broken.name)

        fresh = VaultWriter(vault_path=vault_path)

        assert fresh._find_existing(edited.id, target_dir) is None
        assert broken.exists()

    def test_located_file_without_id_key_is_a_mismatch(
        self,
        vault_path: Path,
    ) -> None:
        """Valid frontmatter with no ``id`` key declares no id, so it never matches."""
        edited = _edited_fragment()
        seed = VaultWriter(vault_path=vault_path)
        target_dir = seed._fragment_target_dir(edited)
        target_dir.mkdir(parents=True, exist_ok=True)
        idless = target_dir / "idless.md"
        idless.write_text(
            "---\ntitle: No Id Here\n---\nidless body\n",
            encoding="utf-8",
        )
        seed._append_index_entry(target_dir, edited.id, idless.name)

        fresh = VaultWriter(vault_path=vault_path)

        assert fresh._find_existing(edited.id, target_dir) is None

    def test_non_str_id_in_frontmatter_is_a_mismatch(
        self,
        vault_path: Path,
    ) -> None:
        """A YAML-int ``id`` does not satisfy the string id the caller asked for."""
        numeric = Fragment(
            id="12345",
            title="Numeric",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 30, 0),
        )
        seed = VaultWriter(vault_path=vault_path)
        target_dir = seed._fragment_target_dir(numeric)
        target_dir.mkdir(parents=True, exist_ok=True)
        int_id_path = target_dir / "int-id.md"
        int_id_path.write_text(
            "---\nid: 12345\ntitle: Int Id\n---\nint id body\n",
            encoding="utf-8",
        )
        before = int_id_path.read_bytes()
        seed._append_index_entry(target_dir, numeric.id, int_id_path.name)

        fresh = VaultWriter(vault_path=vault_path)
        result = fresh.write_fragment(numeric, body="string id body")

        assert result != int_id_path
        assert frontmatter.load(str(result))["id"] == "12345"
        assert int_id_path.read_bytes() == before
        # Both readers go through ``_declared_id``, which reports an unquoted
        # scalar only when YAML itself types it ``str`` — so the int-typed file
        # stays invisible to re-resolution and the honest outcome is two files
        # carrying the same *logical* id. That strictness is a decision, not an
        # oversight (#1291 option (a), normalise-on-read, was rejected: it
        # merges two identities the vault never said were the same). The
        # bounded byte-scan #1543 added would have implemented (a) by accident
        # if it had read ``12345`` as text, which is why ``_typed_scalar``
        # defers a bare numeral to the parser. The hazard is no longer silent:
        # ``creek lint --check nonstring-id`` reports this file by name.
        assert _files_declaring(target_dir, "12345") == sorted([int_id_path, result])

    def test_vanished_file_does_not_trigger_a_rescan(
        self,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ordinary vanished file resolves to ``None`` without a directory scan.

        Regression guard: the mismatch fix must not route the everyday
        deleted-or-tombed case into a full ``_rebuild_index`` sweep, which
        would undo the O(1) lookup PERF-001 bought.
        """

        def _no_rescan(target_dir: Path) -> dict[str, str]:
            """Fail loudly instead of scanning the directory."""
            msg = f"unexpected directory rescan of {target_dir}"
            raise AssertionError(msg)

        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        victim_path.unlink()
        monkeypatch.setattr(VaultWriter, "_rebuild_index", staticmethod(_no_rescan))

        fresh = VaultWriter(vault_path=vault_path)

        assert fresh._find_existing("frag-victim001", target_dir) is None


# ---- Concurrent writes (BUG-006) ----


class TestConcurrentWrites:
    """Tests guarding against the ThreadPoolExecutor race in ``_write_model``."""

    def test_concurrent_distinct_writes_no_loss(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """200 distinct fragments x 8 threads -> 200 distinct files, no loss."""
        from concurrent.futures import ThreadPoolExecutor

        import frontmatter as fm_mod

        fragments = [
            Fragment(
                id=f"frag-{i:09d}",
                title=f"t{i}",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=datetime(2025, 1, 15, 10, 0, 0),
            )
            for i in range(200)
        ]

        def _write(fragment: Fragment) -> Path:
            return writer.write_fragment(fragment, body=f"body for {fragment.id}")

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_write, fragments))

        written = list((vault_path / "01-Fragments" / "Conversations").glob("*.md"))
        assert len(written) == 200
        ids = {fm_mod.load(str(p)).get("id") for p in written}
        assert ids == {f.id for f in fragments}

        entries = _read_provenance(vault_path)
        assert len(entries) == 200

    def test_concurrent_same_id_writes_dedup(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        vault_path: Path,
    ) -> None:
        """Concurrent writes of the same ID converge on a single file."""
        from concurrent.futures import ThreadPoolExecutor

        def _write(_: int) -> Path:
            return writer.write_fragment(sample_fragment, body="x")

        with ThreadPoolExecutor(max_workers=8) as ex:
            paths = list(ex.map(_write, range(50)))

        # All paths point to the same file — no duplicates.
        assert len({str(p) for p in paths}) == 1
        written = list((vault_path / "01-Fragments" / "Conversations").glob("*.md"))
        assert len(written) == 1


class TestAtomicWriteHardening:
    """Regression tests for `_atomic_write_text` / `_atomic_create` review notes."""

    def test_atomic_write_uses_unique_temp_filename(
        self,
        writer: VaultWriter,
        sample_fragment: Fragment,
        tmp_path: Path,
    ) -> None:
        """Concurrent index persists do not race on a fixed `.tmp` sidecar."""
        from concurrent.futures import ThreadPoolExecutor

        # Trigger the index file to exist with one entry.
        writer.write_fragment(sample_fragment)

        # Now hammer the index path with parallel atomic writes — if a
        # fixed-name temp file were used, the second writer would race
        # the first's rename and could leave behind a corrupt JSON.
        from creek.vault.writer import _atomic_write_text

        target = tmp_path / "atomic-target.json"

        def _write(i: int) -> None:
            _atomic_write_text(target, f'{{"value": {i}}}')

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_write, range(50)))

        # Final file must be readable as JSON — not truncated mid-write.
        text = target.read_text(encoding="utf-8")
        assert json.loads(text)["value"] in range(50)
        # And no leftover ``.tmp`` files in the directory.
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert not leftovers

    def test_oversized_index_entry_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        """An index line over PIPE_BUF surfaces a `ValueError` rather than racing."""
        from creek.vault.writer import VaultWriter

        target_dir = tmp_path
        target_dir.mkdir(parents=True, exist_ok=True)
        oversized_id = "x" * 5000  # exceeds the 4 096-byte cap
        with pytest.raises(ValueError, match="exceeds PIPE_BUF"):
            VaultWriter._append_index_entry(target_dir, oversized_id, "file.md")

    def test_atomic_create_caps_collision_retries(
        self,
        writer: VaultWriter,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_atomic_create`` raises rather than spinning when retries exhausted."""
        from creek.vault import writer as writer_mod

        target_dir = vault_path / "01-Fragments" / "Conversations"
        target_dir.mkdir(parents=True, exist_ok=True)

        # Force every os.open to raise FileExistsError so the retry
        # loop must spin until it hits the cap.
        def _always_exists(*args: object, **kwargs: object) -> int:
            raise FileExistsError

        monkeypatch.setattr(writer_mod.os, "open", _always_exists)
        monkeypatch.setattr(writer_mod, "_MAX_FILENAME_COLLISION_RETRIES", 3)

        with pytest.raises(RuntimeError, match="unique filename"):
            writer_mod.VaultWriter._atomic_create(
                target_dir,
                "2025-01-15-collide",
                "irrelevant",
            )

    def test_atomic_create_writes_full_body_under_short_writes(
        self,
        vault_path: Path,
        short_write: ShortWriteController,
    ) -> None:
        """A short ``os.write`` must not truncate the created note (#987).

        ``_atomic_create`` writes straight to the final path — there is no
        tempfile + rename — so a discarded byte count lands a half-written
        note at the real filename with no exception raised.
        """
        short_write.halve()
        target_dir = vault_path / "01-Fragments" / "Conversations"
        content = "x" * 400 + "TAIL"

        path = VaultWriter._atomic_create(target_dir, "demo", content)

        assert path.read_text(encoding="utf-8") == content

    def test_atomic_create_leaves_no_file_when_write_makes_no_progress(
        self,
        vault_path: Path,
        short_write: ShortWriteController,
    ) -> None:
        """A stalled descriptor raises instead of returning an empty note.

        Today the zero-byte return is ignored: the caller receives a path
        to a 0-byte file and indexes it as a real fragment.
        """
        short_write.stall()
        target_dir = vault_path / "01-Fragments" / "Conversations"

        with pytest.raises(OSError) as excinfo:
            VaultWriter._atomic_create(target_dir, "demo", "x" * 400 + "TAIL")

        assert excinfo.value.errno == errno.EIO
        assert not list(target_dir.glob("demo*.md"))

    def test_append_index_entry_writes_complete_line_under_short_writes(
        self,
        vault_path: Path,
        short_write: ShortWriteController,
    ) -> None:
        """A short append writes the whole JSON line, not half of one.

        Half a line is not valid JSON, so ``_load_index_file`` skips it and
        the id-to-filename mapping is silently dropped — the fragment
        becomes invisible to duplicate detection and to ``update_fragment``.

        Also pins the on-the-wire framing: the record is bracketed by a
        newline on *both* sides. The leading one is the record delimiter
        that makes any remnant ahead of it self-terminating (#1120), so
        it is part of the contract, not incidental whitespace.
        """
        from creek.vault.writer import INDEX_FILENAME

        short_write.halve()
        target_dir = vault_path / "01-Fragments" / "Conversations"
        model_id = "frag-short-write"
        filename = "2025-01-15-demo.md"

        VaultWriter._append_index_entry(target_dir, model_id, filename)

        index_path = target_dir / INDEX_FILENAME
        expected = (
            "\n"
            + json.dumps({"id": model_id, "filename": filename}, sort_keys=True)
            + "\n"
        )
        assert index_path.read_text(encoding="utf-8") == expected
        assert VaultWriter._load_index_file(index_path) == {model_id: filename}

    def test_append_index_entry_raises_when_write_makes_no_progress(
        self,
        vault_path: Path,
        short_write: ShortWriteController,
    ) -> None:
        """A stalled index append fails loudly rather than dropping the entry."""
        from creek.vault.writer import INDEX_FILENAME

        short_write.stall()
        target_dir = vault_path / "01-Fragments" / "Conversations"

        with pytest.raises(OSError) as excinfo:
            VaultWriter._append_index_entry(
                target_dir,
                "frag-stalled",
                "2025-01-15-demo.md",
            )

        assert excinfo.value.errno == errno.EIO
        assert VaultWriter._load_index_file(target_dir / INDEX_FILENAME) == {}

    def test_torn_index_append_does_not_swallow_the_next_entry(
        self,
        vault_path: Path,
        short_write: ShortWriteController,
    ) -> None:
        """A half-written index line costs at most *its own* entry (#1120).

        The inversion of the #987-era characterization test that pinned
        the defect. ``_append_index_entry`` opens with ``O_APPEND``, so
        every record lands at EOF with no separator the filesystem
        supplies. When a drain fails part-way (``ENOSPC`` here, after
        half the bytes are on disk) a remnant survives at the tail.

        Each record is now framed as ``\\n{json}\\n``, so the *next*
        record opens with a newline of its own: it terminates whatever
        remnant precedes it instead of being concatenated onto it. The
        torn entry is still lost at this layer — it never got its own
        content on disk — but the innocent next entry parses cleanly.
        Recovery of the torn id itself is the damage-rescan half of
        #1120, covered by ``TestIndexDamageRecovery``.
        """
        from creek.vault.writer import INDEX_FILENAME

        target_dir = vault_path / "01-Fragments" / "Conversations"
        index_path = target_dir / INDEX_FILENAME
        next_line = json.dumps(
            {"id": "frag-next", "filename": "2025-01-16-next.md"},
            sort_keys=True,
        )

        # First append: half the encoded line reaches disk (the payload
        # is ~60 bytes, so the cut lands deep inside the JSON, far short
        # of the terminating newline), then the drain hits ENOSPC.
        short_write.fail_after_half(errno.ENOSPC)
        with pytest.raises(OSError) as excinfo:
            VaultWriter._append_index_entry(
                target_dir,
                "frag-partial",
                "2025-01-15-partial.md",
            )
        assert excinfo.value.errno == errno.ENOSPC

        # Second append: an entirely healthy, complete write.
        short_write.passthrough()
        VaultWriter._append_index_entry(
            target_dir,
            "frag-next",
            "2025-01-16-next.md",
        )

        raw = index_path.read_text(encoding="utf-8")
        # Both halves of the scenario are genuinely on disk, so neither
        # write can have silently become a no-op: the file is non-empty,
        # the second entry landed complete at the tail, and something
        # (the remnant) precedes it.
        assert raw
        assert raw.endswith(next_line + "\n")
        assert len(raw) > len(next_line) + 1
        # ...and the second record's leading newline terminated the
        # remnant, so the two are separate lines rather than one merged,
        # unparseable one. (Three elements, not two: the file now opens
        # with a newline, so ``splitlines`` yields a leading empty item.)
        assert len(raw.splitlines()) >= 2

        index = VaultWriter._load_index_file(index_path)
        # The torn entry never reached disk in full, so it is genuinely
        # absent — but the innocent next one survives.
        assert "frag-partial" not in index
        assert index["frag-next"] == "2025-01-16-next.md"
        # A remnant must never be misread as some *other* valid mapping.
        assert set(index) <= {"frag-partial", "frag-next"}

    def test_any_torn_tail_prefix_leaves_the_next_entry_parseable(
        self,
        tmp_path: Path,
    ) -> None:
        """Every byte-prefix of a record is a tolerable tail (#1120).

        The forcing function. No exception is in flight anywhere here:
        the torn tail is written straight to disk, which is what a
        ``SIGKILL``/OOM/power loss between two ``os.write`` iterations
        inside :func:`creek._fsio.write_all` actually leaves behind. A
        fix that only cleans up inside an ``except OSError:`` handler
        cannot satisfy this, because there is no handler to run.
        """
        from creek.vault.writer import INDEX_FILENAME

        encoded = (
            "\n"
            + json.dumps({"id": "frag-torn", "filename": "torn.md"}, sort_keys=True)
            + "\n"
        ).encode()

        for cut in range(len(encoded)):
            target_dir = tmp_path / f"cut-{cut:03d}"
            target_dir.mkdir()
            index_path = target_dir / INDEX_FILENAME
            index_path.write_bytes(encoded[:cut])

            VaultWriter._append_index_entry(target_dir, "frag-next", "next.md")

            index = VaultWriter._load_index_file(index_path)
            assert index.get("frag-next") == "next.md", f"lost at cut={cut}"
            assert set(index) <= {"frag-torn", "frag-next"}, f"phantom at cut={cut}"

    def test_a_legacy_format_remnant_does_not_swallow_the_next_entry(
        self,
        tmp_path: Path,
    ) -> None:
        """An OLD-format torn tail is tolerated by a NEW-format append (#1120).

        Every ``.id-index.jsonl`` in a live vault was written by the
        pre-#1120 encoder, so any torn tail already on disk at upgrade
        time has **no** leading newline. The new record supplies the
        delimiter, so the legacy remnant terminates without a migration.
        """
        from creek.vault.writer import INDEX_FILENAME

        target_dir = tmp_path / "legacy-remnant"
        target_dir.mkdir()
        index_path = target_dir / INDEX_FILENAME
        index_path.write_bytes(b'{"filename": "old.md", "id": "frag-le')

        VaultWriter._append_index_entry(target_dir, "frag-next", "next.md")

        index = VaultWriter._load_index_file(index_path)
        assert index["frag-next"] == "next.md"
        assert set(index) == {"frag-next"}

    def test_a_legacy_format_index_still_parses_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """Old-format complete records keep parsing beside new-format ones.

        Pins the no-migration claim: the reader already skips blank
        lines, so a file mixing both framings resolves every id.
        """
        from creek.vault.writer import INDEX_FILENAME

        target_dir = tmp_path / "legacy-mixed"
        target_dir.mkdir()
        index_path = target_dir / INDEX_FILENAME
        legacy = "".join(
            json.dumps({"id": mid, "filename": f"{mid}.md"}, sort_keys=True) + "\n"
            for mid in ("frag-old-a", "frag-old-b")
        )
        index_path.write_text(legacy, encoding="utf-8")

        VaultWriter._append_index_entry(target_dir, "frag-new", "frag-new.md")

        assert VaultWriter._load_index_file(index_path) == {
            "frag-old-a": "frag-old-a.md",
            "frag-old-b": "frag-old-b.md",
            "frag-new": "frag-new.md",
        }

    def test_consecutive_torn_tails_do_not_compound(
        self,
        tmp_path: Path,
    ) -> None:
        """Two remnants in a row still cost only themselves (#1120).

        Two processes can tear back-to-back — one running pre-#1120 code
        (no leading newline), one running post-#1120 code. Neither is
        allowed to take the following healthy record down with it.
        """
        from creek.vault.writer import INDEX_FILENAME

        target_dir = tmp_path / "consecutive"
        target_dir.mkdir()
        index_path = target_dir / INDEX_FILENAME
        index_path.write_bytes(
            b'{"filename": "old.md", "id": "frag-le'
            b'\n{"filename": "newer.md", "id": "frag-ne',
        )

        VaultWriter._append_index_entry(target_dir, "frag-healthy", "healthy.md")

        index = VaultWriter._load_index_file(index_path)
        assert index["frag-healthy"] == "healthy.md"
        assert set(index) == {"frag-healthy"}

    def test_write_fragment_survives_short_writes_end_to_end(
        self,
        writer: VaultWriter,
        vault_path: Path,
        short_write: ShortWriteController,
    ) -> None:
        """The public write path keeps both the body and the index intact.

        Exercises the full blast radius of #987 in one pass: the note body
        goes through ``_atomic_create`` and the id mapping goes through
        ``_append_index_entry``, and a short write corrupts both.
        """
        import frontmatter as fm_mod

        from creek.vault.writer import INDEX_FILENAME

        fragment = Fragment(
            id="frag-short-e2e",
            title="Short write end to end",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 0, 0),
        )
        body = "x" * 400 + "TAIL"
        short_write.halve()

        path = writer.write_fragment(fragment, body=body)

        # Raw bytes first: a truncated note can lose its closing ``---``
        # delimiter, and a parser error would obscure the real failure.
        assert path.read_text(encoding="utf-8").rstrip("\n").endswith("TAIL")
        post = fm_mod.load(str(path))
        assert post.content.strip() == body
        assert post["id"] == "frag-short-e2e"
        index_path = vault_path / "01-Fragments" / "Conversations" / INDEX_FILENAME
        index = VaultWriter._load_index_file(index_path)
        assert index == {"frag-short-e2e": path.name}


class TestIndexDamageRecovery:
    """A damaged ``.id-index.jsonl`` re-resolves by scan rather than lying (#1120).

    Self-delimiting records stop a torn append from taking the *next*
    one down with it, but the torn record's own mapping is still absent.
    Before #1120 that absence was permanent: ``_load_index_locked``
    rescanned only when the index file was *missing*, so an id absent
    from a *present* index was never re-derived and ``_write_model``
    minted a second file for it. These tests pin the recovery half.
    """

    def test_damaged_index_rescans_and_recovers_the_dropped_id(
        self,
        vault_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unparseable line makes the reader re-resolve, and say so."""
        from creek.vault.writer import INDEX_FILENAME

        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        edited = _edited_fragment()
        owner_path = seed.write_fragment(edited, body="owner body")

        # Hand-write an index that names only the victim and carries a
        # torn line where the owner's record should be.
        index_path = target_dir / INDEX_FILENAME
        good = json.dumps(
            {"id": "frag-victim001", "filename": victim_path.name},
            sort_keys=True,
        )
        index_path.write_text(
            f'{good}\n{{"filename": "{owner_path.name}", "id": "frag-edi',
            encoding="utf-8",
        )

        fresh = VaultWriter(vault_path=vault_path)
        with caplog.at_level(logging.WARNING, logger="creek.vault.writer"):
            resolved = fresh._find_existing(edited.id, target_dir)

        assert resolved == owner_path
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            INDEX_FILENAME in message and "1 unparseable line" in message
            for message in messages
        ), messages

    def test_unreadable_index_rescans_instead_of_looking_empty(
        self,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An index that cannot be read is damage, not an empty directory.

        The adjacent hole #1120 exposed: a read ``OSError`` used to
        return ``{}``, which ``_load_index_locked`` then cached because
        the file *existed* — silently identical to a vault with no
        fragments, and every subsequent write a duplicate.
        """
        from pathlib import Path as RuntimePath

        from creek.vault.writer import INDEX_FILENAME

        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent

        real_read_text = RuntimePath.read_text

        def _deny_index(path: Path, *args: Any, **kwargs: Any) -> str:
            """Refuse to read the index file; pass everything else through."""
            if path.name == INDEX_FILENAME:
                raise OSError(errno.EACCES, os.strerror(errno.EACCES))
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(RuntimePath, "read_text", _deny_index)

        fresh = VaultWriter(vault_path=vault_path)
        with caplog.at_level(logging.WARNING, logger="creek.vault.writer"):
            resolved = fresh._find_existing("frag-victim001", target_dir)

        assert resolved == victim_path
        messages = [record.getMessage() for record in caplog.records]
        assert any("could not be read" in message for message in messages), messages

    def test_undecodable_index_rescans_instead_of_crashing_the_write(
        self,
        vault_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid UTF-8 in the index is damage, not an exception to the caller.

        ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``,
        so it used to escape the read guard and take down every vault
        write into the directory. Our own writers only ever emit ASCII
        (``json.dumps`` escapes non-ASCII), so a torn append cannot
        cause this — but a hand-edited or externally corrupted index
        can, and a directory scan is the right price for it.
        """
        from creek.vault.writer import INDEX_FILENAME

        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        (target_dir / INDEX_FILENAME).write_bytes(b'\n{"id": "\xff\xfe", "filen')

        fresh = VaultWriter(vault_path=vault_path)
        with caplog.at_level(logging.WARNING, logger="creek.vault.writer"):
            resolved = fresh._find_existing("frag-victim001", target_dir)

        assert resolved == victim_path
        messages = [record.getMessage() for record in caplog.records]
        assert any("could not be read" in message for message in messages), messages

    def test_clean_index_never_rescans(
        self,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An undamaged index still resolves in O(1) — no directory sweep.

        Regression guard for PERF-001: the damage-triggered rescan must
        stay triggered by damage, not paid on every cold load.
        """

        def _no_rescan(target_dir: Path) -> dict[str, str]:
            """Fail loudly instead of scanning the directory."""
            msg = f"unexpected directory rescan of {target_dir}"
            raise AssertionError(msg)

        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        monkeypatch.setattr(VaultWriter, "_rebuild_index", staticmethod(_no_rescan))

        fresh = VaultWriter(vault_path=vault_path)

        assert fresh._find_existing("frag-victim001", target_dir) == victim_path

    def test_a_torn_index_tail_does_not_mint_a_duplicate_file(
        self,
        vault_path: Path,
    ) -> None:
        """The end-to-end consequence: no second file for the same id.

        This is the harm #1120 actually causes in a vault. A torn tail
        drops a mapping; a later ``write_fragment`` for that id sees no
        entry, ``_atomic_create``s a counter-suffixed sibling, and the
        fragment now exists twice with ``update_fragment`` reaching
        neither reliably.
        """
        from creek.vault.writer import INDEX_FILENAME

        seed = VaultWriter(vault_path=vault_path)
        fragment = _edited_fragment()
        original = seed.write_fragment(fragment, body="original body")
        target_dir = original.parent
        index_path = target_dir / INDEX_FILENAME

        # Truncate the tail so the last record is torn — the on-disk
        # residue of a crash mid-append, with no exception in flight.
        index_path.write_bytes(index_path.read_bytes()[:-8])

        fresh = VaultWriter(vault_path=vault_path)
        again = fresh.write_fragment(fragment, body="original body")

        assert again == original
        assert len(list(target_dir.glob("*.md"))) == 1
        assert fresh.update_fragment(fragment, "revised body") == original

    def test_torn_repair_append_does_not_poison_the_index_it_repairs(
        self,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fourth call site: a tear during *recovery* stays contained.

        PR #1295 put an index append on the read path —
        ``_find_existing_locked`` -> ``_repair_index_locked`` -> append.
        A torn write there used to poison the very file the repair was
        fixing, swallowing the next healthy record written to it.
        """
        from creek._fsio import write_all as real_write_all
        from creek.vault import writer as writer_mod
        from creek.vault.writer import INDEX_FILENAME

        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        edited = _edited_fragment()
        owner_path = seed.write_fragment(edited, body="owner body")
        # Poison: the edited id now names the victim's file, which EXISTS
        # and declares a different id — the only shape that reaches the
        # repair append. (Deleting the file instead takes the
        # ``del index[model_id]; return None`` branch and never appends.)
        seed._append_index_entry(target_dir, edited.id, victim_path.name)

        torn = {"done": False}

        def _tear_once(fd: int, data: bytes | memoryview) -> None:
            """Half-write the first drain and fail; drain fully after that."""
            if not torn["done"]:
                torn["done"] = True
                payload = bytes(data)
                os.write(fd, payload[: max(1, len(payload) // 2)])
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            real_write_all(fd, data)

        monkeypatch.setattr(writer_mod, "write_all", _tear_once)

        fresh = VaultWriter(vault_path=vault_path)
        with pytest.raises(OSError) as excinfo:
            fresh._find_existing(edited.id, target_dir)
        assert excinfo.value.errno == errno.ENOSPC

        # A healthy append to the index the repair just tore must not be
        # swallowed by the remnant the repair left behind.
        seed._append_index_entry(target_dir, "frag-bystander", "bystander.md")

        index_path = target_dir / INDEX_FILENAME
        on_disk = VaultWriter._load_index_file(index_path)
        assert on_disk["frag-bystander"] == "bystander.md"

        second = VaultWriter(vault_path=vault_path)
        assert second._find_existing(edited.id, target_dir) == owner_path
        assert second._find_existing("frag-victim001", target_dir) == victim_path


@pytest.mark.slow
class TestVaultWriterScaling:
    """Benchmark guarding against PERF-001's quadratic ``_find_existing``."""

    def test_constant_time_per_write(
        self,
        writer: VaultWriter,
    ) -> None:
        """Per-write time stays roughly flat across 2000 writes."""
        import statistics
        import time

        durations: list[float] = []
        for i in range(2000):
            fragment = Fragment(
                id=f"frag-{i:012d}",
                title=f"t{i}",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=datetime(2025, 1, 15, 10, 0, 0),
            )
            start = time.perf_counter()
            writer.write_fragment(fragment, body=f"body {i}")
            durations.append(time.perf_counter() - start)

        early = statistics.median(durations[10:60])
        late = statistics.median(durations[-50:])
        assert late < 3 * early, f"Per-write grew: early={early:.4f}s late={late:.4f}s"


# ---- The bounded id byte-scan (#1543) ----

_SCAN_ID: Final[str] = "frag-scan000001"
"""The id every readable shape in the agreement corpus declares."""

_ID_SCAN_AGREEMENT_CORPUS: Final[dict[str, str]] = {
    "bare": f"---\nid: {_SCAN_ID}\n---\nbody\n",
    "single_quoted": f"---\nid: '{_SCAN_ID}'\n---\nbody\n",
    "double_quoted": f'---\nid: "{_SCAN_ID}"\n---\nbody\n',
    "yaml_str_tag": "---\nid: !!str 12345\n---\nbody\n",
    "block_scalar": f"---\nid: >-\n  {_SCAN_ID}\n---\nbody\n",
    "crlf": f"---\r\nid: {_SCAN_ID}\r\n---\r\nbody\r\n",
    "byte_order_mark": f"﻿---\nid: {_SCAN_ID}\n---\nbody\n",
    "fence_not_on_line_one": f"\n\n---\nid: {_SCAN_ID}\n---\nbody\n",
    "four_dash_fence": f"----\nid: {_SCAN_ID}\n----\nbody\n",
    "fence_with_trailing_space": f"---   \nid: {_SCAN_ID}\n---\nbody\n",
    "duplicate_id_keys": f"---\nid: frag-decoy00001\nid: {_SCAN_ID}\n---\nbody\n",
    "id_inside_a_block_value": (
        f"---\ntitle: |\n  id: frag-decoy00001\nid: {_SCAN_ID}\n---\nbody\n"
    ),
    "id_below_the_fence": f"---\ntitle: t\n---\nid: {_SCAN_ID}\n",
    "no_fence_at_all": f"id: {_SCAN_ID}\nbody\n",
    "unterminated_fence": f"---\nid: {_SCAN_ID}\nbody with no closing fence\n",
    "empty_frontmatter": "---\n---\nbody\n",
    "no_id_key": "---\ntitle: t\n---\nbody\n",
    "null_id": "---\nid:\n---\nbody\n",
    "int_id": "---\nid: 12345\n---\nbody\n",
    "bool_id": "---\nid: true\n---\nbody\n",
    "date_id": "---\nid: 2024-05-01\n---\nbody\n",
    "float_id": "---\nid: 1.5\n---\nbody\n",
    "trailing_comment": f"---\nid: {_SCAN_ID}  # the id\n---\nbody\n",
    "quoted_key": f'---\n"id": {_SCAN_ID}\n---\nbody\n',
    "nested_mapping_first": (
        f"---\nsource:\n  platform: claude\nid: {_SCAN_ID}\n---\nbody\n"
    ),
    "comment_line": f"---\n# a comment\nid: {_SCAN_ID}\n---\nbody\n",
    "huge_body": f"---\nid: {_SCAN_ID}\n---\n" + ("x" * 100_000),
    "escaped_double_quoted": '---\nid: "a\\tb"\n---\nbody\n',
    "blank_line_in_header": f"---\n\nid: {_SCAN_ID}\n\n---\nbody\n",
    "header_past_the_scan_bound": (
        "---\n" + ("pad: x\n" * 2000) + f"id: {_SCAN_ID}\n---\nbody\n"
    ),
    "no_space_after_the_colon": f"---\nid:{_SCAN_ID}\n---\nbody\n",
    "multiline_plain_scalar": f"---\nid:\n  {_SCAN_ID}\n---\nbody\n",
    "flow_mapping_header": f"---\n{{\n  id: {_SCAN_ID}\n}}\n---\nbody\n",
    "block_scalar_below_the_real_id": (
        f"---\nid: {_SCAN_ID}\ntitle: |\n  id: frag-decoy00001\n---\nbody\n"
    ),
    "folded_onto_the_next_line": f"---\nid: {_SCAN_ID}\n  and more\n---\nbody\n",
    "folded_across_a_blank_line": f"---\nid: {_SCAN_ID}\n\n  and more\n---\nbody\n",
}
"""Header shapes ``frontmatter.load`` reads, where the two readers must agree.

Acceptance criterion 3 of #1543: the byte-scan and ``frontmatter.load``
must still answer identically, *file for file*, on every file the latter
can read. A naive ``^id:`` regex diverges on most of these — quoted
scalars, ``!!str``, block scalars, a fence below line 1, duplicate keys
(YAML takes the last, a regex takes the first), and a bare int (which
YAML types ``int``, not ``str``).

Four shapes pin divergences a line-oriented scan invites in *both*
directions, and each one was a live defect before it was listed here:

- ``no_space_after_the_colon`` — ``id:frag-1`` is not a mapping entry at
  all; YAML reads the line as one plain scalar and the document declares
  nothing. Splitting on the bare colon had the scanner assert an identity
  the parser denies, which widens the #1083 guard against rewriting a
  stranger's file.
- ``multiline_plain_scalar`` — ``id:`` with the value indented on the next
  line. Reading the empty remainder as a null lost the file's identity,
  which is the #1543 defect reintroduced inside its own fix.
- ``flow_mapping_header`` — a header opened as a flow mapping, whose
  ``id`` sits on an *indented* line the walk skips as continuation. Only
  the "something here is not ``key: value``, so defer" arm saves it.
- ``block_scalar_below_the_real_id`` — a decoy ``id:`` inside a block
  value, placed *after* the genuine one so that last-key-wins cannot mask
  a scanner that forgets to skip indented lines.
- ``folded_onto_the_next_line`` / ``folded_across_a_blank_line`` — the
  mirror of the shape above, and the one a differential fuzz surfaces
  first: skipping indented lines is right for a nested mapping and wrong
  for a plain scalar folded over two lines, where it truncates the id to
  its first line. A blank line between them folds to a newline rather
  than ending the scalar, so it is not an escape from the rule.
"""

_ID_SCAN_UNREADABLE_CORPUS: Final[dict[str, str]] = {
    "bare_date_key": (
        f"---\nid: {_SCAN_ID}\ntype: fragment\n2024-05-01: reflection\n---\nbody\n"
    ),
    "bool_key": f"---\nid: {_SCAN_ID}\ntype: fragment\ntrue: yes\n---\nbody\n",
    "int_key": f"---\nid: {_SCAN_ID}\ntype: fragment\n1: x\n---\nbody\n",
    "invalid_yaml": (f"---\nid: {_SCAN_ID}\ntitle: [unterminated\n---\nbody\n"),
}
"""Headers ``frontmatter.load`` cannot read, whose ``id`` line is still plain.

These are the files #1543 is about: the id is sitting in the header in
readable text, and the verifier used to answer "no id here" because a
*different* line of the same header would not parse.
"""


def _expected_declared_id(path: Path) -> str | None:
    """Return what ``frontmatter.load`` says *path*'s ``id`` is, str-typed only."""
    declared = frontmatter.load(str(path)).get("id")
    return declared if isinstance(declared, str) else None


class TestDeclaredIdByteScan:
    """The bounded byte-scan that replaces ``frontmatter.load`` in the verifier.

    ``_file_declares_id`` and ``_rebuild_index`` are bound by an explicit
    contract — "a file this function rejects is exactly a file the rebuild
    would decline to index" — so they are routed through one helper and can
    never disagree by construction. What these tests pin is the *other* half:
    that the helper agrees with ``frontmatter.load`` wherever ``frontmatter.load``
    has an answer, and answers where it does not.
    """

    @pytest.mark.parametrize("shape", sorted(_ID_SCAN_AGREEMENT_CORPUS))
    def test_byte_scan_agrees_with_frontmatter_load_on_every_readable_shape(
        self,
        tmp_path: Path,
        shape: str,
    ) -> None:
        """The byte-scan answers exactly what a str-typed ``frontmatter.load`` does."""
        from creek.vault.writer import _declared_id

        note = tmp_path / f"{shape}.md"
        note.write_bytes(_ID_SCAN_AGREEMENT_CORPUS[shape].encode("utf-8"))

        assert _declared_id(note) == _expected_declared_id(note)

    def test_the_agreement_corpus_exercises_both_answers(self, tmp_path: Path) -> None:
        """Positive control: the corpus is neither all-``None`` nor all-found.

        An agreement assertion over a corpus where every shape resolves to
        ``None`` passes for any implementation that always returns ``None``.
        """
        answers = {}
        for shape, text in _ID_SCAN_AGREEMENT_CORPUS.items():
            note = tmp_path / f"{shape}.md"
            note.write_bytes(text.encode("utf-8"))
            answers[shape] = _expected_declared_id(note)

        assert len(answers) == len(_ID_SCAN_AGREEMENT_CORPUS)
        assert sum(1 for value in answers.values() if value is not None) >= 10
        assert sum(1 for value in answers.values() if value is None) >= 5

    @pytest.mark.parametrize("shape", sorted(_ID_SCAN_UNREADABLE_CORPUS))
    def test_byte_scan_reads_an_id_frontmatter_load_cannot(
        self,
        tmp_path: Path,
        shape: str,
    ) -> None:
        """A header line that will not parse no longer hides the ``id`` beside it."""
        from creek.vault.reader import FRONTMATTER_LOAD_ERRORS
        from creek.vault.writer import _declared_id

        note = tmp_path / f"{shape}.md"
        note.write_bytes(_ID_SCAN_UNREADABLE_CORPUS[shape].encode("utf-8"))

        # Positive control: this really is a file the old reader cannot read,
        # and it fails with exactly the exception set the verifier swallowed.
        with pytest.raises(FRONTMATTER_LOAD_ERRORS):
            frontmatter.load(str(note))

        assert _declared_id(note) == _SCAN_ID

    def test_byte_scan_declines_an_id_that_is_itself_unparseable(
        self,
        tmp_path: Path,
    ) -> None:
        """A broken ``id`` line stays "no id", rather than yielding raw YAML text."""
        from creek.vault.writer import _declared_id

        note = tmp_path / "broken-id.md"
        note.write_text(
            "---\nid: [unterminated\ntitle: Broken\n---\nbroken body\n",
            encoding="utf-8",
        )

        assert _declared_id(note) is None

    def test_byte_scan_reads_a_bounded_prefix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The scan reads a bounded head, not the whole fragment.

        Landmine 2 of #1543: the obvious fix — routing the verifier through
        ``creek.vault.links.read_header_meta`` — is a measured 7.4x regression
        on a per-index-hit path (94.1 us for ``frontmatter.load`` against
        695.7 us; the byte-scan is 29.9 us). The byte-scan is only a win while
        it stays bounded, and a wall-clock assertion on a shared CI runner
        would be a flake generator — so the *bound* is asserted directly, in
        bytes, which is the property the speed follows from.
        """
        import pathlib

        from creek.vault.writer import _ID_SCAN_HEAD_BYTES, _declared_id

        note = tmp_path / "huge.md"
        body = "x" * (_ID_SCAN_HEAD_BYTES * 40)
        note.write_text(f"---\nid: {_SCAN_ID}\n---\n{body}", encoding="utf-8")
        assert note.stat().st_size > _ID_SCAN_HEAD_BYTES * 30  # positive control

        read_sizes: list[int] = []
        real_open = pathlib.Path.open

        class _CountingHandle:
            """A binary handle that records how many bytes each read returned."""

            def __init__(self, inner: Any) -> None:
                """Wrap *inner*, the real file object."""
                self._inner = inner

            def read(self, *args: Any) -> bytes:
                """Delegate the read and record the size of the result."""
                data: bytes = self._inner.read(*args)
                read_sizes.append(len(data))
                return data

            def __enter__(self) -> _CountingHandle:
                """Enter the wrapped handle's context."""
                self._inner.__enter__()
                return self

            def __exit__(self, *args: Any) -> None:
                """Exit the wrapped handle's context."""
                self._inner.__exit__(*args)

        def _counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            """Stand in for ``Path.open``, wrapping the handle in a counter."""
            return _CountingHandle(real_open(self, *args, **kwargs))

        monkeypatch.setattr(pathlib.Path, "open", _counting_open)

        assert _declared_id(note) == _SCAN_ID

        assert read_sizes, "the scan never opened the file"
        assert max(read_sizes) <= _ID_SCAN_HEAD_BYTES
        assert sum(read_sizes) <= _ID_SCAN_HEAD_BYTES

    def test_the_word_scalar_screen_agrees_with_pyyaml_word_for_word(self) -> None:
        """The bool/null word list is checked against the parser, not remembered.

        ``y`` and ``n`` are the trap, and a hand-written list walked into it:
        they *look* like the YAML 1.1 booleans and are not — PyYAML types both
        ``str``. Screening them out would make the byte-scan answer ``None``
        where ``frontmatter.load`` answers ``"y"``, which is the file-for-file
        disagreement #1543's third acceptance criterion forbids. Asserted
        against ``yaml.safe_load`` in both directions so neither a missing
        spelling nor a spurious one can survive.
        """
        import yaml

        from creek.vault.writer import (
            _PLAIN_STRING_SCALAR_RE,
            _YAML_WORD_SCALARS,
        )

        probes = [
            "y",
            "Y",
            "n",
            "N",
            "yes",
            "Yes",
            "YES",
            "no",
            "No",
            "NO",
            "true",
            "True",
            "TRUE",
            "false",
            "False",
            "FALSE",
            "on",
            "On",
            "ON",
            "off",
            "Off",
            "OFF",
            "null",
            "Null",
            "NULL",
            "NaN",
            "inf",
            "Inf",
            "nulls",
            "Yes_no",
            "frag-abc000001",
        ]
        admitted = [word for word in probes if _PLAIN_STRING_SCALAR_RE.match(word)]

        # Positive control: the regex really does admit these words, so the
        # word list is the only thing standing between them and being read
        # as ids.
        assert len(admitted) >= len(_YAML_WORD_SCALARS)

        for word in admitted:
            parsed = yaml.safe_load(f"k: {word}")["k"]
            screened = word in _YAML_WORD_SCALARS
            assert screened is not isinstance(parsed, str), (
                f"{word!r}: screened={screened} but YAML gave {type(parsed).__name__}"
            )

        # And no member is unreachable: every entry is a word the regex admits,
        # so the set carries no spelling that could never have been consulted.
        assert set(admitted) >= _YAML_WORD_SCALARS

    def test_byte_scan_declines_an_unclosed_quoted_scalar(
        self,
        tmp_path: Path,
    ) -> None:
        """An ``id`` whose quote never closes is deferred, not half-read.

        ``id: "frag-a`` is a YAML error, so the honest answer is the one the
        parser gives — no id. Reporting ``frag-a`` by stripping the leading
        quote would be the scanner inventing an identity out of a broken line,
        which is the failure mode a hand-rolled reader exists to avoid.
        """
        from creek.vault.writer import _declared_id

        note = tmp_path / "unclosed.md"
        note.write_text(f'---\nid: "{_SCAN_ID}\n---\nbody\n', encoding="utf-8")

        with pytest.raises(FRONTMATTER_LOAD_ERRORS):
            frontmatter.load(str(note))
        assert _declared_id(note) is None

    def test_byte_scan_returns_none_for_an_unreadable_path(
        self,
        tmp_path: Path,
    ) -> None:
        """An ``OSError`` answers "no id", exactly as the guarded load did."""
        from creek.vault.writer import _declared_id

        missing = tmp_path / "nope" / "gone.md"

        assert _declared_id(missing) is None


def _raw_index_records(target_dir: Path, model_id: str) -> list[dict[str, Any]]:
    """Return the ``.id-index.jsonl`` records naming *model_id*, read as raw bytes.

    Never goes through ``_load_index_file`` or ``find_fragment``. The writer
    *self-heals* a bad index on lookup — ``_find_existing_locked`` repairs a
    mismatch and ``_load_index_locked`` re-derives a damaged file by scan — so
    an assertion made through a reader is vacuous by construction: it measures
    the repair, not the file the repair was meant to avoid needing.
    """
    from creek.vault.writer import INDEX_FILENAME

    records: list[dict[str, Any]] = []
    raw = (target_dir / INDEX_FILENAME).read_bytes().decode("utf-8")
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("id") == model_id:
            records.append(entry)
    return records


class TestPoisonedHeaderIdentity:
    """A header the YAML parser rejects must not cost the file its identity (#1543).

    The index is the vault's identity oracle. Before this fix, one
    hand-edited note with a bare-date frontmatter key — ``2024-05-01:``,
    valid ``SafeLoader`` output and entirely plausible in an Obsidian vault —
    made ``frontmatter.load`` raise ``TypeError: keywords must be strings``,
    which the verifier swallowed as "this file declares no id". The file was
    then invisible to every lifecycle path at once, and the next write minted
    a second file carrying the same id while the index moved to point at the
    duplicate. That is silent data loss, not a cosmetic lookup miss.
    """

    @pytest.mark.parametrize(
        "poison",
        ["2024-05-01: reflection\n", "true: yes\n", "1: x\n", "title: [unterminated\n"],
    )
    def test_a_poisoned_header_does_not_mint_a_duplicate(
        self,
        vault_path: Path,
        poison: str,
    ) -> None:
        """The id still resolves to its own file, and no sibling is created."""
        edited = _edited_fragment()
        seed = VaultWriter(vault_path=vault_path)
        written = seed.write_fragment(edited, body="the original body")
        target_dir = written.parent
        written.write_text(
            f"---\nid: {edited.id}\ntype: fragment\n{poison}---\nthe original body\n",
            encoding="utf-8",
        )
        before = _raw_index_records(target_dir, edited.id)
        assert len(before) == 1  # positive control: one mapping to start with.

        fresh = VaultWriter(vault_path=vault_path)
        result = fresh.write_fragment(edited, body="a second body")

        assert result == written
        assert sorted(path.name for path in target_dir.glob("*.md")) == [written.name]
        after = _raw_index_records(target_dir, edited.id)
        assert len(after) == 1, f"the index gained a second record: {after}"
        assert after[0]["filename"] == written.name

    def test_a_poisoned_header_still_resolves_through_every_lifecycle_path(
        self,
        vault_path: Path,
    ) -> None:
        """``find`` locates it; ``update``/``tomb``/``restore`` name it when they fail.

        The four paths used to answer ``None`` in unison, each one silently
        declining to act on a file that is sitting right there declaring the
        id. They now find it — and the three that need the *body* fail loudly
        through :func:`creek.vault.reader.load_post_or_raise`, naming the path
        so the operator can repair the header instead of hunting for it.
        """
        edited = _edited_fragment()
        seed = VaultWriter(vault_path=vault_path)
        written = seed.write_fragment(edited, body="the original body")
        written.write_text(
            f"---\nid: {edited.id}\ntype: fragment\n2024-05-01: reflection\n"
            "---\nthe original body\n",
            encoding="utf-8",
        )

        fresh = VaultWriter(vault_path=vault_path)

        assert fresh.find_fragment(edited.id) == written
        with pytest.raises(ValueError, match=re.escape(str(written))):
            fresh.update_fragment(edited, body="new body")

    def test_the_scanner_and_the_verifier_agree_on_a_poisoned_file(
        self,
        vault_path: Path,
    ) -> None:
        """Acceptance criterion 3: the two readers answer identically.

        ``_file_declares_id`` and ``_rebuild_index`` carry an explicit
        contract — "a file this function rejects is exactly a file the rebuild
        would decline to index". A fix that taught only the verifier to read a
        poisoned header would break it: the verifier would accept the file and
        a later rebuild would still drop it, so the id would flip between
        resolved and unresolved depending on which reader ran.
        """
        from creek.vault.writer import _file_declares_id

        edited = _edited_fragment()
        seed = VaultWriter(vault_path=vault_path)
        written = seed.write_fragment(edited, body="the original body")
        target_dir = written.parent
        written.write_text(
            f"---\nid: {edited.id}\ntype: fragment\n2024-05-01: reflection\n"
            "---\nthe original body\n",
            encoding="utf-8",
        )

        rebuilt = VaultWriter._rebuild_index(target_dir)

        assert _file_declares_id(written, edited.id) is True
        assert rebuilt.get(edited.id) == written.name


class TestStalledIndexAppend:
    """A lost index append must not become a duplicate fragment (#1299).

    ``_append_index_entry`` opens with ``O_CREAT`` before it writes, so a
    stalled drain leaves an index file that is *present, parseable and
    undamaged* — it simply lacks one mapping. Every recovery
    ``_load_index_locked`` owns keys off ``.damaged``, which such a file is
    not, so no rescan fires: the id resolves to "not indexed", the next write
    mints a sibling, and the fresh append then points the index at the
    sibling. The original file is orphaned permanently.
    """

    @staticmethod
    def _stall_the_next_append(
        monkeypatch: pytest.MonkeyPatch,
        short_write: ShortWriteController,
    ) -> None:
        """Arm ``short_write.stall()`` around exactly one index append.

        Stalling for the whole write would take down ``_atomic_create``'s own
        drain and leave no fragment file at all, which is a different failure.
        The case #1299 is about is the one where the note reached disk and only
        its index line did not.
        """
        real_append = VaultWriter._append_index_entry
        armed = [True]

        def _appending(target_dir: Path, model_id: str, filename: str) -> None:
            """Delegate to the real append, stalling the first call only."""
            if not armed[0]:
                real_append(target_dir, model_id, filename)
                return
            armed[0] = False
            short_write.stall()
            try:
                real_append(target_dir, model_id, filename)
            finally:
                short_write.passthrough()

        monkeypatch.setattr(
            VaultWriter, "_append_index_entry", staticmethod(_appending)
        )

    def test_a_stalled_append_does_not_mint_a_duplicate(
        self,
        vault_path: Path,
        short_write: ShortWriteController,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The id whose mapping was lost re-attaches to its own file.

        The stall is staged against a **non-empty** index on purpose. A
        zero-byte index is the easy case and invites the fix "treat an empty
        index as absent and rebuild", which passes such a test and does
        nothing for the real one: a stall on the *N*-th append leaves an
        (N-1)-entry index that is perfectly valid and simply short one line.
        """
        seed = VaultWriter(vault_path=vault_path)
        first = _seed_victim(seed)
        target_dir = first.parent
        edited = _edited_fragment()
        assert len(_raw_index_records(target_dir, "frag-victim001")) == 1

        self._stall_the_next_append(monkeypatch, short_write)
        with pytest.raises(OSError) as excinfo:
            seed.write_fragment(edited, body="the original body")
        assert excinfo.value.errno == errno.EIO

        # The note reached disk; only its index line did not. The index is
        # present, parses cleanly, and is *not* damaged — so nothing rescans.
        survivor = next(
            path for path in target_dir.glob("*.md") if path.name != first.name
        )
        assert _raw_index_records(target_dir, edited.id) == []
        assert len(_raw_index_records(target_dir, "frag-victim001")) == 1

        fresh = VaultWriter(vault_path=vault_path)
        result = fresh.write_fragment(edited, body="a second body")

        assert result == survivor
        assert sorted(path.name for path in target_dir.glob("*.md")) == sorted(
            [first.name, survivor.name],
        )
        recovered = _raw_index_records(target_dir, edited.id)
        assert len(recovered) == 1, f"expected one mapping, got {recovered}"
        assert recovered[0]["filename"] == survivor.name

    def test_a_stem_collision_between_different_ids_still_suffixes(
        self,
        vault_path: Path,
    ) -> None:
        """Two ids sharing a filename stem keep getting distinct files.

        The counterweight to the test above: the collision retry exists
        because two *different* fragments can compute the same
        ``{date}-{title}`` stem, and reusing the first file for the second
        would be the very data loss this class is trying to prevent — in the
        opposite direction.
        """
        seed = VaultWriter(vault_path=vault_path)
        shared = {
            "title": "Same Title",
            "source": FragmentSource(platform=SourcePlatform.CLAUDE),
            "created": datetime(2025, 1, 15, 10, 30, 0),
        }
        first = seed.write_fragment(
            Fragment(id="frag-collide001", **shared),
            body="one",
        )
        second = seed.write_fragment(
            Fragment(id="frag-collide002", **shared),
            body="two",
        )

        assert first != second
        assert second.name.endswith("-1.md")

    def test_relocation_still_stacks_suffixes_for_a_colliding_tombstone(
        self,
        vault_path: Path,
    ) -> None:
        """``_relocate_fragment_locked`` keeps the un-verified create path.

        Tombstones from different ``01-Fragments/<platform>/`` subfolders land
        in one flat sink, so a stem collision there is between *strangers* and
        must stack a suffix. That caller passes no expected id, which is what
        keeps the verification confined to the duplicate-detection path.
        """
        seed = VaultWriter(vault_path=vault_path)
        orphan_dir = vault_path / "10-Liminal" / "Orphaned"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        squatter = orphan_dir / "2025-01-15-Victim.md"
        squatter.write_text(
            "---\nid: frag-squatter01\ntype: fragment\n---\nsquatter\n",
            encoding="utf-8",
        )
        _seed_victim(seed)

        tombed = seed.tomb_fragment("frag-victim001")

        assert tombed is not None
        assert tombed != squatter
        assert tombed.name.endswith("-1.md")
        assert squatter.read_text(encoding="utf-8").startswith(
            "---\nid: frag-squatter01"
        )


class TestConcurrentRelocation:
    """Tomb and restore must serialise on the index lock, like every write (#1611).

    ``_write_model`` and ``_update_existing`` take
    ``vault_lock(<dir>/.id-index.lock)`` around their check-then-act; the
    relocation paths took only ``self._lock``, which belongs to *one*
    ``VaultWriter`` instance. ``run_ingest`` builds a fresh writer per call and
    the ``/v1`` routes each get their own, so two overlapping tombs of one id
    never shared a lock at all — each located the same live file, each created
    its own tombstone in the flat orphan sink, and the loser's ``unlink``
    then failed on a file the winner had already moved.
    """

    @staticmethod
    def _wedge_after_locating(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[threading.Event, threading.Event]:
        """Hold the *first* relocation between locating its file and moving it.

        The wedge sits on ``load_post_or_raise``, which every relocation calls
        after it has resolved the id and before it creates the destination —
        i.e. squarely inside the window a directory lock has to cover. Putting
        it there is what makes the test decide the lock question rather than
        the scheduler: without the lock the second caller sails through, with
        it the second caller blocks.

        Returns:
            ``(inside, release)`` — set by the wedged caller when it is in the
            window, and set by the test when it may proceed.
        """
        from creek.vault import writer as writer_module

        inside = threading.Event()
        release = threading.Event()
        real_load = writer_module.load_post_or_raise
        armed = [True]

        def _wedged(path: Path) -> Any:
            """Load as usual, pausing the first caller inside the window."""
            post = real_load(path)
            if armed[0]:
                armed[0] = False
                inside.set()
                release.wait(timeout=_WEDGE_TIMEOUT_SECONDS)
            return post

        monkeypatch.setattr(writer_module, "load_post_or_raise", _wedged)
        return inside, release

    def test_two_writers_tombing_one_id_produce_one_tombstone(
        self,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Overlapping tombs of one fragment leave one tombstone and no error."""
        seed = VaultWriter(vault_path=vault_path)
        victim = _seed_victim(seed)
        origin_dir = victim.parent
        orphan_dir = vault_path / "10-Liminal" / "Orphaned"
        first = VaultWriter(vault_path=vault_path)
        second = VaultWriter(vault_path=vault_path)

        inside, release = self._wedge_after_locating(monkeypatch)
        results: dict[str, Path | None] = {}
        errors: dict[str, BaseException] = {}

        def _tomb(label: str, actor: VaultWriter) -> None:
            """Run one tomb, recording its outcome instead of raising."""
            try:
                results[label] = actor.tomb_fragment("frag-victim001")
            except BaseException as exc:  # recorded, then re-asserted
                errors[label] = exc

        leader = threading.Thread(target=_tomb, args=("leader", first))
        leader.start()
        assert inside.wait(timeout=_WEDGE_TIMEOUT_SECONDS), "the wedge never armed"

        follower = threading.Thread(target=_tomb, args=("follower", second))
        follower.start()
        # Wait for the follower to *do* something rather than for a clock. An
        # unserialised follower relocates the file, so a tombstone appears and
        # the wait ends at once; a serialised one is blocked on the index lock
        # and there is nothing to wait for, so the ceiling elapses.
        deadline = time.monotonic() + _OVERLAP_CEILING_SECONDS
        while time.monotonic() < deadline and not any(orphan_dir.glob("*.md")):
            time.sleep(_POLL_SECONDS)
        release.set()
        leader.join(timeout=_WEDGE_TIMEOUT_SECONDS)
        follower.join(timeout=_WEDGE_TIMEOUT_SECONDS)

        assert not leader.is_alive()
        assert not follower.is_alive()
        assert errors == {}, f"a relocation raised: {errors}"
        tombstones = _files_declaring(orphan_dir, "frag-victim001")
        assert len(tombstones) == 1, f"two tombstones for one id: {tombstones}"
        assert _files_declaring(origin_dir, "frag-victim001") == []
        assert sorted(results) == ["follower", "leader"]
        assert [path for path in results.values() if path is not None] == tombstones

    @staticmethod
    def _record_lock_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Record the vault-lock paths, in acquisition order, as vault-relative text."""
        from creek.vault import writer as writer_module

        real_lock = writer_module.vault_lock
        taken: list[str] = []

        @contextlib.contextmanager
        def _recording(lock_path: Path, **kwargs: Any) -> Any:
            """Record the lock, then hold it for real."""
            taken.append(str(lock_path))
            with real_lock(lock_path, **kwargs):
                yield

        monkeypatch.setattr(writer_module, "vault_lock", _recording)
        return taken

    def test_a_restore_takes_its_two_locks_in_resolved_path_order(
        self,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both keys, lowest path first — the same order in both directions.

        A restore moves ``10-Liminal/Orphaned`` -> ``01-Fragments/...`` and a
        tomb moves the other way, so an implementation that locked
        origin-then-destination would take the same pair in opposite orders
        and two overlapping callers could each hold the key the other needs.
        Sorting by resolved path makes the order a property of the *vault*,
        not of the direction of travel — which is why this asserts against a
        restore, where sorted order is the reverse of the argument order.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim = _seed_victim(seed)
        origin_dir = victim.parent
        restorable = Fragment(
            id="frag-victim001",
            title="Victim",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 30, 0),
            privacy_tier=PrivacyTier.INTIMATE,
        )
        assert seed.tomb_fragment("frag-victim001") is not None

        taken = self._record_lock_order(monkeypatch)
        assert seed.restore_fragment(restorable) is not None

        assert len(taken) == 2, taken
        assert taken == sorted(taken)
        # Positive control: sorted order really is the *reverse* of the order
        # a naive origin-then-destination implementation would have used.
        assert taken[0].startswith(str(origin_dir))

    def test_a_lookup_creates_no_lock_file_and_no_directory(
        self,
        vault_path: Path,
    ) -> None:
        """Asking where a fragment is stays genuinely read-only (#1332).

        ``vault_lock`` creates its lock file *and* ``mkdir``s the parent, and
        ``_fragment_search_dirs`` deliberately visits declared-but-absent
        platform subfolders. Taking the lock around the search — rather than
        after something has been located — would therefore materialise a
        directory and a ``.id-index.lock`` in the operator's vault for every
        declared platform, on a lookup that found nothing.

        ``restore_fragment`` is asserted beside the other three because it is
        the path where the guard is easiest to drop: its destination directory
        is computed from the *fragment* rather than from anything located, so
        an implementation that took both keys before probing would ``mkdir``
        the target platform folder and lay a lock file in it for a fragment
        that was never tombed.
        """
        seed = VaultWriter(vault_path=vault_path)
        _seed_victim(seed)
        for stale in vault_path.rglob(INDEX_LOCK_FILENAME):
            stale.unlink()
        before = sorted(path for path in vault_path.rglob("*") if path.is_dir())
        never_tombed = Fragment(
            id="frag-nowhere0001",
            title="Nowhere",
            source=FragmentSource(platform=SourcePlatform.CHATGPT),
            created=datetime(2025, 1, 15, 10, 30, 0),
        )

        probe = VaultWriter(vault_path=vault_path)

        assert probe.find_fragment("frag-nowhere0001") is None
        assert probe.find_tombed_fragment("frag-nowhere0001") is None
        assert probe.tomb_fragment("frag-nowhere0001") is None
        assert probe.restore_fragment(never_tombed) is None
        assert list(vault_path.rglob(INDEX_LOCK_FILENAME)) == []
        assert sorted(path for path in vault_path.rglob("*") if path.is_dir()) == before


class TestIndexLockSpelling:
    """``_index_locks`` orders and de-duplicates on the *resolved* path (#1611).

    The helper's whole job is to make two callers moving a fragment in
    opposite directions take the same pair of keys in the same sequence. That
    only holds if the sort key is a canonical name for the directory: a vault
    reached as ``/vault/01-Fragments`` and as ``/vault/10-Liminal/../01-Fragments``
    is one directory with one ``flock``, but two strings, and two strings sort
    independently of what they denote.

    Asserting through ``tomb_fragment`` cannot see this — both of its paths are
    already spelled canonically, so ``.resolve()`` is a no-op there and can be
    deleted with the whole suite still green. These probe the helper directly.
    """

    @staticmethod
    def _record_lock_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Record every ``vault_lock`` path without taking a real lock.

        The real lock is deliberately *not* delegated to. A regression that
        stops collapsing two spellings of one directory takes ``flock`` on the
        same file twice from one process, which blocks rather than fails — and
        a test that hangs reports nothing. Recording turns that deadlock into
        a visible extra entry.
        """
        from creek.vault import writer as writer_module

        taken: list[str] = []

        @contextlib.contextmanager
        def _recording(lock_path: Path, **_kwargs: Any) -> Any:
            """Record the requested lock and yield without acquiring it."""
            taken.append(str(lock_path))
            yield

        monkeypatch.setattr(writer_module, "vault_lock", _recording)
        return taken

    def test_locks_are_ordered_by_resolved_path_not_by_spelling(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two spellings sort by what they denote, not by how they are written."""
        from creek.vault.writer import _index_locks

        for name in ("a", "m", "z"):
            (tmp_path / name).mkdir()
        zed_via_a = tmp_path / "a" / ".." / "z" / INDEX_LOCK_FILENAME
        emm = tmp_path / "m" / INDEX_LOCK_FILENAME

        # Positive controls: the raw spellings sort one way and the resolved
        # paths the other, so the two orderings are genuinely distinguishable.
        assert sorted([str(zed_via_a), str(emm)]) == [str(zed_via_a), str(emm)]
        assert zed_via_a.resolve(strict=False) > emm.resolve(strict=False)

        taken = self._record_lock_calls(monkeypatch)
        with _index_locks(zed_via_a, emm):
            pass

        assert taken == [str(emm), str(zed_via_a)]

    def test_two_spellings_of_one_directory_take_one_lock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The de-duplication the docstring promises is on the resolved path.

        ``vault_lock`` is explicitly non-reentrant, so a caller that named one
        directory twice would self-deadlock rather than raise — the failure
        mode that ships.
        """
        from creek.vault.writer import _index_locks

        (tmp_path / "d" / "sub").mkdir(parents=True)
        plain = tmp_path / "d" / INDEX_LOCK_FILENAME
        round_about = tmp_path / "d" / "sub" / ".." / INDEX_LOCK_FILENAME
        assert str(plain) != str(round_about)  # positive control

        taken = self._record_lock_calls(monkeypatch)
        with _index_locks(plain, round_about):
            pass

        assert taken == [str(plain)]


# ---- Every index writer takes the directory lock (#1621) ----


@contextlib.contextmanager
def _index_lock_held_elsewhere(lock_path: Path) -> Iterator[None]:
    """Hold *lock_path* on a helper thread for the body of the block.

    ``vault_lock`` fronts ``flock`` with a plain :class:`threading.Lock`, so
    the holder has to be a *different* thread for the lock to read as taken
    to the code under test — taking it on this thread would make the
    assertions measure re-entrancy instead of exclusion.

    Args:
        lock_path: The ``.id-index.lock`` to hold.

    Yields:
        Nothing. The block runs with the lock held by the helper thread.
    """
    held = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def _hold() -> None:
        """Take the lock, announce it, and keep it until released."""
        try:
            with vault_lock(lock_path):
                held.set()
                release.wait(timeout=_WEDGE_TIMEOUT_SECONDS)
        except BaseException as exc:  # recorded, then re-asserted
            failures.append(exc)
            held.set()

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert held.wait(timeout=_WEDGE_TIMEOUT_SECONDS), "the lock holder never armed"
        assert failures == [], f"the lock holder raised: {failures}"
        yield
    finally:
        release.set()
        holder.join(timeout=_WEDGE_TIMEOUT_SECONDS)


def _spawn(call: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    """Run *call* on its own thread, recording the outcome instead of raising.

    Args:
        call: The zero-argument callable to run.

    Returns:
        ``(thread, record)``. The record carries ``done`` once the call has
        returned or raised, plus ``result`` or ``error``.
    """
    record: dict[str, Any] = {}

    def _run() -> None:
        """Invoke the callable, recording whichever way it ends."""
        try:
            record["result"] = call()
        except BaseException as exc:  # recorded, then re-asserted
            record["error"] = exc
        finally:
            record["done"] = True

    thread = threading.Thread(target=_run)
    thread.start()
    return thread, record


def _wait_for_an_effect(record: dict[str, Any]) -> None:
    """Wait until the spawned call finishes, or the overlap ceiling elapses.

    Not a sleep: an unserialised caller finishes in milliseconds and ends the
    wait at once. Only the serialised case pays the ceiling, because there is
    nothing to see while it is blocked on the lock.

    Args:
        record: The record returned by :func:`_spawn`.
    """
    deadline = time.monotonic() + _OVERLAP_CEILING_SECONDS
    while time.monotonic() < deadline and not record.get("done"):
        time.sleep(_POLL_SECONDS)


def _index_records(index_path: Path) -> list[str]:
    """Return the non-blank physical records *index_path* holds.

    Args:
        index_path: The ``.id-index.jsonl`` file to count.

    Returns:
        Every non-blank line, so a test can assert on *records on disk*
        rather than on the mapping they collapse to — which is the whole
        difference compaction makes.
    """
    raw = index_path.read_text(encoding="utf-8")
    return [line for line in raw.splitlines() if line.strip()]


class TestIndexWritesTakeTheDirectoryLock:
    """No ``.id-index.jsonl`` write happens outside ``vault_lock`` (#1621).

    Two writers used to escape the directory key, and both were reached
    from *lookups*: ``_repair_index_locked``'s later-wins append, and the
    whole-file ``_persist_full_index`` in ``_load_index_locked``'s
    missing-file branch. The second is the worse of the pair — a
    rename-into-place from one process's in-memory snapshot can replace a
    file another process just rewrote, where a lost append costs one line.
    Both had to be closed before #1300's compaction pass could be correct.
    """

    @staticmethod
    def _poison(seed: VaultWriter, target_dir: Path, model_id: str, name: str) -> bytes:
        """Point *model_id* at *name* on disk and return the index bytes after.

        Args:
            seed: The writer whose append helper writes the line.
            target_dir: The directory whose index is being poisoned.
            model_id: The id to mis-map.
            name: The filename of a file that declares a *different* id.

        Returns:
            The whole index file as bytes, so a later comparison can prove
            no further byte was written.
        """
        seed._append_index_entry(target_dir, model_id, name)
        return (target_dir / INDEX_FILENAME).read_bytes()

    def test_a_repair_append_waits_for_the_directory_lock(
        self,
        vault_path: Path,
    ) -> None:
        """A mismatch repair may not append while another holder has the key."""
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        edited = _edited_fragment()
        owner_path = seed.write_fragment(edited, body="owner body")
        before = self._poison(seed, target_dir, edited.id, victim_path.name)
        index_path = target_dir / INDEX_FILENAME

        fresh = VaultWriter(vault_path=vault_path)
        with _index_lock_held_elsewhere(VaultWriter._index_lock_path(target_dir)):
            thread, record = _spawn(
                lambda: fresh._find_existing(edited.id, target_dir),
            )
            _wait_for_an_effect(record)
            assert not record.get("done"), "the repair ran without the index lock"
            assert index_path.read_bytes() == before

        thread.join(timeout=_WEDGE_TIMEOUT_SECONDS)
        assert not thread.is_alive()
        assert record.get("error") is None, record.get("error")
        assert record["result"] == owner_path
        assert index_path.read_bytes() != before

    def test_a_cold_index_persist_waits_for_the_directory_lock(
        self,
        vault_path: Path,
    ) -> None:
        """A lookup over an index-less directory may not rewrite it unlocked.

        The writer #1621's own text does not know about: with no
        ``.id-index.jsonl`` present, a *read-only* ``find_fragment`` rebuilt
        the mapping by scan and persisted it with ``_atomic_write_text`` —
        a temp-file rename into place, holding only ``self._lock``.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        index_path = target_dir / INDEX_FILENAME
        index_path.unlink()

        fresh = VaultWriter(vault_path=vault_path)
        with _index_lock_held_elsewhere(VaultWriter._index_lock_path(target_dir)):
            thread, record = _spawn(
                lambda: fresh.find_fragment("frag-victim001"),
            )
            _wait_for_an_effect(record)
            assert not record.get("done"), "a lookup rewrote the index unlocked"
            assert not index_path.exists()

        thread.join(timeout=_WEDGE_TIMEOUT_SECONDS)
        assert not thread.is_alive()
        assert record.get("error") is None, record.get("error")
        assert record["result"] == victim_path
        assert index_path.exists()

    def test_a_tomb_probe_repair_waits_for_the_directory_lock(
        self,
        vault_path: Path,
    ) -> None:
        """``tomb_fragment``'s deliberately-unlocked probe repairs under the key.

        The probe stays outside every vault lock (#1332/#1611) so a miss
        litters nothing — but the moment it *does* have to write, it has to
        take the key for the directory it is writing to.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        edited = _edited_fragment()
        seed.write_fragment(edited, body="owner body")
        before = self._poison(seed, target_dir, edited.id, victim_path.name)
        index_path = target_dir / INDEX_FILENAME

        fresh = VaultWriter(vault_path=vault_path)
        with _index_lock_held_elsewhere(VaultWriter._index_lock_path(target_dir)):
            thread, record = _spawn(lambda: fresh.tomb_fragment(edited.id))
            _wait_for_an_effect(record)
            assert not record.get("done"), "the tomb probe repaired unlocked"
            assert index_path.read_bytes() == before

        thread.join(timeout=_WEDGE_TIMEOUT_SECONDS)
        assert not thread.is_alive()
        assert record.get("error") is None, record.get("error")
        tombed = record["result"]
        assert tombed is not None
        assert tombed.parent == vault_path / "10-Liminal" / "Orphaned"

    def test_a_restore_probe_repair_waits_for_the_directory_lock(
        self,
        vault_path: Path,
    ) -> None:
        """``restore_fragment``'s probe repairs the orphan index under the key."""
        seed = VaultWriter(vault_path=vault_path)
        _seed_victim(seed)
        edited = _edited_fragment()
        seed.write_fragment(edited, body="owner body")
        tombed_victim = seed.tomb_fragment("frag-victim001")
        assert tombed_victim is not None
        assert seed.tomb_fragment(edited.id) is not None
        orphan_dir = tombed_victim.parent
        before = self._poison(seed, orphan_dir, edited.id, tombed_victim.name)
        index_path = orphan_dir / INDEX_FILENAME

        fresh = VaultWriter(vault_path=vault_path)
        with _index_lock_held_elsewhere(VaultWriter._index_lock_path(orphan_dir)):
            thread, record = _spawn(lambda: fresh.restore_fragment(edited))
            _wait_for_an_effect(record)
            assert not record.get("done"), "the restore probe repaired unlocked"
            assert index_path.read_bytes() == before

        thread.join(timeout=_WEDGE_TIMEOUT_SECONDS)
        assert not thread.is_alive()
        assert record.get("error") is None, record.get("error")
        assert record["result"] is not None

    def test_a_lookup_and_a_write_on_one_writer_do_not_stall(
        self,
        vault_path: Path,
    ) -> None:
        """The pinning test for the *naive* answer to #1621.

        Taking ``vault_lock`` inside ``self._lock`` inverts the house order,
        and the inversion resolves the only way a polled lock can: the loser
        waits out ``DEFAULT_LOCK_TIMEOUT_SECONDS`` and raises
        ``VaultLockTimeoutError`` out of a read-only lookup. Both callers
        still *return*, which is why this asserts a clock bound and the
        absence of that exception rather than mere completion.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        rounds = 40
        for i in range(rounds):
            seed._append_index_entry(
                target_dir, f"frag-poison{i:04d}", victim_path.name
            )

        shared = VaultWriter(vault_path=vault_path)
        barrier = threading.Barrier(2)
        errors: dict[str, BaseException] = {}

        def _write_side() -> None:
            """Hammer ``write_fragment``, which takes the key then ``self._lock``."""
            try:
                barrier.wait(timeout=_WEDGE_TIMEOUT_SECONDS)
                for i in range(rounds):
                    shared.write_fragment(
                        Fragment(
                            id=f"frag-newcomer{i:03d}",
                            title=f"Newcomer {i}",
                            source=FragmentSource(platform=SourcePlatform.CLAUDE),
                            created=datetime(2025, 1, 15, 10, 30, 0),
                        ),
                        body="newcomer body",
                    )
            except BaseException as exc:  # recorded, then re-asserted
                errors["write"] = exc

        def _lookup_side() -> None:
            """Hammer the lookup path, every id of which needs a repair."""
            try:
                barrier.wait(timeout=_WEDGE_TIMEOUT_SECONDS)
                for i in range(rounds):
                    shared._find_existing(f"frag-poison{i:04d}", target_dir)
            except BaseException as exc:  # recorded, then re-asserted
                errors["lookup"] = exc

        writer_thread = threading.Thread(target=_write_side)
        lookup_thread = threading.Thread(target=_lookup_side)
        started = time.monotonic()
        writer_thread.start()
        lookup_thread.start()
        writer_thread.join(timeout=_NO_STALL_CEILING_SECONDS * 3)
        lookup_thread.join(timeout=_NO_STALL_CEILING_SECONDS * 3)
        elapsed = time.monotonic() - started

        assert not writer_thread.is_alive()
        assert not lookup_thread.is_alive()
        assert not [
            exc for exc in errors.values() if isinstance(exc, VaultLockTimeoutError)
        ], f"the lock pair inverted: {errors}"
        assert errors == {}, f"a caller raised: {errors}"
        assert elapsed < _NO_STALL_CEILING_SECONDS, (
            f"lookup and write took {elapsed:.1f}s together — "
            "the inverted pair burns the full lock timeout"
        )


# ---- Index compaction (#1300) ----


def _int_id_note(target_dir: Path, stem: str) -> str:
    """Write a note whose ``id`` YAML types as ``int``, and return its filename.

    ``_rebuild_index`` deliberately declines a non-string id (#1291: report,
    do not normalise), so the directory scan cannot see this mapping at all.
    Only the JSONL knows it — which makes it the record that distinguishes a
    compaction that merges the scan with the parsed entries from one that
    simply overwrites the file with a scan.

    Args:
        target_dir: The directory to write the note into.
        stem: The filename stem, without the ``.md`` suffix.

    Returns:
        The note's filename.
    """
    path = target_dir / f"{stem}.md"
    path.write_text("---\nid: 12345\ntitle: Numeral\n---\n\nbody\n", encoding="utf-8")
    return path.name


class TestIndexCompaction:
    """``.id-index.jsonl`` is append-only, and the dead lines are reclaimable (#1300).

    Every superseded mapping, every entry whose file has gone, and every
    torn record stays in the file forever. The torn one is the expensive
    kind: ``_load_index_locked`` routes a damaged parse into
    ``_recover_damaged_index``, which never persists and sets
    ``incremental = False`` — so *every* fresh ``VaultWriter`` pays a full
    directory scan of that directory, permanently.

    The pass needs exclusion from every appender, which is why it could not
    be written until #1621 put the last two index writers under
    ``vault_lock(<dir>/.id-index.lock)``.
    """

    @staticmethod
    def _seed_a_compactable_index(vault_path: Path) -> tuple[Path, Path, Path]:
        """Build a directory whose index holds all three classes of dead record.

        Args:
            vault_path: The test vault root.

        Returns:
            ``(target_dir, victim_path, owner_path)`` — the directory under
            test and the two files that must still resolve afterwards.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        owner_path = seed.write_fragment(_edited_fragment(), body="owner body")
        ghost = Fragment(
            id="frag-ghost0001",
            title="Ghost",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            created=datetime(2025, 1, 15, 10, 30, 0),
        )
        # 1. Superseded lines: the same two mappings, re-appended.
        ghost_path = seed.write_fragment(ghost, body="ghost body")
        for _ in range(3):
            seed._append_index_entry(target_dir, "frag-victim001", victim_path.name)
            seed._append_index_entry(target_dir, _edited_fragment().id, owner_path.name)
        # 2. A mapping whose file is gone (the shape a tomb leaves behind).
        ghost_path.unlink()
        # 3. A torn record — a crash mid-append, with no exception in flight.
        with (target_dir / INDEX_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write('\n{"id": "frag-torn0001", "filen')
        return target_dir, victim_path, owner_path

    def test_compaction_keeps_every_live_answer_and_drops_the_dead_records(
        self,
        vault_path: Path,
    ) -> None:
        """Same resolutions, fewer records, smaller file."""
        target_dir, victim_path, owner_path = self._seed_a_compactable_index(
            vault_path,
        )
        index_path = target_dir / INDEX_FILENAME
        asked = ["frag-victim001", _edited_fragment().id, "frag-ghost0001"]
        before_writer = VaultWriter(vault_path=vault_path)
        before = {mid: before_writer._find_existing(mid, target_dir) for mid in asked}
        assert before == {
            "frag-victim001": victim_path,
            _edited_fragment().id: owner_path,
            "frag-ghost0001": None,
        }
        size_before = index_path.stat().st_size
        records_before = len(_index_records(index_path))

        reclaimed = VaultWriter(vault_path=vault_path).compact_index(target_dir)

        assert reclaimed > 0
        assert index_path.stat().st_size == size_before - reclaimed
        assert len(_index_records(index_path)) < records_before
        assert sorted(VaultWriter._load_index_file(index_path)) == sorted(
            ["frag-victim001", _edited_fragment().id],
        )
        after_writer = VaultWriter(vault_path=vault_path)
        assert {
            mid: after_writer._find_existing(mid, target_dir) for mid in asked
        } == before

    def test_compaction_keeps_a_mapping_the_directory_scan_cannot_see(
        self,
        vault_path: Path,
    ) -> None:
        """The parsed entries fill the gaps the scan leaves, as recovery does.

        Rebuilding purely from the directory would silently drop every id
        whose file the scanner declines. ``_recover_damaged_index`` already
        merges scan-over-parsed for exactly that reason; compaction has to
        use the same precedence or it is a data-losing rewrite wearing a
        maintenance name.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        invisible = _int_id_note(target_dir, "2025-01-15-Numeral")
        seed._append_index_entry(target_dir, "12345", invisible)
        assert VaultWriter._rebuild_index(target_dir).get("12345") is None
        for _ in range(3):
            seed._append_index_entry(target_dir, "frag-victim001", victim_path.name)

        assert VaultWriter(vault_path=vault_path).compact_index(target_dir) > 0

        on_disk = VaultWriter._load_index_file(target_dir / INDEX_FILENAME)
        assert on_disk["12345"] == invisible
        assert on_disk["frag-victim001"] == victim_path.name

    def test_a_compacted_index_no_longer_costs_a_scan_on_every_cold_load(
        self,
        vault_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reason #1300 exists: a torn line taxes every future process.

        ``_recover_damaged_index`` never persists, so the damage — and the
        directory scan it forces — survives every load until something
        rewrites the file.
        """

        def _no_rescan(target_dir: Path) -> dict[str, str]:
            """Fail loudly instead of scanning the directory."""
            msg = f"unexpected directory rescan of {target_dir}"
            raise AssertionError(msg)

        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        with (target_dir / INDEX_FILENAME).open("a", encoding="utf-8") as handle:
            handle.write('\n{"id": "frag-torn0001", "filen')

        # Positive control: today every cold load of this directory rescans.
        monkeypatch.setattr(VaultWriter, "_rebuild_index", staticmethod(_no_rescan))
        with pytest.raises(AssertionError, match="unexpected directory rescan"):
            VaultWriter(vault_path=vault_path)._find_existing(
                "frag-victim001",
                target_dir,
            )
        monkeypatch.undo()

        assert VaultWriter(vault_path=vault_path).compact_index(target_dir) > 0

        monkeypatch.setattr(VaultWriter, "_rebuild_index", staticmethod(_no_rescan))
        healed = VaultWriter(vault_path=vault_path)
        assert healed._find_existing("frag-victim001", target_dir) == victim_path

    def test_compaction_waits_for_the_directory_lock(
        self,
        vault_path: Path,
    ) -> None:
        """A whole-file rewrite is the one write that must never race an append."""
        target_dir, _victim_path, _owner_path = self._seed_a_compactable_index(
            vault_path,
        )
        index_path = target_dir / INDEX_FILENAME
        before = index_path.read_bytes()

        fresh = VaultWriter(vault_path=vault_path)
        with _index_lock_held_elsewhere(VaultWriter._index_lock_path(target_dir)):
            thread, record = _spawn(lambda: fresh.compact_index(target_dir))
            _wait_for_an_effect(record)
            assert not record.get("done"), "compaction rewrote the index unlocked"
            assert index_path.read_bytes() == before

        thread.join(timeout=_WEDGE_TIMEOUT_SECONDS)
        assert not thread.is_alive()
        assert record.get("error") is None, record.get("error")
        assert record["result"] > 0
        assert index_path.read_bytes() != before

    def test_a_compaction_racing_repairs_and_writes_loses_no_record(
        self,
        vault_path: Path,
    ) -> None:
        """The criterion #1621 had to land first to make achievable.

        One ``VaultWriter``, three threads: appends, mismatch repairs, and
        whole-file rewrites into one directory. Every appender now holds the
        directory key, so the rewrite is exclusive with all of them.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        invisible = _int_id_note(target_dir, "2025-01-15-Numeral")
        seed._append_index_entry(target_dir, "12345", invisible)
        rounds = 20
        live: dict[str, Path] = {"frag-victim001": victim_path}
        for i in range(rounds):
            fragment = Fragment(
                id=f"frag-resident{i:03d}",
                title=f"Resident {i}",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=datetime(2025, 1, 15, 10, 30, 0),
            )
            live[fragment.id] = seed.write_fragment(fragment, body=f"body {i}")
            seed._append_index_entry(
                target_dir, f"frag-poison{i:03d}", victim_path.name
            )

        shared = VaultWriter(vault_path=vault_path)
        barrier = threading.Barrier(3)
        errors: dict[str, BaseException] = {}

        def _guarded(label: str, body: Callable[[int], None]) -> Callable[[], None]:
            """Wrap one worker so a failure is recorded rather than swallowed."""

            def _run() -> None:
                try:
                    barrier.wait(timeout=_WEDGE_TIMEOUT_SECONDS)
                    for i in range(rounds):
                        body(i)
                except BaseException as exc:  # recorded, then re-asserted
                    errors[label] = exc

            return _run

        def _append(i: int) -> None:
            """Write a brand-new fragment into the directory under compaction."""
            shared.write_fragment(
                Fragment(
                    id=f"frag-arrival{i:03d}",
                    title=f"Arrival {i}",
                    source=FragmentSource(platform=SourcePlatform.CLAUDE),
                    created=datetime(2025, 1, 15, 10, 30, 0),
                ),
                body=f"arrival {i}",
            )

        def _repair(i: int) -> None:
            """Resolve a poisoned id, which re-scans and appends the correction."""
            shared._find_existing(f"frag-poison{i:03d}", target_dir)

        def _compact(_i: int) -> None:
            """Rewrite the whole file from a merged snapshot."""
            shared.compact_index(target_dir)

        threads = [
            threading.Thread(target=_guarded("append", _append)),
            threading.Thread(target=_guarded("repair", _repair)),
            threading.Thread(target=_guarded("compact", _compact)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_WEDGE_TIMEOUT_SECONDS * 4)

        assert not [thread for thread in threads if thread.is_alive()]
        assert errors == {}, f"a caller raised: {errors}"
        index_path = target_dir / INDEX_FILENAME
        assert not VaultWriter._read_index_records(index_path).damaged
        on_disk = VaultWriter._load_index_file(index_path)
        assert on_disk["12345"] == invisible
        for i in range(rounds):
            live[f"frag-arrival{i:03d}"] = target_dir / on_disk[f"frag-arrival{i:03d}"]
        after = VaultWriter(vault_path=vault_path)
        assert {
            model_id: after._find_existing(model_id, target_dir) for model_id in live
        } == live

    def test_compaction_refuses_a_rewrite_that_would_grow_the_file(
        self,
        vault_path: Path,
    ) -> None:
        """A longer file at the same path is the one shape a reader misreads.

        ``_refresh_index_locked`` treats a file longer than its cursor as
        *appended to* and splices the extra bytes onto the cached mapping.
        A whole-file rewrite that grew the file would hand it unrelated
        content at that offset. The shrink guard is what keeps a reader
        that had consumed the whole file inside the shapes #1603's cursor
        recovers from — so a directory holding notes the index never
        learned about is left alone rather than rewritten. It bounds the
        hazard rather than removing it; ``compact_index``'s docstring
        names the residual a reader with an older cursor still has.
        """
        seed = VaultWriter(vault_path=vault_path)
        victim_path = _seed_victim(seed)
        target_dir = victim_path.parent
        for i in range(5):
            note = target_dir / f"2025-01-15-Unindexed-{i}.md"
            note.write_text(
                f"---\nid: frag-unindexed{i:02d}\ntitle: U{i}\n---\n\nbody\n",
                encoding="utf-8",
            )
        index_path = target_dir / INDEX_FILENAME
        before = index_path.read_bytes()
        assert len(VaultWriter._rebuild_index(target_dir)) == 6  # positive control

        assert VaultWriter(vault_path=vault_path).compact_index(target_dir) == 0

        assert index_path.read_bytes() == before

    def test_compacting_a_directory_with_no_index_writes_nothing(
        self,
        vault_path: Path,
    ) -> None:
        """Asking about an unindexed directory leaves no lock file behind (#1332)."""
        bare = vault_path / "01-Fragments" / "Journal"
        writer = VaultWriter(vault_path=vault_path)

        assert writer.compact_index(bare) == 0

        assert not (bare / INDEX_FILENAME).exists()
        assert list(vault_path.rglob(INDEX_LOCK_FILENAME)) == []
