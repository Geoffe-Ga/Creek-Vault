"""Every ontology folder fills from a dummy corpus, or says why it has no writer.

Epic #2120's promise, proven end to end (#2125): an empty folder in a vault
means "nothing matched yet", never "a stage quietly did nothing". The lane
boots the real ``/v1`` app over a vault ``creek init`` scaffolded, uploads the
corpus in :mod:`tests.e2e.ontology_corpus` through ``POST /v1/uploads``, runs
classification and every linker stage over ``/v1``, then runs each remaining
writer subsystem through the surface that reaches it (``/v1/voice-drafts``, the
``creek`` CLI). It then fails unless:

* every writer named in :data:`creek.vault.folder_writers.ONTOLOGY_FOLDERS` has
  a stage here — a new writer cannot be registered without being proven;
* every folder registered to a writer gained a note **during that writer's own
  stage** — not merely by the end, so a folder another subsystem happens to
  fill cannot hide a registered writer that never writes it;
* ``GET /v1/pipeline/status`` agrees: each written folder non-empty, each
  writerless folder carrying its registered reason, every count equal to an
  independent walk of the disk; and
* every uploaded document's stage trail shows every pass ran ``ok`` — a link
  pass that detected a page and failed to write it records ``partial`` (#2125),
  so a skipped materialisation is a failure here, not a log line.

**Hermetic and deterministic.** No network, no key, no model download. What
cannot run on a bare host is replaced at its narrowest seam, and the real
writer behind it still runs:

* the sentence-transformer, by an encoder that maps a title to a fixed vector
  per topic (the technique of ``test_full_pipeline_linking.py``);
* the ``tesseract`` binary, by an OCR engine that returns fixed text, so the
  real :class:`~creek.ingest.images.ImageIngestor` files the image;
* the three LLM hops — the compile synthesis, the compost verifier's provider
  and the draft model — by fixed completions, so prompt building, parsing and
  the page writes are production code.

The one asynchronous step, the ``embeddings`` link job, is not polled: the
next synchronous link pass is ordered behind the caller's own job (#2132), so
by the time it returns the job is terminal, and the lane asserts so.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

import creek.cli as cli_module
import creek.ingest.images as images_module
from creek.cli import app as cli_app
from creek.ingest.images import OcrResult
from creek.link import embeddings as embeddings_module
from creek.vault.folder_writers import ONTOLOGY_FOLDERS
from creek_mcp.api.models import PipelineStage
from tests.e2e import ontology_corpus as corpus
from tests.v1_api_support import (
    CLASSIFICATIONS_PATH,
    LINKS_PATH,
    PIPELINE_STATUS_PATH,
    UPLOAD_PATH,
    build_app,
    headers,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

pytestmark = [pytest.mark.e2e]

_CEILING: Final[str] = "personal"
"""The ceiling every ``/v1`` call declares, and the tier every upload files at."""

_HTTP_OK: Final[int] = 200
_HTTP_ACCEPTED: Final[int] = 202

_SKIPPED_PREFIXES: Final[tuple[str, ...]] = (".", "_")
"""Entries the status route does not count as notes (indexes, scaffold docs)."""

_TOPIC_VECTOR: Final[list[float]] = [1.0, 0.0, 0.0]
"""The one vector every topic title embeds to: distance 0 inside the topic."""

_OTHER_VECTOR: Final[list[float]] = [0.0, 0.0, 1.0]
"""Every other title's vector: distance 1 from the topic, so nothing merges."""

_OCR_TEXT: Final[str] = "Whiteboard: garden beds, paths and the shed."
"""What the lane's OCR engine reads off any image."""

_COMPILE_TARGET: Final[str] = "frequency-f5"
"""The ``06-Frequencies`` index page the compile stage writes."""

_KNOWN_UNFILLED: Final[dict[str, str]] = {
    "08-Decisions/Archive": (
        "generate.decisions files new candidates into Active only; the move to "
        "Archive is DecisionDetector.update_decision_phase, which no CLI "
        "command, MCP tool or /v1 route calls (#2137)."
    ),
}
"""Folders whose registered writer exists but cannot be reached from any surface.

Each entry is a real defect, recorded rather than hidden: the lane still checks
the folder, and fails the moment it fills, so the entry cannot outlive the fix.
"""


class _TopicEncoder:
    """A sentence-transformer stand-in: one fixed vector per fixture topic."""

    def encode(self, sentences: str | list[str], **_kwargs: object) -> np.ndarray:
        """Return the topic vector for each title.

        Args:
            sentences: One title or a batch; titles are all the linker embeds.
            **_kwargs: Batch size and progress flags, unread.

        Returns:
            A float32 array, one row per title.
        """
        if isinstance(sentences, str):
            return np.array(_vector(sentences), dtype=np.float32)
        return np.array([_vector(text) for text in sentences], dtype=np.float32)


def _vector(text: str) -> list[float]:
    """Return the fixed vector for *text*'s topic."""
    return _TOPIC_VECTOR if corpus.TOPIC_KEYWORD in text.lower() else _OTHER_VECTOR


class _FixedOcrEngine:
    """An OCR backend that reads the same sentence off every image."""

    def __init__(self, language: str = "eng") -> None:
        """Accept the language the production engine is built with.

        Args:
            language: Unused; present for constructor parity.
        """
        self.language = language

    def is_available(self) -> bool:
        """Report the backend usable."""
        return True

    def extract_text(self, image_path: Path) -> OcrResult:
        """Return the fixed reading for *image_path*."""
        del image_path
        return OcrResult(text=_OCR_TEXT, confidence=0.95)

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Return no pages: the corpus holds no scanned PDF."""
        del pdf_path
        return []


@dataclass(frozen=True)
class _Completion:
    """A provider response: the only attribute the compost verifier reads."""

    text: str


class _VerdictProvider:
    """A compost-verifier provider answering ``yes`` for the abandoned project."""

    available: bool = True

    def complete(self, prompt: str, **_kwargs: object) -> _Completion:
        """Return a verdict in the verifier's three-line format.

        Args:
            prompt: The verifier's prompt, which quotes the fragment's title.
            **_kwargs: Provider options, unread.

        Returns:
            ``yes`` for :data:`corpus.COMPOST_TITLE`, ``ambiguous`` otherwise —
            so both the canonical folder and the review queue receive a note.
        """
        verdict = "yes" if corpus.COMPOST_TITLE in prompt else "ambiguous"
        return _Completion(f"VERDICT: {verdict}\nREASONING: fixture verdict.")


@dataclass
class _Run:
    """Everything one lane run shares between its stages.

    Attributes:
        api: The client over the ``/v1`` app.
        vault: The vault root.
        workdir: Scratch space outside the vault.
        today: The run's date; the corpus is dated against it.
        monkeypatch: For the per-stage model seams.
        uploaded: Fragment ids the uploads produced, by filename.
    """

    api: TestClient
    vault: Path
    workdir: Path
    today: date
    monkeypatch: pytest.MonkeyPatch
    uploaded: dict[str, str] = field(default_factory=dict)

    def cli(self, *args: str) -> str:
        """Run one ``creek`` command against the vault and require success.

        Args:
            *args: The command and its options, without ``--vault``.

        Returns:
            The command's output.
        """
        result = CliRunner().invoke(cli_app, [*args, "--vault", str(self.vault)])
        assert result.exit_code == 0, (
            f"creek {' '.join(args)} exited {result.exit_code}\n{result.output}\n"
            f"{result.exception!r}"
        )
        return result.output

    def post(self, path: str, body: dict[str, Any], expected: int) -> dict[str, Any]:
        """POST *body* to *path* and require *expected*.

        Returns:
            The decoded response body.
        """
        response = self.api.post(path, json=body, headers=headers(ceiling=_CEILING))
        assert response.status_code == expected, f"{path}: {response.text}"
        decoded: dict[str, Any] = response.json()
        return decoded


# --------------------------------------------------------------------------- #
# Stages: one per writer subsystem the registry names, in dependency order
# --------------------------------------------------------------------------- #


def _ingest(run: _Run) -> None:
    """Upload the corpus over ``/v1``; tomb one journal page through the CLI.

    ``10-Liminal/Orphaned`` is ingest's too, but only a directory ingest of a
    markdown journal may tomb a unit it no longer sees, so the journal goes
    through ``creek ingest`` twice with a page deleted in between.
    """
    for index, upload in enumerate(corpus.uploads(run.today)):
        response = run.api.post(
            UPLOAD_PATH,
            json={
                "filename": upload.filename,
                "content_base64": base64.b64encode(upload.content).decode("ascii"),
                "external_id": f"ontology-corpus:{index}",
                "tier": _CEILING,
            },
            headers=headers(ceiling=_CEILING),
        )
        assert response.status_code in {_HTTP_OK, _HTTP_ACCEPTED}, (
            f"{upload.filename}: {response.text}"
        )
        fragment_id = response.json().get("fragment_id")
        if fragment_id is not None:
            run.uploaded[upload.filename] = fragment_id
    journal = run.workdir / "journal"
    journal.mkdir()
    corpus.write_journal(journal, run.today)
    run.cli("ingest", "--type", "markdown", "--input", str(journal), "--yes")
    (journal / corpus.REMOVED_JOURNAL_PAGE).unlink()
    run.cli("ingest", "--type", "markdown", "--input", str(journal), "--yes")


def _classify(run: _Run) -> None:
    """Classify by rules over ``/v1``; the praxis post-pass files pages."""
    body = run.post(CLASSIFICATIONS_PATH, {"method": "rules"}, _HTTP_OK)
    assert body["complete"] is True, body


def _link_eddies(run: _Run) -> None:
    """Run ``temporal``, the ``embeddings`` job, then ``eddies``, over ``/v1``.

    ``eddies`` is ordered behind the caller's own ``embeddings`` job (#2132), so
    the job is terminal when it returns; reading it once is the whole wait.
    """
    run.post(LINKS_PATH, {"method": "temporal"}, _HTTP_OK)
    job_id = run.post(LINKS_PATH, {"method": "embeddings"}, _HTTP_ACCEPTED)["job_id"]
    run.post(LINKS_PATH, {"method": "eddies"}, _HTTP_OK)
    job = run.api.get(f"/v1/jobs/{job_id}", headers=headers(ceiling=_CEILING))
    assert job.json()["state"] == "succeeded", job.text


def _link_threads(run: _Run) -> None:
    """Run the ``threads`` stage over ``/v1``."""
    run.post(LINKS_PATH, {"method": "threads"}, _HTTP_OK)


def _voice_drafts(run: _Run) -> None:
    """Keep one AI-authored draft over ``PUT /v1/voice-drafts``."""
    response = run.api.put(
        "/v1/voice-drafts/ontology-corpus-draft",
        json={"content": "A kept AI draft about the strategy.", "tier": _CEILING},
        headers=headers(ceiling=_CEILING),
    )
    assert response.status_code == _HTTP_OK, response.text


def _report(kind: str) -> Callable[[_Run], None]:
    """Return a stage running ``creek report --type <kind>``."""

    def _stage(run: _Run) -> None:
        run.cli("report", "--type", kind)

    return _stage


def _voice(run: _Run) -> None:
    """Write the register profiles, samples and rhetorical-pattern notes."""
    run.cli("report", "--type", "voice")
    run.cli("report", "--type", "rhetorical-patterns")


def _wavelength(run: _Run) -> None:
    """Write the mode profiles and the phase map of the active topic week.

    The period is named explicitly — the ISO week of the newest topic note — so
    the map never depends on which weekday the lane happens to run.
    """
    run.cli("report", "--type", "mode-profiles")
    active = run.today - timedelta(days=corpus.THREAD_GROUP_AGES["active"])
    year, week, _ = active.isocalendar()
    run.cli("report", "--type", "wavelength", "--period", f"{year}-W{week:02d}")


def _save(run: _Run) -> None:
    """File an observation, an unnamed note and an intimate body back."""
    body = str(corpus.OBSERVATION_FILE)
    for target, title in (("observation", "Rising"), ("unnamed", "Nameless")):
        run.cli(
            *("save", "--target", target, "--body", body),
            *("--title", title, "--tier", "open"),
        )
    # An intimate body never enters the vault: it is diverted to the stubs.
    run.cli(
        "save",
        *("--target", "observation", "--body", body),
        *("--title", "Private", "--tier", "intimate"),
    )


def _compost(run: _Run) -> None:
    """Scan for compost with the verifier's provider replaced."""
    with run.monkeypatch.context() as patch:
        patch.setattr(
            "creek.classify.llm.build_provider", lambda _cfg: _VerdictProvider()
        )
        run.cli("compost", "scan")


def _compile(run: _Run) -> None:
    """Roll a topic note up into the ``06-Frequencies`` index."""
    source = run.uploaded["strategy-active-0.md"]
    claims = (
        '{"claims": [{"id": "c1", "text": "The plan holds.", '
        f'"fragment_ids": ["{source}"]}}], "paradoxes": []}}'
    )
    with run.monkeypatch.context() as patch:
        patch.setattr(
            "creek.compile.engine.default_llm",
            lambda _cfg: lambda _prompt: claims,
        )
        run.cli(
            *("compile", source, "--target-kind", "frequency_index"),
            *("--target-id", _COMPILE_TARGET, "--target-title", "F5 index"),
        )


def _draft(run: _Run) -> None:
    """Draft on the topic with the draft model replaced."""
    with run.monkeypatch.context() as patch:
        patch.setattr(
            cli_module,
            "_build_draft_llm_factory",
            lambda *_a, **_k: lambda _tier: lambda _prompt: "The plan has flow.",
        )
        run.cli("draft", "--seed-topic", corpus.TOPIC_KEYWORD, "--include-tier", "all")


_STAGES: Final[tuple[tuple[str, Callable[[_Run], None]], ...]] = (
    ("ingest", _ingest),
    ("classify.praxis", _classify),
    ("link.eddies", _link_eddies),
    ("link.threads", _link_threads),
    ("voice-drafts", _voice_drafts),
    ("generate.decisions", _report("decisions")),
    ("generate.voice", _voice),
    ("generate.lexicon", _report("lexicon")),
    ("generate.wavelength", _wavelength),
    ("generate.paradox", _report("paradox")),
    ("generate.synchronicity", _report("synchronicity")),
    ("save", _save),
    ("generate.unnamed", _report("unnamed")),
    ("generate.compost", _compost),
    ("compile", _compile),
    ("draft", _draft),
)
"""Every writer subsystem, named as the registry names it, in dependency order."""


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


def _owner(relative: str, registered: frozenset[str]) -> str | None:
    """Return the deepest registered folder holding *relative*, if any."""
    candidate = Path(relative)
    while candidate.parts:
        if candidate.as_posix() in registered:
            return candidate.as_posix()
        candidate = candidate.parent
    return None


def _note_counts(vault: Path) -> dict[str, int]:
    """Count notes per registered folder by walking the disk, uncached.

    Mirrors the status route's attribution rule — a ``.md`` file counts toward
    the deepest registered folder holding it; ``.``/``_`` entries and symlinks
    are not notes — but shares none of its code or its mtime cache, so the
    final comparison checks the route rather than restating it.

    Args:
        vault: The vault root.

    Returns:
        Every registered folder's note count.
    """
    registered = frozenset(folder.path for folder in ONTOLOGY_FOLDERS)
    counts = dict.fromkeys(registered, 0)
    for directory, subdirs, files in os.walk(vault):
        subdirs[:] = [
            name for name in subdirs if not name.startswith(_SKIPPED_PREFIXES)
        ]
        owner = _owner(Path(directory).relative_to(vault).as_posix(), registered)
        if owner is None:
            continue
        counts[owner] += sum(
            1
            for name in files
            if name.endswith(".md")
            and not name.startswith(_SKIPPED_PREFIXES)
            and not (Path(directory) / name).is_symlink()
        )
    return counts


def _run_stages(run: _Run) -> dict[str, dict[str, int]]:
    """Run every stage, recording which folders each one added notes to.

    Returns:
        Stage name to ``{folder: notes added}`` for each folder that grew.
    """
    growth: dict[str, dict[str, int]] = {}
    before = _note_counts(run.vault)
    for name, stage in _STAGES:
        stage(run)
        after = _note_counts(run.vault)
        growth[name] = {
            path: after[path] - before[path]
            for path in after
            if after[path] > before[path]
        }
        before = after
    return growth


def _stage_problems(growth: dict[str, dict[str, int]]) -> list[str]:
    """Name every registered writer with no stage, or whose stage wrote nothing."""
    problems = [
        f"writer {writer!r} is registered but this lane has no stage for it"
        for writer in sorted({f.writer for f in ONTOLOGY_FOLDERS if f.writer})
        if writer not in growth
    ]
    for folder in ONTOLOGY_FOLDERS:
        if folder.writer is None or folder.writer not in growth:
            continue
        filled_by = sorted(name for name, grew in growth.items() if folder.path in grew)
        if folder.path in _KNOWN_UNFILLED:
            if filled_by:
                problems.append(
                    f"{folder.path}: now fills (during {filled_by}); "
                    "remove it from _KNOWN_UNFILLED"
                )
        elif folder.path not in growth[folder.writer]:
            problems.append(
                f"{folder.path}: registered to {folder.writer!r}, whose stage "
                f"added no note (notes arrived during: {filled_by or 'no stage'})"
            )
    return problems


def _status_problems(status: dict[str, Any], counts: dict[str, int]) -> list[str]:
    """Check ``GET /v1/pipeline/status``'s folders against the registry and disk."""
    rows = {row["path"]: row for row in status["folders"]}
    problems = []
    for folder in ONTOLOGY_FOLDERS:
        row = rows.pop(folder.path, None)
        if row is None:
            problems.append(f"{folder.path}: missing from the status route")
            continue
        if row["count"] != counts[folder.path]:
            problems.append(
                f"{folder.path}: route counts {row['count']}, disk holds "
                f"{counts[folder.path]}"
            )
        reason = None if folder.reason is None else folder.reason.value
        if (row["writer"], row["writer_reason"]) != (folder.writer, reason):
            problems.append(
                f"{folder.path}: route says {row['writer']!r}/{row['writer_reason']!r}"
            )
        if folder.writer and not row["count"] and folder.path not in _KNOWN_UNFILLED:
            problems.append(f"{folder.path}: 0 notes, but {folder.writer!r} writes it")
    problems.extend(f"{path}: served but not registered" for path in rows)
    return problems


def _document_problems(
    documents: Sequence[dict[str, Any]], uploaded: Iterable[str]
) -> list[str]:
    """Require every upload listed, its trail showing every stage, each ``ok``."""
    listed = {document["filename"] for document in documents}
    problems = [
        f"{name}: uploaded, but not listed by the status route"
        for name in sorted(set(uploaded) - listed)
    ]
    every_stage = {stage.value for stage in PipelineStage}
    for document in documents:
        outcomes = {record["stage"]: record["outcome"] for record in document["stages"]}
        missing = sorted(every_stage - set(outcomes))
        degraded = {stage: out for stage, out in outcomes.items() if out != "ok"}
        if missing or degraded:
            problems.append(
                f"{document['filename']}: missing stages {missing}, not ok {degraded}"
            )
    return problems


# --------------------------------------------------------------------------- #
# The lane
# --------------------------------------------------------------------------- #


def _scaffold(tmp_path: Path) -> Path:
    """Scaffold a vault with ``creek init`` and require it to hold no note."""
    vault = tmp_path / "vault"
    result = CliRunner().invoke(cli_app, ["init", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert not any(_note_counts(vault).values()), "a fresh vault already holds notes"
    return vault


def test_every_ontology_folder_is_populated_or_writerless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run every writer over the dummy corpus; every folder fills or says why not."""
    monkeypatch.setattr(
        embeddings_module, "_load_sentence_transformer", lambda *_a: _TopicEncoder()
    )
    monkeypatch.setattr(images_module, "PytesseractOcrEngine", _FixedOcrEngine)
    vault = _scaffold(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir()

    with TestClient(build_app(vault_path=vault)) as api:
        run = _Run(api, vault, workdir, datetime.now(UTC).date(), monkeypatch)
        growth = _run_stages(run)
        response = api.get(PIPELINE_STATUS_PATH, headers=headers(ceiling=_CEILING))

    assert response.status_code == _HTTP_OK, response.text
    status = response.json()
    problems = [
        *_stage_problems(growth),
        *_status_problems(status, _note_counts(vault)),
        *_document_problems(status["documents"], run.uploaded),
    ]
    if status["documents_withheld"]:
        problems.append(f"documents_withheld is {status['documents_withheld']}")
    assert not problems, "\n  ".join(["ontology population failed:", *problems])
