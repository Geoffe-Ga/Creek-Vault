"""Tests for the vault writer module.

Verifies that VaultWriter correctly writes Creek ontological primitives
(Fragment, Thread, Eddy, Praxis, Decision) as markdown files with YAML
frontmatter to the appropriate vault directories, handles duplicate
detection, provenance logging, filename sanitization, and dispatching
via write_any.
"""

from __future__ import annotations

import errno
import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from pydantic import BaseModel

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
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import ShortWriteController


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
        # ``_rebuild_index`` only indexes ids where ``isinstance(mid, str)``, so
        # the int-typed file is invisible to re-resolution. The honest outcome
        # is therefore two files carrying the same *logical* id — declining the
        # foreign file cannot also deduplicate an id the index cannot see.
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
        """
        from creek.vault.writer import INDEX_FILENAME

        short_write.halve()
        target_dir = vault_path / "01-Fragments" / "Conversations"
        model_id = "frag-short-write"
        filename = "2025-01-15-demo.md"

        VaultWriter._append_index_entry(target_dir, model_id, filename)

        index_path = target_dir / INDEX_FILENAME
        expected = (
            json.dumps({"id": model_id, "filename": filename}, sort_keys=True) + "\n"
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

    def test_failed_index_append_also_swallows_the_next_entry(
        self,
        vault_path: Path,
        short_write: ShortWriteController,
    ) -> None:
        """A half-written index line takes the *next* entry down with it.

        KNOWN DEFECT, tracked as issue #1120. This is a characterization
        test: it pins the behaviour the code has **today**, not the
        behaviour anybody wants. ``_append_index_entry`` opens with
        ``O_APPEND``, so every line lands at EOF with no separator of its
        own — the trailing newline is part of the payload. When a drain
        fails part-way (``ENOSPC`` here, after half the bytes are on
        disk) the remnant survives *without* that newline, and the next
        successful append is concatenated straight onto it. The merged
        line is not valid JSON, so ``_load_index_file`` skips it wholesale
        at ``json.JSONDecodeError`` and **both** the failed entry and the
        innocent next one drop out of the index.

        ``ftruncate``-back is not a safe repair under concurrent
        ``O_APPEND`` writers, so a real fix needs a rewrite-under-lock or
        a framing change — out of scope for #987. When #1120 lands, this
        test must be **inverted** (``frag-next`` at minimum, ideally both
        entries, must survive), never deleted.
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
        # ...and the remnant carried no newline of its own, so the two
        # collided into a single, unparseable line.
        assert len(raw.splitlines()) == 1

        index = VaultWriter._load_index_file(index_path)
        # State both absences explicitly: the failed entry is gone (bad
        # but expected) and so is the *successful* one (the defect).
        assert "frag-partial" not in index
        assert "frag-next" not in index
        assert index == {}

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
