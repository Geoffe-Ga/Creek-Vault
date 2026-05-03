"""Tests for the vault writer module.

Verifies that VaultWriter correctly writes Creek ontological primitives
(Fragment, Thread, Eddy, Praxis, Decision) as markdown files with YAML
frontmatter to the appropriate vault directories, handles duplicate
detection, provenance logging, filename sanitization, and dispatching
via write_any.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

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
    SourcePlatform,
    Thread,
    ThreadStatus,
)
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path


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
