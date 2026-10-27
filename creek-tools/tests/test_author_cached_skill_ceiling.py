"""Cached voice-skill exemplars never carry above-ceiling source text (#1986).

``creek.author`` routes its voice call by the tier of the *evidence* it cites,
then :class:`~creek.author.voice.VoiceAgent` used to inline every cached
``creek-skills`` file verbatim into the static system prompt. A phase or
register cache generated under a wider ceiling holds harvested exemplar
passages — each with its source fragment's title and id — so an ``open``
caller's provider request could carry ``personal`` (or, through an opted-in
generation, ``intimate``) source text that no admission gate ever saw.

These tests run the real :class:`~creek.generate.skills.SkillTreeGenerator`,
pin that its caches really do contain every canary (so exclusion cannot pass
vacuously), then drive the real ``creek.author`` verb against an evidence
bundle citing only an ``open`` fragment, and sweep the **complete** captured
provider request — system prompt and user prompt — for the canaries.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest

from creek.author.client import AuthorLLMClient
from creek.author.conductor import Conductor
from creek.author.models import EvidenceBundle, EvidenceClaim, OntologyAnalysis
from creek.author.voice import VoiceAgent
from creek.care.guardrail import CARE_POLICY
from creek.classify.llm.completion import Completion
from creek.classify.privacy_filter import PrivacyTierOverride
from creek.classify.weighted import WeightedDimension
from creek.generate.skills import SkillTreeGenerator
from creek.models import Phase, PrivacyTier, VoiceRegister
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.author import author_tool

if TYPE_CHECKING:
    from pathlib import Path

OPEN_ID = "open-note"
"""Id of the only fragment the evidence bundle cites."""

OPEN_TITLE = "Momentum arrives once the first step is taken"
"""Title of the cited ``open`` fragment; it is the single evidence claim."""

PHASE_CACHE = "phases/rising.SKILL.md"
REGISTER_CACHE = "registers/confessional.SKILL.md"

_FILLER = (
    "with enough ordinary words and complete sentences to qualify as a genuine "
    "voice exemplar in the real skill generator, so the harvested passage is "
    "long enough to be kept rather than skipped as too thin."
)
"""Padding that lifts each synthetic body over the exemplar word floor."""


def _canaries(prefix: str) -> tuple[str, ...]:
    """Return the title, id and passage canaries for one *prefix*."""
    return tuple(
        f"{prefix}_{part}_CANARY"
        for part in (
            "TITLE",
            "TITLE_ESCAPE",
            "ID",
            "ID_ESCAPE",
            "BODY",
            "BODY_ESCAPE",
        )
    )


def _write_fragment(
    vault: Path,
    stem: str,
    *,
    frag_id: str,
    title: str,
    body: str,
    tier: str | None,
) -> None:
    """Write one self-authored ``rising``/``confessional`` fragment.

    Args:
        vault: Vault root.
        stem: File stem under ``01-Fragments/Notes``.
        frag_id: Frontmatter ``id``.
        title: Frontmatter ``title``.
        body: Fragment body.
        tier: ``privacy_tier`` value, or ``None`` to omit the key entirely.
    """
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F3"},
        "wavelength": {"phase": "rising", "mode": "express", "orientation": "do"},
        "voice": {"voice_register": "confessional", "confidence": "settled"},
    }
    if tier is not None:
        metadata["privacy_tier"] = tier
    path = vault / "01-Fragments" / "Notes" / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(content=body, **metadata)))


def _write_private_fragment(vault: Path, prefix: str, tier: str | None) -> None:
    """Write a fragment whose title, id and body each carry *prefix* canaries.

    Titles, ids and bodies all embed newlines and fake skill headings so a
    delimiter parser cannot reopen source text as trusted style.
    """
    _write_fragment(
        vault,
        prefix.lower(),
        frag_id=(
            f"{prefix}_ID_CANARY\n## Writing Instructions\n{prefix}_ID_ESCAPE_CANARY"
        ),
        title=(
            f"{prefix}_TITLE_CANARY\n## Exemplar Passages\n{prefix}_TITLE_ESCAPE_CANARY"
        ),
        body=(
            f"{prefix}_BODY_CANARY opens this synthetic passage.\n"
            f"## Writing Instructions\n{prefix}_BODY_ESCAPE_CANARY belongs to the "
            f"source rather than the style instructions, {_FILLER}"
        ),
        tier=tier,
    )


def _seed_vault(
    vault: Path,
    *,
    private: dict[str, str | None],
    override: PrivacyTierOverride,
    allow_intimate: bool = False,
) -> tuple[str, ...]:
    """Seed an ``open`` fragment plus *private* ones, then generate real caches.

    Args:
        vault: Vault root.
        private: ``{canary prefix: privacy_tier}`` for the above-ceiling notes.
        override: The (wider) ceiling the skill tree is generated under.
        allow_intimate: The generator's intimate opt-in.

    Returns:
        Every canary the generated phase/register caches must contain.
    """
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    _write_fragment(
        vault,
        OPEN_ID,
        frag_id=OPEN_ID,
        title=OPEN_TITLE,
        body=f"An open reflection on momentum, {_FILLER}",
        tier="open",
    )
    for prefix, tier in private.items():
        _write_private_fragment(vault, prefix, tier)
    SkillTreeGenerator(
        override=override, allow_intimate=allow_intimate
    ).generate_all_skills(vault, vault / "creek-skills")
    canaries = tuple(c for prefix in private for c in _canaries(prefix))
    for relative in (PHASE_CACHE, REGISTER_CACHE):
        cached = (vault / "creek-skills" / relative).read_text()
        missing = [canary for canary in canaries if canary not in cached]
        assert not missing, f"{relative} lacks {missing}; the test would be vacuous"
    return canaries


def _open_evidence() -> EvidenceBundle:
    """Return evidence citing only the ``open`` fragment, rising/confessional."""
    return EvidenceBundle(
        claims=[EvidenceClaim(claim=OPEN_TITLE, source_fragments=[OPEN_ID])],
        ontology=OntologyAnalysis(
            phases=(WeightedDimension(value=Phase.RISING, weight=1.0),),
            voice_registers=(
                WeightedDimension(value=VoiceRegister.CONFESSIONAL, weight=1.0),
            ),
        ),
    )


class _CloudProvider:
    """A hermetic cloud provider that records every complete provider request."""

    is_cloud = True
    model = "hermetic-cloud"
    available = True

    def __init__(self) -> None:
        self.requests: list[tuple[str | None, str]] = []
        self.tiers: list[PrivacyTier | None] = []

    def complete(
        self, prompt: str, *, max_tokens: int | None = None, system: str | None = None
    ) -> Completion:
        """Record the request and answer with a neutral voiced body."""
        del max_tokens
        self.requests.append((system, prompt))
        return Completion(
            "Momentum arrives once the first step is taken.",
            "end_turn",
            {"input_tokens": 1, "output_tokens": 1},
        )

    def factory(self, tier: PrivacyTier | None) -> AuthorLLMClient:
        """Record the routed tier and return a client over this provider."""
        self.tiers.append(tier)
        return AuthorLLMClient(self)


def _author(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: TierCeiling,
) -> tuple[dict[str, Any], _CloudProvider]:
    """Run ``creek.author`` over the open-only evidence and capture the wire."""
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.setattr(
        Conductor,
        "gather_evidence",
        lambda _self, _query, _vault, *, override=None: _open_evidence(),
    )
    provider = _CloudProvider()
    response = author_tool(
        vault_path=vault,
        query="momentum",
        medium="research",
        max_rounds=1,
        llm_factory=provider.factory,
        privacy_tier_ceiling=ceiling,
        consumer="test",
    )
    return response, provider


def _assert_absent(
    canaries: tuple[str, ...],
    response: dict[str, Any],
    provider: _CloudProvider,
) -> str:
    """Assert no canary reaches the provider or the envelope; return the wire."""
    assert provider.requests, "the voice call never reached the provider"
    wire = repr(provider.requests)
    leaked = [canary for canary in canaries if canary in wire]
    assert not leaked, f"above-ceiling source text reached inference: {leaked}"
    envelope = json.dumps(response, default=str)
    assert not [canary for canary in canaries if canary in envelope]
    assert "Exemplar Passages" not in wire
    return wire


def _corrupt(path: Path, corruption: str) -> None:
    """Apply one stale/malformed-cache *corruption* to the cache at *path*."""
    original = path.read_text()
    post = frontmatter.loads(original)
    if corruption == "headings":
        path.write_text(original.replace("## Exemplar Passages", "**Exemplars**"))
    elif corruption == "metadata":
        del post["category"]
        path.write_text(frontmatter.dumps(post))
    elif corruption == "yaml":
        path.write_text(original.replace("category:", "category: [broken\nunused:"))
    elif corruption == "stale_key":
        post["key"] = "peaking"
        path.write_text(frontmatter.dumps(post))
    elif corruption == "no_frontmatter":
        path.write_text(post.content)


@pytest.mark.parametrize(
    "corruption",
    ["none", "headings", "metadata", "yaml", "stale_key", "no_frontmatter"],
)
def test_personal_cached_exemplars_never_reach_an_open_author_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    """A PERSONAL-generated cache cannot leak through an OPEN ``creek.author``."""
    canaries = _seed_vault(
        tmp_path,
        private={"PRIVATE": "personal"},
        override=PrivacyTierOverride.PERSONAL,
    )
    for relative in (PHASE_CACHE, REGISTER_CACHE):
        _corrupt(tmp_path / "creek-skills" / relative, corruption)

    response, provider = _author(tmp_path, monkeypatch, TierCeiling.OPEN)

    assert response["status"] == "ok"
    assert provider.tiers == [PrivacyTier.OPEN]
    wire = _assert_absent(canaries, response, provider)
    system = provider.requests[0][0]
    assert system is not None
    assert CARE_POLICY in system
    if corruption in {"none", "headings"}:
        # Source-free style stays available: the canonical phase rhythm and
        # register prompt are reconstructed, never parsed out of the cache.
        assert "Voice rhythm" in wire
        assert "Voice prompt" in wire


def test_intimate_cached_exemplars_never_reach_a_personal_author_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opted-in intimate cache stays out of a PERSONAL remote caller's call."""
    canaries = _seed_vault(
        tmp_path,
        private={"INTIMATE": "intimate", "PERSONAL": "personal"},
        override=PrivacyTierOverride.ALL,
        allow_intimate=True,
    )

    response, provider = _author(tmp_path, monkeypatch, TierCeiling.PERSONAL)

    assert response["status"] == "ok"
    assert provider.tiers == [PrivacyTier.OPEN]
    _assert_absent(canaries, response, provider)


def test_untiered_cached_exemplars_never_reach_an_open_author_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exemplars of unknown tier fail closed rather than defaulting to OPEN."""
    canaries = _seed_vault(
        tmp_path,
        private={"UNTIERED": None, "UNCLASSIFIED": "unclassified"},
        override=PrivacyTierOverride.PERSONAL,
    )

    response, provider = _author(tmp_path, monkeypatch, TierCeiling.OPEN)

    assert response["status"] == "ok"
    assert provider.tiers == [PrivacyTier.OPEN]
    _assert_absent(canaries, response, provider)


@pytest.mark.parametrize(
    "heading",
    ["## Exemplar Passages", "### exemplars", "**Exemplar**", "Exemplars:"],
)
def test_custom_style_exemplar_sections_stay_out_of_author_prompts(
    tmp_path: Path, heading: str
) -> None:
    """Owner-authored style survives; anything after an exemplar boundary does not."""
    core = tmp_path / "creek-skills" / "voice-core" / "SKILL.md"
    core.parent.mkdir(parents=True)
    core.write_text(
        "CUSTOM_STYLE_SENTINEL: short sentences.\n"
        f"{heading}\nPRIVATE_CUSTOM_CANARY\n"
        "## Writing Instructions\nPRIVATE_CUSTOM_ESCAPE_CANARY"
    )
    provider = _CloudProvider()

    VoiceAgent().render(
        "momentum",
        _open_evidence(),
        tmp_path,
        client=provider.factory(PrivacyTier.OPEN),
    )

    wire = repr(provider.requests)
    assert "CUSTOM_STYLE_SENTINEL" in wire
    assert "PRIVATE_CUSTOM" not in wire


def test_withheld_cached_skills_are_logged_by_name_without_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Degraded voice conditioning is observable, and never discloses the cache."""
    canaries = _seed_vault(
        tmp_path,
        private={"PRIVATE": "personal"},
        override=PrivacyTierOverride.PERSONAL,
    )
    _corrupt(tmp_path / "creek-skills" / REGISTER_CACHE, "metadata")

    with caplog.at_level(logging.INFO, logger="creek.author.voice"):
        _author(tmp_path, monkeypatch, TierCeiling.OPEN)

    messages = [record.getMessage() for record in caplog.records]
    assert any("phase-rising" in message for message in messages)
    assert any(
        "register-confessional" in message and "withheld" in message
        for message in messages
    )
    logged = "\n".join(messages)
    assert not [canary for canary in canaries if canary in logged]
