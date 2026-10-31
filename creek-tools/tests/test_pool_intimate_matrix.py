"""INTIMATE, untiered and unknown-tier content never dials the pool (#2008).

ADR-0016 Decision 5: INTIMATE content never reaches the shared inference
pool, by any route — the ``default`` rescue path included. Content whose tier
is ``None`` or a value outside :class:`~creek.models.PrivacyTier` is not
evidence of safety, so it is held to the same rule (#1948).

The matrix is ``tier x source x path``, 3 x 2 x 3 = 18 cells, every one
asserting on a socket spy that **no dial left for anywhere but the loopback
local runtime**, plus the routing outcome the cell gets:

- **tier** — ``PrivacyTier.INTIMATE``, ``None`` (an untiered fragment), and
  ``"secret"`` (an unrecognised value, which is how a bad frontmatter value or
  a future enum member would arrive).
- **source** — ``fresh``: a fragment body handed straight to the routed
  provider; ``cached``: the Writing Desk's voice call, rendered by the real
  :class:`~creek.author.voice.VoiceAgent` over a ``creek-skills`` cache the
  real :class:`~creek.generate.skills.SkillTreeGenerator` built.
- **path** — ``first``: one call; ``retry``: the first attempt is
  rate-limited and attempt 2 onward run (the classifier's own retry loop for
  ``fresh``; a re-resolved client in a freshly declared pool scope for
  ``cached``, which is the worst case — a retry that re-resolves the stage and
  rebuilds the scope); ``default_rescue``: ``default`` is itself the pool, so
  there is no local backend to rescue to and the router must refuse.

No cell is inapplicable. Assertions are content-free: tier values, stage and
provider names, exception types and dial destinations only.

The pool's own scope refusal is pinned separately
(:func:`test_the_pool_scope_still_refuses_a_tier_it_cannot_name`): it is
defence in depth behind the router, and must keep holding if a caller ever
reaches a pool provider without going through the router at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import frontmatter
import httpx
import pytest

from creek.author.client import VOICE_DRAFTER_ROLE, AuthorLLMClient
from creek.author.models import EvidenceBundle, EvidenceClaim, OntologyAnalysis
from creek.author.voice import VoiceAgent
from creek.classify.llm.orchestrator import LLMClassifier
from creek.classify.llm.pool_boundary import (
    POOL_URL_ENV,
    PoolTierUndeclaredError,
    declared_pool_request,
)
from creek.classify.llm.providers import (
    Completion,
    PoolProvider,
    ProviderRateLimitError,
    build_provider,
)
from creek.classify.llm.router import IntimateRoutingError, ModelRouter
from creek.classify.privacy_filter import PrivacyTierOverride
from creek.classify.weighted import WeightedDimension
from creek.config import LLMConfig, LLMRoutingConfig
from creek.generate.skills import SkillTreeGenerator
from creek.models import (
    Fragment,
    FragmentSource,
    Phase,
    PrivacyTier,
    SourcePlatform,
    VoiceRegister,
)
from tests.pool_support import SocketSpy

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.llm.base import LLMProvider

_POOL_HOST: Final[str] = "pool.vendor.test"
"""The pinned pool destination; a dial here is the failure this suite hunts."""

_LOOPBACK_HOST: Final[str] = "127.0.0.1"
"""The only host a redirected (local) call may aim at."""

_LOOPBACK_URL: Final[str] = f"http://{_LOOPBACK_HOST}:11434"
"""The local Ollama runtime the router redirects to."""

_PROXY_ENV: Final[tuple[str, ...]] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
"""Proxy variables hidden so every dial is aimed at its real destination."""

_UNKNOWN_TIER: Final[str] = "secret"
"""A tier value :class:`PrivacyTier` cannot name."""

_TIERS: Final[tuple[object, ...]] = (PrivacyTier.INTIMATE, None, _UNKNOWN_TIER)
_SOURCES: Final[tuple[str, ...]] = ("fresh", "cached")
_PATHS: Final[tuple[str, ...]] = ("first", "retry", "default_rescue")

_FRESH_BODY: Final[str] = "A synthetic placeholder body; not real journal content."
"""The ``fresh`` source's content. Never asserted on."""

_FILLER: Final[str] = (
    "with enough ordinary words and complete sentences to qualify as a genuine "
    "voice exemplar in the real skill generator, so the harvested passage is "
    "long enough to be kept rather than skipped as too thin."
)
"""Padding that lifts each synthetic body over the exemplar word floor."""

_REFUSED: Final[tuple[type[Exception], ...]] = (RuntimeError, httpx.HTTPError)
"""What a refused call raises: a pool refusal, or the spy refusing loopback."""

_RETRIED_ATTEMPTS: Final[int] = 2
"""A retry cell must have run attempt 2, after the rate-limited first."""

_CITED_ID: Final[str] = "matrix-open-note"
_CITED_TITLE: Final[str] = "A synthetic open note"


def _pool() -> LLMConfig:
    """Return a stage routed to the shared inference pool."""
    return LLMConfig(provider="pool", model="creek-test-model:q4")


def _local() -> LLMConfig:
    """Return a stage routed to the loopback Ollama runtime."""
    return LLMConfig(provider="ollama", ollama_url=_LOOPBACK_URL)


def _router(path: str) -> ModelRouter:
    """Return a router whose every content stage is the pool.

    Args:
        path: The matrix path. ``default_rescue`` puts ``default`` on the pool
            too, so no local backend exists to redirect to.

    Returns:
        The router the cell resolves through.
    """
    default = _pool() if path == "default_rescue" else _local()
    return ModelRouter(
        LLMRoutingConfig(
            default=default,
            classification=_pool(),
            generation=_pool(),
            writing_desk={VOICE_DRAFTER_ROLE: _pool()},
        )
    )


class _Routed:
    """Record every provider the real factory builds, and wrap it.

    The wrapper delegates to the real provider, so a pool provider that is
    built really does try to dial; it only adds an attempt count and, for the
    ``retry`` path, a rate-limited first attempt.

    Attributes:
        built: The ``LLMConfig.provider`` of each provider built, in order.
        attempts: How many completion calls were made across all of them.
    """

    def __init__(self, *, rate_limit_first: bool) -> None:
        """Start with nothing built.

        Args:
            rate_limit_first: Whether attempt 1 raises a rate limit.
        """
        self.built: list[str] = []
        self.attempts = 0
        self._rate_limit_first = rate_limit_first

    def build(self, config: LLMConfig) -> LLMProvider:
        """Build *config*'s real provider and wrap it.

        Args:
            config: The routed stage config.

        Returns:
            The wrapped provider.
        """
        self.built.append(config.provider)
        return _Delegate(build_provider(config), self)

    def next_attempt(self) -> None:
        """Count one completion attempt, rate-limiting the first if asked.

        Raises:
            ProviderRateLimitError: On attempt 1 of a ``retry`` cell.
        """
        self.attempts += 1
        if self._rate_limit_first and self.attempts == 1:
            msg = "rate-limited"
            raise ProviderRateLimitError(msg, retry_after=0.0)


class _Delegate:
    """A provider that counts each attempt, then forwards to the real one."""

    is_cloud = False
    available = True

    def __init__(self, real: LLMProvider, owner: _Routed) -> None:
        """Wrap *real*, reporting attempts to *owner*.

        Args:
            real: The provider the real factory built.
            owner: The recorder counting attempts.
        """
        self._real = real
        self._owner = owner
        self.model = real.model

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Count the attempt, then call the real provider.

        Args:
            prompt: The prompt.
            max_tokens: Optional output ceiling.
            system: Optional system prompt.

        Returns:
            The real provider's completion.
        """
        self._owner.next_attempt()
        return self._real.complete(prompt, max_tokens=max_tokens, system=system)


def _write_note(vault: Path, stem: str, *, tier: str | None) -> None:
    """Write one synthetic self-authored rising/confessional fragment.

    Args:
        vault: Vault root.
        stem: File stem, also the fragment id.
        tier: ``privacy_tier`` value, or ``None`` to omit the key.
    """
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": stem,
        "title": f"Synthetic {stem}",
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F3"},
        "wavelength": {"phase": "rising", "mode": "express", "orientation": "do"},
        "voice": {"voice_register": "confessional", "confidence": "settled"},
    }
    if tier is not None:
        metadata["privacy_tier"] = tier
    path = vault / "01-Fragments" / "Notes" / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"A synthetic passage for the {stem} placeholder, {_FILLER}"
    path.write_text(frontmatter.dumps(frontmatter.Post(content=body, **metadata)))


def _cached_vault(vault: Path) -> Path:
    """Seed synthetic notes of every tier and build the real skill cache.

    Args:
        vault: Vault root.

    Returns:
        The vault, carrying a generated ``creek-skills`` tree.
    """
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    _write_note(vault, _CITED_ID, tier="open")
    _write_note(vault, "matrix-intimate-note", tier="intimate")
    _write_note(vault, "matrix-untiered-note", tier=None)
    SkillTreeGenerator(
        override=PrivacyTierOverride.ALL, allow_intimate=True
    ).generate_all_skills(vault, vault / "creek-skills")
    cached = vault / "creek-skills" / "phases" / "rising.SKILL.md"
    assert cached.is_file(), "no voice-skill cache was built; the row is vacuous"
    return vault


def _evidence() -> EvidenceBundle:
    """Return evidence citing the synthetic open note, rising/confessional."""
    return EvidenceBundle(
        claims=[EvidenceClaim(claim=_CITED_TITLE, source_fragments=[_CITED_ID])],
        ontology=OntologyAnalysis(
            phases=(WeightedDimension(value=Phase.RISING, weight=1.0),),
            voice_registers=(
                WeightedDimension(value=VoiceRegister.CONFESSIONAL, weight=1.0),
            ),
        ),
    )


def _fragment() -> Fragment:
    """Return a minimal synthetic fragment for the classifier's retry loop."""
    return Fragment(
        id="frag-000000002008",
        title="Synthetic matrix fragment",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
    )


def _run_fresh(router: ModelRouter, tier: Any, path: str, routed: _Routed) -> None:
    """Drive a fragment body through the routed provider.

    Args:
        router: The cell's router.
        tier: The cell's tier.
        path: ``first`` (one call) or ``retry`` (the classifier's loop).
        routed: The recorder wrapping the real factory.
    """
    if path == "retry":
        cfg = router.resolve("classification", tier)
        classifier = LLMClassifier(
            config=cfg, provider=routed.build(cfg), available=True
        )
        classifier.RETRY_DELAY = 0.0
        with declared_pool_request(tier):
            result = classifier.classify_with_reasoning(_fragment(), _FRESH_BODY)
        assert not result.succeeded
        return
    provider = router.provider_for("generation", tier)
    with declared_pool_request(tier), pytest.raises(_REFUSED):
        provider.complete(_FRESH_BODY)


def _run_cached(
    router: ModelRouter, tier: Any, path: str, routed: _Routed, vault: Path
) -> None:
    """Drive the Writing Desk's voice call over the cached skill tree.

    Each attempt re-resolves the voice client and declares a fresh pool scope
    — the worst case for a retry, which must still never reach the pool.

    Args:
        router: The cell's router.
        tier: The cell's tier.
        path: ``first`` (one attempt) or ``retry`` (two).
        routed: The recorder wrapping the real factory.
        vault: The vault carrying the generated ``creek-skills`` cache.
    """
    for _attempt in range(_RETRIED_ATTEMPTS if path == "retry" else 1):
        client = AuthorLLMClient.for_role(router, VOICE_DRAFTER_ROLE, tier=tier)
        with declared_pool_request(tier), pytest.raises(_REFUSED):
            VoiceAgent().render("momentum", _evidence(), vault, client=client)


@pytest.fixture(autouse=True)
def _pinned_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a pool destination so a leak would have somewhere to go."""
    monkeypatch.setenv(POOL_URL_ENV, f"https://{_POOL_HOST}")
    for name in _PROXY_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize("tier", _TIERS, ids=["intimate", "untiered", "unknown"])
def test_intimate_or_untiered_content_never_dials_the_pool(
    tier: object,
    source: str,
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero pool dials in every cell; a local redirect or a loud refusal."""
    routed = _Routed(rate_limit_first=path == "retry")
    monkeypatch.setattr("creek.classify.llm.router.build_provider", routed.build)
    monkeypatch.setattr("creek.author.client.build_provider", routed.build)
    vault = _cached_vault(tmp_path) if source == "cached" else tmp_path
    spy = SocketSpy(monkeypatch)
    router = _router(path)

    if path == "default_rescue":
        with pytest.raises(IntimateRoutingError):
            if source == "fresh":
                router.provider_for("generation", tier)
            else:
                AuthorLLMClient.for_role(router, VOICE_DRAFTER_ROLE, tier=tier)
        assert routed.built == []
    elif source == "fresh":
        _run_fresh(router, tier, path, routed)
    else:
        _run_cached(router, tier, path, routed, vault)

    assert _POOL_HOST not in spy.hosts
    assert set(spy.hosts) <= {_LOOPBACK_HOST}
    assert "pool" not in routed.built
    if path == "retry":
        assert routed.attempts >= _RETRIED_ATTEMPTS
    if path != "default_rescue":
        assert routed.built
        assert set(routed.built) == {"ollama"}


@pytest.mark.parametrize("tier", _TIERS, ids=["intimate", "untiered", "unknown"])
def test_the_pool_scope_still_refuses_a_tier_it_cannot_name(
    tier: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind the router, the scope refuses before any dial (defence in depth)."""
    spy = SocketSpy(monkeypatch)
    provider = PoolProvider(_pool())

    with declared_pool_request(tier), pytest.raises(PoolTierUndeclaredError):
        provider.complete(_FRESH_BODY)

    assert spy.destinations == []
