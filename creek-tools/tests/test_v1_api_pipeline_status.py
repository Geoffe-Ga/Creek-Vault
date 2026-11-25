"""``GET /v1/pipeline/status`` — what Creek did with each upload, per folder (#2124).

The route answers adepthood's "Your vault" dashboard (adepthood#3775): did my
files land, which stages has each been through, and which ontology folders hold
something — or say why they never will. The tests are organised around its
promises:

* **Every scaffolded folder is listed**, empty and writerless ones included,
  each with a writer or a closed reason.
* **Stage rows come from records**, not inference: the upload receipts and the
  stage log the classify and link tools append.
* **Ceiling and identity**: above-ceiling documents are counted, never named;
  another caller's uploads and jobs are invisible; no body or title crosses.
* **Read-only and bounded**: nothing is written; ``limit`` clamps.
* **Whole-corpus totals** (#2147, contract 0.37): every document of the caller
  is counted per stage by the per-document rule, whatever ``limit`` and the
  ceiling say, without opening a fragment for it; an earlier minor's body is
  unchanged.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
from starlette.responses import JSONResponse

from creek.audit.stage_log import PassStage, StageOutcome, record_stage_run
from creek.vault.folder_writers import ONTOLOGY_FOLDERS
from creek.vault.writer import VaultWriter
from creek_mcp import pipeline_status
from creek_mcp.api.models import (
    PIPELINE_STATUS_MAX_LIMIT,
    PIPELINE_STATUS_SINCE_MINOR,
    PIPELINE_TOTALS_SINCE_MINOR,
    Capability,
    FolderWriterlessReason,
    JobState,
    PipelineStage,
    PipelineStageOutcome,
    PipelineStatusQuery,
    PipelineStatusResponse,
)
from creek_mcp.api.routes import ROUTES
from creek_mcp.httpapi.jobs import create_job
from creek_mcp.tier_ceiling import TierCeiling
from tests.v1_api_support import (
    CLASSIFICATIONS_PATH,
    LINKS_PATH,
    OTHER_TOKEN,
    PIPELINE_STATUS_PATH,
    UPLOAD_PATH,
    client,
    envelope,
    headers,
    seed_vault,
    snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.testclient import TestClient

_OK: Final[int] = 200
_INCOMPATIBLE_VERSION: Final[int] = 409
_INVALID_REQUEST: Final[int] = 422
_UNAVAILABLE: Final[int] = 503
_CANARY: Final[str] = "synthetic-canary-2124: a sentence only the vault holds"
_TITLE: Final[str] = "Synthetic Title Canary 2124"
_PREVIOUS_MINOR: Final[str] = "0.32"
_PRE_TOTALS_MINORS: Final[tuple[str, ...]] = ("0.34", "0.35", "0.36")
"""Every minor that serves the route but predates ``totals`` (#2147)."""
_PRE_TOTALS_KEYS: Final[list[str]] = [
    "status",
    "tier_ceiling",
    "folders",
    "documents",
    "documents_withheld",
    "jobs",
]
"""The 0.34 body's keys, in the order it has always sent them."""


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a seeded vault."""
    return seed_vault(tmp_path / "vault")


def _status(test_client: TestClient, **params: Any) -> dict[str, Any]:
    """GET the status at the current minor and return the validated body."""
    response = test_client.get(
        PIPELINE_STATUS_PATH, headers=headers(ceiling="personal"), params=params
    )
    assert response.status_code == _OK, response.text
    body = envelope(response)
    PipelineStatusResponse.model_validate(body)
    return body


def _upload(
    test_client: TestClient,
    external_id: str,
    *,
    tier: str = "open",
    token_headers: dict[str, str] | None = None,
) -> None:
    """Upload a small markdown document carrying the canary."""
    text = f"# {_TITLE}\n\n{_CANARY} {external_id}\n"
    response = test_client.post(
        UPLOAD_PATH,
        headers=token_headers or headers(ceiling="personal"),
        json={
            "filename": f"{external_id}.md",
            "content_base64": base64.b64encode(text.encode()).decode(),
            "external_id": external_id,
            "tier": tier,
        },
    )
    assert response.status_code == _OK, response.text


def _folder(body: dict[str, Any], path: str) -> dict[str, Any]:
    """Return the folder row for *path*."""
    rows = [row for row in body["folders"] if row["path"] == path]
    assert len(rows) == 1
    return rows[0]


def _stages(document: dict[str, Any]) -> list[tuple[str, str]]:
    """Return a document's ``(stage, outcome)`` pairs."""
    return [(row["stage"], row["outcome"]) for row in document["stages"]]


# --------------------------------------------------------------------------- #
# Publication
# --------------------------------------------------------------------------- #


def test_the_route_is_published_under_the_pipeline_capability() -> None:
    """One GET, gated by the pipeline capability and its own minor."""
    specs = [spec for spec in ROUTES if spec.path == PIPELINE_STATUS_PATH]

    assert len(specs) == 1
    spec = specs[0]
    assert spec.method == "GET"
    assert spec.operation_id == "getPipelineStatus"
    assert spec.capability is Capability.PIPELINE
    assert spec.response_model is PipelineStatusResponse
    assert spec.query_model is PipelineStatusQuery
    assert spec.since_minor == PIPELINE_STATUS_SINCE_MINOR


def test_a_caller_pinned_below_the_route_minor_is_refused(vault: Path) -> None:
    """A minor that never documented the route renegotiates."""
    with client(vault_path=vault) as test_client:
        response = test_client.get(
            PIPELINE_STATUS_PATH, headers=headers(minor=_PREVIOUS_MINOR)
        )

    assert response.status_code == _INCOMPATIBLE_VERSION
    assert envelope(response)["code"] == "incompatible_version"


def test_the_wire_vocabularies_mirror_the_recorders() -> None:
    """Stage, outcome and reason enums are the recorders' own, plus ingested."""
    assert [stage.value for stage in PipelineStage] == [
        "ingested",
        *(stage.value for stage in PassStage),
    ]
    assert [o.value for o in PipelineStageOutcome] == [o.value for o in StageOutcome]
    assert {reason.value for reason in FolderWriterlessReason} == {
        folder.reason.value for folder in ONTOLOGY_FOLDERS if folder.reason
    }


# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #


def test_every_scaffolded_folder_is_listed_with_its_writer(vault: Path) -> None:
    """Empty and writerless folders are listed, each saying what fills it."""
    with client(vault_path=vault) as test_client:
        body = _status(test_client)

    assert [
        (row["path"], row["writer"], row["writer_reason"]) for row in body["folders"]
    ] == [
        (f.path, f.writer, None if f.reason is None else f.reason.value)
        for f in ONTOLOGY_FOLDERS
    ]
    assert all(row["count"] == 0 for row in body["folders"])
    assert all(row["last_written_at"] is None for row in body["folders"])
    assert body["documents"] == []
    assert body["jobs"] == []
    assert body["documents_withheld"] == 0


def test_a_note_counts_toward_its_deepest_scaffolded_folder(vault: Path) -> None:
    """Nested scaffold folders count alone; unregistered subfolders roll up."""
    writing = vault / "01-Fragments" / "Writing"
    (writing / "Substack").mkdir(parents=True)
    (writing / "Medium").mkdir()
    (writing / "Substack" / "a.md").write_text("x", encoding="utf-8")
    (writing / "b.md").write_text("x", encoding="utf-8")
    (writing / "Medium" / "c.md").write_text("x", encoding="utf-8")
    (writing / ".id-index.jsonl").write_text("{}", encoding="utf-8")
    (writing / "_README.md").write_text("x", encoding="utf-8")
    (writing / "notes.txt").write_text("x", encoding="utf-8")
    stamp = datetime(2026, 11, 20, 17, 2, 11, tzinfo=UTC)
    os.utime(writing / "Medium" / "c.md", (stamp.timestamp(), stamp.timestamp()))
    os.utime(writing / "b.md", (stamp.timestamp() - 60, stamp.timestamp() - 60))

    with client(vault_path=vault) as test_client:
        body = _status(test_client)

    assert _folder(body, "01-Fragments/Writing")["count"] == 2
    assert _folder(body, "01-Fragments/Writing")["last_written_at"] == (
        "2026-11-20T17:02:11Z"
    )
    assert _folder(body, "01-Fragments/Writing/Substack")["count"] == 1
    assert _folder(body, "01-Fragments")["count"] == 0


def test_a_new_note_is_seen_on_the_next_read(vault: Path) -> None:
    """The per-directory cache is keyed on the directory's own mtime."""
    notes = vault / "01-Fragments" / "Notes"
    with client(vault_path=vault) as test_client:
        before = _folder(_status(test_client), "01-Fragments/Notes")["count"]
        (notes / "fresh.md").write_text("x", encoding="utf-8")
        later = notes.stat().st_mtime + 5
        os.utime(notes, (later, later))
        after = _folder(_status(test_client), "01-Fragments/Notes")["count"]

    assert (before, after) == (0, 1)


def test_a_symlinked_folder_is_not_followed(vault: Path, tmp_path: Path) -> None:
    """A link out of the vault is not walked into."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("x", encoding="utf-8")
    (vault / "01-Fragments" / "Notes" / "linked").symlink_to(outside)
    (vault / "01-Fragments" / "Notes" / "linked.md").symlink_to(outside / "x.md")

    with client(vault_path=vault) as test_client:
        body = _status(test_client)

    assert _folder(body, "01-Fragments/Notes")["count"] == 0


# --------------------------------------------------------------------------- #
# Documents and their stage trail
# --------------------------------------------------------------------------- #


def test_an_upload_then_classify_and_link_shows_every_stage(vault: Path) -> None:
    """Upload, classify and link over HTTP; the trail names each stage ok."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        for path, method in ((CLASSIFICATIONS_PATH, "rules"), (LINKS_PATH, "temporal")):
            response = test_client.post(
                path, headers=headers(ceiling="personal"), json={"method": method}
            )
            assert response.status_code == _OK, response.text
        body = _status(test_client)

    [document] = body["documents"]
    assert document["external_id"] == "up-1"
    assert document["filename"] == "up-1.md"
    assert document["fragment_count"] == 1
    assert document["fragment_path"].startswith("01-Fragments/")
    assert (vault / document["fragment_path"]).is_file()
    assert _stages(document) == [
        ("ingested", "ok"),
        ("classified", "ok"),
        ("praxis_scanned", "ok"),
        ("linked:temporal", "ok"),
    ]
    landed = sum(
        row["count"] for row in body["folders"] if row["path"].startswith("01-")
    )
    assert landed == 1


def test_a_pass_that_ran_before_the_upload_does_not_cover_it(vault: Path) -> None:
    """A stage is claimed only for a pass that started after the document landed."""
    with client(vault_path=vault) as test_client:
        test_client.post(
            CLASSIFICATIONS_PATH,
            headers=headers(ceiling="personal"),
            json={"method": "rules"},
        )
        _upload(test_client, "up-1")
        body = _status(test_client)

    assert _stages(body["documents"][0]) == [("ingested", "ok")]


def test_a_failed_attempt_shows_until_a_later_pass_succeeds(vault: Path) -> None:
    """Failed is reported only while no covering pass has done the work."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        later = datetime.now(UTC) + timedelta(seconds=1)
        record_stage_run(
            vault,
            stage=PassStage.LINKED_EDDIES,
            outcome=StageOutcome.FAILED,
            started_at=later,
            finished_at=later,
        )
        failed = _stages(_status(test_client)["documents"][0])
        record_stage_run(
            vault,
            stage=PassStage.LINKED_EDDIES,
            outcome=StageOutcome.PARTIAL,
            started_at=later + timedelta(seconds=1),
            finished_at=later + timedelta(seconds=2),
        )
        partial = _stages(_status(test_client)["documents"][0])

    assert ("linked:eddies", "failed") in failed
    assert ("linked:eddies", "partial") in partial


def test_documents_are_newest_first_and_limited(vault: Path) -> None:
    """``limit`` trims to the most recently landed; above the cap it clamps."""
    with client(vault_path=vault) as test_client:
        for external_id in ("up-1", "up-2", "up-3"):
            _upload(test_client, external_id)
        limited = _status(test_client, limit=2)
        clamped = _status(test_client, limit=PIPELINE_STATUS_MAX_LIMIT + 1)

    assert [d["external_id"] for d in limited["documents"]] == ["up-3", "up-2"]
    assert len(clamped["documents"]) == 3


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=abc", "limit=1&limit=2", "cursor=x"],
)
def test_an_inadmissible_query_is_invalid(vault: Path, query: str) -> None:
    """Out-of-bounds, repeated and unknown parameters are refused."""
    with client(vault_path=vault) as test_client:
        response = test_client.get(f"{PIPELINE_STATUS_PATH}?{query}", headers=headers())

    assert response.status_code == _INVALID_REQUEST
    assert envelope(response)["code"] == "invalid_request"


def test_an_above_ceiling_document_is_counted_never_named(vault: Path) -> None:
    """A personal upload read at an open ceiling is withheld, not listed."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-secret", tier="personal")
        _upload(test_client, "up-open")
        response = test_client.get(PIPELINE_STATUS_PATH, headers=headers())
        body = envelope(response)

    assert [d["external_id"] for d in body["documents"]] == ["up-open"]
    assert body["documents_withheld"] == 1
    assert "up-secret" not in response.text
    assert sum(row["count"] for row in body["folders"]) == 2


def test_a_tier_raised_after_upload_withholds_the_document(vault: Path) -> None:
    """The on-disk tier governs, so a later escalation is honoured."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        path = _status(test_client)["documents"][0]["fragment_path"]
        fragment = vault / path
        fragment.write_text(
            fragment.read_text(encoding="utf-8").replace(
                "privacy_tier: open", "privacy_tier: intimate"
            ),
            encoding="utf-8",
        )
        body = envelope(test_client.get(PIPELINE_STATUS_PATH, headers=headers()))

    assert body["documents"] == []
    assert body["documents_withheld"] == 1


def test_another_callers_uploads_are_invisible(vault: Path) -> None:
    """Documents are scoped to the consumer that uploaded them."""
    with client(vault_path=vault) as test_client:
        _upload(
            test_client,
            "up-theirs",
            token_headers=headers(token=OTHER_TOKEN, ceiling="personal"),
        )
        body = _status(test_client)

    assert body["documents"] == []
    assert body["documents_withheld"] == 0


def test_no_body_or_title_crosses_the_wire(vault: Path) -> None:
    """The response names files and ids, never their content."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        response = test_client.get(
            PIPELINE_STATUS_PATH, headers=headers(ceiling="personal")
        )

    assert _CANARY not in response.text
    assert _TITLE not in response.text


def _withheld_at_open(test_client: TestClient) -> dict[str, Any]:
    """Read the status at an ``open`` ceiling and return the body."""
    response = test_client.get(PIPELINE_STATUS_PATH, headers=headers())
    assert response.status_code == _OK, response.text
    return envelope(response)


def test_a_tombed_fragment_withholds_its_document(vault: Path) -> None:
    """A fragment moved out of ``01-Fragments`` fails closed, never drops out.

    ``VaultWriter.tomb_fragment`` relocates a vanished unit's fragment into
    ``10-Liminal/Orphaned``, beyond the walk that resolves ids. Its tier can
    no longer be read there, so it counts as ``intimate``: the document is
    withheld from a lower ceiling and counted, never listed (#2136 review).
    """
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        path = _status(test_client)["documents"][0]["fragment_path"]
        fragment_id = str(frontmatter.load(str(vault / path)).metadata["id"])
        assert VaultWriter(vault).tomb_fragment(fragment_id) is not None
        body = _withheld_at_open(test_client)

    assert body["documents"] == []
    assert body["documents_withheld"] == 1


_BORROWED_NOTE: Final[str] = (
    "---\nsource:\n  platform: markdown\n  author: other\n"
    "  author_slug: montaigne\n---\n# Of experience\n\n"
    "No desire is more natural than the desire for knowledge.\n"
)
"""A note naming its author: ingest files it under ``11-Other-Authors/montaigne``."""


def _upload_borrowed(test_client: TestClient, tier: str) -> None:
    """Upload :data:`_BORROWED_NOTE` at *tier*."""
    response = test_client.post(
        UPLOAD_PATH,
        headers=headers(ceiling="personal"),
        json={
            "filename": "of-experience.md",
            "content_base64": base64.b64encode(_BORROWED_NOTE.encode()).decode(),
            "external_id": "borrowed-1",
            "tier": tier,
        },
    )
    assert response.status_code == _OK, response.text


def test_an_upload_filed_under_other_authors_is_listed_at_its_tier(
    vault: Path,
) -> None:
    """A borrowed note resolves where ingest filed it, not only ``01-Fragments``.

    Ingest files a note whose frontmatter names ``source.author_slug`` into
    ``11-Other-Authors/<slug>``. Resolving fragments under ``01-Fragments``
    alone left it unresolvable, so the fail-closed rule withheld it at every
    ceiling and its caller never saw its trail (#2125).
    """
    with client(vault_path=vault) as test_client:
        _upload_borrowed(test_client, "personal")
        body = _status(test_client)

    assert body["documents_withheld"] == 0
    [document] = body["documents"]
    assert document["filename"] == "of-experience.md"
    assert document["fragment_path"].startswith("11-Other-Authors/montaigne/")
    assert (vault / document["fragment_path"]).is_file()
    assert _stages(document)[0] == ("ingested", "ok")


def test_an_upload_filed_under_other_authors_is_withheld_above_the_ceiling(
    vault: Path,
) -> None:
    """Resolved there, its own tier still decides: ``personal`` is not ``open``."""
    with client(vault_path=vault) as test_client:
        _upload_borrowed(test_client, "personal")
        body = _withheld_at_open(test_client)

    assert body["documents"] == []
    assert body["documents_withheld"] == 1


def test_the_resolved_roots_are_the_live_ingest_folders() -> None:
    """Every folder ingest writes a live fragment into is walked; tombs are not.

    Derived from the folder registry, so a new ingest destination is resolved
    without a second list to keep in step. ``10-Liminal/Orphaned`` is ingest's
    too, but a tombed fragment is not live and stays fail-closed (#2136).
    """
    ingest = {f.path for f in ONTOLOGY_FOLDERS if f.writer == "ingest"}
    live = ingest - {"10-Liminal/Orphaned"}

    roots = pipeline_status.FRAGMENT_ROOTS

    assert roots == ("01-Fragments", "11-Other-Authors")
    assert all(path.split("/", 1)[0] in roots for path in live)
    assert "10-Liminal" not in roots


def test_a_missing_fragment_withholds_its_document(vault: Path) -> None:
    """A fragment that is simply gone is unprovable, so it is withheld."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        path = _status(test_client)["documents"][0]["fragment_path"]
        (vault / path).unlink()
        body = _withheld_at_open(test_client)

    assert body["documents"] == []
    assert body["documents_withheld"] == 1


@pytest.mark.parametrize("filename", ["../../outside.md", "sub/inner.md", ".."])
def test_a_corrupted_index_filename_is_skipped_and_fails_closed(
    vault: Path, filename: str
) -> None:
    """An index entry naming a path outside its directory is never followed.

    The newest mapping wins in ``.id-index.jsonl``, so a hand-corrupted line
    redirects the id. Following it could leave the vault or put a path the
    wire pattern rejects into the body (an uncaught ``500``); instead the
    entry is skipped, the fragment is unresolved, and the document is
    withheld like any unprovable one.
    """
    (vault / "outside.md").write_text(
        "---\nid: x\nprivacy_tier: open\n---\nbody\n", encoding="utf-8"
    )
    inner = vault / "01-Fragments" / "Notes" / "sub"
    inner.mkdir(parents=True)
    (inner / "inner.md").write_text("x", encoding="utf-8")
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        path = _status(test_client)["documents"][0]["fragment_path"]
        fragment = vault / path
        fragment_id = str(frontmatter.load(str(fragment)).metadata["id"])
        index = fragment.parent / ".id-index.jsonl"
        with index.open("a", encoding="utf-8") as handle:
            handle.write("\n" + json.dumps({"id": fragment_id, "filename": filename}))
        body = _withheld_at_open(test_client)

    assert body["documents"] == []
    assert body["documents_withheld"] == 1


# --------------------------------------------------------------------------- #
# Whole-corpus totals (#2147, contract 0.37)
# --------------------------------------------------------------------------- #


def _totals(body: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """Return ``stage -> (covered, partial, failed)`` from a body's totals."""
    return {
        row["stage"]: (row["covered"], row["partial"], row["failed"])
        for row in body["totals"]["stages"]
    }


def _counted_from_rows(body: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """Count the ``documents`` rows the way ``totals`` claims to."""
    column = {"ok": 0, "partial": 1, "failed": 2}
    counts = {stage.value: [0, 0, 0] for stage in PipelineStage}
    for document in body["documents"]:
        for stage, outcome in _stages(document):
            counts[stage][column[outcome]] += 1
    return {stage: (c[0], c[1], c[2]) for stage, c in counts.items()}


def _record(vault: Path, stage: PassStage, outcome: StageOutcome) -> None:
    """Record a pass that starts now, covering everything landed so far."""
    now = datetime.now(UTC)
    record_stage_run(
        vault, stage=stage, outcome=outcome, started_at=now, finished_at=now
    )


def test_the_totals_minor_is_pinned() -> None:
    """``totals`` arrives at 0.37; the route itself stays a 0.34 route."""
    assert PIPELINE_TOTALS_SINCE_MINOR == "0.37"
    assert PIPELINE_STATUS_SINCE_MINOR == "0.34"


def test_an_empty_corpus_lists_every_stage_at_zero(vault: Path) -> None:
    """Every stage has a row, in pipeline order, so none is read from absence."""
    with client(vault_path=vault) as test_client:
        body = _status(test_client)

    assert body["totals"]["documents"] == 0
    assert [row["stage"] for row in body["totals"]["stages"]] == [
        stage.value for stage in PipelineStage
    ]
    assert set(_totals(body).values()) == {(0, 0, 0)}


def test_totals_count_every_document_whatever_the_limit(vault: Path) -> None:
    """A one-row page still reports the whole corpus, stage by stage."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        _upload(test_client, "up-2")
        _record(vault, PassStage.CLASSIFIED, StageOutcome.PARTIAL)
        _record(vault, PassStage.LINKED_TEMPORAL, StageOutcome.OK)
        _upload(test_client, "up-3")
        _record(vault, PassStage.LINKED_EDDIES, StageOutcome.FAILED)
        page = _status(test_client, limit=1)
        whole = _status(test_client, limit=3)

    assert [d["external_id"] for d in page["documents"]] == ["up-3"]
    assert page["totals"] == whole["totals"]
    assert page["totals"]["documents"] == 3
    assert _totals(page) == {
        "ingested": (3, 0, 0),
        "classified": (0, 2, 0),
        "praxis_scanned": (0, 0, 0),
        "linked:embeddings": (0, 0, 0),
        "linked:temporal": (2, 0, 0),
        "linked:eddies": (0, 0, 3),
        "linked:threads": (0, 0, 0),
    }


def test_totals_agree_with_the_document_rows(vault: Path) -> None:
    """One rule serves both, so a full page recounts to exactly the totals."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        _record(vault, PassStage.LINKED_THREADS, StageOutcome.FAILED)
        _upload(test_client, "up-2")
        _record(vault, PassStage.LINKED_THREADS, StageOutcome.OK)
        _record(vault, PassStage.PRAXIS_SCANNED, StageOutcome.PARTIAL)
        _upload(test_client, "up-3")
        body = _status(test_client)

    assert len(body["documents"]) == body["totals"]["documents"] == 3
    assert _totals(body) == _counted_from_rows(body)
    assert _totals(body)["linked:threads"] == (2, 0, 0)
    assert _totals(body)["ingested"] == (3, 0, 0)


def test_totals_count_above_ceiling_documents_and_name_none(vault: Path) -> None:
    """A count names nothing: withheld documents are counted, never identified."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-secret", tier="personal")
        _upload(test_client, "up-open")
        _record(vault, PassStage.CLASSIFIED, StageOutcome.OK)
        response = test_client.get(PIPELINE_STATUS_PATH, headers=headers())
        body = envelope(response)

    assert [d["external_id"] for d in body["documents"]] == ["up-open"]
    assert body["documents_withheld"] == 1
    assert body["totals"]["documents"] == 2
    assert _totals(body)["ingested"] == (2, 0, 0)
    assert _totals(body)["classified"] == (2, 0, 0)
    assert "up-secret" not in response.text
    assert set(body["totals"]) == {"documents", "stages"}
    assert all(
        set(row) == {"stage", "covered", "partial", "failed"}
        for row in body["totals"]["stages"]
    )


def test_totals_leave_out_another_callers_uploads(vault: Path) -> None:
    """The totals are this caller's corpus, as the rows are."""
    with client(vault_path=vault) as test_client:
        _upload(
            test_client,
            "up-theirs",
            token_headers=headers(token=OTHER_TOKEN, ceiling="personal"),
        )
        _upload(test_client, "up-mine")
        body = _status(test_client)

    assert body["totals"]["documents"] == 1
    assert _totals(body)["ingested"] == (1, 0, 0)


def test_a_re_landed_document_counts_once(vault: Path) -> None:
    """A document is one ``external_id``, however many times it landed."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        response = test_client.post(
            UPLOAD_PATH,
            headers=headers(ceiling="personal"),
            json={
                "filename": "up-1.md",
                "content_base64": base64.b64encode(b"# Rewritten\n\nNew.\n").decode(),
                "external_id": "up-1",
                "tier": "open",
            },
        )
        assert response.status_code == _OK, response.text
        body = _status(test_client)

    assert body["totals"]["documents"] == 1


def test_totals_open_no_fragment_beyond_the_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Totals come from receipts and the stage log: no fragment is read for them.

    With three documents and ``limit=1`` only the page's own fragment is
    resolved and opened; the two beyond it are counted without being looked
    up, so the cost of a poll does not grow with what it merely counts.
    """
    resolved: list[set[str]] = []
    opened: list[Path] = []
    real_paths = pipeline_status._fragment_paths
    real_header = pipeline_status.read_header_meta

    def _paths(root: Path, wanted: set[str]) -> dict[str, Path]:
        resolved.append(set(wanted))
        return real_paths(root, wanted)

    def _header(path: Path) -> Any:
        opened.append(path)
        return real_header(path)

    with client(vault_path=vault) as test_client:
        for external_id in ("up-1", "up-2", "up-3"):
            _upload(test_client, external_id)
        monkeypatch.setattr(pipeline_status, "_fragment_paths", _paths)
        monkeypatch.setattr(pipeline_status, "read_header_meta", _header)
        body = _status(test_client, limit=1)

    [document] = body["documents"]
    assert body["totals"]["documents"] == 3
    assert [len(wanted) for wanted in resolved] == [1]
    assert opened == [vault / document["fragment_path"]]


@pytest.mark.parametrize("minor", _PRE_TOTALS_MINORS)
def test_an_earlier_minor_gets_the_body_it_always_got(vault: Path, minor: str) -> None:
    """0.34 to 0.36 vendored a closed schema without ``totals``: it is not sent."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        _record(vault, PassStage.CLASSIFIED, StageOutcome.OK)
        earlier = test_client.get(
            PIPELINE_STATUS_PATH, headers=headers(minor=minor, ceiling="personal")
        )
        current = test_client.get(
            PIPELINE_STATUS_PATH, headers=headers(ceiling="personal")
        )

    assert earlier.status_code == _OK, earlier.text
    body = envelope(earlier)
    assert list(body) == _PRE_TOTALS_KEYS
    without = {k: v for k, v in envelope(current).items() if k != "totals"}
    assert earlier.content == JSONResponse(without).body
    assert list(envelope(current)) == [*_PRE_TOTALS_KEYS, "totals"]


def test_totals_are_not_computed_when_not_asked_for(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that cannot read ``totals`` does not pay for them."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError

    monkeypatch.setattr(pipeline_status, "_totals", _boom)

    status = pipeline_status.build_status(
        vault,
        consumer="adepthood",
        ceiling=TierCeiling.PERSONAL,
        limit=1,
        worker_id="worker",
        active_job_ids=frozenset(),
        with_totals=False,
    )

    assert status.totals is None


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


def test_jobs_are_this_callers_pipeline_jobs_read_without_recovery(
    vault: Path,
) -> None:
    """Own pipeline jobs only; an orphaned running job reads failed, unwritten."""
    mine = create_job(
        vault,
        consumer="adepthood",
        kind="classification",
        method="llm",
        retier=False,
        ceiling=TierCeiling.PERSONAL.value,
        worker_id="a-dead-worker",
    )
    create_job(
        vault,
        consumer="crawdad",
        kind="link",
        method="embeddings",
        retier=False,
        ceiling=TierCeiling.PERSONAL.value,
        worker_id="a-dead-worker",
    )
    create_job(
        vault,
        consumer="adepthood",
        kind="draft",
        method="draft",
        retier=False,
        ceiling=TierCeiling.PERSONAL.value,
        worker_id="a-dead-worker",
    )
    record = vault / "00-Creek-Meta" / "State" / "jobs" / f"{mine.job_id}.json"
    before = record.read_bytes()

    with client(vault_path=vault) as test_client:
        body = _status(test_client)

    [job] = body["jobs"]
    assert job["job_id"] == mine.job_id
    assert (job["kind"], job["method"]) == ("classification", "llm")
    assert job["state"] == JobState.FAILED.value
    assert job["failure_code"] is None
    assert record.read_bytes() == before


# --------------------------------------------------------------------------- #
# Read-only and availability
# --------------------------------------------------------------------------- #


def test_the_read_writes_nothing(vault: Path) -> None:
    """A status read triggers no stage and leaves the vault as it found it."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        before = snapshot(vault)
        _status(test_client)
        after = snapshot(vault)

    assert after == before


def test_a_missing_vault_is_unavailable(tmp_path: Path) -> None:
    """No vault is ``503 unavailable``, never an empty ``200``."""
    with client(vault_path=tmp_path / "absent") as test_client:
        response = test_client.get(PIPELINE_STATUS_PATH, headers=headers())

    assert response.status_code == _UNAVAILABLE
    assert envelope(response)["code"] == "unavailable"


def test_an_unreadable_vault_mid_read_is_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing failure is ``503``, never a short ``200``."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise PermissionError

    monkeypatch.setattr(pipeline_status, "build_status", _boom)
    with client(vault_path=vault) as test_client:
        response = test_client.get(PIPELINE_STATUS_PATH, headers=headers())

    assert response.status_code == _UNAVAILABLE
