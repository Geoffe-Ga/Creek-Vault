"""Abstract Ingestor base class and shared utilities for the Creek ingest pipeline.

This module provides:

- **Pydantic models**: ``RawDocument``, ``ParsedFragment``, ``ProvenanceEntry``,
  and ``IngestResult`` for structured data flow through the ingest pipeline.
- **Utility functions**: ``normalize_encoding``, ``normalize_timestamp``,
  ``generate_fragment_id``, and ``create_provenance_entry`` for common
  ingest operations.
- **Abstract base class**: ``Ingestor`` defining the four-stage pipeline
  (discover, parse, convert, frontmatter) with a concrete ``ingest()``
  orchestrator.
"""

from __future__ import annotations

import abc
import hashlib
import logging
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import chardet
from pydantic import BaseModel, ConfigDict, Field

from creek._containment import assert_source_contained
from creek.classify.tags_pass import apply_tags
from creek.ingest.source_unit import compose_source_unit
from creek.models import Fragment, FragmentLevel

# ``LA_TZ`` — the target timezone for all normalized timestamps — is declared
# once in :mod:`creek.time` and imported here (#1339). This module re-exports
# it so the historical ``from creek.ingest.base import LA_TZ`` path keeps
# resolving for out-of-tree callers.
from creek.time import LA_TZ

logger = logging.getLogger(__name__)

# ---- Pydantic Models ----


class RawDocument(BaseModel):
    """A raw document discovered by an ingestor before parsing.

    Holds the file path, raw byte content, arbitrary metadata, and the
    detected character encoding.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path
    """Filesystem path to the source file."""

    content: bytes
    """Raw byte content of the file."""

    metadata: dict[str, Any]
    """Arbitrary metadata attached during discovery."""

    detected_encoding: str
    """Character encoding detected or declared for this document."""


class ParsedFragment(BaseModel):
    """A structured content fragment extracted from a raw document.

    Represents one logical unit of content after parsing, with its
    source provenance and timestamp.

    Ingestors with rich structured backend output (workbooks, slide
    decks, etc.) should stash that output on :attr:`payload` as a
    typed dataclass and use :attr:`metadata` only for the small set of
    frontmatter-relevant scalars. See the class docstring of
    :class:`Ingestor` for the migration story.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str
    """The extracted text content."""

    metadata: dict[str, Any]
    """Frontmatter-relevant scalars from parsing.

    Kept as an untyped dict so YAML serialisation in
    :meth:`Ingestor.generate_frontmatter` stays trivial. Reach for
    :attr:`payload` when you need to round-trip structured data
    between :meth:`Ingestor.parse` and :meth:`Ingestor.convert_to_markdown`
    without erasing types or duplicating the schema in two places.
    """

    source_path: str
    """Path to the original source file.

    Always the *whole file*, verbatim, even for an ingestor that emits
    several fragments from it. Downstream consumers rely on that: the
    per-file titles in :mod:`creek.ingest.code` and :mod:`creek.ingest.generic`,
    the extension dispatch in :meth:`Ingestor.discover`, and — load-bearing
    since #1304 — :func:`creek.ingest.routing.arbitrate`, which decides which
    ingestor owns a file by grouping fragments on exactly this string. A
    sub-unit discriminator belongs on :attr:`source_unit`, never here.
    """

    source_unit: str | None = None
    """The sub-unit of :attr:`source_path` this fragment addresses (#1305).

    ``None`` for the overwhelmingly common one-fragment-per-file case, and
    for the *single*-sheet workbook — a CSV or a one-sheet XLSX derives
    exactly the identity it derived before this field existed, which is what
    keeps every such fragment already in an operator's vault from being
    re-minted and duplicated on its next ingest.

    Set only where one file genuinely contains several independently
    identifiable units: today that is ``SpreadsheetIngestor``'s sheets. It
    reaches identity through :attr:`identity_key` and nothing else, so
    adding one cannot disturb a consumer of :attr:`source_path`.
    """

    timestamp: datetime
    """Timestamp associated with this fragment."""

    payload: Any = None
    """Optional typed intermediate from the ingestor backend.

    Carry the backend's structured dataclass (``SheetData``,
    ``PresentationData``, etc.) here instead of flattening it into
    :attr:`metadata`. :meth:`Ingestor.convert_to_markdown` reads the
    typed payload directly — no string-keyed dict access, no implicit
    schema duplicated between the parse and convert sites, and mypy
    catches a renamed field at the call site rather than letting it
    silently flow through.

    Defaults to ``None`` so the pre-FEAT-024 ingestors (chatgpt,
    markdown, html, code, …) that don't need structured payloads are
    not forced to set a slot they don't use.
    """

    @property
    def identity_key(self) -> str:
        """Return the string that identifies this fragment's source (#1305).

        The **only** input any identity derivation may take from a parsed
        fragment's provenance: :func:`generate_fragment_id`, the provenance
        entry in :meth:`Ingestor._process_fragment`, and the ledger key in
        :func:`creek.ingest.pipeline.attach_origin_key` all read this rather
        than :attr:`source_path`.

        Equal to :attr:`source_path` whenever :attr:`source_unit` is unset,
        so every ingestor that predates #1305 derives byte-identical ids.

        Returns:
            ``source_path`` composed with ``source_unit``, or ``source_path``
            unchanged when there is no unit.
        """
        return compose_source_unit(self.source_path, self.source_unit)


class ProvenanceEntry(BaseModel):
    """A structured provenance record for auditing ingest operations.

    Tracks which ingestor processed which source file, when, and whether
    the operation succeeded.
    """

    source_path: str
    """Path to the source file that was ingested."""

    ingestor_name: str
    """Name of the ingestor class that processed this file."""

    timestamp: datetime
    """When the ingest operation occurred."""

    fragment_id: str
    """The generated fragment ID for this entry."""

    status: str
    """Status of the ingest operation (e.g., 'success', 'error', 'skipped')."""


class IngestedFragment(BaseModel):
    """A pipeline-ready fragment paired with its converted markdown body.

    The four-stage ingestor protocol still produces ``ParsedFragment``
    objects internally; this sibling model is the *clean* hand-off shape
    consumed by the pipeline orchestrator and ``VaultWriter`` so the
    body and the structured ``Fragment`` metadata travel together
    instead of being smuggled through ``ParsedFragment.metadata``.

    Attributes:
        fragment: The validated ``Fragment`` carrying frontmatter
            metadata, classifications, and a deterministic ID.
        body: The converted Markdown body that will be written below
            the YAML frontmatter in the vault file.
    """

    fragment: Fragment
    body: str


class IngestResult(BaseModel):
    """Result of a complete ingest pipeline run.

    Collects all parsed fragments, provenance entries, and any error
    messages produced during the ingest process.
    """

    fragments: list[ParsedFragment] = Field(default_factory=list)
    """Parsed fragments produced by the ingest pipeline."""

    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    """Provenance records for auditing."""

    errors: list[str] = Field(default_factory=list)
    """Error messages collected during ingest."""

    discovered: int = 0
    """Count of inputs ``discover()`` found.

    Lets callers distinguish "no inputs found" (``discovered == 0``) from
    "inputs found but nothing parsed" (``discovered > 0`` yet ``fragments``
    empty) — the latter signals an unrecognized export format (#595)."""


# ---- Shared Utility Functions ----


def normalize_encoding(raw_bytes: bytes) -> tuple[str, str]:
    """Detect the encoding of raw bytes and convert to UTF-8 text.

    Uses ``chardet`` for encoding detection. Empty input returns an
    empty string with ``"utf-8"`` as the detected encoding.

    Args:
        raw_bytes: The raw bytes to detect and decode.

    Returns:
        A tuple of ``(decoded_text, detected_encoding)``.
    """
    if not raw_bytes:
        return "", "utf-8"

    detection = chardet.detect(raw_bytes)
    encoding = detection.get("encoding") or "utf-8"

    text = raw_bytes.decode(encoding, errors="replace")
    return text, encoding


def normalize_timestamp(ts_string: str, source_tz: str | None) -> datetime:
    """Parse a timestamp string and normalize to America/Los_Angeles.

    Supports ISO 8601 formats, date-only strings, and common datetime
    formats. If the timestamp is naive (no timezone info), ``source_tz``
    is used to localize it; if ``source_tz`` is also ``None``, UTC is
    assumed.

    Args:
        ts_string: The timestamp string to parse.
        source_tz: Optional IANA timezone name for naive timestamps.

    Returns:
        A timezone-aware ``datetime`` in America/Los_Angeles.

    Raises:
        ValueError: If the timestamp string cannot be parsed.
    """
    parsed = _parse_timestamp_string(ts_string)
    return _localize_naive_timestamp(parsed, source_tz).astimezone(LA_TZ)


def parse_authored_at(value: object, source_tz: str | None = None) -> datetime | None:
    """Parse a value into a tz-aware ``authored_at`` datetime (FEAT-031).

    The FEAT-031 contract: ``authored_at`` preserves the source's
    timezone when one is provided and defaults to UTC otherwise. This
    differs from :func:`normalize_timestamp`, which always converts to
    LA — appropriate for the LA-anchored ``Fragment.created`` /
    ``Fragment.ingested`` defaults but wrong for ``authored_at``,
    where a bare ``2024-03-15`` should land on March 15, not slip to
    March 14 17:00 PDT after the LA conversion.

    Accepts ``str``, ``datetime``, ``date``, and ``None`` so callers
    can hand off raw YAML-parsed values without re-stringifying. A
    ``date`` is promoted to UTC-midnight on that day; a ``datetime``
    is returned as-is when tz-aware, or UTC-localised when naive.

    Args:
        value: Raw value from frontmatter, EXIF, JSON, etc.
        source_tz: Optional IANA timezone name for naive inputs.
            Defaults to UTC per the FEAT-031 spec.

    Returns:
        A tz-aware datetime, or ``None`` when *value* is ``None`` or
        an empty/whitespace string. Never returns a naive datetime.

    Raises:
        ValueError: If *value* is a non-empty string that no parser
            recognises. Caller should ``try/except`` to fall through
            to the next candidate in its extraction chain rather than
            propagating a parse failure into the pipeline.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _localize_naive_timestamp(value, source_tz)
    if isinstance(value, date):  # bare YAML date — promote to midnight in source_tz
        midnight = datetime(value.year, value.month, value.day)
        return _localize_naive_timestamp(midnight, source_tz)
    text = str(value).strip()
    if not text:
        return None
    parsed = _parse_timestamp_string(text)
    return _localize_naive_timestamp(parsed, source_tz)


def _parse_timestamp_string(ts_string: str) -> datetime:
    """Parse a timestamp string into a datetime object.

    Tries ISO 8601 format first, then falls back to common formats.

    Args:
        ts_string: The timestamp string to parse.

    Returns:
        A datetime object (may be naive or aware).

    Raises:
        ValueError: If none of the known formats match.
    """
    # Try ISO 8601 first (handles timezone offsets)
    with suppress(ValueError):
        return datetime.fromisoformat(ts_string)

    # Try common formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(ts_string, fmt)
        except ValueError:
            continue

    msg = f"Unable to parse timestamp: {ts_string}"
    raise ValueError(msg)


def _localize_naive_timestamp(dt: datetime, source_tz: str | None) -> datetime:
    """Attach timezone info to a naive datetime.

    If the datetime is already timezone-aware, returns it unchanged.
    If naive, uses ``source_tz`` or defaults to UTC.

    Args:
        dt: The datetime to localize.
        source_tz: Optional IANA timezone name.

    Returns:
        A timezone-aware datetime.
    """
    if dt.tzinfo is not None:
        return dt

    tz = ZoneInfo(source_tz) if source_tz is not None else UTC
    return dt.replace(tzinfo=tz)


def generate_fragment_id(source: str, timestamp: datetime, content: str) -> str:
    """Generate a deterministic fragment ID from source, timestamp, and content.

    Computes a SHA-256 hash of the concatenated inputs and returns the
    first 12 hex characters prefixed with ``frag-``.

    Args:
        source: The source identifier (e.g., file path).
        timestamp: The fragment timestamp.
        content: The fragment text content.

    Returns:
        A deterministic ID string in the format ``frag-XXXXXXXXXXXX``.
    """
    hash_input = f"{source}:{timestamp.isoformat()}:{content}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"frag-{digest}"


def generate_child_fragment_id(
    parent_id: str,
    level: FragmentLevel,
    index: int,
) -> str:
    """Generate a deterministic child fragment ID (FEAT-020).

    The output matches :func:`generate_fragment_id`'s shape — a
    ``frag-XXXXXXXXXXXX`` SHA-256 prefix — so child IDs are
    indistinguishable from root IDs downstream and the same dedup,
    indexing, and resonance code paths work for both.

    Re-running the splitter (FEAT-021) or aggregator (FEAT-022) against
    an unchanged parent must produce the same child IDs in the same
    order so the second run is a no-op — that idempotency is why the
    tuple is ``(parent_id, level, index)`` and not, say,
    ``(parent_id, child_content)`` (content-keyed IDs would change
    every time a trivial whitespace edit landed upstream and explode
    the resonance graph).

    Args:
        parent_id: ID of the parent fragment (root or otherwise).
        level: Structural level of the child. Typed as
            :data:`creek.models.FragmentLevel` so MyPy strict catches
            invalid level strings (e.g. ``"chapter"``) at call sites
            instead of letting them silently flow through to the hash.
        index: Zero-based position of this child among its siblings.

    Returns:
        A deterministic ID string in the format ``frag-XXXXXXXXXXXX``.
    """
    hash_input = f"{parent_id}:{level}:{index}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"frag-{digest}"


def assemble_ingested_fragment(parsed: ParsedFragment) -> IngestedFragment:
    """Combine a ``ParsedFragment`` with its frontmatter into an ``IngestedFragment``.

    The four-stage ``ingest()`` orchestrator stashes both the converted
    Markdown body (``parsed.metadata["markdown"]``) and the generated
    Creek frontmatter dict (``parsed.metadata["frontmatter"]``) on the
    parsed fragment. This helper:

    1. Pulls the frontmatter dict out and deep-merges in the deterministic
       ``frag-<sha>`` ID so re-running the pipeline against the same
       source remains idempotent.
    2. Falls back to the source file stem for the title when an ingestor
       didn't supply one (e.g. ``DiscordIngestor``'s frontmatter omits it).
    3. Validates the dict into a ``Fragment`` and pairs it with the body.
    4. Runs the issue #878 hashtag pass
       (:func:`creek.classify.tags_pass.apply_tags`) so ``tags`` carries
       the body's hashtags — unioned with, and re-normalising, whatever
       the source's own frontmatter already declared.

    Step 4 lives here rather than in each ingestor because this function
    is the **universal ingest chokepoint**: every adapter and every CLI
    surface funnels through it —
    :meth:`creek.pipeline.Pipeline._run_ingestion` for ``creek process``,
    :func:`creek.ingest.pipeline.run_ingest` for ``creek ingest`` /
    ``creek sync`` / the ``creek.upload`` MCP tool,
    :func:`creek_mcp.tools.ingest.ingest_tool` for the ``creek.ingest`` MCP
    tool, and :mod:`creek.ingest.discord` for Discord capture. Wiring it
    once here is what makes ``tags`` populated for markdown, Discord,
    ChatGPT, Substack and the rest in one place, and it is why the ``creek
    process`` path needs no separate call further down the pipeline —
    nothing between here and the write mutates the body the hashtags come
    from.

    Callers are named by *symbol*, not by file and line. The four
    ``path:line`` citations this paragraph used to carry had every one of
    them rotted by the time #1305 read them (``creek/pipeline.py:498`` had
    drifted into an unrelated function), and the MCP ingest tool — a real
    caller — was missing entirely.

    Args:
        parsed: A parsed fragment produced by an ingestor's four-stage
            pipeline. Must have ``markdown`` and ``frontmatter`` keys
            populated in ``metadata``.

    Returns:
        An :class:`IngestedFragment` with structured metadata and body.

    Raises:
        KeyError: If ``parsed.metadata`` is missing the ``frontmatter``
            or ``markdown`` keys; this signals an ingestor contract
            violation rather than a recoverable parse error.
    """
    for required_key in ("frontmatter", "markdown"):
        if required_key not in parsed.metadata:
            msg = (
                f"Ingestor for {parsed.source_path!r} did not set "
                f"'{required_key}' in ParsedFragment.metadata; "
                "every Ingestor.ingest() must populate both 'markdown' "
                "and 'frontmatter' before yielding a fragment."
            )
            raise KeyError(msg)

    frontmatter_dict: dict[str, Any] = dict(parsed.metadata["frontmatter"])
    body: str = str(parsed.metadata["markdown"])

    frontmatter_dict["id"] = generate_fragment_id(
        parsed.identity_key,
        parsed.timestamp,
        parsed.content,
    )
    frontmatter_dict.setdefault("type", "fragment")
    frontmatter_dict.setdefault("title", Path(parsed.source_path).stem or "Untitled")

    fragment = Fragment.model_validate(frontmatter_dict)
    fragment = apply_tags(fragment, body)
    return IngestedFragment(fragment=fragment, body=body)


def file_modified_time(path: Path) -> datetime:
    """Return *path*'s modification time as a timezone-aware UTC datetime.

    This is the **identity anchor** for every file-based ingestor that has
    no embedded date to fall back on. :func:`generate_fragment_id` hashes
    the timestamp it returns, so the conversion must be a *pure function of
    the epoch float*: invariant under the host's ``TZ`` environment
    variable, its installed tzdata, its DST state, and its operating
    system. ``datetime.fromtimestamp(st_mtime, tz=UTC)`` is exactly that.

    Two ways of getting it wrong were live until #1329, and both are worth
    naming because both look like simplifications:

    * ``datetime.fromtimestamp(st_mtime)`` with no ``tz=`` renders the epoch
      in the *host's* local zone. One file then mints a different
      ``frag-<sha>`` in every timezone it is ingested from.
    * ``getattr(stat, "st_birthtime", stat.st_mtime)`` reads a field that
      exists on macOS/BSD and not on Linux. One file then mints a different
      id on a developer's laptop than in CI. Birth time is *not* available
      here for that reason; mtime is the only field every supported
      platform agrees on.

    Rendering in a fixed non-UTC zone (say LA) would also be
    host-independent, but it bakes a ``-07:00``/``-08:00`` offset that
    flips with DST into the hashed input. UTC is the rule for markdown,
    documents, generic, substack, spreadsheets, presentations and images.
    :mod:`creek.ingest.code` renders LA instead — a deliberate, equally
    host-independent divergence left alone by #1329 because re-minting it
    would orphan every code fragment, and code sources change too
    continuously for any reproduction-based migration to recover them. That
    last divergence is tracked by #1364.

    Note that this value answers "which file is this?", not "when was this
    written?". Authorship is ``authored_at``'s job (FEAT-031).

    Args:
        path: The file to stat.

    Returns:
        The file's mtime as a timezone-aware UTC datetime.
    """
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def create_provenance_entry(
    source_path: str,
    ingestor_name: str,
    timestamp: datetime,
    fragment_id: str,
    status: str,
) -> ProvenanceEntry:
    """Create a structured provenance record for an ingest operation.

    Args:
        source_path: Path to the source file.
        ingestor_name: Name of the ingestor class.
        timestamp: When the operation occurred.
        fragment_id: The generated fragment ID.
        status: Operation status (e.g., 'success', 'error', 'skipped').

    Returns:
        A ``ProvenanceEntry`` instance.
    """
    return ProvenanceEntry(
        source_path=source_path,
        ingestor_name=ingestor_name,
        timestamp=timestamp,
        fragment_id=fragment_id,
        status=status,
    )


# ---- Abstract Base Class ----


class Ingestor(abc.ABC):
    """Abstract base class for all Creek ingestors.

    Defines the four-stage ingest pipeline:

    1. **discover** — find all files/records at a source path
    2. **parse** — extract structured content from a raw document
    3. **convert_to_markdown** — convert a parsed fragment to clean Markdown
    4. **generate_frontmatter** — produce YAML frontmatter metadata

    The concrete ``ingest()`` method orchestrates these stages and collects
    results, provenance, and errors into an ``IngestResult``.

    Subclasses must implement all four abstract methods.
    """

    @abc.abstractmethod
    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find all files or records at the given source path.

        Args:
            source_path: The directory or file path to search.

        Returns:
            A list of ``RawDocument`` objects found at the source.
        """

    @abc.abstractmethod
    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Extract structured content from a raw document.

        Args:
            raw: The raw document to parse.

        Returns:
            A list of ``ParsedFragment`` objects extracted from the document.
        """

    @abc.abstractmethod
    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed fragment to clean Markdown.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            A Markdown-formatted string.
        """

    @abc.abstractmethod
    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter metadata for a parsed fragment.

        Args:
            fragment: The parsed fragment.

        Returns:
            A dict of frontmatter key-value pairs.
        """

    def ingest(self, source_path: Path) -> IngestResult:
        """Orchestrate the full ingest pipeline: discover, parse, convert, frontmatter.

        Calls ``discover()`` to find documents, then for each document calls
        ``parse()`` to extract fragments. For each fragment, calls
        ``convert_to_markdown()`` and ``generate_frontmatter()``. Collects
        all results into an ``IngestResult``, handling errors gracefully.

        Args:
            source_path: The directory or file path to ingest from.

        Returns:
            An ``IngestResult`` containing fragments, provenance, and errors.

        Raises:
            EscapingSymlinkError: When *source_path* is, or contains, a
                symlink whose target resolves outside the tree the caller
                named (#1294).
        """
        # The single containment gate for all eleven registered ingestors,
        # placed here rather than inside each ``discover()`` for two reasons.
        # It runs BEFORE any walk, so no ingestor reads through the link on
        # its way to refusing; and a future twelfth ingestor inherits the
        # guard by construction instead of by someone remembering. A
        # module-level function rather than a method, so a subclass cannot
        # weaken it by overriding.
        #
        # It must stay OUTSIDE ``_discover_safe``, whose ``except Exception``
        # collects failures into ``result.errors`` and returns ``[]``. An
        # empty discovery leaves ``run_ingest``'s ``seen_keys`` empty, and
        # ``creek/ingest/pipeline.py::tomb_missing_units`` then soft-tombs
        # every ``ledger.live_keys()`` the pass did not see — so a refusal
        # swallowed there would orphan every fragment previously ingested
        # from this source. A containment guard must never become a deletion
        # primitive (#1294).
        assert_source_contained(source_path)
        result = IngestResult()
        ingestor_name = type(self).__name__
        now = datetime.now(tz=LA_TZ)

        # Stage 1: Discover
        raw_docs = self._discover_safe(source_path, result)
        result.discovered = len(raw_docs)

        # Stages 2-4: Parse, Convert, Frontmatter
        for raw_doc in raw_docs:
            self._process_document(raw_doc, result, ingestor_name, now)

        return result

    def _discover_safe(
        self, source_path: Path, result: IngestResult
    ) -> list[RawDocument]:
        """Safely call discover(), catching and logging errors.

        Args:
            source_path: The path to discover documents at.
            result: The IngestResult to append errors to.

        Returns:
            A list of discovered RawDocuments, or empty on error.
        """
        try:
            return self.discover(source_path)
        except Exception as exc:
            result.errors.append(f"discover error: {exc}")
            logger.exception("Error during discover for %s", source_path)
            return []

    def _process_document(
        self,
        raw_doc: RawDocument,
        result: IngestResult,
        ingestor_name: str,
        now: datetime,
    ) -> None:
        """Process a single raw document through parse, convert, and frontmatter.

        Args:
            raw_doc: The raw document to process.
            result: The IngestResult to collect into.
            ingestor_name: The class name of this ingestor.
            now: The current timestamp for provenance.
        """
        fragments = self._parse_safe(raw_doc, result)
        for fragment in fragments:
            self._process_fragment(fragment, result, ingestor_name, now)

    def _parse_safe(
        self, raw_doc: RawDocument, result: IngestResult
    ) -> list[ParsedFragment]:
        """Safely call parse(), catching and logging errors.

        Args:
            raw_doc: The raw document to parse.
            result: The IngestResult to append errors to.

        Returns:
            A list of parsed fragments, or empty on error.
        """
        try:
            return self.parse(raw_doc)
        except Exception as exc:
            result.errors.append(f"parse error for {raw_doc.path}: {exc}")
            logger.exception("Error parsing %s", raw_doc.path)
            return []

    def _process_fragment(
        self,
        fragment: ParsedFragment,
        result: IngestResult,
        ingestor_name: str,
        now: datetime,
    ) -> None:
        """Process a single fragment through convert and frontmatter stages.

        Args:
            fragment: The parsed fragment to process.
            result: The IngestResult to collect into.
            ingestor_name: The class name of this ingestor.
            now: The current timestamp for provenance.
        """
        # ``identity_key``, not ``source_path``: a provenance entry per sheet,
        # matching the one fragment id per sheet the same key mints (#1305).
        frag_id = generate_fragment_id(
            fragment.identity_key, fragment.timestamp, fragment.content
        )

        # Stage 3: Convert to markdown
        markdown = self._convert_safe(fragment, result)

        # Stage 4: Generate frontmatter
        frontmatter = self._frontmatter_safe(fragment, result)

        if markdown is not None and frontmatter is not None:
            fragment.metadata["markdown"] = markdown
            fragment.metadata["frontmatter"] = frontmatter
            result.fragments.append(fragment)
            result.provenance.append(
                create_provenance_entry(
                    source_path=fragment.source_path,
                    ingestor_name=ingestor_name,
                    timestamp=now,
                    fragment_id=frag_id,
                    status="success",
                )
            )
        else:
            result.provenance.append(
                create_provenance_entry(
                    source_path=fragment.source_path,
                    ingestor_name=ingestor_name,
                    timestamp=now,
                    fragment_id=frag_id,
                    status="error",
                )
            )

    def _convert_safe(
        self, fragment: ParsedFragment, result: IngestResult
    ) -> str | None:
        """Safely call convert_to_markdown(), catching errors.

        Args:
            fragment: The fragment to convert.
            result: The IngestResult to append errors to.

        Returns:
            The markdown string, or None on error.
        """
        try:
            return self.convert_to_markdown(fragment)
        except Exception as exc:
            result.errors.append(f"convert error for {fragment.source_path}: {exc}")
            logger.exception("Error converting %s to markdown", fragment.source_path)
            return None

    def _frontmatter_safe(
        self, fragment: ParsedFragment, result: IngestResult
    ) -> dict[str, Any] | None:
        """Safely call generate_frontmatter(), catching errors.

        Args:
            fragment: The fragment to generate frontmatter for.
            result: The IngestResult to append errors to.

        Returns:
            The frontmatter dict, or None on error.
        """
        try:
            return self.generate_frontmatter(fragment)
        except Exception as exc:
            result.errors.append(f"frontmatter error for {fragment.source_path}: {exc}")
            logger.exception(
                "Error generating frontmatter for %s", fragment.source_path
            )
            return None
