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
from creek.author.contracts import load_medium_contract
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


def _grounded() -> EvidenceBundle:
    """Return a single grounded, attributed-free, paradox-free evidence bundle."""
    return EvidenceBundle(
        claims=[EvidenceClaim(claim="F6 names pluralism", source_fragments=["frag-a"])]
    )


def _seed_fragment(
    vault: Path,
    frag_id: str,
    body: str,
    tier: PrivacyTier | None = PrivacyTier.OPEN,
    *,
    subtree: str = "01-Fragments",
    title: str = "t",
) -> None:
    """Write a minimal fragment file with *body* and *tier* into the vault.

    The defaults reproduce the pre-#1341 shape exactly — an ``open`` fragment
    under ``01-Fragments/Notes`` — so every existing caller is untouched.

    Args:
        vault: Vault root to seed under.
        frag_id: Fragment id; also the file stem. Two records sharing an id
            must therefore be seeded into two different ``subtree`` values.
        body: Markdown body written below the frontmatter.
        tier: Tier to stamp, or ``None`` to omit the ``privacy_tier`` key
            entirely — the legacy / hand-edited shape, which
            :class:`~creek.models.Fragment` loads as
            :attr:`~creek.models.PrivacyTier.UNCLASSIFIED` (``creek/models.py``
            field default).
        subtree: Top-level vault folder to seed under. The desk's specialists
            gather from ``01-Fragments``, ``09-Reference`` and
            ``11-Other-Authors``; the ``Notes`` leaf is kept under whichever
            one is named.
        title: Fragment title.
    """
    folder = vault / subtree / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    tier_line = "" if tier is None else f"privacy_tier: {tier.value}\n"
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"{tier_line}"
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


def test_privacy_punctuation_led_secret_glued_at_its_word_tail_does_not_flag(
    tmp_path: Path,
) -> None:
    """A punctuation-led secret glued onto a word at its tail does not flag.

    Isolates the trailing boundary assertion (#939): the snippet opens on a
    quote, so the leading assertion is dropped and cannot mask the result. Its
    last character is a word character, so that edge must keep its assertion
    and refuse a match that runs straight into the next word.
    """
    secret = '"I never told anyone what really happened'
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = 'She wrote: "I never told anyone what really happenedX'

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    assert not any(f.dimension == "privacy_compliance" for f in result.findings)


def test_privacy_punctuated_leak_finding_is_high_and_names_the_fragment(
    tmp_path: Path,
) -> None:
    """The punctuation-edged leak finding keeps HIGH severity and full detail.

    Pins the payload of the finding this fix restores (#939) so a downgrade of
    the HARD privacy gate's severity, or a message that stops naming the
    offending fragment and its tier, cannot pass silently.
    """
    secret = "I cheated on my partner last spring."
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)
    body = "Here is the leak: I cheated on my partner last spring. That is all."

    result = ReflectionNode().review(body, evidence, contract=contract, vault=tmp_path)

    finding = next(f for f in result.findings if f.dimension == "privacy_compliance")
    assert finding.severity == "HIGH"
    assert "frag-a" in finding.message
    assert "intimate" in finding.message


def test_privacy_missing_contract_gates_at_the_strictest_ceiling(
    tmp_path: Path,
) -> None:
    """A ``None`` contract must gate at OPEN, not disable the HARD gate (#1310).

    ``check_privacy_compliance`` used to return ``[]`` whenever *either* the
    vault or the contract was ``None``. Every contract-less caller therefore
    got a silently disabled privacy gate — which is how ``creek author`` shipped
    an intimate fragment's protected text to stdout with ``verdict=PASS``.

    A missing contract is now the *strictest* ceiling (``PrivacyTier.OPEN``),
    so an above-OPEN cited fragment reproduced verbatim in the body is one
    ``HIGH`` finding. Every shipped medium template already declares
    ``default_privacy_tier: open``, so this is a no-op for contract-bearing
    callers and closes the hole for everyone else.

    Today this returns zero findings.
    """
    secret = "the intimate confession nobody should publish"
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )

    body = f"Here is the leak: {secret}"

    findings = check_privacy_compliance(body, evidence, tmp_path, None)

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "frag-a" in findings[0].message
    assert "intimate" in findings[0].message


# ---------------------------------------------------------------------------
# #1341 — the HARD leak gate must police every corpus subtree
#
# ``_resolve_cited_tiers`` walks ``<vault>/01-Fragments`` alone, while the desk
# specialists gather evidence from ``01-Fragments``, ``09-Reference`` **and**
# ``11-Other-Authors`` (``creek/author/agents.py``'s ``_CORPUS_SUBDIRS``, used
# by ``_load_corpus``). A cited fragment living in either of the other two
# subtrees resolves to nothing, so the gate never learns its tier and the draft
# ships its protected text with ``verdict=PASS``.
# ---------------------------------------------------------------------------

_LEAK_SECRET = "the confession I never wanted written down"
"""A seven-word protected snippet — comfortably over ``_MIN_PROTECTED_LEAK_WORDS``.

Anything under five words cannot trip the gate at all, so a shorter snippet
would make every test below pass vacuously. Deliberately plain English: no
legacy taxonomy alias (``origins``, ``solo``, ``pitch``, …) and no bespoke
ontology term, so ``privacy_compliance`` is the only dimension it can raise.
"""

_DECOY_BODY = "a plainly publishable paragraph about tide charts"
"""The twin body a duplicate-id record carries, never reproduced in a draft.

Its job is to be the record the resolver *prefers* — by tier, by walk order, or
by both — so that preferring it is observable as a missing finding.
"""


def _draft_reproducing(secret: str) -> str:
    """Return a longer reviewed body that reproduces *secret* verbatim.

    Args:
        secret: The protected snippet to embed word for word.

    Returns:
        Innocuous prose with *secret* inside it — the realistic hazard shape,
        where nothing but the protected text itself gives the leak away.
    """
    return (
        "The piece opens on an ordinary morning and then reproduces its "
        f"source word for word: {secret} — and it keeps going after that."
    )


_SUBTREE_TIER_CASES = [
    pytest.param(subtree, tier, expected, id=f"{subtree}-{label}")
    for subtree in ("09-Reference", "11-Other-Authors")
    for tier, expected, label in (
        (PrivacyTier.INTIMATE, "intimate", "intimate"),
        (PrivacyTier.PERSONAL, "personal", "personal"),
        (None, "unclassified", "absent-privacy-tier-key"),
    )
]
"""The cross product of the two unwalked corpus subtrees and three over-tier shapes.

``PERSONAL`` is included because it is *also* above an ``open`` ceiling — the
gate is not an intimate-only gate — and the key-absent shape because a legacy
or hand-edited fragment is exactly the content nobody has vouched for.
"""


@pytest.mark.parametrize(("subtree", "tier", "expected_tier"), _SUBTREE_TIER_CASES)
def test_privacy_gate_sees_over_tier_fragment_in_every_corpus_subtree(
    tmp_path: Path,
    subtree: str,
    tier: PrivacyTier | None,
    expected_tier: str,
) -> None:
    """An over-tier fragment leaks identically from any corpus subtree (#1341).

    One cited fragment, seeded outside ``01-Fragments``, whose protected body is
    reproduced verbatim in the draft under an ``open`` medium contract. The
    fragment's location in the vault is not a privacy property, so the finding
    must be the same one ``01-Fragments`` already produces: a single ``HIGH``
    ``privacy_compliance`` finding naming the fragment and its tier.

    The key-absent case asserts ``'unclassified'`` because
    :class:`~creek.models.Fragment` defaults ``privacy_tier`` to
    :attr:`~creek.models.PrivacyTier.UNCLASSIFIED` (verified in
    ``creek/models.py``), and the leak gate's ``_TIER_RANK`` puts
    ``UNCLASSIFIED`` at the *top* of the restrictiveness order — an untiered
    fragment is over every ceiling.

    Measured today: the resolver returns ``{}`` for both subtrees, so this
    reviews as ``PASS`` with zero findings.

    Args:
        tmp_path: Vault root for this case.
        subtree: The corpus subtree the fragment is seeded into.
        tier: The stamped tier, or ``None`` to omit the key entirely.
        expected_tier: The tier string the finding's message must name.
    """
    _seed_fragment(tmp_path, "frag-b", _LEAK_SECRET, tier, subtree=subtree)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-b"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    result = ReflectionNode().review(
        _draft_reproducing(_LEAK_SECRET),
        evidence,
        contract=contract,
        vault=tmp_path,
    )

    assert result.decision == "REVISE", result.findings
    leaks = [f for f in result.findings if f.dimension == "privacy_compliance"]
    assert len(leaks) == 1, result.findings
    assert leaks[0].severity == "HIGH"
    assert "'frag-b'" in leaks[0].message
    assert f"'{expected_tier}'" in leaks[0].message


@pytest.mark.parametrize(
    ("strict_subtree", "lax_subtree"),
    [
        pytest.param("09-Reference", "01-Fragments", id="strict-last"),
        pytest.param("01-Fragments", "09-Reference", id="strict-first"),
    ],
)
def test_duplicate_cited_id_resolves_to_the_most_restrictive_tier(
    tmp_path: Path,
    strict_subtree: str,
    lax_subtree: str,
) -> None:
    """Two vault records share an id — the gate must read the stricter tier (#1341).

    Fragment ids are not unique across a real vault: the same id can land in
    ``01-Fragments`` and in ``09-Reference`` (an import, a copy, a re-export).
    Once the resolver walks more than one subtree it will see both records, and
    "last write wins" would let the ``open`` twin overwrite the ``intimate``
    one and silently disarm the HARD gate. Resolution must fail closed to the
    most restrictive tier found for that id.

    Both walk orders are exercised, and the second one is the whole point.
    The walk runs in ``CORPUS_SUBDIRS`` order, so with the strict record in
    ``09-Reference`` it is simply seen *last* — and last-wins happens to give
    the right answer, for the wrong reason. Only ``strict-first`` — the
    ``intimate`` record in ``01-Fragments``, its ``open`` twin behind it —
    separates a real rank-max from that coincidence. Measured against a
    last-wins merge: ``strict-last`` still reports the leak while
    ``strict-first`` reports **nothing at all**, with the protected body
    sitting verbatim in the draft.

    Only the intimate body is reproduced in the draft, so a correct gate has
    both halves of the evidence it needs: the strict tier and the leaked text.

    Args:
        tmp_path: Vault root for this case.
        strict_subtree: Where the ``intimate`` record carrying the secret goes.
        lax_subtree: Where its harmless ``open`` twin goes.
    """
    _seed_fragment(
        tmp_path, "frag-d", _DECOY_BODY, PrivacyTier.OPEN, subtree=lax_subtree
    )
    _seed_fragment(
        tmp_path,
        "frag-d",
        _LEAK_SECRET,
        PrivacyTier.INTIMATE,
        subtree=strict_subtree,
    )
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-d"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    result = ReflectionNode().review(
        _draft_reproducing(_LEAK_SECRET),
        evidence,
        contract=contract,
        vault=tmp_path,
    )

    leaks = [f for f in result.findings if f.dimension == "privacy_compliance"]
    assert len(leaks) == 1, result.findings
    assert leaks[0].severity == "HIGH"
    assert "'frag-d'" in leaks[0].message
    assert "'intimate'" in leaks[0].message


@pytest.mark.parametrize(
    ("decoy_subtree", "leaker_subtree"),
    [
        pytest.param("01-Fragments", "11-Other-Authors", id="decoy-first"),
        pytest.param("11-Other-Authors", "01-Fragments", id="decoy-second"),
    ],
)
def test_duplicate_cited_id_at_equal_tier_cannot_mask_the_leaking_body(
    tmp_path: Path,
    decoy_subtree: str,
    leaker_subtree: str,
) -> None:
    """Same id, same tier, two bodies — walk order must not decide the verdict (#1341).

    Tier alone cannot break this tie: both records are ``intimate``, so a
    resolver that keeps one ``(tier, body)`` pair per id keeps whichever body it
    happened to see last. Sorting the walk makes that deterministic but not
    *correct* — it just fixes which of the two draws. The subtree name decides
    the sort (``01-Fragments`` < ``09-Reference`` < ``11-Other-Authors``), so
    the two cases here put the harmless twin on either side of the leaker and
    demand the same finding from both. Keeping **every** body stored under a
    cited id is the only implementation that satisfies both cases at once.

    Args:
        tmp_path: Vault root for this case.
        decoy_subtree: Where the harmless twin is seeded.
        leaker_subtree: Where the twin carrying the protected text is seeded.
    """
    _seed_fragment(
        tmp_path, "frag-e", _DECOY_BODY, PrivacyTier.INTIMATE, subtree=decoy_subtree
    )
    _seed_fragment(
        tmp_path, "frag-e", _LEAK_SECRET, PrivacyTier.INTIMATE, subtree=leaker_subtree
    )
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-e"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    result = ReflectionNode().review(
        _draft_reproducing(_LEAK_SECRET),
        evidence,
        contract=contract,
        vault=tmp_path,
    )

    leaks = [f for f in result.findings if f.dimension == "privacy_compliance"]
    assert len(leaks) == 1, result.findings
    assert "'frag-e'" in leaks[0].message
    assert "'intimate'" in leaks[0].message


def test_duplicate_cited_id_below_the_winner_is_still_body_checked(
    tmp_path: Path,
) -> None:
    """The loser record's body is protected too, because the *id* resolved intimate.

    An implementation that fails closed on the tier but keeps only the winning
    record's body still leaks: here the draft reproduces the ``open`` twin's
    body while the id resolves to ``intimate``, and the gate must flag it. The
    tier is a property of the cited id, and every body stored under that id is
    text the draft may not reproduce — pinning keep-ALL-bodies against a
    keep-only-the-winner's-body shortcut (#1341).
    """
    _seed_fragment(
        tmp_path,
        "frag-f",
        _LEAK_SECRET,
        PrivacyTier.INTIMATE,
        subtree="09-Reference",
    )
    second_body = "the quieter half of the same note, filed openly"
    _seed_fragment(tmp_path, "frag-f", second_body, PrivacyTier.OPEN)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-f"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    result = ReflectionNode().review(
        _draft_reproducing(second_body),
        evidence,
        contract=contract,
        vault=tmp_path,
    )

    leaks = [f for f in result.findings if f.dimension == "privacy_compliance"]
    assert len(leaks) == 1, result.findings
    assert "'frag-f'" in leaks[0].message
    assert "'intimate'" in leaks[0].message


def test_privacy_gate_polices_a_vault_with_no_01_fragments_dir(
    tmp_path: Path,
) -> None:
    """A vault with no ``01-Fragments`` folder is still policed (#1341).

    The resolver bails on the whole walk when ``<vault>/01-Fragments`` is not a
    directory, so a corpus held entirely under ``09-Reference`` — a
    reference-only vault, or one mid-migration — gets no privacy gate at all,
    not even a degraded one. The absence of one subtree must never disable the
    others.

    Measured today: zero findings, ``PASS``.
    """
    _seed_fragment(
        tmp_path,
        "frag-g",
        _LEAK_SECRET,
        PrivacyTier.INTIMATE,
        subtree="09-Reference",
    )
    assert not (tmp_path / "01-Fragments").exists()  # the precondition under test
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-g"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'frag-g'" in findings[0].message
    assert "'intimate'" in findings[0].message


def test_leak_gate_reads_the_true_tier_while_the_router_reads_the_admitted_one(
    tmp_path: Path,
) -> None:
    """The gate and the router see the same id at two different tiers — on purpose.

    Two records share ``frag-d``: an ``open`` one in ``01-Fragments`` and an
    ``intimate`` one in ``09-Reference``.

    * The **router** (``creek.author.agents.fragment_tier_map``) reads the
      *admitted* view. ``_load_corpus`` filters with ``tier_within_override``,
      so at an ``open`` ceiling the intimate copy never enters the evidence and
      the map reports ``open``. That half passes today; it is a pin, not a fix.
    * The **leak gate** reads the *true* tier, unfiltered and fail-closed. It
      exists to answer "is protected text in this draft?", and a gate that only
      looked at what the ceiling admitted would be blind to exactly the content
      it is there to catch. It must report ``intimate``.

    The divergence is therefore deliberate, and this test states it so a future
    "make these agree" cleanup has to argue with a named assertion rather than a
    silent assumption. The gate half is asserted through the public
    :func:`~creek.author.checks.check_privacy_compliance` message rather than
    the private resolver, whose return shape #1341 changes.
    """
    from creek.author.agents import fragment_tier_map
    from creek.classify.privacy_filter import PrivacyTierOverride

    _seed_fragment(tmp_path, "frag-d", _DECOY_BODY, PrivacyTier.OPEN)
    _seed_fragment(
        tmp_path,
        "frag-d",
        _LEAK_SECRET,
        PrivacyTier.INTIMATE,
        subtree="09-Reference",
    )
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-d"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    # Gate half — the true tier, above the ceiling, so the leak is caught.
    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'intimate'" in findings[0].message
    # Router half — the admitted view at the same ceiling, unchanged.
    assert fragment_tier_map(tmp_path, PrivacyTierOverride.OPEN) == {
        "frag-d": PrivacyTier.OPEN
    }


def test_privacy_without_a_vault_still_skips(tmp_path: Path) -> None:
    """``vault=None`` remains the one branch that skips the check (#1310).

    The surviving half of the old two-part skip. Without a vault there is no
    fragment file to resolve a tier or a protected body from, so there is
    nothing to gate on — asserted against the same corpus and body that
    :func:`test_privacy_missing_contract_gates_at_the_strictest_ceiling` flags,
    so the two tests differ in exactly one argument.
    """
    secret = "the intimate confession nobody should publish"
    _seed_fragment(tmp_path, "frag-a", secret, tier=PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )

    body = f"Here is the leak: {secret}"

    assert check_privacy_compliance(body, evidence, None, None) == []


def test_rubric_cannot_soften_a_verdict(tmp_path: Path) -> None:
    """No rubric can turn a finding into a ``PASS`` (#1310).

    ``ReflectionNode.review`` opens with ``del rubric``
    (``creek/author/reflection.py:102``): the deterministic checks gate on hard
    rules, and the per-dimension weights are accepted for interface stability
    only. That is a *security* property — a medium contract is authored inside
    the vault, so if weights were scored, a contract that weighted
    ``privacy_compliance`` at ``0.0`` could buy itself a clean verdict on a
    leaking draft.

    Pinned three ways against one body that trips ``ontological_accuracy``: no
    rubric, the shipped ``research`` contract's real weights, and an
    adversarial rubric that zeroes the offending dimension. All three must
    return the identical decision *and* the identical findings.
    """
    body = "The piece traces the origins of the wave."
    evidence = _grounded()
    contract_weights = load_medium_contract("research", tmp_path).reflection_rubric
    silencing = {
        "ontological_accuracy": 0.0,
        "citation_completeness": 0.0,
        "voice_fidelity": 0.0,
    }
    node = ReflectionNode()

    baseline = node.review(body, evidence, None)
    weighted = node.review(body, evidence, contract_weights)
    zeroed = node.review(body, evidence, silencing)

    assert baseline.decision == "REVISE"
    assert any(f.dimension == "ontological_accuracy" for f in baseline.findings)
    assert weighted.decision == baseline.decision
    assert weighted.findings == baseline.findings
    assert zeroed.decision == baseline.decision
    assert zeroed.findings == baseline.findings
