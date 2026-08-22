"""Idempotent re-ingest for :class:`creek.ingest.generic.GenericIngestor` (#911).

``GenericIngestor.parse`` stamps ``timestamp=datetime.now(tz=LA_TZ)``, and that
timestamp is hashed into the fragment id by
:func:`creek.ingest.base.generate_fragment_id`
(``f"{source}:{timestamp.isoformat()}:{content}"``). Every re-ingest of an
*unchanged* file therefore mints a brand-new id and writes a brand-new fragment
— unbounded duplicates. The stable answer is the file's mtime, which ``parse``
already computes for ``metadata['authored_at']`` and then throws away.

A second, independent defect blocks the acceptance test:
``generate_frontmatter`` emits ``"platform": "unknown"``, which is **not** a
member of :class:`creek.models.SourcePlatform` (the fallback member is
``OTHER``). ``assemble_ingested_fragment`` therefore raises a pydantic
``ValidationError``, ``run_ingest`` swallows it into ``result.errors``, and the
fragment is silently dropped — generic fragments have never landed in a vault.
That is why every assertion here pins ``errors == []`` *and* an exact count: a
naive "ingest twice, count files" test would otherwise pass vacuously on
``0 == 0``.

Clock injection: there is no freezegun/time_machine in this repo, so the module
attribute ``creek.ingest.generic.datetime`` is monkeypatched with an
**advancing** fake. It must advance — a frozen clock would make the buggy code
produce equal ids and destroy the RED. Only the ``creek.ingest.generic``
attribute is patched; ``Ingestor.ingest`` calls ``datetime.now(tz=LA_TZ)`` for
provenance and that call is correct. No test here depends on real elapsed time.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.ingest import INGESTOR_REGISTRY, generic
from creek.ingest.base import RawDocument, assemble_ingested_fragment
from creek.ingest.generic import GenericIngestor
from creek.ingest.pipeline import run_ingest
from creek.models import SourcePlatform
from creek.time import LA_TZ
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from datetime import tzinfo

    from creek.ingest.base import ParsedFragment
    from creek.ingest.pipeline import IngestRunResult

# Fake wall-clock start + step. The step MUST be non-zero: it is the thing that
# makes the buggy `datetime.now()` id unstable and so the thing that makes these
# tests a real RED rather than an accident of a coarse clock.
_CLOCK_START = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_CLOCK_STEP = timedelta(seconds=37)

# Deterministic file mtimes. These are the values the fragment timestamp (and
# therefore the fragment id) must be derived from.
_PINNED_MTIME = datetime(2024, 3, 15, 14, 30, 0, tzinfo=UTC)
_LATER_MTIME = datetime(2024, 6, 1, 9, 0, 0, tzinfo=UTC)

_NOTE_BODY = "Idempotent generic ingest.\n"


# ---- Fake clock ----


class _AdvancingClock:
    """Stand-in for the ``datetime`` class whose ``now()`` advances each call.

    Only ``now()`` is exercised by :mod:`creek.ingest.generic` at runtime (the
    module's other ``datetime`` references are annotations, which are strings
    under ``from __future__ import annotations``), so a minimal duck-type is
    enough and avoids depending on a time-freezing plugin the repo does not
    have.
    """

    def __init__(self, start: datetime, step: timedelta) -> None:
        """Start the clock at *start*, advancing by *step* on every ``now()``."""
        self._current = start
        self._step = step
        self.ticks: list[datetime] = []

    def now(self, tz: tzinfo | None = None) -> datetime:
        """Return the next tick (converted to *tz*) and advance the clock."""
        value = self._current
        self._current = self._current + self._step
        if tz is not None:
            value = value.astimezone(tz)
        self.ticks.append(value)
        return value


# ---- Fixtures ----


@pytest.fixture()
def ingestor() -> GenericIngestor:
    """Create a GenericIngestor instance for testing."""
    return GenericIngestor()


@pytest.fixture()
def advancing_clock(monkeypatch: pytest.MonkeyPatch) -> _AdvancingClock:
    """Patch ``creek.ingest.generic.datetime`` with the advancing fake clock.

    Scoped to the ``creek.ingest.generic`` module attribute only —
    ``creek.ingest.base`` keeps the real clock for provenance stamping.
    """
    clock = _AdvancingClock(start=_CLOCK_START, step=_CLOCK_STEP)
    monkeypatch.setattr(generic, "datetime", clock)
    return clock


# ---- Helpers ----


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a minimal vault the writer can target."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Unsorted",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _pin_mtime(path: Path, moment: datetime) -> None:
    """Pin *path*'s atime/mtime to *moment* so its mtime is deterministic."""
    epoch = moment.timestamp()
    os.utime(path, (epoch, epoch))


def _make_source_note(tmp_path: Path, moment: datetime = _PINNED_MTIME) -> Path:
    """Write ``src/note.txt`` OUTSIDE the vault with a pinned mtime."""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    note = src / "note.txt"
    note.write_text(_NOTE_BODY, encoding="utf-8")
    _pin_mtime(note, moment)
    return note


def _raw_for(path: Path) -> RawDocument:
    """Build a RawDocument for an on-disk file, as ``discover()`` would."""
    return RawDocument(
        path=path,
        content=path.read_bytes(),
        metadata={"source_type": "generic"},
        detected_encoding="utf-8",
    )


def _assemble_fragment_id(ingestor: GenericIngestor, parsed: ParsedFragment) -> str:
    """Run the convert + frontmatter stages and return the assembled fragment id."""
    parsed.metadata["markdown"] = ingestor.convert_to_markdown(parsed)
    parsed.metadata["frontmatter"] = ingestor.generate_frontmatter(parsed)
    return assemble_ingested_fragment(parsed).fragment.id


def _run_generic_ingest(vault: Path, source_dir: Path) -> IngestRunResult:
    """Drive the real shared pipeline with the registered generic ingestor."""
    return run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["generic"],
        source_type="generic",
        input_path=source_dir,
        vault_path=vault,
    )


def _fragment_files(vault: Path) -> list[Path]:
    """Return every fragment markdown file written under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _fragment_ids(vault: Path) -> set[str]:
    """Return the set of fragment ids written under ``01-Fragments``."""
    return {str(frontmatter.load(str(p))["id"]) for p in _fragment_files(vault)}


# ---- Acceptance: re-ingest is a no-op ----


class TestGenericReingestIsIdempotent:
    """Re-ingesting an unchanged file must not mint a second fragment (#911)."""

    @pytest.mark.usefixtures("advancing_clock")
    def test_reingest_of_unchanged_file_writes_no_duplicate(
        self,
        tmp_path: Path,
    ) -> None:
        """Two full ingests of one unchanged file leave exactly one fragment.

        The wall clock advances between the two runs, so this only holds if the
        fragment id is derived from something stable (the file's mtime) rather
        than from ``datetime.now()``.
        """
        vault = _make_vault(tmp_path)
        note = _make_source_note(tmp_path)

        first = _run_generic_ingest(vault, note.parent)
        assert first.errors == []
        assert len(_fragment_files(vault)) == 1
        ids_after_first = _fragment_ids(vault)

        second = _run_generic_ingest(vault, note.parent)
        assert second.errors == []
        assert len(_fragment_files(vault)) == 1
        assert _fragment_ids(vault) == ids_after_first


# ---- parse(): timestamp stability ----


class TestGenericParseTimestampStability:
    """``parse()`` must stamp a stable, mtime-derived timestamp (#911)."""

    @pytest.mark.usefixtures("advancing_clock")
    def test_parse_timestamp_is_stable_across_calls(
        self,
        ingestor: GenericIngestor,
        tmp_path: Path,
    ) -> None:
        """Parsing the same unchanged file twice yields one timestamp and one id."""
        note = _make_source_note(tmp_path)
        first = ingestor.parse(_raw_for(note))[0]
        second = ingestor.parse(_raw_for(note))[0]

        assert first.timestamp == second.timestamp
        assert _assemble_fragment_id(ingestor, first) == _assemble_fragment_id(
            ingestor, second
        )

    @pytest.mark.usefixtures("advancing_clock")
    def test_parse_timestamp_equals_file_mtime_in_utc(
        self,
        ingestor: GenericIngestor,
        tmp_path: Path,
    ) -> None:
        """The timestamp is the file's mtime expressed in UTC, not local time.

        The hashed ``timestamp.isoformat()`` must end in ``+00:00`` on every
        host, so fragment identity is not a function of the host's tzdata or
        DST state.
        """
        note = _make_source_note(tmp_path)
        parsed = ingestor.parse(_raw_for(note))[0]

        assert parsed.timestamp == _PINNED_MTIME
        assert parsed.timestamp.utcoffset() == timedelta(0)
        assert parsed.timestamp.isoformat().endswith("+00:00")

    @pytest.mark.usefixtures("advancing_clock")
    def test_changed_mtime_yields_new_id(
        self,
        ingestor: GenericIngestor,
        tmp_path: Path,
    ) -> None:
        """Identical content with a bumped mtime is a *different* fragment.

        Documents the accepted trade-off of keying identity on the mtime, and
        guards against a "fix" that simply drops the timestamp from the hash:
        with source path and content held constant, the ids can only differ if
        the timestamp still participates.
        """
        note = _make_source_note(tmp_path)
        before = ingestor.parse(_raw_for(note))[0]

        _pin_mtime(note, _LATER_MTIME)
        after = ingestor.parse(_raw_for(note))[0]

        assert before.content == after.content
        assert before.source_path == after.source_path
        assert before.timestamp == _PINNED_MTIME
        assert after.timestamp == _LATER_MTIME
        assert _assemble_fragment_id(ingestor, before) != _assemble_fragment_id(
            ingestor, after
        )

    def test_synthetic_document_falls_back_to_wall_clock(
        self,
        ingestor: GenericIngestor,
        advancing_clock: _AdvancingClock,
    ) -> None:
        """A RawDocument with no file behind it keeps the wall-clock fallback.

        Preserving this behaviour is an explicit constraint of #911: there is
        no mtime to key on, so ``timestamp`` is the injected ``now`` and
        ``authored_at`` is honestly ``None``. Asserting that the two parses see
        *different* wall-clock values also pins that the fake clock genuinely
        advances — a frozen clock would make the duplicate-id tests vacuous.
        """
        raw = RawDocument(
            path=Path("/nonexistent/synthetic/note.txt"),
            content=b"some synthetic text",
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        first = ingestor.parse(raw)[0]
        second = ingestor.parse(raw)[0]

        assert first.timestamp == _CLOCK_START.astimezone(LA_TZ)
        assert second.timestamp == (_CLOCK_START + _CLOCK_STEP).astimezone(LA_TZ)
        assert second.timestamp > first.timestamp
        assert first.metadata["authored_at"] is None
        assert second.metadata["authored_at"] is None
        assert advancing_clock.ticks == [first.timestamp, second.timestamp]


# ---- generate_frontmatter(): platform must be a real SourcePlatform ----


class TestGenericFrontmatterPlatform:
    """``source.platform`` must be a member of ``SourcePlatform`` (#911)."""

    @pytest.mark.usefixtures("advancing_clock")
    def test_generic_frontmatter_platform_is_a_valid_source_platform(
        self,
        ingestor: GenericIngestor,
        tmp_path: Path,
    ) -> None:
        """The emitted platform validates and still routes to Unsorted.

        ``"unknown"`` is not a ``SourcePlatform`` member, so the assembled
        fragment never validated and generic content never reached the vault.
        The corrected value is ``SourcePlatform.OTHER``, whose routing target
        is the unchanged ``01-Fragments/Unsorted/``.
        """
        vault = _make_vault(tmp_path)
        note = _make_source_note(tmp_path)
        parsed = ingestor.parse(_raw_for(note))[0]

        fm = ingestor.generate_frontmatter(parsed)
        assert SourcePlatform(fm["source"]["platform"]) is SourcePlatform.OTHER

        parsed.metadata["markdown"] = ingestor.convert_to_markdown(parsed)
        parsed.metadata["frontmatter"] = fm
        assembled = assemble_ingested_fragment(parsed)
        written = VaultWriter(vault_path=vault).write_fragment(
            assembled.fragment, body=assembled.body
        )
        assert written.parent == vault / "01-Fragments" / "Unsorted"
