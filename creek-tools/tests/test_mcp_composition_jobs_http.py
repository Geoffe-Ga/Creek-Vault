"""Progress and job handles over ``/mcp`` inside the ``/v1`` process (#1966).

``tests/test_mcp_composition_jobs.py`` proves the machinery through the
registered tools and the SDK's in-memory transport. This module proves the two
properties that only exist once the server is mounted at ``/mcp`` (#1962):

* the progress notifications really travel on the streamable-HTTP response
  stream of the ``tools/call`` that caused them — the mount skips ``/v1``'s
  request timeout and body replay precisely so such a stream survives;
* the ``/mcp`` server and the ``/v1`` routes share **one**
  :class:`~creek_mcp.httpapi.job_runtime.JobRuntime`, so a consumer running a
  detached draft is refused a second expensive ``/v1`` pass, and the handle it
  was given resolves through ``creek.jobs.read`` on the same mount.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any, Final

import pytest
from starlette.testclient import TestClient

from creek.generate.mining import IdeaSeed, MiningStrategy
from creek_mcp import server as server_mod
from creek_mcp.composition_jobs import SYNC_DEADLINE_ENV
from creek_mcp.mcp_path import MCP_PATH
from tests.mcp_http_support import call_tool, open_session
from tests.v1_api_support import CONSUMER, build_app, headers, seed_vault

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_BODY: Final[str] = "A drafted body of words about walking unwalked ground."

_WAIT_SECONDS: Final[float] = 10.0
"""Upper bound on how long a test waits for a released worker to finish."""

_POLL_SECONDS: Final[float] = 0.02
"""Interval between job-state polls."""

_HTTP_OK: Final[int] = 200
_HTTP_UNAVAILABLE: Final[int] = 503


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a seeded vault whose miner surfaces exactly one sourceless seed."""
    seed = IdeaSeed(
        strategy=MiningStrategy.UNEXPLORED_ONTOLOGY,
        title="Unwalked ground",
        source_fragments=(),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="An ontology combination not yet inhabited.",
        score=0.5,
    )
    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **kwargs: type(
            "_Miner",
            (),
            {"mine_all": lambda self, vault_path, *, current_phase: [seed]},
        )(),
    )
    monkeypatch.delenv(SYNC_DEADLINE_ENV, raising=False)
    yield seed_vault(tmp_path)


def _draft_llm(
    gate: threading.Event | None,
) -> Callable[[Path, object], Callable[[str], str]]:
    """Return a stand-in for the production draft-LLM builder.

    ``/mcp`` builds its server with the production factories, so the builder
    itself is replaced; *gate*, when given, holds each draft open.
    """

    def _build(vault: Path, tier: object, **scope: object) -> Callable[[str], str]:
        """Return a client that waits on *gate*, then drafts.

        *scope* is the call's request key and route recorder (#1967), which
        this stand-in neither needs nor keeps.
        """
        del vault, tier, scope

        def _llm(prompt: str) -> str:
            """Return the canned body once released."""
            del prompt
            if gate is not None:
                gate.wait(_WAIT_SECONDS)
            return _BODY

        return _llm

    return _build


def _sse_messages(body: str) -> list[dict[str, Any]]:
    """Return every JSON-RPC message on a streamable-HTTP SSE response."""
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]


def test_draft_progress_streams_on_the_tools_call_response_at_mcp(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six progress notifications, then the result, on one ``/mcp`` response."""
    monkeypatch.setattr(server_mod, "_build_draft_llm", _draft_llm(None))
    with TestClient(build_app(vault_path=vault)) as client:
        session = open_session(client)
        response = client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "creek.draft",
                    "arguments": {},
                    "_meta": {"progressToken": "draft-1"},
                },
            },
            headers=session["headers"],
        )
    assert response.status_code == _HTTP_OK
    messages = _sse_messages(response.text)
    progress = [
        message["params"]
        for message in messages
        if message.get("method") == "notifications/progress"
    ]
    assert [(p["progress"], p["total"], p["message"]) for p in progress] == [
        (1.0, 6.0, "seed"),
        (2.0, 6.0, "skills"),
        (3.0, 6.0, "sources"),
        (4.0, 6.0, "composed"),
        (5.0, 6.0, "voice_guarded"),
        (6.0, 6.0, "saved"),
    ]
    assert {p["progressToken"] for p in progress} == {"draft-1"}
    assert messages[-1]["result"]["structuredContent"]["status"] == "ok"


def test_a_handle_from_mcp_holds_the_v1_slot_and_resolves_at_mcp(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the deadline at ``/mcp``: a handle, one shared slot, a resolution."""
    gate = threading.Event()
    monkeypatch.setattr(server_mod, "_build_draft_llm", _draft_llm(gate))
    monkeypatch.setenv(SYNC_DEADLINE_ENV, "0.05")
    app = build_app(vault_path=vault)
    with TestClient(app) as client:
        session = open_session(client)
        accepted = call_tool(client, session["headers"], "creek.draft", {})
        job_id = accepted["job_id"]
        held = dict(app.state.pipeline_jobs.active_job_by_consumer)
        refused = client.post(
            "/v1/classifications", json={"method": "llm"}, headers=headers()
        )
        running = call_tool(
            client, session["headers"], "creek.jobs.read", {"job_id": job_id}
        )
        gate.set()
        settled = running
        deadline = time.monotonic() + _WAIT_SECONDS
        while settled["state"] in {"queued", "running"}:
            assert time.monotonic() < deadline, "job never settled"
            time.sleep(_POLL_SECONDS)
            settled = call_tool(
                client, session["headers"], "creek.jobs.read", {"job_id": job_id}
            )
    assert accepted["status"] == "accepted"
    assert held == {CONSUMER: job_id}
    assert refused.status_code == _HTTP_UNAVAILABLE
    assert running["state"] == "running"
    assert settled["state"] == "succeeded"
    assert (vault / settled["result"]["draft_path"]).is_file()
