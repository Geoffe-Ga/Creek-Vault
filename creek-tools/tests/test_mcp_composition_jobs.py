"""MCP progress, the server deadline and the job-handle fallback (#1966).

Driven through the *registered* tools — ``server.call_tool`` and, for the
progress notifications, a real in-memory MCP client session — because every
property here lives in the adapter: the ``Context`` injection, the deadline,
the admission slot, the hand-off to a job record and the read-back through
``creek.jobs.read``.

A composition that must outlive the deadline is held open by an LLM double
that blocks on a :class:`threading.Event`; the deadline is shrunk through
``CREEK_MCP_SYNC_DEADLINE_SECONDS`` so a test never waits on a real one. Every
scenario that spans more than one call runs inside a single event loop,
because the detached worker is a task on that loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
from mcp.server.auth.provider import AccessToken
from mcp.shared.memory import create_connected_server_and_client_session

from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.progress import CompositionStage
from creek_mcp import composition_jobs as jobs_mod
from creek_mcp import server as server_mod
from creek_mcp.audit import (
    AUDIT_OUTCOME_ACCEPTED,
    AUDIT_OUTCOME_COMPLETED,
    MCP_AUDIT_RELPATH,
)
from creek_mcp.composition_jobs import (
    DEFAULT_SYNC_DEADLINE_SECONDS,
    SYNC_DEADLINE_ENV,
    CompositionCall,
    CompositionJobs,
    sync_deadline_seconds,
)
from creek_mcp.httpapi.job_runtime import JobRuntime
from creek_mcp.policy import Grant, Transport
from creek_mcp.remote_auth import REMOTE_SCOPE
from creek_mcp.server import build_server
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.jobs import UNAVAILABLE_REASON

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mcp.server.fastmcp import FastMCP

    from creek.progress import StageCallback

_SHORT_DEADLINE: Final[str] = "0.05"
"""Seconds; short enough that a held-open composition always trips it."""

_WAIT_SECONDS: Final[float] = 10.0
"""Upper bound on how long a test waits for a released worker to finish."""

_POLL_SECONDS: Final[float] = 0.01
"""Interval between job-state polls while waiting on a released worker."""

_BODY: Final[str] = "A drafted body of words about walking unwalked ground."

_SEED_TITLE: Final[str] = "Unwalked ground"


def _seed() -> IdeaSeed:
    """Return the sourceless ontology-tuple seed a bare vault mines."""
    return IdeaSeed(
        strategy=MiningStrategy.UNEXPLORED_ONTOLOGY,
        title=_SEED_TITLE,
        source_fragments=(),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="An ontology combination not yet inhabited.",
        score=0.5,
    )


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a bare vault whose miner surfaces exactly one sourceless seed."""
    for sub in ("00-Creek-Meta/audit", "01-Fragments/Notes", "creek-skills"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **kwargs: type(
            "_Miner",
            (),
            {"mine_all": lambda self, vault_path, *, current_phase: [_seed()]},
        )(),
    )
    monkeypatch.delenv(SYNC_DEADLINE_ENV, raising=False)
    yield tmp_path


class _Gate:
    """An LLM double that blocks until released, so a call outlives a deadline."""

    def __init__(self) -> None:
        """Start closed."""
        self.entered = threading.Event()
        self.released = threading.Event()

    def draft_factory(self, tier: object) -> Callable[[str], str]:
        """Return a draft client that waits for :meth:`release`."""
        del tier

        def _llm(prompt: str) -> str:
            """Block until released, then return the canned body."""
            del prompt
            self.entered.set()
            self.released.wait(_WAIT_SECONDS)
            return _BODY

        return _llm

    def author_factory(self, tier: object) -> None:
        """Block until released, then keep the desk's deterministic voice."""
        del tier
        self.entered.set()
        self.released.wait(_WAIT_SECONDS)

    def release(self) -> None:
        """Let the held composition finish."""
        self.released.set()


def _token(consumer: str) -> AccessToken:
    """Return a remote token for *consumer* holding the default profile."""
    return AccessToken(
        token="opaque",  # test literal, not a real credential
        client_id=consumer,
        scopes=[REMOTE_SCOPE, Grant.READ.value, Grant.DESK.value],
        expires_at=None,
    )


def _structured(result: object) -> dict[str, Any]:
    """Pull the structured payload out of a FastMCP ``call_tool`` result."""
    payload = result[1] if isinstance(result, tuple) else result
    assert isinstance(payload, dict)
    return payload


def _server(
    vault: Path,
    *,
    gate: _Gate | None = None,
    runtime: JobRuntime | None = None,
) -> FastMCP:
    """Build a stdio server over *vault*, held open by *gate* when given."""
    draft_factory = (
        gate.draft_factory if gate is not None else (lambda tier: lambda prompt: _BODY)
    )
    return build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        draft_llm_factory=draft_factory,
        author_llm_factory=gate.author_factory if gate is not None else None,
        job_runtime=runtime,
    )


async def _call(server: FastMCP, name: str, **arguments: Any) -> dict[str, Any]:
    """Call one registered tool and return its structured payload."""
    return _structured(await server.call_tool(name, arguments))


async def _settled(server: FastMCP, job_id: str) -> dict[str, Any]:
    """Poll ``creek.jobs.read`` until *job_id* leaves the running state."""
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    while True:
        read = await _call(server, "creek.jobs.read", job_id=job_id)
        if read.get("state") not in {"queued", "running"}:
            return read
        assert asyncio.get_running_loop().time() < deadline, "job never settled"
        await asyncio.sleep(_POLL_SECONDS)


async def _idle(runtime: JobRuntime) -> None:
    """Wait until *runtime* has released every admission slot."""
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    while runtime.tasks:
        assert asyncio.get_running_loop().time() < deadline, "slot never freed"
        await asyncio.sleep(_POLL_SECONDS)


def _audit(vault: Path) -> list[dict[str, Any]]:
    """Return every audit entry, oldest first."""
    lines = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _lifecycle(vault: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """Return ``(tool, outcome, args)`` for every job-lifecycle audit entry."""
    return [
        (entry["tool"], entry["outcome"], entry["args_summary"])
        for entry in _audit(vault)
        if entry.get("outcome") in {AUDIT_OUTCOME_ACCEPTED, AUDIT_OUTCOME_COMPLETED}
    ]


# --------------------------------------------------------------------------- #
# The deadline setting
# --------------------------------------------------------------------------- #


def test_the_default_deadline_is_ninety_seconds() -> None:
    """Unset and blank both mean the documented 90 s default."""
    assert DEFAULT_SYNC_DEADLINE_SECONDS == 90.0
    assert sync_deadline_seconds({}) == DEFAULT_SYNC_DEADLINE_SECONDS
    assert sync_deadline_seconds({SYNC_DEADLINE_ENV: "  "}) == 90.0


def test_the_deadline_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator's positive value is honoured, from ``os.environ`` by default."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, "12.5")
    assert sync_deadline_seconds() == 12.5


@pytest.mark.parametrize("raw", ["soon", "0", "-3", "inf", "nan"])
def test_a_nonsense_deadline_fails_loudly(raw: str) -> None:
    """Anything but a positive, finite number of seconds is refused."""
    with pytest.raises(ValueError, match=SYNC_DEADLINE_ENV):
        sync_deadline_seconds({SYNC_DEADLINE_ENV: raw})


def test_build_server_refuses_a_nonsense_deadline(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd deadline stops start-up rather than detaching every call."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, "ninety")
    with pytest.raises(ValueError, match=SYNC_DEADLINE_ENV):
        _server(vault)


# --------------------------------------------------------------------------- #
# Progress over the wire
# --------------------------------------------------------------------------- #


async def _progress_of(
    server: FastMCP, name: str, arguments: dict[str, Any]
) -> tuple[list[tuple[float, float | None, str | None]], dict[str, Any]]:
    """Call *name* through a real client session, recording its progress."""
    seen: list[tuple[float, float | None, str | None]] = []

    async def _record(
        progress: float, total: float | None, message: str | None
    ) -> None:
        """Keep one notification."""
        seen.append((progress, total, message))

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool(name, arguments, progress_callback=_record)
    assert result.structuredContent is not None
    return seen, result.structuredContent


def test_draft_progress_is_emitted_in_stage_order(vault: Path) -> None:
    """The six draft stages arrive as 1/6 .. 6/6, named and nothing more."""
    seen, payload = asyncio.run(
        _progress_of(_server(vault), "creek.draft", {"privacy_tier_ceiling": "open"})
    )
    assert payload["status"] == "ok"
    assert seen == [
        (1.0, 6.0, "seed"),
        (2.0, 6.0, "skills"),
        (3.0, 6.0, "sources"),
        (4.0, 6.0, "composed"),
        (5.0, 6.0, "voice_guarded"),
        (6.0, 6.0, "saved"),
    ]
    stage_names = {stage.value for stage in CompositionStage}
    assert all(message in stage_names for _, _, message in seen)
    assert not any(_SEED_TITLE in str(message) for _, _, message in seen)


def test_author_progress_is_emitted_in_stage_order(vault: Path) -> None:
    """The desk's three stages arrive once each, however many rounds it ran."""
    seen, payload = asyncio.run(
        _progress_of(_server(vault), "creek.author", {"query": "What is pluralism?"})
    )
    assert payload["status"] == "ok"
    assert seen == [
        (1.0, 3.0, "sources"),
        (2.0, 3.0, "composed"),
        (3.0, 3.0, "voice_guarded"),
    ]


def test_a_client_that_asks_for_no_progress_gets_none(vault: Path) -> None:
    """No ``progressToken`` means no notifications, and the call still works."""

    async def _scenario() -> dict[str, Any]:
        """Call without a progress callback."""
        async with create_connected_server_and_client_session(
            _server(vault)._mcp_server
        ) as client:
            result = await client.call_tool("creek.draft", {})
        assert result.structuredContent is not None
        return result.structuredContent

    assert asyncio.run(_scenario())["status"] == "ok"


# --------------------------------------------------------------------------- #
# The fast path
# --------------------------------------------------------------------------- #


def test_the_fast_path_answers_exactly_as_the_synchronous_tool(vault: Path) -> None:
    """A call that beats the deadline returns the tool's own envelope.

    Every key the synchronous tool has always returned is still there, with
    its value; no lifecycle audit entry is written, because nothing detached.
    """
    payload = asyncio.run(_call(_server(vault), "creek.draft"))
    assert payload["status"] == "ok"
    assert payload["tool"] == "creek.draft"
    assert payload["title"] == _SEED_TITLE
    assert payload["source_fragments"] == []
    assert payload["idea_strategy"] == "unexplored_ontology"
    assert payload["draft_path"].startswith("07-Voice/Drafts/")
    assert (vault / payload["draft_path"]).is_file()
    assert payload["source_count"] == 0
    assert payload["voice_guard_status"] == "skipped:disabled"
    assert _lifecycle(vault) == []


def test_the_fast_path_author_still_returns_its_body(vault: Path) -> None:
    """A desk run that beats the deadline keeps its body and saves nothing."""
    payload = asyncio.run(_call(_server(vault), "creek.author", query="pluralism"))
    assert payload["status"] == "ok"
    assert payload["body"].strip()
    assert not (vault / "07-Voice" / "Drafts").exists()


def test_a_dry_run_author_bypasses_the_job_machinery(vault: Path) -> None:
    """The plan composes nothing: no record, no slot, just the plan."""
    runtime = JobRuntime()
    payload = asyncio.run(
        _call(_server(vault, runtime=runtime), "creek.author", query="q", dry_run=True)
    )
    assert payload["status"] == "ok"
    assert payload["dry_run"] is True
    assert not (vault / "00-Creek-Meta" / "State" / "jobs").exists()


def test_a_fast_call_leaves_a_succeeded_content_free_job_record(vault: Path) -> None:
    """The record is written from the first moment and finished either way."""
    asyncio.run(_call(_server(vault), "creek.draft"))
    (record_path,) = (vault / "00-Creek-Meta" / "State" / "jobs").glob("*.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["kind"] == "draft"
    assert record["state"] == "succeeded"
    assert "title" not in record["result"]
    assert "source_fragments" not in record["result"]
    assert _SEED_TITLE not in json.dumps(record["result"])


# --------------------------------------------------------------------------- #
# The deadline trips
# --------------------------------------------------------------------------- #


def test_a_draft_past_the_deadline_returns_a_handle_that_resolves(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the deadline: a handle; then ``creek.jobs.read`` resolves it."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, _SHORT_DEADLINE)
    gate = _Gate()

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Detach, read while running, release, read once settled."""
        server = _server(vault, gate=gate)
        accepted = await _call(server, "creek.draft", privacy_tier_ceiling="personal")
        running = await _call(server, "creek.jobs.read", job_id=accepted["job_id"])
        gate.release()
        return accepted, running, await _settled(server, accepted["job_id"])

    accepted, running, settled = asyncio.run(_scenario())
    assert accepted == {
        "status": "accepted",
        "tool": "creek.draft",
        "tier_ceiling": "personal",
        "job_id": accepted["job_id"],
        "state": "running",
    }
    assert running["state"] == "running"
    assert "result" not in running
    assert settled["status"] == "ok"
    assert settled["state"] == "succeeded"
    result = settled["result"]
    assert result["status"] == "ok"
    assert result["source_count"] == 0
    assert result["voice_guard_status"] == "skipped:disabled"
    assert set(result) == {
        "status",
        "tool",
        "tier_ceiling",
        "draft_path",
        "idea_strategy",
        "source_count",
        "voice_guard_status",
    }
    saved = vault / result["draft_path"]
    assert saved.is_file()
    assert _BODY in saved.read_text(encoding="utf-8")
    assert _BODY not in json.dumps(settled)


def test_the_audit_records_accepted_then_completed_under_one_job_id(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two lifecycle entries, in order, both carrying the same job id."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, _SHORT_DEADLINE)
    gate = _Gate()

    async def _scenario() -> str:
        """Detach, release, wait."""
        server = _server(vault, gate=gate)
        accepted = await _call(server, "creek.draft")
        gate.release()
        await _settled(server, accepted["job_id"])
        return str(accepted["job_id"])

    job_id = asyncio.run(_scenario())
    assert _lifecycle(vault) == [
        ("creek.draft", AUDIT_OUTCOME_ACCEPTED, {"job_id": job_id}),
        (
            "creek.draft",
            AUDIT_OUTCOME_COMPLETED,
            {"job_id": job_id, "state": "succeeded"},
        ),
    ]


def test_an_author_past_the_deadline_files_its_body_in_the_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached desk run has nobody to hand its body to, so it saves it."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, _SHORT_DEADLINE)
    gate = _Gate()

    async def _scenario() -> dict[str, Any]:
        """Detach, release, wait."""
        server = _server(vault, gate=gate)
        accepted = await _call(server, "creek.author", query="What is pluralism?")
        assert accepted["status"] == "accepted"
        gate.release()
        return await _settled(server, accepted["job_id"])

    settled = asyncio.run(_scenario())
    assert settled["state"] == "succeeded"
    result = settled["result"]
    for withheld in ("body", "query", "claims", "provenance"):
        assert withheld not in result
    assert "pluralism" not in json.dumps(result)
    saved = frontmatter.load(vault / result["draft_path"])
    assert saved.content.strip()
    assert saved["idea_strategy"] == "writing_desk"
    assert saved["verdict"] == result["verdict"]
    assert result["claim_count"] >= 0
    assert result["source_count"] == len(saved["source_fragments"])


def test_a_detached_failure_finishes_the_job_failed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A composition that raises after detaching is a ``failed`` job, audited."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, _SHORT_DEADLINE)
    gate = _Gate()

    def _exploding(tier: object) -> Callable[[str], str]:
        """Hold the call open, then raise something the tool does not catch."""
        del tier
        gate.released.wait(_WAIT_SECONDS)
        msg = "a provider fault the tool does not translate"
        raise LookupError(msg)

    async def _scenario() -> dict[str, Any]:
        """Detach, release into the failure, wait."""
        server = build_server(
            transport=Transport.STDIO, vault_path=vault, draft_llm_factory=_exploding
        )
        accepted = await _call(server, "creek.draft")
        gate.release()
        return await _settled(server, accepted["job_id"])

    settled = asyncio.run(_scenario())
    assert settled["state"] == "failed"
    assert "result" not in settled
    assert _lifecycle(vault)[-1][1:] == (
        AUDIT_OUTCOME_COMPLETED,
        {"job_id": settled["job_id"], "state": "failed"},
    )


def test_a_synchronous_failure_still_raises_and_records_failed(vault: Path) -> None:
    """Within the deadline an uncaught fault surfaces exactly as it always has."""

    def _exploding(tier: object) -> Callable[[str], str]:
        """Raise something the tool does not catch."""
        del tier
        msg = "a provider fault the tool does not translate"
        raise LookupError(msg)

    server = build_server(
        transport=Transport.STDIO, vault_path=vault, draft_llm_factory=_exploding
    )
    with pytest.raises(Exception, match="provider fault"):
        asyncio.run(_call(server, "creek.draft"))
    (record_path,) = (vault / "00-Creek-Meta" / "State" / "jobs").glob("*.json")
    assert json.loads(record_path.read_text(encoding="utf-8"))["state"] == "failed"


def test_a_refused_composition_is_a_failed_job_with_no_result(vault: Path) -> None:
    """A tool refusal is answered synchronously and recorded ``failed``."""
    payload = asyncio.run(_call(_server(vault), "creek.draft", index=7))
    assert payload["status"] == "refused"
    (record_path,) = (vault / "00-Creek-Meta" / "State" / "jobs").glob("*.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["state"] == "failed"
    assert record["result"] is None


# --------------------------------------------------------------------------- #
# Admission: one long call per consumer
# --------------------------------------------------------------------------- #


def test_a_second_long_call_while_one_runs_is_refused_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #1605 rule: one active composition per consumer, across both verbs."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, _SHORT_DEADLINE)
    gate = _Gate()

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Detach one, try two more, release, then try again."""
        runtime = JobRuntime()
        server = _server(vault, gate=gate, runtime=runtime)
        accepted = await _call(server, "creek.draft")
        second_draft = await _call(server, "creek.draft")
        second_author = await _call(server, "creek.author", query="q")
        gate.release()
        await _settled(server, accepted["job_id"])
        await _idle(runtime)
        assert second_author == {**second_draft, "tool": "creek.author"}
        return accepted, second_draft, await _call(server, "creek.draft")

    accepted, refused, after = asyncio.run(_scenario())
    assert accepted["status"] == "accepted"
    assert refused == {
        "status": "refused",
        "tool": "creek.draft",
        "tier_ceiling": "open",
        "reason": UNAVAILABLE_REASON,
        "code": "unavailable",
    }
    assert after["status"] == "ok"


def test_the_admission_slot_is_shared_with_the_v1_runtime(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime already running a consumer's ``/v1`` job refuses its draft."""
    monkeypatch.setattr(
        server_mod, "_current_access_token", lambda: _token("adepthood")
    )
    runtime = JobRuntime()
    runtime.active_job_by_consumer["adepthood"] = "a-v1-pipeline-job"
    payload = asyncio.run(_call(_server(vault, runtime=runtime), "creek.draft"))
    assert payload["code"] == "unavailable"


# --------------------------------------------------------------------------- #
# Consumer binding, unknown ids and restarts
# --------------------------------------------------------------------------- #


def test_another_consumers_job_is_refused_exactly_like_an_unknown_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JWT agent and the ``adepthood`` bearer never see each other's jobs."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, _SHORT_DEADLINE)
    gate = _Gate()
    caller = {"token": _token("agent-jwt")}
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: caller["token"])

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Start as one consumer, read as the other and as the owner."""
        server = _server(vault, gate=gate)
        accepted = await _call(server, "creek.draft")
        caller["token"] = _token("adepthood")
        foreign = await _call(server, "creek.jobs.read", job_id=accepted["job_id"])
        unknown = await _call(
            server, "creek.jobs.read", job_id="00000000-0000-4000-8000-000000000000"
        )
        caller["token"] = _token("agent-jwt")
        gate.release()
        return foreign, unknown, await _settled(server, accepted["job_id"])

    foreign, unknown, owned = asyncio.run(_scenario())
    assert foreign == unknown
    assert foreign["status"] == "refused"
    assert foreign["code"] == "unavailable"
    assert owned["state"] == "succeeded"
    assert {
        entry["consumer"]
        for entry in _audit(vault)
        if entry.get("outcome") == AUDIT_OUTCOME_ACCEPTED
    } == {"agent-jwt"}


def test_a_restart_marks_an_in_flight_job_failed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh server (new worker) reads a dead worker's running job as failed."""
    monkeypatch.setenv(SYNC_DEADLINE_ENV, _SHORT_DEADLINE)
    gate = _Gate()

    async def _scenario() -> dict[str, Any]:
        """Detach on one server, read from its replacement, then release."""
        accepted = await _call(_server(vault, gate=gate), "creek.draft")
        restarted = _server(vault)
        read = await _call(restarted, "creek.jobs.read", job_id=accepted["job_id"])
        gate.release()
        return read

    read = asyncio.run(_scenario())
    assert read["status"] == "ok"
    assert read["state"] == "failed"


def test_jobs_read_audits_the_handle_it_was_asked_for(vault: Path) -> None:
    """``creek.jobs.read`` appends one entry naming the id, like every tool."""
    asyncio.run(_call(_server(vault), "creek.jobs.read", job_id="nope"))
    entry = _audit(vault)[-1]
    assert entry["tool"] == "creek.jobs.read"
    assert entry["args_summary"] == {"job_id": "nope"}


# --------------------------------------------------------------------------- #
# The machinery's edges, driven directly
# --------------------------------------------------------------------------- #


def _call_spec(
    work: Callable[..., dict[str, Any]],
    settle: Callable[..., dict[str, Any] | None],
) -> CompositionCall:
    """Return a minimal draft-kind call over *work* and *settle*."""
    return CompositionCall(
        tool="creek.draft",
        kind="draft",
        consumer="direct",
        ceiling=TierCeiling.OPEN,
        stages=(CompositionStage.SEED,),
        work=work,
        settle=settle,
    )


def _ok_work(*, on_stage: StageCallback) -> dict[str, Any]:
    """A composition that reports one stage and succeeds."""
    on_stage(CompositionStage.SEED)
    return {"status": "ok"}


class _NoRequestContext:
    """A ``Context`` stand-in outside any request, as ``call_tool`` gives."""

    @property
    def request_context(self) -> object:
        """Raise exactly as FastMCP's ``Context`` does outside a request."""
        msg = "Context is not available outside of a request"
        raise ValueError(msg)


def test_a_settlement_fault_is_recorded_as_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A projection that raises leaves a ``failed`` record, and is logged."""

    def _broken(response: dict[str, Any], *, detached: bool) -> dict[str, Any]:
        """Fail to project."""
        del response, detached
        msg = "cannot project"
        raise KeyError(msg)

    jobs = CompositionJobs(vault=tmp_path, runtime=JobRuntime(), deadline_seconds=5.0)
    with caplog.at_level(logging.ERROR, logger=jobs_mod.__name__):
        response = asyncio.run(
            jobs.run(_call_spec(_ok_work, _broken), _NoRequestContext())
        )
    assert response == {"status": "ok"}
    (record_path,) = (tmp_path / "00-Creek-Meta" / "State" / "jobs").glob("*.json")
    assert json.loads(record_path.read_text(encoding="utf-8"))["state"] == "failed"
    assert "could not be settled" in caplog.text


def test_a_failed_claim_is_refused_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that cannot be claimed is never run and never answered ok."""
    monkeypatch.setattr(jobs_mod, "claim_job", lambda *args: None)
    ran: list[bool] = []

    def _work(*, on_stage: StageCallback) -> dict[str, Any]:
        """Record that the composition ran."""
        del on_stage
        ran.append(True)
        return {"status": "ok"}

    jobs = CompositionJobs(vault=tmp_path, runtime=JobRuntime(), deadline_seconds=5.0)
    response = asyncio.run(
        jobs.run(
            _call_spec(_work, lambda response, *, detached: None),
            _NoRequestContext(),
        )
    )
    assert response["code"] == "unavailable"
    assert ran == []


def test_a_record_that_cannot_be_finished_is_logged_not_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A disk fault on the terminal write never costs the caller its answer."""

    def _disk_fault(*args: object) -> None:
        """Fail the terminal write."""
        del args
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(jobs_mod, "finish_job", _disk_fault)
    jobs = CompositionJobs(vault=tmp_path, runtime=JobRuntime(), deadline_seconds=5.0)
    with caplog.at_level(logging.ERROR, logger=jobs_mod.__name__):
        response = asyncio.run(
            jobs.run(
                _call_spec(_ok_work, lambda response, *, detached: dict(response)),
                _NoRequestContext(),
            )
        )
    assert response == {"status": "ok"}
    assert "could not be finished" in caplog.text


def test_a_worker_that_finishes_at_the_deadline_still_answers_synchronously(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The near-tie: the deadline fires, but the worker had already finished.

    The detach is forced to run only after the worker has called ``finish``,
    which is the interleaving the shared lock exists for: the caller must get
    the synchronous answer, and no ``accepted`` entry may be written.
    """
    original = jobs_mod._Handoff.detach

    def _late_detach(
        handoff: jobs_mod._Handoff, record_acceptance: Callable[[], None]
    ) -> bool:
        """Release the worker and wait for it to finish before detaching."""
        proceed.set()
        finished.wait(_WAIT_SECONDS)
        return original(handoff, record_acceptance)

    original_finish = jobs_mod._Handoff.finish

    def _signalling_finish(handoff: jobs_mod._Handoff) -> bool:
        """Finish, then tell the detach it may now try."""
        result = original_finish(handoff)
        finished.set()
        return result

    proceed = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(jobs_mod._Handoff, "detach", _late_detach)
    monkeypatch.setattr(jobs_mod._Handoff, "finish", _signalling_finish)

    def _work(*, on_stage: StageCallback) -> dict[str, Any]:
        """Hold until the deadline has fired, then succeed."""
        del on_stage
        proceed.wait(_WAIT_SECONDS)
        return {"status": "ok", "late": True}

    jobs = CompositionJobs(vault=tmp_path, runtime=JobRuntime(), deadline_seconds=0.01)
    response = asyncio.run(
        jobs.run(
            _call_spec(_work, lambda response, *, detached: dict(response)),
            _NoRequestContext(),
        )
    )
    assert response == {"status": "ok", "late": True}
    assert not (tmp_path / MCP_AUDIT_RELPATH).exists()


def test_forwarding_skips_unknown_and_repeated_stages() -> None:
    """Only first sightings of this tool's own stages become notifications."""
    sent: list[tuple[float, float | None, str | None]] = []

    async def _sink(progress: float, total: float | None, message: str | None) -> None:
        """Record one notification."""
        sent.append((progress, total, message))

    async def _scenario() -> None:
        """Queue a messy sequence and forward it."""
        queue: asyncio.Queue[CompositionStage | None] = asyncio.Queue()
        for stage in (
            CompositionStage.SKILLS,
            CompositionStage.SOURCES,
            CompositionStage.SAVED,
            CompositionStage.SOURCES,
            CompositionStage.COMPOSED,
            None,
        ):
            queue.put_nowait(stage)
        stages = (CompositionStage.SOURCES, CompositionStage.COMPOSED)
        await jobs_mod._forward(queue, _sink, stages)

    asyncio.run(_scenario())
    assert sent == [(1.0, 2.0, "sources"), (2.0, 2.0, "composed")]


def test_a_progress_send_failure_stops_forwarding_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client that went away costs the progress, never the composition."""

    async def _gone(progress: float, total: float | None, message: str | None) -> None:
        """Fail like a closed stream."""
        del progress, total, message
        msg = "stream closed"
        raise ConnectionError(msg)

    async def _scenario() -> None:
        """Forward into a dead sink."""
        queue: asyncio.Queue[CompositionStage | None] = asyncio.Queue()
        queue.put_nowait(CompositionStage.SEED)
        await jobs_mod._forward(queue, _gone, (CompositionStage.SEED,))

    with caplog.at_level(logging.WARNING, logger=jobs_mod.__name__):
        asyncio.run(_scenario())
    assert "progress notification not delivered" in caplog.text


def test_posting_to_a_closed_loop_is_swallowed() -> None:
    """A detached worker outliving its server must not fail on a stage post."""
    loop = asyncio.new_event_loop()
    queue: asyncio.Queue[CompositionStage | None] = asyncio.Queue()
    loop.close()
    jobs_mod._post(loop, queue, CompositionStage.SEED)
    assert queue.empty()


def test_a_detached_handoff_drops_later_stages() -> None:
    """Once the call has answered with a handle, no stage reaches it."""
    emitted: list[CompositionStage] = []
    handoff = jobs_mod._Handoff(emitted.append)
    handoff.report(CompositionStage.SEED)
    assert handoff.detach(lambda: None) is True
    handoff.report(CompositionStage.SKILLS)
    assert handoff.finish() is True
    assert emitted == [CompositionStage.SEED]


# --------------------------------------------------------------------------- #
# The content-free projections
# --------------------------------------------------------------------------- #


def test_author_projection_counts_distinct_cited_fragments_and_drops_content() -> None:
    """Counts survive; the body, query, claims and provenance do not."""
    from creek_mcp.tools.author import author_job_result

    response = {
        "status": "ok",
        "tool": "creek.author",
        "tier_ceiling": "open",
        "medium": "research",
        "query": "a private question",
        "body": "a private answer",
        "verdict": "PASS",
        "rounds": 2,
        "provenance": [{"claim_excerpt": "a private claim"}],
        "claims": [
            {"claim": "one", "source_fragments": ["frag-a", "frag-b"]},
            {"claim": "two", "source_fragments": ["frag-b", "frag-c"]},
        ],
        "warnings": [],
    }
    result = author_job_result(response, "07-Voice/Drafts/x.md")
    assert result == {
        "status": "ok",
        "tool": "creek.author",
        "tier_ceiling": "open",
        "medium": "research",
        "verdict": "PASS",
        "rounds": 2,
        "draft_path": "07-Voice/Drafts/x.md",
        "claim_count": 2,
        "source_count": 3,
        "warnings": [],
    }
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("status", ["refused", "error"])
def test_a_non_ok_response_projects_to_no_result(status: str) -> None:
    """A refusal or error is never republished through a job."""
    from creek_mcp.tools.author import author_job_result
    from creek_mcp.tools.draft import draft_job_result

    response = {"status": status, "reason": "names something private"}
    assert author_job_result(response, None) is None
    assert draft_job_result(response) is None


def test_an_unreadable_or_unstamped_draft_has_no_voice_status(tmp_path: Path) -> None:
    """A missing file or a non-string stamp reads as ``None``, never raises."""
    from creek_mcp.tools.draft import _voice_guard_status

    assert _voice_guard_status(tmp_path / "missing.md") is None
    odd = tmp_path / "odd.md"
    odd.write_text("---\nvoice_guard_status: 3\n---\nbody\n", encoding="utf-8")
    assert _voice_guard_status(odd) is None
