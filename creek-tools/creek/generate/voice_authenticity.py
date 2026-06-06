"""Read-only ``creek voice-authenticity`` diagnostic.

This is the *tracer skeleton* for the voice-authenticity feature: it makes
three otherwise-hidden defects observable without changing any behaviour.
Each sub-score is backed by a minimal-but-real probe over the data that
exists today; later issues in the epic deepen each probe in place without
re-wiring this surface.

The three sub-scores:

``audience_mix``
    Distribution of the *voice-eligible* corpus (self-authored, non-INTIMATE
    fragments — see :attr:`creek.models.Fragment.voice_proxy_eligible`) by
    ``privacy_tier``. ``weighting_active`` is a stub ``False`` until the
    graduated audience-authority model lands.

``ai_corpus_leak``
    Count and fraction of voice-eligible fragments whose
    ``source.platform`` is an AI-chat platform (``claude`` / ``chatgpt``).
    These leak into the voice corpus until AI-chat turns are split and the
    AI half is quarantined.

``deslop``
    Given an optional drafted essay, whether the AI-mannerism guard left a
    ``voice_distance`` attestation in the draft frontmatter, how many
    residual findings it recorded, and a ``status`` reason. The status string
    is a stub until the faithful de-slop pass lands.

Read-only: nothing here mutates the vault.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import frontmatter

from creek.generate.ai_style.guard import VOICE_DISTANCE_KEY, VOICE_FINDINGS_KEY
from creek.models import Authorship, Fragment, PrivacyTier, SourcePlatform
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from pathlib import Path

_FRAGMENTS_SUBDIR = "01-Fragments"

#: Platforms whose ingests are AI-chat conversations (they leak in today).
_AI_CHAT_PLATFORMS = frozenset(
    {SourcePlatform.CLAUDE.value, SourcePlatform.CHATGPT.value},
)


@dataclass(frozen=True)
class AudienceMix:
    """Distribution of the voice-eligible corpus by privacy tier.

    Attributes:
        by_tier: Count of eligible fragments per ``privacy_tier`` value
            (``open`` / ``personal`` / ``intimate`` / ``unclassified``).
            ``intimate`` is always ``0`` because INTIMATE fragments are not
            voice-eligible; it is reported for transparency.
        weighting_active: Whether graduated audience-authority weighting is
            applied during voice selection. Stub ``False`` until audience
            weighting lands.
    """

    by_tier: dict[str, int]
    weighting_active: bool

    @property
    def total(self) -> int:
        """Total eligible fragments across all tiers."""
        return sum(self.by_tier.values())


@dataclass(frozen=True)
class AICorpusLeak:
    """AI-chat ingests leaking into the voice-eligible corpus.

    Attributes:
        leaked: Voice-eligible fragments from an AI-chat platform
            (``claude`` / ``chatgpt``) that have **not** been quarantined —
            i.e. their conversation has no AI-authored sibling turn, so the
            fragment still fuses AI prose into the voice corpus. Once AI-chat
            turns are split, a freshly-ingested chat has an AI-authored sibling
            per turn, so its human turns no longer count and this drops to ≈0;
            legacy *merged* fragments (no AI sibling) still flag, which is what
            drives the re-ingest migration.
        eligible_total: Size of the voice-eligible corpus.
    """

    leaked: int
    eligible_total: int

    @property
    def fraction(self) -> float:
        """Leaked fraction of the eligible corpus; ``0.0`` for an empty corpus."""
        if self.eligible_total == 0:
            return 0.0
        return self.leaked / self.eligible_total


@dataclass(frozen=True)
class DeslopStatus:
    """De-slop attestation read from a drafted essay's frontmatter.

    Attributes:
        draft: The drafted-essay path that was probed.
        attested: Whether a ``voice_distance`` attestation is present (the
            AI-mannerism guard ran and recorded a result).
        voice_distance: The recorded distance, or ``None`` when unattested.
        residual_findings: Count of recorded residual voice findings.
        status: A human-readable reason. Stub wording until the faithful
            de-slop pass adds the rewritten-vs-measured distinction.
    """

    draft: str
    attested: bool
    voice_distance: float | None
    residual_findings: int
    status: str


@dataclass(frozen=True)
class VoiceAuthenticityReport:
    """The full voice-authenticity audit for one vault (and optional draft).

    Attributes:
        audience_mix: The audience-mix sub-score.
        ai_corpus_leak: The AI-corpus-leak sub-score.
        deslop: The de-slop sub-score, or ``None`` when no draft was probed.
    """

    audience_mix: AudienceMix
    ai_corpus_leak: AICorpusLeak
    deslop: DeslopStatus | None

    def _deslop_line(self) -> str:
        """Render the de-slop summary fragment."""
        if self.deslop is None:
            return "(no --draft given)"
        if self.deslop.attested:
            return (
                f"voice_distance {self.deslop.voice_distance:.4f}, "
                f"{self.deslop.residual_findings} residual finding(s) "
                f"— {self.deslop.status}"
            )
        return f"no attestation — {self.deslop.status}"

    def summary_line(self) -> str:
        """Return the human-readable multi-line summary of the report."""
        mix = self.audience_mix
        leak = self.ai_corpus_leak
        weighting = "ON" if mix.weighting_active else "OFF"
        pct = f"{leak.fraction * 100:.1f}"
        return (
            f"Voice authenticity — corpus of {mix.total} eligible fragments\n"
            f"  audience mix:  OPEN {mix.by_tier['open']}  "
            f"PERSONAL {mix.by_tier['personal']}  "
            f"INTIMATE {mix.by_tier['intimate']} (excluded)  "
            f"UNCLASSIFIED {mix.by_tier['unclassified']}   "
            f"weighting: {weighting}\n"
            f"  AI-corpus leak: {leak.leaked}/{leak.eligible_total} ({pct}%) "
            f"fragments are claude/chatgpt ingests\n"
            f"  de-slop:       {self._deslop_line()}"
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return the JSON-serialisable representation of the report."""
        deslop: dict[str, object] | None = None
        if self.deslop is not None:
            deslop = {
                "draft": self.deslop.draft,
                "attested": self.deslop.attested,
                "voice_distance": self.deslop.voice_distance,
                "residual_findings": self.deslop.residual_findings,
                "status": self.deslop.status,
            }
        return {
            "eligible_total": self.audience_mix.total,
            "audience_mix": {
                "weighting_active": self.audience_mix.weighting_active,
                "by_tier": self.audience_mix.by_tier.copy(),
                "total": self.audience_mix.total,
            },
            "ai_corpus_leak": {
                "leaked": self.ai_corpus_leak.leaked,
                "eligible_total": self.ai_corpus_leak.eligible_total,
                "fraction": self.ai_corpus_leak.fraction,
            },
            "deslop": deslop,
        }

    def to_json(self) -> str:
        """Return the report as a stable, indented JSON string."""
        return json.dumps(self.to_json_dict(), indent=2)


def _probe_audience_mix(eligible: list[Fragment]) -> AudienceMix:
    """Bucket the eligible corpus by ``privacy_tier``.

    Args:
        eligible: The voice-eligible fragments.

    Returns:
        The audience-mix sub-score (``weighting_active`` stubbed ``False``).
    """
    by_tier = {tier.value: 0 for tier in PrivacyTier}
    for fragment in eligible:
        tier = str(fragment.privacy_tier)
        by_tier[tier] = by_tier.get(tier, 0) + 1
    return AudienceMix(by_tier=by_tier, weighting_active=False)


def _conversation_key(fragment: Fragment) -> tuple[str, str | None, str | None] | None:
    """Return a key grouping a turn's human and AI fragments, or ``None``.

    Both halves of a split turn share platform, conversation id, and source
    file, so this pairs a human turn with its AI sibling even when the export
    carried no conversation id. Returns ``None`` when the fragment has neither
    a conversation id nor a source file — there is then no reliable signal to
    pair siblings on, so such a fragment is never treated as quarantined.
    """
    source = fragment.source
    if source.conversation_id is source.original_file is None:
        return None
    return (
        str(source.platform),
        source.conversation_id,
        source.original_file,
    )


def _probe_ai_corpus_leak(
    eligible: list[Fragment],
    all_fragments: list[Fragment],
) -> AICorpusLeak:
    """Count un-quarantined AI-chat fragments in the voice-eligible corpus.

    A conversation is *quarantined* once it contains an AI-authored sibling
    turn (the per-turn split). An eligible AI-chat fragment from a
    conversation with no such sibling is still a merged fragment fusing AI
    prose into the corpus, and counts as leaked.

    Args:
        eligible: The voice-eligible fragments.
        all_fragments: Every loaded fragment (used to find AI-authored
            siblings that mark a conversation as quarantined).

    Returns:
        The AI-corpus-leak sub-score.
    """
    quarantined = {
        key
        for fragment in all_fragments
        if str(fragment.source.platform) in _AI_CHAT_PLATFORMS
        and str(fragment.source.author) == Authorship.AI.value
        and (key := _conversation_key(fragment)) is not None
    }
    leaked = sum(
        1
        for fragment in eligible
        if str(fragment.source.platform) in _AI_CHAT_PLATFORMS
        and _conversation_key(fragment) not in quarantined
    )
    return AICorpusLeak(leaked=leaked, eligible_total=len(eligible))


def _probe_deslop(draft_path: Path) -> DeslopStatus:
    """Read a draft's voice-fidelity attestation from its frontmatter.

    Args:
        draft_path: The drafted-essay markdown file.

    Returns:
        The de-slop sub-score for *draft_path*.
    """
    post = frontmatter.load(str(draft_path))
    raw_distance = post.metadata.get(VOICE_DISTANCE_KEY)
    findings = post.metadata.get(VOICE_FINDINGS_KEY, [])
    residual = len(findings) if isinstance(findings, list) else 0
    if not isinstance(raw_distance, (int, float)):
        return DeslopStatus(
            draft=str(draft_path),
            attested=False,
            voice_distance=None,
            residual_findings=residual,
            status="no voice-fidelity attestation in draft frontmatter",
        )
    return DeslopStatus(
        draft=str(draft_path),
        attested=True,
        voice_distance=float(raw_distance),
        residual_findings=residual,
        status="attested (rewritten-vs-measured detection not yet recorded)",
    )


def build_voice_authenticity_report(
    vault_path: Path,
    *,
    draft_path: Path | None,
) -> VoiceAuthenticityReport:
    """Build the voice-authenticity report for *vault_path*.

    Walks the vault's ``01-Fragments`` tree via the canonical
    :func:`creek.vault.reader.iter_vault_fragments` loader, filters to the
    voice-eligible corpus, and runs the three skeleton probes. When
    *draft_path* is given its frontmatter is read for the de-slop sub-score.

    Args:
        vault_path: Vault root.
        draft_path: Optional drafted essay to probe for de-slop attestation.

    Returns:
        The assembled :class:`VoiceAuthenticityReport`.
    """
    records = iter_vault_fragments(vault_path / _FRAGMENTS_SUBDIR)
    fragments = [fragment for _path, fragment, _body, _raw in records]
    eligible = [fragment for fragment in fragments if fragment.voice_proxy_eligible]
    deslop = _probe_deslop(draft_path) if draft_path is not None else None
    return VoiceAuthenticityReport(
        audience_mix=_probe_audience_mix(eligible),
        ai_corpus_leak=_probe_ai_corpus_leak(eligible, fragments),
        deslop=deslop,
    )
