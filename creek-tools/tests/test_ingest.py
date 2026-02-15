"""Tests for creek.ingest — abstract Ingestor base class and shared utilities.

Covers encoding normalization, timestamp normalization, fragment ID generation,
provenance creation, Pydantic models (RawDocument, ParsedFragment, IngestResult),
the Ingestor ABC contract enforcement, and the concrete ``ingest()`` orchestrator.
"""

import abc
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from creek.ingest.base import (
    Ingestor,
    IngestResult,
    ParsedFragment,
    ProvenanceEntry,
    RawDocument,
    create_provenance_entry,
    generate_fragment_id,
    normalize_encoding,
    normalize_timestamp,
)

# ---- Fixtures ----

LA_TZ = ZoneInfo("America/Los_Angeles")


class _ConcreteIngestor(Ingestor):
    """A minimal concrete implementation of Ingestor for testing.

    Implements all abstract methods with simple, predictable behavior.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Return a single RawDocument for any source path.

        Args:
            source_path: The path to search for documents.

        Returns:
            A list with a single RawDocument.
        """
        return [
            RawDocument(
                path=source_path / "test.txt",
                content=b"hello world",
                metadata={"source_type": "test"},
                detected_encoding="utf-8",
            )
        ]

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse a RawDocument into a single ParsedFragment.

        Args:
            raw: The raw document to parse.

        Returns:
            A list with a single ParsedFragment.
        """
        return [
            ParsedFragment(
                content=raw.content.decode(raw.detected_encoding),
                metadata=raw.metadata,
                source_path=str(raw.path),
                timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
            )
        ]

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a ParsedFragment to markdown.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            The content as markdown.
        """
        return f"# Fragment\n\n{fragment.content}"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter for a ParsedFragment.

        Args:
            fragment: The parsed fragment.

        Returns:
            A dict of frontmatter fields.
        """
        return {
            "title": "Test Fragment",
            "source": fragment.source_path,
            "created": fragment.timestamp.isoformat(),
        }


class _PartialIngestor(Ingestor):
    """An incomplete Ingestor that only implements some abstract methods.

    Used to verify that ABC enforcement works correctly.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Discover documents (the only implemented abstract method).

        Args:
            source_path: The path to search for documents.

        Returns:
            An empty list.
        """
        return []

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse a raw document.

        Args:
            raw: The raw document.

        Returns:
            An empty list.
        """
        return []

    # Missing: convert_to_markdown and generate_frontmatter


# ---- RawDocument Model Tests ----


class TestRawDocument:
    """Tests for the RawDocument Pydantic model."""

    def test_creation_with_required_fields(self) -> None:
        """RawDocument should be creatable with all required fields."""
        doc = RawDocument(
            path=Path("/fake/test.txt"),
            content=b"hello",
            metadata={},
            detected_encoding="utf-8",
        )
        assert doc.path == Path("/fake/test.txt")
        assert doc.content == b"hello"
        assert doc.metadata == {}
        assert doc.detected_encoding == "utf-8"

    def test_metadata_dict_values(self) -> None:
        """RawDocument metadata should accept arbitrary string keys."""
        doc = RawDocument(
            path=Path("/fake/doc.md"),
            content=b"content",
            metadata={"author": "test", "version": "1.0"},
            detected_encoding="utf-8",
        )
        assert doc.metadata["author"] == "test"
        assert doc.metadata["version"] == "1.0"

    def test_bytes_content(self) -> None:
        """RawDocument content should accept arbitrary bytes."""
        raw_bytes = b"\xff\xfe\x00\x01"
        doc = RawDocument(
            path=Path("/fake/binary.bin"),
            content=raw_bytes,
            metadata={},
            detected_encoding="utf-8",
        )
        assert doc.content == raw_bytes

    def test_model_dump(self) -> None:
        """RawDocument model_dump should produce a serializable dict."""
        doc = RawDocument(
            path=Path("/fake/test.txt"),
            content=b"test",
            metadata={"key": "value"},
            detected_encoding="utf-8",
        )
        dump = doc.model_dump()
        assert isinstance(dump, dict)
        assert dump["detected_encoding"] == "utf-8"


# ---- ParsedFragment Model Tests ----


class TestParsedFragment:
    """Tests for the ParsedFragment Pydantic model."""

    def test_creation_with_required_fields(self) -> None:
        """ParsedFragment should be creatable with all required fields."""
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        frag = ParsedFragment(
            content="Hello world",
            metadata={},
            source_path="/fake/test.txt",
            timestamp=now,
        )
        assert frag.content == "Hello world"
        assert frag.metadata == {}
        assert frag.source_path == "/fake/test.txt"
        assert frag.timestamp == now

    def test_metadata_accepts_arbitrary_values(self) -> None:
        """ParsedFragment metadata should accept various value types."""
        frag = ParsedFragment(
            content="test",
            metadata={"count": 42, "tags": ["a", "b"]},
            source_path="/fake/doc.md",
            timestamp=datetime(2024, 6, 1, tzinfo=LA_TZ),
        )
        assert frag.metadata["count"] == 42
        assert frag.metadata["tags"] == ["a", "b"]

    def test_model_dump(self) -> None:
        """ParsedFragment model_dump should produce a serializable dict."""
        frag = ParsedFragment(
            content="test",
            metadata={},
            source_path="/fake/doc.md",
            timestamp=datetime(2024, 6, 1, tzinfo=LA_TZ),
        )
        dump = frag.model_dump()
        assert isinstance(dump, dict)
        assert dump["content"] == "test"


# ---- ProvenanceEntry Model Tests ----


class TestProvenanceEntry:
    """Tests for the ProvenanceEntry Pydantic model."""

    def test_creation(self) -> None:
        """ProvenanceEntry should be creatable with all fields."""
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        entry = ProvenanceEntry(
            source_path="/fake/test.txt",
            ingestor_name="TestIngestor",
            timestamp=now,
            fragment_id="frag-abc123def456",
            status="success",
        )
        assert entry.source_path == "/fake/test.txt"
        assert entry.ingestor_name == "TestIngestor"
        assert entry.timestamp == now
        assert entry.fragment_id == "frag-abc123def456"
        assert entry.status == "success"

    def test_model_dump(self) -> None:
        """ProvenanceEntry model_dump should produce a serializable dict."""
        entry = ProvenanceEntry(
            source_path="/fake/test.txt",
            ingestor_name="TestIngestor",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            fragment_id="frag-abc123def456",
            status="success",
        )
        dump = entry.model_dump()
        assert isinstance(dump, dict)
        assert dump["status"] == "success"


# ---- IngestResult Model Tests ----


class TestIngestResult:
    """Tests for the IngestResult Pydantic model."""

    def test_creation_with_defaults(self) -> None:
        """IngestResult should be creatable with default empty lists."""
        result = IngestResult()
        assert result.fragments == []
        assert result.provenance == []
        assert result.errors == []

    def test_creation_with_data(self) -> None:
        """IngestResult should accept lists of fragments, provenance, and errors."""
        frag = ParsedFragment(
            content="test",
            metadata={},
            source_path="/fake/doc.md",
            timestamp=datetime(2024, 6, 1, tzinfo=LA_TZ),
        )
        prov = ProvenanceEntry(
            source_path="/fake/doc.md",
            ingestor_name="TestIngestor",
            timestamp=datetime(2024, 6, 1, tzinfo=LA_TZ),
            fragment_id="frag-abc123def456",
            status="success",
        )
        result = IngestResult(
            fragments=[frag],
            provenance=[prov],
            errors=["warning: something minor"],
        )
        assert len(result.fragments) == 1
        assert len(result.provenance) == 1
        assert len(result.errors) == 1
        assert result.errors[0] == "warning: something minor"

    def test_model_dump(self) -> None:
        """IngestResult model_dump should produce a serializable dict."""
        result = IngestResult()
        dump = result.model_dump()
        assert isinstance(dump, dict)
        assert dump["fragments"] == []
        assert dump["provenance"] == []
        assert dump["errors"] == []


# ---- normalize_encoding Tests ----


class TestNormalizeEncoding:
    """Tests for the normalize_encoding utility function."""

    def test_utf8_passthrough(self) -> None:
        """UTF-8 encoded bytes should decode correctly."""
        text, encoding = normalize_encoding(b"hello world")
        assert text == "hello world"
        # chardet may detect pure ASCII as "ascii" which is a subset of UTF-8
        assert encoding.lower() in ("utf-8", "ascii", "utf8")

    def test_latin1_detection(self) -> None:
        """Latin-1 (ISO-8859-1) bytes should be detected and decoded."""
        raw = "caf\u00e9".encode("latin-1")
        text, _encoding = normalize_encoding(raw)
        assert "caf" in text

    def test_utf16_detection(self) -> None:
        """UTF-16 encoded bytes with BOM should be detected and decoded."""
        raw = "hello".encode("utf-16")
        text, _encoding = normalize_encoding(raw)
        assert "hello" in text

    def test_empty_bytes(self) -> None:
        """Empty bytes should return an empty string."""
        text, encoding = normalize_encoding(b"")
        assert text == ""
        assert isinstance(encoding, str)

    def test_ascii_content(self) -> None:
        """Pure ASCII content should be detected and decoded."""
        raw = b"plain ascii text"
        text, _encoding = normalize_encoding(raw)
        assert text == "plain ascii text"

    def test_return_type(self) -> None:
        """normalize_encoding should return a tuple of (str, str)."""
        result = normalize_encoding(b"test")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], str)


# ---- normalize_timestamp Tests ----


class TestNormalizeTimestamp:
    """Tests for the normalize_timestamp utility function."""

    def test_iso_format_with_timezone(self) -> None:
        """ISO format timestamp with timezone should normalize to LA time."""
        result = normalize_timestamp("2024-01-15T18:30:00+00:00", None)
        assert result.tzinfo is not None
        assert str(result.tzinfo) == "America/Los_Angeles"
        # 18:30 UTC = 10:30 PST (UTC-8 in January)
        assert result.hour == 10
        assert result.minute == 30

    def test_iso_format_without_timezone(self) -> None:
        """ISO format without timezone should use source_tz if provided."""
        result = normalize_timestamp("2024-01-15T10:30:00", "America/New_York")
        assert result.tzinfo is not None
        assert str(result.tzinfo) == "America/Los_Angeles"
        # 10:30 ET = 07:30 PT in January
        assert result.hour == 7
        assert result.minute == 30

    def test_naive_timestamp_defaults_to_utc(self) -> None:
        """Naive timestamp without source_tz should default to UTC."""
        result = normalize_timestamp("2024-01-15T18:30:00", None)
        assert result.tzinfo is not None
        assert str(result.tzinfo) == "America/Los_Angeles"
        # 18:30 UTC = 10:30 PST
        assert result.hour == 10

    def test_date_only_format(self) -> None:
        """Date-only string should be parsed as midnight UTC."""
        result = normalize_timestamp("2024-01-15", None)
        assert result.tzinfo is not None
        assert result.year == 2024
        assert result.month == 1

    def test_common_date_format(self) -> None:
        """Common date format 'YYYY-MM-DD HH:MM:SS' should parse correctly."""
        result = normalize_timestamp("2024-06-15 14:30:00", None)
        assert result.tzinfo is not None
        assert result.year == 2024
        assert result.month == 6
        assert result.day == 15

    def test_result_always_has_la_timezone(self) -> None:
        """Result should always be in America/Los_Angeles timezone."""
        result = normalize_timestamp("2024-01-15T10:00:00+05:30", None)
        assert str(result.tzinfo) == "America/Los_Angeles"

    def test_source_tz_parameter(self) -> None:
        """Source timezone should be used to interpret naive timestamps."""
        result = normalize_timestamp("2024-07-15T12:00:00", "Europe/London")
        assert str(result.tzinfo) == "America/Los_Angeles"
        # 12:00 BST (UTC+1 in July) = 04:00 PDT (UTC-7 in July)
        assert result.hour == 4

    def test_invalid_timestamp_raises(self) -> None:
        """Invalid timestamp string should raise ValueError."""
        with pytest.raises(ValueError, match="Unable to parse timestamp"):
            normalize_timestamp("not-a-timestamp", None)


# ---- generate_fragment_id Tests ----


class TestGenerateFragmentId:
    """Tests for the generate_fragment_id utility function."""

    def test_id_starts_with_frag_prefix(self) -> None:
        """Generated ID should start with 'frag-' prefix."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        frag_id = generate_fragment_id("source.txt", ts, "content")
        assert frag_id.startswith("frag-")

    def test_id_has_correct_length(self) -> None:
        """Generated ID should be 'frag-' + 12 hex chars = 17 chars total."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        frag_id = generate_fragment_id("source.txt", ts, "content")
        assert len(frag_id) == 17  # "frag-" (5) + 12 hex chars

    def test_id_hex_portion_is_valid_hex(self) -> None:
        """The hex portion of the ID should be valid hexadecimal."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        frag_id = generate_fragment_id("source.txt", ts, "content")
        hex_part = frag_id[5:]
        int(hex_part, 16)  # Should not raise

    def test_deterministic(self) -> None:
        """Same inputs should produce the same ID (deterministic)."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        id1 = generate_fragment_id("source.txt", ts, "content")
        id2 = generate_fragment_id("source.txt", ts, "content")
        assert id1 == id2

    def test_different_source_produces_different_id(self) -> None:
        """Different source should produce a different ID."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        id1 = generate_fragment_id("source_a.txt", ts, "content")
        id2 = generate_fragment_id("source_b.txt", ts, "content")
        assert id1 != id2

    def test_different_timestamp_produces_different_id(self) -> None:
        """Different timestamp should produce a different ID."""
        ts1 = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        ts2 = datetime(2024, 1, 15, 11, 0, 0, tzinfo=LA_TZ)
        id1 = generate_fragment_id("source.txt", ts1, "content")
        id2 = generate_fragment_id("source.txt", ts2, "content")
        assert id1 != id2

    def test_different_content_produces_different_id(self) -> None:
        """Different content should produce a different ID."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        id1 = generate_fragment_id("source.txt", ts, "content A")
        id2 = generate_fragment_id("source.txt", ts, "content B")
        assert id1 != id2

    def test_uses_sha256(self) -> None:
        """ID should match SHA-256 of the expected input string."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        source = "source.txt"
        content = "content"
        expected_hash = hashlib.sha256(
            f"{source}:{ts.isoformat()}:{content}".encode()
        ).hexdigest()[:12]
        expected_id = f"frag-{expected_hash}"
        actual_id = generate_fragment_id(source, ts, content)
        assert actual_id == expected_id


# ---- create_provenance_entry Tests ----


class TestCreateProvenanceEntry:
    """Tests for the create_provenance_entry utility function."""

    def test_creates_valid_entry(self) -> None:
        """Should create a ProvenanceEntry with all fields set."""
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        entry = create_provenance_entry(
            source_path="/fake/test.txt",
            ingestor_name="TestIngestor",
            timestamp=now,
            fragment_id="frag-abc123def456",
            status="success",
        )
        assert isinstance(entry, ProvenanceEntry)
        assert entry.source_path == "/fake/test.txt"
        assert entry.ingestor_name == "TestIngestor"
        assert entry.timestamp == now
        assert entry.fragment_id == "frag-abc123def456"
        assert entry.status == "success"

    def test_error_status(self) -> None:
        """Should allow 'error' status."""
        entry = create_provenance_entry(
            source_path="/fake/test.txt",
            ingestor_name="TestIngestor",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            fragment_id="frag-abc123def456",
            status="error",
        )
        assert entry.status == "error"

    def test_skipped_status(self) -> None:
        """Should allow 'skipped' status."""
        entry = create_provenance_entry(
            source_path="/fake/test.txt",
            ingestor_name="TestIngestor",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            fragment_id="frag-abc123def456",
            status="skipped",
        )
        assert entry.status == "skipped"


# ---- Ingestor ABC Tests ----


class TestIngestorABC:
    """Tests for the Ingestor abstract base class contract."""

    def test_cannot_instantiate_directly(self) -> None:
        """Ingestor ABC should not be instantiable directly."""
        with pytest.raises(TypeError, match="abstract method"):
            Ingestor()  # type: ignore[abstract]

    def test_concrete_implementation_instantiates(self) -> None:
        """A fully concrete implementation should instantiate without error."""
        ingestor = _ConcreteIngestor()
        assert isinstance(ingestor, Ingestor)

    def test_is_abstract_base_class(self) -> None:
        """Ingestor should be an ABC."""
        assert issubclass(Ingestor, abc.ABC)

    def test_abstract_methods_defined(self) -> None:
        """Ingestor should define the four required abstract methods."""
        abstract_methods = getattr(Ingestor, "__abstractmethods__", set())
        assert "discover" in abstract_methods
        assert "parse" in abstract_methods
        assert "convert_to_markdown" in abstract_methods
        assert "generate_frontmatter" in abstract_methods

    def test_ingest_is_concrete(self) -> None:
        """The ingest() method should be concrete (not abstract)."""
        abstract_methods = getattr(Ingestor, "__abstractmethods__", set())
        assert "ingest" not in abstract_methods

    def test_partial_implementation_fails(self) -> None:
        """Partial implementation missing abstract methods should fail."""
        with pytest.raises(TypeError, match="abstract method"):
            _PartialIngestor()  # type: ignore[abstract]


# ---- Ingestor.ingest() Orchestration Tests ----


class TestIngestOrchestration:
    """Tests for the concrete ingest() orchestration method."""

    def test_ingest_returns_ingest_result(self) -> None:
        """ingest() should return an IngestResult."""
        ingestor = _ConcreteIngestor()
        result = ingestor.ingest(Path("/fake/source"))
        assert isinstance(result, IngestResult)

    def test_ingest_populates_fragments(self) -> None:
        """ingest() should populate fragments from discover -> parse pipeline."""
        ingestor = _ConcreteIngestor()
        result = ingestor.ingest(Path("/fake/source"))
        assert len(result.fragments) > 0
        assert result.fragments[0].content == "hello world"

    def test_ingest_populates_provenance(self) -> None:
        """ingest() should populate provenance entries."""
        ingestor = _ConcreteIngestor()
        result = ingestor.ingest(Path("/fake/source"))
        assert len(result.provenance) > 0

    def test_ingest_calls_discover(self) -> None:
        """ingest() should call discover() with the source path."""
        ingestor = _ConcreteIngestor()
        source = Path("/fake/source")
        with patch.object(
            ingestor, "discover", wraps=ingestor.discover
        ) as mock_discover:
            ingestor.ingest(source)
            mock_discover.assert_called_once_with(source)

    def test_ingest_calls_parse_for_each_document(self) -> None:
        """ingest() should call parse() for each discovered RawDocument."""
        ingestor = _ConcreteIngestor()
        with patch.object(ingestor, "parse", wraps=ingestor.parse) as mock_parse:
            ingestor.ingest(Path("/fake/source"))
            assert mock_parse.call_count == 1  # _ConcreteIngestor returns 1 doc

    def test_ingest_calls_convert_to_markdown(self) -> None:
        """ingest() should call convert_to_markdown() for each fragment."""
        ingestor = _ConcreteIngestor()
        with patch.object(
            ingestor, "convert_to_markdown", wraps=ingestor.convert_to_markdown
        ) as mock_convert:
            ingestor.ingest(Path("/fake/source"))
            assert mock_convert.call_count == 1

    def test_ingest_calls_generate_frontmatter(self) -> None:
        """ingest() should call generate_frontmatter() for each fragment."""
        ingestor = _ConcreteIngestor()
        with patch.object(
            ingestor, "generate_frontmatter", wraps=ingestor.generate_frontmatter
        ) as mock_frontmatter:
            ingestor.ingest(Path("/fake/source"))
            assert mock_frontmatter.call_count == 1

    def test_ingest_handles_discover_error(self) -> None:
        """ingest() should handle errors during discover gracefully."""
        ingestor = _ConcreteIngestor()
        with patch.object(
            ingestor, "discover", side_effect=OSError("Permission denied")
        ):
            result = ingestor.ingest(Path("/fake/source"))
            assert isinstance(result, IngestResult)
            assert len(result.errors) > 0
            assert "Permission denied" in result.errors[0]

    def test_ingest_handles_parse_error(self) -> None:
        """ingest() should handle errors during parse gracefully."""
        ingestor = _ConcreteIngestor()
        with patch.object(ingestor, "parse", side_effect=ValueError("Bad content")):
            result = ingestor.ingest(Path("/fake/source"))
            assert isinstance(result, IngestResult)
            assert len(result.errors) > 0
            assert "Bad content" in result.errors[0]

    def test_ingest_handles_convert_error(self) -> None:
        """ingest() should handle errors during convert_to_markdown gracefully."""
        ingestor = _ConcreteIngestor()
        with patch.object(
            ingestor, "convert_to_markdown", side_effect=RuntimeError("Convert failed")
        ):
            result = ingestor.ingest(Path("/fake/source"))
            assert isinstance(result, IngestResult)
            assert len(result.errors) > 0

    def test_ingest_handles_frontmatter_error(self) -> None:
        """ingest() should handle errors during generate_frontmatter gracefully."""
        ingestor = _ConcreteIngestor()
        with patch.object(
            ingestor,
            "generate_frontmatter",
            side_effect=RuntimeError("Frontmatter failed"),
        ):
            result = ingestor.ingest(Path("/fake/source"))
            assert isinstance(result, IngestResult)
            assert len(result.errors) > 0

    def test_ingest_empty_discover(self) -> None:
        """ingest() should handle discover returning an empty list."""
        ingestor = _ConcreteIngestor()
        with patch.object(ingestor, "discover", return_value=[]):
            result = ingestor.ingest(Path("/fake/source"))
            assert isinstance(result, IngestResult)
            assert len(result.fragments) == 0

    def test_ingest_multiple_documents(self) -> None:
        """ingest() should process multiple documents from discover."""
        ingestor = _ConcreteIngestor()
        docs = [
            RawDocument(
                path=Path("/fake/a.txt"),
                content=b"doc a",
                metadata={},
                detected_encoding="utf-8",
            ),
            RawDocument(
                path=Path("/fake/b.txt"),
                content=b"doc b",
                metadata={},
                detected_encoding="utf-8",
            ),
        ]
        with patch.object(ingestor, "discover", return_value=docs):
            result = ingestor.ingest(Path("/fake/source"))
            assert len(result.fragments) == 2

    def test_ingest_multiple_fragments_from_one_document(self) -> None:
        """ingest() should handle parse returning multiple fragments."""
        ingestor = _ConcreteIngestor()
        multi_frags = [
            ParsedFragment(
                content="frag 1",
                metadata={},
                source_path="/fake/test.txt",
                timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            ),
            ParsedFragment(
                content="frag 2",
                metadata={},
                source_path="/fake/test.txt",
                timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            ),
        ]
        with patch.object(ingestor, "parse", return_value=multi_frags):
            result = ingestor.ingest(Path("/fake/source"))
            assert len(result.fragments) == 2


# ---- Ingest Package __init__ Tests ----


class TestIngestPackage:
    """Tests for the creek.ingest package __init__.py."""

    def test_registry_exists(self) -> None:
        """The ingest package should export a registry of ingestors."""
        from creek.ingest import INGESTOR_REGISTRY

        assert isinstance(INGESTOR_REGISTRY, dict)

    def test_registry_contains_ingestors(self) -> None:
        """The registry should contain registered ingestors."""
        from creek.ingest import INGESTOR_REGISTRY

        assert "discord" in INGESTOR_REGISTRY
        assert "markdown" in INGESTOR_REGISTRY

    def test_base_classes_importable(self) -> None:
        """Core classes should be importable from creek.ingest."""
        from creek.ingest import (
            Ingestor,
            IngestResult,
            ParsedFragment,
            RawDocument,
        )

        assert RawDocument is not None
        assert ParsedFragment is not None
        assert IngestResult is not None
        assert Ingestor is not None
