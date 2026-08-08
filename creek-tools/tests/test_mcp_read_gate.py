"""Structural and behavioural guardrails for the MCP read surface's tier gates (#932).

``creek_mcp.read_gate`` is a *manifest plus two primitives*. The manifest
(:data:`~creek_mcp.read_gate.TOOL_POSTURES`) records, for every registered MCP
tool, how that tool relates to the caller's ``privacy_tier_ceiling``: it either
enforces the ceiling through a named gate, never reads unsupplied content, is
gated by an elevated auth token instead, or has a **known, tracked gap**. The
primitives (:func:`~creek_mcp.read_gate.refuse_above_ceiling` and
:func:`~creek_mcp.read_gate.iter_admitted_fragments`) are the two canonical
ways a tool may satisfy the ceiling, so gaps can be closed by adoption rather
than by re-deriving the policy per tool.

A manifest is only worth having if it cannot lie. Six layers keep it honest:

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
(f) **Runtime canary probe** — every ``GATED`` tool is *called for real* at
    ``ceiling=open`` against a vault holding an ``intimate`` fragment whose
    title, body and tags each carry a unique sentinel, and the sentinel must
    appear nowhere in the JSON-serialised response. Driven off a manifest of
    its own (:data:`_RUNTIME_PROBES` / :data:`_PROBE_EXEMPT`) so a newly
    ``GATED`` tool must either grow a probe or record a justified exemption —
    the same forcing function as layer (a), one level deeper.

Layer (f) exists because layers (a)-(e) are **structural**. Between them they
prove that a gate is declared in the manifest, exists in the named module, and
is invoked there. None of that can prove the gate is the *only* path to the
corpus: a second, ungated read added alongside the still-present, still-called
gate satisfies every structural check. Only calling the tool and looking at
what comes back can see that, which is what (f) does.

Injection drill (each step is expected to turn exactly one layer red):

1. Add a ``@server.tool(name="creek.dummy")`` to ``build_server`` → layer (a)
   fails.
2. Point a ``GATED`` entry's ``gate_symbol`` at a nonexistent name → layer (c)
   fails.
3. Drop the ``gap_issue`` from an ``UNGATED_KNOWN_GAP`` entry → layer (d)
   fails.
4. In ``creek_mcp/tools/wheel.py``, leave the ``to_privacy_override(...)`` gate
   call exactly where it is and *additionally* return every raw fragment body
   from ``wheel_tool``::

       "unclassified": counts.get(Frequency.UNCLASSIFIED, 0),
       "leak": [
           b
           for _p, _f, b, _m in iter_vault_fragments(vault_path / _FRAGMENTS_SUBDIR)
       ],

   → layer (f) fails, and **only** layer (f). This mutation was run against
   the file before (f) existed: 101 passed here, 8 passed in
   ``tests/test_mcp_wheel.py``, nothing caught it. Layers (a)-(e) stay green
   by construction — the manifest still names ``to_privacy_override``, the
   symbol still exists, and the AST walk still finds a call site — which is
   precisely the blind spot (f) covers.

Deliberately absent: any runtime probe asserting that a known gap *still
leaks*. Such a test passes because the bug exists and breaks when it is fixed.
The gap issues (#968/#969/#970/#971) each carry their own runtime-probe
acceptance criterion.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import importlib
import inspect
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import frontmatter
import pytest

from creek.config import CreekConfig
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
from creek_mcp.tools.compile import _ABOVE_CEILING_REASON, compile_tool
from creek_mcp.tools.journal import journal_ingest_tool
from creek_mcp.tools.mine import mine_tool
from creek_mcp.tools.redact import _OUT_OF_SCOPE_REASON, redact_scan_tool
from creek_mcp.tools.reflect import reflect_tool
from creek_mcp.tools.report import report_tool
from creek_mcp.tools.state import state_render_tool
from creek_mcp.tools.state_read import state_read_tool
from creek_mcp.tools.upload import upload_tool
from creek_mcp.tools.wheel import wheel_tool

if TYPE_CHECKING:
    from collections.abc import Callable
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

_EXPECTED_TOOL_COUNT = 24

_EXPECTED_PRIMITIVES = frozenset({"refuse_above_ceiling", "iter_admitted_fragments"})

_PINNED_GATE_ROWS = [
    ("creek.reflect", "creek_mcp.tools.reflect", "_above_ceiling"),
    # NB: issue #932's "prior art" section names ``_sources_above_ceiling``.
    # No such symbol exists — ``creek_mcp/tools/compile.py`` defines
    # ``_survey_sources``, which returns a ``_SourceGate`` the caller branches
    # on. Pinned to the real name; the wrong one would have made layer (c) a
    # permanent, uninformative failure.
    ("creek.compile", "creek_mcp.tools.compile", "_survey_sources"),
    ("creek.wheel", "creek_mcp.tools.wheel", "to_privacy_override"),
    ("creek.mine", "creek_mcp.tools.mine", "to_privacy_override"),
    ("creek.draft", "creek_mcp.tools.draft", "to_privacy_override"),
    ("creek.author", "creek_mcp.tools.author", "to_privacy_override"),
    # #968 closed the report gap. The entry is *re-pointed* at the gate that
    # closed it rather than relabelled: report_tool now converts the ceiling
    # with ``to_privacy_override`` and threads the result into all six
    # generators, so the honest record is a GATED claim layers (c) and (e) can
    # check — which is exactly what the failure message on
    # ``test_pinned_gaps_keep_their_posture_and_issue`` asks for.
    ("creek.report", "creek_mcp.tools.report", "to_privacy_override"),
    # #969 closed both state gaps, and they close in *different shapes* —
    # which is why they are two rows rather than one. ``creek.state.render``
    # names no target: it is a corpus walk like report/wheel/mine, so it
    # EXCLUDES, converting the ceiling with ``to_privacy_override`` and
    # threading it into StateReportGenerator's per-section gates. Refusing it
    # would make the report unreachable, which is #968's explicit anti-goal.
    # ``creek.state.read`` addresses one atomic cached artifact, so there is
    # nothing to partially admit — you cannot exclude half a rendered markdown
    # document without re-rendering it, and re-rendering is what ``render`` is
    # — so it REFUSES on the artifact's own ``privacy_tier`` stamp via
    # ``refuse_above_ceiling``. Same reasoning #1068 applied to compile and
    # reflect, read one level up: read's target is not caller-*named* but it is
    # caller-*addressed* and singular, which is the property that matters.
    ("creek.state.read", "creek_mcp.tools.state_read", "refuse_above_ceiling"),
    ("creek.state.render", "creek_mcp.tools.state", "to_privacy_override"),
    # #970 closed the journal gap, and this row is a *re-point* of the same
    # kind #968 made for ``report`` and #969 made for the two ``state.*``
    # entries: the entry moves from a gap claim to a gate claim layers (c),
    # (e) and (f) can check, rather than being deleted. ``creek.journal``'s
    # gap was never on its *response* — the entry body is the caller's own —
    # but on its idempotent update-in-place, which overwrote whatever fragment
    # an ``external_id`` already mapped to without ranking that fragment's
    # tier. It now refuses on the rule *you may only overwrite what you could
    # have read*, which is a read question, so it goes through
    # ``refuse_above_ceiling`` (and thus ``tier_allowed``) rather than through
    # the write-side ``write_tier_allowed`` that still gates the incoming
    # entry's own tier. Pinning the exact pair is what keeps the posture and
    # the behaviour from drifting apart — the property the deleted
    # ``test_journal_is_pinned_as_no_unsupplied_read`` used to hold.
    ("creek.journal", "creek_mcp.tools.journal", "refuse_above_ceiling"),
    # ``creek.upload`` (#1023) is born gated, on the same rule and through the
    # same primitive as ``creek.journal``: its idempotent update-in-place
    # destroys whatever fragment an ``external_id`` already maps to, so you may
    # only overwrite what you could have read. Pinned as its own row rather
    # than left to layers (a)-(c) alone because the pair is the checkable part
    # — ``write_tier_allowed`` also runs in that module, on the *incoming*
    # tier, and a manifest re-pointed at it would read as a gate while
    # answering a different question entirely.
    ("creek.upload", "creek_mcp.tools.upload", "refuse_above_ceiling"),
    # #972 closed the redact gap, and this row is a re-point of the same kind
    # #968/#969/#970 made. The gap entry it replaces was recorded as
    # CALLER_NAMED_PATHS until the #932 review: the caller *does* name the
    # path, which was true and insufficient, because ``resolve_within_vault``
    # confines that path to the whole vault and a finding names the file it
    # came from — and Creek fragment filenames are slugified titles, so an
    # open-ceiling caller aimed at 01-Fragments read intimate titles off the
    # response.
    #
    # It closed by **scope** plus an as-scanned path renderer, not by adopting
    # either canonical primitive, so layer (e) has nothing to say about it and
    # layers (c)/(f) carry the weight. ``_refuse_outside_scan_scope`` admits
    # the FEAT-027 staging subtree at every ceiling and ranks every other
    # in-vault target as intimate, because the scan is a regex pass over bytes
    # that reads no per-file tiers and therefore has nothing to rank a
    # fragment *with*. ``refuse_above_ceiling`` was deliberately NOT adopted
    # for exactly that reason: it would carry GENERIC_ABOVE_CEILING_REASON —
    # "resolved content exceeds the declared tier ceiling" — which claims the
    # tool ranked content it never opened, and tells a CrawDad operator
    # nothing about the one thing they can act on, which is where they pointed
    # the scan. Same call #968 made for creek.report, one step further out.
    ("creek.redact.scan", "creek_mcp.tools.redact", "_refuse_outside_scan_scope"),
]

_PINNED_GAPS = {
    "creek.skills.refresh": 971,
}

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
    "creek.redact.scan": "creek_mcp.tools.redact",
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

# Used only by the #961 agreement test below. The title is deliberately shared
# between an ``unclassified`` and a ``personal`` fragment there: the personal
# summary is derived from the title, so identical titles make "treated exactly
# as personal" assertable as string equality between the two yielded bodies,
# with no hardcoded stub format to drift.
_UNCLASSIFIED_BODY = "CANARY-UNCLASSIFIED-BODY-1d58"
_TWIN_TITLE = "Twinned note"


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


def _discarded_calls(tree: ast.AST) -> set[int]:
    """Return the ids of call nodes whose result is thrown away.

    A bare expression statement — ``_above_ceiling(tier, ceiling)`` on a line
    of its own — is a call whose value nothing consumes. Python allows it and
    the AST is indistinguishable from a real gate at the ``ast.Call`` level,
    which is how a "call-and-discard" edit can leave the declared gate present,
    imported, and syntactically invoked while it decides nothing.

    Args:
        tree: The parsed module.

    Returns:
        ``id()`` of every :class:`ast.Call` sitting directly under an
        :class:`ast.Expr` statement. Identity rather than equality because AST
        nodes are unhashable and two structurally identical calls at different
        sites are genuinely different call sites.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    }


def _calls_symbol(module: ModuleType, symbol: str) -> bool:
    """Return whether *module* calls *symbol* and uses the result.

    Deliberately looks for a *call*, not a definition or an import: a module
    can import (or even define) a gate and never invoke it, which is exactly
    the failure mode layers (c) and (e) exist to detect.

    Calls whose result is discarded do not count. Requiring the value to be
    consumed — by a condition, an assignment, a comparison, a return — is what
    separates a gate from a gesture: replacing
    ``if _above_ceiling(t, c): return refusal_response(...)`` with a bare
    ``_above_ceiling(t, c)`` and falling through leaves a call site that
    refuses nothing, and this check is the reason that edit cannot pass.

    Args:
        module: The imported tool module to scan.
        symbol: The callee name to look for.

    Returns:
        ``True`` when at least one *load-bearing* call site names *symbol*.
    """
    tree = ast.parse(inspect.getsource(module))
    discarded = _discarded_calls(tree)
    return any(
        isinstance(node, ast.Call)
        and _call_name(node) == symbol
        and id(node) not in discarded
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
    tags: list[str] | None = None,
    file_stem: str | None = None,
) -> Path:
    """Write one classified fragment under ``01-Fragments/Notes``.

    Args:
        vault: Vault root.
        frag_id: Fragment id, and by default the file stem too.
        title: Fragment title — what a personal-tier summary is built from.
        body: Markdown body (a canary string in these tests).
        privacy_tier: The fragment's ``privacy_tier`` front-matter value.
        tags: Optional Obsidian tags. ``None`` — the default — omits the key
            entirely rather than writing an empty list, so front matter written
            by the callers that predate layer (f) is byte-identical to before.
        file_stem: Filename stem, when it must differ from *frag_id*. Added for
            :func:`_seed_redact_canaries`, whose whole point is a sentinel that
            lives in the **filename** — Creek fragment filenames are slugified
            titles, and ``creek.redact.scan`` reports the file a finding came
            from. Defaults to *frag_id*, so every caller that predates it
            writes byte-identical front matter to the same path as before. The
            two are kept separable rather than folded together because a
            canary in an *id* would be echoed back legitimately by
            ``creek.reflect`` and ``creek.compile``, which are handed the id by
            the caller — see :data:`_RUNTIME_INTIMATE_ID`.

    Returns:
        The path the fragment was written to.
    """
    metadata: dict[str, Any] = {
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
    if tags is not None:
        metadata["tags"] = tags
    target = vault / "01-Fragments" / "Notes" / f"{file_stem or frag_id}.md"
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
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
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
    """The MCP surface is 24 tools; growing it is a deliberate act.

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

    These read vault content without honouring the caller's ceiling. The
    posture is the honest record of that, and the issue number is the promise
    that someone is on the hook for it. Relabelling either one — without the
    code changing — converts a tracked gap into an invisible one.

    The table shrinks as gaps close: #968 took ``creek.report`` out of it and
    #969 took the two ``creek.state.*`` entries, each *re-pointed* at the gate
    that closed it in :data:`_PINNED_GATE_ROWS` rather than merely deleted, so
    the claim stays checkable by layers (c) and (e) instead of disappearing.
    """
    entry = TOOL_POSTURES[tool]
    assert entry.posture is ReadPosture.UNGATED_KNOWN_GAP, (
        f"{tool} was recorded as {entry.posture.value!r}, but it reads vault "
        f"content without honouring the ceiling — a gap tracked by #{issue}. "
        "If the gap is genuinely closed, point the entry at the gate that "
        "closed it (GATED) rather than relabelling the posture."
    )
    assert entry.gap_issue == issue


def test_journal_carries_no_residual_gap_issue() -> None:
    """``creek.journal``'s posture and its story stay in step (#970).

    The other half of the re-point recorded in :data:`_PINNED_GATE_ROWS`,
    which pins the gate itself. What is pinned *here* is the property the
    deleted ``test_journal_is_pinned_as_no_unsupplied_read`` held before the
    gap closed: a reader must not be able to draw the wrong conclusion about
    journal from the manifest alone. That test said "the posture is
    reassuring, so the gap must be recorded next to it"; with the gap closed,
    the same intent inverts — a leftover ``gap_issue`` on a now-``GATED``
    entry would advertise a hole that is not there, send an auditor hunting
    #970 in a module that fixed it, and (via ``_GAP_ISSUE_TOOLS``) keep
    demanding the tool's own source still mention the issue as an open gap.
    """
    entry = TOOL_POSTURES["creek.journal"]
    assert entry.posture is ReadPosture.GATED
    assert entry.gap_issue is None, (
        "creek.journal is GATED by the #970 overwrite gate but still names a "
        f"gap issue (#{entry.gap_issue}). Drop gap_issue, or explain in the "
        "rationale what remains ungated."
    )


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
        (PrivacyTier.UNCLASSIFIED, TierCeiling.PERSONAL),
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

    ``unclassified`` at ``ceiling=personal`` is included on purpose, and #961
    is why it reads ``personal`` rather than ``open``. The MCP-side ranking
    (``creek_mcp.tier_ceiling._TIER_RANK``) used to admit ``unclassified`` at
    ``open``, diverging from the reader-caution ranking in
    ``creek.classify.privacy_filter``. That split is now **closed**: both rank
    it with ``personal``, so ``personal`` is the lowest ceiling that admits it.
    The row stays here for the same reason it was here before — this primitive
    must not re-decide admission either way, and pinning the tier the two
    rankings now agree on is what makes a future re-divergence fail here.
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


# ---------------------------------------------------------------------------
# The two primitives must agree with each other (#961)
#
# Everything above tests each primitive on its own terms. This section tests
# the one property neither can establish alone: that a caller who satisfies
# the ceiling through ``refuse_above_ceiling`` and a caller who satisfies it
# through ``iter_admitted_fragments`` are told the same thing about the same
# fragment. They were not, before #961.
# ---------------------------------------------------------------------------


def test_both_gate_primitives_treat_unclassified_exactly_as_personal(
    tmp_path: Path,
) -> None:
    """The two canonical primitives agree about an explicit ``unclassified`` (#961).

    This module's whole premise is that a tool satisfies the ceiling through one
    of exactly two primitives (:data:`CANONICAL_GATE_PRIMITIVES`) rather than
    re-deriving policy. That premise is worth nothing if the two primitives
    disagree — and they did: ``refuse_above_ceiling`` delegates to
    ``creek_mcp.tier_ceiling.tier_allowed``, which ranked ``unclassified`` with
    ``open`` and therefore *admitted* it at ``ceiling=open``, while
    ``iter_admitted_fragments`` delegates to
    ``creek.classify.privacy_filter.filter_fragments_by_tier``, which has
    treated the same value as ``personal`` since #876. Two adopters of "the
    canonical gate" reached opposite conclusions about one fragment. #961 closes
    that by moving the MCP ranking to 1, with ``personal``.

    "Agree" needs stating carefully, because the primitives are not the same
    shape: ``refuse_above_ceiling`` is a hard cutoff (admit or refuse) and
    ``iter_admitted_fragments`` summarises rather than drops. So agreement is
    asserted as *both treat ``unclassified`` exactly as they treat ``personal``*:

    - the refusal payload for ``unclassified`` must be non-``None`` **and**
      equal to the one ``personal`` produces at the same ceiling;
    - the body yielded for the unclassified fragment must equal the body
      yielded for a ``personal`` fragment with the same title — string
      equality against a sibling's real treatment, not against a hardcoded
      ``"[Personal-tier summary: ..."`` this test would have to keep in sync.

    The ``open`` fragment is seeded alongside them so a degenerate filter that
    summarised or emptied everything — which would satisfy both equalities —
    still fails.

    Args:
        tmp_path: pytest's per-test temporary directory, used as the vault root
            (the seeded fixture holds no unclassified fragment, and adding one
            to it would change the exact-set assertions its other users make).
    """
    _write_fragment(
        tmp_path,
        frag_id="frag-unclassified",
        title=_TWIN_TITLE,
        body=_UNCLASSIFIED_BODY,
        privacy_tier="unclassified",
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-personal-twin",
        title=_TWIN_TITLE,
        body=_PERSONAL_BODY,
        privacy_tier="personal",
    )
    _write_fragment(
        tmp_path,
        frag_id="frag-open",
        title="Open note",
        body=_OPEN_BODY,
        privacy_tier="open",
    )

    as_unclassified = refuse_above_ceiling(
        tool="creek.reflect",
        content_tier=PrivacyTier.UNCLASSIFIED,
        ceiling=TierCeiling.OPEN,
    )
    as_personal = refuse_above_ceiling(
        tool="creek.reflect",
        content_tier=PrivacyTier.PERSONAL,
        ceiling=TierCeiling.OPEN,
    )
    assert as_unclassified is not None, (
        "refuse_above_ceiling admitted an explicitly-unclassified fragment at "
        "ceiling=open while iter_admitted_fragments summarises the same "
        "fragment as personal; the two canonical gates disagree (#961)"
    )
    assert as_unclassified == as_personal

    admitted = _admitted_by_id(tmp_path, TierCeiling.OPEN)
    blob = json.dumps({key: value[1] for key, value in admitted.items()})
    assert _UNCLASSIFIED_BODY not in blob
    assert (
        admitted["frag-unclassified"][1].strip()
        == admitted["frag-personal-twin"][1].strip()
    )
    assert admitted["frag-open"][1].strip() == _OPEN_BODY


# ---------------------------------------------------------------------------
# Layer (f) — runtime canary probe
#
# Everything above is static: it reads the manifest, the tool schemas, and the
# tools' source. That is enough to prove a gate is declared, exists and is
# called — and no more. A second read of the corpus, added *alongside* an
# untouched gate call, is invisible to all of it. What follows calls each
# GATED tool for real and looks at what comes back.
# ---------------------------------------------------------------------------

# Sentinels in the same unmistakable style as the bodies above, but distinct
# values: these travel through four different tools, and a collision with the
# iter_admitted_fragments canaries would make a failure ambiguous about which
# fixture leaked.
_RUNTIME_OPEN_CANARY = "CANARY-RUNTIME-OPEN-4e8b"
_RUNTIME_INTIMATE_CANARY = "CANARY-RUNTIME-INTIMATE-7d21"

# The ids are deliberately canary-free. ``creek.reflect`` and ``creek.compile``
# are *given* these ids by the caller, so echoing one back is not a leak; only
# the content behind it is. Keeping the sentinel out of the id means a probe
# failure can only mean "the tool reached the content".
_RUNTIME_OPEN_ID = "frag-runtime-open"
_RUNTIME_INTIMATE_ID = "frag-runtime-intimate"

# ``creek_mcp/tools/reflect.py`` inlines this string at its #846 gate rather
# than naming a constant, so it is repeated here. Asserted (rather than a bare
# ``status == "refused"``) because reflect has a second refusal — "entry_ref
# not found" — and a probe that accepted either would go green if reflect
# simply stopped resolving the fragment, which proves nothing about the gate.
_REFLECT_ABOVE_CEILING_REASON = "entry_ref tier exceeds ceiling"

# ``creek.journal``'s probe writes its own entry (see ``_seed_journal_canary``),
# so it needs an idempotency key and a stable timestamp. The key is deliberately
# canary-free: it is the caller's own string and the tool echoes it back on
# success, so a sentinel in it would make a leak assertion ambiguous.
_JOURNAL_PROBE_EXTERNAL_ID = "runtime-probe-journal-970"
_JOURNAL_PROBE_TS = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
# The refused caller's *own* text. It must reach no file in the vault: a
# refusal that still staged or wrote this body would be the #970 bug with a
# tidier response.
_JOURNAL_REPLACEMENT_CANARY = "CANARY-RUNTIME-JOURNAL-REPLACEMENT-3a6c"

# ``creek.upload``'s probe writes its own document, for the same reason
# ``creek.journal``'s does: the gate resolves its target through the *upload*
# source ledger, so a fragment that never arrived through this tool has no
# record and the call would be a plain creation. The payload is deliberately a
# ``.md``: it routes to the markdown ingestor, which needs nothing beyond the
# standard library and ``python-frontmatter``, where a ``.png`` would need the
# tesseract system binary and an ``.xlsx`` an optional extra — either would
# make layer (f) environment-dependent.
_UPLOAD_PROBE_EXTERNAL_ID = "runtime-probe-upload-1023"
_UPLOAD_PROBE_FILENAME = "runtime-probe.md"
# The refused caller's own bytes, which must reach no file in the vault — the
# staged copy under 00-Creek-Meta/adepthood/uploads/ included, since that is
# what makes the gate's position above ``_stage_upload`` checkable.
_UPLOAD_REPLACEMENT_CANARY = "CANARY-RUNTIME-UPLOAD-REPLACEMENT-8f52"

_PROBE_EXEMPT: dict[str, str] = {
    "creek.draft": (
        "draft_tool needs a DraftGenerator driven over a deployed skills tree; "
        "on a bare fixture it answers from generator scaffolding "
        "(status='empty' / a RuntimeError refusal) before the response shape a "
        "probe wants to inspect exists, so a canary-free result would be "
        "evidence about the scaffolding, not about the ceiling. Its corpus "
        "walk is IdeaMiner(privacy_override=to_privacy_override(ceiling)) — "
        "byte-for-byte the walk creek.mine is probed through below — so the "
        "read path is covered even though draft's own envelope is not."
    ),
    "creek.author": (
        "author_tool calls load_config() and drives the Writing Desk, and it "
        "wraps that whole span in `except Exception` returning status='error'. "
        "On any unconfigured-provider or missing-contract failure it therefore "
        "returns an envelope with no corpus content in it at all, so the "
        "canary assertion would pass for the wrong reason — the desk never "
        "started. A real probe needs a configured desk and belongs beside the "
        "other author tests."
    ),
}
"""``GATED`` tools that cannot be probed *here*, each with the reason why.

Read these as claims, not as boilerplate: an exemption is the one way a
``GATED`` tool escapes layer (f), so a reason that does not survive being
checked against the tool's source is a hole in the layer.
"""


def _forbidden_llm_factory(tier: PrivacyTier) -> Any:
    """Fail loudly instead of returning an LLM — the gate must never reach here.

    Both probed write-side tools build their model client *below* the ceiling
    gate, so on an above-ceiling request this factory must not be invoked at
    all. A stub that quietly returned ``"{}"`` would let a broken gate look
    clean: the tool would call the model, get nothing quotable back, and answer
    with a canary-free envelope. Raising turns that same path into a failure
    that names what happened.

    Args:
        tier: The routing tier the tool derived — reported in the message
            because *which* tier reached the factory is the first thing a
            reader will want.

    Returns:
        Never returns.

    Raises:
        AssertionError: Always. Neither ``reflect_tool`` (catches
            ``RuntimeError``) nor ``compile_tool`` (catches ``ValueError`` /
            ``RuntimeError``) swallows it, so it surfaces as a test failure
            rather than as a structured refusal.
    """
    msg = (
        "the ceiling gate let an above-ceiling read through to the model: an "
        f"LLM client was built for tier {tier!r} on a request the tool was "
        "supposed to refuse before reaching any provider"
    )
    raise AssertionError(msg)


def _probe_wheel(vault: Path) -> dict[str, Any]:
    """Call ``creek.wheel`` at the open ceiling over the canary vault.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    return wheel_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.OPEN)


def _probe_mine(vault: Path) -> dict[str, Any]:
    """Call ``creek.mine`` at the open ceiling, unbounded.

    ``limit=0`` returns *every* seed. A default-limited call could hide a leak
    behind truncation — the probe would then be asserting on the first ten
    seeds rather than on the tool's whole output.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    return mine_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
        limit=0,
    )


def _probe_reflect(vault: Path) -> dict[str, Any]:
    """Call ``creek.reflect`` at the open ceiling on the intimate canary.

    ``care_guard=None`` keeps the #753 seam out of the picture: this probe is
    about the #846 read gate, and the gate sits above the care seam precisely
    so an unadmitted caller cannot reach it.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    return reflect_tool(
        vault_path=vault,
        llm_factory=_forbidden_llm_factory,
        entry_ref=_RUNTIME_INTIMATE_ID,
        privacy_tier_ceiling=TierCeiling.OPEN,
        care_guard=None,
    )


def _probe_compile(vault: Path) -> dict[str, Any]:
    """Call ``creek.compile`` at the open ceiling over the intimate canary.

    The target metadata is deliberately sentinel-free: ``target_id`` and
    ``target_title`` are the caller's own strings, so a response echoing them
    would say nothing about the corpus and would only blur the assertion.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    return compile_tool(
        vault_path=vault,
        fragment_ids=[_RUNTIME_INTIMATE_ID],
        target_kind="thread",
        target_id="thread-runtime-probe",
        target_title="Runtime probe target",
        llm_factory=_forbidden_llm_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _probe_report(vault: Path) -> dict[str, Any]:
    """Call ``creek.report`` (``tags``) at the open ceiling over the canary vault.

    ``tags`` is the report type the #968 reproduction used and the only one that
    reaches all five of ``TagGardenGenerator``'s scan directories.
    ``generate_garden`` writes ``00-Creek-Meta/Tag-Garden.md`` without creating
    its parent, and ``canary_vault`` is a bare fragment vault, so the probe
    creates the meta folder first.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    return report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _seed_state_carriers(vault: Path) -> None:
    """Give the two ``creek.state.*`` probes a canary the state report can render.

    ``canary_vault`` puts each sentinel in a fragment's ``title``, body and
    ``tags``, which covers an index-shaped, a body-shaped and a tag-garden-shaped
    response. The state report renders **none** of those three: it aggregates
    counts, eddy/thread titles, synchronicity ids, orphan *paths* and liminal
    file *stems*. A state probe run against the bare fixture would therefore
    produce a canary-free report whether or not any ceiling was enforced —
    vacuous in exactly the way ``_probe_state_read``'s docstring warns about,
    and the reason this helper exists rather than the probe simply calling the
    tool.

    So the probe adds a carrier the report does render: a ``10-Liminal/Unnamed``
    note whose file stem *is* the sentinel, one per tier. The ``open`` one is
    the positive control — the render probe's own test asserts it is present in
    the same bytes, so a gate broken in the drop-everything direction cannot
    pass by writing an empty report.

    ``00-Creek-Meta`` is created for the same reason ``_probe_report`` creates
    it: ``canary_vault`` is a bare fragment vault and the MCP audit log writes
    there.

    Args:
        vault: The seeded canary vault, mutated in place.
    """
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    unnamed = vault / "10-Liminal" / "Unnamed"
    unnamed.mkdir(parents=True, exist_ok=True)
    for canary, tier in (
        (_RUNTIME_OPEN_CANARY, "open"),
        (_RUNTIME_INTIMATE_CANARY, "intimate"),
    ):
        stem = f"unnamed-{canary}"
        metadata: dict[str, Any] = {
            "type": "fragment",
            "id": stem,
            "title": stem,
            "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
            "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
            "source": {"platform": "journal", "author": "self"},
            "frequency": {"primary": "unclassified", "secondary": []},
            "privacy_tier": tier,
        }
        (unnamed / f"{stem}.md").write_text(
            frontmatter.dumps(frontmatter.Post(content="unnamed body", **metadata)),
            encoding="utf-8",
        )


def _probe_state_render(vault: Path) -> dict[str, Any]:
    """Call ``creek.state.render`` at the open ceiling over the canary vault.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    _seed_state_carriers(vault)
    return state_render_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _probe_state_read(vault: Path) -> dict[str, Any]:
    """Render at ``ceiling=all`` first, *then* read at the open ceiling.

    The render is not setup convenience — it is what makes this probe mean
    anything. ``canary_vault`` holds no ``00-Creek-Meta/State/latest.md``, so a
    bare ``state_read_tool`` call would return ``status="empty"`` with an empty
    ``content``, and the shared canary assertion would pass on a vault where
    there was never anything to leak. Rendering at ``ceiling=all`` first puts
    the intimate canary genuinely into the artifact the read then addresses, so
    a missing gate produces a real leak rather than a vacuous pass.

    :func:`_seed_state_carriers` is the other half of that: without it the
    ``ceiling=all`` render would itself be canary-free, because the state
    report renders none of the three places ``canary_vault`` hides a sentinel.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    _seed_state_carriers(vault)
    state_render_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.ALL)
    return state_read_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _seed_journal_canary(vault: Path) -> None:
    """Ingest one ``intimate`` journal entry the later overwrite attempt targets.

    The probe has to create its own entry rather than reuse the fixture's
    fragments: ``creek.journal``'s gate resolves its target through the
    **source ledger**, keyed on the staged entry's stable path, so a fragment
    that was never ingested through this tool has no ledger record and the
    call would be a plain creation. ``canary_vault`` is a bare fragment vault,
    so the meta subtree is created first — the same reason
    :func:`_probe_report` creates ``00-Creek-Meta``, widened to the layout
    ``tests/test_mcp_journal.py`` gives the real ingest.

    Seeded at ``ceiling=all`` so the intimate entry is genuinely admitted and
    genuinely stored — the probe below then attempts to destroy it from
    ``ceiling=open``.

    Args:
        vault: The seeded canary vault, mutated in place.
    """
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    seeded = journal_ingest_tool(
        vault_path=vault,
        content=f"Intimate journal canary body {_RUNTIME_INTIMATE_CANARY}",
        external_id=_JOURNAL_PROBE_EXTERNAL_ID,
        timestamp=_JOURNAL_PROBE_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert seeded["status"] == "ok"
    assert seeded["action"] == "created"


def _journal_overwrite_at_open(vault: Path) -> dict[str, Any]:
    """Re-send the seeded ``external_id`` as ``open`` text at ``ceiling=open``.

    The incoming tier is ``open``, so the pre-existing ``write_tier_allowed``
    gate admits it; only a gate that ranks the tier of the fragment being
    *overwritten* refuses this call.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    return journal_ingest_tool(
        vault_path=vault,
        content=f"Open replacement body {_JOURNAL_REPLACEMENT_CANARY}",
        external_id=_JOURNAL_PROBE_EXTERNAL_ID,
        timestamp=_JOURNAL_PROBE_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _probe_journal(vault: Path) -> dict[str, Any]:
    """Seed an intimate entry, then attempt to overwrite it at ``ceiling=open``.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope for the overwrite attempt.
    """
    _seed_journal_canary(vault)
    return _journal_overwrite_at_open(vault)


def _upload_b64(text: str) -> str:
    """Return the base64 of *text*, derived here rather than hardcoded.

    Args:
        text: The document body to encode.

    Returns:
        The ASCII base64 string ``creek.upload`` takes as ``content_base64``.
    """
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _seed_upload_canary(vault: Path) -> None:
    """Upload one ``intimate`` document the later overwrite attempt targets.

    The same argument :func:`_seed_journal_canary` makes, one tool over: the
    gate resolves its target through the upload source ledger, keyed on the
    staged file's stable path, so the fixture's own fragments are invisible to
    it and a probe over them would be a plain creation. ``canary_vault`` is a
    bare fragment vault, so the meta subtree is created first.

    Seeded at ``ceiling=all`` so the intimate document is genuinely admitted
    and genuinely stored — the probe below then attempts to destroy it from
    ``ceiling=open``.

    Args:
        vault: The seeded canary vault, mutated in place.
    """
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    seeded = upload_tool(
        vault_path=vault,
        filename=_UPLOAD_PROBE_FILENAME,
        content_base64=_upload_b64(
            f"Intimate upload canary body {_RUNTIME_INTIMATE_CANARY}",
        ),
        external_id=_UPLOAD_PROBE_EXTERNAL_ID,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert seeded["status"] == "ok"
    assert seeded["action"] == "created"


def _upload_overwrite_at_open(vault: Path) -> dict[str, Any]:
    """Re-send the seeded ``external_id`` as ``open`` bytes at ``ceiling=open``.

    The incoming tier is ``open``, so the pre-existing ``write_tier_allowed``
    gate admits it; only a gate that ranks the tier of the fragment being
    *overwritten* refuses this call.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    return upload_tool(
        vault_path=vault,
        filename=_UPLOAD_PROBE_FILENAME,
        content_base64=_upload_b64(
            f"Open replacement body {_UPLOAD_REPLACEMENT_CANARY}",
        ),
        external_id=_UPLOAD_PROBE_EXTERNAL_ID,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _probe_upload(vault: Path) -> dict[str, Any]:
    """Upload an intimate document, then attempt to overwrite it at ``open``.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope for the overwrite attempt.
    """
    _seed_upload_canary(vault)
    return _upload_overwrite_at_open(vault)


_REDACT_PII_LINE = "Reach me at someone@example.com any time."
"""One real ``email`` match, so a redact scan of the file yields findings.

``creek.redact.scan`` reports nothing about a file it finds nothing in, so a
carrier with no PII in it would never appear in the response at all and the
canary assertions below would hold over an empty findings list.
"""

_REDACT_SCAN_TARGET = "01-Fragments"
"""The out-of-scope subtree ``_probe_redact_scan`` aims at."""

_REDACT_STAGING_TARGET = "00-Creek-Meta/Inbound"
"""The FEAT-027 staging subtree, admitted at every ceiling."""


def _seed_redact_canaries(vault: Path) -> None:
    """Give the redact probe carriers whose sentinels live in **filenames**.

    ``canary_vault`` is structurally blind to this leak class, in exactly the
    way :func:`_seed_state_carriers`' docstring warns about. Two independent
    reasons, and either one alone would make the probe vacuous:

    * The fixture's file stems come from ``frag_id``
      (``frag-runtime-intimate``), and the sentinels live in the ``title``,
      the body and the ``tags``. ``creek.redact.scan`` returns none of those
      three — it returns the *path* of each file a pattern matched in, plus a
      line number and a salted hash — so no canary is anywhere it could
      surface.
    * The fixture's fragments carry no PII, so the scanner matches nothing in
      them and reports them not at all.

    Run the probe over the bare fixture and it comes back canary-free whether
    or not any gate exists, which is a green layer (f) that proves nothing.

    So this seeds two carriers of the shape the tool actually reports. (a) An
    ``intimate`` fragment whose **file stem** carries
    :data:`_RUNTIME_INTIMATE_CANARY` and whose body carries a real email
    match — the thing an ``open``-ceiling caller must not be able to name,
    and a faithful model of the real leak, since Creek fragment filenames are
    slugified titles. (b) A staged file under ``00-Creek-Meta/Inbound/`` whose
    file stem carries :data:`_RUNTIME_OPEN_CANARY` and whose body carries the
    same match — the positive control, asserted *present* by the probe's own
    test so a gate broken in the refuse-everything direction cannot pass by
    returning nothing.

    Both bodies are deliberately canary-free: the scan never returns matched
    text, so a sentinel in a body could not surface even through a completely
    ungated tool, and its absence from the response would be evidence about
    nothing.

    Args:
        vault: The seeded canary vault, mutated in place.
    """
    _write_fragment(
        vault,
        frag_id="frag-redact-intimate",
        title="Intimate redact carrier",
        body=_REDACT_PII_LINE,
        privacy_tier="intimate",
        file_stem=f"redact-carrier-{_RUNTIME_INTIMATE_CANARY}",
    )
    staged = vault / "00-Creek-Meta" / "Inbound" / "ch1"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / f"attachment-{_RUNTIME_OPEN_CANARY}.md").write_text(
        f"{_REDACT_PII_LINE}\n",
        encoding="utf-8",
    )


def _probe_redact_scan(vault: Path) -> dict[str, Any]:
    """Aim ``creek.redact.scan`` at ``01-Fragments`` from the open ceiling.

    The scope gate answers above ``load_config()`` — and above the existence
    check, so the refusal cannot be used as an existence oracle over slugified
    filenames — which is what keeps layer (f) a unit test: no config file, no
    provider, no skills tree. The audit log is appended before the gate, by
    design, and creates its own parent directory, so the bare fixture needs no
    ``00-Creek-Meta`` of its own.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    _seed_redact_canaries(vault)
    return redact_scan_tool(
        vault_path=vault,
        input_path=_REDACT_SCAN_TARGET,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _fragment_bytes(vault: Path) -> dict[Path, bytes]:
    """Return every fragment file under ``01-Fragments`` mapped to its bytes.

    Args:
        vault: Vault root.

    Returns:
        ``{path: bytes}``. A mapping rather than a list so a file that
        appeared, vanished or moved shows up as a changed key rather than as
        an off-by-one in a positional comparison.
    """
    return {
        path: path.read_bytes()
        for path in sorted((vault / "01-Fragments").rglob("*.md"))
    }


_RUNTIME_PROBES: dict[str, Callable[[Path], dict[str, Any]]] = {
    "creek.wheel": _probe_wheel,
    "creek.mine": _probe_mine,
    "creek.reflect": _probe_reflect,
    "creek.compile": _probe_compile,
    "creek.report": _probe_report,
    "creek.state.render": _probe_state_render,
    "creek.state.read": _probe_state_read,
    "creek.journal": _probe_journal,
    "creek.upload": _probe_upload,
    "creek.redact.scan": _probe_redact_scan,
}
"""``GATED`` tool → a callable that invokes it at ``ceiling=open``.

Each probe is callable with no live provider, no config file and no skills
tree, so layer (f) stays a unit test.
"""


@pytest.fixture
def canary_vault(tmp_path: Path) -> Path:
    """Return a vault holding one ``open`` and one ``intimate`` canary fragment.

    Each fragment carries its sentinel in **three** places — ``title``, body and
    ``tags`` — so a tool that leaks only titles (an index-shaped response), only
    tags (a tag-garden-shaped one) or only bodies is caught by the same
    assertion. The ``open`` fragment is not decoration: it is the positive
    control, and the per-tool tests below assert it *is* reachable, so a tool
    that returned nothing at all could not pass layer (f) by being empty.
    """
    _write_fragment(
        tmp_path,
        frag_id=_RUNTIME_OPEN_ID,
        title=f"Open canary {_RUNTIME_OPEN_CANARY}",
        body=f"Open canary body {_RUNTIME_OPEN_CANARY}",
        privacy_tier="open",
        tags=[_RUNTIME_OPEN_CANARY],
    )
    _write_fragment(
        tmp_path,
        frag_id=_RUNTIME_INTIMATE_ID,
        title=f"Intimate canary {_RUNTIME_INTIMATE_CANARY}",
        body=f"Intimate canary body {_RUNTIME_INTIMATE_CANARY}",
        privacy_tier="intimate",
        tags=[_RUNTIME_INTIMATE_CANARY],
    )
    return tmp_path


def test_every_gated_tool_is_probed_or_explicitly_exempt() -> None:
    """Each ``GATED`` tool is either runtime-probed or exempt, never neither.

    This is layer (a)'s forcing function applied one level down. Hand-listing
    the probed tools and leaving the rest silently unprobed would let a newly
    ``GATED`` tool inherit "structurally verified, behaviourally unchecked" —
    which is exactly the state the wheel mutation exploited. Driving the
    parametrisation off :data:`_RUNTIME_PROBES` while asserting its union with
    :data:`_PROBE_EXEMPT` covers the manifest means the choice has to be made
    out loud.

    Disjointness is asserted too: a tool in both dicts would carry an
    exemption reason nobody reads and a probe nobody knows is authoritative.
    So is the reverse direction — an entry for a tool that is no longer
    ``GATED`` is a claim about nothing, and it inflates the apparent depth of
    this layer.
    """
    probed = set(_RUNTIME_PROBES)
    exempt = set(_PROBE_EXEMPT)
    gated = set(_GATED_TOOLS)
    both = probed & exempt
    assert not both, (
        f"tool(s) both probed and exempt: {sorted(both)}. An exemption is a "
        "statement that no probe is possible here; a probe refutes it."
    )
    unchecked = gated - probed - exempt
    assert not unchecked, (
        "GATED tool(s) with no runtime probe and no exemption: "
        f"{sorted(unchecked)}. Add a probe to _RUNTIME_PROBES, or record in "
        "_PROBE_EXEMPT the specific reason this tool cannot be called here. "
        "Layers (a)-(e) only prove the gate is declared and invoked — they "
        "cannot see a second, ungated read next to it."
    )
    stale = (probed | exempt) - gated
    assert not stale, (
        "_RUNTIME_PROBES/_PROBE_EXEMPT name non-GATED tool(s): "
        f"{sorted(stale)}. Delete the entry, or restore the tool's gate."
    )
    assert (probed | exempt) == gated


@pytest.mark.parametrize("tool", sorted(_PROBE_EXEMPT))
def test_probe_exemptions_are_specific_to_their_tool(tool: str) -> None:
    """An exemption reason names the tool it excuses and says something.

    The only escape hatch out of layer (f) is a string, so the string has to
    carry weight. Requiring it to mention the tool's own module leaf rules out
    a reason copy-pasted from a sibling entry (the failure mode that turns two
    exemptions into one unexamined one), and the length floor rules out
    ``"n/a"`` and ``"TODO"``. Neither check can judge whether the reason is
    *true* — that is a reviewer's job, and the reasons are written to be read
    as claims about the tool's source.
    """
    reason = _PROBE_EXEMPT[tool]
    module = TOOL_POSTURES[tool].gate_module
    assert module is not None
    leaf = module.rsplit(".", maxsplit=1)[-1]
    assert leaf in reason, (
        f"{tool}'s probe exemption never mentions {leaf!r}, the module it is "
        f"excusing: {reason!r}. A reason that does not name the tool cannot "
        "be checked against it."
    )
    assert len(reason) >= 60, (
        f"{tool}'s probe exemption is too short to be a reason: {reason!r}. "
        "State what specifically makes the tool unprobeable here."
    )


@pytest.mark.parametrize("tool", sorted(_RUNTIME_PROBES))
def test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling(
    tool: str,
    canary_vault: Path,
) -> None:
    """A ``GATED`` tool called at ``ceiling=open`` never returns intimate content.

    The behavioural half of the manifest, and the only layer that can see a
    *parallel* read path. Layers (a)-(e) verify that the declared gate exists
    and is called; a leak added next to that call — a second
    ``iter_vault_fragments`` walk whose output is spliced into the response —
    leaves every one of them green.

    Asserted against ``json.dumps(response, default=str)`` rather than against
    named keys: the leak is just as real one level down, in a nested
    ``seeds[].source_fragments`` entry or a debugging key nobody reviewed, and
    a key-by-key check would only ever cover the shape that exists today.
    ``default=str`` keeps a ``Path`` or an enum from turning a leak into a
    ``TypeError``.

    The response is checked for a ``tool`` echo as well, so a probe that
    silently returned ``{}`` — from an exception swallowed somewhere, or a tool
    that stopped reading the vault entirely — cannot pass this assertion by
    having nothing in it. The per-tool tests below carry the stronger positive
    controls.
    """
    response = _RUNTIME_PROBES[tool](canary_vault)
    assert response["tool"] == tool
    serialised = json.dumps(response, default=str)
    entry = TOOL_POSTURES[tool]
    assert _RUNTIME_INTIMATE_CANARY not in serialised, (
        f"{tool} reached content above the caller's ceiling: the intimate "
        f"canary {_RUNTIME_INTIMATE_CANARY!r} appears in its response at "
        f"privacy_tier_ceiling=open.\n\n{serialised}\n\n"
        f"Note what this does NOT mean: {entry.gate_module}.{entry.gate_symbol} "
        "is still declared in TOOL_POSTURES and still called on the request "
        "path — layers (c) and (e) are green. The leak is a *second*, ungated "
        f"route to the corpus inside {entry.gate_module}. Find that route and "
        "remove it; adding another gate call leaves the first one standing."
    )


def test_wheel_probe_still_counts_the_fragment_it_is_admitted_to(
    canary_vault: Path,
) -> None:
    """``creek.wheel``'s probe is not passing by returning an empty wheel.

    The fixture holds two ``F1`` fragments, one ``open`` and one ``intimate``.
    At ``ceiling=open`` exactly one must be tallied: ``0`` would mean the
    corpus walk never ran (and the leak assertion above would be vacuous),
    ``2`` would mean the intimate fragment was counted — which is itself a leak,
    a one-bit disclosure that a fragment exists at that frequency, even though
    no body came back with it.

    Wheel returns counts and never any fragment text, so the count *is* the
    reachability evidence for the ``open`` canary; there is no string of it in
    the response to assert on.
    """
    response = _probe_wheel(canary_vault)
    assert response["status"] == "ok"
    assert response["total_classified"] == 1
    assert response["wheel"]["F1"]["count"] == 1


def test_report_probe_leaves_no_canary_in_the_artifact_it_writes(
    canary_vault: Path,
) -> None:
    """``creek.report``'s leak is the file it writes, not the dict it returns.

    The shared assertion in
    ``test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling`` is
    **structurally vacuous for this tool**. ``report_tool`` returns
    ``report_paths`` and nothing else — never a tag, a title, or a body — so its
    envelope is canary-free at ``ceiling=open`` whether or not the ceiling is
    enforced, and it was canary-free for the whole life of #968. The envelope
    check still earns its place as a tripwire against a future response shape
    that *does* carry content, but it is not evidence about this gap.

    The only evidence that means anything lives in the bytes of the artifacts
    the call writes, and #968 reproduced against **two** of them, so both are
    asserted: the regenerated ``Tag-Garden.md`` and the append-only
    ``tag-history.json``, where a wrongly-admitted tag would persist across
    every later run.

    The ``open`` canary is asserted *present* as the positive control, so the
    tool cannot pass by writing an empty garden — which a gate broken in the
    drop-everything direction would do: leak-free, and useless.
    """
    response = _probe_report(canary_vault)
    assert response["status"] == "ok"

    garden_path = canary_vault / "00-Creek-Meta" / "Tag-Garden.md"
    history_path = (
        canary_vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
    )
    garden = garden_path.read_text(encoding="utf-8")
    history = history_path.read_text(encoding="utf-8")

    assert _RUNTIME_INTIMATE_CANARY not in garden, (
        "creek.report distilled an intimate fragment's tag into "
        "00-Creek-Meta/Tag-Garden.md at privacy_tier_ceiling=open. The "
        "response envelope was clean — it always is — so nothing above this "
        f"line could have caught it.\n\n{garden}"
    )
    assert _RUNTIME_INTIMATE_CANARY not in history, (
        "creek.report recorded an intimate fragment's tag in "
        "00-Creek-Meta/Processing-Log/tag-history.json at "
        "privacy_tier_ceiling=open. This file is append-only: an entry written "
        f"at the wrong ceiling stays in the vault.\n\n{history}"
    )
    assert _RUNTIME_OPEN_CANARY in garden, (
        "creek.report wrote a tag garden with no admitted tag in it, so the "
        f"exclusion assertions above are vacuous.\n\n{garden}"
    )


def test_mine_probe_still_reaches_the_admitted_corpus(canary_vault: Path) -> None:
    """``creek.mine``'s probe is not passing by returning zero seeds.

    On this minimal fixture — no threads, no eddies, no liminal folder, no
    synchronicities, and ``phase="unclassified"`` skipping the wavelength
    strategy — the only strategy that fires is unexplored-ontology, whose seeds
    are coordinates in classification space and carry no fragment-derived text.
    So mine offers no *string* positive control here; the fragment-level one
    lives on the wheel probe above.

    What it does offer is a walk-level one, and it is exact:
    ``mine_unexplored_ontology`` runs with ``require_corpus=True`` and returns
    ``[]`` when the ceiling-filtered snapshot holds no fragments at all. A
    non-zero ``total`` therefore proves the corpus walk ran *and* admitted the
    ``open`` fragment. Had the tier filter been broken in the "drop everything"
    direction — leak-free and useless — this assertion would fail.
    """
    response = _probe_mine(canary_vault)
    assert response["status"] == "ok"
    assert response["total"] > 0


def test_reflect_probe_refuses_rather_than_merely_staying_quiet(
    canary_vault: Path,
) -> None:
    """``creek.reflect`` *refuses* the above-ceiling ``entry_ref`` (#846).

    Absence of the canary is necessary but not sufficient here. Reflect could
    resolve the intimate fragment, hand it to the model, and answer
    ``status="ok"`` with zero verbatim-validated notes — a canary-free response
    that has already egressed the entry. The gate's contract is a refusal, so
    the refusal is what is asserted.

    The reason is pinned too, not just the status: reflect's other refusal is
    ``"entry_ref not found"``, and a change that stopped resolving the fragment
    would satisfy a bare ``status == "refused"`` while telling us nothing about
    the ceiling.
    """
    response = _probe_reflect(canary_vault)
    assert response["status"] == "refused"
    assert response["reason"] == _REFLECT_ABOVE_CEILING_REASON
    assert response["tier_ceiling"] == TierCeiling.OPEN.value


def test_compile_probe_refuses_rather_than_merely_staying_quiet(
    canary_vault: Path,
) -> None:
    """``creek.compile`` *refuses* the above-ceiling source fragment (#848).

    Same argument as reflect's probe, with a sharper edge: compile's leak is
    not primarily its response but the page it writes. A tool that compiled the
    intimate fragment into ``02-Threads`` and returned only a path would be
    canary-free in the envelope and have laundered intimate content into an
    artifact every ``open``-ceiling read tool then serves.

    Pinned against the module's own ``_ABOVE_CEILING_REASON`` rather than a
    literal, because compile's other refusals (unknown ``target_kind``, empty
    ``fragment_ids``, the engine's not-found ``ValueError``) are also
    ``status="refused"`` and none of them would mean the gate fired.
    """
    response = _probe_compile(canary_vault)
    assert response["status"] == "refused"
    assert response["reason"] == _ABOVE_CEILING_REASON
    assert response["tier_ceiling"] == TierCeiling.OPEN.value


def test_state_render_probe_leaves_no_canary_in_the_artifact_it_writes(
    canary_vault: Path,
) -> None:
    """``creek.state.render``'s leak is the files it writes, not the dict it returns.

    The shared assertion in
    ``test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling`` is
    **not the evidence for this tool**. ``state_render_tool``'s envelope
    happens to echo ``content`` today, so that assertion is not vacuous the way
    ``creek.report``'s was — but it is contingent on a response shape, and a
    change that dropped ``content`` (to keep the envelope small, say) would
    silently turn it vacuous while the artifact on disk kept leaking. For a
    write-side surface the durable evidence is the bytes on disk, and #969
    reproduced three separate leaks in exactly those bytes.

    Both artifacts are asserted, because they are two independent write paths:
    the ISO-week file that ``write()`` renders, and ``latest.md`` — a symlink
    where the filesystem allows one and an independent byte copy where it does
    not, which is the file ``creek.state.read``, ``creek state-budget`` and
    every documented session-start flow actually open.

    The ``open`` canary is asserted *present* in the same bytes as the positive
    control, so the tool cannot pass by writing an empty report — which a gate
    broken in the drop-everything direction would do: leak-free, and useless.
    """
    response = _probe_state_render(canary_vault)
    assert response["status"] == "ok"

    state_dir = canary_vault / "00-Creek-Meta" / "State"
    week_files = [p for p in sorted(state_dir.glob("*.md")) if p.name != "latest.md"]
    assert len(week_files) == 1, (
        f"expected exactly one ISO-week report under {state_dir}, found "
        f"{[p.name for p in week_files]}"
    )
    week = week_files[0].read_text(encoding="utf-8")
    latest = (state_dir / "latest.md").read_text(encoding="utf-8")

    assert _RUNTIME_INTIMATE_CANARY not in week, (
        "creek.state.render wrote an intimate fragment's canary into "
        f"{week_files[0].name} at privacy_tier_ceiling=open. This is the "
        "artifact every later reader of the vault serves, and the response "
        f"envelope is not evidence for a write-side surface.\n\n{week}"
    )
    assert _RUNTIME_INTIMATE_CANARY not in latest, (
        "creek.state.render wrote an intimate fragment's canary into "
        "00-Creek-Meta/State/latest.md at privacy_tier_ceiling=open. This is "
        "the documented session-start context: CrawDad, /creek and "
        f"creek.state.read all read this file.\n\n{latest}"
    )
    assert _RUNTIME_OPEN_CANARY in week, (
        "creek.state.render wrote a report with no admitted content in it, so "
        f"the exclusion assertions above are vacuous.\n\n{week}"
    )


def test_state_read_probe_refuses_rather_than_merely_staying_quiet(
    canary_vault: Path,
) -> None:
    """``creek.state.read`` *refuses* an above-ceiling artifact (#969).

    Absence of the canary is necessary but not sufficient, the same argument
    reflect's and compile's probes make. ``state_read_tool`` has a second
    quiet answer — ``status="empty"`` for a vault with no rendered report — and
    a change that stopped resolving ``latest.md`` at all would satisfy a bare
    canary sweep while proving nothing about the ceiling. So the refusal is
    what is asserted, and the reason is pinned to
    :data:`~creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON` rather than to
    ``status`` alone.

    The reason is deliberately the *generic* one, shared with every other
    above-ceiling refusal on the surface, and it names no tier. A distinguishable
    "this report predates the stamp" reason would itself be an oracle for
    whether the vault holds above-ceiling content.
    """
    response = _probe_state_read(canary_vault)
    assert response["status"] == "refused"
    assert response["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert response["tier_ceiling"] == TierCeiling.OPEN.value
    assert response == refusal_response(
        tool="creek.state.read",
        ceiling=TierCeiling.OPEN,
        reason=GENERIC_ABOVE_CEILING_REASON,
    ), (
        "creek.state.read's refusal carries keys beyond the canonical four. "
        "Every extra key on a refusal is derived from content the caller was "
        f"not admitted to.\n\n{response}"
    )


def test_journal_probe_refuses_and_leaves_the_fragment_bytes_untouched(
    canary_vault: Path,
) -> None:
    """``creek.journal``'s evidence is the fragment bytes, not the envelope (#970).

    The shared assertion in
    ``test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling``
    passes **vacuously** for this tool. ``creek.journal``'s gate answers with
    the canonical four-key refusal, which carries no content at all — so its
    envelope is canary-free at ``ceiling=open`` whether or not the ceiling is
    enforced, and it was canary-free for the whole life of the gap. Worse, the
    *unfixed* tool answered ``status="ok"`` with a fragment id and no canary
    either. The envelope check still earns its place as a tripwire against a
    future response shape that does carry content, but presenting a green
    layer (f) as evidence that this write path is gated is exactly the #968
    blind spot: there, a response-level guardrail passed 5/5 while all six
    report generators went on leaking to disk.

    The evidence that means anything is the bytes on disk, so the fragment
    files are snapshotted across the refused call and compared. Three
    directions are pinned: the protected bytes are identical, the intimate
    canary and its ``intimate`` stamp both survive, and the refused caller's
    own replacement text reached **no** markdown file anywhere in the vault —
    the staged copy under ``00-Creek-Meta/adepthood/journal/`` included, which
    is what makes the gate's position above ``_stage_entry`` checkable here.
    The seeded canary is asserted present first, as the positive control:
    without it every exclusion below would hold over a vault where nothing was
    ever written.
    """
    fixture_fragments = set(_fragment_bytes(canary_vault))
    _seed_journal_canary(canary_vault)
    before = _fragment_bytes(canary_vault)
    # Identified as "the file that was not there before the seed" rather than
    # by directory or by canary match: the fixture's own intimate fragment
    # carries the same sentinel, and the journal routing directory is an
    # implementation detail this test has no reason to pin.
    seeded = sorted(set(before) - fixture_fragments)
    assert len(seeded) == 1, (
        "the journal probe's intimate entry is not on disk as exactly one new "
        f"fragment, so every assertion below is vacuous: {seeded}"
    )
    assert _RUNTIME_INTIMATE_CANARY in before[seeded[0]].decode("utf-8")

    response = _journal_overwrite_at_open(canary_vault)

    assert _fragment_bytes(canary_vault) == before, (
        "creek.journal's update-in-place rewrote an intimate fragment for a "
        "caller at privacy_tier_ceiling=open. The response envelope carries "
        "no content either way, so nothing in layer (f)'s shared canary sweep "
        "could have caught this."
    )
    protected = frontmatter.load(seeded[0])
    assert _RUNTIME_INTIMATE_CANARY in protected.content
    assert protected.metadata["privacy_tier"] == "intimate"
    leaked = [
        path
        for path in sorted(canary_vault.rglob("*.md"))
        if _JOURNAL_REPLACEMENT_CANARY in path.read_text(encoding="utf-8")
    ]
    assert leaked == [], (
        "the refused caller's own text was written to the vault anyway: "
        f"{leaked}. A refusal returned above _stage_entry writes nothing; one "
        "returned below it leaves a staged entry whose privacy_tier has "
        "already been rewritten downward."
    )
    assert response == refusal_response(
        tool="creek.journal",
        ceiling=TierCeiling.OPEN,
        reason=GENERIC_ABOVE_CEILING_REASON,
    ), (
        "creek.journal's refusal carries keys beyond the canonical four. Every "
        "extra key is derived from a fragment the caller was not admitted to — "
        f"including the id it resolved.\n\n{response}"
    )


def test_upload_probe_refuses_and_leaves_the_staged_document_untouched(
    canary_vault: Path,
) -> None:
    """``creek.upload``'s evidence is the bytes on disk, not the envelope (#1023).

    Read this as ``creek.journal``'s test one tool over, and for the same
    reason: the shared assertion in
    ``test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling``
    passes **vacuously** here. An above-ceiling overwrite is answered with the
    canonical four-key refusal, which carries no content — and an *ungated*
    tool would answer ``status="ok"`` with a fragment id and no canary either.
    A green layer (f) is therefore not evidence that this write path is gated.

    Two artifacts are checked because ``creek.upload`` writes two, and only one
    of them is a fragment. The staged document under
    ``00-Creek-Meta/adepthood/uploads/`` is the sharper of the pair: it holds
    the caller's bytes verbatim with no frontmatter and hence no escalate-only
    ratchet, so a gate placed below :func:`creek_mcp.tools.upload._stage_upload`
    would return a correct-looking refusal over an intimate document it had
    already overwritten. The seeded canary is asserted present in those bytes
    first, as the positive control: without it every exclusion below would hold
    over a vault where nothing was ever staged.
    """
    _seed_upload_canary(canary_vault)
    staged_files = [
        path
        for path in sorted(
            (canary_vault / "00-Creek-Meta" / "adepthood" / "uploads").rglob("*"),
        )
        if path.is_file()
    ]
    assert len(staged_files) == 1, (
        "the upload probe's intimate document is not on disk as exactly one "
        f"staged file, so every assertion below is vacuous: {staged_files}"
    )
    staged_before = staged_files[0].read_bytes()
    assert _RUNTIME_INTIMATE_CANARY.encode() in staged_before
    fragments_before = _fragment_bytes(canary_vault)

    response = _upload_overwrite_at_open(canary_vault)

    assert staged_files[0].read_bytes() == staged_before, (
        "creek.upload restaged over an intimate document for a caller at "
        "privacy_tier_ceiling=open. The refusal it returned is correct and "
        "arrived too late: the staged bytes carry no privacy_tier of their "
        "own, so nothing downstream could have restored them."
    )
    assert _fragment_bytes(canary_vault) == fragments_before, (
        "creek.upload's update-in-place rewrote an intimate fragment for a "
        "caller at privacy_tier_ceiling=open. The response envelope carries "
        "no content either way, so nothing in layer (f)'s shared canary sweep "
        "could have caught this."
    )
    leaked = [
        path
        for path in sorted(canary_vault.rglob("*"))
        if path.is_file() and _UPLOAD_REPLACEMENT_CANARY.encode() in path.read_bytes()
    ]
    assert leaked == [], (
        f"the refused caller's own bytes were written to the vault anyway: "
        f"{leaked}. Swept over raw bytes rather than over *.md text, because "
        "an upload's staged file can be a .docx or a .pdf that no text read "
        "would survive."
    )
    assert response == refusal_response(
        tool="creek.upload",
        ceiling=TierCeiling.OPEN,
        reason=GENERIC_ABOVE_CEILING_REASON,
    ), (
        "creek.upload's refusal carries keys beyond the canonical four. Every "
        "extra key is derived from a fragment the caller was not admitted to — "
        f"including the id it resolved.\n\n{response}"
    )


def test_redact_scan_probe_refuses_and_is_not_vacuous(
    canary_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.redact.scan``'s leak *is* its response, unlike its neighbours (#972).

    Worth stating plainly, because the three tests above it all exist to say
    the opposite. ``creek.report``'s and ``creek.state.render``'s evidence is
    the artifact they write, and ``creek.journal``'s is the fragment bytes it
    overwrites; for all three, the shared assertion in
    ``test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling``
    passes vacuously or contingently. Here the artifact and the envelope are
    the same object: the tool's entire output is a list of the files it
    matched in, and CrawDad posts ``report_markdown`` straight into a Discord
    channel. So the shared layer-(f) assertion is genuinely load-bearing for
    this tool — **provided** the seeded carrier puts a sentinel somewhere the
    response can reach, which is why :func:`_seed_redact_canaries` puts it in
    a filename rather than in a title, a tag or a body.

    Four things are pinned, and the middle two are the ones that make the
    first mean anything:

    (a) The open-ceiling probe refuses, with the reason pinned to the module's
        own ``_OUT_OF_SCOPE_REASON``. A bare ``status == "refused"`` would
        also pass if the tool merely stopped resolving paths — it has a
        "not found" refusal too, and at ``ceiling=intimate``/``all`` an
        "outside the vault" one as well, and neither would say anything about
        scope. Below that ceiling those two collapse into this same fixed
        string on purpose (the Gate-2.5 review's M1: a distinguishable
        off-vault message is an oracle for "there is an outward symlink at
        exactly this path"), which is what makes the reason pin here a
        statement about scope rather than about resolution.
    (b) The *same* ``01-Fragments`` scan at ``ceiling=all`` returns
        ``status="ok"`` and its serialised response contains the intimate
        canary. This is the sharpest anti-vacuity control available anywhere
        in layer (f): it proves the sentinel was genuinely reachable through
        this exact call, so the open refusal is what withheld it rather than a
        fixture that never carried it.
    (c) A scan of ``00-Creek-Meta/Inbound`` at ``ceiling=open`` still returns
        findings naming the staged canary. FEAT-027 is the reason this tool
        exists; CrawDad calls it at the channel's configured ceiling —
        ``personal`` by default, ``open`` where a channel is mapped to it —
        so admission has to hold at the lowest of them, and a scope gate that
        refused everything would satisfy (a) perfectly while breaking the only
        production caller.
    (d) The vault root appears in none of the three responses — the second
        half of #972, where ``report_markdown`` rendered absolute paths and
        leaked the operator's home directory alongside the filename.

    ``load_config`` is stubbed for the admitted calls; the refused one returns
    above it.
    """
    monkeypatch.setattr(
        "creek_mcp.tools.redact.load_config",
        lambda: CreekConfig(),
    )
    vault_root = str(canary_vault.resolve())

    refused = _probe_redact_scan(canary_vault)
    assert refused["status"] == "refused"
    assert refused["reason"] == _OUT_OF_SCOPE_REASON, (
        "creek.redact.scan refused for some reason other than scope, so this "
        "probe is evidence that the tool stopped working rather than that it "
        f"started enforcing anything.\n\n{refused}"
    )
    assert refused["tier_ceiling"] == TierCeiling.OPEN.value

    admitted = redact_scan_tool(
        vault_path=canary_vault,
        input_path=_REDACT_SCAN_TARGET,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert admitted["status"] == "ok"
    assert _RUNTIME_INTIMATE_CANARY in json.dumps(admitted, default=str), (
        "the same 01-Fragments scan at ceiling=all did not surface the "
        "intimate carrier's filename, so the open-ceiling refusal above "
        "withheld nothing and every canary assertion in layer (f) is vacuous "
        f"for this tool.\n\n{admitted}"
    )

    staged = redact_scan_tool(
        vault_path=canary_vault,
        input_path=_REDACT_STAGING_TARGET,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert staged["status"] == "ok"
    assert _RUNTIME_OPEN_CANARY in json.dumps(staged, default=str), (
        "the FEAT-027 staging subtree is no longer scannable at ceiling=open. "
        "That is the one call CrawDad makes, and a scope gate that refuses it "
        f"has replaced a disclosure with an outage.\n\n{staged}"
    )

    for response in (refused, admitted, staged):
        assert vault_root not in json.dumps(response, default=str), (
            "a creek.redact.scan response embeds the vault root, leaking the "
            "operator's home directory into whatever CrawDad posts next.\n\n"
            f"{response}"
        )
