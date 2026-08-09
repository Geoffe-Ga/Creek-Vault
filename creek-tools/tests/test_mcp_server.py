"""Bootstrap + registration tests for the creek-tools MCP server (FEAT-010)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek_mcp.auth import ELEVATED_TOKEN_ENV
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    ConsumerTokenVerifier,
    load_consumer_tokens,
)
from creek_mcp.server import SERVER_NAME, build_server

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


# Low-entropy test literals, not real credentials.
_WEAK_ELEVATED_TOKEN = "weak-" + "a" * 6  # 11 chars, under the 32-char floor
_BOUNDARY_ELEVATED_TOKEN = "a" * 32  # exactly on the floor
_STRONG_CONSUMER_TOKEN = "server-test-consumer-" + "b" * 22  # 43 chars


EXPECTED_TOOLS = {
    "creek.handshake",
    "creek.reflect",
    "creek.wheel",
    "creek.journal",
    "creek.upload",
    "creek.state.read",
    "creek.state.render",
    "creek.lint",
    "creek.mine",
    "creek.draft",
    "creek.author",
    "creek.save",
    "creek.ingest",
    "creek.redact.scan",
    "creek.classify",
    "creek.link",
    "creek.report",
    "creek.skills.refresh",
    "creek.compile",
    "creek.purge.fragment",
    "creek.purge.source",
    "creek.purge.classifications",
    "creek.purge.daterange",
    "creek.purge.vault",
}


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the server tests."""
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/audit",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "creek-skills",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _structured(result: object) -> dict[str, object]:
    """Pull the structured-content dict out of a FastMCP ``call_tool`` result."""
    return result[1] if isinstance(result, tuple) else result  # type: ignore[return-value, index]


def test_build_server_returns_fastmcp_instance(vault: Path) -> None:
    """The bootstrap returns a configured :class:`FastMCP` instance."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    assert server.name == SERVER_NAME


def test_build_server_registers_all_tools(vault: Path) -> None:
    """All FEAT-010 read + FEAT-011 write tools surface via ``list_tools``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS


def test_call_tool_handshake_reflects_registered_tools(vault: Path) -> None:
    """End-to-end: ``creek.handshake`` negotiates versions + the live tool list.

    Its ``capabilities`` must equal the names of the tools actually registered
    (not a hardcoded list), proving the #750 acceptance criterion through the
    registered async handler rather than only the unit-level helper.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    result = asyncio.run(
        server.call_tool("creek.handshake", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["available"] is True
    assert structured["contract_version"] == CONTRACT_VERSION
    assert structured["ontology_version"] == ONTOLOGY_VERSION
    assert structured["tiers"] == ["open", "personal", "intimate"]
    assert set(structured["capabilities"]) == registered  # type: ignore[arg-type]
    for expected in ("creek.handshake", "creek.ingest", "creek.classify"):
        assert expected in structured["capabilities"]  # type: ignore[operator]


def test_call_tool_reflect_returns_verbatim_notes(vault: Path) -> None:
    """End-to-end: ``creek.reflect`` returns verbatim-quoted notes (injected factory).

    The tier-keyed LLM factory is injected (no live provider); the registered
    tool wires it through ``reflect_tool``, whose verbatim-quote guard keeps only
    spans copied from the entry.
    """
    entry = "I rest sometimes and the work survives."
    note = '{"quote": "I rest sometimes", "kind": "reframe", "note": "Yours."}'
    payload = '{"notes": [' + note + "]}"
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
        reflect_llm_factory=lambda: lambda tier: lambda prompt: payload,
    )
    result = asyncio.run(
        server.call_tool(
            "creek.reflect",
            {"content": entry, "privacy_tier_ceiling": "open"},
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.reflect"
    assert structured["notes"][0]["quote"] == "I rest sometimes"  # type: ignore[index]


def test_call_tool_reflect_escalates_on_acute_distress(vault: Path) -> None:
    """The registered ``creek.reflect`` wires the real care guard (#753).

    Acute-distress content must escalate with the structured care signal and
    never reach the (injected) LLM — proving ``server.py`` threads
    ``acute_distress_guard`` into ``reflect_tool``.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
        reflect_llm_factory=lambda: lambda tier: lambda prompt: '{"notes": []}',
    )
    result = asyncio.run(
        server.call_tool(
            "creek.reflect",
            {
                "content": "I am going to kill myself tonight.",
                "privacy_tier_ceiling": "open",
            },
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "escalate"
    assert structured["care_signal"]["kind"] == "acute_distress"  # type: ignore[index]


def test_call_tool_journal_ingests_entry_idempotently(vault: Path) -> None:
    """End-to-end: ``creek.journal`` ingests an entry as a fragment, idempotently.

    Re-sending the same external id through the registered tool yields the same
    fragment id and no duplicate — proving the ledger-backed path is wired.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    payload = {
        "content": "A registered journal entry.",
        "external_id": "adep-srv-1",
        "timestamp": "2026-06-20T10:00:00+00:00",
        "privacy_tier_ceiling": "personal",
    }
    first = _structured(
        asyncio.run(server.call_tool("creek.journal", payload)),
    )
    second = _structured(
        asyncio.run(server.call_tool("creek.journal", payload)),
    )
    assert first["status"] == "ok"
    assert first["tool"] == "creek.journal"
    assert second["fragment_id"] == first["fragment_id"]
    assert len(sorted((vault / "01-Fragments").rglob("*.md"))) == 1


def test_upload_is_registered_and_advertised_as_a_capability(vault: Path) -> None:
    """``creek.upload`` reaches the handshake through registration alone (#1023).

    Worth asserting separately from the set-equality above because the two
    statements are made by different code. ``handshake_tool`` never holds a
    tool list of its own — ``build_server`` derives ``capabilities`` from
    ``server.list_tools()`` — so a name that appears there appears *because*
    the closure was registered. Hardcoding ``creek.upload`` into the handshake
    would satisfy this test while the tool itself was missing, which is exactly
    the regression the derivation exists to prevent.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "creek.upload" in names
    structured = _structured(
        asyncio.run(
            server.call_tool("creek.handshake", {"privacy_tier_ceiling": "open"}),
        ),
    )
    capabilities = structured["capabilities"]
    assert isinstance(capabilities, list)
    assert "creek.upload" in capabilities
    assert structured["contract_version"] == CONTRACT_VERSION


def test_upload_over_the_server_surface_ingests_bytes(vault: Path) -> None:
    """End-to-end: ``creek.upload`` stages bytes and ingests them, idempotently.

    Driven through ``call_tool`` rather than through ``upload_tool`` because
    the registered closure is the only path a remote consumer has: it is what
    binds the vault, resolves the per-call consumer, and decides which
    arguments the tool even accepts. ``tests/test_mcp_upload.py`` owns the
    tool's behaviour; what is proved here is that the wiring reaches it.

    The second identical send is the wiring's own idempotency claim: it can
    only answer ``unchanged`` if the ledger-backed pipeline — not a bespoke
    per-call write — is what the closure hands the bytes to.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    payload = {
        "filename": "notes.txt",
        "content_base64": base64.b64encode(
            b"An uploaded note that arrived as bytes.",
        ).decode("ascii"),
        "external_id": "srv-1",
        "timestamp": "2026-06-20T10:00:00+00:00",
        "tier": "personal",
        "privacy_tier_ceiling": "personal",
    }
    first = _structured(asyncio.run(server.call_tool("creek.upload", payload)))
    second = _structured(asyncio.run(server.call_tool("creek.upload", payload)))
    assert first["status"] == "ok"
    assert first["tool"] == "creek.upload"
    assert first["source_type"] == "document"
    assert first["fragment_id"]
    assert second["action"] == "unchanged"
    assert second["fragment_id"] == first["fragment_id"]


def test_call_tool_wheel_returns_complete_frequency_balance(vault: Path) -> None:
    """End-to-end: ``creek.wheel`` returns a complete F1-F10 balance map.

    Even with an empty corpus the wheel is present and all-zero (the new-user
    case), proving the read-only aggregation is reachable through the registry.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool("creek.wheel", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.wheel"
    assert list(structured["wheel"].keys()) == [f"F{n}" for n in range(1, 11)]  # type: ignore[union-attr]
    assert structured["total_classified"] == 0


def test_call_tool_save_through_mcp(vault: Path) -> None:
    """End-to-end: ``call_tool("creek.save")`` writes the note and returns path."""
    for relparts in (
        ("02-Threads", "Active"),
        ("00-Creek-Meta", "audit"),
    ):
        (vault.joinpath(*relparts)).mkdir(parents=True, exist_ok=True)
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.save",
            {
                "target": "thread",
                "body": "Note worth keeping.",
                "title": "Saved thread",
                "tier": "open",
                "privacy_tier_ceiling": "open",
            },
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.save"


def test_every_tool_requires_privacy_tier_ceiling_parameter(vault: Path) -> None:
    """The FEAT-010 acceptance criterion: ceiling is in every tool's schema.

    FEAT-012 carve-out: ``creek.purge.*`` tools don't read vault
    content — they take an elevated ``auth_token`` instead — so the
    tier-ceiling invariant doesn't apply to them.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    for tool in tools:
        if tool.name.startswith("creek.purge."):
            continue
        schema = tool.inputSchema
        assert "privacy_tier_ceiling" in schema["properties"], (
            f"{tool.name} missing privacy_tier_ceiling in its input schema"
        )


def test_purge_tools_require_auth_token_parameter(vault: Path) -> None:
    """FEAT-012: every ``creek.purge.*`` tool exposes an ``auth_token`` slot."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    purge_tools = [t for t in tools if t.name.startswith("creek.purge.")]
    assert len(purge_tools) == 5
    for tool in purge_tools:
        schema = tool.inputSchema
        assert "auth_token" in schema["properties"], (
            f"{tool.name} missing auth_token in its input schema"
        )


def test_call_tool_state_read_through_mcp(vault: Path) -> None:
    """End-to-end: ``call_tool("creek.state.read")`` returns the report bytes.

    The fixture carries the ``privacy_tier: open`` stamp that
    ``StateReportGenerator.write`` writes since #969. Only the *fixture*
    changed: every assertion below is the one this test has always made. An
    unstamped report now reads as ``intimate`` — ``raw_privacy_tier`` fails
    closed on a missing key, which is an accurate statement about bytes that
    were rendered with no ceiling at all — so leaving the fixture unstamped
    would have quietly turned this end-to-end read-path test into a second copy
    of the refusal test below.
    """
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "---\ntype: state-report\nprivacy_tier: open\ntier_ceiling: open\n---\n\n"
        "# Audit\n\nhello\n",
        encoding="utf-8",
    )
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool("creek.state.read", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.state.read"
    assert "Audit" in structured["content"]  # type: ignore[operator]


def test_call_tool_state_read_refuses_a_legacy_report_through_mcp(vault: Path) -> None:
    """The #969 fail-closed path is reachable end to end, not just in-process.

    Added alongside the test above rather than replacing its coverage: the
    stamped-``open`` fixture there only means something if an *unstamped* report
    is not also served. Pinning both halves on the real ``call_tool`` boundary
    is what proves the refusal survives MCP's response serialisation — a
    refusal that only exists inside ``state_read_tool`` would be no gate at all
    if the transport layer re-shaped it.
    """
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit\n\nhello\n",
        encoding="utf-8",
    )
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool("creek.state.read", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "refused"
    assert "hello" not in json.dumps(structured, default=str)


def test_call_tool_state_render_through_mcp(vault: Path) -> None:
    """The render path is reachable via ``call_tool``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.state.render",
            {"privacy_tier_ceiling": "open"},
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"


def test_call_tool_lint_through_mcp(vault: Path) -> None:
    """The lint path is reachable via ``call_tool`` and returns ``checks``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool("creek.lint", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert "checks" in structured


def test_call_tool_mine_through_mcp(vault: Path) -> None:
    """The mine path is reachable via ``call_tool``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.mine",
            {"privacy_tier_ceiling": "open", "phase": "rising", "limit": 3},
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"


def test_call_tool_draft_through_mcp(vault: Path) -> None:
    """The draft path is reachable via ``call_tool``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.draft",
            {"privacy_tier_ceiling": "open", "phase": "rising"},
        ),
    )
    structured = _structured(result)
    # Empty vault → no seeds; tool returns structured ``empty``.
    assert structured["status"] == "empty"


def test_build_server_falls_back_to_load_config(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit ``vault_path``, the bootstrap reads ``load_config``."""

    class _StubConfig:
        vault_path = vault

    monkeypatch.setattr("creek_mcp.server.load_config", lambda: _StubConfig())
    server = build_server(draft_llm_factory=lambda tier: lambda prompt: "x")
    assert server.name == SERVER_NAME


# The two ``_build_draft_llm`` unit tests that used to sit here monkeypatched
# ``creek.classify.llm.LLMClassifier`` with a stub accepting any config, then
# called the untiered ``_build_draft_llm()``. That stub is what hid #958: the
# real ``LLMClassifier`` reads ``.provider`` off the ``LLMRoutingConfig`` it is
# handed and raises ``AttributeError`` on every production call, and the
# router's Intimate-never-cloud gate was never in the path at all. Both
# properties they claimed to pin ("returns a usable callable when the provider
# is available" / "raises a clear RuntimeError when it is not") are asserted
# against the *routed* factory by
# ``test_build_draft_llm_routes_by_tier_then_degrades`` below.


def test_main_invokes_server_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` calls ``FastMCP.run(transport='stdio')``."""
    from creek_mcp import server as server_module

    runs: list[tuple[object, ...]] = []

    class _StubServer:
        def run(self, transport: str) -> None:
            runs.append((transport,))

    monkeypatch.setattr(server_module, "build_server", lambda: _StubServer())
    server_module.main([])
    assert runs == [("stdio",)]


def test_main_config_flag_sets_env_var_before_build_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--config <path>`` sets ``CREEK_CONFIG`` before tools register (INC-008)."""
    from creek.config import CONFIG_PATH_ENV_VAR
    from creek_mcp import server as server_module

    config_file = tmp_path / "vault-config.yaml"
    config_file.write_text("timezone: UTC\n")
    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)

    captured_env: dict[str, str | None] = {}

    class _StubServer:
        def run(self, transport: str) -> None:
            del transport

    def _capture_build() -> _StubServer:
        captured_env[CONFIG_PATH_ENV_VAR] = os.environ.get(CONFIG_PATH_ENV_VAR)
        return _StubServer()

    monkeypatch.setattr(server_module, "build_server", _capture_build)
    server_module.main(["--config", str(config_file)])

    assert captured_env[CONFIG_PATH_ENV_VAR] == str(config_file.resolve())


def test_main_config_flag_missing_file_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--config`` pointing at a missing file exits nonzero with a clear message."""
    from creek_mcp import server as server_module

    missing = tmp_path / "no-such-config.yaml"

    # build_server must not be called when --config is invalid.
    def _explode() -> object:  # pragma: no cover - asserts non-invocation
        msg = "build_server should not run when --config is missing"
        raise AssertionError(msg)

    monkeypatch.setattr(server_module, "build_server", _explode)

    with pytest.raises(SystemExit) as exc_info:
        server_module.main(["--config", str(missing)])

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "file not found" in err.lower()


def test_main_without_config_flag_leaves_env_var_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--config`` does not modify ``CREEK_CONFIG``."""
    from creek.config import CONFIG_PATH_ENV_VAR
    from creek_mcp import server as server_module

    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)

    class _StubServer:
        def run(self, transport: str) -> None:
            del transport

    monkeypatch.setattr(server_module, "build_server", lambda: _StubServer())
    server_module.main([])

    assert CONFIG_PATH_ENV_VAR not in os.environ


def test_build_reflect_llm_factory_routes_then_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reflect factory returns a working callable, and raises when no provider.

    Exercises the production INTIMATE-routing seam without a live model: the
    router + provider are stubbed so both the available path (a usable callable)
    and the unavailable path (a clean ``RuntimeError`` the tool turns into a
    refusal) are covered.
    """
    from creek.models import PrivacyTier
    from creek_mcp import server as server_mod

    class _Cfg:
        provider = "ollama"
        model = "m"

    class _Router:
        def resolve(self, stage: str, tier: object) -> _Cfg:
            return _Cfg()

    class _Config:
        model_router = _Router()

    monkeypatch.setattr(server_mod, "load_config", lambda: _Config())

    class _Completion:
        text = "reflected"

    class _Provider:
        available = True

        def complete(self, prompt: str) -> _Completion:
            return _Completion()

    monkeypatch.setattr(
        "creek.classify.llm.providers.build_provider",
        lambda cfg: _Provider(),
    )
    factory = server_mod._build_reflect_llm_factory()
    assert factory(PrivacyTier.OPEN)("p") == "reflected"

    class _Unavailable:
        available = False

    monkeypatch.setattr(
        "creek.classify.llm.providers.build_provider",
        lambda cfg: _Unavailable(),
    )
    with pytest.raises(RuntimeError):
        server_mod._build_reflect_llm_factory()(PrivacyTier.OPEN)


# --------------------------------------------------------------------------- #
# Compile LLM: tier-routed production factory (#928) + real wiring (#929)
# --------------------------------------------------------------------------- #


class _CompletionStub:
    """A provider completion exposing only the ``.text`` field callers read."""

    def __init__(self, text: str) -> None:
        """Store the completion *text*."""
        self.text = text


class _ProviderSpy:
    """A ``build_provider`` stand-in recording the configs it was handed.

    Doubles as the provider it returns so one object covers construction
    (which resolved config the router produced) and use (which prompts were
    completed). ``available`` mirrors the real provider flag so the
    degraded path can be exercised without a live backend.
    """

    def __init__(self, *, available: bool = True, text: str = "{}") -> None:
        """Start with empty recordings, the given availability and response."""
        self.configs: list[object] = []
        self.prompts: list[str] = []
        self.available = available
        self.text = text

    def build(self, config: object) -> _ProviderSpy:
        """Record *config* and return this spy as the constructed provider."""
        self.configs.append(config)
        return self

    def complete(self, prompt: str) -> _CompletionStub:
        """Record *prompt* and return the canned completion."""
        self.prompts.append(prompt)
        return _CompletionStub(self.text)

    @property
    def provider_names(self) -> list[object]:
        """Return the ``provider`` of every recorded config, in call order."""
        return [getattr(config, "provider", None) for config in self.configs]


def _patch_build_provider(monkeypatch: pytest.MonkeyPatch, spy: _ProviderSpy) -> None:
    """Route every ``build_provider`` import path through *spy*.

    The factory is reachable as ``creek.classify.llm.providers.build_provider``
    (the router/reflect import) and via the ``creek.classify.llm`` re-export
    (what :func:`creek.compile.engine.default_llm` imports), so both names are
    patched and the spy records whichever path production actually takes.
    ``creek.author.client`` binds a third copy at import time, which is the one
    the Writing Desk's voice factory calls.
    """
    monkeypatch.setattr("creek.classify.llm.providers.build_provider", spy.build)
    monkeypatch.setattr("creek.classify.llm.build_provider", spy.build)
    monkeypatch.setattr("creek.author.client.build_provider", spy.build)


def _split_routing_config() -> object:
    """Return a config stub whose ``default`` is local and ``generation`` cloud.

    The two stages deliberately disagree so a resolved ``ollama`` proves the
    ``Intimate``-never-cloud redirect ran, while a resolved ``anthropic``
    proves the ``generation`` stage (not the ``default``) was consulted.
    """
    from creek.classify.llm.router import ModelRouter
    from creek.config import AuthorConfig, LLMConfig, LLMRoutingConfig

    routing = LLMRoutingConfig(
        default=LLMConfig(provider="ollama"),
        generation=LLMConfig(provider="anthropic"),
    )

    class _Config:
        """Minimal ``CreekConfig`` stand-in exposing the LLM routing surface."""

        llm = routing
        model_router = ModelRouter(routing)
        # ``_build_author_llm`` reads the author block for the legacy
        # ``voice_model`` fallback; the defaults leave the routing above in
        # charge, which is what these assertions are about.
        author = AuthorConfig()

    return _Config()


def _write_open_fragment(vault: Path, frag_id: str) -> None:
    """Write one ``open``-tier fragment under ``01-Fragments/Notes``."""
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": f"Title {frag_id}",
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": "open",
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="Body text.", **metadata)),
        encoding="utf-8",
    )


def test_build_compile_llm_routes_by_tier_then_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compile factory is tier-keyed and honours the router (#928/#929).

    Mirrors :func:`test_build_reflect_llm_factory_routes_then_degrades`, but
    against a *real* :class:`~creek.classify.llm.router.ModelRouter` so the
    ``Intimate``-never-cloud redirect under test is the production one and
    not a stub's opinion.

    Three properties, each of which the pre-fix ``_build_compile_llm``
    violates: it takes no tier at all, it builds straight from
    ``load_config().llm`` (never touching the router), and it imports a name
    (``creek.compile.engine._default_llm``) that does not exist — so every
    production ``creek.compile`` call raises ``ImportError``.
    """
    from creek.models import PrivacyTier
    from creek_mcp import server as server_mod

    monkeypatch.setattr(server_mod, "load_config", _split_routing_config)
    spy = _ProviderSpy(text="compiled")
    _patch_build_provider(monkeypatch, spy)

    # INTIMATE: the cloud ``generation`` stage is redirected to local default.
    assert server_mod._build_compile_llm(PrivacyTier.INTIMATE)("p") == "compiled"
    assert spy.provider_names == ["ollama"]

    # OPEN: no redirect, and the ``generation`` stage (not ``default``) wins.
    assert server_mod._build_compile_llm(PrivacyTier.OPEN)("p") == "compiled"
    assert spy.provider_names == ["ollama", "anthropic"]
    assert spy.prompts == ["p", "p"]

    unavailable = _ProviderSpy(available=False)
    _patch_build_provider(monkeypatch, unavailable)
    with pytest.raises(RuntimeError) as excinfo:
        server_mod._build_compile_llm(PrivacyTier.OPEN)
    assert "unavailable" in str(excinfo.value).lower()


def test_build_author_llm_routes_the_voice_role_through_the_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production author factory obeys the same chokepoint as its siblings.

    ``_build_author_llm`` is the seam #1254 extracted out of ``author_tool`` so
    ``build_server`` could be handed a different one. Extracting it must not
    quietly change what production does, so the same two-arm assertion the
    draft and compile factories carry is made here: an ``Intimate`` run is
    redirected onto the local ``default`` model, and an ``Open`` run resolves
    the cloud ``generation`` stage.

    The third arm has no sibling. Where draft and compile raise on an
    unavailable provider, the desk *degrades*: the factory answers ``None`` and
    the voice node renders deterministically. That is the fallback #460/#649/
    #658 hid behind, so it is asserted rather than assumed.
    """
    from creek.models import PrivacyTier
    from creek_mcp import server as server_mod

    monkeypatch.setattr(server_mod, "load_config", _split_routing_config)
    spy = _ProviderSpy()
    _patch_build_provider(monkeypatch, spy)

    assert server_mod._build_author_llm(PrivacyTier.INTIMATE) is not None
    assert spy.provider_names == ["ollama"]

    assert server_mod._build_author_llm(PrivacyTier.OPEN) is not None
    assert spy.provider_names == ["ollama", "anthropic"]

    _patch_build_provider(monkeypatch, _ProviderSpy(available=False))
    assert server_mod._build_author_llm(None) is None


def test_build_server_wires_the_production_author_llm_factory(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.author`` reaches the production factory with none injected (#1254).

    #958's lesson applied to the seam that closes #1254: every other test of
    this verb now injects ``author_llm_factory``, and an injected factory is
    exactly what lets a production builder rot while the suite stays green. So
    the injection point is deliberately left empty and the recording stand-in
    is installed one level down, on the module attribute ``build_server``
    resolves — proving the fallback is wired, not merely written.

    The stand-in answers ``None`` (the "no provider" reply), because what is
    under test is that the factory is *reached*; what it resolves to is the
    subject of the router test above.
    """
    from creek_mcp import server as server_mod

    reached: list[object] = []

    def _recording(tier: object) -> None:
        """Record the tier the desk asked for and decline to voice."""
        reached.append(tier)
        return None

    monkeypatch.setattr(server_mod, "_build_author_llm", _recording)

    server = build_server(vault_path=vault)
    result = _structured(asyncio.run(server.call_tool("creek.author", {"query": "q"})))

    assert result["status"] == "ok", result
    assert reached, (
        "creek.author never called the production voice factory — the verb is "
        "wired to whatever build_server was handed and nothing else."
    )


def test_build_server_wires_the_production_compile_llm_factory(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.compile`` works with no ``compile_llm_factory`` injected (#929).

    This is the test that would have caught #929. Every existing compile
    test injects ``compile_llm_factory``, which is exactly what hid a
    production factory that raises ``ImportError`` on its first line — the
    registered tool was never once exercised against the code path real
    clients take.

    So the injection point is deliberately left empty: ``build_server`` must
    fall back to its own factory, and the tool must complete end to end
    through the registered FastMCP handler.
    """
    from creek_mcp import server as server_mod

    _write_open_fragment(vault, "frag-open")
    monkeypatch.setattr(server_mod, "load_config", _split_routing_config)
    spy = _ProviderSpy()
    _patch_build_provider(monkeypatch, spy)

    server = build_server(vault_path=vault)
    result = asyncio.run(
        server.call_tool(
            "creek.compile",
            {
                "fragment_ids": ["frag-open"],
                "target_kind": "thread",
                "target_id": "thread-x",
                "target_title": "Thread X",
                "privacy_tier_ceiling": "open",
            },
        ),
    )

    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["compiled_path"] == "02-Threads/Active/thread-x.md"
    assert (vault / "02-Threads" / "Active" / "thread-x.md").exists()
    # An ``open`` ceiling over ``open`` sources routes to the cloud
    # ``generation`` stage — the router ran, and did not over-restrict.
    assert spy.provider_names == ["anthropic"]


# --------------------------------------------------------------------------- #
# Draft LLM: tier-routed production factory + real wiring (#958)
# --------------------------------------------------------------------------- #


def _open_source_seed(frag_id: str) -> object:
    """Return an ``IdeaSeed`` whose single source fragment is *frag_id*."""
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="Wired seed",
        source_fragments=(frag_id,),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="brief",
        score=0.5,
    )


def _patch_draft_miner(monkeypatch: pytest.MonkeyPatch, seed: object) -> None:
    """Force ``draft_tool``'s ``IdeaMiner`` to surface exactly *seed*.

    A bare fixture vault legitimately mines zero seeds, which would let
    ``creek.draft`` answer ``status="empty"`` before the LLM factory is ever
    consulted — and a provider assertion would then pass vacuously.
    """
    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **kwargs: type(
            "_Miner",
            (),
            {"mine_all": lambda self, vault_path, *, current_phase: [seed]},
        )(),
    )


def test_build_draft_llm_routes_by_tier_then_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft factory is tier-keyed and honours the router (#958).

    Mirrors :func:`test_build_compile_llm_routes_by_tier_then_degrades`
    against a *real* :class:`~creek.classify.llm.router.ModelRouter`, so the
    ``Intimate``-never-cloud redirect under test is the production one and
    not a stub's opinion.

    Three properties, each of which the pre-fix ``_build_draft_llm``
    violates: it takes no tier at all, it builds straight from
    ``load_config().llm`` (never touching the router or ``build_provider``),
    and it hands that ``LLMRoutingConfig`` to ``LLMClassifier``, which reads
    ``.provider`` off it — so every production ``creek.draft`` call raises
    ``AttributeError: 'LLMRoutingConfig' object has no attribute 'provider'``
    and the tool is dead by accident rather than routed by design.
    """
    from creek.models import PrivacyTier
    from creek_mcp import server as server_mod

    monkeypatch.setattr(server_mod, "load_config", _split_routing_config)
    spy = _ProviderSpy(text="drafted")
    _patch_build_provider(monkeypatch, spy)

    # INTIMATE: the cloud ``generation`` stage is redirected to local default.
    assert server_mod._build_draft_llm(PrivacyTier.INTIMATE)("p") == "drafted"
    assert spy.provider_names == ["ollama"]

    # OPEN: no redirect, and the ``generation`` stage (not ``default``) wins.
    assert server_mod._build_draft_llm(PrivacyTier.OPEN)("p") == "drafted"
    assert spy.provider_names == ["ollama", "anthropic"]
    assert spy.prompts == ["p", "p"]

    unavailable = _ProviderSpy(available=False)
    _patch_build_provider(monkeypatch, unavailable)
    with pytest.raises(RuntimeError) as excinfo:
        server_mod._build_draft_llm(PrivacyTier.OPEN)
    assert "unavailable" in str(excinfo.value).lower()


def test_build_server_wires_the_production_draft_llm_factory(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.draft`` works with no ``draft_llm_factory`` injected (#958).

    This is the test that would have caught #958. Every other server test
    injects ``draft_llm_factory``, which is exactly what hid a production
    factory that raises ``AttributeError`` on its ``LLMClassifier(config.llm)``
    line — the registered tool was never once exercised against the code path
    real clients take. So the injection point is deliberately left empty:
    ``build_server`` must fall back to its own factory, that factory must go
    through :class:`~creek.classify.llm.router.ModelRouter`, and the tool must
    complete end to end through the registered FastMCP handler.

    The miner and the generator are stubbed so the call is *guaranteed* to
    reach the factory. Without that, a bare fixture vault answers
    ``status="empty"`` (no seeds) or a ``refused`` envelope from the generator
    scaffolding, the factory is never invoked, and every provider assertion
    below would hold vacuously while the production path stayed dead.
    """
    from creek.generate.drafts import Draft
    from creek_mcp import server as server_mod

    _write_open_fragment(vault, "frag-open")
    _patch_draft_miner(monkeypatch, _open_source_seed("frag-open"))
    monkeypatch.setattr(server_mod, "load_config", _split_routing_config)
    spy = _ProviderSpy(text="drafted body")
    _patch_build_provider(monkeypatch, spy)

    recorded: list[Callable[[str], str]] = []

    class _RecordingGenerator:
        """A ``DraftGenerator`` stand-in recording the llm it was built with."""

        def __init__(self, *, llm: Callable[[str], str], **kwargs: object) -> None:
            """Record *llm*; the remaining generator options are irrelevant."""
            del kwargs
            self._llm = llm
            recorded.append(llm)

        def generate_draft(self, idea: object, *, vault_path: Path) -> Draft:
            """Call the recorded llm once and return a minimal draft."""
            del idea, vault_path
            self._llm("draft prompt")
            return Draft(
                title="Wired seed",
                body="drafted body",
                idea_strategy="thread_terminus",
                source_fragments=("frag-open",),
                threads=(),
                eddies=(),
                skill_stack=(),
                prompt="draft prompt",
                generated_date=datetime(2026, 5, 11, tzinfo=UTC),
            )

        def save_draft(self, draft: Draft, vault_path: Path) -> Path:
            """Persist a placeholder file so the tool can report its path."""
            del draft
            target = vault_path / "07-Voice" / "Drafts" / "2026-05-11-wired.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("body", encoding="utf-8")
            return target

    monkeypatch.setattr("creek_mcp.tools.draft.DraftGenerator", _RecordingGenerator)

    server = build_server(vault_path=vault)
    result = asyncio.run(
        server.call_tool(
            "creek.draft",
            {"privacy_tier_ceiling": "open", "phase": "unclassified", "index": 0},
        ),
    )

    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["draft_path"] == "07-Voice/Drafts/2026-05-11-wired.md"
    # An ``open`` ceiling over ``open`` sources routes to the cloud
    # ``generation`` stage — the router ran, and did not over-restrict.
    assert spy.provider_names == ["anthropic"]
    # ...and the callable the generator was handed is the routed provider's,
    # not some other object that merely happens to be callable.
    assert spy.prompts == ["draft prompt"]
    assert len(recorded) == 1
    assert recorded[0]("second prompt") == "drafted body"
    assert spy.prompts == ["draft prompt", "second prompt"]


# --------------------------------------------------------------------------- #
# Elevated-token minimum-length floor at startup (#907)
# --------------------------------------------------------------------------- #


def test_main_rejects_short_elevated_token_on_stdio(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A sub-minimum elevated token aborts stdio startup before any tool exists.

    ``CREEK_MCP_ELEVATED_TOKEN`` guards irreversible ``creek.purge.*``
    calls, so a guessable value must be a loud startup failure — not a
    quietly weak gate. Stubbing ``build_server`` with an exploding
    callable proves the refusal happens *before* the purge tools are
    ever registered or reachable.
    """
    from creek_mcp import server as server_module

    monkeypatch.setenv(ELEVATED_TOKEN_ENV, _WEAK_ELEVATED_TOKEN)
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)

    def _explode() -> object:  # pragma: no cover - asserts non-invocation
        msg = "build_server must not run with a weak elevated token"
        raise AssertionError(msg)

    monkeypatch.setattr(server_module, "build_server", _explode)

    with pytest.raises(SystemExit) as excinfo:
        server_module.main([])

    assert excinfo.value.code == 2  # argparse parser.error convention
    err = capsys.readouterr().err
    assert ELEVATED_TOKEN_ENV in err  # the offending setting is named
    assert "11" in err  # the observed length
    assert "32" in err  # the enforced minimum
    assert "secrets.token_urlsafe(32)" in err  # the rotation recipe
    assert _WEAK_ELEVATED_TOKEN not in err  # NEVER echo the token value


def test_main_rejects_short_elevated_token_on_network_transport(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The elevated-token check runs before the transport branch, so network too.

    Consumer tokens are valid here; only the elevated token is weak. The
    *elevated* message must be what surfaces, proving the check precedes
    the network branch rather than riding along inside it.
    """
    from creek_mcp import server as server_module

    monkeypatch.setenv(ELEVATED_TOKEN_ENV, _WEAK_ELEVATED_TOKEN)
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={_STRONG_CONSUMER_TOKEN}")

    def _explode(**_kwargs: object) -> object:  # pragma: no cover - non-invocation
        msg = "build_server must not run with a weak elevated token"
        raise AssertionError(msg)

    def _no_serve(server: object, args: object) -> None:  # pragma: no cover
        msg = "_serve_network must not run with a weak elevated token"
        raise AssertionError(msg)

    monkeypatch.setattr(server_module, "build_server", _explode)
    monkeypatch.setattr(server_module, "_serve_network", _no_serve)

    with pytest.raises(SystemExit) as excinfo:
        server_module.main(["--transport", "network", "--host", "127.0.0.1"])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert ELEVATED_TOKEN_ENV in err  # the elevated complaint, not the consumer one
    assert "adepthood" not in err  # the consumer token is fine; not the subject
    assert "32" in err  # the enforced minimum
    assert "secrets.token_urlsafe(32)" in err  # the rotation recipe
    assert _WEAK_ELEVATED_TOKEN not in err  # NEVER echo the token value


def test_main_starts_with_compliant_elevated_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token of exactly 32 chars sits on the floor and startup proceeds."""
    from creek_mcp import server as server_module

    monkeypatch.setenv(ELEVATED_TOKEN_ENV, _BOUNDARY_ELEVATED_TOKEN)
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)

    runs: list[str] = []

    class _StubServer:
        def run(self, transport: str) -> None:
            runs.append(transport)

    monkeypatch.setattr(server_module, "build_server", lambda: _StubServer())
    server_module.main([])

    assert runs == ["stdio"]


def test_main_starts_when_elevated_token_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent elevated token is the supported 'purge disabled' posture.

    No token configured means every ``creek.purge.*`` call already fails
    closed in :func:`creek_mcp.auth.is_elevated`; refusing to boot would
    break every operator who never wanted purge enabled.
    """
    from creek_mcp import server as server_module

    monkeypatch.delenv(ELEVATED_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)

    runs: list[str] = []

    class _StubServer:
        def run(self, transport: str) -> None:
            runs.append(transport)

    monkeypatch.setattr(server_module, "build_server", lambda: _StubServer())
    server_module.main([])

    assert runs == ["stdio"]


def test_main_treats_empty_elevated_token_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty elevated token is 'unset', not 'zero chars, too short'."""
    from creek_mcp import server as server_module

    monkeypatch.setenv(ELEVATED_TOKEN_ENV, "")
    monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)

    runs: list[str] = []

    class _StubServer:
        def run(self, transport: str) -> None:
            runs.append(transport)

    monkeypatch.setattr(server_module, "build_server", lambda: _StubServer())
    server_module.main([])

    assert runs == ["stdio"]


# --------------------------------------------------------------------------- #
# Rotation-window startup notice (#895)
#
# A consumer may hold several currently-valid tokens so a secret can be rotated
# without a hard cutover. That window has to be *closed* again, and an operator
# who cannot see it is open will not close it — so network startup announces it.
#
# **On stderr, never stdout.** stdout on this entry point belongs to the stdio
# transport's JSON-RPC framing, and the sibling ``creek-tools-api`` prints its
# OpenAPI document there; an operator notice on stdout corrupts one and breaks
# every pipe consuming the other.
# --------------------------------------------------------------------------- #

# 43 chars each. Low-entropy test literals, not real credentials.
_ROTATION_TOKEN_A = "server-test-rotation-" + "c" * 22
_ROTATION_TOKEN_B = "server-test-rotation-" + "d" * 22


class _StubNetworkServer:
    """Socket-free stand-in for the built network server.

    ``main`` stamps ``settings.host``/``settings.port`` onto whatever
    ``build_server`` returns, so the stub only has to carry those.
    """

    def __init__(self) -> None:
        """Start with mutable settings and nothing bound."""
        self.settings = SimpleNamespace(host=None, port=None)


def _stub_network_startup(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Route the network branch away from sockets; return the served-server log.

    Mirrors ``_stub_build_server`` in ``tests/test_mcp_remote.py``: a guard that
    fails to fire then shows up as an unexpected entry in the log rather than as
    a real bind that hangs the run.

    Args:
        monkeypatch: The active monkeypatch fixture.

    Returns:
        A list that records each server handed to ``_serve_network``.
    """
    from creek_mcp import server as server_module

    served: list[object] = []
    monkeypatch.setattr(
        server_module, "build_server", lambda **_kwargs: _StubNetworkServer()
    )
    monkeypatch.setattr(
        server_module, "_serve_network", lambda server, _args: served.append(server)
    )
    return served


def test_network_startup_announces_an_open_rotation_window_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A consumer holding two tokens is announced — by name and count, on stderr.

    The notice is compared against the verifier's own ``rotation_notice()`` for
    the same configuration, so ``main`` is pinned to *emit the shared message*
    rather than to a second wording that could drift from it.
    """
    from creek_mcp import server as server_module

    raw = f"adepthood={_ROTATION_TOKEN_A},{_ROTATION_TOKEN_B}"
    monkeypatch.delenv(ELEVATED_TOKEN_ENV, raising=False)
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, raw)
    served = _stub_network_startup(monkeypatch)

    server_module.main(["--transport", "network", "--host", "127.0.0.1"])

    captured = capsys.readouterr()
    assert len(served) == 1  # it announced and then served, not instead of serving
    expected = ConsumerTokenVerifier(
        load_consumer_tokens({CONSUMER_TOKENS_ENV: raw})
    ).rotation_notice()
    assert expected is not None
    assert expected in captured.err
    assert "adepthood" in captured.err  # the consumer mid-rotation is named
    assert "2" in captured.err  # ...with its token count
    assert _ROTATION_TOKEN_A not in captured.err  # NEVER echo a token value
    assert _ROTATION_TOKEN_B not in captured.err
    assert captured.out == ""  # stdout belongs to JSON-RPC, not to notices


def test_network_startup_is_silent_when_no_rotation_window_is_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One token per consumer is the steady state, and the steady state says nothing.

    A notice on every start is a notice operators stop reading, at which point
    the one that matters goes unread too.
    """
    from creek_mcp import server as server_module

    monkeypatch.delenv(ELEVATED_TOKEN_ENV, raising=False)
    monkeypatch.setenv(CONSUMER_TOKENS_ENV, f"adepthood={_STRONG_CONSUMER_TOKEN}")
    served = _stub_network_startup(monkeypatch)

    server_module.main(["--transport", "network", "--host", "127.0.0.1"])

    captured = capsys.readouterr()
    assert len(served) == 1
    assert captured.err == ""
    assert captured.out == ""
