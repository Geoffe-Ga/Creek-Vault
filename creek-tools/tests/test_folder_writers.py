"""The ontology folder registry: each scaffold folder, its writer or why none."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from creek.classify.praxis_writer import write_praxis_pages
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PraxisPotential,
    PrivacyTier,
    SourcePlatform,
)
from creek.scaffold import VAULT_TEMPLATE_DIR
from creek.vault.folder_writers import (
    ONTOLOGY_FOLDERS,
    PRAXIS_WRITER,
    WRITER_PATTERN,
    WriterlessReason,
)
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

_META_ROOT = "00-Creek-Meta"

_PRAXIS_BODIES = {
    "frag-habit": "A daily walk after lunch.",
    "frag-commitment": "I commit to finishing the manuscript this year.",
    "frag-insight": "I should read more slowly.",
}
"""One explicit body per cadence the praxis post-pass maps to (#2122)."""


def test_the_registry_covers_exactly_the_scaffolded_ontology_folders() -> None:
    """Every folder ``creek init`` makes, except the meta tree, and nothing else."""
    scaffolded = {
        path.relative_to(VAULT_TEMPLATE_DIR).as_posix()
        for path in VAULT_TEMPLATE_DIR.rglob("*")
        if path.is_dir()
    }
    ontology = {path for path in scaffolded if not path.startswith(_META_ROOT)}

    assert {folder.path for folder in ONTOLOGY_FOLDERS} == ontology


def test_the_registry_is_sorted_and_unique() -> None:
    """Document order is path order, so a status response is stable."""
    paths = [folder.path for folder in ONTOLOGY_FOLDERS]

    assert paths == sorted(set(paths))


def test_every_folder_names_a_writer_or_a_reason_never_both() -> None:
    """A folder either fills from a named writer or says why it cannot."""
    for folder in ONTOLOGY_FOLDERS:
        assert (folder.writer is None) != (folder.reason is None), folder.path
        if folder.writer is not None:
            assert re.fullmatch(WRITER_PATTERN, folder.writer), folder.writer


def test_the_epics_writerless_folders_are_flagged() -> None:
    """Reference material and decision frameworks say why nothing fills them."""
    reasons = {folder.path: folder.reason for folder in ONTOLOGY_FOLDERS}

    assert reasons["09-Reference/External-Sources"] is WriterlessReason.OWNER_CURATED
    assert reasons["08-Decisions/Frameworks"] is WriterlessReason.OWNER_CURATED
    assert reasons["01-Fragments"] is WriterlessReason.CONTAINER


def test_the_writers_the_population_lane_corrected_are_pinned() -> None:
    """``Unnamed`` is filled by ``save``; other authors' notes by ingest (#2125).

    ``generate.unnamed`` only reads ``10-Liminal/Unnamed`` (it writes the
    ``Digests`` beneath it), and no ``authors`` subsystem exists: a note lands
    in ``11-Other-Authors/<slug>`` when ingest reads its ``author_slug``.
    """
    writers = {folder.path: folder.writer for folder in ONTOLOGY_FOLDERS}

    assert writers["10-Liminal/Unnamed"] == "save"
    assert writers["10-Liminal/Unnamed/Digests"] == "generate.unnamed"
    assert writers["11-Other-Authors"] == "ingest"


def test_the_fragment_folders_fill_from_ingest() -> None:
    """Every ``01-Fragments`` subfolder is an ingest destination."""
    for folder in ONTOLOGY_FOLDERS:
        if folder.path.startswith("01-Fragments/"):
            assert folder.writer == "ingest", folder.path


def test_the_writerless_reason_vocabulary_is_pinned() -> None:
    """The closed reason vocabulary is part of the published contract."""
    assert [reason.value for reason in WriterlessReason] == [
        "container",
        "owner_curated",
        "indexed_at_parent",
        "template_example",
    ]


def test_every_folder_the_praxis_writer_fills_is_registered_to_it(
    tmp_path: Path,
) -> None:
    """Run the real post-pass: each page's folder names it as its writer.

    One explicit fragment per cadence, so a new ``praxis_type`` routed to an
    unregistered (or writerless-flagged) folder fails here rather than showing
    up on the status route as an empty folder that is in fact filling.
    """
    (tmp_path / _META_ROOT).mkdir()
    for fragment_id, body in _PRAXIS_BODIES.items():
        fragment = Fragment(
            id=fragment_id,
            title="Notes",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN, author=Authorship.SELF
            ),
            praxis_potential=PraxisPotential.EXPLICIT,
            privacy_tier=PrivacyTier.PERSONAL,
        )
        write_fragment_file(vault=tmp_path, fragment=fragment, body=body)

    summary = write_praxis_pages(tmp_path)
    filled = {
        page.parent.relative_to(tmp_path).as_posix()
        for page in (tmp_path / "04-Praxis").rglob("*.md")
    }
    writers = {folder.path: folder.writer for folder in ONTOLOGY_FOLDERS}

    assert summary.written == len(_PRAXIS_BODIES)
    assert filled == {
        "04-Praxis/Daily",
        "04-Praxis/Seasonal",
        "04-Praxis/Situational",
    }
    assert {writers.get(path) for path in filled} == {PRAXIS_WRITER}
