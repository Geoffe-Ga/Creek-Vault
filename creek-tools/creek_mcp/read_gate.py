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

**Why these primitives have no production caller yet.** That is a decision,
not an oversight. ``creek.reflect`` and ``creek.compile`` are deliberately
*not* retrofitted onto :func:`refuse_above_ceiling`: their refusal reasons and
ordering comments are tool-specific and load-bearing. Reflect's "ACCEPTED
RESIDUAL RISK" block documents exactly why its reason must stay distinct from
``entry_ref not found`` — including the timing-equalisation caveat that would
have to be handled if the two were ever unified — and folding it into a shared
reason would delete that reasoning along with the behaviour. The primitives
exist so the four filed gaps (#968/#969/#970/#971) can be closed by *adoption*
rather than by each tool re-deriving the policy, and so that a *new* tool
finds an already-gated corpus walk on the path of least resistance.

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
            "An entry_ref resolves a fragment the caller did not supply; "
            "_above_ceiling refuses it before the care seam, the grounding "
            "retrieval, or the model (#846)."
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
        posture=ReadPosture.UNGATED_KNOWN_GAP,
        rationale=(
            "The ceiling is audited and echoed but never converted or "
            "threaded: creek/generate/{tags,lexicon,decisions,wavelength}.py "
            "contain no tier filtering at all, so report_type='tags' at "
            "ceiling=open writes an intimate fragment's tags into "
            "00-Creek-Meta/Tag-Garden.md. Reproduced (#968)."
        ),
        gap_issue=968,
    ),
    "creek.state.read": ToolPosture(
        posture=ReadPosture.UNGATED_KNOWN_GAP,
        rationale=(
            "Returns 00-Creek-Meta/State/latest.md verbatim at any ceiling. "
            "The artifact records no ceiling of its own, so it may have been "
            "rendered at ceiling=all and be read back at ceiling=open (#969)."
        ),
        gap_issue=969,
    ),
    "creek.state.render": ToolPosture(
        posture=ReadPosture.UNGATED_KNOWN_GAP,
        rationale=(
            "Walks the whole vault through StateReportGenerator, which "
            "threads no override, and returns the rendered content. Its one "
            "fragment-derived section is safe by default "
            "(phase_filtered_seeds with override=None, which ranks as "
            "ceiling=open); the eddy/thread/liminal title sections are "
            "unfiltered and unfilterable — Eddy and Thread carry no "
            "privacy_tier (#969)."
        ),
        gap_issue=969,
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
            "len(findings). Titles live in findings, which is never returned."
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
        posture=ReadPosture.CALLER_NAMED_PATHS,
        rationale=(
            "The caller names the path, which resolve_within_vault confines "
            "to the vault; findings carry counts, line numbers and salted "
            "hashes, never the matched text."
        ),
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
