"""A fragment's id must not depend on the host it was ingested on (#1329).

``generate_fragment_id`` hashes the fragment's timestamp
(``creek/ingest/base.py``: ``f"{source}:{timestamp.isoformat()}:{content}"``),
so any host-dependent input to that timestamp leaks straight into the
fragment's identity. Two such leaks existed:

1. **The host ``TZ`` env var.** ``datetime.fromtimestamp(mtime)`` with no
   ``tz=`` renders the epoch in the *host's* local zone, and
   ``normalize_timestamp`` then re-interpreted that naive wall-clock as UTC
   before shifting it to LA — a double shift. One file, four ids.
2. **The host operating system.** ``getattr(stat, "st_birthtime", st_mtime)``
   reads a field that exists on macOS/BSD but not on Linux, so the same file
   minted one id on a developer's Mac and a different one in Linux CI.

Both are pinned here. The assertions are on the **fragment id**, not merely on
``created``: the timestamp's UTC-offset *string* is inside the hashed input, so
a change that fixed the instant but re-rendered the offset would slip past a
``created``-only check.

Nothing in this module uses ``--incremental``/``since``. ``should_skip_unit``
short-circuits the write for an unchanged ledgered unit, so an incremental-mode
test is green while the bug is live.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from creek.ingest.base import assemble_ingested_fragment
from creek.ingest.documents import DocumentIngestor
from creek.ingest.markdown import MarkdownIngestor

if TYPE_CHECKING:
    from collections.abc import Iterator

ZONES = ("UTC", "America/Los_Angeles", "Europe/Berlin", "Australia/Sydney")
"""Host timezones the derivation must be invariant under.

Chosen to straddle the epoch instant: 2024-03-15T09:00:00Z is the *previous*
calendar day in Los Angeles and the *same* day in Sydney, so a naive
derivation cannot accidentally agree.
"""

PINNED_MTIME = 1710493200.0
"""2024-03-15T09:00:00+00:00 — the fixed mtime every case in this module uses."""

EXPECTED_INSTANT = datetime(2024, 3, 15, 9, 0, tzinfo=UTC)
"""The UTC instant ``PINNED_MTIME`` denotes."""

MARKDOWN_ID = "frag-b145fde4406d"
"""``generate_fragment_id("note.md", 2024-03-15T09:00:00+00:00, "body text")``.

Pinned as a literal rather than asserted only as ``len(ids) == 1`` because a
future "simplification" that made all four zones agree on some *other*
derivation would slip past a set-size check.
"""

DOCUMENT_ID = "frag-e71aebc3ad98"
"""``generate_fragment_id("note.txt", 2024-03-15T09:00:00+00:00, "# body text")``.

``DocumentIngestor`` renders a ``.txt`` body through the markdown converter,
which promotes the first line to a heading — hence ``"# body text"``.
"""


@pytest.fixture
def restore_host_tz() -> Iterator[None]:
    """Restore the process timezone after a test perturbs ``TZ``.

    ``time.tzset()`` mutates *process-global* state that ``monkeypatch.setenv``
    alone cannot undo, so a leaked ``TZ`` would poison every later test in the
    session.

    Yields:
        ``None``; the restoration happens on teardown.
    """
    original = os.environ.get("TZ")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def _write_source(name: str, body: str = "body text\n") -> Path:
    """Create *name* in the CWD with its mtime pinned to ``PINNED_MTIME``.

    The path is deliberately **relative**. ``assemble_ingested_fragment``
    hashes ``parsed.source_path``, and the file ingestors set that to
    ``str(raw.path)``, so discovering the file through an absolute
    ``tmp_path`` would put the per-run pytest temp directory inside the hash
    and make the pinned literals above unreproducible between runs.

    Args:
        name: Relative filename to create.
        body: File contents.

    Returns:
        The relative :class:`~pathlib.Path` to the created file.
    """
    path = Path(name)
    path.write_text(body, encoding="utf-8")
    os.utime(path, (PINNED_MTIME, PINNED_MTIME))
    return path


def _ids_across_zones(
    ingestor_cls: type[MarkdownIngestor] | type[DocumentIngestor],
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[set[str], list[datetime]]:
    """Ingest *source* once per zone in :data:`ZONES` and collect the results.

    Args:
        ingestor_cls: The concrete ingestor to drive.
        source: Relative path to the source file.
        monkeypatch: Pytest fixture used to set ``TZ``.

    Returns:
        A ``(set of fragment ids, list of created datetimes)`` pair. A set with
        more than one member *is* the bug.
    """
    ids: set[str] = set()
    createds: list[datetime] = []
    for tz_name in ZONES:
        monkeypatch.setenv("TZ", tz_name)
        time.tzset()
        parsed = ingestor_cls().ingest(source).fragments[0]
        fragment = assemble_ingested_fragment(parsed).fragment
        ids.add(fragment.id)
        createds.append(fragment.created)
    return ids, createds


@pytest.mark.usefixtures("restore_host_tz")
def test_markdown_fragment_id_is_identical_across_host_timezones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One markdown file mints one id, whatever ``TZ`` the host is set to.

    Pre-fix this set had four members (frag-c8bfc3e67c4d, frag-aa53901eaffa,
    frag-fe2989a00f9b, frag-5ebe48b2ce65) — one per zone.
    """
    monkeypatch.chdir(tmp_path)
    source = _write_source("note.md")

    ids, createds = _ids_across_zones(MarkdownIngestor, source, monkeypatch)

    assert ids == {MARKDOWN_ID}
    assert [c.astimezone(UTC) for c in createds] == [EXPECTED_INSTANT] * len(ZONES)


@pytest.mark.usefixtures("restore_host_tz")
def test_document_fragment_id_is_identical_across_host_timezones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One document mints one id, whatever ``TZ`` the host is set to.

    ``DocumentIngestor`` has the same naive fallback as ``MarkdownIngestor``
    and is *strictly worse off*: it is unledgered by default, so a shifted id
    is an unconditional ``created`` rather than a merely possible duplicate.

    Pre-fix this set had four members (frag-dd49a01f5539, frag-7286deeb5d27,
    frag-430e7de61929, frag-936750c2ef1f).
    """
    monkeypatch.chdir(tmp_path)
    source = _write_source("note.txt")

    ids, createds = _ids_across_zones(DocumentIngestor, source, monkeypatch)

    assert ids == {DOCUMENT_ID}
    assert [c.astimezone(UTC) for c in createds] == [EXPECTED_INSTANT] * len(ZONES)


class _FakeStat:
    """A minimal ``os.stat_result`` stand-in exposing chosen fields only.

    Real ``os.stat_result`` instances cannot be constructed with
    ``st_birthtime`` selectively absent, and a filesystem-based test is no
    substitute: on APFS, backdating a file's mtime with ``os.utime`` also
    clamps its ``st_birthtime``, so the two agree by accident and the test
    proves nothing.
    """

    def __init__(self, *, st_mtime: float, st_birthtime: float | None = None) -> None:
        """Store the stat fields this fake exposes.

        Args:
            st_mtime: Modification time, always present.
            st_birthtime: Creation time; omitted entirely when ``None``, which
                is the Linux shape.
        """
        self.st_mtime = st_mtime
        self.st_size = 9
        self.st_mode = 0o100644
        if st_birthtime is not None:
            self.st_birthtime = st_birthtime


def _id_with_stat(
    fake: _FakeStat,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Ingest *source* with ``Path.stat`` faked for that one path.

    The ingest chain stats more than once (discovery, read), and it stats
    other paths too, so the patch delegates to the real ``stat`` for
    everything except the target file.

    Args:
        fake: The stat stand-in to return for *source*.
        source: Relative path whose ``stat()`` is faked.
        monkeypatch: Pytest fixture used to patch ``Path.stat``.

    Returns:
        The resulting fragment id.
    """
    real_stat = Path.stat
    target = str(source)

    def _patched(self: Path, **kwargs: Any) -> Any:
        if str(self) == target:
            return fake
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", _patched)
    parsed = MarkdownIngestor().ingest(source).fragments[0]
    return assemble_ingested_fragment(parsed).fragment.id


def test_markdown_id_ignores_st_birthtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A macOS host and a Linux host mint the SAME id for the same file.

    ``st_birthtime`` exists on macOS/BSD and not on Linux. Reading it through
    ``getattr(stat, "st_birthtime", stat.st_mtime)`` therefore made a
    fragment's identity a function of the ingesting machine's operating
    system, independently of ``TZ`` — an axis the issue never mentioned. The
    derivation must read ``st_mtime`` unconditionally.
    """
    monkeypatch.chdir(tmp_path)
    source = _write_source("note.md")

    linux_shape = _FakeStat(st_mtime=PINNED_MTIME)
    macos_shape = _FakeStat(st_mtime=PINNED_MTIME, st_birthtime=PINNED_MTIME - 86400)

    linux_id = _id_with_stat(linux_shape, source, monkeypatch)
    macos_id = _id_with_stat(macos_shape, source, monkeypatch)

    assert linux_id == macos_id == MARKDOWN_ID
