"""Read-surface posture manifest + the two canonical ceiling gates (#932).

:data:`TOOL_POSTURES` is the audit record this issue asked for: one entry per
registered MCP tool, stating how that tool relates to the caller's
``privacy_tier_ceiling``. A tool either enforces the ceiling through a named
gate (``GATED``), returns nothing the caller did not supply
(``NO_UNSUPPLIED_READ``), reads only paths the caller named
(``CALLER_NAMED_PATHS``), returns count-shaped metadata rather than content
(``METADATA_ONLY``), is gated by an elevated auth token instead
(``AUTH_TOKEN``), or does not honour the ceiling at all and says so against a
tracking issue (``UNGATED_KNOWN_GAP``). The record is worth keeping because a
tool nobody triaged is otherwise indistinguishable, to a reader of the
surface, from a tool somebody decided needs no gate.

**Gate ordering.** The discipline is worked out at length in
:mod:`creek_mcp.tools.reflect`, around its ceiling gate and the comment blocks
either side of it. It is restated — not copied — here, because every adopter
of the primitives below inherits it:

1. Audit-log the attempt first and unconditionally, recording only ``has_*``
   booleans. Never the probed target id and never the outcome, so the trail
   cannot answer "did consumer X read fragment F?" in either direction; a
   probing consumer shows up as a rate, never as a named target.
2. Resolve the target and answer *not found* before anything else touches it.
3. Run the ceiling gate **above** every derived-signal seam — care guards,
   grounding retrieval, the model. Any signal downstream of the gate is a
   one-bit oracle about content the caller is not admitted to: an
   ``escalate`` response tells an unadmitted caller that the fragment carries
   acute-distress markers just as surely as returning the body would.
4. Keep the refusal reason generic (:data:`GENERIC_ABOVE_CEILING_REASON`) and
   never echo the resolved tier. That tier was derived from content the
   caller cannot read, so echoing it — in the reason or in an extra key added
   "for debugging" — turns every refusal into a tier-classification oracle
   over the corpus.
5. Only *after* admission derive :func:`creek_mcp.tier_ceiling.routing_tier`
   to key a model call.

**Who calls these primitives, and why the rest still do not.**
:func:`refuse_above_ceiling` has exactly one production caller:
``creek_mcp.tools.state_read``, which adopted it in #969. That adoption is the
shape the primitives were written for — the tool addresses a *single* cached
artifact, so there is nothing to partially admit, and its refusal has no
tool-specific story to tell. It gets the generic reason, the four-key payload
and the no-tier-echo rule for free rather than re-deriving any of them.

The other refusing tools are deliberately *not* retrofitted.
``creek.reflect`` and ``creek.compile`` keep their own refusal reasons and
ordering comments because those are tool-specific and load-bearing: reflect's
"ACCEPTED RESIDUAL RISK" block documents exactly why its reason must stay
distinct from ``entry_ref not found`` — including the timing-equalisation
caveat that would have to be handled if the two were ever unified — and folding
it into a shared reason would delete that reasoning along with the behaviour.

:func:`iter_admitted_fragments` still has no production caller, and that too is
a decision. Every gap closed so far needed the **hard rank cutoff**
(``tier_within_override``) rather than this primitive's summarising filter: a
report or a state artifact must *omit* above-ceiling content, because a
``"[Personal-tier summary: <title>]"`` stub written into a voice profile or a
mined prompt leaks the title it claims to be protecting. The primitive remains
the right answer for a future tool that hands bodies to a model, and it is what
stops such a tool re-deriving Ontology §13.2 for itself.

**Adoption is not the only honest way to close a gap, and #968 is the worked
example.** ``creek.report`` is now ``GATED`` without adopting either
primitive, deliberately. ``refuse_above_ceiling`` *refuses*, and the issue's
explicit anti-goal forbids refusing ``creek.report``: that breaks every
legitimate caller and makes the reports unreachable rather than tier-correct,
so the inputs are filtered instead. ``iter_admitted_fragments`` summarises
``PERSONAL`` bodies rather than dropping them — a title-only stub written into
a voice profile's ``### Sample Passages`` leaks the title and poisons the
corpus — and it reads the tier through the validated
:class:`~creek.models.Fragment`, so it fails open on a *missing*
``privacy_tier`` key. ``creek.report`` therefore joins ``wheel``, ``mine``,
``draft`` and ``author``, all ``GATED`` on
:func:`creek_mcp.tier_ceiling.to_privacy_override` without adopting either
primitive. What the manifest checks is that the named gate exists and decides
something, not which of two shapes it takes.

#969 closed the two ``creek.state.*`` gaps and needed *both* shapes at once,
which is the clearest statement of the rule. ``creek.state.render`` names no
target — it is a corpus walk — so it excludes, on ``to_privacy_override``, like
``report``. ``creek.state.read`` addresses one atomic cached artifact, so it
refuses, on ``refuse_above_ceiling``. Read that as #1068's compile/reflect
reasoning one level up: read's target is not caller-*named*, but it is
caller-*addressed* and singular, which is the property that decides.

:data:`TOOL_POSTURES` is verified by ``tests/test_mcp_read_gate.py``, which
fails if an entry lies: the manifest must match ``server.list_tools()``
exactly, a ``GATED`` claim is checked against a real call site in the named
module, a gap must name a positive issue that the tool's own source mentions,
and a tool recorded as a gap must not already call a primitive below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from creek.classify.privacy_filter import filter_fragments_by_tier
from creek.vault.reader import iter_vault_fragments
from creek_mcp.tier_ceiling import refusal_response, tier_allowed, to_privacy_override

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from creek.models import Fragment, PrivacyTier
    from creek_mcp.tier_ceiling import TierCeiling

_FRAGMENTS_SUBDIR = "01-Fragments"

GENERIC_ABOVE_CEILING_REASON = "resolved content exceeds the declared tier ceiling"
"""The one refusal reason for an above-ceiling read.

Deliberately names no tier and no fragment. It has to be identical across
every above-ceiling tier at a given ceiling, or the refusal itself ranks the
content the caller was not admitted to.
"""


class ReadPosture(StrEnum):
    """How a tool relates to the caller's ``privacy_tier_ceiling``.

    Attributes:
        NO_UNSUPPLIED_READ: Returns nothing the caller did not supply, so
            there is no unadmitted content for a ceiling to gate.
        CALLER_NAMED_PATHS: Reads only vault paths the caller named, and
            returns no matched content from them.
        GATED: Enforces the ceiling through the ``gate_module`` /
            ``gate_symbol`` pair recorded alongside it.
        METADATA_ONLY: Returns counts, check names and other count-shaped
            summaries rather than fragment bodies or titles.
        AUTH_TOKEN: Takes no ceiling at all; gated fail-closed by an elevated
            token instead, because it destroys content rather than reading it.
        UNGATED_KNOWN_GAP: Reads vault content without honouring the ceiling.
            A tracked, reproduced gap — ``gap_issue`` names the issue.
    """

    NO_UNSUPPLIED_READ = "no-unsupplied-read"
    CALLER_NAMED_PATHS = "caller-named-paths"
    GATED = "gated"
    METADATA_ONLY = "metadata-only"
    AUTH_TOKEN = "auth-token"
    UNGATED_KNOWN_GAP = "ungated-known-gap"


@dataclass(frozen=True)
class ToolPosture:
    """One tool's recorded read posture.

    Attributes:
        posture: The tool's :class:`ReadPosture`.
        rationale: The specific, checkable fact that justifies *posture* —
            this is the audit record, and it is meant to be read against the
            tool's source rather than taken on trust.
        gate_module: Dotted path of the module holding the gate. Set on
            ``GATED`` entries only.
        gate_symbol: Name of the gate callable within *gate_module*. Set on
            ``GATED`` entries only.
        gap_issue: Issue tracking an unenforced ceiling — required on every
            ``UNGATED_KNOWN_GAP`` entry, and also carried by ``creek.journal``
            whose gap is on its update-in-place path rather than a read.
    """

    posture: ReadPosture
    rationale: str
    gate_module: str | None = None
    gate_symbol: str | None = None
    gap_issue: int | None = None


CANONICAL_GATE_PRIMITIVES: frozenset[str] = frozenset(
    {"refuse_above_ceiling", "iter_admitted_fragments"}
)
"""The two ways a tool may satisfy the ceiling without inventing a third.

Consumed as *strings* by the manifest's AST checks, so these names and the
callables defined below have to stay in step; the test resolves each one back
to an attribute of this module.
"""


_COUNTS_ONLY_RATIONALE = (
    "Returns counts only and produces no new tiered content; the ceiling is "
    "audited for the trail, not enforced."
)

_PURGE_RATIONALE = (
    "Destroys vault content rather than returning it, so it takes no "
    "privacy_tier_ceiling at all and is gated fail-closed by "
    "CREEK_MCP_ELEVATED_TOKEN."
)


TOOL_POSTURES: dict[str, ToolPosture] = {
    "creek.handshake": ToolPosture(
        posture=ReadPosture.NO_UNSUPPLIED_READ,
        rationale=(
            "Returns tool names, the contract and ontology versions and the "
            "tier model itself — no vault content of any kind."
        ),
    ),
    "creek.reflect": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Two unsupplied reads, under two gates. (1) An entry_ref resolves "
            "a fragment the caller did not supply; _above_ceiling refuses it "
            "before the care seam, the grounding retrieval, or the model "
            "(#846). (2) The grounding corpus walk, live since #964, is gated "
            "by to_privacy_override -> tier_within_override's hard rank cutoff "
            "inside RetrievalSpecialist: only within-ceiling fragments are "
            "admitted, and they contribute their titles only."
        ),
        gate_module="creek_mcp.tools.reflect",
        gate_symbol="_above_ceiling",
    ),
    "creek.compile": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Source fragment_ids name fragments the caller did not supply; "
            "_survey_sources ranks every one of them with write_tier_allowed "
            "and compile_tool refuses the whole call on its above_ceiling flag, "
            "before the LLM or the compiled-page write (#848)."
        ),
        gate_module="creek_mcp.tools.compile",
        gate_symbol="_survey_sources",
    ),
    "creek.wheel": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Converts the ceiling to a PrivacyTierOverride and threads it "
            "into the corpus walk via tier_within_override, so above-ceiling "
            "fragments never enter the frequency tally."
        ),
        gate_module="creek_mcp.tools.wheel",
        gate_symbol="to_privacy_override",
    ),
    "creek.mine": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Converts the ceiling to a PrivacyTierOverride and threads it "
            "into IdeaMiner's corpus walk, so above-ceiling fragments are "
            "excluded at the source."
        ),
        gate_module="creek_mcp.tools.mine",
        gate_symbol="to_privacy_override",
    ),
    "creek.draft": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Converts the ceiling to a PrivacyTierOverride and threads it "
            "into DraftGenerator's corpus walk, so above-ceiling fragments "
            "are excluded at the source."
        ),
        gate_module="creek_mcp.tools.draft",
        gate_symbol="to_privacy_override",
    ),
    "creek.author": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Converts the ceiling to a PrivacyTierOverride and threads it "
            "into the Writing Desk specialists (#660), so above-ceiling "
            "fragments never reach the evidence."
        ),
        gate_module="creek_mcp.tools.author",
        gate_symbol="to_privacy_override",
    ),
    "creek.report": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Converts the ceiling with to_privacy_override and threads it "
            "into all six generators (tags, voice, lexicon, decisions, "
            "rhetorical-patterns, mode-profiles), each of which admits a note "
            "only when within_ceiling clears tier_within_override's hard rank "
            "cutoff against the file's *raw* frontmatter — so a missing "
            "privacy_tier fails closed to intimate rather than to the model's "
            "unclassified default (#968). The gap was write-side: the "
            "response carries only report_paths, so the evidence was always "
            "the artifact bytes. Consequence to know: TagGardenGenerator's "
            "four non-fragment scan directories hold note types with no "
            "privacy_tier field, so a ceiling-filtered tag garden is "
            "fragment-derived only."
        ),
        gate_module="creek_mcp.tools.report",
        gate_symbol="to_privacy_override",
    ),
    "creek.state.read": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Refuses, rather than excludes, on the artifact's own privacy_tier "
            "stamp: its target is one atomic cached artifact, so there is "
            "nothing to partially admit — you cannot exclude half a rendered "
            "markdown document without re-rendering it, and re-rendering is "
            "what creek.state.render is. refuse_above_ceiling supplies the "
            "generic reason and the four-key payload, so the refusal never "
            "names the stamped tier. Consequence to know: a pre-#969 "
            "latest.md carries no stamp, fails closed to intimate and is "
            "refused below ceiling=intimate — accurate rather than cautious, "
            "since those bytes were rendered completely unfiltered. Recovery "
            "is one re-render, and ceiling=all admits every stamp, so no "
            "report is permanently unreachable. A *missing* report stays "
            "status=empty: refusing there would be a vault-emptiness oracle "
            "in the other direction."
        ),
        gate_module="creek_mcp.tools.state_read",
        gate_symbol="refuse_above_ceiling",
    ),
    "creek.state.render": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "Converts the ceiling with to_privacy_override and threads it "
            "into StateReportGenerator, which admits ten of its eleven "
            "sections against it and stamps the written artifact with the "
            "highest tier it admitted. The exception is Pre-LLM yield, "
            "ungated on purpose: it renders the last line of "
            "run-summary.jsonl — a run id, a timestamp and four integers "
            "describing one pipeline run — which names no fragment, so there "
            "is nothing in it to filter by. Excludes rather than refuses: the tool "
            "names no target, so refusing would make the audit report "
            "unreachable — #968's explicit anti-goal. The gap was write-side, "
            "which is why it survived a response-level sweep: the evidence is "
            "the bytes under 00-Creek-Meta/State, where three leaks were "
            "reproduced (an orphan path carrying an intimate fragment's "
            "slugified title, a 10-Liminal/Unnamed file stem, and an eddy "
            "title derived from an above-ceiling member). Consequences to "
            "know: type: paradox notes carry no privacy_tier field at all and "
            "fail closed out of the Liminal Watch below ceiling=intimate; the "
            "lint summary is an untierable verbatim Processing-Log copy and is "
            "admitted whole or not at all, i.e. intimate-only; suggested "
            "questions are dropped below ceiling=personal because the miner's "
            "filter summarises rather than drops a personal title; and "
            "latest.md is a single slot, so a narrow render replaces a broader "
            "one for every later reader."
        ),
        gate_module="creek_mcp.tools.state",
        gate_symbol="to_privacy_override",
    ),
    "creek.skills.refresh": ToolPosture(
        posture=ReadPosture.UNGATED_KNOWN_GAP,
        rationale=(
            "SkillTreeGenerator hardcodes an intimate exclusion but the "
            "ceiling is never threaded, so personal bodies pass unsummarised "
            "at ceiling=open — looser than every sibling generation tool "
            "(#971)."
        ),
        gap_issue=971,
    ),
    "creek.journal": ToolPosture(
        posture=ReadPosture.NO_UNSUPPLIED_READ,
        rationale=(
            "The entry body is the caller's own, and write_tier_allowed gates "
            "the tier it creates; but the idempotent update-in-place "
            "overwrites the fragment an external_id already maps to without "
            "consulting that fragment's tier (#970)."
        ),
        gap_issue=970,
    ),
    "creek.lint": ToolPosture(
        posture=ReadPosture.METADATA_ONLY,
        rationale=(
            "The checks read fragment frontmatter (paradox, unnamed and "
            "orphan-compiled scan 01-Fragments), never bodies; the MCP "
            "response returns only check names, count-shaped summaries and "
            "len(findings). Titles live in findings, which is never returned. "
            "The posture is about the *response* only: the lint run also "
            "writes a markdown report under 00-Creek-Meta/Processing-Log/ "
            "that does embed above-ceiling titles and tag names, and the "
            "tags check deliberately surveys the whole vault "
            "(creek/lint/checks/tags.py passes PrivacyTierOverride.ALL) so it "
            "cannot report 'no orphan tags' about a vault that has them. That "
            "artifact IS served back through MCP — creek state appends it "
            "verbatim as its ## Lint summary section — and #969 closed the "
            "hole at that boundary rather than here: the section is now "
            "rendered only at ceiling=intimate or broader, and its presence "
            "escalates the state artifact's own tier stamp to intimate. The "
            "copy is untierable row by row, so it is admitted whole or not at "
            "all. What is left unresolved here is narrower than the caveat "
            "this replaces: the file on disk is still written at ALL, so any "
            "*future* surface that serves Processing-Log/ must make the same "
            "whole-or-nothing decision creek state did."
        ),
    ),
    "creek.link": ToolPosture(
        posture=ReadPosture.METADATA_ONLY,
        rationale=_COUNTS_ONLY_RATIONALE,
    ),
    "creek.classify": ToolPosture(
        posture=ReadPosture.METADATA_ONLY,
        rationale=_COUNTS_ONLY_RATIONALE,
    ),
    "creek.redact.scan": ToolPosture(
        posture=ReadPosture.UNGATED_KNOWN_GAP,
        rationale=(
            "The caller names the path and resolve_within_vault confines it to "
            "the vault, and no matched text is returned — but the confinement "
            "is to the whole vault, not to the FEAT-027 staging subtree, and "
            "the tier is never consulted. An open-ceiling caller pointed at "
            "01-Fragments gets back the filenames of intimate fragments, which "
            "are slugified titles, plus which PII types each contains. This "
            "posture was CALLER_NAMED_PATHS until the #932 review; the name "
            "was true and the conclusion drawn from it was not (#972)."
        ),
        gap_issue=972,
    ),
    "creek.save": ToolPosture(
        posture=ReadPosture.NO_UNSUPPLIED_READ,
        rationale=(
            "The content is the caller's own; write_tier_allowed gates the "
            "tier the call would create."
        ),
    ),
    "creek.ingest": ToolPosture(
        posture=ReadPosture.NO_UNSUPPLIED_READ,
        rationale=(
            "The content is the caller's own — a source path they supplied — "
            "and write_tier_allowed gates the tier the call would create."
        ),
    ),
    "creek.purge.fragment": ToolPosture(
        posture=ReadPosture.AUTH_TOKEN,
        rationale=_PURGE_RATIONALE,
    ),
    "creek.purge.source": ToolPosture(
        posture=ReadPosture.AUTH_TOKEN,
        rationale=_PURGE_RATIONALE,
    ),
    "creek.purge.classifications": ToolPosture(
        posture=ReadPosture.AUTH_TOKEN,
        rationale=_PURGE_RATIONALE,
    ),
    "creek.purge.daterange": ToolPosture(
        posture=ReadPosture.AUTH_TOKEN,
        rationale=_PURGE_RATIONALE,
    ),
    "creek.purge.vault": ToolPosture(
        posture=ReadPosture.AUTH_TOKEN,
        rationale=(
            f"{_PURGE_RATIONALE} It additionally requires a matching "
            "confirm_vault_path."
        ),
    ),
}
"""Every registered MCP tool's read posture — the #932 audit record."""


def refuse_above_ceiling(
    *,
    tool: str,
    content_tier: PrivacyTier | None,
    ceiling: TierCeiling,
) -> dict[str, object] | None:
    """Return the canonical refusal for above-ceiling content, or ``None``.

    Admission is delegated wholesale to
    :func:`creek_mcp.tier_ceiling.tier_allowed`, never re-derived: a second,
    private ranking inside the read gate is how two halves of the MCP surface
    end up disagreeing about whether a fragment is readable. Delegation also
    inherits the fail-closed handling of an unrecognised tier, which ranks
    with the most sensitive tier rather than the least.

    Args:
        tool: The registered tool name, echoed in the refusal.
        content_tier: The resolved tier of content the caller did *not*
            supply, or ``None`` when the text came from the caller inline and
            so carries no classification to compare. ``None`` is never above
            the ceiling — refusing it would mean refusing callers their own
            words.
        ceiling: The caller's declared ceiling.

    Returns:
        ``None`` when the content is admitted, so a call site reads as an
        early return on a non-``None`` result. Otherwise the four-key
        :func:`creek_mcp.tier_ceiling.refusal_response` payload carrying
        :data:`GENERIC_ABOVE_CEILING_REASON` — and nothing else. Anything
        further would be derived from content the caller is not admitted to.
    """
    if content_tier is None or tier_allowed(content_tier, ceiling):
        return None
    return refusal_response(
        tool=tool,
        ceiling=ceiling,
        reason=GENERIC_ABOVE_CEILING_REASON,
    )


def iter_admitted_fragments(
    vault_path: Path,
    ceiling: TierCeiling,
) -> Iterator[tuple[Path, Fragment, str]]:
    """Yield the vault's fragments as admitted under *ceiling*.

    The tier policy is not implemented here. The ceiling is converted by
    :func:`creek_mcp.tier_ceiling.to_privacy_override` and handed to
    :func:`creek.classify.privacy_filter.filter_fragments_by_tier`, which owns
    the one implementation of the Ontology §13.2 promise: intimate content is
    dropped outright, personal content contributes a title-only summary. The
    body yielded is always the *filtered* body, so an adopting tool cannot
    reach the raw text by accident.

    Each pair is filtered on its own rather than as one sequence. The filter
    takes ``(fragment, body)`` and drops the path, and it also drops whole
    fragments, so re-pairing its shorter output with the walk by position
    would attribute one fragment's body to another fragment's path. It is a
    stateless per-pair generator, so one-at-a-time application is identical in
    behaviour and keeps the path attached by construction.

    Args:
        vault_path: Vault root; fragments are read from ``01-Fragments``.
        ceiling: The caller's declared ceiling.

    Yields:
        ``(path, fragment, body)`` in the shared reader's sorted order. A
        vault with no ``01-Fragments`` directory yields nothing: the
        freshly-initialised case is empty, not an error, and every adopting
        tool inherits that.
    """
    override = to_privacy_override(ceiling)
    for path, fragment, body, _raw in iter_vault_fragments(
        vault_path / _FRAGMENTS_SUBDIR,
    ):
        for admitted, admitted_body in filter_fragments_by_tier(
            [(fragment, body)],
            override=override,
        ):
            yield path, admitted, admitted_body
