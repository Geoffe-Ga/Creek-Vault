"""Bootstrap + registration tests for the creek-tools MCP server (FEAT-010)."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

from creek_mcp.auth import ELEVATED_TOKEN_ENV
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.remote_auth import CONSUMER_TOKENS_ENV
from creek_mcp.server import SERVER_NAME, build_server

if TYPE_CHECKING:
    from collections.abc import Iterator
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    assert server.name == SERVER_NAME


def test_build_server_registers_all_tools(vault: Path) -> None:
    """All FEAT-010 read + FEAT-011 write tools surface via ``list_tools``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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


def test_call_tool_wheel_returns_complete_frequency_balance(vault: Path) -> None:
    """End-to-end: ``creek.wheel`` returns a complete F1-F10 balance map.

    Even with an empty corpus the wheel is present and all-zero (the new-user
    case), proving the read-only aggregation is reachable through the registry.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
    """End-to-end: ``call_tool("creek.state.read")`` returns the report bytes."""
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit\n\nhello\n",
        encoding="utf-8",
    )
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool("creek.state.read", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.state.read"
    assert "Audit" in structured["content"]  # type: ignore[operator]


def test_call_tool_state_render_through_mcp(vault: Path) -> None:
    """The render path is reachable via ``call_tool``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
        draft_llm_factory=lambda: lambda prompt: "ignored",
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
    server = build_server(draft_llm_factory=lambda: lambda prompt: "x")
    assert server.name == SERVER_NAME


def test_build_draft_llm_raises_when_classifier_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production factory bubbles a clear error when no LLM is reachable."""
    from creek_mcp import server as server_module

    class _UnavailableClassifier:
        available = False

        def __init__(self, _config: object) -> None:
            pass

        def invoke_prompt(self, prompt: str) -> str:  # pragma: no cover
            return ""

    monkeypatch.setattr(
        "creek.classify.llm.LLMClassifier",
        _UnavailableClassifier,
    )
    with pytest.raises(RuntimeError, match="LLM provider unavailable"):
        server_module._build_draft_llm()


def test_build_draft_llm_returns_invoke_prompt_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the classifier reports ``available``, the factory returns the callable."""
    from creek_mcp import server as server_module

    class _AvailableClassifier:
        available = True

        def __init__(self, _config: object) -> None:
            pass

        def invoke_prompt(self, prompt: str) -> str:
            return "drafted body"

    monkeypatch.setattr(
        "creek.classify.llm.LLMClassifier",
        _AvailableClassifier,
    )
    llm = server_module._build_draft_llm()
    assert callable(llm)
    assert llm("hi") == "drafted body"


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
