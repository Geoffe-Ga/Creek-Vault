"""A vault's own config must reach the consumer acting on it (#1409/#1410).

Eleven production sites resolve configuration with the bare, process-wide
``load_config()``. That form reads ``creek_config.yaml`` relative to the
server's *current working directory* and never opens
``<vault>/00-Creek-Meta/creek_config.yaml`` — so every knob the operator set on
the vault they named is silently discarded and a built-in default is used in
its place. Nothing raises, nothing logs; the tool returns ``status: ok`` having
consulted the wrong file, or no file at all.

The harness is the load-bearing part of this module, and it has exactly two
rules:

* **Never ``chdir`` into the vault**, and
* **never set ``CREEK_CONFIG``**.

Either one makes the broken code pass. Every test therefore runs from an empty
scratch directory holding no ``creek_config.yaml`` (:func:`_isolate`), which is
the only cwd from which "the vault's value arrived" and "a default arrived"
are distinguishable.

The second discipline is the **two-vault diff**. Every behavioural assertion
builds two vaults differing in exactly one config value and asserts the
observable differs. A single-vault assertion cannot tell "read the vault's
file" apart from "got the built-in default, which happened to match" — and
because the defect *is* silent defaulting, that distinction is the entire test.

Vault configs are written as the full ``CreekConfig().model_dump()`` block that
``creek init`` scaffolds, not a hand-trimmed fragment, so the fix is proven
against the file a real vault carries.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml
from typer.testing import CliRunner

import creek
import creek_mcp
from creek.classify.classify_engine import ClassifySummary
from creek.config import CreekConfig
from creek.link.link_engine import LinkSummary
from creek_mcp.policy import Transport
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_CONFIG_SUBPATH: Final = ("00-Creek-Meta", "creek_config.yaml")


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from a directory holding no ``creek_config.yaml``, ``CREEK_CONFIG`` unset.

    ``tests/conftest.py``'s autouse fixture already unsets ``CREEK_CONFIG``
    suite-wide (#1354). It is restated here so this module's guarantee does not
    depend on a fixture defined in another file — if that fixture is ever
    narrowed, these tests must not quietly start passing for the wrong reason.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest temporary directory.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir(exist_ok=True)
    monkeypatch.chdir(nowhere)


def _vault(
    tmp_path: Path,
    name: str,
    edit: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Scaffold a vault whose own config carries *edit*'s change.

    Args:
        tmp_path: Pytest temporary directory.
        name: Directory name, so one test can build two vaults.
        edit: Mutator applied to the full default config dump. ``None``
            writes the untouched defaults — the control half of a diff.

    Returns:
        The vault root.
    """
    vault = tmp_path / name
    (vault / "01-Fragments").mkdir(parents=True, exist_ok=True)
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    if edit is not None:
        edit(data)
    vault.joinpath(*_CONFIG_SUBPATH).write_text(
        yaml.dump(data, sort_keys=False),
        encoding="utf-8",
    )
    return vault


# ---------------------------------------------------------------------------
# #1409 — the structural sweep: no NEW site may resolve a vault knob from cwd
# ---------------------------------------------------------------------------

_BOOTSTRAP_BARE_LOAD_CONFIG: Final[dict[tuple[str, str], str]] = {
    (
        "creek_mcp.server",
        "_resolve_vault",
    ): "Bootstrap: this is the function that ANSWERS 'which vault?'. It runs "
    "before any vault is known, so scoping it to a vault would be circular.",
    (
        "creek_mcp.httpapi.vault",
        "configured_vault",
    ): "Bootstrap: the HTTP surface's equivalent of _resolve_vault, resolving "
    "app.state.vault_path's fallback. Same circularity.",
    (
        "creek.cli",
        "_gdrive_revoke",
    ): "The `gdrive` command declares no --vault flag, so it is cwd-scoped by "
    "construction rather than by omission. Giving it one is tracked as #1571.",
    (
        "creek.cli",
        "_gdrive_check",
    ): "As _gdrive_revoke: `gdrive` has no --vault flag (#1571).",
    (
        "creek.cli",
        "gdrive",
    ): "As _gdrive_revoke: `gdrive` has no --vault flag (#1571).",
    (
        "creek.cli",
        "_run_compost_calibration",
    ): "`compost calibrate` declares no --vault flag either. Same follow-up, #1571.",
}
"""Sites that legitimately read the cwd, each with the reason it may.

Keyed by ``(module, enclosing function)`` rather than by module: four of
``creek_mcp.server``'s five bare calls are defects and one is not, so a
module-level allowlist would hide them.

Deliberately **not** listed here is ``creek_mcp.tools.drive._loaded_config``.
Its docstring calls the cwd resolution intentional — "the same way ``creek
gdrive`` resolves it" — but the parallel does not hold: the CLI's ``gdrive``
command declares no ``--vault`` flag, whereas all three MCP drive tools that
call this helper (``_drive_status``-style bodies at the ``config =
_loaded_config()`` sites) take ``vault_path`` as a parameter. A remote caller
naming vault A therefore drives the Google Drive connector — including its
staging directory — that some other vault's config declared. It is the same
defect as ``link``/``classify``/``redact``, so it is reported as one.
"""


def _call_name(node: ast.Call) -> str | None:
    """Resolve a call's callee to a bare symbol name.

    Args:
        node: The call node to inspect.

    Returns:
        ``func.id`` for a direct call, ``func.attr`` for a qualified one, or
        ``None`` when the callee is a dynamic expression.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _dotted(path: Path, root: Path) -> str:
    """Map a source file to its dotted module name.

    Args:
        path: The ``.py`` file.
        root: The directory the package lives directly beneath.

    Returns:
        The dotted module name, with a trailing ``.__init__`` stripped.
    """
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _bare_load_config_sites() -> list[tuple[str, str, int]]:
    """Return ``(module, enclosing function, lineno)`` for every bare call.

    "Bare" means ``load_config()`` invoked with **no positional argument** —
    i.e. no resolved config path — which is precisely the form that falls
    through to ``Path("creek_config.yaml")`` in the process's current
    directory.

    The walk parses file *text* rather than importing. Importing every module
    under ``creek/`` would pull in optional heavy extras (pyarrow,
    sentence-transformers, the MCP SDK), can fail on any import-time side
    effect, and would make a structural guard the slowest test in the suite.

    Returns:
        One tuple per bare call site, sorted.
    """
    found: list[tuple[str, str, int]] = []

    def walk(node: ast.AST, dotted: str, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                walk(child, dotted, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and _call_name(child) == "load_config"
                and not child.args
            ):
                found.append((dotted, enclosing, child.lineno))
            walk(child, dotted, enclosing)

    for package in (creek, creek_mcp):
        pkg_root = Path(str(package.__file__)).parent
        root = pkg_root.parent
        for source in sorted(pkg_root.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            walk(tree, _dotted(source, root), "<module>")
    return sorted(found)


def test_no_vault_scoped_site_resolves_its_config_from_the_process_cwd() -> None:
    """Every bare ``load_config()`` is either a bootstrap or a defect (#1409).

    This is the guard that makes #1409 non-recurring. The behavioural tests
    below prove today's eleven sites read the wrong file; only this one stops
    the twelfth from being added next month, because a bare ``load_config()``
    is indistinguishable from correct code at the call site — it has no
    argument to look wrong.
    """
    offenders = [
        (module, function, lineno)
        for module, function, lineno in _bare_load_config_sites()
        if (module, function) not in _BOOTSTRAP_BARE_LOAD_CONFIG
    ]
    assert not offenders, (
        "These sites resolve configuration from the process's current "
        f"directory rather than from the vault they act on: {offenders}. "
        "Each has a vault in scope and must resolve through it. If a site is "
        "genuinely a bootstrap that answers 'which vault?', add it to "
        "_BOOTSTRAP_BARE_LOAD_CONFIG with the reason it may read the cwd."
    )


def test_bare_load_config_bootstrap_allowlist_is_non_vacuous() -> None:
    """Every allowlisted exemption still names a real call site.

    An exemption whose site was renamed or deleted is an assertion about
    nothing, and it silently widens the guard above: the stale key would go on
    excusing whatever function later takes that name.
    """
    live = {
        (module, function) for module, function, _lineno in _bare_load_config_sites()
    }
    stale = sorted(set(_BOOTSTRAP_BARE_LOAD_CONFIG) - live)
    assert not stale, (
        f"_BOOTSTRAP_BARE_LOAD_CONFIG excuses {stale}, which no longer calls a "
        "bare load_config(). Remove the entry — a stale exemption quietly "
        "excuses the next function to take that name."
    )


def test_the_bare_load_config_sweep_actually_scans_something() -> None:
    """The sweep found calls in at least two distinct modules.

    A discovery walk that returns nothing reports green while asserting
    nothing. This pins that the walk is really reaching ``creek/`` and
    ``creek_mcp/`` rather than silently globbing an empty tree.
    """
    sites = _bare_load_config_sites()
    assert len({module for module, _function, _lineno in sites}) >= 2, (
        f"The bare-load_config sweep found {sites}, which is too little to be "
        "a real scan of creek/ and creek_mcp/. The walk is broken, not clean."
    )


# ---------------------------------------------------------------------------
# #1409 — the shared resolver's one deliberate behaviour change
# ---------------------------------------------------------------------------

_MISSING_CONFIG_WARNING: Final = "Config file"
"""The ARCH-002 "no config anywhere" WARNING, by its opening words."""


@pytest.mark.parametrize(
    ("warn", "expect_warning"),
    [(False, False), (True, True)],
    ids=["library-surface-default", "cli-wrapper"],
)
def test_the_shared_resolver_warns_only_where_an_operator_is_listening(
    warn: bool,
    expect_warning: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A config-less vault is silent for a library and warned about for the CLI.

    Converging eleven sites onto :func:`~creek.config.load_vault_config`
    changed one thing besides *which file* is read: four of them previously
    called the bare ``load_config()``, whose ``warn_on_missing`` defaults to
    ``True``, and now get the resolver's ``False``. That is deliberate — an MCP
    tool answering a remote caller has no console to warn at, and a WARNING per
    request is noise, not a signal — but a silent default is exactly the class
    of change #1409 exists to stop happening unnoticed. So the split is pinned
    in both directions rather than left as a docstring claim.

    Args:
        warn: The ``warn_on_missing`` the caller passes.
        expect_warning: Whether the ARCH-002 WARNING must be emitted.
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
        caplog: Pytest log capture fixture.
    """
    import logging

    from creek.config import load_vault_config

    _isolate(monkeypatch, tmp_path)
    bare = tmp_path / "bare"
    (bare / "00-Creek-Meta").mkdir(parents=True)

    with caplog.at_level(logging.WARNING, logger="creek.config"):
        load_vault_config(bare, warn_on_missing=warn)

    warned = any(_MISSING_CONFIG_WARNING in record.message for record in caplog.records)
    assert warned is expect_warning, (
        f"load_vault_config(warn_on_missing={warn}) "
        f"{'did not warn' if expect_warning else 'warned'} about a vault that "
        "carries no creek_config.yaml. The library surfaces must default to "
        "silence and the CLI wrappers must keep opting in — see "
        "creek/cli.py's _load_config_for_vault, which passes True."
    )


# ---------------------------------------------------------------------------
# #1409 — creek_mcp/tools/report.py: paradox and synchronicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report_type", "generator_attr"),
    [
        ("synchronicity", "generate_synchronicities"),
        ("paradox", "generate_paradoxes"),
    ],
)
def test_report_hands_the_generator_the_named_vaults_embeddings_config(
    report_type: str,
    generator_attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``report --type paradox/synchronicity`` must read the vault's embeddings knob.

    Both generators are handed an :class:`~creek.config.EmbeddingsConfig` that
    decides which cached vectors are even *loadable*:
    ``EmbeddingLinker.load_cache`` drops every row whose ``model_name`` differs
    from ``config.model``. Reading that knob from the cwd therefore does not
    merely mis-tune the report — it silently empties the corpus the report is
    computed from, and the tool still answers ``status: ok`` with zero notes.

    The recording sits at the generator boundary, which is exactly the edge the
    defect crosses: the assertion is on the value the *vault's* file put into
    the config object handed across it.

    Args:
        report_type: The report type to request.
        generator_attr: The generator ``report_tool`` fans out to.
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek_mcp.tools import report as report_mod

    _isolate(monkeypatch, tmp_path)
    seen: list[str] = []

    def _record(vault_path: Path, embeddings_config: Any) -> list[Path]:
        seen.append(embeddings_config.model)
        return []

    monkeypatch.setattr(report_mod, generator_attr, _record)

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["embeddings"].update(model="creek-test-embed-v1"),
    )
    control = _vault(tmp_path, "control")

    for vault in (tuned, control):
        report_mod.report_tool(
            vault_path=vault,
            report_type=report_type,
            privacy_tier_ceiling=TierCeiling.ALL,
        )

    assert seen == ["creek-test-embed-v1", CreekConfig().embeddings.model], (
        f"report --type {report_type} handed {generator_attr} the embeddings "
        f"models {seen}. The first vault's own creek_config.yaml sets "
        "'creek-test-embed-v1'; reading it from the process cwd instead yields "
        "the built-in default and drops every cached vector in that vault."
    )


def test_report_unnamed_builds_its_linker_from_the_named_vaults_embeddings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``report --type unnamed`` must read the vault's embeddings knob too.

    The third of the three ``report.py`` sites #1409 names, and it is separate
    from the parametrized case above because its config does not reach a
    generator function: ``_generate_unnamed`` builds an
    :class:`~creek.link.embeddings.EmbeddingLinker` itself and hands *that* to
    :class:`~creek.generate.unnamed.UnnamedDigestGenerator`. So the recording
    boundary is the linker's constructor rather than a generator call.

    Same consequence as its siblings, and worse for being invisible:
    ``EmbeddingLinker.load_cache`` drops every cached row whose ``model_name``
    differs from ``config.model``, so a cwd-resolved config silently empties
    the digest's corpus while the tool still answers ``status: ok``.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek_mcp.tools import report as report_mod

    _isolate(monkeypatch, tmp_path)
    seen: list[str] = []

    class _Linker:
        """An ``EmbeddingLinker`` stand-in recording the config it was built from."""

        def __init__(self, *, config: Any) -> None:
            """Record *config*'s model name.

            Args:
                config: The ``EmbeddingsConfig`` the tool resolved.
            """
            seen.append(config.model)

    class _Generator:
        """An ``UnnamedDigestGenerator`` stand-in that writes no corpus."""

        def __init__(self, *, embedding_linker: Any) -> None:
            """Accept the linker; the assertion is made on its constructor.

            Args:
                embedding_linker: The linker ``_generate_unnamed`` built.
            """
            del embedding_linker

        def generate_weekly_digest(self, vault_path: Path, week_start: Any) -> Path:
            """Return a vault-relative placeholder path.

            Args:
                vault_path: The vault the digest belongs to.
                week_start: The Monday the digest covers.

            Returns:
                A path under *vault_path*, so ``report_tool`` can relativise it.
            """
            del week_start
            return vault_path / "00-Creek-Meta" / "unnamed-digest.md"

    monkeypatch.setattr(report_mod, "EmbeddingLinker", _Linker)
    monkeypatch.setattr(report_mod, "UnnamedDigestGenerator", _Generator)

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["embeddings"].update(model="creek-test-embed-v1"),
    )
    control = _vault(tmp_path, "control")

    for vault in (tuned, control):
        report_mod.report_tool(
            vault_path=vault,
            report_type="unnamed",
            privacy_tier_ceiling=TierCeiling.ALL,
        )

    assert seen == ["creek-test-embed-v1", CreekConfig().embeddings.model], (
        f"report --type unnamed built its EmbeddingLinker from the embeddings "
        f"models {seen}. The first vault's own creek_config.yaml sets "
        "'creek-test-embed-v1'; reading it from the process cwd instead yields "
        "the built-in default, and load_cache then drops every cached vector "
        "in that vault — an empty digest reported as status: ok."
    )


# ---------------------------------------------------------------------------
# #1409 — creek_mcp/server.py: the four LLM builders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("builder", ["_build_draft_llm", "_build_compile_llm"])
def test_server_llm_builder_routes_with_the_served_vaults_config(
    builder: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model that sees a vault's text is chosen by *that vault's* routing.

    ``build_server`` has already resolved the vault before it binds these
    factories, so the vault is available to thread; resolving the router from
    the cwd instead means the operator's per-vault model routing — including
    which provider a given stage egresses to — is decided by wherever the
    server process happens to have been started.

    The stub sits at ``build_provider``, the provider boundary, so the
    assertion is on the :class:`~creek.config.LLMConfig` the vault's file
    produced rather than on the config layer that produced it.

    Args:
        builder: Name of the builder under test.
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    import creek_mcp.server as server_mod
    from creek.classify.llm import providers as providers_mod
    from creek.models import PrivacyTier

    _isolate(monkeypatch, tmp_path)
    seen: list[str] = []

    class _Stub:
        """A provider that records the config it was built from."""

        available = True

        def complete(self, prompt: str) -> Any:
            """Return an empty completion; no test here reads the text."""
            raise NotImplementedError(prompt)

    def _record(cfg: Any) -> _Stub:
        seen.append(cfg.model)
        return _Stub()

    monkeypatch.setattr(providers_mod, "build_provider", _record)

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["llm"]["default"].update(
            model="creek-test-generation-model",
        ),
    )
    control = _vault(tmp_path, "control")

    for vault in (tuned, control):
        getattr(server_mod, builder)(vault, PrivacyTier.OPEN)

    assert seen[0] == "creek-test-generation-model", (
        f"{builder} resolved the model {seen[0]!r} for a vault whose own "
        "creek_config.yaml routes generation to 'creek-test-generation-model'. "
        "The routing was read from the process cwd, not from the served vault."
    )
    assert seen[0] != seen[1], (
        f"{builder} resolved the same model {seen} for two vaults whose "
        "configs differ, which means it read neither of them."
    )


def test_server_reflect_factory_routes_with_the_served_vaults_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_reflect_llm_factory`` must bind to the served vault too.

    Split from the parametrized case above because this builder returns a
    *factory* rather than a callable: the vault is bound once, the tier
    arrives per call.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    import creek_mcp.server as server_mod
    from creek.classify.llm import providers as providers_mod
    from creek.models import PrivacyTier

    _isolate(monkeypatch, tmp_path)
    seen: list[str] = []

    class _Stub:
        """A provider that records the config it was built from."""

        available = True

        def complete(self, prompt: str) -> Any:
            """Return an empty completion; no test here reads the text."""
            raise NotImplementedError(prompt)

    monkeypatch.setattr(
        providers_mod,
        "build_provider",
        lambda cfg: (seen.append(cfg.model), _Stub())[1],
    )

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["llm"]["default"].update(
            model="creek-test-generation-model",
        ),
    )
    server_mod._build_reflect_llm_factory(tuned)(PrivacyTier.OPEN)

    assert seen == ["creek-test-generation-model"], (
        f"_build_reflect_llm_factory resolved {seen} for a vault routing "
        "generation to 'creek-test-generation-model'; it read the cwd instead."
    )


def test_server_author_llm_uses_the_served_vaults_routing_and_author_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_author_llm`` must read the served vault's routing *and* author block.

    Recorded at ``AuthorLLMClient.for_voice_or_none`` rather than at
    ``build_provider``: unlike its draft/compile siblings this builder never
    constructs a provider itself, it hands the router and the ``author``
    section to the Writing Desk's client. It is therefore the one builder that
    reads *two* config sections from the wrong vault.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    import creek_mcp.server as server_mod
    from creek.author import client as client_mod
    from creek.models import PrivacyTier

    _isolate(monkeypatch, tmp_path)
    seen: list[str | None] = []

    monkeypatch.setattr(
        client_mod.AuthorLLMClient,
        "for_voice_or_none",
        classmethod(
            lambda _cls, router, *, author, tier: (
                seen.append(router.resolve("generation", tier).model),
                None,
            )[1],
        ),
    )

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["llm"]["default"].update(
            model="creek-test-generation-model",
        ),
    )
    server_mod._build_author_llm(tuned, PrivacyTier.OPEN)

    assert seen == ["creek-test-generation-model"], (
        f"_build_author_llm resolved {seen} for a vault whose own config routes "
        "generation to 'creek-test-generation-model'. Both the model router and "
        "the author block came from the process cwd, not the served vault."
    )


# ---------------------------------------------------------------------------
# #1409 — creek_mcp/server.py: build_server must BIND the served vault
# ---------------------------------------------------------------------------

_SERVER_SEAMS: Final[list[tuple[str, str, str, dict[str, Any]]]] = [
    ("creek.draft", "_build_draft_llm", "draft_tool", {}),
    ("creek.author", "_build_author_llm", "author_tool", {"query": "q"}),
    (
        "creek.compile",
        "_build_compile_llm",
        "compile_tool",
        {
            "fragment_ids": ["frag-open"],
            "target_kind": "thread",
            "target_id": "thread-x",
            "target_title": "Thread X",
        },
    ),
    ("creek.reflect", "_build_reflect_llm_factory", "reflect_tool", {"content": "hi"}),
]
"""One row per ``(tool name, production builder, tool function, call args)``.

The four seams where ``build_server`` decides *which vault's* routing the
model that sees a caller's text is chosen by. The tool function is named so it
can be replaced by a stand-in: the subject here is the binding, not the tool.
"""


@pytest.mark.parametrize(
    ("tool_name", "builder_attr", "tool_attr", "args"),
    _SERVER_SEAMS,
    ids=[row[0] for row in _SERVER_SEAMS],
)
def test_build_server_binds_the_served_vault_into_its_default_llm_builder(
    tool_name: str,
    builder_attr: str,
    tool_attr: str,
    args: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each default builder must be bound to the vault ``build_server`` serves.

    The tests above prove a builder honours whatever vault it is *handed*.
    Nothing there looks at the seam that decides what it is handed, and that
    seam is where the whole server-side half of #1409 takes effect: a
    ``partial(_build_draft_llm, Path.cwd())`` would route vault A's text with
    vault B's model routing while every unit-level assertion stayed green.

    So this records the vault argument at the builder's own boundary and pins
    it to the served root by exact equality. The stand-in tool exists only to
    *reach* the factory — for draft, author and compile the tool is what
    invokes ``llm_factory(tier)``; ``creek.reflect`` calls ``reflect_factory()``
    in the closure itself, before the tool is entered.

    Args:
        tool_name: The registered MCP tool to call.
        builder_attr: The production builder ``build_server`` binds.
        tool_attr: The tool function on ``creek_mcp.server`` to stand in for.
        args: Arguments for the MCP call.
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    import asyncio

    import creek_mcp.server as server_mod
    from creek.models import PrivacyTier

    _isolate(monkeypatch, tmp_path)
    bound: list[Path] = []

    def _recording_builder(vault: Path, *_args: Any) -> Callable[..., None]:
        """Record the vault this builder was bound to and build nothing.

        Args:
            vault: The vault ``build_server`` bound in.
            *_args: The tier, which this seam does not assert on.

        Returns:
            An inert callable, so a caller may invoke the result.
        """
        bound.append(vault)
        return lambda *_a, **_k: None

    def _standin_tool(**kwargs: Any) -> dict[str, Any]:
        """Invoke the injected factory, then answer a minimal envelope.

        Args:
            **kwargs: The tool's real keyword arguments; only ``llm_factory``
                is used.

        Returns:
            An ``ok`` envelope in the shape every tool returns.
        """
        factory = kwargs.get("llm_factory")
        if callable(factory):
            factory(PrivacyTier.OPEN)
        return {"status": "ok", "tool": tool_name}

    monkeypatch.setattr(server_mod, builder_attr, _recording_builder)
    monkeypatch.setattr(server_mod, tool_attr, _standin_tool)

    served = _vault(tmp_path, "served")
    server = server_mod.build_server(transport=Transport.STDIO, vault_path=served)
    asyncio.run(server.call_tool(tool_name, args))

    assert bound == [served], (
        f"build_server bound {bound} into {builder_attr} while serving "
        f"{served}. The default factory for {tool_name} therefore resolves its "
        "model routing — including which provider a stage egresses to — from "
        "some other vault's config than the one it is acting on."
    )


# ---------------------------------------------------------------------------
# #1409 — POST /v1/reflections: the HTTP surface's egress decision
# ---------------------------------------------------------------------------

_REFLECT_ENTRY: Final = (
    "zz-vaultconfig-1409-zz I keep saying yes to things I do not want."
)
"""A benign inline entry: distinctive, and nothing the care guard escalates."""


class _RecordingProvider:
    """A ``build_provider`` result that answers an empty, well-formed turn.

    Attributes:
        available: Always ``True``, so the factory does not degrade to a
            refusal before the model is chosen.
    """

    available = True

    def complete(self, prompt: str) -> Any:
        """Return a parseable turn carrying no notes.

        Args:
            prompt: The rendered prompt, which no assertion here reads.

        Returns:
            An object exposing ``.text``, the shape the factory unwraps.
        """
        del prompt
        return type("_Completion", (), {"text": json.dumps({"notes": []})})()


def test_http_reflection_routes_with_the_configured_vaults_own_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /v1/reflections`` must egress to the *served* vault's provider.

    The one #1409 site reachable by a remote caller, and the one where reading
    the wrong config decides where a person's journal entry is sent. Before the
    fix ``_default_llm_factory`` was a bare thunk bound at request entry, so it
    could only call ``_build_reflect_llm_factory()`` — with no vault, hence the
    process cwd. The fix binds it inside :func:`creek_mcp.httpapi.reflect._reflect`,
    after the vault is resolved.

    Driven through the real route with **no** ``reflect_llm_factory``
    injected, because an injected factory is exactly what would let this
    default rot unnoticed — the whole suite for this endpoint stubs it. The
    recording sits at ``build_provider``, the provider boundary the egress
    decision crosses, so the assertion is on the routing the vault's own file
    produced rather than on the config layer that produced it.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from starlette.testclient import TestClient

    from creek.classify.llm import providers as providers_mod
    from tests.v1_api_support import (
        REFLECTIONS_PATH,
        build_app,
        headers,
        seed_vault,
    )

    _isolate(monkeypatch, tmp_path)
    seen: list[str | None] = []

    def _record(cfg: Any) -> _RecordingProvider:
        """Record the resolved model and return an available provider.

        Args:
            cfg: The ``LLMConfig`` the router resolved.

        Returns:
            The recording provider.
        """
        seen.append(cfg.model)
        return _RecordingProvider()

    monkeypatch.setattr(providers_mod, "build_provider", _record)

    tuned = seed_vault(
        _vault(
            tmp_path,
            "tuned",
            lambda data: data["llm"]["default"].update(
                model="creek-test-generation-model",
            ),
        ),
    )
    control = seed_vault(_vault(tmp_path, "control"))

    for vault in (tuned, control):
        with TestClient(build_app(vault_path=vault)) as http:
            http.post(
                REFLECTIONS_PATH,
                json={"content": _REFLECT_ENTRY},
                headers=headers(ceiling="open"),
            )

    assert seen and seen[0] == "creek-test-generation-model", (
        f"POST /v1/reflections resolved the models {seen}. The first vault's "
        "own creek_config.yaml routes generation to "
        "'creek-test-generation-model'; the model a remote caller's reflection "
        "text is sent to was chosen by the server's working directory instead."
    )
    assert seen[0] != seen[1], (
        f"POST /v1/reflections resolved the same model {seen} for two vaults "
        "whose configs differ, which means it read neither of them."
    )


# ---------------------------------------------------------------------------
# #1409 — link, classify and redact: three tools neither issue named
# ---------------------------------------------------------------------------


def test_link_tool_hands_run_link_the_linked_vaults_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.link`` must run with the config of the vault it is linking.

    ``vault_path`` is already a parameter of ``link_tool``, so this site had
    everything it needed to resolve correctly and simply did not.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek_mcp.tools import link as link_mod

    _isolate(monkeypatch, tmp_path)
    seen: list[str] = []

    def _record(*, vault_path: Path, config: CreekConfig, **_kw: Any) -> LinkSummary:
        seen.append(config.embeddings.model)
        return LinkSummary(method="temporal", fragment_count=0, link_count=0)

    monkeypatch.setattr(link_mod, "run_link", _record)

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["embeddings"].update(model="creek-test-embed-v1"),
    )
    control = _vault(tmp_path, "control")

    for vault in (tuned, control):
        link_mod.link_tool(vault_path=vault, method="temporal")

    assert seen == ["creek-test-embed-v1", CreekConfig().embeddings.model], (
        f"link_tool handed run_link the embeddings models {seen}; the first "
        "vault's own config sets 'creek-test-embed-v1'. The config came from "
        "the process cwd rather than from the vault being linked."
    )


def test_classify_tool_hands_run_classify_the_classified_vaults_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.classify`` must run with the config of the vault it classifies.

    The sharpest of the three unreported sites: this config selects
    ``llm.provider``, i.e. **which model is shown the vault's text**. A vault
    whose operator pinned classification to a local provider can have its
    fragments sent to a cloud one because the server was started elsewhere.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek_mcp.tools import classify as classify_mod

    _isolate(monkeypatch, tmp_path)
    seen: list[str] = []

    def _record(
        *,
        vault_path: Path,
        config: CreekConfig,
        **_kw: Any,
    ) -> ClassifySummary:
        seen.append(config.llm.default.provider)
        return ClassifySummary(
            total=0,
            classified=0,
            preserved_manual=0,
            preserved_llm=0,
            skipped_high_confidence=0,
            errors=(),
        )

    monkeypatch.setattr(classify_mod, "run_classify", _record)

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["llm"]["default"].update(provider="ollama"),
    )
    control = _vault(
        tmp_path,
        "control",
        lambda data: data["llm"]["default"].update(provider="anthropic"),
    )

    for vault in (tuned, control):
        classify_mod.classify_tool(vault_path=vault, method="rules")

    assert seen == ["ollama", "anthropic"], (
        f"classify_tool handed run_classify the providers {seen}, but the two "
        "vaults' own configs pin 'ollama' and 'anthropic' respectively. The "
        "provider that sees a vault's text was chosen by the process cwd."
    )


def test_redact_scan_applies_the_scanned_vaults_custom_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.redact`` must scan with the patterns the scanned vault defines.

    Fully behavioural — no boundary recording — because this is the surface
    where reading the wrong config is worst: ``redaction.custom_patterns`` is
    how an operator names the secrets specific to *their* material. Losing
    them means the scan reports a clean file that is not clean.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek_mcp.tools import redact as redact_mod

    _isolate(monkeypatch, tmp_path)

    tuned = _vault(
        tmp_path,
        "tuned",
        lambda data: data["redaction"]["custom_patterns"].update(
            lane_marker=r"CREEK-LANE-[0-9]{4}",
        ),
    )
    control = _vault(tmp_path, "control")

    counts: list[int] = []
    for vault in (tuned, control):
        target = vault / "01-Fragments" / "note.md"
        target.write_text("badge CREEK-LANE-1409 issued\n", encoding="utf-8")
        response = redact_mod.redact_scan_tool(
            vault_path=vault,
            input_path="01-Fragments/note.md",
            privacy_tier_ceiling=TierCeiling.ALL,
        )
        counts.append(response["statistics"]["total_findings"])

    assert counts[0] >= 1, (
        "redact_scan_tool found no match for CREEK-LANE-1409 in a vault whose "
        "own creek_config.yaml defines that exact custom pattern. The "
        "operator's patterns were read from the process cwd, so the scan "
        "reported a file as clean while their own rule matched it."
    )
    assert counts[1] == 0, (
        "The control vault defines no custom pattern yet matched anyway, so "
        "this fixture cannot tell the two configs apart."
    )


# ---------------------------------------------------------------------------
# #1410 — voice-check's fallback fingerprint ignores the vault's weighting
# ---------------------------------------------------------------------------

_FRAGMENT_BODY = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu."


def _voice_vault(
    tmp_path: Path,
    name: str,
    *,
    personal_authority: float,
) -> Path:
    """Build a vault of personal-tier self-authored fragments and no fingerprint.

    The absent ``00-Creek-Meta/voice-fingerprint.json`` is deliberate: it is
    what makes ``_resolve_voice_fingerprint`` fall through ``load_fingerprint``
    to ``build_fingerprint``, which is the one branch #1410 is about.

    Args:
        tmp_path: Pytest temporary directory.
        name: Directory name.
        personal_authority: Authority multiplier for the ``personal`` tier.

    Returns:
        The vault root.
    """

    def _edit(data: dict[str, Any]) -> None:
        weighting = data["voice_audience_weighting"]
        weighting["enabled"] = True
        weighting["privacy_tier_authority"] = {
            "open": 1.5,
            "personal": personal_authority,
            "unclassified": 0.75,
            "intimate": 0.0,
        }

    vault = _vault(tmp_path, name, _edit)
    meta = {
        "source": {"author": "self", "platform": "journal"},
        "privacy_tier": "personal",
        "representativeness": "self",
    }
    for index in range(3):
        vault.joinpath("01-Fragments", f"f{index}.md").write_text(
            "---\n" + yaml.safe_dump(meta, sort_keys=True) + "---\n\n" + _FRAGMENT_BODY,
            encoding="utf-8",
        )
    return vault


def test_voice_check_fallback_fingerprint_honours_the_vaults_audience_weighting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek voice-check`` must weight its built fingerprint as the vault says.

    The observable is a **mode change**, not a number, which is what makes this
    immune to float drift. ``_audience_factor`` multiplies by the tier's
    authority and ``_eligible_texts`` gates on ``weight > 0.0``, so an
    authority of ``0.0`` does not merely down-rank a fragment — it removes it
    from the corpus. A vault that zeroes ``personal`` and holds only
    personal-tier writing therefore has *no* eligible corpus, and the command
    must say so and skip the check.

    ``_resolve_voice_fingerprint`` calls ``build_fingerprint(vault_path,
    ai_style)`` with no ``audience_weighting=``, and that parameter defaults to
    ``None`` — which means **off** for this function. So the weighting section
    of the vault's config is inert on the fallback path and both vaults below
    score identically.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek.cli import app

    _isolate(monkeypatch, tmp_path)
    silenced = _voice_vault(tmp_path, "silenced", personal_authority=0.0)
    audible = _voice_vault(tmp_path, "audible", personal_authority=1.0)
    target = tmp_path / "draft.md"
    target.write_text(_FRAGMENT_BODY, encoding="utf-8")

    runner = CliRunner()
    results = [
        runner.invoke(
            app,
            ["voice-check", str(target), "--vault", str(vault), "--json"],
        )
        for vault in (silenced, audible)
    ]

    assert results[1].exit_code == 0, results[1].output

    # The control half: an authority of 1.0 keeps the corpus, so the command
    # scores the file and emits a JSON payload carrying a voice_distance.
    control_payload = results[1].output[results[1].output.find("{") :]
    assert "voice_distance" in json.loads(control_payload), (
        "The control vault (personal authority 1.0) produced no score, so this "
        f"fixture proves nothing about the other half. Output: {results[1].output}"
    )
    # The assertion #1410 is about.
    assert "No voice fingerprint available" in results[0].output, (
        "creek voice-check scored a file against a vault whose own "
        "creek_config.yaml gives personal-tier writing an authority of 0.0 — "
        "and that vault holds nothing but personal-tier writing, so its "
        "eligible corpus is empty. The fingerprint was built from the flat "
        "platform average because _resolve_voice_fingerprint never forwards "
        "voice_audience_weighting to build_fingerprint (#1410). Output: "
        f"{results[0].output}"
    )
    assert "voice_distance" not in results[0].output, (
        "creek voice-check emitted a voice_distance for a vault with an empty "
        "eligible corpus, so the weighting did not reach build_fingerprint."
    )


def test_voice_check_with_no_vault_flag_reads_the_resolved_roots_own_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--vault``, the config must come from the root actually scored.

    ``voice_check`` resolves two things separately: ``_resolve_vault(vault)``
    names the corpus, and ``_load_config_for_vault(...)`` names the knobs. Pass
    the *raw* ``--vault`` argument to the second and, with the flag omitted,
    they diverge — the corpus comes from the vault the working directory's
    ``creek_config.yaml`` points at, while the weighting comes from that
    working-directory file itself. Two different vaults, one answer.

    The fixture makes that divergence observable rather than theoretical: the
    cwd's config *names* the vault and leaves ``personal`` authority at the
    built-in 1.0, while the vault's own config zeroes it. So reading the wrong
    one is the difference between "this vault has no eligible corpus, skip" and
    a confidently reported ``voice_distance`` computed over writing the
    operator excluded from their own voice.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek.cli import app

    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    silenced = _voice_vault(tmp_path, "resolved-root", personal_authority=0.0)

    station = tmp_path / "station"
    station.mkdir()
    pointer: dict[str, Any] = CreekConfig().model_dump(mode="json")
    pointer["vault_path"] = str(silenced)
    station.joinpath("creek_config.yaml").write_text(
        yaml.dump(pointer, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(station)

    target = tmp_path / "unflagged-draft.md"
    target.write_text(_FRAGMENT_BODY, encoding="utf-8")

    result = CliRunner().invoke(app, ["voice-check", str(target), "--json"])

    assert "No voice fingerprint available" in result.output, (
        "creek voice-check scored a file against a vault whose own "
        "creek_config.yaml zeroes personal-tier authority, and which holds "
        "nothing but personal-tier writing. With --vault omitted the weighting "
        "was read from the working directory's creek_config.yaml — the file "
        "that merely *names* the vault — rather than from the resolved root "
        f"the corpus was loaded from. Output: {result.output}"
    )
    assert "voice_distance" not in result.output, (
        "creek voice-check emitted a voice_distance with --vault omitted, so "
        "the config it applied did not come from the resolved vault root."
    )


def test_new_vault_config_tests_are_neither_skipped_nor_deselected() -> None:
    """This module's tests must actually run; a skip reads as a pass.

    Cheap insurance against the failure mode where an import guard or a
    missing optional extra turns the whole module into a silent no-op.
    """
    assert "creek_mcp" in sys.modules, (
        "creek_mcp did not import, so every test in this module would be "
        "collected against a package that is not present."
    )
