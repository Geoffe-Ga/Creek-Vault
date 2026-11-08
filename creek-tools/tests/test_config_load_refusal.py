"""A vault config that cannot load is a type-name refusal everywhere (#2096, #2097).

#2091 fixed ``creek.reflect`` and ``POST /v1/reflections``. The same failure
reached seven more MCP tools and four more ``/v1`` routes:

- **MCP (#2096).** ``creek.classify``, ``creek.link``, ``creek.redact.scan``,
  ``creek.report`` (six config-reading types), ``creek.draft`` (its factory and
  its own read) and ``creek.lint`` (the ``paradox`` check) read the served
  vault's config outside any catch for a config-load failure, so FastMCP served
  the exception text as ``isError``: a stale ``CREEK_CONFIG``'s path, a YAML
  parser's ``while parsing a flow sequence in "…/creek_config.yaml"``, or a
  schema error's ``validation error for CreekConfig`` plus the offending key.
  Each now answers ``<surface> unavailable: <TypeName>`` and logs one WARNING,
  type name only, on ``creek_mcp.config_refusal``.
- **/v1 (#2097).** ``yaml.YAMLError`` is not a ``ValueError``, so the letter and
  reading-recommendation catches let a YAML-invalid config reach the error
  boundary as ``500``; the classification and link routes had no config catch
  at all, so every broken config was a ``500`` there. All four now answer the
  ``503 temporarily_unavailable`` ``POST /v1/reflections`` gives the same fault.

``creek.compile`` is deliberately absent: its catch is rewritten by #2074, and
whichever of the two lands second aligns it with this shape.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import TYPE_CHECKING, Any, Final

import pydantic
import pytest
import yaml
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from creek.classify.classify_engine import LLM_PROVIDER_UNAVAILABLE_REASON
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.lint import LintRunner
from creek_mcp import server as server_mod
from creek_mcp.api.models import ErrorCode
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.httpapi.pipeline import pipeline_refusal_code
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from creek_mcp.server import Transport, build_server
from creek_mcp.tools import classify as classify_mod
from creek_mcp.tools import draft as draft_mod
from creek_mcp.tools import link as link_mod
from creek_mcp.tools import report as report_mod
from tests.test_mcp_provider_refusal_redaction import (
    _CONFIG_LEAKS,
    _UNLOADABLE_CONFIGS,
    _seed,
    _unloadable_config,
)
from tests.test_reading_recommendations import request as reading_request
from tests.v1_api_support import (
    CLASSIFICATIONS_PATH,
    LINKS_PATH,
    build_app,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_REFUSAL_LOGGER: Final[str] = "creek_mcp.config_refusal"
"""The one logger every MCP config-load refusal records on."""

_PIPELINE_LOGGER: Final[str] = "creek_mcp.httpapi.pipeline"
"""The ``/v1`` pipeline adapter's operator log."""

_SCHEMA_KEY: Final[str] = "not_a_real_key_xyz"
"""The forbidden key the schema-invalid configs carry; never served or logged."""

_LEAKS: Final[tuple[str, ...]] = (*_CONFIG_LEAKS, _SCHEMA_KEY)
"""Fragments of each config-load exception's message; none may be served.

A schema-invalid config's ``ValidationError`` names no path, but it does name
the offending key and value — so the key is a leak of its own.
"""

_SERVICE_UNAVAILABLE: Final[int] = 503
_ACCEPTED: Final[int] = 202
_OK: Final[int] = 200

_INBOUND_FILE: Final[str] = "00-Creek-Meta/Inbound/staged.md"
"""A staged file, so ``creek.redact.scan`` clears its scope and existence gates."""

_CONFIG_READING_REPORTS: Final[tuple[str, ...]] = (
    "voice",
    "rhetorical-patterns",
    "unnamed",
    "fingerprint",
    "paradox",
    "synchronicity",
)
"""The ``creek.report`` types whose generator reads the vault config."""

_CONFIG_FREE_REPORTS: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    ("tags", {}),
    ("lexicon", {}),
    ("decisions", {}),
    ("mode-profiles", {}),
    ("wavelength", {"period": "weekly"}),
)
"""The ``creek.report`` types that read no config, so stay ``ok`` on a broken one."""


@pytest.fixture(name="vault")
def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a seeded vault, outside both model-pin modes.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: pytest's patcher.

    Returns:
        The vault root.
    """
    vault = seed_vault(tmp_path / "vault")
    monkeypatch.delenv(LOOPBACK_ONLY_ENV, raising=False)
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    return vault


def _vault_file_config(case: str, vault: Path) -> Path:
    """Write a broken config to the vault's own file, with ``CREEK_CONFIG`` unset.

    ``_unloadable_config``'s schema-invalid case points ``CREEK_CONFIG`` at an
    outside file, which the lint ``paradox`` check never consults: it reads
    ``00-Creek-Meta/creek_config.yaml`` directly. This helper puts both broken
    shapes where that check looks.

    Args:
        case: ``"schema-invalid"`` or ``"yaml-syntax-invalid"``.
        vault: The served vault.

    Returns:
        The config path, which must never be served or logged.
    """
    path = vault / "00-Creek-Meta" / "creek_config.yaml"
    if case == "schema-invalid":
        path.write_text(f"{_SCHEMA_KEY}: 1\n", encoding="utf-8")
    else:
        path.write_text("model_router: [unclosed\n  : : x\n", encoding="utf-8")
    return path


_VAULT_FILE_CONFIGS: Final[list[tuple[str, str]]] = [
    ("schema-invalid", "ValidationError"),
    ("yaml-syntax-invalid", "ParserError"),
]
"""The two broken shapes :func:`_vault_file_config` writes, and their types."""


def _structured(answer: Any) -> dict[str, Any]:
    """Return the structured payload of a FastMCP ``call_tool`` answer.

    Args:
        answer: FastMCP's answer.

    Returns:
        The tool's dict.
    """
    structured = answer[1] if isinstance(answer, tuple) else answer
    assert isinstance(structured, dict), structured
    return structured


def _audit_lines(vault: Path) -> int:
    """Return how many lines the vault's MCP audit log holds.

    Args:
        vault: The vault root.

    Returns:
        The line count; ``0`` when the log does not exist yet.
    """
    path = vault / MCP_AUDIT_RELPATH
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())


def _files(vault: Path) -> set[str]:
    """Return every file under *vault* except the audit log, vault-relative.

    Args:
        vault: The vault root.

    Returns:
        The relative paths.
    """
    audit = vault / MCP_AUDIT_RELPATH
    return {
        str(path.relative_to(vault))
        for path in vault.rglob("*")
        if path.is_file() and path != audit
    }


def _assert_refused(
    answer: Any, *, tool: str, reason: str, leaks: tuple[str, ...]
) -> None:
    """Assert *answer* is the fixed refusal and carries none of *leaks*.

    Args:
        answer: FastMCP's ``call_tool`` answer.
        tool: The tool name the refusal echoes.
        reason: The exact refusal reason.
        leaks: Strings neither the payload nor its rendering may carry.
    """
    payload = _structured(answer)
    assert payload["status"] == "refused"
    assert payload["tool"] == tool
    assert payload["reason"] == reason
    rendered = (json.dumps(payload), str(answer))
    for leaked in leaks:
        for text in rendered:
            assert leaked not in text, (leaked, text)


def _assert_one_type_name_record(
    caplog: pytest.LogCaptureFixture, type_name: str, leaks: tuple[str, ...]
) -> None:
    """Assert one WARNING on the refusal logger, type name only, and no leak.

    Args:
        caplog: pytest's log capture.
        type_name: The exception class name the record must end with.
        leaks: Strings no captured record may carry.
    """
    records = [r for r in caplog.records if r.name == _REFUSAL_LOGGER]
    assert len(records) == 1, [(r.name, r.getMessage()) for r in caplog.records]
    (record,) = records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert record.getMessage().endswith(f": {type_name}"), record.getMessage()
    for leaked in leaks:
        assert leaked not in caplog.text, leaked


# --------------------------------------------------------------------------- #
# The shared catch tuple
# --------------------------------------------------------------------------- #


def test_one_shared_tuple_names_every_config_load_failure() -> None:
    """``CONFIG_LOAD_FAILURES`` lives once, in ``creek.config``, and is reused.

    ``yaml.YAMLError`` is listed because it is *not* a ``ValueError`` — the
    whole of #2097 — while a pydantic ``ValidationError`` already is one. The
    three restated copies now name the shared tuple rather than re-spelling it.
    Read by attribute so this module still collects against a tree that has
    not defined it.
    """
    config_mod = importlib.import_module("creek.config")
    vault_mod = importlib.import_module("creek_mcp.httpapi.vault")
    reflect_mod = importlib.import_module("creek_mcp.tools.reflect")
    shared = getattr(config_mod, "CONFIG_LOAD_FAILURES", None)

    assert shared == (OSError, ValueError, yaml.YAMLError)
    assert vault_mod.UNREADABLE_CONFIG is shared
    assert (RuntimeError, *shared) == reflect_mod._SEAM_BUILD_FAILURES
    assert not issubclass(yaml.YAMLError, ValueError)
    assert issubclass(pydantic.ValidationError, ValueError)


# --------------------------------------------------------------------------- #
# MCP (#2096): classify, link, redact.scan, report
# --------------------------------------------------------------------------- #

_MCP_ROWS: Final[list[tuple[str, dict[str, Any], str]]] = [
    ("creek.classify", {"method": "rules"}, "classification unavailable: "),
    ("creek.link", {"method": "temporal"}, "linking unavailable: "),
    (
        "creek.redact.scan",
        {"input_path": "00-Creek-Meta/Inbound"},
        "redaction scan unavailable: ",
    ),
    *[
        (
            "creek.report",
            {"report_type": report_type, "privacy_tier_ceiling": "all"},
            "report unavailable: ",
        )
        for report_type in _CONFIG_READING_REPORTS
    ],
]
"""``(tool, arguments, reason prefix)`` for each config-reading MCP call."""

_MCP_ROW_IDS: Final[list[str]] = [
    tool if tool != "creek.report" else f"report-{args['report_type']}"
    for tool, args, _prefix in _MCP_ROWS
]


@pytest.mark.parametrize(
    ("case", "type_name"), _UNLOADABLE_CONFIGS, ids=[c for c, _ in _UNLOADABLE_CONFIGS]
)
@pytest.mark.parametrize(("tool", "args", "prefix"), _MCP_ROWS, ids=_MCP_ROW_IDS)
def test_mcp_tool_refuses_an_unloadable_config_without_its_path(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tool: str,
    args: dict[str, Any],
    prefix: str,
    case: str,
    type_name: str,
) -> None:
    """A config that cannot load is ``<surface> unavailable: <Type>``, not ``isError``.

    Before #2096 each call raised ``ToolError`` whose text was the stale path,
    the YAML parser's snippet of the file, or the schema error naming the
    forbidden key. Nothing is written: classify, link and report append no
    audit line and no artifact; redact.scan's audit line precedes its gates,
    as it always has.
    """
    (vault / _INBOUND_FILE).parent.mkdir(parents=True, exist_ok=True)
    (vault / _INBOUND_FILE).write_text("nothing sensitive here\n", encoding="utf-8")
    config_path = _unloadable_config(case, vault, tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    audit_before = _audit_lines(vault)
    files_before = _files(vault)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    answer = asyncio.run(server.call_tool(tool, args))

    leaks = (str(config_path), *_LEAKS)
    _assert_refused(answer, tool=tool, reason=prefix + type_name, leaks=leaks)
    _assert_one_type_name_record(caplog, type_name, leaks)
    audited = 1 if tool == "creek.redact.scan" else 0
    assert _audit_lines(vault) == audit_before + audited
    assert _files(vault) == files_before


@pytest.mark.parametrize(
    ("case", "type_name"), _UNLOADABLE_CONFIGS, ids=[c for c, _ in _UNLOADABLE_CONFIGS]
)
@pytest.mark.parametrize(
    ("report_type", "extra"),
    _CONFIG_FREE_REPORTS,
    ids=[report_type for report_type, _ in _CONFIG_FREE_REPORTS],
)
def test_a_report_that_reads_no_config_is_unaffected_by_a_broken_one(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_type: str,
    extra: dict[str, Any],
    case: str,
    type_name: str,
) -> None:
    """The refusal is per generator, never an eager read in ``report_tool``."""
    del type_name
    _unloadable_config(case, vault, tmp_path, monkeypatch)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    answer = asyncio.run(
        server.call_tool(
            "creek.report",
            {"report_type": report_type, "privacy_tier_ceiling": "all", **extra},
        )
    )

    assert _structured(answer)["status"] == "ok"


# --------------------------------------------------------------------------- #
# MCP (#2096): the catch is narrowed to the config read
# --------------------------------------------------------------------------- #


def _raising(exc: Exception) -> Callable[..., Any]:
    """Return a stand-in that raises *exc* whatever it is called with.

    Args:
        exc: What to raise.

    Returns:
        The raising callable.
    """

    def _raise(*_args: object, **_kwargs: object) -> Any:
        """Fail the way an unrelated engine fault does.

        Raises:
            Exception: Always, *exc*.
        """
        raise exc

    return _raise


def test_an_unrelated_link_engine_value_error_still_escapes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the config read is caught; a ``run_link`` ``ValueError`` is a bug."""
    monkeypatch.setattr(link_mod, "run_link", _raising(ValueError("engine bug")))
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    with pytest.raises(ToolError, match="engine bug"):
        asyncio.run(server.call_tool("creek.link", {"method": "temporal"}))


def test_an_unrelated_classify_engine_os_error_still_escapes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the config read is caught; a ``run_classify`` ``OSError`` is a bug."""
    monkeypatch.setattr(classify_mod, "run_classify", _raising(OSError("disk bug")))
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    with pytest.raises(ToolError, match="disk bug"):
        asyncio.run(server.call_tool("creek.classify", {"method": "rules"}))


def test_a_report_generator_os_error_after_a_good_config_still_escapes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report sentinel wraps the config read alone, never the generator."""

    class _BrokenVoice:
        """A voice generator whose write fails after the config loaded."""

        def __init__(self, **_kwargs: object) -> None:
            """Accept the real constructor's keywords."""

        def generate_all_profiles(self, _vault: Path) -> list[Path]:
            """Fail the way a full disk does.

            Raises:
                OSError: Always.
            """
            msg = "write bug"
            raise OSError(msg)

    monkeypatch.setattr(report_mod, "VoiceProfileGenerator", _BrokenVoice)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    with pytest.raises(ToolError, match="write bug"):
        asyncio.run(
            server.call_tool(
                "creek.report",
                {"report_type": "voice", "privacy_tier_ceiling": "all"},
            )
        )


# --------------------------------------------------------------------------- #
# MCP (#2096): creek.draft, both reads
# --------------------------------------------------------------------------- #


def _mine_one_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``creek.draft``'s miner surface exactly one sourceless seed.

    Args:
        monkeypatch: pytest's patcher.
    """
    monkeypatch.setattr(
        draft_mod,
        "IdeaMiner",
        lambda **_kwargs: type(
            "_Miner",
            (),
            {"mine_all": lambda _self, _vault_path, *, current_phase: [_seed()]},
        )(),
    )


def _never_called(_prompt: str) -> str:
    """Stand in for a draft LLM the refusal must never reach.

    Raises:
        AssertionError: Always — a refused draft dials nothing.
    """
    msg = "a refused draft must not dial a model"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    ("case", "type_name"), _UNLOADABLE_CONFIGS, ids=[c for c, _ in _UNLOADABLE_CONFIGS]
)
@pytest.mark.parametrize("injected", [False, True], ids=["factory-path", "direct-read"])
def test_mcp_draft_refuses_an_unloadable_config_without_its_path(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
    type_name: str,
    injected: bool,
) -> None:
    """Both of draft's config reads refuse as ``draft unavailable: <Type>``.

    ``factory-path`` builds the production factory, which read the config in
    ``server._build_draft_llm`` outside draft's ``PROVIDER_CALL_FAILURES``
    catch; ``direct-read`` injects a factory, so the leak was draft's own read
    of the grounding thresholds. The read now happens once, before the factory
    is built, so neither path reaches a model.
    """
    _mine_one_seed(monkeypatch)
    config_path = _unloadable_config(case, vault, tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    factory = (lambda _tier: _never_called) if injected else None
    server = build_server(
        transport=Transport.STDIO, vault_path=vault, draft_llm_factory=factory
    )

    answer = asyncio.run(server.call_tool("creek.draft", {}))

    leaks = (str(config_path), *_LEAKS)
    _assert_refused(
        answer,
        tool="creek.draft",
        reason=f"draft unavailable: {type_name}",
        leaks=leaks,
    )
    _assert_one_type_name_record(caplog, type_name, leaks)


def test_a_draft_provider_build_value_error_is_not_relabelled_as_config(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ValueError`` from building the provider is not a config failure.

    The config read is hoisted above the factory call rather than the factory
    call being wrapped in the config catch, so a provider fault met after a
    good config read keeps escaping exactly as it did before #2096.
    """
    _mine_one_seed(monkeypatch)
    monkeypatch.setattr(
        server_mod, "_generation_provider", _raising(ValueError("provider bug"))
    )
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    with pytest.raises(ToolError, match="provider bug"):
        asyncio.run(server.call_tool("creek.draft", {}))


# --------------------------------------------------------------------------- #
# MCP (#2096): creek.lint's paradox check
# --------------------------------------------------------------------------- #

_PARADOX_SELECTIONS: Final[list[dict[str, Any]]] = [
    {"checks": ["paradox"]},
    {"checks": None, "since": "30d"},
]
"""The two ways a lint call selects ``paradox``: by name, or by ``since``."""


@pytest.mark.parametrize(
    ("case", "type_name"), _VAULT_FILE_CONFIGS, ids=[c for c, _ in _VAULT_FILE_CONFIGS]
)
@pytest.mark.parametrize("selection", _PARADOX_SELECTIONS, ids=["named", "since"])
def test_mcp_lint_refuses_an_unloadable_paradox_config_without_its_path(
    vault: Path,
    caplog: pytest.LogCaptureFixture,
    selection: dict[str, Any],
    case: str,
    type_name: str,
) -> None:
    """``paradox`` reads the vault's own file; a broken one is ``lint unavailable``.

    The lint audit line precedes the refusal, as it precedes every lint call;
    no report is written.
    """
    config_path = _vault_file_config(case, vault)
    caplog.set_level(logging.DEBUG)
    audit_before = _audit_lines(vault)
    files_before = _files(vault)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    answer = asyncio.run(server.call_tool("creek.lint", selection))

    leaks = (str(config_path), *_LEAKS)
    _assert_refused(
        answer, tool="creek.lint", reason=f"lint unavailable: {type_name}", leaks=leaks
    )
    _assert_one_type_name_record(caplog, type_name, leaks)
    assert _audit_lines(vault) == audit_before + 1
    assert _files(vault) == files_before


@pytest.mark.parametrize(
    ("case", "type_name"), _VAULT_FILE_CONFIGS, ids=[c for c, _ in _VAULT_FILE_CONFIGS]
)
@pytest.mark.parametrize(
    "selection", [{}, {"checks": ["tags"]}], ids=["default-no-since", "tags"]
)
def test_mcp_lint_without_paradox_is_unaffected_by_a_broken_config(
    vault: Path, selection: dict[str, Any], case: str, type_name: str
) -> None:
    """Paradox joins the default set only with ``since``; without it, lint runs."""
    del type_name
    _vault_file_config(case, vault)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    answer = asyncio.run(server.call_tool("creek.lint", selection))

    assert _structured(answer)["status"] == "ok"


def test_mcp_lint_paradox_ignores_a_stale_creek_config(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paradox`` reads ``00-Creek-Meta/creek_config.yaml`` and nothing else.

    A dangling ``CREEK_CONFIG`` is therefore not its failure to refuse: the
    check runs on the vault's own (here absent) file and the call is ``ok``.
    """
    _unloadable_config("stale-creek-config", vault, tmp_path, monkeypatch)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    answer = asyncio.run(server.call_tool("creek.lint", {"checks": ["paradox"]}))

    assert _structured(answer)["status"] == "ok"


def test_mcp_lint_still_raises_on_an_unknown_check(vault: Path) -> None:
    """Selecting checks stays outside the config catch: a bad name still raises."""
    _vault_file_config("yaml-syntax-invalid", vault)
    server = build_server(transport=Transport.STDIO, vault_path=vault)

    with pytest.raises(ToolError, match="unknown check"):
        asyncio.run(server.call_tool("creek.lint", {"checks": ["no-such-check"]}))


def test_the_lint_cli_runner_still_propagates_a_broken_paradox_config(
    vault: Path,
) -> None:
    """The refusal is the MCP boundary's; the runner the CLI drives still raises."""
    _vault_file_config("yaml-syntax-invalid", vault)

    with pytest.raises(yaml.YAMLError):
        LintRunner(vault_path=vault).run(checks=["paradox"])


# --------------------------------------------------------------------------- #
# /v1 (#2097): a 503, never a 500
# --------------------------------------------------------------------------- #

_LETTER_PATH: Final[str] = "/v1/letters/adepthood-voicedraft-2097aa"

_LETTER_BODY: Final[dict[str, Any]] = {
    "content": "I walked the long way home so I could hear the river.",
    "note": {
        "kind": "pattern",
        "anchor_text": "hear the river",
        "note": "You already know how to wait for clarity.",
    },
    "tier": "personal",
}
"""A valid, non-distress letter request."""

_V1_ROWS: Final[list[tuple[str, dict[str, Any]]]] = [
    (_LETTER_PATH, _LETTER_BODY),
    ("/v1/reading-recommendations", reading_request().model_dump(mode="json")),
    (CLASSIFICATIONS_PATH, {"method": "rules"}),
    (LINKS_PATH, {"method": "temporal"}),
    (LINKS_PATH, {"method": "eddies"}),
]
"""``(path, body)`` for each ``/v1`` route that builds from the vault config."""


@pytest.mark.parametrize(
    ("case", "type_name"), _UNLOADABLE_CONFIGS, ids=[c for c, _ in _UNLOADABLE_CONFIGS]
)
@pytest.mark.parametrize(
    ("path", "body"),
    _V1_ROWS,
    ids=[
        "letters",
        "reading-recommendations",
        "classify-rules",
        "link-temporal",
        "link-eddies",
    ],
)
def test_an_unloadable_vault_config_is_a_503_without_its_path(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
    body: dict[str, Any],
    case: str,
    type_name: str,
) -> None:
    """Each route answers the documented retryable ``503``, as reflections does.

    No factory is injected, so each route takes its production path through
    the served vault's config. Before #2097 the YAML-invalid case was ``500``
    on every row, and every case was ``500`` on the pipeline rows.
    """
    del type_name
    config_path = _unloadable_config(case, vault, tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)

    app = build_app(vault_path=vault)
    with TestClient(app, raise_server_exceptions=False) as api:
        response = api.post(path, json=body, headers=headers(ceiling="personal"))

    assert response.status_code == _SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    for leaked in (str(config_path), *_LEAKS):
        assert leaked not in response.text, leaked
        assert leaked not in caplog.text, leaked


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("classification unavailable: ParserError", ErrorCode.TEMPORARILY_UNAVAILABLE),
        ("linking unavailable: ValidationError", ErrorCode.TEMPORARILY_UNAVAILABLE),
        ("linking unavailable: FileNotFoundError", ErrorCode.TEMPORARILY_UNAVAILABLE),
        ("linking unavailable: /etc/x", ErrorCode.INTERNAL_ERROR),
        ("linking unavailable: ", ErrorCode.INTERNAL_ERROR),
        ("linking unavailable: Parser Error", ErrorCode.INTERNAL_ERROR),
        ("classification unavailable: ParserError\n", ErrorCode.INTERNAL_ERROR),
        ("report unavailable: ParserError", ErrorCode.INTERNAL_ERROR),
        ("provider said: classification unavailable: X", ErrorCode.INTERNAL_ERROR),
        (LLM_PROVIDER_UNAVAILABLE_REASON, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_pipeline_refusal_code_maps_the_config_unavailable_reason(
    reason: str, code: ErrorCode
) -> None:
    """A creek-owned prefix plus a closed type-name tail, never free text."""
    assert pipeline_refusal_code(reason) is code


@pytest.mark.parametrize(
    ("case", "type_name"), _VAULT_FILE_CONFIGS, ids=[c for c, _ in _VAULT_FILE_CONFIGS]
)
@pytest.mark.parametrize(
    ("path", "method", "surface"),
    [
        (CLASSIFICATIONS_PATH, "llm", "classification"),
        (LINKS_PATH, "embeddings", "linking"),
    ],
    ids=["classify-llm", "link-embeddings"],
)
def test_an_async_pipeline_job_with_an_unloadable_config_fails_as_the_tool_refusal(
    vault: Path,
    caplog: pytest.LogCaptureFixture,
    path: str,
    method: str,
    surface: str,
    case: str,
    type_name: str,
) -> None:
    """The durable job records the tool's fixed refusal, with no public code.

    No resubmit clears an operator's broken config, so the job is codeless like
    the other operator-side refusals; it fails as a refusal rather than as an
    exception, so the worker writes no traceback.
    """
    config_path = _vault_file_config(case, vault)
    caplog.set_level(logging.DEBUG)

    with TestClient(build_app(vault_path=vault)) as api:
        accepted = api.post(path, json={"method": method}, headers=headers())
        assert accepted.status_code == _ACCEPTED, accepted.text
        job_id = accepted.json()["job_id"]
        body = _await_terminal(api, job_id)

    assert body["state"] == "failed"
    assert body["failure_code"] is None
    record = json.loads(
        (vault / "00-Creek-Meta" / "State" / "jobs" / f"{job_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["failure_reason"] == f"refused: {surface} unavailable: {type_name}"
    pipeline_records = [r for r in caplog.records if r.name == _PIPELINE_LOGGER]
    assert all(r.exc_info is None for r in pipeline_records)
    for leaked in (str(config_path), *_LEAKS):
        assert leaked not in caplog.text, leaked
        assert leaked not in json.dumps(record), leaked


def _await_terminal(api: TestClient, job_id: str) -> dict[str, Any]:
    """Poll one job to a terminal state within a short bounded wait.

    Args:
        api: The client the job was accepted on.
        job_id: The accepted job's id.

    Returns:
        The terminal ``/v1/jobs`` body.
    """
    for _ in range(400):
        response = api.get(f"/v1/jobs/{job_id}", headers=headers())
        assert response.status_code == _OK
        body: dict[str, Any] = response.json()
        if body["state"] in {"succeeded", "failed"}:
            return body
        asyncio.run(asyncio.sleep(0.01))
    pytest.fail("pipeline job did not reach a terminal state")
