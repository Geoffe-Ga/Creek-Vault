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

A manifest is only worth having if it cannot lie. Seven layers keep it honest:

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
    the same forcing function as layer (a), one level deeper. That forcing
    function cannot tell probing from excusing, so two assertions stand behind
    it (#1279): the exemption set's membership is **pinned**, and every
    exemption must name a test in :data:`_PROBE_EXEMPT_GUARDS` that
    **executes** its reason. Both were missing while ``creek.author`` sat here
    on a reason whose every load-bearing clause was false.
(g) **Prompt-channel canary probe** — layer (f) at the other end of the wire.
    Every ``GATED`` tool that can hand corpus text to a model is driven all
    the way *to* the provider with a recording factory, and the gated
    sentinels must appear in none of the prompts it sent — nor in the
    response of that same call. Two positive controls run first: a prompt
    was captured, and the *admitted* canary is in it. A probe that reached
    no model, or built a prompt with no corpus in it, would satisfy every
    exclusion while proving nothing. The subject set is **derived** rather
    than listed (:data:`_LLM_BACKED_GATED_TOOLS` — every
    ``GATED`` tool whose own module defines a function taking both
    ``llm_factory`` and ``privacy_tier_ceiling``; four of them today), so a
    tool cannot *become* LLM-backed without being asked for a probe. Forced
    by a manifest pair of its own (:data:`_PROMPT_PROBES` /
    :data:`_PROMPT_PROBE_EXEMPT`) exactly as (f) is, plus one assertion (f)
    does not need: the derived set must be **non-empty**. A posture somebody
    declared cannot quietly empty itself; a set computed by signature
    introspection can, on nothing worse than a parameter rename, and would
    take the whole layer green with it.

Layer (f) exists because layers (a)-(e) are **structural**. Between them they
prove that a gate is declared in the manifest, exists in the named module, and
is invoked there. None of that can prove the gate is the *only* path to the
corpus: a second, ungated read added alongside the still-present, still-called
gate satisfies every structural check. Only calling the tool and looking at
what comes back can see that, which is what (f) does.

Layer (g) exists because layers (a)-(f) all terminate at the **response
envelope**. Even (f), the only one that runs the tool at all, reads exactly
what the caller got back — and the prompt leaves the process on its way to the
provider, before any envelope exists. So a tool can paste an intimate fragment
into a prompt, ship it to a cloud endpoint, and return an envelope (f)
certifies as canary-free: the disclosure already happened, off-envelope, where
nothing above was looking. ``creek.reflect`` is the concrete case rather than
the hypothetical one. The manifest records three gates for it, and (f) can only
ever reach the first: ``_probe_reflect`` passes ``entry_ref=<intimate id>``,
which the #846 gate refuses *before* the grounding walk, so
``creek_mcp.tools.reflect._default_retrieve`` — the second corpus read, live
since #964 — is never exercised by that probe at all. The same is true of the
third corpus read, ``creek_mcp.compiled_pages`` (#873), which sits below the
same #846 gate; its own admission rule is exercised directly by
``tests/test_mcp_compiled_pages.py`` and end-to-end through ``reflect_tool``
by ``tests/test_mcp_reflect.py``.

Within (g), the **personal** canary is the assertion that matters. Excluding
``intimate`` is close to free: every path in this repo drops it. ``personal``
is the tier the known leak shape survives at — at ``ceiling=open`` the
pipeline drops intimate outright, but
:func:`~creek.classify.privacy_filter.filter_fragments_by_tier` *summarises*
personal to ``[Personal-tier summary: {title}]``, so an above-ceiling personal
**title** reaches the prompt by design on that path. That is the
personal-summary residue shape, pinned as live behaviour by
``tests/test_mcp_tools.py``'s
``test_draft_prompt_carries_the_personal_title_at_an_open_ceiling``. A layer
whose only security assertion were "intimate not in prompt" would therefore be
green forever over the one channel shape this repo already knows leaks.

Injection drill. Steps 1-4 each turn exactly one layer red; step 5 turns
two, and which two is the whole of what it measures:

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
5. Neutralise the hard rank cutoff every Writing Desk consumer's corpus walk
   runs through — ``creek.author.agents.tier_within_override``, called by
   ``_load_corpus`` and therefore by the grounding retrieval behind
   ``creek_mcp.tools.reflect._default_retrieve``::

       monkeypatch.setattr(
           "creek.author.agents.tier_within_override",
           lambda *_a, **_k: True,
       )

   → layers (f) and (g) both fail. Re-measured on this worktree after #1279
   gave ``creek.author`` a layer-(f) probe of its own: (g)'s
   ``[creek.reflect]`` and ``[creek.author]`` go red, and so do (f)'s
   ``[creek.author]`` — on the intimate canary — and
   ``test_author_probe_still_cites_the_corpus_it_is_admitted_to``, whose
   ``factory.tiers`` reads ``[PrivacyTier.INTIMATE]`` instead of ``[OPEN]``.
   **Every other response probe in** :data:`_RUNTIME_PROBES` **passes**,
   ``[creek.reflect]`` included, and that survivor is what this step is
   really measuring. Layer (f) cannot see this mutation *for reflect*
   specifically: ``_probe_reflect`` is refused at the #846 entry gate and
   never reaches the grounding walk the mutation opens, so the second corpus
   read is not exercised whether or not it admits everything.
   ``creek.author`` reaches that same walk with no gate above it, which is
   why its probe does see it — and why (f) covering one consumer of a shared
   cutoff says nothing about the others.

   Until #1279 this step read "and **only** layer (g)", with every layer-(f)
   probe green. That was true only because ``creek.author`` was exempt from
   (f) on a reason whose every load-bearing clause was false. The sentence is
   corrected here from a fresh measurement rather than renumbered.

   A second, independent injection was run to check that the *reflect*
   observation is a property of the channel rather than of one mutation, and
   it is scoped to reflect's own module: forcing
   ``creek_mcp.tools.reflect``'s ``to_privacy_override`` to return
   ``PrivacyTierOverride.ALL`` reddens (g)'s ``[creek.reflect]`` and leaves
   **every** layer-(f) probe passing — ``[creek.author]`` and its positive
   control included, measured.

Deliberately absent: any runtime probe asserting that a known gap *still
leaks*. Such a test passes because the bug exists and breaks when it is fixed.
Each gap issue (#968/#969/#970/#971, and #972 before them) carried its own
runtime-probe acceptance criterion instead, discharged in this file as the gap
closed. With #971 the last of them is closed and the manifest records **no**
ungated gaps at all — a state layers (d) and (e) now describe rather than
enforce, which is why
:func:`test_the_manifest_records_no_ungated_gaps` exists to say so out loud and
why the four gap-driven tests below are written as loops over an empty
collection rather than as parametrisations of one. The next gap re-arms them.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import importlib
import inspect
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import frontmatter
import pytest

from creek.author.client import AuthorLLMClient
from creek.classify.llm.completion import Completion
from creek.config import CreekConfig
from creek.generate.mining import MiningStrategy
from creek.models import PrivacyTier
from creek_mcp.policy import Transport
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
from creek_mcp.tools.author import author_tool
from creek_mcp.tools.classify_entry import entry_classification_tool
from creek_mcp.tools.compile import _ABOVE_CEILING_REASON, compile_tool
from creek_mcp.tools.draft import draft_tool
from creek_mcp.tools.journal import journal_ingest_tool
from creek_mcp.tools.mine import mine_tool
from creek_mcp.tools.redact import _OUT_OF_SCOPE_REASON, redact_scan_tool
from creek_mcp.tools.reflect import reflect_tool
from creek_mcp.tools.report import report_tool
from creek_mcp.tools.skills import skills_refresh_tool
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

_EXPECTED_TOOL_COUNT = 25

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
    # #874's ``creek.classify.entry`` is born gated, and it is the third
    # adopter of ``refuse_above_ceiling`` on ``creek.state.read``'s exact
    # argument rather than a fourth shape: an ``entry_ref`` is caller-ADDRESSED
    # and singular, so there is nothing to partially admit — you cannot return
    # half a fragment's frequency. Pinned as its own row because the pair is
    # the checkable part: the tool also imports ``source_tiers`` and
    # ``max_source_tier``, and a manifest re-pointed at either of those would
    # read as a gate while naming a function that only *reads a tier* and
    # decides nothing.
    (
        "creek.classify.entry",
        "creek_mcp.tools.classify_entry",
        "refuse_above_ceiling",
    ),
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
    # #971 closed the skills gap, and this row is a re-point of exactly the
    # kind #968/#969/#970 made: the entry moves from a gap claim to a gate
    # claim layers (c) and (f) can check, rather than being deleted.
    #
    # ``creek.skills.refresh`` is the third tool to reach for the HARD CUTOFF
    # (``creek.classify.privacy_filter.tier_within_override``) inside the
    # generator rather than adopting either canonical primitive, and for
    # ``creek.report``'s reason one step further in: a skill file is a
    # voice-exemplar corpus, so ``filter_fragments_by_tier``'s
    # ``"[Personal-tier summary: <title>]"`` stub would be written into
    # ``## Exemplar Passages`` as a quoted passage — leaking the title it was
    # built from, beside the fragment id in bold, and teaching the model a
    # sentence nobody wrote. A skill tree must omit, not summarise.
    #
    # What layers (c)/(e) can therefore see is the *ceiling conversion*, not
    # the cutoff: ``to_privacy_override`` is what turns the caller's
    # ``TierCeiling`` into the ``PrivacyTierOverride`` the generator is
    # constructed with, and it is the one symbol whose absence from this module
    # would mean the ceiling never left the tool wrapper — which is precisely
    # what #971 was. The cutoff itself lives one layer down in
    # ``creek/generate/skills.py`` and is pinned behaviourally by
    # ``tests/test_skills_tier_ceiling.py``.
    ("creek.skills.refresh", "creek_mcp.tools.skills", "to_privacy_override"),
]

_PINNED_GAPS: dict[str, int] = {}
"""Tools pinned as ``UNGATED_KNOWN_GAP``, with the issue tracking each.

**Empty on purpose, and empty for the first time.** Every gap this table has
ever held has closed — #968 (report), #969 (both state tools), #970 (journal),
#972 (redact.scan) and finally #971 (skills.refresh) — and each was *re-pointed*
into :data:`_PINNED_GATE_ROWS` at the gate that closed it rather than deleted,
so the claim stays checkable by layers (c) and (e) instead of disappearing.

The table stays here rather than being removed with its last row: the next tool
triaged as a known gap re-arms this pin and the three tests that read
:data:`_UNGATED_GAP_TOOLS` / :data:`_GAP_ISSUE_TOOLS`, and a deleted table is
one a future author has to reinvent (and might not).
"""

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
_PERSONAL_SUMMARY_PREFIX = "[Personal-tier summary:"
"""The opening of ``_summarize_personal``'s stub, title-independent.

Named separately from :data:`_PERSONAL_SUMMARY` because #971 needs to assert
the stub's *absence* from an artifact whose title it cannot predict: the whole
claim is that no such summary was written, for any fragment.
"""
_PERSONAL_SUMMARY = f"{_PERSONAL_SUMMARY_PREFIX} {_PERSONAL_TITLE}]"

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
        transport=Transport.STDIO,
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
    """The MCP surface is 25 tools; growing it is a deliberate act.

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


def test_pinned_gaps_keep_their_posture_and_issue() -> None:
    """The known-ungated tools stay labelled as gaps against their own issues.

    These read vault content without honouring the caller's ceiling. The
    posture is the honest record of that, and the issue number is the promise
    that someone is on the hook for it. Relabelling either one — without the
    code changing — converts a tracked gap into an invisible one.

    The table shrinks as gaps close: #968 took ``creek.report`` out of it, #969
    the two ``creek.state.*`` entries, #970 ``creek.journal`` and #971
    ``creek.skills.refresh`` — each *re-pointed* at the gate that closed it in
    :data:`_PINNED_GATE_ROWS` rather than merely deleted, so the claim stays
    checkable by layers (c) and (e) instead of disappearing.

    **Written as a loop rather than a parametrisation, and that is not style.**
    :data:`_PINNED_GAPS` is now empty, and ``pyproject.toml`` sets no
    ``empty_parameter_set_mark``, so pytest's default of ``skip`` would have
    turned this test into a silent skip behind a green gate — the assertion
    still in the file, still read as protection, and never executed again. A
    bare loop over an empty collection is a *pass*, which is honest: there is
    nothing to check today, and the moment a gap is recorded the loop has
    something to check with no edit required.
    :func:`test_the_manifest_records_no_ungated_gaps` is what keeps the empty
    state itself asserted rather than merely tolerated.
    """
    for tool, issue in sorted(_PINNED_GAPS.items()):
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


def test_gap_issue_numbers_are_positive_integers() -> None:
    """A tracking issue is a positive integer, not a placeholder.

    Guards the obvious escape hatches — ``0``, ``-1``, or a bool sneaking
    through ``int`` — that would satisfy a bare "is not None" check while
    pointing at no issue anyone can open.

    A loop rather than a parametrisation over :data:`_GAP_ISSUE_TOOLS`, which
    #971 emptied. ``pyproject.toml`` sets no ``empty_parameter_set_mark``, so
    pytest's default of ``skip`` would have deleted this check from the suite
    silently the moment the last gap closed; a loop over an empty list passes
    instead, and re-arms itself the moment a gap is recorded.
    """
    for tool in _GAP_ISSUE_TOOLS:
        issue = TOOL_POSTURES[tool].gap_issue
        assert isinstance(issue, int)
        assert not isinstance(issue, bool)
        assert issue > 0


def test_gap_issues_are_named_in_their_own_tool_module() -> None:
    """The tool's own source names the issue tracking its gap.

    The manifest is a file most readers of ``creek_mcp/tools/report.py`` will
    never open. Requiring the literal ``#<issue>`` in the module itself means
    the person editing the tool learns about the gap where they already are,
    and cannot extend the tool believing its reads are ceiling-filtered.

    A loop rather than a parametrisation, for the reason recorded on
    :func:`test_gap_issue_numbers_are_positive_integers`: an empty argument set
    is a silent skip under pytest's default ``empty_parameter_set_mark``, and a
    guardrail that skips itself out of existence when its subject list empties
    is worse than no guardrail, because the file still reads as protection.
    """
    for tool in _GAP_ISSUE_TOOLS:
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


def test_ungated_gap_tools_do_not_call_a_canonical_gate_primitive() -> None:
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

    Looped over :data:`_UNGATED_GAP_TOOLS` rather than parametrised over it,
    now that #971 emptied the list. Under pytest's default
    ``empty_parameter_set_mark`` an empty argument set is a *skip*, so the
    anti-rot layer would have quietly stopped running at exactly the moment its
    subject list emptied — and the next gap recorded would have inherited a
    skipped guardrail that looks, in the file, like a live one.
    """
    for tool in _UNGATED_GAP_TOOLS:
        module = _import_tool_module(tool)
        for primitive in sorted(CANONICAL_GATE_PRIMITIVES):
            assert not _calls_symbol(module, primitive), (
                f"{tool} is recorded as an ungated known gap "
                f"(#{TOOL_POSTURES[tool].gap_issue}) but "
                f"{_module_path_for(tool)} already calls {primitive}. If the "
                "gap is closed, change the posture to GATED and name the gate; "
                "if it is not, this call is misleading."
            )


def test_the_manifest_records_no_ungated_gaps() -> None:
    """Every tracked read-side gap on the MCP surface is closed (#971).

    The three tests above are now loops over empty collections, which is the
    honest shape for "there is nothing to check" — but an empty loop passes just
    as happily when the collection is empty *by accident*, e.g. because someone
    renamed ``ReadPosture.UNGATED_KNOWN_GAP`` or broke the comprehension that
    derives these lists. This test states the empty set as a claim, so the
    layer keeps teeth: reaching zero ungated gaps was the point of #968, #969,
    #970, #972 and #971, and staying there is a property worth failing on.

    When a new gap is genuinely triaged, this is the test that says so out loud
    — and its failure is the prompt to add the tool to :data:`_PINNED_GAPS`,
    which re-arms the three loops above.
    """
    assert _UNGATED_GAP_TOOLS == [], (
        "the manifest records ungated known gap(s) again: "
        f"{_UNGATED_GAP_TOOLS}. That is allowed — a tracked gap is better than "
        "an untracked one — but it must be deliberate: add each tool to "
        "_PINNED_GAPS with its issue number so the loops above start checking "
        "it, and update this assertion."
    )
    assert _GAP_ISSUE_TOOLS == [], (
        "a TOOL_POSTURES entry carries a gap_issue again: "
        f"{_GAP_ISSUE_TOOLS}. A gap_issue on a GATED entry advertises a hole "
        "that is not there and sends an auditor hunting a closed issue — see "
        "test_journal_carries_no_residual_gap_issue for the same argument one "
        "tool at a time."
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
    :func:`creek_mcp.tools.reflect._admit_entry` carries a comment block at its
    ``_above_ceiling`` refusal saying so (cited by symbol per #1103; the line
    span this sentence used to name had drifted onto a different function):
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

# A third sentinel and a third id, belonging to layer (g) (#1036) but declared
# here with the other two so the next probe author picks a value that cannot
# collide with one already in flight. Layer (f) has no use for a ``personal``
# fragment; layer (g) does, and the reason is the whole point of that layer —
# ``personal`` is the tier the personal-summary residue survives at, so a
# prompt-channel assertion that only knows about ``intimate`` is green over
# the one leak this repo has already found. See
# :func:`_assert_prompt_channel_clean`.
_RUNTIME_PERSONAL_CANARY = "CANARY-RUNTIME-PERSONAL-6c19"
_RUNTIME_PERSONAL_ID = "frag-runtime-personal"

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
        "draft_tool DOES answer status='ok' on a bare fixture, so nothing "
        "about the response shape is what excuses it. The reason is upstream "
        "of the envelope: on any fixture this small the IdeaMiner falls back "
        "to MiningStrategy.UNEXPLORED_ONTOLOGY, whose seed is built from "
        "ontology enum labels rather than from the corpus, so the response "
        "comes back with source_fragments == [] and no fragment title, tag or "
        "body of ANY tier in it. A canary assertion over that envelope would "
        "be vacuous in both directions — the intimate exclusion could not "
        "fail and the open positive control could not pass. Its corpus walk "
        "is IdeaMiner(privacy_override=to_privacy_override(ceiling)) — "
        "byte-for-byte the walk creek.mine is probed through below — so the "
        "read path is covered even though draft's own envelope is not. "
        "test_the_draft_response_exemption_is_still_true executes this reason "
        "rather than trusting it."
    ),
}
"""``GATED`` tools that cannot be probed *here*, each with the reason why.

Read these as claims, not as boilerplate: an exemption is the one way a
``GATED`` tool escapes layer (f), so a reason that does not survive being
checked against the tool's source is a hole in the layer.

``creek.author`` was the second entry here until **#1279**. Every
load-bearing clause of its reason was false — ``author_tool`` calls no
``load_config``, its ``except Exception`` span is never entered on the canary
fixture, and the desk answers ``status='ok'`` with cited corpus content on a
vault with no config file, no credential and no skills tree. It is probed by
:func:`_probe_author` now. Two things kept that claim alive for as long as it
stood, and both are closed above: nothing pinned the *membership* of this
dict, and nothing *executed* any reason in it. See
:func:`test_the_response_probe_exemption_set_is_pinned` and
:data:`_PROBE_EXEMPT_GUARDS`.
"""

_PROBE_EXEMPT_GUARDS: dict[str, str] = {
    "creek.draft": "test_the_draft_response_exemption_is_still_true",
}
"""Layer-(f) exemption → the test that *executes* its stated reason.

Kept as a second dict rather than folded into :data:`_PROBE_EXEMPT`, and the
difference is exactly the one this issue is about. An exemption reason may
*mention* its guard — the entry above does — but a mention is prose, and
nothing resolves it; that is how ``creek.author``'s reason could name a
``load_config`` call the tool does not make. A key here is a name
:func:`test_every_response_probe_exemption_has_an_executable_guard` looks up
in module globals and asserts callable, in both directions. Folding the two
together would put that name inside the string the prose grader measures for
length and specificity, which is how a registry entry starts counting as an
argument.
"""


def _forbidden_llm_factory(tier: PrivacyTier) -> Any:
    """Fail loudly instead of returning an LLM — the gate must never reach here.

    Both probed tools that take it — ``creek.reflect`` and ``creek.compile`` —
    build their model client *below* the ceiling gate, so on an above-ceiling
    request this factory must not be invoked at all. It is deliberately **not**
    given to :func:`_probe_author`, which addresses no above-ceiling target and
    therefore reaches the model legitimately at ``ceiling=open``; that probe's
    docstring gives the argument.

    A stub that quietly returned ``"{}"`` would let a broken gate look clean:
    the tool would call the model, get nothing quotable back, and answer with
    a canary-free envelope. Raising turns that same path into a failure
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


_SKILLS_PERSONAL_CANARY = "CANARY-RUNTIME-SKILLS-PERSONAL-2f19"
"""The sentinel #971 actually reproduced: a *personal* body in a SKILL file.

Its own value rather than a reuse of :data:`_PERSONAL_BODY`, for the reason
recorded above :data:`_RUNTIME_OPEN_CANARY`: a sentinel shared between two
fixtures makes a failure ambiguous about which one leaked.
"""

_SKILLS_EXEMPLAR_WORDS_MIN = 30
"""Mirrors ``creek.generate.skills._EXEMPLAR_WORDS_MIN``.

Repeated rather than imported so the helper below asserts against the floor a
*reader* of this file can see, and so a change to the private constant surfaces
here as a failed assertion rather than as a silently-vacuous probe.
"""


def _skills_exemplar_body(sentinel: str) -> str:
    """Return a body long enough for the skill generator to actually quote.

    ``_extract_passage`` accumulates whole sentences until it clears
    ``_EXEMPLAR_WORDS_MIN`` (30) and yields nothing at all below it. Every body
    in ``canary_vault`` is four words long, so a skills probe run over the bare
    fixture would produce an exemplar-free tree — canary-free whether or not any
    ceiling was enforced, which is a green layer (f) that proves nothing. This
    is the same vacuity trap :func:`_seed_state_carriers` and
    :func:`_seed_redact_canaries` document, arriving by a third route.

    The sentinel opens the first sentence so it lands *inside* the extracted
    passage rather than past whatever boundary the extractor stops at.

    Args:
        sentinel: The canary this body carries.

    Returns:
        A body whose first four sentences total 34 words.
    """
    body = (
        f"{sentinel} opens this note and names it. "
        "I sat down at the table and wrote until the light changed. "
        "The house was quiet then. "
        "Nobody asked me what I meant by any of it. "
        "I kept the page and read it back the next morning."
    )
    assert len(body.split()) > _SKILLS_EXEMPLAR_WORDS_MIN, (
        "the skills carrier body no longer clears _EXEMPLAR_WORDS_MIN, so the "
        "generator would quote nothing and the probe would be vacuous"
    )
    return body


def _seed_skills_canaries(vault: Path) -> None:
    """Give the skills probe carriers the generator will really quote.

    Two independent reasons the shared ``canary_vault`` cannot serve here, and
    either alone would make the probe vacuous:

    * Its bodies are four words long, under ``_EXEMPLAR_WORDS_MIN`` (30), so no
      exemplar is built from them and no title, id or body is ever rendered.
    * It holds no ``personal`` fragment at all, and ``personal`` is the tier
      #971 leaked. ``intimate`` was already excluded by
      ``_is_snapshot_fragment``'s ``allow_intimate=False`` hardcode, so an
      intimate-only fixture would come back clean against the *unfixed* tool.

    The fixture is deliberately **not** extended to fix this. Ten other probes
    assert exact sets and exact counts against ``canary_vault`` —
    ``test_wheel_probe_still_counts_the_fragment_it_is_admitted_to`` pins
    ``total_classified == 1`` — so a third fragment there would break tools that
    have nothing to do with skills.

    Three carriers are seeded instead: ``open`` (the positive control, asserted
    *present* so a gate broken in the drop-everything direction cannot pass by
    writing an empty tree), ``personal`` (the leak), and ``intimate`` (which
    must stay out even though a different gate already keeps it out — so a fix
    that *replaced* the consent gate rather than ANDing with it is caught).

    Args:
        vault: The seeded canary vault, mutated in place.
    """
    for frag_id, tier, canary in (
        ("frag-skills-open", "open", _RUNTIME_OPEN_CANARY),
        ("frag-skills-personal", "personal", _SKILLS_PERSONAL_CANARY),
        ("frag-skills-intimate", "intimate", _RUNTIME_INTIMATE_CANARY),
    ):
        _write_fragment(
            vault,
            frag_id=frag_id,
            title=f"Skills carrier {frag_id}",
            body=_skills_exemplar_body(canary),
            privacy_tier=tier,
        )


def _probe_skills_refresh(vault: Path) -> dict[str, Any]:
    """Call ``creek.skills.refresh`` at the open ceiling over the canary vault.

    **The shared layer-(f) assertion is near-vacuous for this tool, and saying
    so is the point.** ``skills_refresh_tool`` returns ``skill_count`` and a
    list of vault-relative *paths* — never a title, a tag or a body — so its
    envelope was canary-free for the whole life of #971 and would stay
    canary-free under a completely ungated generator. The envelope sweep still
    earns its place as a tripwire against a future response shape that does
    carry content, exactly as it does for ``creek.report`` and
    ``creek.journal``, but it is not evidence about this gap.

    The real weight is carried two places: file-level here by
    :func:`test_skills_refresh_probe_leaves_no_canary_in_the_tree_it_writes`,
    and end-to-end across both production surfaces (MCP *and*
    ``creek skills generate``) by ``tests/test_skills_tier_ceiling.py``.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    _seed_skills_canaries(vault)
    return skills_refresh_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="read-gate-probe",
    )


def _skills_tree_blob(vault: Path) -> str:
    """Return the text of every file in the generated skill tree.

    A whole-tree sweep rather than one named file: a single fragment reaches
    the frequency, phase, mode and register categories, so a leak into any of
    them is the same leak. Naming one file would pin the shape of the tree that
    exists today.

    Args:
        vault: Vault root; the tree is read from ``<vault>/creek-skills``.

    Returns:
        One string covering every ``*.md`` under the tree.
    """
    return "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((vault / "creek-skills").rglob("*.md"))
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


def _probe_classify_entry(vault: Path) -> dict[str, Any]:
    """Address the *intimate* canary fragment at the open ceiling.

    **The shared envelope sweep is near-vacuous for this tool, and saying so
    is the point** — the same honesty ``_probe_skills_refresh`` records.
    ``entry_classification_tool`` publishes four bounded enum-valued strings
    plus the caller's own echoed input; it returns no title, tag or body, so
    its envelope would be canary-free even with the gate deleted outright. The
    sweep still earns its place as a tripwire against a future response shape
    that does carry content.

    The real weight for this tool is carried by
    :func:`test_classify_entry_probe_refuses_the_fragment_it_is_not_admitted_to`
    below, which asserts the *verdict* rather than the envelope — refused for
    the intimate fragment, ``ok`` for the open one, at the same ceiling.

    Args:
        vault: The seeded canary vault.

    Returns:
        The tool's response envelope.
    """
    return entry_classification_tool(
        vault_path=vault,
        entry_ref=_RUNTIME_INTIMATE_ID,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="read-gate-probe",
    )


def _probe_author(
    vault: Path,
    factory: _RecordingAuthorFactory | None = None,
) -> dict[str, Any]:
    """Run the Writing Desk at the open ceiling and return its envelope (#1279).

    ``creek.author`` carried a layer-(f) *exemption* until #1279, on a reason
    whose every load-bearing clause was false: ``author_tool`` calls no
    ``load_config``, the blanket ``except Exception`` span it named is never
    entered here, and the desk does not "never start". Called on the two-
    fragment :func:`canary_vault` with no ``creek_config.yaml``, no credential
    in the environment and no deployed skills tree, it answers ``status="ok"``
    with the admitted fragment cited in its provenance. There was never
    anything to excuse.

    Four choices in the call below are load-bearing, and each is a way this
    probe could quietly become vacuous:

    * ``dry_run=False``. A dry run returns ``plan_author``'s plan and evidence
      summary *before* the desk speaks, so the envelope a dry-run probe swept
      would never have been through the voice seam at all.
    * :class:`_RecordingAuthorFactory`, **not**
      :func:`_forbidden_llm_factory`. That factory is the right one for
      ``creek.reflect`` and ``creek.compile``, which refuse an above-ceiling
      request above the point a client is built, so reaching it is the bug it
      reports. ``creek.author`` addresses no above-ceiling target: it takes a
      free-text ``query`` and ranks the corpus behind
      :func:`~creek.classify.privacy_filter.tier_within_override`, so at
      ``ceiling=open`` it *legitimately* reaches the model and
      :func:`_forbidden_llm_factory` would raise on correct behaviour.
    * ``llm_factory`` is passed at all. ``llm_factory=None`` is a supported
      call and would keep this probe hermetic, but the conductor's factory
      branch falls through to a ``None`` client, so the desk runs its
      deterministic renderer instead of the #1254/#1260 voice seam — a
      different code path, measurably so: three rounds against this fixture
      where the recorder takes one. The seam the real tool uses would go
      unexercised. It also leaves
      :func:`test_author_probe_still_cites_the_corpus_it_is_admitted_to`
      nothing to inspect: with no factory object there is no record of whether
      the desk reached a model, nor of which tier it routed by.
    * ``_RecordingAuthorFactory`` and :data:`_AUTHOR_PROMPT_PROBE_QUERY` are
      both defined **below** this function, with layer (g)'s constants. The
      forward reference is deliberate and resolves at call time. Hoisting the
      recorder would drag ``_CANNED_LLM_RESPONSE``, ``_RECORDING_MODEL_ID`` and
      ``_RECORDING_USAGE`` up with it and split the block it shares with
      :class:`_RecordingLLMFactory`; registering this probe into
      :data:`_RUNTIME_PROBES` later, from below the recorder, is not an option
      because the parametrisation over that dict is evaluated at import. Do
      not "tidy" the order: moving this definition below the dict is a
      ``NameError`` at collection.

    Args:
        vault: The seeded canary vault.
        factory: An optional recorder to drive the desk through, so a caller
            that wants to assert on what crossed to the model uses the *same*
            call site the sweep runs rather than a second one that could drift
            from it. Defaults to a fresh recorder.

    Returns:
        The tool's response envelope.
    """
    recorder = factory if factory is not None else _RecordingAuthorFactory()
    return author_tool(
        vault_path=vault,
        query=_AUTHOR_PROMPT_PROBE_QUERY,
        llm_factory=recorder,
        privacy_tier_ceiling=TierCeiling.OPEN,
        dry_run=False,
    )


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
    "creek.skills.refresh": _probe_skills_refresh,
    "creek.classify.entry": _probe_classify_entry,
    "creek.author": _probe_author,
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


def test_the_response_probe_exemption_set_is_pinned() -> None:
    """Only ``creek.draft`` escapes layer (f) through the exemption hatch.

    The forcing function above is satisfied by probing a tool *or* excusing
    it, so it cannot tell the two apart: moving ``creek.wheel`` out of
    :data:`_RUNTIME_PROBES` into :data:`_PROBE_EXEMPT` with any
    sixty-character string containing ``"wheel"`` would delete a probe and
    leave every layer-(f) test green.
    :func:`test_probe_exemptions_are_specific_to_their_tool` cannot catch it
    either -- it grades the prose, not the membership, and its own docstring
    concedes it cannot judge whether a reason is true.

    That is not hypothetical: it is how ``creek.author``'s exemption sat
    unchallenged while every load-bearing clause of it was false (#1279).
    Layer (g) has had this pin since #1036
    (:func:`test_the_prompt_probe_exemption_set_is_pinned`); layer (f) did
    not, and this is the counterpart. Pinned in the direction that matters:
    a tool moving *into* the exemption set is a real weakening and has to be
    argued for in a diff a reviewer can see.
    """
    assert set(_PROBE_EXEMPT) == {"creek.draft"}, (
        "the layer-(f) exemption set changed. Adding a tool here REMOVES "
        "the only assertion watching its response channel, so the change "
        "needs the same scrutiny as deleting a test: state in the PR what "
        "specifically stops the tool being called on a bare fixture, and "
        "update this pin deliberately. Currently exempt: "
        f"{sorted(_PROBE_EXEMPT)}."
    )


def test_every_response_probe_exemption_has_an_executable_guard() -> None:
    """Every layer-(f) exemption names a test that *runs* its stated reason.

    The pin above stops the set growing unnoticed; it says nothing about
    whether the reason in a new entry is true. That gap is the whole of
    #1279: ``creek.author``'s exemption was graded for prose by
    :func:`test_probe_exemptions_are_specific_to_their_tool`, pronounced
    specific and long enough, and was false in every load-bearing clause for
    as long as it stood, because nothing ever executed it.

    So the registry is the forcing function one level in from membership. An
    exemption is a claim about what happens when the tool is called; this
    requires that somebody actually call it and assert the claim's *cause*,
    the way :func:`test_the_draft_prompt_exemption_is_still_true` does for
    layer (g). Both directions are checked. A new exemption with no guard is
    an unexecuted claim -- the defect this issue exists to close. A guard
    entry naming a test that no longer exists (renamed, or deleted along with
    the probe it excused) leaves the exemption looking supervised while
    nothing runs, which is the same failure wearing a registry entry.
    """
    assert set(_PROBE_EXEMPT_GUARDS) == set(_PROBE_EXEMPT), (
        "every layer-(f) exemption needs a test that EXECUTES its reason, and "
        "the two manifests disagree. Exempt without a guard: "
        f"{sorted(set(_PROBE_EXEMPT) - set(_PROBE_EXEMPT_GUARDS))}; guards for "
        "tools that are not exempt: "
        f"{sorted(set(_PROBE_EXEMPT_GUARDS) - set(_PROBE_EXEMPT))}. A reason "
        "nobody runs is how creek.author's exemption stayed false through "
        "every review it passed (#1279): write the guard, or delete the "
        "exemption and write a probe."
    )
    for tool, guard in sorted(_PROBE_EXEMPT_GUARDS.items()):
        assert guard in globals(), (
            f"{tool}'s exemption guard {guard!r} does not resolve in this "
            "module, so the exemption is supervised on paper and unexecuted "
            "in fact. Point the entry at the test that runs the reason."
        )
        assert callable(globals()[guard]), (
            f"{tool}'s exemption guard {guard!r} resolves to something that "
            "is not callable, so no test runs the reason."
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


def test_classify_entry_probe_refuses_the_fragment_it_is_not_admitted_to(
    canary_vault: Path,
) -> None:
    """``creek.classify.entry``'s probe is not passing by publishing nothing.

    The layer-(f) envelope sweep cannot carry this tool: its response holds no
    title, tag or body, so it is canary-free with or without a gate. The
    checkable claim is the *verdict* under one fixed ceiling, and it needs both
    directions to mean anything.

    Negative control: the ``intimate`` fragment at ``ceiling=open`` must be
    ``refused``, carrying the generic reason and naming neither the fragment
    nor its tier. Positive control: the ``open`` fragment at the *same* ceiling
    must come back ``ok`` with a real classification — without it, a tool that
    refused unconditionally, or that had stopped reading the vault at all,
    would pass the negative half on its own.
    """
    refused = entry_classification_tool(
        vault_path=canary_vault,
        entry_ref=_RUNTIME_INTIMATE_ID,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert refused == {
        "status": "refused",
        "tool": "creek.classify.entry",
        "tier_ceiling": "open",
        "reason": GENERIC_ABOVE_CEILING_REASON,
    }

    admitted = entry_classification_tool(
        vault_path=canary_vault,
        entry_ref=_RUNTIME_OPEN_ID,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert admitted["status"] == "ok"
    assert admitted["entry_ref"] == _RUNTIME_OPEN_ID
    assert admitted["privacy_tier"] == "open"


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


def test_skills_refresh_probe_leaves_no_canary_in_the_tree_it_writes(
    canary_vault: Path,
) -> None:
    """``creek.skills.refresh``'s leak is the tree it writes, not the dict it returns.

    Read this as ``creek.report``'s test one tool over, and for the same reason.
    The shared assertion in
    ``test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling`` is
    **near-vacuous for this tool**: ``skills_refresh_tool`` returns a count and
    a list of vault-relative paths, never a title, a tag or a body, so its
    envelope was canary-free for the whole life of #971 and would be canary-free
    under a completely ungated generator too.

    The evidence that means anything lives in the bytes under
    ``<vault>/creek-skills``, where an admitted fragment is rendered as a quoted
    passage beneath ``> **<title>** (`<id>`)`` — body, title and id, three leaks
    from one admission.

    Both above-ceiling tiers are asserted, and they fail for different reasons:

    * ``personal`` is the tier #971 reproduced. Nothing but the new ceiling gate
      keeps it out.
    * ``intimate`` was *already* excluded, by ``_is_snapshot_fragment``'s
      ``allow_intimate=False`` hardcode. Its absence is therefore not evidence
      that the ceiling is enforced — it is a check that the ceiling gate was
      **ANDed with** the consent gate rather than substituted for it.

    ``_PERSONAL_SUMMARY_PREFIX`` is asserted absent because the other candidate
    fix — routing the corpus through ``filter_fragments_by_tier`` — would write
    ``"[Personal-tier summary: <title>]"`` into ``## Exemplar Passages`` as a
    voice exemplar, leaking the title and teaching the model a sentence nobody
    wrote. A skill tree must omit, not summarise; same call #968 made for
    ``creek.report``.

    The ``open`` canary is asserted *present* as the positive control, so a gate
    broken in the drop-everything direction cannot pass by writing an
    exemplar-free tree: leak-free, and useless.
    """
    response = _probe_skills_refresh(canary_vault)
    assert response["status"] == "ok"

    blob = _skills_tree_blob(canary_vault)
    assert blob, (
        "creek.skills.refresh wrote no skill tree at all, so every assertion "
        f"below is vacuous: {response}"
    )
    assert _RUNTIME_OPEN_CANARY in blob, (
        "creek.skills.refresh wrote a skill tree with no admitted exemplar in "
        "it, so the exclusion assertions below hold over an empty corpus."
    )
    assert _SKILLS_PERSONAL_CANARY not in blob, (
        "creek.skills.refresh copied a personal fragment's full body into "
        "<vault>/creek-skills at privacy_tier_ceiling=open. The response "
        "envelope carries only paths — it was clean before this gate existed "
        f"too — so nothing above this line could have caught it.\n\n{blob}"
    )
    assert _RUNTIME_INTIMATE_CANARY not in blob, (
        "an intimate fragment's body reached the skill tree. This was already "
        "excluded by _is_snapshot_fragment(allow_intimate=False) before #971, "
        "so seeing it here means the ceiling gate replaced the consent gate "
        f"instead of being ANDed with it.\n\n{blob}"
    )
    assert _PERSONAL_SUMMARY_PREFIX not in blob, (
        "the skill tree carries a filter_fragments_by_tier summary stub. The "
        "gate here must be the hard cutoff (tier_within_override): a summary "
        "written into ## Exemplar Passages is a fabricated voice exemplar "
        f"carrying the title it was built from.\n\n{blob}"
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

    ``load_vault_config`` is stubbed for the admitted calls; the refused one
    returns above it.
    """
    monkeypatch.setattr(
        "creek_mcp.tools.redact.load_vault_config",
        lambda _vault_path, **_kwargs: CreekConfig(),
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


def test_author_probe_still_cites_the_corpus_it_is_admitted_to(
    canary_vault: Path,
) -> None:
    """``creek.author``'s probe is not passing by never reaching the corpus.

    The response-side positive control for the Writing Desk, and until #1279
    it existed on **neither** channel: layer (f) excused the tool, and layer
    (g)'s probe asserts reachability of the open canary in the *prompt*, not
    in the envelope. An exemption can never carry a control like this, because
    an exemption's whole claim is that nothing worth asserting comes back.

    ``response["status"] == "ok"`` is asserted here rather than relying on the
    ``tool`` echo the shared sweep checks, and the difference is not
    cosmetic. ``creek.author`` is the one probed tool that wraps its whole
    span in a blanket ``except Exception``, and the envelope it answers with
    on that path -- :func:`creek_mcp.tools.author._error_response` -- sets
    ``"tool": TOOL_NAME`` itself. So the echo is satisfied by a crash. Only
    ``status`` separates a desk that ran from a desk that fell over, and a
    crashed desk would sail through every exclusion in
    ``test_gated_tools_leak_no_above_ceiling_content_at_the_open_ceiling``
    while proving nothing at all.

    The factory assertions are the second half, and they are about *routing*
    as much as about invocation. ``prompts`` non-empty says the desk reached a
    model rather than short-circuiting; every recorded tier being ``OPEN``
    says which tier it routed the corpus by, so a broken rank cutoff that
    admitted the intimate fragment is caught here — as ``PrivacyTier.INTIMATE``
    in ``factory.tiers`` — even before the canary assertions are consulted.

    ``all(...)`` over a non-empty ``tiers`` rather than ``tiers ==
    [PrivacyTier.OPEN]``, deliberately. The exact-list form additionally pins
    that the desk voices in exactly **one** round, which is true today and is
    not this test's claim: a future revision round would redden a security
    assertion for a reason that has nothing to do with the tier cutoff. The
    security property is "no tier above the ceiling was ever routed", and it
    holds for any number of calls. The non-emptiness half is belt-and-braces
    rather than a demonstrated tripwire: ``all()`` over an empty list is
    ``True``, so a desk that never reached a model would satisfy the tier
    clause by vacuity — but the assertion above it,
    ``len(factory.prompts) >= 1``, is the one that actually fires first in
    every such case reachable today.
    """
    factory = _RecordingAuthorFactory()
    response = _probe_author(canary_vault, factory)

    assert response["status"] == "ok", (
        "creek.author did not author on the canary fixture, so the shared "
        "layer-(f) canary sweep for this tool is asserting over an envelope "
        "the desk never filled. Note that the sweep's `tool` echo does NOT "
        "catch this: creek_mcp.tools.author._error_response sets "
        '"tool": TOOL_NAME on the failure path, so an error envelope '
        f"satisfies the echo and every exclusion beside it.\n\n{response}"
    )
    serialised = json.dumps(response, default=str)
    assert _RUNTIME_OPEN_CANARY in serialised, (
        "creek.author returned an envelope with no admitted corpus content in "
        f"it — the open canary {_RUNTIME_OPEN_CANARY!r} is absent — so the "
        "intimate exclusion in the shared sweep is vacuous for this tool. "
        "This is what a tier filter broken in the drop-everything direction "
        f"looks like: leak-free, and useless.\n\n{serialised}"
    )
    assert len(factory.prompts) >= 1, (
        "creek.author's probe reached no model at all, so the desk never ran "
        "the voice seam this probe exists to drive. Whatever the envelope "
        "says, it was not produced by the path the real tool takes."
    )
    assert factory.tiers and all(tier is PrivacyTier.OPEN for tier in factory.tiers), (
        "creek.author routed its corpus by "
        f"{factory.tiers!r} at privacy_tier_ceiling=open. Anything above OPEN "
        "here means the rank cutoff in creek.author.agents._load_corpus "
        "admitted content the caller was not entitled to, and the model "
        "client was built for that tier. An empty list fails too: it would "
        "mean the desk built no client at all, and every tier assertion over "
        "it would be vacuously true."
    )


def test_the_draft_response_exemption_is_still_true(
    canary_vault: Path,
) -> None:
    """``creek.draft``'s layer-(f) exemption is executed, not taken on trust.

    The layer-(g) counterpart of
    :func:`test_the_draft_prompt_exemption_is_still_true`, and it has to be
    written again rather than inherited: the two exemptions are claims about
    two different channels, over two different fixtures. This one runs over
    :func:`canary_vault` — the two-fragment vault layer (f) actually probes —
    and **not** over :func:`prompt_canary_vault`. The two agree today; a guard
    for a layer-(f) exemption run on layer (g)'s corpus would go on agreeing
    after they stopped, and would then be evidence about an environment this
    layer never enters.

    What is asserted is the reason's stated *cause*, not its conclusion: that
    draft answers ``ok`` (so the guard is on the path the exemption describes
    rather than on a crash), that the ontology-tuple fallback is the strategy
    that fired, that the seed cites no corpus fragment, and that the **open**
    canary is absent from the envelope. That last absence is the load-bearing
    one, exactly as it is on the prompt channel. Asserting only that the
    *gated* canaries are missing would leave the exemption green on the day
    draft starts putting corpus text in its response — the day it stops being
    true and starts needing a real probe.

    When this goes red the fix is never to edit the reason. It is to delete
    the entry from :data:`_PROBE_EXEMPT` and write ``_probe_draft``.
    """
    response = draft_tool(
        vault_path=canary_vault,
        llm_factory=_RecordingLLMFactory(),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert response["status"] == "ok", (
        "creek.draft did not draft on the layer-(f) canary fixture, so this "
        "guard is asserting over a path the exemption does not describe. The "
        "exemption's claim is about what a successful draft leaves out of its "
        f"envelope, not about a failure.\n\n{response}"
    )
    assert response["idea_strategy"] == MiningStrategy.UNEXPLORED_ONTOLOGY.value, (
        "creek.draft drafted from a strategy other than unexplored-ontology: "
        f"{response['idea_strategy']!r}. The exemption rests on the "
        "ontology-tuple seed being the only one this fixture can produce. "
        "Delete the exemption from _PROBE_EXEMPT and write a real probe."
    )
    assert response["source_fragments"] == [], (
        "creek.draft's seed now cites corpus fragments: "
        f"{response['source_fragments']}. The exemption claims no corpus text "
        "of any tier reaches the envelope; a cited fragment is how that stops "
        "being true. Delete the exemption from _PROBE_EXEMPT and write a real "
        "probe."
    )
    serialised = json.dumps(response, default=str)
    for canary in (_RUNTIME_OPEN_CANARY, _RUNTIME_INTIMATE_CANARY):
        assert canary not in serialised, (
            f"creek.draft's response now carries {canary!r}. The exemption "
            "says no corpus text of any tier reaches this envelope, which is "
            "the whole reason a canary probe over it would be vacuous — and "
            "it is no longer true. Delete the exemption from _PROBE_EXEMPT "
            f"and add a probe to _RUNTIME_PROBES.\n\n{serialised}"
        )


# ---------------------------------------------------------------------------
# Layer (g) — prompt-channel canary probe (#1036)
#
# Every layer above terminates at the response envelope. Even (f) — the only
# one that runs the tool at all — reads exactly what the caller got back. For a
# tool that hands corpus content to a model, that is the wrong end of the wire.
# The prompt leaves the process on its way *to* the provider; the response is
# written afterwards, by the model, out of whatever it was told. So a tool can
# paste an intimate fragment into its prompt, ship it to a cloud endpoint, and
# return an envelope layer (f) certifies as canary-free: the disclosure has
# already happened, off-envelope, where nothing in this file was looking. What
# follows watches the prompt.
# ---------------------------------------------------------------------------

# The pair of parameters that together mark a function as the place where the
# corpus meets a model: it takes a caller ceiling (so it reads gated content)
# and an LLM factory (so it has somewhere to send what it read).
_LLM_PROMPT_SIGNATURE_PARAMS = frozenset({"llm_factory", "privacy_tier_ceiling"})


def _defines_llm_backed_entrypoint(module: ModuleType) -> bool:
    """Return whether *module* itself defines a ceiling-taking, LLM-taking function.

    Membership is judged on ``fn.__module__`` rather than on the attribute
    merely being present: a tool module that imports a sibling's ``*_tool`` in
    order to call it would otherwise enrol itself on the strength of somebody
    else's signature, and the derived set would name the wrong tools while
    still looking derived.

    Args:
        module: An imported tool module.

    Returns:
        ``True`` when at least one function *defined in* this module accepts
        both ``llm_factory`` and ``privacy_tier_ceiling``.
    """
    return any(
        _LLM_PROMPT_SIGNATURE_PARAMS.issubset(inspect.signature(fn).parameters)
        for _name, fn in inspect.getmembers(module, inspect.isfunction)
        if fn.__module__ == module.__name__
    )


def _derive_llm_backed_gated_tools() -> list[str]:
    """Return the ``GATED`` tools whose own module can put corpus text in a prompt.

    Derived by introspection rather than hand-listed, and that choice is the
    layer. A hand-list is a snapshot of the day it was written: the next
    LLM-backed tool to be registered would be triaged into ``TOOL_POSTURES`` by
    layer (a), given a probe or an exemption by layer (f), and then inherit
    "response-probed, prompt-unchecked" in silence — because a list nobody
    recomputes cannot ask a new tool for anything. Computing the set from the
    tools' own signatures means a tool cannot *become* LLM-backed without the
    forcing function below demanding a prompt probe for it.

    Returns:
        Sorted tool names: every entry of :data:`_GATED_TOOLS` whose
        implementing module defines a function taking both ``llm_factory`` and
        ``privacy_tier_ceiling``.
    """
    return sorted(
        tool
        for tool in _GATED_TOOLS
        if _defines_llm_backed_entrypoint(_import_tool_module(tool))
    )


_LLM_BACKED_GATED_TOOLS = _derive_llm_backed_gated_tools()

# Pinned in the _EXPECTED_TOOL_COUNT idiom. The derivation above answers "which
# GATED tools talk to a model today"; this answers "which ones did when layer
# (g) was written". The two disagreeing is news in either direction.
_PINNED_LLM_BACKED_GATED_TOOLS = (
    "creek.author",
    "creek.compile",
    "creek.draft",
    "creek.reflect",
)


@dataclass(frozen=True)
class _PromptCapture:
    """The two egress channels of one call, captured together.

    Pairing them is the point: the same invocation has to be assertable against
    both, because a tool that withholds a fragment from its response while
    pasting it into its prompt is precisely what this layer exists to catch,
    and evidence gathered from two separate calls could never distinguish that
    from two different code paths.
    """

    response: dict[str, Any]
    prompts: tuple[str, ...]


def test_the_llm_backed_gated_set_is_pinned() -> None:
    """The derived LLM-backed set is the four tools layer (g) was written against.

    The derivation is the load-bearing half of this layer, so it gets a pin of
    its own. Without one, a fifth LLM-backed tool would be enrolled silently
    and surface only as an "unchecked" name in the forcing function below —
    correct, but arriving as a puzzle in somebody else's PR. Failing here first
    says what changed before saying what is missing.
    """
    assert tuple(_LLM_BACKED_GATED_TOOLS) == _PINNED_LLM_BACKED_GATED_TOOLS, (
        "a new LLM-backed GATED tool appeared: add it to the pin AND give it a "
        "prompt probe or a prompt-channel exemption. Derived "
        f"{_LLM_BACKED_GATED_TOOLS}, pinned "
        f"{list(_PINNED_LLM_BACKED_GATED_TOOLS)}."
    )


def test_the_prompt_probe_exemption_set_is_pinned() -> None:
    """Only ``creek.draft`` escapes layer (g) through the exemption hatch.

    The forcing function above is satisfied by probing a tool *or* excusing it,
    which means it cannot tell the two apart — moving ``creek.reflect`` out of
    :data:`_PROMPT_PROBES` and into :data:`_PROMPT_PROBE_EXEMPT` with any
    sixty-character string containing ``"reflect"`` would delete the sharpest
    probe in this layer and leave every layer-(g) test green.
    :func:`test_prompt_probe_exemptions_are_specific_to_their_tool` cannot
    catch it either: it grades the prose, not the membership.

    So the membership is pinned, and pinned in the direction that matters. A
    tool moving *into* the exemption set is a real weakening of this layer and
    has to be argued for in a diff a reviewer can see, which is exactly what
    failing here forces.
    """
    assert set(_PROMPT_PROBE_EXEMPT) == {"creek.draft"}, (
        "the layer-(g) exemption set changed. Adding a tool here REMOVES the "
        "only assertion that watches its prompt channel, so the change needs "
        "the same scrutiny as deleting a test: state in the PR why no prompt "
        "can be captured for it, and update this pin deliberately. Currently "
        f"exempt: {sorted(_PROMPT_PROBE_EXEMPT)}."
    )


def test_every_llm_backed_gated_tool_is_prompt_probed_or_exempt() -> None:
    """Each LLM-backed ``GATED`` tool is prompt-probed or exempt, never neither.

    Layer (f)'s forcing function, moved one channel over — with one difference
    that has to be asserted rather than assumed. Layer (f) is driven off a
    posture a human *declared* in the manifest, so its set cannot quietly empty
    itself. This set is computed from the tools' own signatures, so it can: the
    non-emptiness check comes first because an empty derivation would turn
    every remaining assertion here into a green statement about nothing.

    Disjointness and staleness are asserted for the same reasons they are in
    layer (f): a tool in both dicts carries an exemption nobody reads, and an
    entry for a tool that no longer belongs is a claim about nothing that
    inflates the apparent depth of the layer.
    """
    probed = set(_PROMPT_PROBES)
    exempt = set(_PROMPT_PROBE_EXEMPT)
    backed = set(_LLM_BACKED_GATED_TOOLS)
    assert backed, (
        "the LLM-backed GATED set derived EMPTY, so layer (g) is guarding "
        "nothing while reporting green. The set is computed by signature "
        "introspection over _GATED_TOOLS, which means it silently shrinks to "
        "[] if _import_tool_module stops resolving these modules or a tool "
        "renames its llm_factory / privacy_tier_ceiling parameter. Repair the "
        "derivation; never pin around it."
    )
    both = probed & exempt
    assert not both, (
        f"tool(s) both prompt-probed and prompt-exempt: {sorted(both)}. An "
        "exemption states that no prompt can be captured here; a probe that "
        "captures one refutes it."
    )
    unchecked = backed - probed - exempt
    assert not unchecked, (
        "LLM-backed GATED tool(s) with no prompt probe and no exemption: "
        f"{sorted(unchecked)}. Add a probe to _PROMPT_PROBES, or record in "
        "_PROMPT_PROBE_EXEMPT the specific reason this tool's prompt cannot be "
        "captured here. Layer (f)'s response probe cannot see the prompt "
        "channel: it reads what came back, and by then the prompt has already "
        "crossed to the provider."
    )
    stale = (probed | exempt) - backed
    assert not stale, (
        "_PROMPT_PROBES/_PROMPT_PROBE_EXEMPT name tool(s) that are not "
        f"LLM-backed GATED tools: {sorted(stale)}. Delete the entry, or find "
        "out why the derivation no longer sees that tool talking to a model."
    )
    assert (probed | exempt) == backed


# ---------------------------------------------------------------------------
# Layer (g), continued — the recorders, the fixture, and the three probes
#
# Two recorders, not one, and that is a fact about the production code rather
# than a preference: ``creek.reflect``, ``creek.compile`` and ``creek.draft``
# take a factory that hands back a bare ``(str) -> str`` callable, while
# ``creek.author``'s hands back an :class:`~creek.author.client.AuthorLLMClient`
# consumed as ``client.complete_with_usage(dynamic, system=static)``. One
# harness-owned recorder would have to fake the second protocol to serve both.
# ---------------------------------------------------------------------------

_CANNED_LLM_RESPONSE = '{"notes": []}'
"""The one canned completion every recorder returns.

Accepted by all three probed tools: ``creek.reflect`` parses it into zero notes
(``status="empty"``), ``creek.compile`` parses it into an empty synthesis, and
``creek.author`` voices it as the draft body. It carries no sentinel, so a
canary found in a probe's *response* can only have come from the corpus and
never from the recorder's own answer being echoed back.
"""

_RECORDING_MODEL_ID = "recording-stub"
"""The model id :class:`_RecordingAuthorFactory` reports as a provider.

Never sent anywhere — :meth:`_RecordingAuthorFactory.complete` answers locally —
but :class:`~creek.classify.llm.base.LLMProvider` declares ``model``, and
satisfying the protocol honestly is the point of wrapping the real client.
"""

_RECORDING_USAGE = {"input_tokens": 0, "output_tokens": 0}
"""Zero token counts, in the shape the desk's pydantic model validates.

:attr:`~creek.author.models.AuthoredDraft.usage` is a ``dict[str, int] | None``
field, so this must be a mapping. An object with the right attributes fails
validation, ``author_tool``'s boundary ``except Exception`` turns that into
``status="error"``, and the probe would then be asserting canary-freedom over
an error envelope produced *after* a real prompt had already been captured —
green, and about nothing.
"""


class _RecordingLLMFactory:
    """A ``(PrivacyTier) -> (str) -> str`` factory recording tiers and prompts.

    The shape ``creek.reflect``, ``creek.compile`` and ``creek.draft`` all
    accept. It doubles as the LLM callable it hands back, the idiom
    ``tests/test_mcp_tools.py::_TierRecordingFactory`` uses, so one object
    captures both the routing tier the tool derived and the bytes that actually
    reached the model.

    **This does not replace, and is not replaced by,**
    :func:`_forbidden_llm_factory`. That one is authoritative for *the model was
    never reached*: it raises, so a gate that let an above-ceiling read through
    fails loudly instead of quietly returning a clean-looking envelope. This one
    is authoritative for *what crossed to the model on a call that was supposed
    to reach it*. Layer (f) needs the first because its probes are refusals;
    layer (g) needs the second because its probes are admissions. A single
    helper could serve only one of those two jobs.

    Follow-up **#1275** tracks hoisting the five near-identical recorders in
    this suite — ``tests/test_mcp_reflect.py::_RecordingFactory``,
    ``tests/test_mcp_write_tools.py::_ProviderSpy``,
    ``tests/test_mcp_tools.py::_TierRecordingFactory``, and this class and its
    sibling below — into one shared *pair*, one per real factory protocol;
    that issue explicitly rejects collapsing them to a single recorder, for
    the reason the sibling's docstring gives. Keeping them local here is the
    deliberate minimal-diff call for #1036 and a known tension with house rule
    §1.2: five copies of a recorder are five things to keep in step, and the
    day one of them stops recording the static prefix is the day a layer goes
    quietly vacuous.

    Attributes:
        tiers: Every routing tier the tool asked for, in order. Not asserted on
            here — the tier-routing claims live in ``tests/test_mcp_tools.py``
            and ``tests/test_mcp_reflect.py`` — but recorded anyway, because it
            keeps this recorder's shape identical to the siblings #1275 will
            fold it into, and because a prompt-channel failure is much easier to
            read when the tier that produced it is in the same object.
        prompts: Every prompt the tool sent, in order.
    """

    def __init__(self, response: str = _CANNED_LLM_RESPONSE) -> None:
        """Start with empty recordings and the canned completion *response*."""
        self.tiers: list[PrivacyTier] = []
        self.prompts: list[str] = []
        self._response = response

    def __call__(self, tier: PrivacyTier) -> Callable[[str], str]:
        """Record *tier* and return the recording completion callable."""
        self.tiers.append(tier)
        return self._complete

    def _complete(self, prompt: str) -> str:
        """Record *prompt* and answer with the canned response."""
        self.prompts.append(prompt)
        return self._response


class _RecordingAuthorFactory:
    """A ``creek.author`` voice-client factory recording tiers and prompts.

    **Two probes depend on this class, and one of them is defined far above
    it.** Layer (f)'s :func:`_probe_author` resolves this name at call time,
    which is why it can sit beside the other response probes while the
    recorder stays here with :class:`_RecordingLLMFactory` and the layer-(g)
    constants. Moving this class *below* :data:`_RUNTIME_PROBES` is fine;
    deleting or renaming it without following the reference up is a
    ``NameError`` at probe time, not at import, so the failure arrives as a
    red parametrised case rather than as a collection error.

    ``creek.author``'s seam (#1254/#1260) is a different protocol from the one
    :class:`_RecordingLLMFactory` serves:
    :class:`~creek_mcp.tools.author.AuthorLLMFactory` answers with an
    :class:`~creek.author.client.AuthorLLMClient`, which
    :meth:`creek.author.voice.VoiceAgent.render` consumes as
    ``client.complete_with_usage(dynamic, system=static)`` and whose result must
    expose ``.text`` and ``.usage``. So this class does **not** fake the client:
    it doubles as an :class:`~creek.classify.llm.base.LLMProvider` and hands
    back a real ``AuthorLLMClient`` wrapped around itself, which puts the
    recording seam one level below the code under test rather than in place of
    it. A hand-rolled client would be free to drift from the real one — and the
    one thing this probe must not get wrong is *what the real client sends*.

    ``system`` is recorded joined to ``prompt`` rather than discarded, because
    the static prefix is a cached block and is exactly where the corpus evidence
    lands. A recorder that captured only the dynamic half would report a
    canary-free prompt for a call that had just shipped the whole corpus.

    The same two things :class:`_RecordingLLMFactory`'s docstring says apply
    here: :func:`_forbidden_llm_factory` remains authoritative for "the model
    was never reached" and these two for "what crossed to the model", so they
    coexist by design; and follow-up **#1275** tracks folding all five
    recorders in this suite into a shared pair — one per protocol, not one
    helper. There are two of them today only
    because two real client protocols exist — a single harness-owned recorder
    was expressible right up until ``creek.author`` grew its LLM seam, and
    writing one now would mean re-faking the protocol #1254 added precisely so
    it would not have to be faked.

    Attributes:
        tiers: Every content tier the desk routed by, in order. ``None`` is a
            legitimate value here (unlike the sibling above): the conductor's
            ``_content_tier`` yields it for evidence with no classified source.
        prompts: Every ``system + prompt`` pair the desk sent, in order.
    """

    is_cloud = False
    """Never egresses, and says so — the router's cloud gate reads this."""

    def __init__(self, response: str = _CANNED_LLM_RESPONSE) -> None:
        """Start with empty recordings and the canned completion *response*."""
        self.tiers: list[PrivacyTier | None] = []
        self.prompts: list[str] = []
        self._response = response

    @property
    def model(self) -> str:
        """The resolved model id, per the provider protocol."""
        return _RECORDING_MODEL_ID

    @property
    def available(self) -> bool:
        """Always ready: this provider's prerequisite is nothing at all."""
        return True

    def __call__(self, tier: PrivacyTier | None) -> AuthorLLMClient:
        """Record *tier* and return a real client speaking through this recorder."""
        self.tiers.append(tier)
        return AuthorLLMClient(self)

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Record ``system`` + *prompt* and answer with the canned completion.

        Args:
            prompt: The dynamic user prompt.
            system: The static, cache-eligible prefix — recorded, not dropped.
            max_tokens: Accepted to satisfy the provider protocol; a recorder
                that generates nothing has nothing to truncate.

        Returns:
            The canned :class:`~creek.classify.llm.completion.Completion`.
        """
        self.prompts.append(f"{system or ''}\n\n{prompt}")
        return Completion(text=self._response, usage=dict(_RECORDING_USAGE))


@pytest.fixture
def prompt_canary_vault(canary_vault: Path) -> Path:
    """``canary_vault`` plus a third, ``personal`` canary fragment.

    Extends the layer (f) fixture rather than editing it, and that is not
    politeness: ``test_wheel_probe_still_counts_the_fragment_it_is_admitted_to``
    asserts an exact tally of ``1``, and the mine, report and redact positive
    controls make exact-set claims over the same two fragments. A third fragment
    in ``canary_vault`` would break all of them, and "the fixture grew" is a
    much worse failure to debug than the leak they exist to catch.

    The ``personal`` fragment is what makes :func:`_assert_prompt_channel_clean`
    more than a restatement of layer (f). ``intimate`` is dropped by every path
    this repo has; ``personal`` is not. At ``ceiling=open``
    :func:`~creek.classify.privacy_filter.filter_fragments_by_tier` replaces a
    personal body with ``[Personal-tier summary: {title}]``, so the *title*
    survives into the prompt by design — the personal-summary residue, pinned as
    live behaviour by ``tests/test_mcp_tools.py``'s
    ``test_draft_prompt_carries_the_personal_title_at_an_open_ceiling``.
    The sentinel goes in the title, the body and the tags for the same reason
    ``canary_vault``'s do: so a tool that leaks only one of the three is caught
    by the same assertion.
    """
    _write_fragment(
        canary_vault,
        frag_id=_RUNTIME_PERSONAL_ID,
        title=f"Personal canary {_RUNTIME_PERSONAL_CANARY}",
        body=f"Personal canary body {_RUNTIME_PERSONAL_CANARY}",
        privacy_tier="personal",
        tags=[_RUNTIME_PERSONAL_CANARY],
    )
    return canary_vault


def _assert_prompt_channel_clean(tool: str, capture: _PromptCapture) -> None:
    """Assert the admitted canary crossed and neither gated canary did.

    The order is load-bearing, and both positive controls come first. A prompt
    probe has two ways to be green about nothing — no prompt at all, and a
    prompt with no corpus in it — and both are reachable by *breaking* the
    tool rather than by fixing it. A tool that stopped talking to a provider,
    or a tier filter broken in the drop-everything direction, would satisfy
    every exclusion below while proving strictly nothing. So non-emptiness is
    asserted first and reachability second, and only then do the exclusions
    run.

    Every captured prompt is checked, never ``prompts[0]``. A tool free to make
    more than one model call — the Writing Desk's voice/reflect loop does, per
    round — could put the corpus in the first prompt and the leak in the second,
    and an index-0 assertion would report clean.

    **Assertion four is the one that matters.** Excluding ``intimate`` is close
    to free: every path in this repo drops it. ``personal`` is the tier the
    known leak shape survives at — at ``ceiling=open`` the pipeline drops
    intimate outright, but
    :func:`~creek.classify.privacy_filter.filter_fragments_by_tier` *summarises*
    personal to ``[Personal-tier summary: {title}]``, so an above-ceiling
    personal **title** reaches the prompt by design on that path (pinned
    live by ``tests/test_mcp_tools.py``'s
    ``test_draft_prompt_carries_the_personal_title_at_an_open_ceiling``).
    A layer whose only security assertion is "intimate not in prompt" is
    therefore green forever over the single channel shape this repo already
    knows leaks. The tools probed here take the other route —
    ``tier_within_override``'s hard rank cutoff, which admits nothing above the
    ceiling in any form — and assertion four is what pins that they keep taking
    it.

    The response is swept for the same two sentinels at the end. It is nearly
    free, and it closes the response channel for *this* call rather than for a
    separate one, which is the property :class:`_PromptCapture` exists to
    provide.

    **The ``tool`` echo before that sweep is weaker than it looks, and #1279
    corrected the claim that used to be made for it here.** It catches an
    envelope nobody can identify — ``{}`` from a swallowed exception, or a
    different tool's response through a mis-wired probe. It does *not* catch
    ``creek.author`` crashing: :func:`creek_mcp.tools.author._error_response`
    sets ``"tool": TOOL_NAME`` on the failure path, so an ``status='error'``
    envelope satisfies the echo, and a crash after the first voice call
    satisfies both positive controls above as well. The assertion that
    separates them is on ``status``, and it lives one layer over, in
    :func:`test_author_probe_still_cites_the_corpus_it_is_admitted_to`.

    Args:
        tool: The probed tool's registered name, for the failure messages.
        capture: The probe's paired prompt/response evidence.
    """
    prompts = capture.prompts
    assert prompts, (
        f"{tool}'s prompt probe captured no prompt at all: the model was never "
        "reached, so this probe proves nothing. Every assertion below would "
        "hold just as well over a tool that had stopped building a prompt "
        "entirely. Repair the probe, or find out why the tool went quiet; do "
        "not relax the assertion."
    )
    captured = "\n\n--- next prompt ---\n\n".join(prompts)
    assert any(_RUNTIME_OPEN_CANARY in prompt for prompt in prompts), (
        f"{tool} built a prompt with no admitted corpus content in it — the "
        f"open canary {_RUNTIME_OPEN_CANARY!r} appears in none of "
        f"{len(prompts)} captured prompt(s) — so the exclusions below are "
        "vacuous. This is what a tier filter broken in the drop-everything "
        f"direction looks like: leak-free, and useless.\n\n{captured}"
    )
    for prompt in prompts:
        # Matched case-insensitively: the sentinels are uppercase, and a
        # renderer that slugified or lower-cased a title on its way into the
        # prompt would egress exactly the same string while evading a
        # case-sensitive check. Costs nothing; the sentinels collide with
        # nothing at any casing.
        folded = prompt.lower()
        assert _RUNTIME_INTIMATE_CANARY.lower() not in folded, (
            f"{tool} put intimate content in a prompt at "
            f"privacy_tier_ceiling=open: {_RUNTIME_INTIMATE_CANARY!r} crossed "
            "to the model. Layer (f) cannot see this — it reads the envelope, "
            "and by the time the envelope exists the prompt has already left "
            f"the process.\n\n{prompt}"
        )
        assert _RUNTIME_PERSONAL_CANARY.lower() not in folded, (
            f"{tool} put above-ceiling personal content in a prompt at "
            f"privacy_tier_ceiling=open: {_RUNTIME_PERSONAL_CANARY!r} crossed "
            "to the model. Before assuming a summary is harmless, note that "
            "the title IS the disclosure: filter_fragments_by_tier renders a "
            "personal fragment as [Personal-tier summary: {title}], which is "
            "the personal-summary residue shape. The tools probed here use "
            "tier_within_override's hard rank cutoff instead, so nothing above "
            f"the ceiling should be reachable in any form.\n\n{prompt}"
        )
    assert capture.response.get("tool") == tool, (
        f"{tool}'s probe came back with no tool echo, so the response half of "
        "this assertion is about an envelope nobody can identify — an empty "
        "dict from a swallowed exception, or a different tool's envelope "
        "through a mis-wired probe. Note the case it does NOT catch: "
        'creek_mcp.tools.author._error_response sets "tool": TOOL_NAME '
        "itself, so an author crash answering status='error' satisfies this "
        "echo. Only a status assertion separates the two, and layer (f)'s "
        "test_author_probe_still_cites_the_corpus_it_is_admitted_to makes it "
        f"(#1279).\n\n{capture.response}"
    )
    serialised = json.dumps(capture.response, default=str)
    folded_response = serialised.lower()
    assert _RUNTIME_INTIMATE_CANARY.lower() not in folded_response, (
        f"{tool} returned intimate content at privacy_tier_ceiling=open: "
        f"{_RUNTIME_INTIMATE_CANARY!r} is in its response.\n\n{serialised}"
    )
    assert _RUNTIME_PERSONAL_CANARY.lower() not in folded_response, (
        f"{tool} returned above-ceiling personal content at "
        f"privacy_tier_ceiling=open: {_RUNTIME_PERSONAL_CANARY!r} is in its "
        f"response.\n\n{serialised}"
    )


# The reflect probe's entry text. Deliberately sentinel-free and deliberately
# dull: it is the caller's own words, it lands verbatim in the prompt's ENTRY
# block, and a canary in it would make every assertion in
# ``_assert_prompt_channel_clean`` ambiguous about which side of the wire the
# sentinel came from.
_REFLECT_PROMPT_PROBE_ENTRY = (
    "Sat with the same question again this morning and let it stay open."
)

# The author probe's query. A common word rather than a sentinel, for the same
# reason ``_probe_compile``'s target metadata is sentinel-free: ``author_tool``
# echoes ``query`` straight back into the response envelope *and* into the
# prompt, so a canary here would be a leak assertion firing on the caller's own
# input. It matches the fixture titles, which is what gives retrieval something
# to rank on a three-fragment corpus.
_AUTHOR_PROMPT_PROBE_QUERY = "canary"


def _prompt_probe_reflect(vault: Path) -> _PromptCapture:
    """Reflect on raw ``content`` at the open ceiling and capture the prompt.

    Raw ``content=`` rather than an ``entry_ref``, and that choice *is* the
    blind spot #1036 is about. An above-ceiling ``entry_ref`` is refused by the
    #846 gate before the grounding walk ever runs, which is why layer (f)'s
    ``_probe_reflect`` never reaches a second corpus read — its evidence stops
    at the refusal. Raw content carries no classification, so the gate has
    nothing to refuse and the tool proceeds to
    ``creek_mcp.tools.reflect._default_retrieve``, which walks the corpus under
    the caller's ceiling and folds what it finds into ``SOURCE FRAGMENTS:``.
    That walk is the only path here that can put somebody else's fragment in a
    prompt, and it is reachable only through this shape.

    The exclusion this then asserts duplicates ``tests/test_mcp_reflect.py``'s
    ``test_above_ceiling_fragment_contributes_nothing_not_even_a_title``
    **on purpose**. The new thing is not the assertion, it is the *manifest
    requirement*: that test is one file's private good idea, and nothing makes
    the next LLM-backed tool grow an equivalent. Registering reflect in
    :data:`_PROMPT_PROBES` is what turns a good idea into a rule.

    ``care_guard=None`` for the reason ``_probe_reflect`` gives: the #753 seam
    is not what is under test, and an escalation would return before the model.

    Args:
        vault: The seeded three-tier canary vault.

    Returns:
        The tool's response paired with every prompt it sent.
    """
    factory = _RecordingLLMFactory()
    response = reflect_tool(
        vault_path=vault,
        llm_factory=factory,
        content=_REFLECT_PROMPT_PROBE_ENTRY,
        privacy_tier_ceiling=TierCeiling.OPEN,
        care_guard=None,
    )
    return _PromptCapture(response=response, prompts=tuple(factory.prompts))


def _prompt_probe_compile(vault: Path) -> _PromptCapture:
    """Compile the *open* fragment at the open ceiling and capture the prompt.

    Only the open id is passed, which looks like the probe declining to test
    anything and is the opposite. Hand ``compile_tool`` a personal or intimate
    id at ``ceiling=open`` and ``_survey_sources`` refuses with
    :data:`~creek_mcp.tools.compile._ABOVE_CEILING_REASON` *above* the factory,
    so no client is ever built and no prompt is ever captured — the probe would
    fail its own non-emptiness control and say nothing about the prompt channel.
    That refusal already has a test: layer (f)'s
    ``test_compile_probe_refuses_rather_than_merely_staying_quiet``. What is
    untested until here is the admitted call: whether an *accepted* compile can
    still sweep above-ceiling material into the prompt it builds around the
    fragment it was allowed to read.

    **The two exclusion assertions are weak for this tool, and that has to be
    said rather than left for a reader to assume otherwise.** Stated in the
    same spirit #1068 stated report's, because the dangerous failure of a
    canary layer is not a red probe — it is a green one taken for a broader
    guarantee than it makes. ``compile_tool`` loads only the ids its caller
    names, so the personal and intimate fragments in this fixture are never
    candidates and steps 3 and 4 of :func:`_assert_prompt_channel_clean`
    exclude sentinels that could not have been present. Both routes by which
    above-ceiling material *could* reach this prompt refuse above the factory,
    which puts them out of a prompt probe's reach by construction:

    * a **named** above-ceiling id — ``_survey_sources`` refuses with
      :data:`~creek_mcp.tools.compile._ABOVE_CEILING_REASON`;
    * an admitted child's **ancestry** — :func:`creek.compile.engine._build_prompt`
      emits a ``structural_path:`` breadcrumb of parent titles, and
      :func:`creek.hierarchy.structural_path_context` returns the *persisted*
      frontmatter field before it walks ``parent_id`` at all, so no ancestor
      need be loaded for one to be rendered. That was #931, now **closed by
      #1283**, which ranks the whole chain through
      ``creek.classify.privacy_filter.ancestry_tiers`` at the same gate —
      including the depth-0 case (a breadcrumb with no resolvable
      ``parent_id``), which fails closed under its rule (e).

    So what this probe actually carries is its **positive control**: an
    admitted fragment's title and body demonstrably do reach the prompt, which
    is what makes the channel observable and the harness trustworthy for this
    tool. Asserting that #1283's refusal holds is a *layer (f)* claim — there
    is no prompt to inspect once it fires — and it belongs beside the other
    refusal tests, not here. Tracked in #1287, which also notes that neither
    ``canary_vault`` nor :func:`prompt_canary_vault` is ancestry-shaped, so no
    layer in this file pins that refusal today.

    The target metadata is the caller's own sentinel-free strings, matching
    ``_probe_compile``'s for the same reason: an echo of them would prove
    nothing and only blur the assertion.

    Args:
        vault: The seeded three-tier canary vault.

    Returns:
        The tool's response paired with every prompt it sent.
    """
    factory = _RecordingLLMFactory()
    response = compile_tool(
        vault_path=vault,
        fragment_ids=[_RUNTIME_OPEN_ID],
        target_kind="thread",
        target_id="thread-runtime-probe",
        target_title="Runtime probe target",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    return _PromptCapture(response=response, prompts=tuple(factory.prompts))


def _prompt_probe_author(vault: Path) -> _PromptCapture:
    """Run the Writing Desk at the open ceiling and capture what it sends.

    ``dry_run=False`` is required, not incidental: a dry run returns
    ``plan_author``'s plan and evidence summary *before* the desk speaks, so a
    dry-run probe would capture no prompt and fail its own non-emptiness
    control. It needs :class:`_RecordingAuthorFactory` rather than the sibling
    recorder because the voice node consumes an
    :class:`~creek.author.client.AuthorLLMClient` through
    ``complete_with_usage``, not a bare ``(str) -> str``.

    This probe used to contradict a standing ``creek.author`` entry in
    :data:`_PROBE_EXEMPT`, which claimed the desk "never started" on an
    unconfigured fixture and returned a content-free error envelope. Running
    it refuted every clause, and **#1279 resolved the contradiction in layer
    (f)'s favour**: the exemption is gone and :func:`_probe_author` took its
    place. The tool is now probed on both channels, and they are not
    redundant. This one reads the bytes that already left for the provider;
    :func:`_probe_author` reads the envelope that came back, and carries the
    response-side positive control — ``status``, the admitted canary, and the
    tier the desk routed by — which no prompt probe can make and which an
    exemption, by construction, can never carry at all.

    Args:
        vault: The seeded three-tier canary vault.

    Returns:
        The tool's response paired with every ``system + prompt`` it sent.
    """
    factory = _RecordingAuthorFactory()
    response = author_tool(
        vault_path=vault,
        query=_AUTHOR_PROMPT_PROBE_QUERY,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
        dry_run=False,
    )
    return _PromptCapture(response=response, prompts=tuple(factory.prompts))


_PROMPT_PROBES: dict[str, Callable[[Path], _PromptCapture]] = {
    "creek.author": _prompt_probe_author,
    "creek.compile": _prompt_probe_compile,
    "creek.reflect": _prompt_probe_reflect,
}
"""LLM-backed ``GATED`` tool → a callable that invokes it and captures its prompts.

The layer-(g) counterpart of :data:`_RUNTIME_PROBES`, and read by the same kind
of forcing function. Each probe drives its tool all the way to the model with a
recording factory and returns both egress channels of that one call, so the
prompt and the envelope are always evidence about the same invocation.
"""

_PROMPT_PROBE_EXEMPT: dict[str, str] = {
    "creek.draft": (
        "draft_tool DOES build a prompt and DOES invoke llm_factory on a bare "
        "fixture, so its layer-(f) exemption's reason does not transfer here and "
        "this one must stand on its own. The real reason is upstream of the "
        "prompt: on any fixture this small the IdeaMiner falls back to "
        "MiningStrategy.UNEXPLORED_ONTOLOGY, whose seed "
        "(creek/generate/mining.py::_seed_from_ontology_tuple) is built from "
        "ontology enum labels and leaves source_fragments / threads / eddies "
        "empty. No corpus text of any tier reaches the prompt, so a canary "
        "assertion would be vacuous in BOTH directions — the exclusions could not "
        "fail and the positive control could not pass. draft's prompt channel is "
        "not unasserted, it is asserted elsewhere and differently: "
        "tests/test_mcp_tools.py::"
        "test_draft_prompt_carries_the_personal_title_at_an_open_ceiling drives "
        "the real DraftGenerator over a corpus-backed seed and pins that an "
        "above-ceiling personal TITLE does reach the prompt at ceiling=open by "
        "design — defended by routing (the tier is the more sensitive of the "
        "ceiling and the sources' own tiers) rather than by exclusion, which is "
        "the opposite contract from the three probes above and cannot share their "
        "assertion. test_the_draft_prompt_exemption_is_still_true executes this "
        "reason rather than trusting it."
    ),
}
"""LLM-backed ``GATED`` tools whose prompts cannot be usefully asserted here.

Kept separate from :data:`_PROBE_EXEMPT` deliberately. "The envelope is not
worth inspecting on a bare fixture" is a claim about the response, so a
layer-(f) exemption buys a tool nothing on this channel; the prompt is
assembled before the envelope exists, and the two can disagree.
``creek.draft`` is the standing illustration: it is exempt on **both**
channels, for two different reasons, each executed by its own guard —
:func:`test_the_draft_response_exemption_is_still_true` for the envelope and
:func:`test_the_draft_prompt_exemption_is_still_true` for the prompt. Sharing
one reason across the two would have been the shortcut, and it would have
carried draft's response-side claim onto a channel it says nothing about.
"""


@pytest.mark.parametrize("tool", sorted(_PROMPT_PROBES))
def test_llm_backed_gated_tools_leak_no_above_ceiling_content_into_the_prompt(
    tool: str,
    prompt_canary_vault: Path,
) -> None:
    """An LLM-backed ``GATED`` tool sends no above-ceiling content to the model.

    Layer (f)'s per-tool sweep moved to the other end of the wire. Its subject
    is the envelope the caller got back; this one's is the bytes that left for
    the provider, which is where an egress has already happened by the time any
    envelope exists. The two are not redundant and neither implies the other: a
    tool can return a clean response having shipped the corpus, and — the shape
    #1068 recorded for report — return a clean response having shipped nothing
    because it never read anything.

    All of the substance is in :func:`_assert_prompt_channel_clean`, shared so
    the three probes cannot drift into asserting three different things.
    """
    _assert_prompt_channel_clean(tool, _PROMPT_PROBES[tool](prompt_canary_vault))


@pytest.mark.parametrize("tool", sorted(_PROMPT_PROBE_EXEMPT))
def test_prompt_probe_exemptions_are_specific_to_their_tool(tool: str) -> None:
    """A prompt-channel exemption names its tool and says something.

    ``test_probe_exemptions_are_specific_to_their_tool``'s argument, applied to
    the second manifest — and it has to be applied again rather than inherited,
    because that test is parametrised over :data:`_PROBE_EXEMPT` and would never
    look at an entry here. The module leaf rules out a reason copy-pasted from a
    sibling (the failure mode that turns two exemptions into one unexamined
    one) and the length floor rules out ``"n/a"``. Neither can judge whether the
    reason is *true*; for the one entry that exists,
    :func:`test_the_draft_prompt_exemption_is_still_true` does that by running
    it.
    """
    reason = _PROMPT_PROBE_EXEMPT[tool]
    module = TOOL_POSTURES[tool].gate_module
    assert module is not None
    leaf = module.rsplit(".", maxsplit=1)[-1]
    assert leaf in reason, (
        f"{tool}'s prompt-channel exemption never mentions {leaf!r}, the "
        f"module it is excusing: {reason!r}. A reason that does not name the "
        "tool cannot be checked against it."
    )
    assert len(reason) >= 60, (
        f"{tool}'s prompt-channel exemption is too short to be a reason: "
        f"{reason!r}. State what specifically makes this tool's prompt "
        "uncapturable here."
    )


def test_the_draft_prompt_exemption_is_still_true(
    prompt_canary_vault: Path,
) -> None:
    """``creek.draft``'s prompt exemption is executed, not taken on trust.

    An exemption is the one way out of layer (g), so the only one on the books
    is checked by running the tool it excuses. What is asserted is precisely the
    reason's stated *cause*, not its conclusion: the strategy that fired, the
    emptiness of the seed's ``source_fragments``, and the single prompt that
    resulted carrying **no canary of any tier** — the open one included. That
    last absence is the load-bearing one. Asserting only that the gated canaries
    are missing would leave the exemption green on the day draft starts feeding
    the corpus to the model, which is the day it stops being true and starts
    needing a real probe.

    An AST check that the entry merely exists was the alternative, and it could
    not distinguish a reason that is true from one that used to be. This can.
    When it goes red, the fix is not to edit the reason: it is to delete the
    exemption and write ``_prompt_probe_draft``.
    """
    factory = _RecordingLLMFactory()
    response = draft_tool(
        vault_path=prompt_canary_vault,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert response["status"] == "ok", (
        "creek.draft did not draft on the canary fixture, so this guard is "
        f"asserting over a path the exemption does not describe.\n\n{response}"
    )
    assert response["idea_strategy"] == MiningStrategy.UNEXPLORED_ONTOLOGY.value, (
        "creek.draft drafted from a strategy other than unexplored-ontology: "
        f"{response['idea_strategy']!r}. The exemption rests on the "
        "ontology-tuple seed being the only one this fixture can produce. "
        "Delete the exemption and write a real prompt probe."
    )
    assert response["source_fragments"] == [], (
        "creek.draft's seed now cites corpus fragments: "
        f"{response['source_fragments']}. The exemption claims no corpus text "
        "reaches the prompt; a cited fragment is how that stops being true. "
        "Delete the exemption and write a real prompt probe."
    )
    assert len(factory.prompts) == 1, (
        f"creek.draft sent {len(factory.prompts)} prompts, not the one the "
        "exemption describes. Whatever the new call is, it is unasserted."
    )
    prompt = factory.prompts[0]
    for canary in (
        _RUNTIME_OPEN_CANARY,
        _RUNTIME_PERSONAL_CANARY,
        _RUNTIME_INTIMATE_CANARY,
    ):
        assert canary not in prompt, (
            f"creek.draft's prompt now carries {canary!r}. The exemption says "
            "no corpus text of any tier reaches it, and that is no longer "
            "true — which means the canary assertions a real probe would make "
            "are no longer vacuous. Delete the exemption from "
            f"_PROMPT_PROBE_EXEMPT and add a probe to _PROMPT_PROBES.\n\n{prompt}"
        )
