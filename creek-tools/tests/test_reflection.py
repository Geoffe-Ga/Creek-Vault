"""Mutation-grade tests for the deterministic Reflection node (#473).

Each rubric dimension gets a test that injects *exactly* that defect and asserts
the EXACT verdict plus a finding for the right dimension — not merely "not
PASS". A clean draft passes; a no-draft input escalates; and the Conductor
escalates rather than ships when the round budget is exhausted on REVISE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.author.checks import (
    _TIER_RANK,
    check_privacy_compliance,
    check_voice_fidelity,
)
from creek.author.conductor import Conductor
from creek.author.models import (
    EvidenceBundle,
    EvidenceClaim,
    OntologyAnalysis,
    OntologyParadox,
    ReflectionResult,
)
from creek.author.reflection import ReflectionNode
from creek.author.voice import VoiceAgent
from creek.config import AIStyleConfig
from creek.generate.ai_style.model import (
    Finding,
    ScanReport,
    Span,
    VoiceFingerprint,
)
from creek.models import MediumContract, PrivacyTier

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _grounded() -> EvidenceBundle:
    """Return a single grounded, attributed-free, paradox-free evidence bundle."""
    return EvidenceBundle(
        claims=[EvidenceClaim(claim="F6 names pluralism", source_fragments=["frag-a"])]
    )


def _seed_fragment(
    vault: Path,
    frag_id: str,
    body: str,
    tier: PrivacyTier = PrivacyTier.OPEN,
) -> None:
    """Write a minimal fragment file with *body* and *tier* into the vault."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "t"\n'
        f"privacy_tier: {tier.value}\n"
        f"source:\n  platform: journal\n  author: self\n---\n{body}\n",
        encoding="utf-8",
    )


def test_uncited_claim_revises_with_citation_finding() -> None:
    """An uncited claim → REVISE with a citation_completeness finding."""
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="bold unsupported assertion", source_fragments=[])]
    )

    result = ReflectionNode().review("A clean body about ideas.", evidence)

    assert result.decision == "REVISE"
    assert any(f.dimension == "citation_completeness" for f in result.findings)


def test_alias_misuse_revises_with_ontology_finding() -> None:
    """A legacy alias ("origins" not "rising") → REVISE + ontological_accuracy."""
    body = "The piece traces the origins of the wave."

    result = ReflectionNode().review(body, _grounded())

    assert result.decision == "REVISE"
    finding = next(f for f in result.findings if f.dimension == "ontological_accuracy")
    assert "rising" in finding.message


def test_unglossed_jargon_revises_with_jargon_finding() -> None:
    """A bespoke term used without a gloss → REVISE + unglossed_jargon finding."""
    body = "The whole arc climbs toward Ultraviolet and then settles."

    result = ReflectionNode().review(body, _grounded())

    assert result.decision == "REVISE"
    finding = next(f for f in result.findings if f.dimension == "unglossed_jargon")
    assert finding.severity == "MID"
    assert "Ultraviolet" in finding.message


def test_glossed_jargon_raises_no_jargon_finding() -> None:
    """A bespoke term glossed on first mention → no unglossed_jargon finding."""
    body = (
        "The whole arc climbs toward Ultraviolet — the altitude where your own "
        "will quiets and something larger acts through you."
    )

    result = ReflectionNode().review(body, _grounded())

    assert not any(f.dimension == "unglossed_jargon" for f in result.findings)


def test_privacy_leak_revises_with_privacy_finding(tmp_path: Path) -> None:
    """A cited intimate fragment leaked above the OPEN default → privacy finding."""
    secret = "the intimate confession nobody should publish"
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = f"Here is the leak: {secret}"

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert result.decision == "REVISE"
    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_short_coincidental_fragment_does_not_flag(tmp_path: Path) -> None:
    """A short, common over-tier fragment body present by chance must NOT flag.

    A one-to-three-word fragment body ("the project") can appear verbatim in
    innocuous prose coincidentally; flagging it would force the HARD privacy
    gate to REVISE on a draft that leaked nothing. Only substantive verbatim
    overlap should trip the gate (#508).
    """
    _seed_fragment(tmp_path, "frag-a", "the project", tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "We discussed the project roadmap in an open, publishable way."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_substantive_leak_still_flags(tmp_path: Path) -> None:
    """A substantive (>= 4-word) verbatim over-tier leak still trips the gate.

    Tightening the match to dodge short-fragment false positives must not let a
    genuine leak through — a multi-word protected snippet appearing verbatim is
    still a HARD privacy finding (#508).
    """
    secret = "my private therapy session notes"
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = f"Here it is: {secret} — oops."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_exact_threshold_fragment_flags(tmp_path: Path) -> None:
    """A body at exactly the word threshold still flags (off-by-one guard).

    The threshold is ``len(words) < _MIN_PROTECTED_LEAK_WORDS``; an accidental
    ``<=`` would silently swallow 4-word secrets. This pins the boundary (#508).
    """
    secret = "four word protected secret"  # exactly _MIN_PROTECTED_LEAK_WORDS
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = f"The draft reveals: {secret}."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_reflowed_line_break_still_flags(tmp_path: Path) -> None:
    """A verbatim leak split across a line break is caught after normalisation.

    Whitespace is collapsed on both sides before matching, so reflowing the
    protected text across a newline cannot hide an otherwise-verbatim leak from
    the HARD gate (#508).
    """
    secret = "my private therapy session notes"
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "leaked here: my private therapy\nsession notes — oops"

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_no_flag_on_substring_within_larger_word(tmp_path: Path) -> None:
    """A protected phrase embedded inside larger tokens must not flag.

    Word-boundary anchoring means the snippet only counts as leaked when it
    appears as a bounded phrase, not as a substring glued inside other text
    (#508).
    """
    secret = "private session notes record"
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "Xprivate session notes recordY"  # no word boundaries around the phrase

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_open_fragment_passes(tmp_path: Path) -> None:
    """An OPEN cited fragment at the OPEN default → no privacy finding (PASS)."""
    text = "an openly publishable observation"
    _seed_fragment(tmp_path, "frag-a", text, tier=PrivacyTier.OPEN)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    result = ReflectionNode().review(
        f"Body: {text}", evidence, contract=contract, vault=tmp_path
    )

    assert result.decision == "PASS"
    assert result.findings == []


def test_privacy_skips_cited_fragment_absent_from_vault(tmp_path: Path) -> None:
    """A claim citing a fragment id not present in the vault raises no privacy finding.

    The privacy check can only resolve the tier of fragments it can load; a
    cited id with no matching vault file is unresolvable, so it is skipped
    rather than guessed (no crash, no fabricated finding). The claim still
    carries a source-fragment id, so the hard citation gate is satisfied.
    """
    (tmp_path / "01-Fragments").mkdir(parents=True, exist_ok=True)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-missing"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    result = ReflectionNode().review(
        "a body with no leak", evidence, contract=contract, vault=tmp_path
    )

    assert result.decision == "PASS"
    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_every_privacy_tier_has_a_rank() -> None:
    """Every PrivacyTier member is ranked, so the privacy gate never KeyErrors.

    Adding a tier without updating ``_TIER_RANK`` would otherwise raise at
    review time; this fails loudly at test time instead (#509).
    """
    for tier in PrivacyTier:
        assert tier in _TIER_RANK


def test_privacy_unranked_fragment_tier_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unranked cited fragment tier fails closed to most-restrictive, not KeyError.

    Simulates a future ``PrivacyTier`` missing from ``_TIER_RANK`` by deleting
    an existing entry: the cited fragment is still treated as over-tier
    (flagged) and the rank lookup does not raise (#509).
    """
    monkeypatch.delitem(_TIER_RANK, PrivacyTier.INTIMATE)
    secret = "my private therapy session notes"
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = f"leaked: {secret}"

    findings = check_privacy_compliance(body, evidence, tmp_path, contract)

    assert any(f.dimension == "privacy_compliance" for f in findings)


def test_legacy_alias_maps_have_no_key_collisions() -> None:
    """The merged alias map keeps every key — no phase/mode/frequency collision."""
    from creek.author.checks import _LEGACY_ALIASES
    from creek.models import (
        FREQUENCY_LEGACY_ALIASES,
        MODE_LEGACY_ALIASES,
        PHASE_LEGACY_ALIASES,
    )

    expected = (
        len(PHASE_LEGACY_ALIASES)
        + len(MODE_LEGACY_ALIASES)
        + len(FREQUENCY_LEGACY_ALIASES)
    )
    assert len(_LEGACY_ALIASES) == expected


def test_paradox_bare_conjunction_off_topic_is_flattened() -> None:
    """A contrast cue unrelated to the paradox's vocabulary does not preserve it.

    Closes the bare-conjunction bypass (#505 review): a stray "but" with no
    topical overlap is treated as a flattened tension, not a preserved one.
    """
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])],
        ontology=OntologyAnalysis(
            paradoxes=(
                OntologyParadox(
                    kind="dosage",
                    fragment_ids=("frag-a",),
                    description="solitude nourishes while crowds drain",
                ),
            )
        ),
    )
    body = "I went to the store but forgot the milk."

    result = ReflectionNode().review(body, evidence)

    assert result.decision == "REVISE"
    assert any(f.dimension == "paradox_preservation" for f in result.findings)


def test_resolved_paradox_revises_with_paradox_finding() -> None:
    """A flattened paradox (no contrast cue) → REVISE + paradox_preservation."""
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])],
        ontology=OntologyAnalysis(
            paradoxes=(
                OntologyParadox(
                    kind="dosage",
                    fragment_ids=("frag-a", "frag-b"),
                    description="solitude nourishes while crowds drain",
                ),
            )
        ),
    )
    # Body surfaces only one side, with no contrast/tension marker.
    body = "Solitude is purely nourishing and good for everyone always."

    result = ReflectionNode().review(body, evidence)

    assert result.decision == "REVISE"
    assert any(f.dimension == "paradox_preservation" for f in result.findings)


def test_preserved_paradox_passes_on_contrast_cue() -> None:
    """A body carrying a contrast cue keeps the tension → no paradox finding."""
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])],
        ontology=OntologyAnalysis(
            paradoxes=(
                OntologyParadox(
                    kind="dosage",
                    fragment_ids=("frag-a",),
                    description="solitude nourishes while crowds drain",
                ),
            )
        ),
    )
    body = "Solitude nourishes, but crowds can drain — both are true at once."

    result = ReflectionNode().review(body, evidence)

    assert result.decision == "PASS"


def test_false_attribution_revises_with_attribution_finding() -> None:
    """A claim borrowed from an author, unattributed in the body → attribution."""
    evidence = EvidenceBundle(
        claims=[
            EvidenceClaim(
                claim="Specific knowledge cannot be taught.",
                source_fragments=["frag-a"],
                author_slug="naval-ravikant",
            )
        ]
    )
    body = "Specific knowledge cannot be taught, only learned through apprenticeship."

    result = ReflectionNode().review(body, evidence)

    assert result.decision == "REVISE"
    assert any(f.dimension == "attribution_correctness" for f in result.findings)


def test_attributed_claim_passes() -> None:
    """Naming the borrowed author in the body clears the attribution gate."""
    evidence = EvidenceBundle(
        claims=[
            EvidenceClaim(
                claim="Specific knowledge cannot be taught.",
                source_fragments=["frag-a"],
                author_slug="naval-ravikant",
            )
        ]
    )
    body = "As Naval Ravikant argues, specific knowledge cannot be taught."

    assert ReflectionNode().review(body, evidence).decision == "PASS"


def test_voice_fidelity_maps_scan_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    """``check_voice_fidelity`` maps a ScanReport's findings to voice_fidelity."""
    report = ScanReport(
        findings=[
            Finding(
                tell_id="t",
                category="lexical_tics",
                feature_key="k",
                span=Span(0, 0),
                line=1,
                excerpt="",
                draft_rate=2.0,
                user_rate=0.0,
                direction="over",
                message="over-uses em dashes",
            )
        ],
        voice_distance=0.9,
    )

    def _fake_scan(
        _text: str,
        *,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
        context: str = "article",
    ) -> ScanReport:
        del fingerprint, config, context
        return report

    monkeypatch.setattr("creek.author.checks.scan", _fake_scan)

    findings = check_voice_fidelity(
        "some body", VoiceFingerprint(fragment_count=5), AIStyleConfig()
    )

    assert findings  # non-empty
    assert all(f.dimension == "voice_fidelity" for f in findings)
    # The over-ceiling distance (0.9 > 0.35) adds a HIGH finding.
    assert any(f.severity == "HIGH" for f in findings)


def test_voice_fidelity_within_ceiling_adds_no_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan under the distance ceiling yields no HIGH voice finding."""
    report = ScanReport(findings=[], voice_distance=0.1)

    def _fake_scan(
        _text: str,
        *,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
        context: str = "article",
    ) -> ScanReport:
        del fingerprint, config, context
        return report

    monkeypatch.setattr("creek.author.checks.scan", _fake_scan)

    findings = check_voice_fidelity(
        "body", VoiceFingerprint(fragment_count=5), AIStyleConfig()
    )

    assert findings == []


def test_voice_fidelity_skips_without_fingerprint() -> None:
    """No fingerprint/config → voice cannot be measured → no findings."""
    assert check_voice_fidelity("body", None, None) == []


def test_privacy_skips_uncited_vault_fragment(tmp_path: Path) -> None:
    """An intimate fragment present in the vault but NOT cited is not flagged."""
    secret = "an intimate note nobody cited"
    _seed_fragment(tmp_path, "frag-uncited", secret, tier=PrivacyTier.INTIMATE)
    _seed_fragment(tmp_path, "frag-a", "an open cited note", tier=PrivacyTier.OPEN)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    result = ReflectionNode().review(
        f"Body mentions {secret}", evidence, contract=contract, vault=tmp_path
    )

    # The intimate fragment is uncited, so its presence is not a privacy breach.
    assert result.decision == "PASS"
    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_clean_draft_passes() -> None:
    """A grounded, canonical, attributed, leak-free draft → PASS, no findings."""
    result = ReflectionNode().review("A grounded, canonical observation.", _grounded())

    assert result.decision == "PASS"
    assert result.findings == []


def test_empty_body_or_no_evidence_escalates() -> None:
    """An empty body or claim-less evidence cannot be authored → ESCALATE."""
    assert ReflectionNode().review("   ", _grounded()).decision == "ESCALATE"
    assert ReflectionNode().review("body", EvidenceBundle()).decision == "ESCALATE"


def test_default_biographical_grounding_lower_tracks_draft_config() -> None:
    """The desk default cannot silently drift from ``DraftConfig.grounding_lower``."""
    from creek.author.reflection import _DEFAULT_BIOGRAPHICAL_GROUNDING_LOWER
    from creek.config import DraftConfig

    assert DraftConfig().grounding_lower == _DEFAULT_BIOGRAPHICAL_GROUNDING_LOWER


def test_conductor_escalates_on_exhaustion(tmp_path: Path) -> None:
    """A reflection that always REVISEs exhausts the budget → ESCALATE at round 2."""

    class _AlwaysRevise:
        def review(self, *_args: object, **_kwargs: object) -> ReflectionResult:
            return ReflectionResult(decision="REVISE")

    conductor = Conductor(
        specialists=[],
        voice=VoiceAgent(),
        reflection=_AlwaysRevise(),
        max_rounds=2,
    )

    draft = conductor.run(medium="research", query="q", vault=tmp_path)

    assert draft.rounds == 2
    assert draft.verdict == "ESCALATE"


def test_privacy_leak_flags_when_protected_body_ends_with_period(
    tmp_path: Path,
) -> None:
    """A leak whose protected body ends in a period must still flag (#939).

    The verbatim matcher wraps the escaped snippet in unconditional word-boundary
    anchors. A trailing anchor can never assert after a period, so an exact,
    word-for-word disclosure of an INTIMATE fragment passes the HARD privacy
    gate in silence.
    """
    secret = "I cheated on my partner last spring."
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "Here is the leak: I cheated on my partner last spring. That is all."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_leak_flags_when_protected_body_starts_with_a_quote(
    tmp_path: Path,
) -> None:
    """A leak whose protected body opens on a quote mark must still flag (#939).

    A leading word-boundary anchor cannot assert when the protected snippet's
    first character is punctuation and the draft precedes it with a space, so a
    quoted INTIMATE passage is reproduced verbatim with no privacy finding.
    """
    secret = '"I never told anyone what really happened'
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = 'She wrote: "I never told anyone what really happened in her diary.'

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_leak_flags_when_protected_body_is_punctuated_on_both_edges(
    tmp_path: Path,
) -> None:
    """A leak punctuated on both edges must still flag (#939).

    Both word-boundary anchors fail at once here, and a fully quoted sentence is
    the most common real shape for a confession, so the HARD gate is completely
    blind to an exact reproduction of the INTIMATE fragment.
    """
    secret = '"I lied to my therapist repeatedly."'
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = 'The note read: "I lied to my therapist repeatedly." Yikes.'

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_leak_flags_when_leading_punctuation_abuts_a_word_character(
    tmp_path: Path,
) -> None:
    """A leak opening on an em dash glued to a word must keep flagging (#939).

    Regression guard against the naive repair: the leading/trailing boundary
    assertion must be dropped when the protected snippet's own edge character is
    punctuation, otherwise this leak regresses (#939). Swapping the anchors for
    unconditional lookarounds would fail here because the em dash abuts a word
    character in the draft. Un-spaced em dashes are ordinary prose, so this is a
    real disclosure path, not a synthetic one.
    """
    secret = "—the affair nobody knows about"  # leading em dash, U+2014
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "He said—the affair nobody knows about, quietly."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_leak_flags_when_trailing_punctuation_abuts_a_word_character(
    tmp_path: Path,
) -> None:
    """A leak ending on a period glued to the next word must keep flagging (#939).

    Regression guard against the naive repair: the leading/trailing boundary
    assertion must be dropped when the protected snippet's own edge character is
    punctuation, otherwise this leak regresses (#939). An unconditional trailing
    lookahead would reject this match because the sentence-final period runs
    straight into the following word.
    """
    secret = "I told a lie about the money."
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "It reads I told a lie about the money.Then it stops."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_punctuated_secret_glued_inside_a_word_does_not_flag(
    tmp_path: Path,
) -> None:
    """A punctuated secret glued onto a word at its word-char edge does not flag.

    The snippet's first character is a word character, so that edge keeps its
    boundary assertion even once the trailing one is dropped: the phrase is not
    bounded here and must stay unflagged both before and after the fix (#939).
    """
    secret = "I cheated on my partner last spring."
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "xI cheated on my partner last spring."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_paraphrase_of_punctuated_secret_does_not_flag(tmp_path: Path) -> None:
    """A paraphrase of a punctuated INTIMATE secret raises no privacy finding.

    Loosening the boundary assertions must not turn the deterministic verbatim
    check into a fuzzy one. Paraphrase is out of deterministic scope and belongs
    to the semantic judge (#474); it stays that way under #939.
    """
    secret = "I cheated on my partner last spring."
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "He was unfaithful to his partner sometime in the spring."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_short_punctuated_secret_does_not_flag(tmp_path: Path) -> None:
    """A sub-threshold punctuated secret stays unflagged.

    The four-word short-circuit still fires first, so loosening the boundaries
    does not widen the HARD gate for short, generic snippets that can co-occur
    with innocuous prose by coincidence (#939).
    """
    secret = "I cheated badly."  # 3 words — below _MIN_PROTECTED_LEAK_WORDS
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "The confession was blunt: I cheated badly. Nothing more."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_open_tier_punctuated_body_does_not_flag(tmp_path: Path) -> None:
    """An OPEN cited fragment quoted verbatim raises no privacy finding.

    The tier-rank guard runs before the verbatim matcher, so a fragment sitting
    at the contract's own ceiling is not over-tier and the loosened boundaries
    cannot turn ordinary quotation of publishable material into a leak (#939).
    """
    secret = "I published this openly last spring."
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.OPEN)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "Here it is: I published this openly last spring."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert not any(f.dimension == "privacy_compliance" for f in result.findings)
