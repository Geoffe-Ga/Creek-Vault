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
- **Discovery signalling**: ``PartialDiscoveryError``, which an ingestor raises to
  report that it enumerated only part of a source while still handing back
  what it read (#1444).
"""

from __future__ import annotations

import abc
import hashlib
import logging
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from creek._containment import assert_source_contained
from creek.classify.tags_pass import apply_tags
from creek.ingest.encoding import UndecodableBytesError, decode_bytes
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


PASSTHROUGH_FRONTMATTER_KEYS: Final[frozenset[str]] = frozenset(
    {"sheet", "rows", "columns"}
)
"""Unmodelled frontmatter keys an ingestor may put on the vault file (#1392).

``Fragment`` leaves pydantic's default ``extra="ignore"``, so any key an
ingestor emits that the model does not declare is discarded by
``Fragment.model_validate`` and never reaches disk. That is the right default
— it is what stops an ingestor's typo from becoming permanent frontmatter —
but it also silently dropped the spreadsheet ingestor's ``sheet``, ``rows``
and ``columns``, which post-#1305 are the only structured record of *which
sheet* a per-sheet fragment came from. #1305 pinned the drop as
accepted-for-now and named #1392 as where to decide it; this is the decision.

A key belongs here only when it is **structured provenance an automated
consumer needs and ``Fragment`` deliberately does not model**. Everything else
an ingestor emits is still dropped. The allowlist exists so that surfacing
these three did not become ``extra="allow"``, where every future typo lands on
disk in a thousand fragments before anyone notices.

Deliberately a module-level constant rather than a per-ingestor class
attribute: :func:`assemble_ingested_fragment` is the universal chokepoint and
receives only a ``ParsedFragment``, with no ingestor instance in hand. One
declaration site beats threading an instance through it.

Note this is *not* a model field on ``Fragment`` or ``FragmentSource``.
``_write_model`` dumps with ``model_dump(mode="json")`` and no
``exclude_none``, so a nullable ``sheet`` field would print ``sheet: null`` on
every fragment from every ingestor — the way ``author_name``, ``channel`` and
``origin_key`` already do. Riding the writer's ``extra_frontmatter`` seam on
*presence* keeps that structurally unreachable.
"""


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
        extra_frontmatter: Unmodelled frontmatter keys, drawn from
            :data:`PASSTHROUGH_FRONTMATTER_KEYS`, that this ingestor
            actually emitted (#1392). Empty for every ingestor that
            emitted none, so a fragment never gains a key its source
            had nothing to say about.
    """

    fragment: Fragment
    body: str
    extra_frontmatter: dict[str, Any] = Field(default_factory=dict)


DISCOVERY_ERROR_PREFIX = "discover error: "
"""Prefix under which every discovery-stage failure enters ``errors``.

One string, read by both arms of :meth:`Ingestor._discover_safe` and by
:meth:`IngestResult.discovery_failure_count`, so the count an operator is
shown and the lines they read can never describe different sets.
"""


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

    discovery_complete: bool = True
    """Whether ``discover()`` managed to enumerate the whole source (#1444).

    ``True`` means the walk saw the whole tree, so a ledgered unit this pass
    did not see can be *proven* absent and
    :func:`creek.ingest.pipeline.tomb_missing_units` may soft-tomb it.
    ``False`` means some part of the source could not be listed or read, which
    makes absence unknowable rather than proven.

    **The default is authoritative on purpose.** Every existing construction
    of this model keeps exactly today's meaning, so #674's rule — a genuinely
    *readable* empty directory MUST still tomb — survives untouched, and only
    a discovery that reported a failure lowers the flag. An incomplete
    enumeration is not evidence of deletion, so it must not arm the sweep."""

    def mark_discovery_incomplete(self) -> None:
        """Record that ``discover()`` did not see the whole source (#1444).

        A method rather than an assignment at each call site: one grep
        target for "who disarms the tomb sweep", and one home for the
        reason. The transition is deliberately one-way — nothing raises
        the flag back — because later success elsewhere in the tree cannot
        un-fail the part the walk never reached.
        """
        self.discovery_complete = False

    def discovery_failure_count(self) -> int:
        """Count the discovery-stage failures recorded on this result.

        Derived from :attr:`errors` rather than tallied into a second
        field, so the number an operator is shown is exactly the number of
        lines they can go and read; a second counter is a second thing that
        can disagree. Discovery failures are the entries carrying
        :data:`DISCOVERY_ERROR_PREFIX`, which only
        :meth:`Ingestor._discover_safe` writes.

        Returns:
            How many discovery failures were recorded — necessarily ``0``
            whenever :attr:`discovery_complete` is ``True``, because the
            two are written by the same arms.
        """
        return sum(1 for err in self.errors if err.startswith(DISCOVERY_ERROR_PREFIX))


class PartialDiscoveryError(Exception):
    """``discover()`` enumerated part of a source but could not see all of it.

    Raised rather than returned because :meth:`Ingestor.discover` —
    ``(self, source_path) -> list[RawDocument]`` — is the public extension
    point all eleven registered ingestors implement, and that signature has
    no channel for "…and here is the part I could not see". Widening it to a
    tuple would rewrite every implementation to carry a fact ten of them
    never produce.

    Unlike a bare ``raise OSError``, this one does **not** discard the
    documents that *were* enumerated: :meth:`Ingestor._discover_safe` returns
    them, so an unreadable corner of a tree costs the tombing pass and never
    the ingest itself. A re-raise would return ``[]`` instead and turn one
    unreadable junk folder into a permanent, silent ingest outage on the
    unattended ``creek sync`` path (#1444). The idiom is
    :attr:`http.client.IncompleteRead.partial`, which carries the bytes that
    did arrive on the very exception reporting that the read was short.

    Attributes:
        documents: Everything the walk did successfully read.
        reasons: One operator-readable line per part it could not, in the
            house style of the ``errors`` channel they are recorded on.
    """

    def __init__(self, documents: list[RawDocument], reasons: list[str]) -> None:
        """Carry the partial harvest alongside the reasons it is partial.

        Args:
            documents: Everything the walk did successfully read.
            reasons: One operator-readable line per part it could not.
        """
        self.documents = documents
        self.reasons = reasons
        super().__init__(f"discovery incomplete: {'; '.join(reasons)}")


# ---- Shared Utility Functions ----


def normalize_encoding(raw_bytes: bytes) -> tuple[str, str]:
    """Detect the encoding of raw bytes and convert to UTF-8 text.

    The decision itself belongs to
    :func:`creek.ingest.encoding.decode_bytes`, which every ingest path
    shares (#1600). Before that, this trusted ``chardet`` at any
    confidence, which rewrote a genuine cp1252 file's ``£85`` to
    ``Ł85`` on the markdown, documents, code, chatgpt, claude and
    substack paths at once.

    Two properties of this function are contract rather than detail,
    because ~19 call sites depend on them — several inside discovery
    loops that guard only ``OSError``, and three ingestors that later
    ``decode()`` by the codec name this returns:

    * it never raises, so binary content comes back as ``latin-1``
      text rather than as an exception;
    * the codec name it reports always decodes *raw_bytes*, so a bare
      ``raw_bytes.decode(name)`` downstream cannot fail.

    Empty input returns an empty string with ``"utf-8"``.

    Args:
        raw_bytes: The raw bytes to detect and decode.

    Returns:
        A tuple of ``(decoded_text, detected_encoding)``.
    """
    if not raw_bytes:
        return "", "utf-8"

    try:
        decoded = decode_bytes(raw_bytes)
    except UndecodableBytesError:
        return raw_bytes.decode("latin-1"), "latin-1"
    return decoded.text, decoded.codec


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


def safe_parse_authored_at(candidate: object) -> datetime | None:
    """Parse one ``authored_at`` candidate, swallowing the parse failure.

    Every format-specific ingestor walks an **ordered** candidate chain
    — ``created`` before ``modified``, ``/CreationDate`` before
    ``/ModDate``, ``\\creatim`` before ``\\revtim`` — and a candidate
    that fails to parse must fall through to the next one rather than
    take the whole file down with it. :func:`parse_authored_at` raises
    :class:`ValueError` for an unrecognised non-empty string precisely
    so a caller can make that decision; this is that decision, written
    once.

    It lived as a byte-identical private copy in
    :mod:`creek.ingest.spreadsheets` and
    :mod:`creek.ingest.presentations` before #855 promoted it here.
    Note that no ``None`` guard is needed:
    :func:`parse_authored_at` already returns ``None`` for ``None`` and
    for blank strings, so one would be unreachable.

    Args:
        candidate: A raw value from core properties, an info
            dictionary, EXIF, or frontmatter.

    Returns:
        A tz-aware datetime, or ``None`` when the candidate is absent,
        blank, or unparseable. Never raises, and never guesses a date
        the source did not supply.
    """
    try:
        return parse_authored_at(candidate)
    except ValueError:
        return None


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

    Re-running the splitter (FEAT-021) against an unchanged parent must
    produce the same child IDs in the same order so the second run is a
    no-op — that idempotency is why the tuple is
    ``(parent_id, level, index)`` and not, say,
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
    # Keyed on PRESENCE, never on a nullable field: a fragment whose ingestor
    # emitted no dimensions carries an empty dict and therefore gains no
    # frontmatter line at all (#1392). See PASSTHROUGH_FRONTMATTER_KEYS.
    extras = {
        key: frontmatter_dict[key]
        for key in PASSTHROUGH_FRONTMATTER_KEYS
        if key in frontmatter_dict
    }
    return IngestedFragment(fragment=fragment, body=body, extra_frontmatter=extras)


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

        **What happens to a key ``Fragment`` does not model** (#1392) — the
        rule, because returning a key here is not the same as writing it:

        * A key ``Fragment`` declares is validated onto the model and written.
        * A key in :data:`PASSTHROUGH_FRONTMATTER_KEYS` is *not* a model
          field, but is carried around the model to the vault file via
          ``IngestedFragment.extra_frontmatter`` and the writer's
          ``extra_frontmatter`` seam. Today: ``sheet``, ``rows``, ``columns``.
        * **Every other key is silently dropped.** ``Fragment`` leaves
          pydantic's default ``extra="ignore"``, so ``model_validate``
          discards it and ``_write_model`` serialises model fields only. It
          will not reach disk and nothing will warn you.

        So a new key needs either a ``Fragment`` field or an allowlist entry.
        Emitting one and expecting it on disk is the mistake #1305 recorded
        and #1392 fixed for the three spreadsheet dimensions; the allowlist is
        deliberately narrow so that an ingestor's *typo* still gets dropped
        rather than becoming permanent frontmatter.

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
        # collects failures into ``result.errors``, returns ``[]`` and lets
        # the run continue. A refusal is not a degraded pass: #1294 decided
        # that the ingest *write* path refuses an escaping source outright
        # rather than reading part of it, so ``EscapingSymlinkError`` has to
        # propagate to the caller instead of becoming one more collected
        # error on a run that then proceeds. (The orphaning that swallowing
        # it used to cause is separately closed: ``_discover_safe`` now marks
        # ``IngestResult.discovery_complete`` false on every failure arm,
        # which disarms the tomb sweep — see #1444.)
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

        Both failure arms mark the result's discovery **incomplete**, and
        that is the whole #1444 fix: the sole ``def ingest(self`` in
        ``creek/ingest/`` is the one above, so all eleven registered
        ingestors — and any future twelfth — inherit the protection by
        construction rather than by each remembering to ask.

        The arms differ only in what survives. :class:`PartialDiscoveryError`
        carries the documents the walk *did* read, so an unreadable corner
        costs the tombing pass and not the harvest; any other exception left
        the ingestor with nothing to hand back, so the harvest is empty.

        Args:
            source_path: The path to discover documents at.
            result: The IngestResult to append errors to.

        Returns:
            The documents discovery managed to read: all of them on success,
            the partial harvest for :class:`PartialDiscoveryError`, empty
            otherwise.
        """
        try:
            return self.discover(source_path)
        except PartialDiscoveryError as partial:
            result.errors.extend(
                f"{DISCOVERY_ERROR_PREFIX}{reason}" for reason in partial.reasons
            )
            result.mark_discovery_incomplete()
            logger.warning(
                "Incomplete discovery for %s: %s",
                source_path,
                "; ".join(partial.reasons),
            )
            return partial.documents
        except Exception as exc:
            # Marked before the message is composed: a discovery that raised
            # saw an unknown amount of its source, and "unknown" may not arm
            # a deletion primitive whatever the exception turns out to say.
            result.mark_discovery_incomplete()
            result.errors.append(f"{DISCOVERY_ERROR_PREFIX}{exc}")
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
