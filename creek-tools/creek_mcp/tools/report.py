"""``creek.report`` MCP tool — produce a vault-state report (FEAT-011).

Wraps the CLI's report dispatcher. The accepted types come from
:data:`creek.surface_modes.REPORT_TYPES`, the same declaration ``creek report``
reads, so a type added to one surface reaches the other without anybody
remembering to copy it across. Until #1253 this module carried its own retyped
tuple listing six of the eleven types; ``unnamed``, ``fingerprint``,
``paradox``, ``synchronicity`` and ``wavelength`` were not refused, they were
*absent* — an MCP caller was told they were not report types at all.

Being reachable is not the same as being served unconditionally. Two gates sit
above the generators, and both answer **by name**:

- **Tier-blind types** (:data:`_TIER_BLIND_GENERATORS`). Four generators take no
  :class:`~creek.classify.privacy_filter.PrivacyTierOverride` at all, so there
  is no way to honour ``privacy_tier_ceiling`` while running them — serving
  ``paradox`` at ``ceiling=open`` would distil ``intimate`` fragments into
  ``10-Liminal/Paradoxes/``, which is precisely the leak #968 closed for the
  other six. They are therefore served **only at ``ceiling=all``**, the ceiling
  ``creek report`` itself runs them under, and refused below it with the reason
  stated. Widening them is a change to those four generators, not to this file.
- **``wavelength``** needs a period, exactly as ``creek report --type
  wavelength --period`` does, so the tool takes one; an unparseable or missing
  period is refused rather than guessed at.

Tier ceiling (#968, closed). ``privacy_tier_ceiling`` is converted here by
:func:`creek_mcp.tier_ceiling.to_privacy_override` and threaded into every
ceiling-aware generator, each of which admits a note only when
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

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final

from creek.config import CreekConfig, load_config, resolve_config_path
from creek.generate.ai_style.fingerprint import build_fingerprint, save_fingerprint
from creek.generate.decisions import generate_decisions
from creek.generate.lexicon import generate_lexicon
from creek.generate.paradox import generate_paradoxes
from creek.generate.synchronicity import generate_synchronicities
from creek.generate.tags import TagGardenGenerator
from creek.generate.unnamed import UnnamedDigestGenerator
from creek.generate.voice import VoiceProfileGenerator
from creek.generate.wavelength import (
    PERIOD_HELP,
    ModeProfileGenerator,
    generate_phase_map,
    resolve_period,
)
from creek.link.embeddings import EmbeddingLinker
from creek.surface_modes import REPORT_TYPES
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, to_privacy_override

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.classify.privacy_filter import PrivacyTierOverride

TOOL_NAME = "creek.report"


@dataclass(frozen=True)
class _ReportRequest:
    """Everything a report generator may need, in one shape.

    A uniform argument keeps :data:`_MCP_REPORTS` a plain ``{name: callable}``
    dict even though ``wavelength`` needs a period and the other ten do not.
    The alternative — a second dispatch path for the one type with an extra
    parameter — is the shape that let ``wavelength`` fall out of the surface
    inventory in the first place (#1027, #1253).

    Attributes:
        vault_path: Vault root.
        override: The caller's ceiling, already converted.
        period: The raw ``period`` argument; meaningful only to ``wavelength``.
    """

    vault_path: Path
    override: PrivacyTierOverride
    period: str | None


def _vault_config(request: _ReportRequest) -> CreekConfig:
    """Load the config belonging to *request*'s vault, quietly.

    The precedent is ``creek_mcp/tools/draft.py``, whose comment names the
    hazard: this is deliberately **not** the bare process-wide
    ``load_config()`` the other tool wrappers use. That form resolves
    ``creek_config.yaml`` against the server's *current directory* and never
    reads ``<vault>/00-Creek-Meta/creek_config.yaml``, so a vault-scoped
    setting passed through it is silently ignored — the exact defect #1313
    fixes on the voice path.

    Resolution is lazy, per generator, rather than eager in ``report_tool``.
    That construction serves all eleven report types, eight of which read no
    config at all today; resolving there would turn a stale-but-harmless vault
    config into an unhandled ``ValidationError`` (``CreekConfig`` forbids
    extras) and a dangling ``CREEK_CONFIG`` into a ``FileNotFoundError`` on
    ``report_type="tags"``.

    ``warn_on_missing=False`` keeps the MCP surface quiet: a config-less vault
    is an ordinary case here, and the tool's contract is a structured
    response, not log noise.

    Args:
        request: The resolved report request.

    Returns:
        The vault's fully-validated :class:`~creek.config.CreekConfig`.
    """
    return load_config(
        resolve_config_path(request.vault_path, None),
        warn_on_missing=False,
    )


def _generate_tags(request: _ReportRequest) -> list[Path]:
    """Render the Tag Garden.

    Args:
        request: The resolved report request.

    Returns:
        The written garden path.
    """
    return [
        TagGardenGenerator(
            vault_path=request.vault_path,
            override=request.override,
        ).generate_garden(),
    ]


def _generate_voice(request: _ReportRequest) -> list[Path]:
    """Render the per-register voice profiles.

    Args:
        request: The resolved report request.

    Returns:
        One path per rendered profile.
    """
    return list(
        VoiceProfileGenerator(
            override=request.override,
            audience_weighting=_vault_config(request).voice_audience_weighting,
        ).generate_all_profiles(
            request.vault_path,
        ),
    )


def _generate_lexicon(request: _ReportRequest) -> list[Path]:
    """Render the voice glossary and metaphor index (#580).

    Args:
        request: The resolved report request.

    Returns:
        One path per rendered lexicon page.
    """
    _lexicon, written_paths = generate_lexicon(
        request.vault_path,
        override=request.override,
    )
    return list(written_paths)


def _generate_decisions(request: _ReportRequest) -> list[Path]:
    """Draft Decision notes from decision-signalling fragments (#581).

    Args:
        request: The resolved report request.

    Returns:
        One path per written Decision note.
    """
    return list(generate_decisions(request.vault_path, override=request.override))


def _generate_rhetorical_patterns(request: _ReportRequest) -> list[Path]:
    """Render the per-register rhetorical-move notes (#582).

    Args:
        request: The resolved report request.

    Returns:
        One path per rendered note.
    """
    return list(
        VoiceProfileGenerator(
            override=request.override,
            audience_weighting=_vault_config(request).voice_audience_weighting,
        ).generate_rhetorical_patterns(
            request.vault_path,
        ),
    )


def _generate_mode_profiles(request: _ReportRequest) -> list[Path]:
    """Render the per-mode engagement profiles (#583).

    Args:
        request: The resolved report request.

    Returns:
        One path per rendered profile.
    """
    return list(
        ModeProfileGenerator().generate_mode_profiles(
            request.vault_path,
            override=request.override,
        ),
    )


def _generate_wavelength(request: _ReportRequest) -> list[Path]:
    """Write the descriptive wavelength phase-map for the requested period.

    Shares :func:`creek.generate.wavelength.generate_phase_map` with ``creek
    report --type wavelength`` so the two surfaces cannot disagree about the
    corpus, the emptiness check, or the weekly/monthly choice.

    Args:
        request: The resolved report request.

    Returns:
        The written phase-map path, or an empty list when no admitted fragment
        carries a classified wavelength phase — a genuine "nothing to report",
        not a failure. Also empty when the period does not parse, which
        :func:`report_tool` refuses before reaching here; the branch is
        defensive so a future caller cannot get a silent misdated map.
    """
    resolved = resolve_period(request.period)
    if resolved is None:
        return []
    mode, anchor = resolved
    written = generate_phase_map(
        request.vault_path,
        mode=mode,
        anchor=anchor,
        override=request.override,
    )
    return [] if written is None else [written]


def _generate_unnamed(request: _ReportRequest) -> list[Path]:
    """Write this week's Unnamed digest.

    Args:
        request: The resolved report request.

    Returns:
        The written digest path.
    """
    # Still cwd-scoped rather than vault-scoped; see _vault_config. Reading a
    # different config section (embeddings), so out of #1313's scope — #1409.
    config = load_config()
    generator = UnnamedDigestGenerator(
        embedding_linker=EmbeddingLinker(config=config.embeddings),
    )
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    return [generator.generate_weekly_digest(request.vault_path, week_start)]


def _generate_fingerprint(request: _ReportRequest) -> list[Path]:
    """Build and persist the voice fingerprint (FEAT-040.2).

    Args:
        request: The resolved report request.

    Returns:
        The written fingerprint path, or an empty list when the vault holds no
        self-authored fragment to build one from.
    """
    config = _vault_config(request)
    fingerprint = build_fingerprint(
        request.vault_path,
        config.ai_style,
        audience_weighting=config.voice_audience_weighting,
    )
    if fingerprint.fragment_count == 0:
        return []
    return [save_fingerprint(fingerprint, request.vault_path, config.ai_style)]


def _generate_paradox(request: _ReportRequest) -> list[Path]:
    """Write Paradox notes for contradictory fragment pairs (#711).

    Args:
        request: The resolved report request.

    Returns:
        One path per written note; empty when no contradictory pair is found.
    """
    # cwd-scoped, not vault-scoped: a different config section, tracked in #1409.
    return list(generate_paradoxes(request.vault_path, load_config().embeddings))


def _generate_synchronicity(request: _ReportRequest) -> list[Path]:
    """Write Synchronicity notes for surprising cross-source resonances (#711).

    Args:
        request: The resolved report request.

    Returns:
        One path per written note; empty when no pair qualifies.
    """
    # cwd-scoped, not vault-scoped: a different config section, tracked in #1409.
    return list(generate_synchronicities(request.vault_path, load_config().embeddings))


_MCP_REPORTS: Final[dict[str, Callable[[_ReportRequest], list[Path]]]] = {
    "tags": _generate_tags,
    "unnamed": _generate_unnamed,
    "voice": _generate_voice,
    "fingerprint": _generate_fingerprint,
    "decisions": _generate_decisions,
    "lexicon": _generate_lexicon,
    "rhetorical-patterns": _generate_rhetorical_patterns,
    "mode-profiles": _generate_mode_profiles,
    "paradox": _generate_paradox,
    "synchronicity": _generate_synchronicity,
    "wavelength": _generate_wavelength,
}
"""One entry per :data:`creek.surface_modes.REPORT_TYPES` name.

``tests/test_mcp_write_tools.py`` asserts the two sets are equal, which is what
turns a CLI report type added without an MCP branch into a failing build rather
than a type an agent is told does not exist.
"""

_TIER_BLIND_GENERATORS: Final[dict[str, str]] = {
    "unnamed": "creek.generate.unnamed.UnnamedDigestGenerator",
    "fingerprint": "creek.generate.ai_style.fingerprint.build_fingerprint",
    "paradox": "creek.generate.paradox.generate_paradoxes",
    "synchronicity": "creek.generate.synchronicity.generate_synchronicities",
}
"""Report types whose generator accepts no ``PrivacyTierOverride``.

The value is the symbol a reader should go and widen. Serving these below
``ceiling=all`` would write above-ceiling content into vault artifacts with no
way to filter it, so they are refused there — by name, and saying why.
"""


def _tier_blind_refusal(report_type: str, ceiling: TierCeiling) -> str | None:
    """Return why *report_type* cannot be served at *ceiling*, if it cannot.

    Args:
        report_type: A name from :data:`creek.surface_modes.REPORT_TYPES`.
        ceiling: The caller's declared ceiling.

    Returns:
        A refusal reason naming the un-filterable generator, or ``None`` when
        the type is filterable or the caller already asked for no filtering.
    """
    generator = _TIER_BLIND_GENERATORS.get(report_type)
    if generator is None or ceiling is TierCeiling.ALL:
        return None
    return (
        f"report_type {report_type!r} has no tier-filtered generator "
        f"({generator} accepts no PrivacyTierOverride), so it is served only at "
        f"privacy_tier_ceiling='all' — the ceiling `creek report` itself runs it "
        f"under. Requested {ceiling.value!r}."
    )


def report_tool(
    *,
    vault_path: Path,
    report_type: str = "tags",
    period: str | None = None,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Generate the requested report and return its written path(s).

    Reports iterate vault content internally rather than operating on a
    caller-supplied fragment list, so ``affected_fragment_ids`` is the
    empty list and ``created_path`` carries the rendered file location.
    The three refusals — unknown type, tier-blind type, unparseable period —
    all come before the ceiling is converted and before the vault is touched,
    so a refused call writes nothing at all, not even an audit entry.

    Args:
        vault_path: Vault root.
        report_type: One of :data:`creek.surface_modes.REPORT_TYPES`.
        period: Required by ``wavelength`` and ignored by every other type —
            ``"weekly"``, ``"monthly"``, an ISO week (``YYYY-Www``) or a month
            (``YYYY-MM``).
        privacy_tier_ceiling: The caller's ceiling (#968).
        consumer: Audit-trail attribution.

    Returns:
        The ``"ok"`` envelope with ``report_paths``, or a structured refusal.
    """
    if report_type not in REPORT_TYPES:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unsupported report_type {report_type!r}; "
                f"valid types: {', '.join(REPORT_TYPES)}"
            ),
        )
    blind = _tier_blind_refusal(report_type, privacy_tier_ceiling)
    if blind is not None:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=blind,
        )
    if report_type == "wavelength" and resolve_period(period) is None:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"report_type 'wavelength' needs a period — {PERIOD_HELP}; "
                f"got {period!r}"
            ),
        )
    written_paths = _MCP_REPORTS[report_type](
        _ReportRequest(
            vault_path=vault_path,
            override=to_privacy_override(privacy_tier_ceiling),
            period=period,
        ),
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
