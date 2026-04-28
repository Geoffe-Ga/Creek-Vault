"""Presentation ingestor for the Creek pipeline.

Implements the PPTX half of §57 of the Creek Ontology — ingest
PowerPoint presentations as one fragment per file, with each slide
rendered as a labelled markdown section. Speaker notes are inlined
under their slide so reviewers can see what was said alongside the
visual content.

The presentation backend is decoupled through the
:class:`PresentationBackend` Protocol so callers can plug in any
implementation; tests inject a deterministic stub. The default
:class:`PythonPptxBackend` lazily imports ``python-pptx`` so the
rest of the package — and the unit tests — run cleanly even when
that optional dependency is not installed.

Optional dependency (install separately to enable PPTX support):

* ``python-pptx`` — pure-Python PPTX reader.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    file_modified_time,
)
from creek.models import SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


PRESENTATION_EXTENSIONS: frozenset[str] = frozenset({".pptx"})
"""File extensions handled by :class:`PresentationIngestor`."""


# ---- Public dataclasses + protocol --------------------------------------


@dataclass(frozen=True)
class SlideData:
    """A single slide's content extracted from a presentation.

    Attributes:
        index: 1-based slide number.
        title: Slide title text. ``None`` if no title placeholder.
        body: Concatenated body / placeholder text, newline-separated.
        notes: Speaker notes. Empty string when the slide has none.
    """

    index: int
    title: str | None
    body: str
    notes: str = ""


@dataclass(frozen=True)
class PresentationData:
    """A presentation decomposed into ordered slides.

    Attributes:
        title: Optional presentation title (from core properties).
        slides: Slides in presentation order.
    """

    title: str | None
    slides: tuple[SlideData, ...]


@runtime_checkable
class PresentationBackend(Protocol):
    """Pluggable presentation backend.

    Implementations must be deterministic and side-effect-free; the
    same input file should always produce the same
    :class:`PresentationData`.
    """

    def is_available(self) -> bool:
        """Return ``True`` when the backend can perform reads right now."""

    def read_presentation(self, path: Path) -> PresentationData:
        """Read *path* and return its decomposed :class:`PresentationData`."""


class PythonPptxUnavailableError(RuntimeError):
    """Raised when a PPTX read is attempted without ``python-pptx`` installed."""


# ---- Default backend ---------------------------------------------------


class PythonPptxBackend:
    """Presentation backend backed by ``python-pptx``.

    Imports of the optional dependency are deferred to call time so
    the rest of the package runs cleanly without it.
    """

    def is_available(self) -> bool:
        """Return ``True`` when ``python-pptx`` imports cleanly."""
        try:
            import pptx  # noqa: F401
        except ImportError:
            return False
        return True

    def read_presentation(self, path: Path) -> PresentationData:
        """Read *path* and return a :class:`PresentationData`.

        Args:
            path: Filesystem path to a ``.pptx`` file.

        Returns:
            The decomposed :class:`PresentationData`.

        Raises:
            PythonPptxUnavailableError: When ``python-pptx`` is not
                installed.
        """
        try:
            from pptx import Presentation
        except ImportError as exc:
            msg = (
                "python-pptx is required for PPTX ingestion. Install it with "
                "`pip install python-pptx`."
            )
            raise PythonPptxUnavailableError(msg) from exc

        prs = Presentation(str(path))
        slides: list[SlideData] = []
        for index, slide in enumerate(prs.slides, start=1):
            slides.append(_slide_to_data(slide, index))
        title = self._extract_title(prs)
        return PresentationData(title=title, slides=tuple(slides))

    @staticmethod
    def _extract_title(prs: Any) -> str | None:
        """Return the presentation's core-properties title, if set.

        ``python-pptx`` exposes ``core_properties.title`` as either a
        non-empty string, an empty string, or ``None``. We coerce all
        absent-or-blank cases to ``None`` so callers can safely fall
        back to the file stem with ``data.title or path.stem`` —
        without the literal string ``"None"`` ever surfacing in
        rendered markdown or YAML frontmatter.
        """
        try:
            title = prs.core_properties.title
        except (AttributeError, ValueError):
            return None
        if title is None:
            return None
        stripped = str(title).strip()
        return stripped or None


def _slide_to_data(slide: Any, index: int) -> SlideData:
    """Convert a python-pptx slide object into a :class:`SlideData`."""
    title: str | None = None
    title_shape = slide.shapes.title
    body_chunks: list[str] = []
    for shape in slide.shapes:
        if not getattr(shape, "has_text_frame", False):
            continue
        text = shape.text_frame.text.strip()
        if not text:
            continue
        if title_shape is not None and shape is title_shape and title is None:
            title = text
        else:
            body_chunks.append(text)
    notes = ""
    if getattr(slide, "has_notes_slide", False):
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        except AttributeError:
            notes = ""
    return SlideData(
        index=index,
        title=title,
        body="\n".join(body_chunks),
        notes=notes,
    )


# ---- PresentationIngestor ----------------------------------------------


class PresentationIngestor(Ingestor):
    """Ingest PPTX files as one fragment per presentation."""

    def __init__(self, backend: PresentationBackend | None = None) -> None:
        """Initialise with a backend; defaults to :class:`PythonPptxBackend`."""
        self.backend = backend if backend is not None else PythonPptxBackend()

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Recursively find ``.pptx`` files under *source_path*.

        Single-file paths whose suffix is unsupported return an empty
        list. File bytes are not slurped at discovery time — the
        backend reads from disk via the :class:`Path`.
        """
        if source_path.is_file():
            paths = (
                [source_path]
                if source_path.suffix.lower() in PRESENTATION_EXTENSIONS
                else []
            )
        else:
            paths = [
                p
                for p in sorted(source_path.rglob("*"))
                if p.is_file() and p.suffix.lower() in PRESENTATION_EXTENSIONS
            ]
        return [
            RawDocument(
                path=path,
                content=b"",
                metadata={"original_file": str(path)},
                detected_encoding="binary",
            )
            for path in paths
        ]

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Emit a single fragment carrying the entire presentation."""
        data = self.backend.read_presentation(raw.path)
        if not data.slides:
            logger.info(
                "Presentation %s has no slides; skipping fragment.",
                raw.path,
            )
            return []
        timestamp = file_modified_time(raw.path)
        return [
            ParsedFragment(
                content="",  # Markdown rendered in convert_to_markdown.
                metadata={
                    "original_file": str(raw.path),
                    "title": data.title or raw.path.stem,
                    "slide_count": len(data.slides),
                    "slides": [
                        {
                            "index": slide.index,
                            "title": slide.title or "",
                            "body": slide.body,
                            "notes": slide.notes,
                        }
                        for slide in data.slides
                    ],
                },
                source_path=str(raw.path),
                timestamp=timestamp,
            ),
        ]

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Render the presentation as a markdown document.

        One ``## Slide N: Title`` block per slide with the body inlined
        and speaker notes (when present) under a ``**Speaker notes:**``
        sub-section.
        """
        title = str(fragment.metadata.get("title", "Presentation"))
        slides: list[dict[str, Any]] = list(fragment.metadata.get("slides", []))
        lines: list[str] = [f"# {title}", ""]
        for slide in slides:
            heading = f"## Slide {slide['index']}"
            if slide.get("title"):
                heading = f"{heading}: {slide['title']}"
            lines.append(heading)
            lines.append("")
            body = str(slide.get("body", ""))
            if body:
                lines.append(body)
                lines.append("")
            notes = str(slide.get("notes", ""))
            if notes:
                lines.append("**Speaker notes:**")
                lines.append("")
                lines.append(notes)
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Produce YAML frontmatter for a presentation fragment."""
        return {
            "type": "fragment",
            "source": {
                "platform": SourcePlatform.PRESENTATION.value,
                "original_file": fragment.metadata.get(
                    "original_file",
                    fragment.source_path,
                ),
            },
            "title": fragment.metadata.get("title", ""),
            "slide_count": fragment.metadata.get("slide_count", 0),
            "ingested": fragment.timestamp.isoformat(),
        }


__all__ = [
    "PRESENTATION_EXTENSIONS",
    "PresentationBackend",
    "PresentationData",
    "PresentationIngestor",
    "PythonPptxBackend",
    "PythonPptxUnavailableError",
    "SlideData",
]
