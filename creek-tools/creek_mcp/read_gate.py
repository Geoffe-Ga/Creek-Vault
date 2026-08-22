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
:func:`refuse_above_ceiling` has three production callers.
``creek_mcp.tools.state_read`` adopted it in #969, and that adoption is the
shape the primitives were written for — the tool addresses a *single* cached
artifact, so there is nothing to partially admit, and its refusal has no
tool-specific story to tell. It gets the generic reason, the four-key payload
and the no-tier-echo rule for free rather than re-deriving any of them.
``creek_mcp.tools.journal`` adopted it in #970 on the same property one step
further out: it is a *write* gate asking a read question. Its idempotent
update-in-place would overwrite the one fragment an ``external_id`` resolves
to, and the rule is that you may only overwrite what you could have read — so
admission is decided by ``tier_allowed`` through this primitive, not by
``write_tier_allowed``, which ranks the incoming entry and knows nothing about
the fragment on disk. Caller-addressed and singular again: you cannot
overwrite half a body, so there is nothing to partially admit.
``creek_mcp.tools.upload`` adopted it in #1023 on that identical argument,
with one edge journal does not have: its staged bytes are a document rather
than markdown, so they carry no ``privacy_tier`` frontmatter and no
escalate-only ratchet, which is what forces the gate above the staging write
rather than merely above the fragment write.

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
``draft``, ``author`` and — since #971 closed the voice-skill-tree gap on
exactly this argument — ``skills.refresh``, all ``GATED`` on
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

#972 closed ``creek.redact.scan`` one step further out again: by narrowing the
tool's *domain* rather than filtering its output. There was nothing to filter.
The scan is a regex pass over bytes that opens no front matter, so it holds no
tier for any file it names, and the leak was the filename itself — Creek
fragment filenames are slugified titles. Its gate therefore decides *where*:
the FEAT-027 staging subtree at every ceiling, because that is the one call
CrawDad makes and it makes it at whatever ceiling the channel is configured
for — ``personal`` unless an operator mapped that channel, ``open`` where one
did — so the subtree has to be admitted at the lowest of them; everything else
in the vault ranked as ``intimate``, because for all this tool knows it is.
``refuse_above_ceiling`` was deliberately not adopted, for a sharper reason
than ``report``'s: :data:`GENERIC_ABOVE_CEILING_REASON` asserts that *resolved
content* exceeds the ceiling, which would be false here — nothing was resolved
and nothing was ranked — and useless to a CrawDad operator, whose only
actionable fact is where they pointed the scan. The gap also had a second half
no gate could have closed, and it belongs to the same lesson: the response
rendered a symlinked child under its target's name, so the scope fix is paired
with a single as-scanned path renderer every field goes through.

:data:`TOOL_POSTURES` is verified by ``tests/test_mcp_read_gate.py``, which
fails if an entry lies: the manifest must match ``server.list_tools()``
exactly, a ``GATED`` claim is checked against a real call site in the named
module, a gap must name a positive issue that the tool's own source mentions,
and a tool recorded as a gap must not already call a primitive below.

**The three egress channels, and which one each layer watches.** #1036 added
a seventh layer, (g): every ``GATED`` tool that can hand corpus text to a
model is driven all the way *to* the provider with a recording factory, and no
sentinel from an above-ceiling fragment may appear in any prompt it sent. It
exists because every layer before it terminates at the response envelope, and
a prompt has already crossed to the provider by the time an envelope exists.
The follow-up on #1036 asked for the probes to be read per *egress channel*
rather than per tool. Named on that axis, the surface is:

1. **JSON response** — layer (f), driven off ``_RUNTIME_PROBES`` /
   ``_PROBE_EXEMPT``. Forced: a newly ``GATED`` tool must grow a probe or
   record a justified exemption.
2. **Model prompt** — layer (g), driven off ``_PROMPT_PROBES`` /
   ``_PROMPT_PROBE_EXEMPT``, over a set derived from the tools' own
   signatures. Forced the same way, plus a non-emptiness assertion on the
   derivation, since a derived set can silently empty itself.
3. **Disk artifacts** — covered per tool, and **not forced**.
   ``test_report_probe_leaves_no_canary_in_the_artifact_it_writes``,
   ``test_state_render_probe_leaves_no_canary_in_the_artifact_it_writes``,
   ``test_journal_probe_refuses_and_leaves_the_fragment_bytes_untouched`` and
   ``test_upload_probe_refuses_and_leaves_the_staged_document_untouched`` are
   four good ideas, and nothing obliges the fifth artifact-writing tool to
   grow a fifth. That gap is deliberately **out of scope here** and split to
   **#1273** rather than left implied: #968 and #969 were both found on this
   channel, so it is simultaneously the channel with the worst track record
   and the only one with no forcing function.

**Scope item 4 of #1036 — a second gate pair on :class:`ToolPosture` — is
decided NO.** ``creek.reflect``'s rationale names two gates while the
dataclass records one, so the obvious repair is a second
``(gate_module, gate_symbol)`` pair. It was not taken. A second pair is only
ever checkable at the strength layers (c)/(e) can offer — the symbol exists in
the named module and is called there — and for reflect's grounding gate that
check is green *by construction*: ``creek/author/agents.py`` imports
``tier_within_override`` at line 28 and calls it in ``_load_corpus`` at line
115 for every Writing Desk consumer. It would stay green if
``creek_mcp.tools.reflect._default_retrieve`` stopped passing ``override``,
stopped being called, or reflect dropped grounding altogether — three ways to
lose the gate entirely without disturbing the evidence for it. Layer (g)
checks that gate's *effect* instead, which is strictly stronger, and the
injection drill at the top of ``tests/test_mcp_read_gate.py`` is the evidence:
the two independent mutations that neutralise the grounding cutoff turn (g)
red and leave every structural layer and all ten response probes green.
Revisit predicate: **a tool whose second gate's effect no runtime probe can
observe.** For that tool the structural check is the only one available, and a
weak check is better than none.

**``creek.classify`` is a known prompt-channel egress outside both probe
manifests.** Its posture is ``METADATA_ONLY`` on a rationale about its
*response* — "Returns counts only … the ceiling is audited for the trail, not
enforced" — and ``creek_mcp/tools/classify.py`` says the same in its own
words: the ceiling "is recorded for the audit trail; it does not gate
execution". Both are true of the envelope and beside the point on this
channel. With ``method="llm"`` the whole corpus, every tier of it, goes
through a provider, defended only by
:class:`creek.classify.llm.router.ModelRouter`'s Intimate-never-cloud gate —
which ``creek/classify/classify_engine.py`` reaches by resolving a second,
intimate-only classifier config, and which says nothing about ``personal``.
Layer (g) structurally cannot enrol it: the derivation looks for a
function taking both ``llm_factory`` and ``privacy_tier_ceiling``, and
``classify_tool`` takes no factory — so the reason this tool is absent from
the prompt manifest is a fact about its signature, not a finding about its
safety. Tracked in **#1274**.
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
            ``UNGATED_KNOWN_GAP`` entry, and set on nothing else. It was also
            carried by ``creek.journal`` until #970 gated its update-in-place
            path; that entry is now ``GATED`` and names its gate instead.
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
            "Three unsupplied reads, under three gates. (1) An entry_ref "
            "resolves a fragment the caller did not supply; _above_ceiling "
            "refuses it before the care seam, the grounding retrieval, or the "
            "model (#846). (2) The grounding corpus walk, live since #964, is "
            "gated by to_privacy_override -> tier_within_override's hard rank "
            "cutoff inside RetrievalSpecialist: only within-ceiling fragments "
            "are admitted, and they contribute their titles only. (3) The "
            "compiled-layer lookup behind related_praxis / related_eddies "
            "(#873) walks 01-Fragments unfiltered on purpose -- it must see "
            "the above-ceiling fragments in order to notice that a page was "
            "compiled from one -- and gates on the way out instead, in "
            "creek_mcp.compiled_pages._provenance_admitted: an eddy or praxis "
            "page reaches the response only when every fragment it was "
            "compiled from ranks within the ceiling, and a page whose "
            "provenance cannot be enumerated in full is withheld as opaque. "
            "No fragment body, title or id from that walk is returned; only "
            "the compiled page's own published fields are."
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
            "before the LLM or the compiled-page write (#848). Their "
            "ancestors are ranked too (#931): the prompt renders an admitted "
            "fragment's persisted structural_path, which carries ancestor "
            "headings the caller may never have named, so ancestry_tiers "
            "walks parent_id and an above-ceiling ancestor refuses the whole "
            "call with the same content-free reason."
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
        posture=ReadPosture.GATED,
        rationale=(
            "Converts the ceiling with to_privacy_override and threads it "
            "into SkillTreeGenerator, whose corpus walk admits a fragment "
            "only when tier_within_override clears the hard rank cutoff — so "
            "no above-ceiling body, title or id reaches the untiered "
            "<vault>/creek-skills tree. Thread and eddy skills are gated too, "
            "on a DERIVED tier (#1284): Thread and Eddy carry no privacy_tier "
            "field, but their titles, descriptions and member lists are "
            "computed from their member fragments, so admit_by_derived_tier "
            "ranks each one at the maximum tier of every fragment whose "
            "threads/eddies wikilinks name its title — equivalently, it is "
            "written only when every fragment naming it clears the ceiling. "
            "The reduction runs over the UNFILTERED corpus, or an eddy with "
            "one open and one intimate member would resolve to open; that is "
            "leak (3) of the three #969 closed for creek.state.render, and "
            "this is the same reduction, shared rather than re-derived. "
            "Empty evidence reduces to INTIMATE, so an orphaned thread or "
            "eddy is unreachable below ceiling=intimate. skill_count no "
            "longer moves with above-ceiling thread or eddy cardinality, "
            "which matters here and not for the four fragment-derived "
            "categories because a thread's slugified title IS its filename "
            "and so rides out in skill_paths. An admitted eddy's rendered "
            "'Member threads' line is intersected with the admitted threads "
            "for the same reason. Excludes "
            "rather than refuses: the tool names no target, it is a corpus "
            "walk like report/wheel/mine, and refusing would make the tree "
            "unreachable — #968's explicit anti-goal. The cutoff is hard "
            "rather than summarising for #968's reason one step further in: "
            "a skill file IS a voice-exemplar corpus, so "
            "filter_fragments_by_tier's '[Personal-tier summary: <title>]' "
            "stub would be written into ## Exemplar Passages beside the "
            "fragment id in bold — leaking the title it claims to protect "
            "and teaching the model a sentence nobody wrote. The intimate "
            "exclusion remains a SEPARATE consent gate ANDed with this one "
            "(_is_snapshot_fragment's allow_intimate), and the MCP surface "
            "never opens it, so intimate exemplars are unreachable here at "
            "every ceiling, ceiling=all included. The #971 gap was "
            "write-side, which is why a response-level sweep missed it: for "
            "the four fragment-derived categories the response carries only "
            "a count and fixed skill names (F1, rising, express-do…), so the "
            "evidence was always the tree's bytes — the threads/ and eddies/ "
            "entries #1284 closed were the one read-side exception. "
            "Consequence to know: "
            "unclassified ranks with personal (#876), so a vault that has "
            "never been through creek classify yields a complete tree whose "
            "## Exemplar Passages sections all carry the 'no qualifying "
            "exemplars' placeholder at ceiling=open — the gate working, not "
            "an empty vault; a broader ceiling recovers them."
        ),
        gate_module="creek_mcp.tools.skills",
        gate_symbol="to_privacy_override",
    ),
    "creek.journal": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "The entry body is the caller's own and write_tier_allowed gates "
            "the tier it creates, but the idempotent update-in-place destroys "
            "the fragment an external_id already maps to — so it refuses on "
            "THAT fragment's current vault tier: you may only overwrite what "
            "you could have read (#970). A write gate asking a read question, "
            "hence refuse_above_ceiling (tier_allowed) rather than "
            "write_tier_allowed; the target is caller-addressed and singular, "
            "and you cannot overwrite half a body. Ordering is load-bearing: "
            "the gate sits ABOVE _stage_entry, because the staged copy under "
            "00-Creek-Meta/adepthood/journal/ has no escalate-only ratchet, so "
            "a gate placed below staging would return a correct refusal over "
            "an already-destroyed staged entry whose privacy_tier had been "
            "rewritten downward. Two fail-closed rules, not one: no ledger "
            "record at all passes content_tier=None and CREATES (creation must "
            "keep working at every ceiling), while a ledger record whose "
            "fragment does not resolve reduces max_source_tier([]) to intimate "
            "and is refused — so any divergence from the writer's own id index "
            "fails safe. Consequence to know: PurgeEngine leaves a dangling "
            "ledger record (#1080), so a purged id is refused below "
            "ceiling=intimate until it is re-sent by an admitted caller — a "
            "LOCAL stdio caller only, since a remote consumer token is capped "
            "at ceiling=personal and can never send ceiling=intimate/all "
            "(#1082 tracks a possible content-hash carve-out for the "
            "unchanged-resend case). Accepted residual: the refusal is an "
            "existence-AND-rank oracle, matching reflect's own honesty "
            "standard for its analogous oracle. No stronger than the "
            "pre-existing action: created|updated bit on the existence "
            "question those two share; the rank bit — 'above your ceiling' — "
            "is new, and is the price of refusing at all. It is deliberately "
            "blurred, not a clean tier read: every fail-closed unresolvable "
            "case (purged, orphaned, schema-invalid, deleted out of band) "
            "collapses into the identical refusal as a genuine above-ceiling "
            "fragment, so refused means 'above your ceiling OR unresolvable'."
        ),
        gate_module="creek_mcp.tools.journal",
        gate_symbol="refuse_above_ceiling",
    ),
    "creek.upload": ToolPosture(
        posture=ReadPosture.GATED,
        rationale=(
            "creek.journal's gate applied to document bytes instead of text "
            "(#1023). The uploaded bytes are the caller's own and "
            "write_tier_allowed gates the tier they create, but the idempotent "
            "update-in-place destroys the fragment an external_id already maps "
            "to — so it refuses on THAT fragment's current vault tier: you may "
            "only overwrite what you could have read (#970). A write gate "
            "asking a read question, hence refuse_above_ceiling (tier_allowed) "
            "rather than write_tier_allowed; the target is caller-addressed "
            "and singular, and you cannot overwrite half a document. Ordering "
            "is load-bearing twice over. The overwrite gate sits ABOVE "
            "_stage_upload, because a staged .docx or .pdf carries no "
            "frontmatter and therefore no escalate-only ratchet at all — a "
            "gate placed below staging would return a correct refusal over an "
            "already-overwritten intimate document, with nothing downstream "
            "able to restore it. And write_tier_allowed sits above "
            "base64.b64decode, so a refused intimate upload never turns a byte "
            "of above-ceiling content into memory. Two fail-closed rules, not "
            "one: no ledger record at all passes content_tier=None and CREATES "
            "(creation must keep working at every ceiling), while a record "
            "whose fragment no longer resolves reduces max_source_tier([]) to "
            "intimate and is refused, so any divergence from the writer's own "
            "id index fails safe. Consequences to know: the refusal is the "
            "same existence-AND-rank oracle creek.journal already accepts on "
            "its own targets, blurred the same way — purged, orphaned, "
            "schema-invalid and deleted-out-of-band all collapse into the "
            "identical refusal as a genuine above-ceiling document. And the "
            "ledger this gate resolves through is upload.jsonl, not "
            "markdown.jsonl, so a journal external_id and an upload "
            "external_id can never collide however alike they look."
        ),
        gate_module="creek_mcp.tools.upload",
        gate_symbol="refuse_above_ceiling",
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
        posture=ReadPosture.GATED,
        rationale=(
            "Closed by narrowing the tool's domain, not by filtering its "
            "output: the scan is a regex pass over bytes that reads no "
            "per-file privacy_tier at all, so it has nothing to rank a "
            "fragment with. _refuse_outside_scan_scope admits the FEAT-027 "
            "staging subtree 00-Creek-Meta/Inbound/ at every ceiling — "
            "CrawDad's safety pass runs there at the channel's configured "
            "ceiling, personal by default and open only where an operator "
            "mapped that channel (crawdad/crawdad/bot.py::_channel_tier), so "
            "it has to keep working at the lowest of them — and ranks every "
            "other in-vault target as if it held intimate content, so only "
            "ceiling=intimate or all admits it. Consequence to know: that is "
            "why a personal-ceiling caller cannot scan 09-Reference/ either — "
            "the tool cannot tell it from 01-Fragments. Ordering is "
            "load-bearing: the gate sits ABOVE the existence check, because "
            "'input_path not found' versus a successful scan is a one-bit "
            "existence oracle over every slugified fragment title in the "
            "vault. Below ceiling=intimate all three out-of-scope answers are "
            "now the same fixed string, derived from the caller's own "
            "input_path and their own declared ceiling and echoing no tier, "
            "no counts and no path: the file that is there, the name that is "
            "nothing, and the symlink that resolves off the vault — which "
            "resolve_within_vault necessarily catches ABOVE the gate, having "
            "no resolved path to hand it, and which therefore collapses onto "
            "the same reason in _outside_vault_reason rather than being "
            "reordered under it. So for a caller the ceiling does not admit "
            "vault-wide, the refusal is an oracle for neither existence nor "
            "rank. The precise 'resolves outside the vault root' message "
            "survives at ceiling=intimate and all, where it discloses nothing "
            "the caller could not already read and a dangling link — a stale "
            "sync folder, a moved drive — is the operator's one actionable "
            "diagnostic. "
            "refuse_above_ceiling was deliberately not adopted because its "
            "generic reason asserts that resolved content was ranked, which "
            "nothing here ever was. Deliberate divergence from rule 1 above: "
            "the audit entry DOES record the probed target. It records the "
            "caller's own input_path string and not the resolved path — which "
            "for a staged symlink would be an intimate fragment's path — "
            "because 'where did consumer X aim the scanner' is the actionable "
            "fact about a security tool, and the string was already the "
            "caller's. The second, independent half: every path in the "
            "response — findings, the report_markdown headings CrawDad posts "
            "verbatim into a channel, and the input_path echo — is "
            "vault-relative through the single helper _vault_relative, and "
            "rendered as scanned rather than as resolved, because a renderer "
            "that resolved first let a symlink staged under Inbound/ disclose "
            "an intimate fragment's slugified title from inside the admitted "
            "subtree (#972). The echo is the one exception — it renders the "
            "already-resolved target — and it is safe only because the gate "
            "refuses an out-of-scope symlink before there is anything to "
            "echo. #1087 closed the scan_batch residual: the module's one "
            "filesystem walk now resolves the scan root once and, for a "
            "child that is itself a symlink, requires its resolved target "
            "to land under that root before it is opened — the same "
            "predicate the shipped SEC-003 write guard uses — so an "
            "escaping symlinked child is declined rather than read, and "
            "neither its PII types nor its line numbers nor its existence "
            "reach this tool's caller. What remains to know: existence "
            "probing *within* Inbound/ still works, which is the tool's "
            "job rather than a leak; and the decline is counted on "
            "ScanSummary.files_skipped_symlink and rendered into "
            "report_markdown, but is not yet a typed key on the "
            "statistics object returned here (#1292)."
        ),
        gate_module="creek_mcp.tools.redact",
        gate_symbol="_refuse_outside_scan_scope",
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
