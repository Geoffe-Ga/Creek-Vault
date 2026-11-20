"""The dummy corpus the ontology-population lane uploads (#2125, epic #2120).

Each document is aimed at a writer, and the comment beside it says which: a
folder that stays empty after this corpus has run is a writer that did not
fire, not a corpus that never asked it to. The text lives in
``tests/fixtures/ontology_corpus/``; this module renders it and builds the
binary formats in process, so no opaque blob is checked in.

**Dates are relative to the run, on purpose.** A thread's status is computed
against the wall clock (``ThreadDetector._compute_status``: active within 30
days, dormant within 180, resolved after), so a corpus pinned to absolute dates
would drift from ``Active`` to ``Dormant`` as the calendar moved and the lane
would rot. Each templated note is dated a fixed number of days before the run,
with every offset far from a status boundary.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Final

from tests.archive_export_support import chatgpt_archive, discord_archive

if TYPE_CHECKING:
    from collections.abc import Iterator

CORPUS_DIR: Final[Path] = Path(__file__).parents[1] / "fixtures" / "ontology_corpus"
"""Where the corpus's text fixtures live."""

TOPIC_KEYWORD: Final[str] = "strategy"
"""The word every topic note carries in its H1, so all of them embed alike.

``fragment_embedding_text`` embeds the title only, and the lane's encoder maps
any title holding this word to one vector. It is also an ``F5`` keyword in
``creek.classify.rules``, so the rules classifier gives every topic note the
same primary frequency — the topic consistency ``ThreadDetector`` needs.
"""

NOTES_PER_GROUP: Final[int] = 6
"""Topic notes per dated group: above ``eddy_min_fragments`` (5), so a cluster."""

THREAD_GROUP_AGES: Final[dict[str, int]] = {
    "active": 2,
    "dormant": 60,
    "resolved": 400,
}
"""Days before the run each group's first note is dated, one group per status.

Within the 30-day thread window a group links into one thread; across groups
the gap is far wider, so the three never merge. ``2`` is inside ``Active``'s
30 days, ``60`` inside ``Dormant``'s 180, ``400`` beyond it — each at least
two weeks from a boundary even after the group's six daily notes.
"""

_ESSAY_AGE: Final[int] = 700
"""Days before the run the topic essay is dated.

The essay is ``Writing`` and the topic notes are ``Journal``: same vector,
different platform, more than 30 days apart — the synchronicity criteria.
"""

_RECENT_AGE: Final[int] = 5
"""Days before the run the undated-in-spirit notes are dated."""

_COMPOST_AGE: Final[int] = 200
"""Days before the run the abandoned project is dated."""

COMPOST_TITLE: Final[str] = "Pottery project"
"""The note the lane's compost verifier answers ``yes`` for."""

OBSERVATION_FILE: Final[Path] = CORPUS_DIR / "observation.md"
"""The body ``creek save`` files as a wavelength observation."""

JOURNAL_DIR: Final[Path] = CORPUS_DIR / "journal"
"""A markdown journal ingested as a directory, so a removed page is tombed."""

REMOVED_JOURNAL_PAGE: Final[str] = "removed-page.md"
"""The journal page deleted between the two directory ingests."""


@dataclass(frozen=True)
class Upload:
    """One document the lane sends through ``POST /v1/uploads``.

    Attributes:
        filename: The name sent; only its extension and shape are trusted.
        content: The document's bytes.
    """

    filename: str
    content: bytes


def _dated(name: str, authored: date, **extra: str) -> bytes:
    """Render a templated fixture with its ``$authored`` date filled in.

    Args:
        name: The fixture's file name under :data:`CORPUS_DIR`.
        authored: The date the note claims.
        **extra: Any further ``$placeholders`` the template names.

    Returns:
        The rendered bytes.
    """
    template = Template((CORPUS_DIR / name).read_text(encoding="utf-8"))
    return template.substitute(authored=authored.isoformat(), **extra).encode()


def _raw(name: str) -> bytes:
    """Return a fixture's bytes unchanged."""
    return (CORPUS_DIR / name).read_bytes()


def _topic_notes(today: date) -> Iterator[Upload]:
    """Yield the three dated groups of topic notes (threads, eddies, voice)."""
    for status, first_age in THREAD_GROUP_AGES.items():
        for index in range(NOTES_PER_GROUP):
            authored = today - timedelta(days=first_age + index)
            label = f"{status}-{index}"
            yield Upload(
                f"strategy-{label}.md",
                _dated("strategy-session.md", authored, label=label),
            )


def _docx() -> bytes:
    """Return a one-paragraph Word document (``01-Fragments/Documents``)."""
    import docx

    document = docx.Document()
    document.add_heading("Quarterly garden report", 1)
    document.add_paragraph("The garden plots need water and compost this month.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pptx() -> bytes:
    """Return a one-slide deck (``01-Fragments/Decks``)."""
    import pptx

    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Garden deck"
    slide.placeholders[1].text = "Beds, paths and the shed."
    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


def _png() -> bytes:
    """Return a tiny blank image (``01-Fragments/Images``, via the lane's OCR)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def uploads(today: date) -> list[Upload]:
    """Return every document the lane uploads, in upload order.

    Args:
        today: The run's date, which every templated note is dated against.

    Returns:
        The uploads. The Substack manifest comes before its post, as an
        unzipped export's folder walk sends it.
    """
    recent = today - timedelta(days=_RECENT_AGE)
    return [
        *_topic_notes(today),
        # Writing, and the far end of every synchronicity pair.
        Upload(
            "strategy-essay.md",
            _dated("strategy-essay.md", today - timedelta(days=_ESSAY_AGE)),
        ),
        Upload("shopping-list.md", _dated("shopping-list.md", recent)),  # Notes
        Upload("config-notes.md", _dated("config-notes.md", recent)),  # Technical
        # 04-Praxis: one explicit practice per cadence (habit, commitment,
        # anything else), each scoring the praxis pass's explicit threshold.
        Upload("morning-sit.md", _dated("morning-sit.md", recent)),
        Upload("manuscript.md", _dated("manuscript.md", recent)),
        Upload("reading.md", _dated("reading.md", recent)),
        # 08-Decisions/Active.
        Upload("portland-decision.md", _dated("portland-decision.md", recent)),
        # 10-Liminal/Paradoxes: a contradiction phrase on the topic.
        Upload("changed-mind.md", _dated("changed-mind.md", recent)),
        # 10-Liminal/Compost: an abandoned project.
        Upload(
            "pottery-project.md",
            _dated("pottery-project.md", today - timedelta(days=_COMPOST_AGE)),
        ),
        # 11-Other-Authors: a borrowed note declaring its author.
        Upload("of-experience.md", _dated("of-experience.md", recent)),
        Upload("server.log", _raw("server.log")),  # Unsorted (generic)
        Upload("plants.csv", _raw("plants.csv")),  # Data
        Upload("garden-report.docx", _docx()),  # Documents
        Upload("garden-deck.pptx", _pptx()),  # Decks
        Upload("whiteboard.png", _png()),  # Images
        Upload("chatgpt-export.zip", chatgpt_archive()),  # Conversations
        Upload("discord-export.zip", discord_archive()),  # Messages
        # Writing/Substack: a Substack export uploaded as loose files (#2123).
        Upload("posts.csv", _raw("posts.csv")),
        Upload("171234567.on-attention.html", _raw("171234567.on-attention.html")),
    ]


def write_journal(destination: Path, today: date) -> None:
    """Render the markdown journal into *destination* for a directory ingest.

    Args:
        destination: An empty directory.
        today: The run's date.
    """
    authored = today - timedelta(days=_RECENT_AGE)
    for page in sorted(JOURNAL_DIR.glob("*.md")):
        (destination / page.name).write_bytes(_dated(f"journal/{page.name}", authored))
