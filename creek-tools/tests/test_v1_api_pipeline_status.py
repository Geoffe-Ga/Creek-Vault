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
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek.audit.stage_log import PassStage, StageOutcome, record_stage_run
from creek.vault.folder_writers import ONTOLOGY_FOLDERS
from creek_mcp import pipeline_status
from creek_mcp.api.models import (
    PIPELINE_STATUS_MAX_LIMIT,
    PIPELINE_STATUS_SINCE_MINOR,
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


def test_a_purged_fragment_leaves_its_document_without_a_path(vault: Path) -> None:
    """A fragment no longer live reads ``fragment_path: null``, still listed."""
    with client(vault_path=vault) as test_client:
        _upload(test_client, "up-1")
        path = _status(test_client)["documents"][0]["fragment_path"]
        (vault / path).unlink()
        body = _status(test_client)

    assert body["documents"][0]["fragment_path"] is None


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
