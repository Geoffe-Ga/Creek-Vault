"""``creek.report`` MCP tool — produce a vault-state report (FEAT-011).

Wraps the CLI's report dispatcher. Generating report types exposed via
MCP today: ``tags`` (the tag-garden generator), ``voice`` (the
per-register voice profiles), ``lexicon`` (the voice glossary + metaphor
index, #580), and ``decisions`` (draft Decision notes from
decision-signalling fragments, #581), ``rhetorical-patterns``
(per-register rhetorical-move notes, #582), and ``mode-profiles``
(per-mode engagement profiles, #583). ``unnamed`` and ``wavelength`` are
deferred to the CLI because they need date arithmetic the MCP shape
should not own. The wrapper writes one audit entry per invocation
including ``created_path`` for the resulting report file(s).

Tier ceiling (#968, closed). ``privacy_tier_ceiling`` is converted here by
:func:`creek_mcp.tier_ceiling.to_privacy_override` and threaded into **all
six** generators, each of which admits a note only when
:func:`creek.classify.privacy_filter.within_ceiling` says its *raw*
frontmatter clears :func:`~creek.classify.privacy_filter.tier_within_override`'s
hard rank cutoff. A missing ``privacy_tier`` key fails closed to
``intimate``: the model would default it to ``unclassified``, which ranks
with ``personal`` and would be *admitted* at ``ceiling=personal``.

What #968 closed was **write-side, not read-side**, and that framing is why
the gap survived two earlier sweeps. This tool returns only
``report_paths`` — never a tag, a title, or a body — so its response envelope
was canary-free at ``ceiling=open`` for the whole life of the bug and no
response-level test could ever have caught it. The evidence lives only in the
bytes of the artifacts a call writes; the reproduction was an ``intimate``
fragment's tag appearing verbatim in ``00-Creek-Meta/Tag-Garden.md`` *and* in
the append-only ``00-Creek-Meta/Processing-Log/tag-history.json``. That is
also why ``tag-history.json`` entries now record the ``tier_ceiling`` they
were taken under: counts from two different ceilings survey two different
corpora and are not comparable.

One consequence must be printed on the tin: ``TagGardenGenerator`` scans five
directories, and the four beyond ``01-Fragments`` (``02-Threads``,
``03-Eddies``, ``04-Praxis``, ``08-Decisions``) hold note types with no
``privacy_tier`` field at all, so the fail-closed read ranks them
``intimate``. **A ceiling-filtered tag garden is fragment-derived only.**
Those notes are derived from fragments and untierable by construction — a
Decision note's ``title:`` is a source fragment's title verbatim — so reading
them at ``ceiling=open`` would hand back what a ``ceiling=all`` run distilled
there.

Neither canonical read-gate primitive fits, and both were considered:
``refuse_above_ceiling`` *refuses*, which would make the reports unreachable
rather than tier-correct, and ``iter_admitted_fragments`` summarises
``personal`` bodies rather than dropping them (a summary stub written into
``### Sample Passages`` is a leak with extra steps) while reading the tier
through the validated model, which fails open on a missing key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.generate.decisions import generate_decisions
from creek.generate.lexicon import generate_lexicon
from creek.generate.tags import TagGardenGenerator
from creek.generate.voice import VoiceProfileGenerator
from creek.generate.wavelength import ModeProfileGenerator
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, to_privacy_override

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.report"
_VALID_TYPES = (
    "tags",
    "voice",
    "decisions",
    "lexicon",
    "rhetorical-patterns",
    "mode-profiles",
)


def report_tool(
    *,
    vault_path: Path,
    report_type: str = "tags",
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Generate the requested report and return its written path(s).

    Reports iterate vault content internally rather than operating on a
    caller-supplied fragment list, so ``affected_fragment_ids`` is the
    empty list and ``created_path`` carries the rendered file location.
    The ceiling is converted once, below the ``report_type`` refusal, and
    threaded into every branch — the refusal comes first so an unsupported
    type is answered without touching the vault at all.
    """
    if report_type not in _VALID_TYPES:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unsupported report_type {report_type!r}; "
                f"available via MCP: {', '.join(_VALID_TYPES)}"
            ),
        )
    override = to_privacy_override(privacy_tier_ceiling)
    if report_type == "tags":
        written_paths = [
            TagGardenGenerator(
                vault_path=vault_path,
                override=override,
            ).generate_garden(),
        ]
    elif report_type == "lexicon":
        _lexicon, written_paths = generate_lexicon(vault_path, override=override)
    elif report_type == "decisions":
        written_paths = generate_decisions(vault_path, override=override)
    elif report_type == "rhetorical-patterns":
        written_paths = list(
            VoiceProfileGenerator(override=override).generate_rhetorical_patterns(
                vault_path,
            ),
        )
    elif report_type == "mode-profiles":
        written_paths = list(
            ModeProfileGenerator().generate_mode_profiles(
                vault_path,
                override=override,
            ),
        )
    else:
        written_paths = list(
            VoiceProfileGenerator(override=override).generate_all_profiles(vault_path),
        )
    relative_paths = [str(p.relative_to(vault_path)) for p in written_paths]
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"report_type": report_type},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=relative_paths[0] if relative_paths else None,
        created_tier=None,
        affected_fragment_ids=[],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "report_type": report_type,
        "report_paths": relative_paths,
    }
