"""A writer's request-scoped key over ``/mcp``, and nowhere else (#1967, ADR-0015).

``tests/test_request_scoped_byok.py`` proves the key machinery on ``/v1``.
This module proves the same two headers on the MCP streamable-HTTP transport
served inside the ``/v1`` process (#1962), for the four tools that call the
vault's model on a writer's behalf — ``creek.draft``, ``creek.author``,
``creek.revoice`` and ``creek.reflect``:

1. **It works.** A managed vault with no local model drafts, authors and
   reflects with the writer's key, and names the route it took —
   ``inference_source`` on the answer and, for a draft, in its frontmatter.
2. **The refusals that stood still stand.** One header alone or an unknown
   vendor is the MCP refusal envelope with ``/v1``'s fixed
   ``invalid_request`` message; a keyless call, a self-connected vault and an
   agent's own bearer change nothing.
3. **The tier gate is the router's, unchanged.** An intimate-derived draft is
   never handed to the writer's vendor.
4. **The key goes nowhere.** A canary is swept from every response, every log
   record and every file under the test root — audit chain, job records and
   the saved draft included — on the synchronous path, on a detached
   composition (#1966) and on a vendor ``401`` that quotes it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
from starlette.testclient import TestClient

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.request_key import RequestKey
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek_mcp import server as server_mod
from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER
from creek_mcp.composition_jobs import SYNC_DEADLINE_ENV
from creek_mcp.httpapi import mcp_request_key
from creek_mcp.policy import Transport
from creek_mcp.provenance import InferenceProvenance
from tests.mcp_http_support import call_tool, open_session
from tests.test_request_scoped_byok import (
    CANARY_KEY,
    CANARY_TAIL,
    ENTRY,
    HTTP_UNAUTHORIZED,
    OPENAI_CANARY,
    OPERATOR_KEY,
    QUOTE,
    _fragment,
    _swept,
    _Vendor,
    _write_config,
)
from tests.v1_api_support import build_app, seed_vault

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx

_DRAFTS: Final[tuple[str, str]] = ("07-Voice", "Drafts")
_INVALID: Final[str] = ERROR_MESSAGES[ErrorCode.INVALID_REQUEST]
_KEYLESS_REFUSAL: Final[str] = "LLM provider unavailable for draft"
_WAIT_SECONDS: Final[float] = 10.0
_POLL_SECONDS: Final[float] = 0.02
_INTIMATE_SOURCE: Final[str] = "intimate-1967"


def _seed(*sources: str) -> IdeaSeed:
    """Return one mined seed drafted from *sources* (none: ontology-only)."""
    return IdeaSeed(
        strategy=(
            MiningStrategy.RESONANCE_CHAIN
            if sources
            else MiningStrategy.UNEXPLORED_ONTOLOGY
        ),
        title="Unwalked ground",
        source_fragments=sources,
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="An ontology combination not yet inhabited.",
        score=0.5,
    )


def _mine(monkeypatch: pytest.MonkeyPatch, seed: IdeaSeed) -> None:
    """Make the draft tool's miner surface exactly *seed*."""
    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **_kwargs: type(
            "_Miner",
            (),
            {"mine_all": lambda _self, _vault, *, current_phase: [seed]},
        )(),
    )


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A managed vault in container mode with no local model served.

    The operator's own cloud key and consent are in the environment and must
    stay refused, as in ``tests/test_request_scoped_byok.py``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv(SYNC_DEADLINE_ENV, raising=False)
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)
    _mine(monkeypatch, _seed())
    return _write_config(seed_vault(tmp_path / "vault"))


@pytest.fixture
def every_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture every logger at DEBUG, the SDKs' and httpx's included."""
    caplog.set_level("DEBUG")
    for name in ("anthropic", "openai", "httpx", "httpcore", "creek", "creek_mcp"):
        caplog.set_level("DEBUG", logger=name)
    yield caplog


def _keyed(provider: str = "anthropic", key: str = CANARY_KEY) -> dict[str, str]:
    """Return the two ADR-0015 headers."""
    return {PROVIDER_HEADER: provider, PROVIDER_KEY_HEADER: key}


def _call(
    vault: Path,
    name: str,
    arguments: dict[str, Any],
    extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Open an ``/mcp`` session, call *name* once with *extra* headers, return it."""
    with TestClient(build_app(vault_path=vault)) as client:
        session = open_session(client)
        result: dict[str, Any] = call_tool(
            client, {**session["headers"], **(extra or {})}, name, arguments
        )
    return result


def _assert_canary_absent(
    root: Path, logs: pytest.LogCaptureFixture, *results: object
) -> None:
    """Sweep the results, every log record and every file under *root*."""
    for needle in (CANARY_KEY, CANARY_TAIL, OPENAI_CANARY):
        for result in results:
            assert needle not in json.dumps(result)
        assert needle not in logs.text
        for record in logs.records:
            assert needle not in repr(record.args)
        assert _swept(root, needle) == []


def _only_draft(vault: Path) -> frontmatter.Post:
    """Return the one draft saved under ``07-Voice/Drafts/``."""
    (path,) = sorted(vault.joinpath(*_DRAFTS).glob("*.md"))
    return frontmatter.load(path)


def _audit_entries(vault: Path) -> list[dict[str, Any]]:
    """Return every MCP audit entry the vault holds."""
    log = vault / "00-Creek-Meta" / "audit" / "mcp.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines() if line]


# --------------------------------------------------------------------------- #
# It works, and names its route
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "secret"),
    [("anthropic", CANARY_KEY), ("openai", OPENAI_CANARY)],
    ids=["anthropic", "openai"],
)
def test_a_draft_over_mcp_is_written_with_the_writers_key(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
    provider: str,
    secret: str,
) -> None:
    """The epic's driving journey: an agent's draft bought with the writer's key."""
    vendor = _Vendor(monkeypatch)

    result = _call(managed, "creek.draft", {}, _keyed(provider, secret))

    assert result["status"] == "ok", result
    assert result["inference_source"] == f"request_key:{provider}"
    assert _only_draft(managed)["inference_source"] == f"request_key:{provider}"
    assert vendor.requests, "the writer's vendor was never dialled"
    assert set(vendor.keys_sent()) == {secret}
    _assert_canary_absent(tmp_path, every_log, result)


def test_an_authored_draft_over_mcp_uses_the_writers_key(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """``creek.author`` voices through the writer's vendor and says so."""
    vendor = _Vendor(monkeypatch)

    result = _call(
        managed, "creek.author", {"query": "garden"}, _keyed("openai", OPENAI_CANARY)
    )

    assert result["status"] == "ok", result
    assert result["inference_source"] == "request_key:openai"
    assert set(vendor.keys_sent()) == {OPENAI_CANARY}
    _assert_canary_absent(tmp_path, every_log, result)


def test_revoice_over_mcp_uses_the_writers_key_without_saving(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """Revoice inherits the request-key route without persisting either passage."""
    vendor = _Vendor(monkeypatch)
    passage = "Please send the report on Friday."
    result = _call(
        managed, "creek.revoice", {"text": passage}, _keyed("openai", OPENAI_CANARY)
    )
    assert result["status"] == "ok", result
    assert result["inference_source"] == "request_key:openai"
    assert result["text"]
    assert set(vendor.keys_sent()) == {OPENAI_CANARY}
    assert not list(managed.joinpath(*_DRAFTS).glob("*.md"))
    assert _swept(managed, passage) == []
    assert _swept(managed, str(result["text"])) == []
    _assert_canary_absent(tmp_path, every_log, result)


def test_a_reflection_over_mcp_uses_the_writers_key(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """``creek.reflect`` is grounded in the vault and answered by the vendor."""
    vendor = _Vendor(monkeypatch)

    result = _call(managed, "creek.reflect", {"content": ENTRY}, _keyed())

    assert result["status"] == "ok", result
    assert result["notes"][0]["quote"] == QUOTE
    assert result["inference_source"] == "request_key:anthropic"
    assert vendor.keys_sent() == [CANARY_KEY]
    _assert_canary_absent(tmp_path, every_log, result)


class _GatedVendor(_Vendor):
    """A vendor that holds every answer until *gate* is set."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, gate: threading.Event):
        super().__init__(monkeypatch)
        self._gate = gate

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self._gate.wait(_WAIT_SECONDS)
        return super()._answer(request)


def test_a_detached_draft_keeps_the_key_for_its_run_and_persists_none_of_it(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """Past the deadline the worker still drafts with the key; no record holds it."""
    gate = threading.Event()
    vendor = _GatedVendor(monkeypatch, gate)
    monkeypatch.setenv(SYNC_DEADLINE_ENV, "0.05")

    with TestClient(build_app(vault_path=managed)) as client:
        session = open_session(client)
        keyed = {**session["headers"], **_keyed()}
        accepted = call_tool(client, keyed, "creek.draft", {})
        gate.set()
        settled = accepted
        deadline = time.monotonic() + _WAIT_SECONDS
        while settled.get("state") in {"queued", "running"}:
            assert time.monotonic() < deadline, "the detached draft never settled"
            time.sleep(_POLL_SECONDS)
            settled = call_tool(
                client,
                session["headers"],
                "creek.jobs.read",
                {"job_id": accepted["job_id"]},
            )

    assert accepted["status"] == "accepted", accepted
    assert settled["state"] == "succeeded", settled
    assert settled["result"]["inference_source"] == "request_key:anthropic"
    assert _only_draft(managed)["inference_source"] == "request_key:anthropic"
    assert set(vendor.keys_sent()) == {CANARY_KEY}
    _assert_canary_absent(tmp_path, every_log, accepted, settled)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("creek.draft", {}), ("creek.reflect", {"content": ENTRY})],
    ids=["draft", "reflect"],
)
def test_a_vendor_401_quoting_the_key_never_reaches_the_agent_or_the_vault(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    """A rejected key is reduced to the vendor's status before it is reported."""
    _Vendor(monkeypatch, status=HTTP_UNAUTHORIZED)

    result = _call(managed, tool, arguments, _keyed())

    assert result["status"] != "ok", result
    assert "inference_source" not in result
    _assert_canary_absent(tmp_path, every_log, result)


# --------------------------------------------------------------------------- #
# The refusals that stood still stand
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sent",
    [
        {PROVIDER_HEADER: "anthropic"},
        {PROVIDER_KEY_HEADER: CANARY_KEY},
        _keyed("gemini"),
        _keyed("anthropic", "not a key: " + CANARY_KEY),
    ],
    ids=["provider-alone", "key-alone", "unknown-vendor", "not-key-shaped"],
)
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("creek.draft", {}),
        ("creek.author", {"query": "garden"}),
        ("creek.revoice", {"text": "Please send the report on Friday."}),
        ("creek.reflect", {"content": ENTRY}),
    ],
    ids=["draft", "author", "revoice", "reflect"],
)
def test_a_malformed_pair_is_refused_with_the_v1_message_and_never_echoed(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
    sent: dict[str, str],
    tool: str,
    arguments: dict[str, Any],
) -> None:
    """The refusal envelope, ``/v1``'s fixed text, audited content-free."""
    vendor = _Vendor(monkeypatch)

    result = _call(managed, tool, arguments, sent)

    assert result == {
        "status": "refused",
        "tool": tool,
        "tier_ceiling": "open",
        "reason": _INVALID,
    }
    assert vendor.requests == []
    refused = [e for e in _audit_entries(managed) if e["tool"] == tool]
    assert [e.get("outcome") for e in refused] == ["refused"]
    assert refused[0]["args_summary"] == {}
    _assert_canary_absent(tmp_path, every_log, result)
    assert not managed.joinpath(*_DRAFTS).exists() or not list(
        managed.joinpath(*_DRAFTS).glob("*.md")
    )


def test_a_tool_that_calls_no_model_ignores_the_headers(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like ``/v1``'s keyless routes: a malformed pair on ``creek.mine`` is moot."""
    _Vendor(monkeypatch)

    result = _call(managed, "creek.mine", {}, {PROVIDER_HEADER: "anthropic"})

    assert result["status"] == "ok", result


def test_without_a_key_a_managed_draft_is_refused_and_no_vendor_is_dialled(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key, no local model: the pre-ADR-0015 refusal, unchanged."""
    vendor = _Vendor(monkeypatch)

    result = _call(managed, "creek.draft", {})

    assert result["status"] == "refused"
    assert result["reason"].startswith(_KEYLESS_REFUSAL)
    assert "inference_source" not in result
    assert vendor.requests == []


def test_an_agents_bearer_never_becomes_a_provider_key(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``Authorization`` header authenticates to the vault and goes no further.

    The session carries a bearer and no ``X-Creek-*`` header, so the call is
    keyless: the vendor is never dialled, with that bearer or any other.
    """
    vendor = _Vendor(monkeypatch)

    result = _call(managed, "creek.draft", {})

    assert result["status"] == "refused"
    assert vendor.requests == []


def test_a_self_connected_vault_parses_but_never_spends_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the managed marker the key is ignored: the pin refusal stands."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    _mine(monkeypatch, _seed())
    vault = _write_config(seed_vault(tmp_path / "self"))
    vendor = _Vendor(monkeypatch)

    result = _call(vault, "creek.draft", {}, _keyed())

    assert result["status"] == "refused"
    assert result["reason"].startswith(_KEYLESS_REFUSAL)
    assert vendor.requests == []


# --------------------------------------------------------------------------- #
# The tier gate is the router's
# --------------------------------------------------------------------------- #


def test_an_intimate_derived_draft_never_reaches_the_writers_vendor(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """A seed drawn from an intimate fragment is forced local — here, refused."""
    _fragment(
        managed, _INTIMATE_SOURCE, "the thing I have never told anyone", "intimate"
    )
    _mine(monkeypatch, _seed(_INTIMATE_SOURCE))
    vendor = _Vendor(monkeypatch)
    monkeypatch.setattr(
        server_mod,
        "build_request_scoped_provider",
        lambda _key: pytest.fail("the writer's provider was built for INTIMATE"),
    )

    result = _call(
        managed, "creek.draft", {"privacy_tier_ceiling": "personal"}, _keyed()
    )

    assert result["status"] == "refused"
    assert result["reason"].startswith(_KEYLESS_REFUSAL)
    assert vendor.requests == []
    _assert_canary_absent(tmp_path, every_log, result)


# --------------------------------------------------------------------------- #
# The handshake says whether a key would be spent
# --------------------------------------------------------------------------- #


def test_a_managed_handshake_reports_both_request_key_providers(managed: Path) -> None:
    """Mirrors ``GET /v1/capabilities`` on the same vault."""
    result = _call(managed, "creek.handshake", {})

    assert result["request_key_providers"] == ["anthropic", "openai"]


def test_a_self_connected_handshake_reports_no_request_key_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-connected vault ignores keys, so it advertises none."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(MANAGED_VAULT_ENV, raising=False)
    vault = seed_vault(tmp_path / "self")

    result = _call(vault, "creek.handshake", {})

    assert result["request_key_providers"] == []


def test_a_stdio_handshake_reports_no_request_key_providers(
    managed: Path,
) -> None:
    """Stdio carries no headers, so even a managed vault advertises none there."""
    server = server_mod.build_server(transport=Transport.STDIO, vault_path=managed)

    _content, result = asyncio.run(server.call_tool("creek.handshake", {}))

    assert result["request_key_providers"] == []


# --------------------------------------------------------------------------- #
# The extract seam
# --------------------------------------------------------------------------- #


def test_stdio_never_reads_the_request_headers(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design default: only the network transport consults the headers."""
    monkeypatch.setattr(
        mcp_request_key,
        "request_key_in_flight",
        lambda: pytest.fail("a stdio call read the request headers"),
    )
    seen: list[RequestKey | None] = []
    server = server_mod.build_server(
        transport=Transport.STDIO,
        vault_path=managed,
        draft_llm_factory=lambda _tier: (
            seen.append(mcp_request_key.bound_request_key()) or (lambda _prompt: "body")
        ),
    )

    asyncio.run(server.call_tool("creek.draft", {}))

    assert seen == [None]


def test_outside_a_request_there_is_no_http_request_and_no_key() -> None:
    """An in-process call has no transport request, so it carries no key."""
    assert mcp_request_key.current_http_request() is None
    assert mcp_request_key.request_key_in_flight() is None
    assert mcp_request_key.bound_request_key() is None


def test_the_binding_lasts_exactly_one_call() -> None:
    """The bound key is visible inside the block and gone after it."""
    key = RequestKey("anthropic", CANARY_KEY)

    with mcp_request_key.bound_for_call(key):
        assert mcp_request_key.bound_request_key() is key
    assert mcp_request_key.bound_request_key() is None


def test_a_keyless_route_is_still_labelled_by_the_draft_tool(
    managed: Path,
) -> None:
    """The draft tool stamps whatever route its factory recorded."""
    from creek_mcp.tools.draft import draft_tool

    provenance = InferenceProvenance()

    def _factory(_tier: object) -> Any:
        provenance.record("local")
        return lambda _prompt: "A drafted body."

    result = draft_tool(vault_path=managed, llm_factory=_factory, provenance=provenance)

    assert result["inference_source"] == "local"
    assert _only_draft(managed)["inference_source"] == "local"


def test_an_unlabelled_route_leaves_the_draft_unstamped(managed: Path) -> None:
    """No recorded source: no key on the answer, none in the frontmatter."""
    from creek_mcp.tools.draft import draft_tool

    result = draft_tool(
        vault_path=managed,
        llm_factory=lambda _tier: lambda _prompt: "A drafted body.",
        provenance=InferenceProvenance(),
    )

    assert "inference_source" not in result
    assert "inference_source" not in _only_draft(managed).metadata
