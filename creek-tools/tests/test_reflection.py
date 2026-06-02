"""Mutation-grade tests for the deterministic Reflection node (#473).

Each rubric dimension gets a test that injects *exactly* that defect and asserts
the EXACT verdict plus a finding for the right dimension — not merely "not
PASS". A clean draft passes; a no-draft input escalates; and the Conductor
escalates rather than ships when the round budget is exhausted on REVISE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author.checks import check_voice_fidelity
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
