"""Which stage fills each ontology folder, or why none does (#2124, epic #2120).

Every folder ``creek init`` scaffolds outside ``00-Creek-Meta`` appears here
exactly once (``tests/test_folder_writers.py`` pins the set to the template
tree), with either the name of the subsystem that writes into it or a closed
:class:`WriterlessReason` saying why nothing does. ``GET /v1/pipeline/status``
serves this beside each folder's count, so an empty folder that is filling and
an empty folder that never will are told apart on the wire rather than by a
person reading the code.

**Writer names are subsystem names, not promises about a surface.** Several of
them (``generate.*``, ``save``) are reached from the ``creek`` CLI or MCP rather
than from a ``/v1`` route, so on a managed vault such a folder can stay empty
until that subsystem is run. The name says which one to run.

**Provisional until proven.** #2125
proves each entry against a live server and a dummy corpus. The three praxis
cadences are written by the classify post-pass
(:func:`creek.classify.praxis_writer.write_praxis_pages`, #2122), which files
each page by its ``praxis_type`` into ``Daily``, ``Seasonal`` or
``Situational``; ``tests/test_folder_writers.py`` runs it and fails if a page
lands in a folder not registered to :data:`PRAXIS_WRITER`.

How a note is attributed to a folder: each ``.md`` file counts toward the
deepest registered folder that contains it, so a nested registered folder is
counted on its own and never again in its parent, while an unregistered
subfolder (``11-Other-Authors/<author>``, a new platform under
``01-Fragments/Writing``) counts toward its nearest registered ancestor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

WRITER_PATTERN: Final[str] = r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*$"
"""What a writer name may be: a dotted lower-case subsystem name."""


class WriterlessReason(StrEnum):
    """Why a scaffolded folder has no writer.

    Attributes:
        CONTAINER: A grouping folder; notes are filed into its subfolders.
        OWNER_CURATED: Material the owner places by hand; no stage writes it.
        INDEXED_AT_PARENT: The stage that covers this topic files one index in
            the parent folder instead of notes here.
        TEMPLATE_EXAMPLE: A scaffold example showing a layout, never filled.
    """

    CONTAINER = "container"
    OWNER_CURATED = "owner_curated"
    INDEXED_AT_PARENT = "indexed_at_parent"
    TEMPLATE_EXAMPLE = "template_example"


@dataclass(frozen=True)
class OntologyFolder:
    """One scaffolded folder and what fills it.

    Attributes:
        path: Vault-relative POSIX path.
        writer: The subsystem that writes into it, or ``None``.
        reason: Why nothing writes into it, or ``None`` when ``writer`` is set.
    """

    path: str
    writer: str | None = None
    reason: WriterlessReason | None = None


PRAXIS_WRITER: Final[str] = "classify.praxis"
"""The classify post-pass that files ``04-Praxis`` pages (#2122)."""

_INGEST: Final[str] = "ingest"
_THREADS: Final[str] = "link.threads"
_SAVE: Final[str] = "save"
_WAVELENGTH: Final[str] = "generate.wavelength"
_VOICE: Final[str] = "generate.voice"
_LEXICON: Final[str] = "generate.lexicon"
_DECISIONS: Final[str] = "generate.decisions"
_COMPOST: Final[str] = "generate.compost"
_UNNAMED: Final[str] = "generate.unnamed"

_FREQUENCY_FOLDERS: Final[tuple[str, ...]] = (
    "F1-Agency",
    "F10-Emptiness",
    "F2-Receptivity",
    "F3-Self-Love-Power",
    "F4-Community-Love",
    "F5-Achievism",
    "F6-Pluralism",
    "F7-Integration",
    "F8-True-Self",
    "F9-Unity",
)
"""The per-frequency subfolders of ``06-Frequencies``, in path order."""

_FRAGMENT_FOLDERS: Final[tuple[str, ...]] = (
    "Conversations",
    "Data",
    "Decks",
    "Documents",
    "Images",
    "Journal",
    "Messages",
    "Notes",
    "Technical",
    "Unsorted",
    "Writing",
    "Writing/Substack",
)
"""The ``01-Fragments`` routing destinations, in path order."""


def _container(path: str) -> OntologyFolder:
    """Return a grouping folder with no writer of its own."""
    return OntologyFolder(path=path, reason=WriterlessReason.CONTAINER)


def _written(path: str, writer: str) -> OntologyFolder:
    """Return a folder *writer* fills."""
    return OntologyFolder(path=path, writer=writer)


def _writerless(path: str, reason: WriterlessReason) -> OntologyFolder:
    """Return a folder nothing fills, with why."""
    return OntologyFolder(path=path, reason=reason)


ONTOLOGY_FOLDERS: Final[tuple[OntologyFolder, ...]] = (
    _container("01-Fragments"),
    *(_written(f"01-Fragments/{name}", _INGEST) for name in _FRAGMENT_FOLDERS),
    _container("02-Threads"),
    _written("02-Threads/Active", _THREADS),
    _written("02-Threads/Dormant", _THREADS),
    _written("02-Threads/Resolved", _THREADS),
    _written("03-Eddies", "link.eddies"),
    _container("04-Praxis"),
    _written("04-Praxis/Daily", PRAXIS_WRITER),
    _written("04-Praxis/Seasonal", PRAXIS_WRITER),
    _written("04-Praxis/Situational", PRAXIS_WRITER),
    _container("05-Wavelength"),
    _written("05-Wavelength/Mode-Profiles", _WAVELENGTH),
    _written("05-Wavelength/Observations", _SAVE),
    _written("05-Wavelength/Phase-Maps", _WAVELENGTH),
    _written("06-Frequencies", "compile"),
    *(
        _writerless(f"06-Frequencies/{name}", WriterlessReason.INDEXED_AT_PARENT)
        for name in _FREQUENCY_FOLDERS
    ),
    _written("07-Voice", _VOICE),
    _written("07-Voice/Drafts", "draft"),
    _written("07-Voice/Lexicon", _LEXICON),
    _written("07-Voice/Lexicon/Metaphors", _LEXICON),
    _written("07-Voice/Register-Samples", _VOICE),
    _written("07-Voice/Rhetorical-Patterns", _VOICE),
    _container("08-Decisions"),
    _written("08-Decisions/Active", _DECISIONS),
    _written("08-Decisions/Archive", _DECISIONS),
    _writerless("08-Decisions/Frameworks", WriterlessReason.OWNER_CURATED),
    _container("09-Reference"),
    _writerless("09-Reference/APTITUDE-Course", WriterlessReason.OWNER_CURATED),
    _writerless("09-Reference/External-Sources", WriterlessReason.OWNER_CURATED),
    _writerless("09-Reference/Published-Essays", WriterlessReason.OWNER_CURATED),
    _container("10-Liminal"),
    _written("10-Liminal/Compost", _COMPOST),
    _written("10-Liminal/Compost/Review", _COMPOST),
    _written("10-Liminal/Compost/intimate-stubs", _SAVE),
    _written("10-Liminal/Orphaned", _INGEST),
    _written("10-Liminal/Paradoxes", "generate.paradox"),
    _written("10-Liminal/Synchronicities", "generate.synchronicity"),
    _written("10-Liminal/Unnamed", _UNNAMED),
    _written("10-Liminal/Unnamed/Digests", _UNNAMED),
    _written("11-Other-Authors", "authors"),
    _written("11-Other-Authors/ai-as-user", "voice-drafts"),
    _writerless("11-Other-Authors/example-author", WriterlessReason.TEMPLATE_EXAMPLE),
)
"""Every scaffolded ontology folder, in path order."""
