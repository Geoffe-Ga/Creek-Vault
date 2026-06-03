"""The six deterministic reflection checks for the Creek Writing Desk (#473).

Each check inspects a drafted body (and its evidence) for exactly one rubric
dimension and returns zero or more :class:`~creek.author.models.ReflectionFinding`
records. The checks are *deterministic* — no LLM — so the mutation tests in
``tests/test_reflection.py`` can assert the exact verdict and dimension a defect
produces. Citation completeness and privacy compliance are HARD gates for the
research medium; the rest are softer rubric divergences.

The privacy check reuses the vault fragment loader
(:func:`creek.vault.reader.iter_vault_fragments`) to resolve each cited
fragment's :class:`~creek.models.PrivacyTier`; the voice check reuses the
FEAT-040.x AI-style scanner (:func:`creek.generate.ai_style.scanner.scan`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import yaml

from creek.author.models import ReflectionFinding
from creek.generate.ai_style.scanner import scan
from creek.generate.grounding import scan_biographical_sentences

# Reuse the canonical INC-019 alias vocabulary (public constants) rather than
# duplicating it, so the ontological-accuracy check and the model validators
# stay in sync.
from creek.models import (
    FREQUENCY_LEGACY_ALIASES,
    MODE_LEGACY_ALIASES,
    PHASE_LEGACY_ALIASES,
    PrivacyTier,
)
from creek.vault.reader import try_load_fragment

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author.models import EvidenceBundle
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint
    from creek.generate.grounding import EmbeddingFn
    from creek.models import MediumContract

#: Privacy tiers from least to most restrictive; index = restrictiveness rank.
#: ``UNCLASSIFIED`` is treated as the most restrictive (fail closed), matching
#: :mod:`creek.classify.privacy_filter`.
_TIER_RANK: dict[PrivacyTier, int] = {
    PrivacyTier.OPEN: 0,
    PrivacyTier.PERSONAL: 1,
    PrivacyTier.INTIMATE: 2,
    PrivacyTier.UNCLASSIFIED: 3,
}

#: Legacy alias keys whose presence in a body signals non-canonical taxonomy
#: (INC-019). Maps each deprecated alias to its canonical replacement.
_LEGACY_ALIASES: dict[str, str] = (
    PHASE_LEGACY_ALIASES | MODE_LEGACY_ALIASES | FREQUENCY_LEGACY_ALIASES
)


def check_citation_completeness(evidence: EvidenceBundle) -> list[ReflectionFinding]:
    """Flag any claim that asserts something without a source fragment (HARD).

    A research draft may make no unsupported assertion: a claim whose
    ``source_fragments`` is empty is an uncited claim and fails the hard
    citation gate.

    Args:
        evidence: The evidence the draft was rendered from.

    Returns:
        One ``HIGH`` finding per uncited claim, dimension
        ``"citation_completeness"``.
    """
    return [
        ReflectionFinding(
            dimension="citation_completeness",
            severity="HIGH",
            message=f"Uncited claim has no source fragments: {claim.claim!r}.",
        )
        for claim in evidence.claims
        if not claim.source_fragments
    ]


def check_biographical_grounding(
    body: str,
    evidence: EvidenceBundle,
    *,
    embedding_fn: EmbeddingFn | None,
    grounding_lower: float,
    voice_core: str | None = None,
) -> list[ReflectionFinding]:
    """Flag invented first-person biographical claims in the body (HARD).

    A research/desk draft must not assert a biographical fact about the owner
    (an event, an upbringing, a past state) that no source supports — e.g.
    amplifying a brief's "the LDS Christ I was handed" into a childhood the
    corpus never mentions (issue #515). Each grounded claim's text plus the
    optional voice-core brief form the source corpus; a first-person
    biographical sentence whose cosine similarity falls below *grounding_lower*
    against all of them is fabrication.

    The check is dormant — and returns no findings — when *embedding_fn* is
    ``None``, mirroring how :func:`check_voice_fidelity` stays dormant without
    a fingerprint. This keeps the deterministic reflection node free of the
    sentence-transformer import unless a caller wires the embedder in.

    Args:
        body: The drafted prose under review.
        evidence: The evidence the draft was rendered from; each claim's text
            is a legitimate grounding source.
        embedding_fn: Embedding callable, or ``None`` to skip the check.
        grounding_lower: Cosine floor a biographical sentence must clear
            against some source to count as grounded.
        voice_core: The voice-core brief text, treated as tone guidance the
            claim may also trace to, or ``None``.

    Returns:
        One ``HIGH`` ``biographical_grounding`` finding per ungrounded
        biographical sentence.
    """
    if embedding_fn is None:
        return []
    source_texts = [claim.claim for claim in evidence.claims]
    if voice_core:
        source_texts.append(voice_core)
    return [
        ReflectionFinding(
            dimension="biographical_grounding",
            severity="HIGH",
            message=(
                "First-person biographical claim is not grounded in any "
                f"source (similarity {finding.max_similarity:.2f} < "
                f"{grounding_lower:.2f}): {finding.sentence!r}."
            ),
        )
        for finding in scan_biographical_sentences(
            body,
            source_texts=source_texts,
            embedding_fn=embedding_fn,
            threshold=grounding_lower,
        )
    ]


def _resolve_cited_tiers(
    evidence: EvidenceBundle,
    vault: Path,
) -> dict[str, tuple[PrivacyTier, str]]:
    """Map each cited fragment id to its ``(privacy_tier, body)`` from *vault*.

    Args:
        evidence: The evidence whose cited fragment ids are resolved.
        vault: The vault root to load fragments from.

    Returns:
        A mapping of cited fragment id to its tier and stored body. Fragments
        not found in the vault are omitted.

    The walk is lazy — ``rglob`` is iterated directly (not materialised) and
    exits as soon as every cited fragment is resolved, so a draft citing a
    handful of fragments stops early instead of parsing the whole vault on every
    ``review`` call inside the retry loop. Iteration order is therefore the
    filesystem's; that is irrelevant here since results are keyed by fragment id.
    """
    cited = set(evidence.all_source_fragments())
    resolved: dict[str, tuple[PrivacyTier, str]] = {}
    fragments_root = vault / "01-Fragments"
    if not cited or not fragments_root.is_dir():
        return resolved
    for md_file in fragments_root.rglob("*.md"):
        try:
            record = try_load_fragment(md_file)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if record is None:
            continue
        fragment, body, _raw = record
        if fragment.id in cited:
            resolved[fragment.id] = (fragment.privacy_tier, body)
            if len(resolved) == len(cited):
                break  # every cited fragment found — stop scanning the vault
    return resolved


def check_privacy_compliance(
    body: str,
    evidence: EvidenceBundle,
    vault: Path | None,
    contract: MediumContract | None,
) -> list[ReflectionFinding]:
    """Flag a draft that leaks text above the contract's privacy tier (HARD).

    For each cited fragment whose resolved tier is *more restrictive* than the
    contract's ``default_privacy_tier``, the draft breaches privacy iff the body
    still contains that fragment's protected text. The check is deterministic;
    when *vault* or *contract* is ``None`` there is nothing to resolve against,
    so no finding is raised. A cited fragment id with no matching file in the
    vault is unresolvable (its tier cannot be known) and is skipped rather than
    guessed — the hard citation gate still requires every claim to carry an id.

    Args:
        body: The drafted prose under review.
        evidence: The evidence the draft was rendered from.
        vault: The vault root, or ``None`` to skip the check.
        contract: The medium contract whose ``default_privacy_tier`` is the
            ceiling, or ``None`` to skip.

    Returns:
        One ``HIGH`` finding per leaked over-tier fragment, dimension
        ``"privacy_compliance"``.
    """
    if vault is None or contract is None:
        return []
    # Pydantic stores tiers as the underlying str; coerce back to the enum so
    # the ordering lookup and display are well-defined.
    ceiling_tier = PrivacyTier(contract.default_privacy_tier)
    ceiling = _TIER_RANK[ceiling_tier]
    findings: list[ReflectionFinding] = []
    for frag_id, (raw_tier, frag_body) in _resolve_cited_tiers(evidence, vault).items():
        tier = PrivacyTier(raw_tier)
        protected = frag_body.strip()
        # Deterministic scope: this catches verbatim leakage of the protected
        # body. A paraphrase of over-tier content is NOT caught here — that
        # needs the semantic LLM judge tracked under #474.
        if _TIER_RANK[tier] > ceiling and protected and protected in body:
            findings.append(
                ReflectionFinding(
                    dimension="privacy_compliance",
                    severity="HIGH",
                    message=(
                        f"Cited fragment {frag_id!r} is {tier.value!r} "
                        f"(above the {ceiling_tier.value!r} "
                        "default) yet its protected text appears in the draft."
                    ),
                )
            )
    return findings


def check_ontological_accuracy(body: str) -> list[ReflectionFinding]:
    """Flag legacy (non-canonical) taxonomy terms in the body (INC-019).

    A word-boundary regex match of any deprecated phase/mode/frequency alias
    key in the body raises a ``MID`` finding naming the canonical replacement.

    Caveat: a few alias keys are ordinary English words (e.g. ``"origins"``),
    so this word-boundary match can false-positive on non-ontological prose. It
    is a ``MID`` (soft) finding, so a false hit costs at most one revise round,
    not a hard block; semantic disambiguation is deferred to #474.

    Args:
        body: The drafted prose under review.

    Returns:
        One ``MID`` finding per legacy alias used, dimension
        ``"ontological_accuracy"``.
    """
    return [
        ReflectionFinding(
            dimension="ontological_accuracy",
            severity="MID",
            message=(
                f"Legacy taxonomy term {alias!r} used; the canonical "
                f"term is {canonical!r} (INC-019)."
            ),
        )
        for alias, canonical in _LEGACY_ALIASES.items()
        if re.search(rf"\b{re.escape(alias)}\b", body, flags=re.IGNORECASE)
    ]


def _paradox_is_preserved(body: str, description: str) -> bool:
    """Return whether *body* keeps the tension of a paradox *description*.

    Deterministic heuristic: the paradox is preserved iff the body engages the
    paradox's own vocabulary in a both-sides way — either (a) it carries a
    contrast/tension cue (``but``, ``yet``, ``however``, ``while``,
    ``although``, ``tension``, ``paradox``, ``both``) AND mentions at least one
    significant (>=4-char) word of the paradox description, or (b) it mentions
    every significant word of the description. Requiring topical overlap
    alongside the contrast cue closes the bare-conjunction bypass — a stray
    "but" elsewhere in the draft no longer counts as preserving *this* tension.

    This is a deterministic approximation; genuine semantic both-sides
    detection is deferred to the LLM judge (#474). It can still miss a paraphrase
    that reframes the tension without the description's words.

    Args:
        body: The drafted prose under review.
        description: The paradox's neutral one-sentence description.

    Returns:
        ``True`` when the tension is preserved, ``False`` when flattened.
    """
    lowered = body.lower()
    key_terms = re.findall(r"[a-z]{4,}", description.lower())
    if not key_terms:
        return True  # no vocabulary to check against — don't flag spuriously
    mentions_topic = any(term in lowered for term in key_terms)
    contrast_cues = (
        "but",
        "yet",
        "however",
        "while",
        "although",
        "tension",
        "paradox",
        "both",
    )
    has_contrast = any(re.search(rf"\b{cue}\b", lowered) for cue in contrast_cues)
    if has_contrast and mentions_topic:
        return True
    return all(term in lowered for term in key_terms)


def check_paradox_preservation(
    body: str,
    evidence: EvidenceBundle,
) -> list[ReflectionFinding]:
    """Flag a surfaced paradox the draft flattened to one side.

    The Ontology specialist *names* tensions rather than resolving them; the
    draft must keep both sides visible. For each
    :class:`~creek.author.models.OntologyParadox`, a flattened tension (per
    :func:`_paradox_is_preserved`) raises a ``MID`` finding.

    Args:
        body: The drafted prose under review.
        evidence: The evidence whose ``ontology.paradoxes`` are checked.

    Returns:
        One ``MID`` finding per flattened paradox, dimension
        ``"paradox_preservation"``.
    """
    if evidence.ontology is None:
        return []
    return [
        ReflectionFinding(
            dimension="paradox_preservation",
            severity="MID",
            message=(
                f"Paradox flattened — the draft surfaces only one side "
                f"of: {paradox.description!r}."
            ),
        )
        for paradox in evidence.ontology.paradoxes
        if not _paradox_is_preserved(body, paradox.description)
    ]


def check_attribution_correctness(
    body: str,
    evidence: EvidenceBundle,
) -> list[ReflectionFinding]:
    """Flag a borrowed idea presented as the owner's own.

    A claim carrying an ``author_slug`` (drawn from ``11-Other-Authors/``)
    must be attributed in the body: if neither the slug nor its de-slugged
    full name appears (on a word boundary), the borrowed idea is uncredited.

    Limitation: only the slug and its full de-slugged form are recognised — an
    attribution by partial name alone (e.g. surname-only "Ravikant" for
    ``naval-ravikant``) is not matched and would still flag. Tightening this to
    accept partial-name attribution is a semantic concern for the LLM judge
    (#474); the deterministic check intentionally errs toward demanding the full
    name a borrowed-author folder is keyed on.

    Args:
        body: The drafted prose under review.
        evidence: The evidence whose attributed claims are checked.

    Returns:
        One ``HIGH`` finding per unattributed borrowed claim, dimension
        ``"attribution_correctness"``.
    """
    lowered = body.lower()
    findings: list[ReflectionFinding] = []
    for claim in evidence.claims:
        slug = claim.author_slug
        if slug is None:
            continue
        # Word-boundary match (not substring) so a slug appearing inside a
        # longer word — e.g. "naval" within "navalny" — is not mistaken for an
        # attribution, mirroring check_ontological_accuracy.
        name = slug.replace("-", " ").lower()
        attributed = any(
            re.search(rf"\b{re.escape(token)}\b", lowered)
            for token in (slug.lower(), name)
        )
        if not attributed:
            findings.append(
                ReflectionFinding(
                    dimension="attribution_correctness",
                    severity="HIGH",
                    message=(
                        f"Claim borrowed from {slug!r} is presented without "
                        f"attribution: {claim.claim!r}."
                    ),
                )
            )
    return findings


def check_voice_fidelity(
    body: str,
    fingerprint: VoiceFingerprint | None,
    config: AIStyleConfig | None,
) -> list[ReflectionFinding]:
    """Flag voice divergences via the FEAT-040.x AI-style scanner.

    Reuses :func:`creek.generate.ai_style.scanner.scan`: each
    :class:`~creek.generate.ai_style.model.Finding` becomes a ``MID``
    ``voice_fidelity`` finding, and a ``voice_distance`` over the config's
    ``voice_distance_upper`` bound adds one ``HIGH`` finding. When no
    fingerprint or config is available the voice cannot be measured, so no
    finding is raised.

    Args:
        body: The drafted prose under review.
        fingerprint: The owner's voice fingerprint, or ``None`` to skip.
        config: The AI-style configuration, or ``None`` to skip.

    Returns:
        ``voice_fidelity`` findings mapped from the scan report.
    """
    if fingerprint is None or config is None:
        return []
    report = scan(body, fingerprint=fingerprint, config=config)
    findings = [
        ReflectionFinding(
            dimension="voice_fidelity",
            severity="MID",
            message=finding.message,
        )
        for finding in report.findings
    ]
    if report.voice_distance > config.voice_distance_upper:
        findings.append(
            ReflectionFinding(
                dimension="voice_fidelity",
                severity="HIGH",
                message=(
                    f"Voice distance {report.voice_distance:.2f} exceeds the "
                    f"configured ceiling {config.voice_distance_upper:.2f}."
                ),
            )
        )
    return findings
