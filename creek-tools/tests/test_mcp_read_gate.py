"""Structural guardrails for the MCP read surface's tier gates (#932).

``creek_mcp.read_gate`` is a *manifest plus two primitives*. The manifest
(:data:`~creek_mcp.read_gate.TOOL_POSTURES`) records, for every registered MCP
tool, how that tool relates to the caller's ``privacy_tier_ceiling``: it either
enforces the ceiling through a named gate, never reads unsupplied content, is
gated by an elevated auth token instead, or has a **known, tracked gap**. The
primitives (:func:`~creek_mcp.read_gate.refuse_above_ceiling` and
:func:`~creek_mcp.read_gate.iter_admitted_fragments`) are the two canonical
ways a tool may satisfy the ceiling, so gaps can be closed by adoption rather
than by re-deriving the policy per tool.

A manifest is only worth having if it cannot lie. Five layers keep it honest:

(a) **Surface completeness** — the manifest covers exactly the live tool list,
    derived from ``server.list_tools()`` rather than a second hardcoded copy,
    so a newly registered tool has to be triaged rather than silently
    inheriting "no posture recorded".
(b) **Ceiling-parameter presence** — every non-``AUTH_TOKEN`` tool exposes
    ``privacy_tier_ceiling``; the five ``creek.purge.*`` tools deliberately do
    not (see ``docs/mcp.md`` §"Purge tools deliberately do not accept a
    privacy_tier_ceiling"), and that asymmetry is pinned in both directions.
(c) **Anti-lying** — a ``GATED`` claim is verified against the implementation:
    the named symbol must exist in the named module *and* be called there.
(d) **Anti-whitewash** — a gap entry must name a positive tracking issue, and
    the tool's own module must mention that issue number, so a reader of the
    module learns the gap exists without consulting the manifest.
(e) **Anti-rot** — a tool recorded as an ungated gap must not already call a
    canonical gate primitive.

Injection drill (each step is expected to turn exactly one layer red):

1. Add a ``@server.tool(name="creek.dummy")`` to ``build_server`` → layer (a)
   fails.
2. Point a ``GATED`` entry's ``gate_symbol`` at a nonexistent name → layer (c)
   fails.
3. Drop the ``gap_issue`` from an ``UNGATED_KNOWN_GAP`` entry → layer (d)
   fails.

Deliberately absent: any runtime probe asserting that a known gap *still
leaks*. Such a test passes because the bug exists and breaks when it is fixed.
The gap issues (#968/#969/#970/#971) each carry their own runtime-probe
acceptance criterion.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import frontmatter
import pytest

from creek.models import PrivacyTier
from creek_mcp.read_gate import (
    CANONICAL_GATE_PRIMITIVES,
    GENERIC_ABOVE_CEILING_REASON,
    TOOL_POSTURES,
    ReadPosture,
    iter_admitted_fragments,
    refuse_above_ceiling,
)
from creek_mcp.server import build_server
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from mcp.types import Tool


# ---------------------------------------------------------------------------
# Pinned expectations
#
# The tool-name list is NOT duplicated here — layer (a) derives it from the
# live server. What is pinned below is the *posture* of the tools whose posture
# is load-bearing, so a later edit that silently downgrades a real gate to
# METADATA_ONLY (or upgrades a tracked gap to a gate it never grew) fails.
# ---------------------------------------------------------------------------

_EXPECTED_TOOL_COUNT = 23

_EXPECTED_PRIMITIVES = frozenset({"refuse_above_ceiling", "iter_admitted_fragments"})

_PINNED_GATE_ROWS = [
    ("creek.reflect", "creek_mcp.tools.reflect", "_above_ceiling"),
    ("creek.compile", "creek_mcp.tools.compile", "_sources_above_ceiling"),
    ("creek.wheel", "creek_mcp.tools.wheel", "to_privacy_override"),
    ("creek.mine", "creek_mcp.tools.mine", "to_privacy_override"),
    ("creek.draft", "creek_mcp.tools.draft", "to_privacy_override"),
    ("creek.author", "creek_mcp.tools.author", "to_privacy_override"),
]

_PINNED_GAPS = {
    "creek.report": 968,
    "creek.state.read": 969,
    "creek.state.render": 969,
    "creek.skills.refresh": 971,
}

_PINNED_JOURNAL_GAP_ISSUE = 970

_PINNED_AUTH_TOKEN_TOOLS = [
    "creek.purge.fragment",
    "creek.purge.source",
    "creek.purge.classifications",
    "creek.purge.daterange",
    "creek.purge.vault",
]

# Tool name → implementing module, for the tools whose posture note has to be
# read out of their own source (layers (d) and (e)). ``GATED`` entries carry
# their module in the manifest itself and are resolved from there.
_TOOL_MODULES = {
    "creek.report": "creek_mcp.tools.report",
    "creek.state.read": "creek_mcp.tools.state_read",
    "creek.state.render": "creek_mcp.tools.state",
    "creek.skills.refresh": "creek_mcp.tools.skills",
    "creek.journal": "creek_mcp.tools.journal",
}


# ---------------------------------------------------------------------------
# Manifest-derived parameter sets
# ---------------------------------------------------------------------------

_GATED_TOOLS = sorted(
    name for name, entry in TOOL_POSTURES.items() if entry.posture is ReadPosture.GATED
)
_UNGATED_GAP_TOOLS = sorted(
    name
    for name, entry in TOOL_POSTURES.items()
    if entry.posture is ReadPosture.UNGATED_KNOWN_GAP
)
_GAP_ISSUE_TOOLS = sorted(
    name for name, entry in TOOL_POSTURES.items() if entry.gap_issue is not None
)
_CEILING_TOOLS = sorted(
    name
    for name, entry in TOOL_POSTURES.items()
    if entry.posture is not ReadPosture.AUTH_TOKEN
)
_AUTH_TOKEN_TOOLS = sorted(
    name
    for name, entry in TOOL_POSTURES.items()
    if entry.posture is ReadPosture.AUTH_TOKEN
)


# ---------------------------------------------------------------------------
# Fragment-body canaries — unmistakable if they ever surface where they must
# not. Plain sentinels rather than realistic prose so a leak cannot be excused
# as "that phrase could have come from anywhere".
# ---------------------------------------------------------------------------

_OPEN_BODY = "CANARY-OPEN-BODY-5c04"
_PERSONAL_BODY = "CANARY-PERSONAL-BODY-2b71"
_INTIMATE_BODY = "CANARY-INTIMATE-BODY-9f3a"
_PERSONAL_TITLE = "Personal note"
_PERSONAL_SUMMARY = f"[Personal-tier summary: {_PERSONAL_TITLE}]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_name(node: ast.Call) -> str | None:
    """Resolve a call's callee to a bare symbol name.

    Args:
        node: The call node to inspect.

    Returns:
        ``func.id`` for a direct call (``gate(x)``), ``func.attr`` for a
        qualified one (``mod.gate(x)``), or ``None`` when the callee is a
        dynamic expression no static reader can name.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_symbol(module: ModuleType, symbol: str) -> bool:
    """Return whether *module* contains a call to *symbol*.

    Deliberately looks for a *call*, not a definition or an import: a module
    can import (or even define) a gate and never invoke it, which is exactly
    the failure mode layers (c) and (e) exist to detect.

    Args:
        module: The imported tool module to scan.
        symbol: The callee name to look for.

    Returns:
        ``True`` when at least one call site names *symbol*.
    """
    tree = ast.parse(inspect.getsource(module))
    return any(
        isinstance(node, ast.Call) and _call_name(node) == symbol
        for node in ast.walk(tree)
    )


def _module_path_for(tool: str) -> str:
    """Return the dotted path of the module implementing *tool*.

    ``GATED`` entries name their module in the manifest; everything else is
    resolved through :data:`_TOOL_MODULES`, since the tool-name → file-name
    mapping is not mechanical (``creek.state.read`` lives in ``state_read.py``,
    ``creek.skills.refresh`` in ``skills.py``).

    Args:
        tool: The registered MCP tool name.

    Returns:
        The dotted module path.
    """
    entry = TOOL_POSTURES[tool]
    if entry.gate_module is not None:
        return entry.gate_module
    path = _TOOL_MODULES.get(tool)
    assert path is not None, (
        f"{tool} carries a read-posture note that must be checked against its "
        "own source, but this test cannot tell which module implements it. "
        f"Add {tool!r} to _TOOL_MODULES in this file."
    )
    return path


def _import_tool_module(tool: str) -> ModuleType:
    """Import and return the module implementing *tool*.

    Args:
        tool: The registered MCP tool name.

    Returns:
        The imported module object.
    """
    return importlib.import_module(_module_path_for(tool))


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str,
    body: str,
    privacy_tier: str,
) -> Path:
    """Write one classified fragment under ``01-Fragments/Notes``.

    Args:
        vault: Vault root.
        frag_id: Fragment id, also used as the file stem.
        title: Fragment title — what a personal-tier summary is built from.
        body: Markdown body (a canary string in these tests).
        privacy_tier: The fragment's ``privacy_tier`` front-matter value.

    Returns:
        The path the fragment was written to.
    """
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": privacy_tier,
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return target


def _admitted_by_id(vault: Path, ceiling: TierCeiling) -> dict[str, tuple[Path, str]]:
    """Return ``{fragment_id: (path, body)}`` for everything admitted at *ceiling*.

    Args:
        vault: Vault root.
        ceiling: The caller's declared ceiling.

    Returns:
        A mapping keyed by fragment id, so a *dropped* fragment shows up as a
        missing key rather than an off-by-one in a list comparison.
    """
    return {
        fragment.id: (path, body)
        for path, fragment, body in iter_admitted_fragments(vault, ceiling)
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registered_tools(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Tool]:
    """Return the live MCP tool surface keyed by tool name.

    Built once per module from a throwaway vault: the schemas are derived from
    the tool signatures at registration time, so nothing here depends on vault
    contents. Module-scoped because these tests read the surface and never
    mutate it.
    """
    vault = tmp_path_factory.mktemp("read-gate-surface")
    for sub in ("00-Creek-Meta/audit", "01-Fragments/Notes", "creek-skills"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    return {tool.name: tool for tool in tools}


@pytest.fixture
def seeded_vault(tmp_path: Path) -> Path:
    """Return a vault holding one open, one personal and one intimate fragment.

    Each body is a distinct canary so a tier-filter regression is visible as a
    specific string in a specific place, not as a count that happens to differ.
    """
    _write_fragment(
        tmp_path,
        frag_id="frag-open",
        title="Open note",
        body=_OPEN_BODY,
        privacy_tier="open",
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-personal",
        title=_PERSONAL_TITLE,
        body=_PERSONAL_BODY,
        privacy_tier="personal",
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-intimate",
        title="Intimate note",
        body=_INTIMATE_BODY,
        privacy_tier="intimate",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Layer (a) — surface completeness
# ---------------------------------------------------------------------------


def test_tool_postures_cover_every_registered_tool(
    registered_tools: dict[str, Tool],
) -> None:
    """The manifest matches the live tool surface exactly, in both directions.

    Derived from ``server.list_tools()`` rather than a second hardcoded list,
    so the only way to add an MCP tool is to state, in
    ``creek_mcp.read_gate.TOOL_POSTURES``, how it relates to the caller's
    ceiling. "Nobody wrote a posture down" is the state this whole module
    exists to make impossible — an untriaged tool is indistinguishable from a
    tool somebody decided needs no gate.

    The reverse direction matters too: a manifest entry for a tool that no
    longer exists is a claim about nothing, and it inflates the apparent
    coverage of the surface.
    """
    manifest = set(TOOL_POSTURES)
    live = set(registered_tools)
    untriaged = live - manifest
    stale = manifest - live
    assert not untriaged, (
        f"MCP tool(s) registered with no read posture: {sorted(untriaged)}. "
        "Triage each one in creek_mcp.read_gate.TOOL_POSTURES: name the gate "
        "it calls (GATED), record it as NO_UNSUPPLIED_READ / "
        "CALLER_NAMED_PATHS / METADATA_ONLY / AUTH_TOKEN with a rationale, or "
        "file a gap issue and record it as UNGATED_KNOWN_GAP with gap_issue."
    )
    assert not stale, (
        "TOOL_POSTURES claims a posture for unregistered tool(s): "
        f"{sorted(stale)}. Delete the entry, or restore the tool."
    )
    assert live == manifest


def test_registered_tool_count_is_pinned(registered_tools: dict[str, Tool]) -> None:
    """The MCP surface is 23 tools; growing it is a deliberate act.

    A bare count is a cheap tripwire against the one edit the set-equality test
    above cannot see: adding a tool *and* a matching posture entry in a single
    change, where the posture was chosen to make the test quiet rather than to
    describe the tool. Bumping this number forces the author to look at the
    surface as a whole.
    """
    assert len(registered_tools) == _EXPECTED_TOOL_COUNT
    assert len(TOOL_POSTURES) == _EXPECTED_TOOL_COUNT


@pytest.mark.parametrize(("tool", "module", "symbol"), _PINNED_GATE_ROWS)
def test_pinned_gate_claims_are_not_downgraded(
    tool: str,
    module: str,
    symbol: str,
) -> None:
    """The tools that really enforce the ceiling keep saying so.

    Without this pin, a future edit could quietly relabel a genuinely gated
    tool as ``METADATA_ONLY`` — which reads as "no gate needed" — and every
    other layer would still pass, because the anti-lying checks only fire on
    entries that *claim* a gate. Pinning the exact ``(module, symbol)`` pair
    additionally stops the claim from being redirected at some other callable
    that happens to be invoked nearby.
    """
    entry = TOOL_POSTURES[tool]
    assert entry.posture is ReadPosture.GATED, (
        f"{tool} was recorded as {entry.posture.value!r}, but it enforces the "
        f"ceiling through {module}.{symbol}. Downgrading its posture hides a "
        "real gate from every reader of the manifest."
    )
    assert (entry.gate_module, entry.gate_symbol) == (module, symbol)


@pytest.mark.parametrize(("tool", "issue"), sorted(_PINNED_GAPS.items()))
def test_pinned_gaps_keep_their_posture_and_issue(tool: str, issue: int) -> None:
    """The known-ungated tools stay labelled as gaps against their own issues.

    These four read vault content without honouring the caller's ceiling. The
    posture is the honest record of that, and the issue number is the promise
    that someone is on the hook for it. Relabelling either one — without the
    code changing — converts a tracked gap into an invisible one.
    """
    entry = TOOL_POSTURES[tool]
    assert entry.posture is ReadPosture.UNGATED_KNOWN_GAP, (
        f"{tool} was recorded as {entry.posture.value!r}, but it reads vault "
        f"content without honouring the ceiling — a gap tracked by #{issue}. "
        "If the gap is genuinely closed, point the entry at the gate that "
        "closed it (GATED) rather than relabelling the posture."
    )
    assert entry.gap_issue == issue


def test_journal_is_pinned_as_no_unsupplied_read() -> None:
    """``creek.journal`` never returns content the caller did not supply.

    Its posture is ``NO_UNSUPPLIED_READ`` rather than a gate: the write-side is
    guarded by ``write_tier_allowed``, and the read-side gap it still carries
    (#970) is recorded on the same entry. Pinning both together keeps the two
    halves of that story from drifting apart — a reader must not be able to
    conclude "journal has no gap" from the posture alone.
    """
    entry = TOOL_POSTURES["creek.journal"]
    assert entry.posture is ReadPosture.NO_UNSUPPLIED_READ
    assert entry.gap_issue == _PINNED_JOURNAL_GAP_ISSUE


@pytest.mark.parametrize("tool", _PINNED_AUTH_TOKEN_TOOLS)
def test_purge_tools_are_pinned_as_auth_token(tool: str) -> None:
    """The five ``creek.purge.*`` tools are token-gated, not tier-gated.

    Purge is not tier-scoped — it destroys vault content rather than reading it
    — so it is gated fail-closed by ``CREEK_MCP_ELEVATED_TOKEN`` instead. The
    posture must say ``AUTH_TOKEN`` and not something ceiling-shaped, because
    layer (b) reads this field to decide whether the *absence* of a
    ``privacy_tier_ceiling`` parameter is deliberate or a bug.
    """
    assert TOOL_POSTURES[tool].posture is ReadPosture.AUTH_TOKEN


# ---------------------------------------------------------------------------
# Layer (b) — ceiling-parameter presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", _CEILING_TOOLS)
def test_non_auth_token_tools_accept_a_privacy_tier_ceiling(
    tool: str,
    registered_tools: dict[str, Tool],
) -> None:
    """Every tool that is not token-gated exposes ``privacy_tier_ceiling``.

    The ceiling is the caller's *only* handle on how much of the vault a call
    may reach. A tool that does not accept it cannot be asked to restrict
    itself, and the remote cap in ``_BoundedFastMCP.call_tool`` silently reads
    ``OPEN`` for it. Driven off the manifest rather than a name prefix, so the
    invariant is tied to the posture a human recorded.
    """
    schema = registered_tools[tool].inputSchema
    assert "privacy_tier_ceiling" in schema["properties"], (
        f"{tool} has posture {TOOL_POSTURES[tool].posture.value!r} but exposes "
        "no privacy_tier_ceiling parameter. Either add the parameter or "
        "justify the omission with an AUTH_TOKEN posture."
    )


@pytest.mark.parametrize("tool", _AUTH_TOKEN_TOOLS)
def test_auth_token_tools_expose_no_privacy_tier_ceiling(
    tool: str,
    registered_tools: dict[str, Tool],
) -> None:
    """Token-gated tools deliberately do **not** take a ceiling.

    Documented in ``docs/mcp.md`` §"Purge tools deliberately do not accept a
    privacy_tier_ceiling". The asymmetry is asserted rather than merely skipped
    because adding the parameter here would be actively misleading: it would
    advertise a tier gate that ``creek_mcp.tools.purge`` does not implement,
    inviting a caller to believe ``ceiling=open`` limits what a purge destroys.
    """
    schema = registered_tools[tool].inputSchema
    assert "privacy_tier_ceiling" not in schema["properties"], (
        f"{tool} is AUTH_TOKEN-gated but now advertises a "
        "privacy_tier_ceiling. A ceiling parameter nobody enforces is worse "
        "than none; see docs/mcp.md."
    )


# ---------------------------------------------------------------------------
# Layer (c) — GATED claims verified against the implementation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", _GATED_TOOLS)
def test_gated_entries_declare_both_module_and_symbol(tool: str) -> None:
    """A ``GATED`` claim is only checkable if it names where to look.

    ``gate_module`` and ``gate_symbol`` are what turn "this tool is gated" from
    an assertion into a verifiable statement; a ``GATED`` entry missing either
    one would make layers (c) below vacuously pass.
    """
    entry = TOOL_POSTURES[tool]
    assert entry.gate_module is not None, f"{tool} claims GATED with no module"
    assert entry.gate_symbol is not None, f"{tool} claims GATED with no symbol"


def test_non_gated_entries_declare_no_gate() -> None:
    """Only ``GATED`` entries carry gate coordinates.

    A leftover ``gate_symbol`` on a downgraded entry would read, to a human
    skimming the manifest, as evidence of a gate that the posture itself
    disclaims — and it would be checked by nothing.
    """
    for name, entry in TOOL_POSTURES.items():
        if entry.posture is ReadPosture.GATED:
            continue
        assert entry.gate_module is None, (
            f"{name} is {entry.posture.value!r} but names gate module "
            f"{entry.gate_module!r}"
        )
        assert entry.gate_symbol is None, (
            f"{name} is {entry.posture.value!r} but names gate symbol "
            f"{entry.gate_symbol!r}"
        )


@pytest.mark.parametrize("tool", _GATED_TOOLS)
def test_gated_tools_expose_their_named_gate_symbol(tool: str) -> None:
    """The named gate exists in the named module.

    The cheapest way for the manifest to lie is to name a gate that was
    renamed, moved, or never existed. Importing the module and asking for the
    attribute catches that on the next test run instead of at the next audit.
    """
    entry = TOOL_POSTURES[tool]
    module = _import_tool_module(tool)
    assert entry.gate_symbol is not None
    assert hasattr(module, entry.gate_symbol), (
        f"{tool} claims to be gated by {entry.gate_module}."
        f"{entry.gate_symbol}, but that module has no such attribute. "
        "Either the gate moved (update the manifest) or it is gone (the tool "
        "is no longer gated and the posture is now a lie)."
    )


@pytest.mark.parametrize("tool", _GATED_TOOLS)
def test_gated_tools_actually_call_their_named_gate_symbol(tool: str) -> None:
    """The named gate is *invoked*, not merely importable.

    A gate that exists but is never called protects nothing, and this is the
    realistic decay path: a refactor moves the call out of the request path
    while the helper — and its docstring, and the manifest entry pointing at it
    — survive untouched. The AST walk requires a real call site, so an import,
    a re-export, or a mention in a comment cannot satisfy it.
    """
    entry = TOOL_POSTURES[tool]
    module = _import_tool_module(tool)
    assert entry.gate_symbol is not None
    assert _calls_symbol(module, entry.gate_symbol), (
        f"{tool} claims to be gated by {entry.gate_symbol}, but "
        f"{entry.gate_module} never calls it. A gate that is not invoked on "
        "the request path enforces nothing."
    )


# ---------------------------------------------------------------------------
# Layer (d) — gap-entry integrity
# ---------------------------------------------------------------------------


def test_every_ungated_gap_declares_a_tracking_issue() -> None:
    """``UNGATED_KNOWN_GAP`` without an issue is an *unknown* gap.

    The posture's whole claim is that the gap is tracked. Dropping
    ``gap_issue`` keeps the reassuring word "KNOWN" while removing the only
    thing that makes it true, and it also exempts the entry from the
    module-mention check below.
    """
    for tool in _UNGATED_GAP_TOOLS:
        entry = TOOL_POSTURES[tool]
        assert entry.gap_issue is not None, (
            f"{tool} is recorded as a known gap with no tracking issue. File "
            "one and record its number in gap_issue, or pick a posture that "
            "does not promise the gap is tracked."
        )


@pytest.mark.parametrize("tool", _GAP_ISSUE_TOOLS)
def test_gap_issue_numbers_are_positive_integers(tool: str) -> None:
    """A tracking issue is a positive integer, not a placeholder.

    Guards the obvious escape hatches — ``0``, ``-1``, or a bool sneaking
    through ``int`` — that would satisfy a bare "is not None" check while
    pointing at no issue anyone can open.
    """
    issue = TOOL_POSTURES[tool].gap_issue
    assert isinstance(issue, int)
    assert not isinstance(issue, bool)
    assert issue > 0


@pytest.mark.parametrize("tool", _GAP_ISSUE_TOOLS)
def test_gap_issues_are_named_in_their_own_tool_module(tool: str) -> None:
    """The tool's own source names the issue tracking its gap.

    The manifest is a file most readers of ``creek_mcp/tools/report.py`` will
    never open. Requiring the literal ``#<issue>`` in the module itself means
    the person editing the tool learns about the gap where they already are,
    and cannot extend the tool believing its reads are ceiling-filtered.
    """
    issue = TOOL_POSTURES[tool].gap_issue
    assert issue is not None
    source = inspect.getsource(_import_tool_module(tool))
    assert f"#{issue}" in source, (
        f"{_module_path_for(tool)} does not mention #{issue}. Add a posture "
        "note to the module docstring naming the gap and its issue, so a "
        "reader of the tool learns the ceiling is not enforced there."
    )


# ---------------------------------------------------------------------------
# Layer (e) — drift protection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", _UNGATED_GAP_TOOLS)
def test_ungated_gap_tools_do_not_call_a_canonical_gate_primitive(tool: str) -> None:
    """A tool recorded as an ungated gap must not already use a canonical gate.

    This is the anti-rot direction. When someone closes one of these gaps by
    adopting :func:`~creek_mcp.read_gate.refuse_above_ceiling` or
    :func:`~creek_mcp.read_gate.iter_admitted_fragments`, this test fails until
    they also flip the posture to ``GATED`` — so the manifest cannot rot into
    advertising a gap that was fixed months ago, which would send an auditor
    hunting a hole that is not there and leave a real gate unverified by layer
    (c).

    Scoped deliberately to the two *new* primitives and not to
    ``tier_allowed``/``write_tier_allowed``: ``creek.journal`` legitimately
    calls ``write_tier_allowed`` for its write-side gate while still carrying a
    read-side gap, so keying off the older helpers would make this test
    unsatisfiable for an honest entry.
    """
    module = _import_tool_module(tool)
    for primitive in sorted(CANONICAL_GATE_PRIMITIVES):
        assert not _calls_symbol(module, primitive), (
            f"{tool} is recorded as an ungated known gap "
            f"(#{TOOL_POSTURES[tool].gap_issue}) but {_module_path_for(tool)} "
            f"already calls {primitive}. If the gap is closed, change the "
            "posture to GATED and name the gate; if it is not, this call is "
            "misleading."
        )


def test_canonical_gate_primitives_name_the_exported_callables() -> None:
    """The primitive names are the two callables this module actually exports.

    :data:`~creek_mcp.read_gate.CANONICAL_GATE_PRIMITIVES` is consumed as
    *strings* by the AST walk above, so a typo or a rename would silently
    weaken layer (e) into scanning for a symbol nothing can ever call.
    Resolving each name back to a callable on the module closes that loop.
    """
    read_gate = importlib.import_module("creek_mcp.read_gate")
    assert CANONICAL_GATE_PRIMITIVES == _EXPECTED_PRIMITIVES
    for primitive in CANONICAL_GATE_PRIMITIVES:
        assert callable(getattr(read_gate, primitive))


# ---------------------------------------------------------------------------
# ``refuse_above_ceiling`` — the refusal primitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "ceiling"),
    [
        (PrivacyTier.OPEN, TierCeiling.OPEN),
        (PrivacyTier.UNCLASSIFIED, TierCeiling.OPEN),
        (PrivacyTier.OPEN, TierCeiling.PERSONAL),
        (PrivacyTier.PERSONAL, TierCeiling.PERSONAL),
        (PrivacyTier.INTIMATE, TierCeiling.INTIMATE),
        (PrivacyTier.PERSONAL, TierCeiling.ALL),
        (PrivacyTier.INTIMATE, TierCeiling.ALL),
    ],
)
def test_refuse_above_ceiling_admits_content_within_the_ceiling(
    tier: PrivacyTier,
    ceiling: TierCeiling,
) -> None:
    """Admitted content produces no refusal — ``None`` means "carry on".

    The rows mirror ``tier_allowed``'s matrix because this primitive must
    *delegate* to it rather than re-deriving admission. A second, private
    ranking inside the read gate is exactly how two parts of the MCP surface
    end up disagreeing about whether a fragment is readable.

    ``unclassified`` at ``ceiling=open`` is included on purpose: the MCP-side
    ranking admits it (``creek_mcp.tier_ceiling._TIER_RANK``), which differs
    from the reader-caution ranking in ``creek.classify.privacy_filter`` — a
    deliberate, documented split that this primitive must not quietly
    re-decide.
    """
    assert (
        refuse_above_ceiling(tool="creek.reflect", content_tier=tier, ceiling=ceiling)
        is None
    )


@pytest.mark.parametrize("ceiling", list(TierCeiling))
def test_refuse_above_ceiling_passes_raw_caller_content(ceiling: TierCeiling) -> None:
    """``content_tier=None`` is never above the ceiling, at any ceiling.

    ``None`` is what a tool has when the caller supplied the text inline: it
    carries no classification and it is the caller's own words, so refusing it
    would mean refusing callers access to what they just typed. Mirrors
    ``creek_mcp.tools.reflect._above_ceiling``, which this primitive
    generalises. Parametrised over every ceiling so the ``None`` short-circuit
    cannot be implemented as a quirk of one branch.
    """
    assert (
        refuse_above_ceiling(tool="creek.reflect", content_tier=None, ceiling=ceiling)
        is None
    )


def test_refuse_above_ceiling_returns_the_canonical_refusal_payload() -> None:
    """An above-ceiling read yields exactly the shared refusal dict.

    Equality against ``refusal_response(...)`` — rather than a spot-check of
    ``status`` — pins the whole payload: the same four keys every other MCP
    refusal uses, no extras, and the fixed generic reason. An extra key here
    would be a new, unreviewed channel out of the gate.
    """
    response = refuse_above_ceiling(
        tool="creek.reflect",
        content_tier=PrivacyTier.INTIMATE,
        ceiling=TierCeiling.OPEN,
    )
    assert response == refusal_response(
        tool="creek.reflect",
        ceiling=TierCeiling.OPEN,
        reason=GENERIC_ABOVE_CEILING_REASON,
    )


@pytest.mark.parametrize(
    ("tier", "ceiling"),
    [
        (PrivacyTier.PERSONAL, TierCeiling.OPEN),
        (PrivacyTier.INTIMATE, TierCeiling.OPEN),
        (PrivacyTier.INTIMATE, TierCeiling.PERSONAL),
    ],
)
def test_refuse_above_ceiling_never_echoes_the_content_tier(
    tier: PrivacyTier,
    ceiling: TierCeiling,
) -> None:
    """The refusal leaks no bit about the tier of content the caller cannot read.

    This is the load-bearing property, and the reason
    ``creek_mcp/tools/reflect.py`` carries the comment block at lines 403-431:
    the offending tier is derived from content the caller is *not* admitted to,
    so echoing it turns every refusal into a tier-classification oracle over
    the corpus — probe an id, read the tier back out of the error.

    Asserted against the serialised response rather than the ``reason`` field
    alone, because the leak is just as real in an extra ``content_tier`` key
    added "for debugging". The refusal must be indistinguishable across every
    above-ceiling tier at a given ceiling.
    """
    response = refuse_above_ceiling(
        tool="creek.reflect", content_tier=tier, ceiling=ceiling
    )
    assert response is not None
    serialised = json.dumps(response)
    assert tier.value not in serialised, (
        f"the refusal for {tier.value!r} content at ceiling {ceiling.value!r} "
        f"echoes the content tier: {serialised}. That is a one-bit oracle over "
        "content the caller is not admitted to."
    )
    assert set(response) == {"status", "tool", "tier_ceiling", "reason"}
    assert response["reason"] == GENERIC_ABOVE_CEILING_REASON


def test_generic_above_ceiling_reason_names_no_tier() -> None:
    """The fixed reason string mentions no privacy tier at all.

    The oracle test above compares against the tier that was actually passed;
    this one closes the remaining hole, where a well-meaning reason like
    "personal or intimate content was excluded" would be tier-free for the
    *offending* tier while still narrowing the corpus for an attacker.
    """
    for tier in PrivacyTier:
        assert tier.value not in GENERIC_ABOVE_CEILING_REASON, (
            f"GENERIC_ABOVE_CEILING_REASON names the {tier.value!r} tier: "
            f"{GENERIC_ABOVE_CEILING_REASON!r}"
        )


def test_refuse_above_ceiling_fails_closed_on_an_unrecognised_tier() -> None:
    """An unrecognised tier is handled as ``intimate``, never as ``open``.

    A tier value the ranking has never heard of is a tier nobody can vouch for.
    ``tier_sensitivity`` ranks it with ``intimate``, so it must be refused
    everywhere ``intimate`` is refused (``open`` and ``personal``) and admitted
    only where ``intimate`` is admitted.

    The admitted rows are asserted as *equality with ``intimate``'s outcome*
    rather than as a flat "refused at every ceiling but ``all``". That keeps
    the test honest about delegation: making this primitive stricter than
    ``tier_allowed`` would give the read gate a private admission rule that can
    drift from the predicate the rest of the MCP surface uses, and drift is the
    failure this module exists to prevent. The unknown value must also never
    appear in the refusal — an unrecognised tier is still content the caller
    cannot read.
    """
    unknown = cast("PrivacyTier", "not-a-tier")
    for ceiling in (TierCeiling.OPEN, TierCeiling.PERSONAL):
        response = refuse_above_ceiling(
            tool="creek.reflect", content_tier=unknown, ceiling=ceiling
        )
        assert response is not None, (
            f"an unrecognised tier was admitted at ceiling {ceiling.value!r}; "
            "unknown tiers must fail closed with intimate"
        )
        assert "not-a-tier" not in json.dumps(response)
    for ceiling in (TierCeiling.INTIMATE, TierCeiling.ALL):
        as_unknown = refuse_above_ceiling(
            tool="creek.reflect", content_tier=unknown, ceiling=ceiling
        )
        as_intimate = refuse_above_ceiling(
            tool="creek.reflect",
            content_tier=PrivacyTier.INTIMATE,
            ceiling=ceiling,
        )
        assert as_intimate is None
        assert as_unknown == as_intimate


# ---------------------------------------------------------------------------
# ``iter_admitted_fragments`` — the corpus-walk primitive
# ---------------------------------------------------------------------------


def test_iter_admitted_fragments_drops_intimate_at_the_open_ceiling(
    seeded_vault: Path,
) -> None:
    """At ``ceiling=open`` an intimate fragment is not yielded at all.

    Not yielded — not yielded-with-an-empty-body, not yielded-as-a-stub. A
    caller at the open ceiling must not learn that the fragment exists, since
    its id and title are themselves content above the ceiling. The canary
    assertion covers the sloppier failure where the fragment is filtered out of
    the ids but its body still rides along in some other slot.
    """
    admitted = _admitted_by_id(seeded_vault, TierCeiling.OPEN)
    assert "frag-intimate" not in admitted
    assert set(admitted) == {"frag-open", "frag-personal"}
    blob = json.dumps({key: value[1] for key, value in admitted.items()})
    assert _INTIMATE_BODY not in blob


def test_iter_admitted_fragments_summarises_personal_at_the_open_ceiling(
    seeded_vault: Path,
) -> None:
    """At ``ceiling=open`` a personal fragment yields a title-only summary.

    This is the Ontology §13.2 promise, delegated to
    ``creek.classify.privacy_filter.filter_fragments_by_tier`` via
    ``to_privacy_override``: personal content contributes that it exists and
    what it is called, never its body. Exact string equality against
    ``_summarize_personal``'s output — a substring or truthiness check would
    pass for a body that merely *starts* with the summary.

    The open fragment's full body is asserted in the same test so a filter that
    summarised everything (trivially leak-free, and useless) cannot pass.
    """
    admitted = _admitted_by_id(seeded_vault, TierCeiling.OPEN)
    assert admitted["frag-personal"][1].strip() == _PERSONAL_SUMMARY
    assert _PERSONAL_BODY not in admitted["frag-personal"][1]
    assert admitted["frag-open"][1].strip() == _OPEN_BODY


def test_iter_admitted_fragments_yields_the_fragment_path(
    seeded_vault: Path,
) -> None:
    """Each yielded triple carries the path the fragment was loaded from.

    Callers need the path to attribute, re-read, or link the fragment; a
    primitive that yields the right bodies against the wrong paths would have
    every tier assertion above still pass.
    """
    admitted = _admitted_by_id(seeded_vault, TierCeiling.ALL)
    notes = seeded_vault / "01-Fragments" / "Notes"
    assert admitted["frag-open"][0] == notes / "frag-open.md"
    assert admitted["frag-personal"][0] == notes / "frag-personal.md"
    assert admitted["frag-intimate"][0] == notes / "frag-intimate.md"


def test_iter_admitted_fragments_yields_full_bodies_at_the_all_ceiling(
    seeded_vault: Path,
) -> None:
    """At ``ceiling=all`` every fragment is yielded with its full body.

    The permissive direction has to be pinned too: a gate that drops everything
    is not a gate, it is an outage, and it would satisfy every leak assertion
    in this file. ``all`` is the explicit operator override, so nothing is
    dropped and nothing is summarised.
    """
    admitted = _admitted_by_id(seeded_vault, TierCeiling.ALL)
    assert set(admitted) == {"frag-open", "frag-personal", "frag-intimate"}
    assert admitted["frag-open"][1].strip() == _OPEN_BODY
    assert admitted["frag-personal"][1].strip() == _PERSONAL_BODY
    assert admitted["frag-intimate"][1].strip() == _INTIMATE_BODY
    blob = json.dumps({key: value[1] for key, value in admitted.items()})
    assert "[Personal-tier summary" not in blob


@pytest.mark.parametrize("ceiling", list(TierCeiling))
def test_iter_admitted_fragments_tolerates_a_missing_fragments_directory(
    tmp_path: Path,
    ceiling: TierCeiling,
) -> None:
    """A vault with no ``01-Fragments`` yields nothing instead of crashing.

    This is the new-user and freshly-``creek init``-ed case. Every tool that
    adopts this primitive inherits its empty-vault behaviour, so raising here
    would turn "you have no fragments yet" into a tool-wide error at every
    ceiling.
    """
    assert list(iter_admitted_fragments(tmp_path, ceiling)) == []
