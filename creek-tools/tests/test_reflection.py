"""Mutation-grade tests for the deterministic Reflection node (#473).

Each rubric dimension gets a test that injects *exactly* that defect and asserts
the EXACT verdict plus a finding for the right dimension — not merely "not
PASS". A clean draft passes; a no-draft input escalates; and the Conductor
escalates rather than ships when the round budget is exhausted on REVISE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

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


def test_ordinary_english_verbs_do_not_block_pass() -> None:
    """Plain prose using "rising"/"absorb"/"collaborate"/"express" reaches PASS.

    Issue #1343's acceptance criterion. Those four words are ordinary English
    verbs *and* lower-cased ontology surface forms, so today each raises a MID
    ``unglossed_jargon`` finding on this body. ``reflection.py`` turns any
    finding into REVISE, and ``AuthorConductor`` loops REVISE until
    ``max_rounds`` is spent and then ESCALATEs to a human — so an essay about
    bread becomes unshippable. Asserted as the exact verdict plus an empty
    finding list: "no jargon finding" alone would pass while some other
    dimension quietly took over the block.
    """
    body = (
        "The bread was rising in the pan while I waited. I could not absorb "
        "what he said. We collaborate on Tuesdays and express ourselves badly."
    )

    result = ReflectionNode().review(body, _grounded())

    assert result.decision == "PASS"
    assert result.findings == []


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


# ---------------------------------------------------------------------------
# #1354 — the privacy ceiling must not be raisable from inside the vault
#
# ``check_privacy_compliance`` takes its ceiling from exactly one place: the
# medium contract's ``default_privacy_tier``. A medium contract is loaded from
# ``<vault>/00-Creek-Meta/Skills/mediums/<medium>.MEDIUM.md`` — i.e. from
# *inside the vault*, alongside the content it is meant to protect. Editing one
# YAML line in a skill file therefore disarms a HARD gate, and a contract
# declaring ``unclassified`` disarms it completely for every tier.
#
# The ceiling becomes the MORE RESTRICTIVE (lower ``_TIER_RANK``) of an
# operator-set ``author.max_reproduced_tier`` in ``creek_config.yaml`` and the
# contract's declared tier. A vault-authored contract can then only ever
# *narrow* the gate; widening it takes a deliberate edit to the config.
# ---------------------------------------------------------------------------


def _write_vault_config(vault: Path, tier: str) -> None:
    """Write ``<vault>/00-Creek-Meta/creek_config.yaml`` declaring the ceiling.

    The file is written through ``yaml.safe_dump`` so the on-disk shape is
    whatever :func:`creek.config.load_config` would itself round-trip, rather
    than a hand-formatted string that only happens to parse.

    Args:
        vault: Vault root; ``00-Creek-Meta`` is created if absent.
        tier: Value for ``author.max_reproduced_tier``. Deliberately typed as
            a bare ``str`` so a test can write a value the model must reject
            (see :func:`test_a_garbage_configured_tier_fails_closed_to_open`).
    """
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "creek_config.yaml").write_text(
        yaml.safe_dump({"author": {"max_reproduced_tier": tier}}),
        encoding="utf-8",
    )


def test_a_contract_alone_cannot_widen_the_privacy_gate(tmp_path: Path) -> None:
    """A vault-authored contract cannot licence its own leak (#1354).

    The headline defect. The contract declares ``intimate``, so today the
    intimate fragment sits at exactly the ceiling, ``rank > ceiling`` is
    ``False``, and the draft ships the protected body with zero findings —
    even though nobody outside the vault ever agreed to that ceiling.

    With no ``creek_config.yaml`` the configured tier is its default ``open``,
    which is stricter than the contract's ``intimate``, so the effective
    ceiling must be ``open`` and the leak must be one ``HIGH`` finding.

    Measured at HEAD: ``[]``.

    Args:
        tmp_path: Vault root for this case.
    """
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.INTIMATE
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'frag-a'" in findings[0].message
    assert "'intimate'" in findings[0].message
    assert "'open'" in findings[0].message
    assert "author.max_reproduced_tier" in findings[0].message


def test_an_unclassified_contract_cannot_disarm_the_gate_for_an_unvouched_fragment(
    tmp_path: Path,
) -> None:
    """``default_privacy_tier: unclassified`` is a total off-switch today (#1354).

    ``_TIER_RANK`` tops out at ``UNCLASSIFIED``, so a contract declaring that
    tier sets ``ceiling = 3`` and no fragment tier can ever exceed it. Measured
    at HEAD, the whole ``unclassified`` contract column is zeros for all five
    fragment shapes — including this one, a fragment file with no
    ``privacy_tier`` key at all, which is the content *nobody has vouched for*.

    This test also records that the issue body's claim — that "only
    ``unclassified`` fragments would still be caught" under such a contract —
    is measurably false. Nothing is caught. Under an ``unclassified`` ceiling
    the gate does not exist.

    Args:
        tmp_path: Vault root for this case.
    """
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, tier=None)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.UNCLASSIFIED
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'unclassified'" in findings[0].message
    assert "'open'" in findings[0].message


def test_a_contract_declaring_personal_cannot_disarm_the_gate_for_a_personal_fragment(
    tmp_path: Path,
) -> None:
    """The graded case: one notch of widening is still widening (#1354).

    The escalation is not all-or-nothing. A contract that quietly moves from
    ``open`` to ``personal`` buys itself verbatim reproduction of every
    ``personal`` fragment, which is the realistic edit — far less conspicuous
    than declaring ``unclassified`` — and at HEAD it succeeds.

    Args:
        tmp_path: Vault root for this case.
    """
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.PERSONAL)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.PERSONAL
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'personal'" in findings[0].message
    assert "'open'" in findings[0].message


def test_the_effective_ceiling_is_the_more_restrictive_not_the_more_permissive(
    tmp_path: Path,
) -> None:
    """Pin the fold direction: ``min`` of the ranks, never ``max`` (#1354).

    Do not delete this test; it is the only direct detector of the
    ``min``->``max`` inversion. Every other assertion in this file survives
    that mutation somewhere, because most rows of the matrix agree under either
    fold — the two ceilings differ in only one direction and the fragment tier
    often sits outside both.

    Here the configured tier is its default ``open`` (rank 0) and the contract
    declares ``intimate`` (rank 2). ``min`` resolves the effective ceiling to
    ``open``, so the intimate fragment leaks and this is one finding. ``max``
    resolves it to ``intimate``, the fragment sits at the ceiling, and the
    function returns zero findings while the protected body sits verbatim in
    the draft.

    Args:
        tmp_path: Vault root for this case.
    """
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.INTIMATE
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    leaks = [f for f in findings if f.dimension == "privacy_compliance"]
    assert len(leaks) == 1, findings
    assert "'open'" in leaks[0].message


# ---------------------------------------------------------------------------
# The full ceiling matrix.
#
# Both expectation tables below are written out BY HAND. Deriving them from
# ``_TIER_RANK`` (or from any ``min``/``max`` expression) would make the test
# and the implementation share a single bug: invert the fold in both places and
# the suite stays green. Every entry here is an independent statement about
# what the gate should do.
# ---------------------------------------------------------------------------

_CONFIG_ABSENT = "absent"
"""The ``configured`` key meaning "no ``creek_config.yaml`` in the vault at all".

Distinct from ``"open"`` on disk even though the two must behave identically:
the default has to hold for a vault that predates the key, not only for one
whose operator wrote it out.
"""

_CONFIGURED_KEYS = ("open", "personal", "intimate", "unclassified", _CONFIG_ABSENT)
"""Every state ``author.max_reproduced_tier`` can be in — one vault per entry."""

_CONTRACT_KEYS = ("open", "personal", "intimate", "unclassified")
"""Every tier a vault-authored medium contract can declare."""

_FRAGMENT_KEYS = ("open", "personal", "intimate", "unclassified", "unvouched")
"""Every cited-fragment shape, including the file with no ``privacy_tier`` key."""

_MATRIX_FRAGMENT_TIERS: dict[str, PrivacyTier | None] = {
    "open": PrivacyTier.OPEN,
    "personal": PrivacyTier.PERSONAL,
    "intimate": PrivacyTier.INTIMATE,
    "unclassified": PrivacyTier.UNCLASSIFIED,
    "unvouched": None,
}
"""Tier stamped on each seeded fragment; ``None`` omits the key entirely."""

_MATRIX_FRAGMENT_BODIES: dict[str, str] = {
    "open": "the open note about weekday grocery lists",
    "personal": "the personal note about a birthday dinner",
    "intimate": "the intimate letter I never mailed anywhere",
    "unclassified": "the unclassified page nobody has reviewed yet",
    "unvouched": "the unvouched scrap left in a drawer somewhere",
}
"""One distinct protected sentence per fragment, all reproduced verbatim.

Each is seven words — over ``_MIN_PROTECTED_LEAK_WORDS``, so none can pass
vacuously — plain English, and carries no legacy taxonomy alias, so
``privacy_compliance`` is the only dimension any of them can raise. No sentence
contains another, so a draft reproducing one cannot accidentally reproduce a
second and change the finding count.
"""

_EFFECTIVE_CEILING: dict[tuple[str, str], str] = {
    # An ``open`` contract already declares the strictest ceiling there is, so
    # nothing the operator configures can widen it.
    ("open", "open"): "open",
    ("open", "personal"): "open",
    ("open", "intimate"): "open",
    ("open", "unclassified"): "open",
    ("open", _CONFIG_ABSENT): "open",
    # A ``personal`` contract is narrowed by a stricter config and holds
    # against a laxer one.
    ("personal", "open"): "open",
    ("personal", "personal"): "personal",
    ("personal", "intimate"): "personal",
    ("personal", "unclassified"): "personal",
    ("personal", _CONFIG_ABSENT): "open",
    # An ``intimate`` contract: the config governs until the config is the
    # laxer of the two.
    ("intimate", "open"): "open",
    ("intimate", "personal"): "personal",
    ("intimate", "intimate"): "intimate",
    ("intimate", "unclassified"): "intimate",
    ("intimate", _CONFIG_ABSENT): "open",
    # ``unclassified`` is the total off-switch at HEAD. Under the fold the
    # config alone decides, because no ceiling is laxer than this one.
    ("unclassified", "open"): "open",
    ("unclassified", "personal"): "personal",
    ("unclassified", "intimate"): "intimate",
    ("unclassified", "unclassified"): "unclassified",
    ("unclassified", _CONFIG_ABSENT): "open",
}
"""``(contract tier, configured key)`` -> the tier the gate must actually use."""

_FLAGGED: dict[tuple[str, str], bool] = {
    # An ``open`` fragment is at or below every ceiling — never a leak.
    ("open", "open"): False,
    ("open", "personal"): False,
    ("open", "intimate"): False,
    ("open", "unclassified"): False,
    ("personal", "open"): True,
    ("personal", "personal"): False,
    ("personal", "intimate"): False,
    ("personal", "unclassified"): False,
    ("intimate", "open"): True,
    ("intimate", "personal"): True,
    ("intimate", "intimate"): False,
    ("intimate", "unclassified"): False,
    ("unclassified", "open"): True,
    ("unclassified", "personal"): True,
    ("unclassified", "intimate"): True,
    ("unclassified", "unclassified"): False,
    # An unvouched fragment loads as ``unclassified``, so it must be flagged
    # exactly where the explicit ``unclassified`` row is.
    ("unvouched", "open"): True,
    ("unvouched", "personal"): True,
    ("unvouched", "intimate"): True,
    ("unvouched", "unclassified"): False,
}
"""``(fragment tier, effective ceiling)`` -> whether the verbatim leak is flagged."""

_CASES = [
    pytest.param(
        fragment,
        contract,
        configured,
        id=f"frag-{fragment}-contract-{contract}-config-{configured}",
    )
    for fragment in _FRAGMENT_KEYS
    for contract in _CONTRACT_KEYS
    for configured in _CONFIGURED_KEYS
]
"""The full 5 x 4 x 5 cross product of fragment, contract and configured tier."""

# Guard the matrix against being quietly emptied: an empty ``parametrize``
# list collects zero tests and reports nothing, which is a green gate with the
# coverage silently gone. This turns that into a collection error instead.
assert len(_CASES) == 100, len(_CASES)


@pytest.fixture(scope="module")
def ceiling_matrix_vaults(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """Build one vault per configured-tier state, shared by all 100 cases.

    Module-scoped on purpose: :func:`creek.config.load_config` parses a real
    YAML file on every call (~19ms measured), and the matrix would otherwise
    build and re-parse one hundred vaults. Five is enough — the configured tier
    is the only axis that lives on disk; the contract and the cited fragment
    are both per-call arguments.

    Each vault holds all five fragment shapes under distinct ids
    (``frag-open`` … ``frag-unvouched``), each carrying its own protected
    sentence, so a case selects its subject by citing one id.

    Args:
        tmp_path_factory: Session temp-directory factory; each vault gets its
            own root so a config written for one case cannot reach another.

    Returns:
        A mapping of configured key (including ``"absent"``, which writes no
        config file at all) to that vault's root.
    """
    vaults: dict[str, Path] = {}
    for configured in _CONFIGURED_KEYS:
        vault = tmp_path_factory.mktemp(f"ceiling-{configured}")
        for name, tier in _MATRIX_FRAGMENT_TIERS.items():
            body = _MATRIX_FRAGMENT_BODIES[name]
            _seed_fragment(vault, f"frag-{name}", body, tier)
        if configured != _CONFIG_ABSENT:
            _write_vault_config(vault, configured)
        vaults[configured] = vault
    return vaults


@pytest.mark.parametrize(("fragment", "contract_tier", "configured"), _CASES)
def test_privacy_ceiling_matrix(
    ceiling_matrix_vaults: dict[str, Path],
    fragment: str,
    contract_tier: str,
    configured: str,
) -> None:
    """Every (fragment, contract, config) triple gets the verdict it should (#1354).

    One hundred cases, each a verbatim reproduction of exactly one cited
    fragment's protected sentence. The expectation is looked up in two
    hand-written tables — :data:`_EFFECTIVE_CEILING` and :data:`_FLAGGED` — so
    the test states the intended behaviour independently of how the
    implementation computes it.

    At HEAD the configured tier is ignored entirely, so every column of this
    matrix collapses onto the contract's declared tier. Twenty of the hundred
    cases then return the wrong verdict, and every one of the twenty is a
    missed leak — never a false alarm.

    Args:
        ceiling_matrix_vaults: The five pre-built vaults.
        fragment: Which seeded fragment the draft cites and reproduces.
        contract_tier: The tier the medium contract declares.
        configured: The ``author.max_reproduced_tier`` state of the vault.
    """
    vault = ceiling_matrix_vaults[configured]
    frag_id = f"frag-{fragment}"
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=[frag_id])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier(contract_tier)
    )
    expected_ceiling = _EFFECTIVE_CEILING[(contract_tier, configured)]
    expected_flagged = _FLAGGED[(fragment, expected_ceiling)]

    findings = check_privacy_compliance(
        _draft_reproducing(_MATRIX_FRAGMENT_BODIES[fragment]),
        evidence,
        vault,
        contract,
    )

    assert len(findings) == (1 if expected_flagged else 0), findings
    if expected_flagged:
        assert findings[0].dimension == "privacy_compliance"
        assert findings[0].severity == "HIGH"
        assert f"'{frag_id}'" in findings[0].message
        assert f"'{expected_ceiling}'" in findings[0].message


def test_a_configured_ceiling_is_narrowed_further_by_a_stricter_contract(
    tmp_path: Path,
) -> None:
    """A contract may still tighten a raised configured ceiling (#1354).

    The operator has permitted ``personal`` reproduction globally, but this
    medium's contract declares ``open``. The stricter of the two governs, so
    the personal fragment's protected text is still a leak, and the finding
    must attribute the ceiling to the contract rather than to the config —
    otherwise the operator debugging it goes and edits the wrong file.

    Args:
        tmp_path: Vault root for this case.
    """
    _write_vault_config(tmp_path, "personal")
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.PERSONAL)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(medium="research", default_privacy_tier=PrivacyTier.OPEN)

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "the medium contract" in findings[0].message
    assert "'open'" in findings[0].message


def test_a_configured_ceiling_widens_a_permissive_contract_only_up_to_itself(
    tmp_path: Path,
) -> None:
    """The configured tier is a cap, not a licence for everything above it (#1354).

    Configured ``personal`` against a contract declaring ``intimate``. The
    effective ceiling is ``personal``, so the two halves must disagree:

    * a ``personal`` fragment reproduced verbatim is permitted — zero findings;
    * an ``intimate`` one is not — one ``HIGH`` finding, attributed to
      ``author.max_reproduced_tier``.

    Both halves live in one test because either alone is satisfiable by a
    degenerate implementation: "always flag" passes the second, "never flag"
    passes the first.

    Args:
        tmp_path: Vault root holding both fragments.
    """
    permitted = "the personal note about a birthday dinner"
    protected = "the intimate letter I never mailed anywhere"
    _write_vault_config(tmp_path, "personal")
    _seed_fragment(tmp_path, "frag-p", permitted, PrivacyTier.PERSONAL)
    _seed_fragment(tmp_path, "frag-i", protected, PrivacyTier.INTIMATE)
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.INTIMATE
    )

    at_the_ceiling = check_privacy_compliance(
        _draft_reproducing(permitted),
        EvidenceBundle(
            claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-p"])]
        ),
        tmp_path,
        contract,
    )
    above_the_ceiling = check_privacy_compliance(
        _draft_reproducing(protected),
        EvidenceBundle(
            claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-i"])]
        ),
        tmp_path,
        contract,
    )

    assert at_the_ceiling == []
    assert [(f.dimension, f.severity) for f in above_the_ceiling] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "author.max_reproduced_tier" in above_the_ceiling[0].message
    assert "'personal'" in above_the_ceiling[0].message


def test_a_stricter_contract_still_governs_when_the_config_is_raised(
    tmp_path: Path,
) -> None:
    """``MediumContract.default_privacy_tier`` is not a dead field (#1354).

    This is the test that proves it. Everywhere else in this suite the config's
    ``open`` default is the stricter of the pair, so an implementation that
    ignored the contract entirely and used the configured tier alone would pass
    — and the fix would have replaced one single-source ceiling with another.

    Here the operator has raised the configured ceiling to ``intimate`` and the
    contract declares ``personal``. The contract is now the stricter source, so
    it must win the fold and the intimate fragment must still be flagged, with
    the message naming the contract as the source.

    Args:
        tmp_path: Vault root for this case.
    """
    _write_vault_config(tmp_path, "intimate")
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.PERSONAL
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "the medium contract" in findings[0].message
    assert "'personal'" in findings[0].message


def test_an_operator_may_deliberately_permit_intimate_reproduction(
    tmp_path: Path,
) -> None:
    """The escape hatch exists, and only the operator can reach it (#1354).

    Configured ``intimate`` and a contract declaring ``intimate``: both sources
    agree, so the intimate fragment sits at the ceiling and reproducing it is
    permitted. This is the deliberate, operator-only escape hatch — someone
    drafting from their own journal for their own eyes.

    What makes it safe is *where it lives*: ``creek_config.yaml``, which no
    template deploys with a raised value, and which a skill file cannot write.
    The same permission is unreachable from
    ``00-Creek-Meta/Skills/mediums/<medium>.MEDIUM.md``, which ``creek init``
    deploys by default and any vault-editing agent can rewrite.

    Args:
        tmp_path: Vault root for this case.
    """
    _write_vault_config(tmp_path, "intimate")
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.INTIMATE
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert findings == []


def test_a_missing_contract_still_gates_at_the_strictest_ceiling_under_a_raised_config(
    tmp_path: Path,
) -> None:
    """A raised config must not loosen the contract-less ceiling (#1310, #1354).

    ``_NO_CONTRACT_CEILING`` is ``OPEN``, and it is a *declared* ceiling, not an
    absence of one — so it enters the fold like any other and wins the ``min``
    against a configured ``intimate``. A contract-less caller therefore keeps
    the strict gate #1310 gave it, however high the operator set the config.

    Treating "no contract" as "no opinion" and deferring to the config would
    silently re-open the hole #1310 closed: ``creek author`` shipping an
    intimate fragment's protected text to stdout with ``verdict=PASS``.

    Args:
        tmp_path: Vault root for this case.
    """
    _write_vault_config(tmp_path, "intimate")
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, None
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "no medium contract" in findings[0].message
    assert "'open'" in findings[0].message


def test_a_garbage_configured_tier_fails_closed_to_open(tmp_path: Path) -> None:
    """An unparseable configured tier gates at ``open``, it does not raise (#1354).

    ``author.max_reproduced_tier: not-a-tier`` is a typo, not consent. The
    field validator must fall back to ``open`` rather than raise, because a
    ``ValidationError`` escaping a HARD leak gate turns a typo into a crashed
    review — and the pressure that follows is to catch the exception and skip
    the check, which is worse than either.

    The contract declares ``intimate``, so a config that silently *raised* the
    ceiling instead of failing closed would show up as zero findings.

    Args:
        tmp_path: Vault root for this case.
    """
    _write_vault_config(tmp_path, "not-a-tier")
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.INTIMATE
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'open'" in findings[0].message


def test_a_malformed_vault_config_fails_closed_to_open(tmp_path: Path) -> None:
    """A config file that is not valid YAML gates at ``open`` (#1354).

    A truncated or half-edited ``creek_config.yaml`` makes ``yaml.safe_load``
    raise before any field validator can run, so the fail-closed behaviour has
    to be implemented one level up from the model. The gate must still return
    findings rather than let a ``YAMLError`` escape into the reflection loop.

    Args:
        tmp_path: Vault root for this case.
    """
    meta = tmp_path / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "creek_config.yaml").write_text(
        "author: {max_reproduced_tier: [unclosed\n", encoding="utf-8"
    )
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.INTIMATE
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'open'" in findings[0].message


def test_an_unranked_contract_tier_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unranked contract tier cannot beat the configured ceiling (#509, #1354).

    ``_LEAST_RESTRICTIVE_RANK`` is the fail-closed rank for a contract tier
    missing from ``_TIER_RANK`` — a future :class:`~creek.models.PrivacyTier`
    nobody added to the table. Deleting ``PERSONAL`` from the table simulates
    exactly that, and the contract's ceiling drops to rank 0.

    The fold must not lose that property: the configured ``open`` is at the
    same rank, so the effective ceiling is still ``open`` and the intimate
    fragment is still flagged. An implementation that read the *tier* rather
    than its rank — or that let an unranked contract short-circuit the fold —
    would gate at ``personal`` here and let the leak through.

    Args:
        tmp_path: Vault root for this case.
        monkeypatch: Removes ``PERSONAL`` from ``_TIER_RANK`` for this test.
    """
    monkeypatch.delitem(_TIER_RANK, PrivacyTier.PERSONAL)
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.PERSONAL
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "'intimate'" in findings[0].message


def test_an_unranked_contract_tier_cannot_outrank_a_raised_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unranked contract tier still *wins* the fold, at rank 0 (#509, #1354).

    The twin of
    :func:`test_an_unranked_contract_tier_still_fails_closed`, and the case
    that actually detects the fallback's direction. That test leaves the
    configured ceiling at its default ``open`` (rank 0), where
    ``_LEAST_RESTRICTIVE_RANK`` and ``_MOST_RESTRICTIVE_RANK`` yield the same
    verdict — the config ties or wins either way — so flipping
    ``_ceiling_rank``'s fallback to the most-restrictive rank leaves it green.
    MEASURED: that mutation survived the entire privacy lane until this test
    existed.

    Here the operator has deliberately raised the ceiling to ``intimate``
    (rank 2) and the contract declares an unranked ``personal``. Failing closed
    means the unranked contract is treated as rank 0 — the strictest ceiling
    there is — so it beats the raised config, the effective ceiling is
    ``personal``, and the intimate leak is still flagged and still attributed
    to the contract. Under the inverted fallback the contract would score
    rank 3, the config would win at ``intimate``, and an intimate fragment
    would be reproduced verbatim with no finding at all.

    Args:
        tmp_path: Vault root for this case.
        monkeypatch: Removes ``PERSONAL`` from ``_TIER_RANK`` for this test.
    """
    monkeypatch.delitem(_TIER_RANK, PrivacyTier.PERSONAL)
    _write_vault_config(tmp_path, "intimate")
    _seed_fragment(tmp_path, "frag-a", _LEAK_SECRET, PrivacyTier.INTIMATE)
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["frag-a"])]
    )
    contract = MediumContract(
        medium="research", default_privacy_tier=PrivacyTier.PERSONAL
    )

    findings = check_privacy_compliance(
        _draft_reproducing(_LEAK_SECRET), evidence, tmp_path, contract
    )

    assert [(f.dimension, f.severity) for f in findings] == [
        ("privacy_compliance", "HIGH")
    ]
    assert "the medium contract" in findings[0].message
