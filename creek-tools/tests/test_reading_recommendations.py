"""A reading recommendation must name offered material and quote its real source."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from creek.classify.llm.providers import ProviderRateLimitError
from creek.models import PrivacyTier
from creek_mcp.reading_recommendations import (
    ReadingRecommendationRequest,
    RecommendationCache,
    recommend_reading,
)
from creek_mcp.tier_ceiling import TierCeiling
from tests.v1_api_support import client, headers, seed_vault

if TYPE_CHECKING:
    from pathlib import Path


def request() -> ReadingRecommendationRequest:
    """Return synthetic course material and one synthetic journal source."""
    return ReadingRecommendationRequest.model_validate(
        {
            "catalog_version": "course-revision-1",
            "readings": [
                {
                    "content_id": 24,
                    "title": "Practicing Embodied Understanding Off the Cushion",
                    "content": (
                        "Notice resentment in your body before deciding how to respond."
                    ),
                }
            ],
            "sources": [
                {
                    "entry_id": 8,
                    "tier": "personal",
                    "content": (
                        "Yesterday I noticed resentment in my shoulders. "
                        "I want to understand it."
                    ),
                }
            ],
        }
    )


def answer(**changes: object) -> str:
    """Return an offered reading grounded in a verbatim synthetic passage."""
    return json.dumps(
        {
            "content_id": 24,
            "entry_id": 8,
            "quote": "I noticed resentment in my shoulders.",
            "reason": "You describe noticing resentment as a bodily sensation.",
            "benefit": (
                "The reading offers a way to examine that sensation before responding."
            ),
            **changes,
        }
    )


def generate(tmp_path: Path, completion: str, **kwargs: object):
    """Run the real recommendation path with an injected completion callable."""
    llm = Mock(return_value=completion)
    factory = Mock(return_value=llm)
    result = recommend_reading(
        vault=tmp_path,
        request=request(),
        ceiling=TierCeiling.PERSONAL,
        build_factory=lambda: factory,
        cache=RecommendationCache(),
        **kwargs,
    )
    return result, factory, llm


def test_reading_recommendation_is_grounded_and_uses_the_tier_router(
    tmp_path: Path,
) -> None:
    """The model selects IDs; it cannot invent a reading, URL or source quote."""
    result, factory, llm = generate(tmp_path, answer())
    assert result is not None
    assert result.content_id == 24
    assert result.entry_id == 8
    assert result.quote == "I noticed resentment in my shoulders."
    factory.assert_called_once_with(PrivacyTier.PERSONAL, max_tokens=512)
    prompt = llm.call_args.args[0]
    assert "Notice resentment in your body" in prompt
    assert "Yesterday I noticed resentment" in prompt
    assert prompt.index("<readings>") < prompt.index("<sources>")


@pytest.mark.parametrize(
    "completion",
    [
        "not JSON",
        answer(content_id=999),
        answer(content_id=True),
        answer(entry_id=999),
        answer(quote="An invented journal passage"),
        answer(quote=""),
        answer(benefit=""),
        answer(url="https://unoffered.example/reading"),
    ],
)
def test_reading_recommendation_refuses_ungrounded_answers(
    tmp_path: Path, completion: str
) -> None:
    """Malformed, invented and out-of-menu answers produce no suggestion."""
    result, _, _ = generate(tmp_path, completion)
    assert result is None


def test_reading_recommendation_checks_care_before_a_model_or_cache(
    tmp_path: Path,
) -> None:
    """An acute-distress source is never an opportunity to promote a reading."""
    result, factory, _ = generate(tmp_path, answer(), care_guard=lambda _: "acute")
    assert result is None
    factory.assert_not_called()


def test_reading_recommendation_respects_the_declared_ceiling(tmp_path: Path) -> None:
    """A personal source cannot be processed under an open-only request."""
    factory = Mock()
    result = recommend_reading(
        vault=tmp_path,
        request=request(),
        ceiling=TierCeiling.OPEN,
        build_factory=factory,
        cache=RecommendationCache(),
    )
    assert result is None
    factory.assert_not_called()


def test_reading_recommendation_cache_is_scoped_and_revision_sensitive(
    tmp_path: Path,
) -> None:
    """Repeated polling is free; another vault or changed context generates afresh."""
    llm = Mock(return_value=answer())
    factory = Mock(return_value=llm)
    cache = RecommendationCache()
    payload = request()

    def run(vault: Path, body: ReadingRecommendationRequest, version: str = "model-1"):
        return recommend_reading(
            vault=vault,
            request=body,
            ceiling=TierCeiling.PERSONAL,
            build_factory=lambda: factory,
            cache=cache,
            model_version=version,
        )

    assert run(tmp_path, payload) == run(tmp_path, payload)
    assert llm.call_count == 1
    assert run(tmp_path / "another-vault", payload) is not None
    assert llm.call_count == 2
    changed = payload.model_copy(update={"catalog_version": "course-revision-2"})
    assert run(tmp_path, changed) is not None
    assert llm.call_count == 3
    assert run(tmp_path, payload, "model-2") is not None
    assert llm.call_count == 4
    cache.invalidate(tmp_path)
    assert run(tmp_path, payload) is not None
    assert llm.call_count == 5


def test_reading_recommendation_wire_refuses_intimate_and_unbounded_material() -> None:
    """Remote context uses the same structural open/personal ceiling as letters."""
    payload = request().model_dump(mode="json")
    payload["sources"][0]["tier"] = "intimate"
    with pytest.raises(ValidationError):
        ReadingRecommendationRequest.model_validate(payload)
    payload = request().model_dump(mode="json")
    payload["sources"][0]["content"] = "x" * 2001
    with pytest.raises(ValidationError):
        ReadingRecommendationRequest.model_validate(payload)


def test_reading_recommendation_http_uses_the_real_routed_factory(
    tmp_path: Path,
) -> None:
    """The route reaches generation, validates grounding, and reuses its cache."""
    vault = seed_vault(tmp_path)
    llm = Mock(return_value=answer())
    factory = Mock(return_value=llm)
    with client(vault_path=vault, letter_llm_factory=lambda: factory) as api:
        response = api.post(
            "/v1/reading-recommendations",
            headers=headers(minor="0.25", ceiling="personal"),
            json=request().model_dump(mode="json"),
        )
        assert response.status_code == 200
        assert response.json()["recommendation"]["content_id"] == 24
        again = api.post(
            "/v1/reading-recommendations",
            headers=headers(minor="0.25", ceiling="personal"),
            json=request().model_dump(mode="json"),
        )
        assert again.json() == response.json()
    assert llm.call_count == 1


def test_reading_recommendation_includes_shared_care_policy() -> None:
    """All generated prose inherits the treatment and medication boundary."""
    from creek.care.guardrail import CARE_POLICY
    from creek_mcp.reading_recommendations import build_reading_prompt

    assert build_reading_prompt(request()).startswith(CARE_POLICY)


def test_reading_recommendation_refuses_full_prompt_over_budget(tmp_path: Path) -> None:
    """Serialization, titles and policy count toward the real pool prompt cap."""
    from creek_mcp.reading_recommendations import RecommendationPromptError

    payload = request().model_dump(mode="json")
    payload["readings"] = [
        {"content_id": index + 1, "title": "t" * 240, "content": "c" * 1500}
        for index in range(6)
    ]
    payload["sources"] = [
        {"entry_id": index + 1, "tier": "personal", "content": "s" * 1000}
        for index in range(3)
    ]
    factory = Mock()
    with pytest.raises(RecommendationPromptError):
        recommend_reading(
            vault=tmp_path,
            request=ReadingRecommendationRequest.model_validate(payload),
            ceiling=TierCeiling.PERSONAL,
            build_factory=factory,
        )
    factory.assert_not_called()


def test_reading_recommendation_does_not_return_or_cache_during_mutation(
    tmp_path: Path,
) -> None:
    """A generation overlapping withdrawal cannot resurrect a derived quote."""
    cache = RecommendationCache()

    def changed(_prompt: str) -> str:
        with cache.mutating(tmp_path):
            pass
        return answer()

    factory = Mock(return_value=changed)
    result = recommend_reading(
        vault=tmp_path,
        request=request(),
        ceiling=TierCeiling.PERSONAL,
        build_factory=lambda: factory,
        cache=cache,
    )
    assert result is None
    assert not cache._values


def test_reading_recommendation_coalesces_concurrent_polling(tmp_path: Path) -> None:
    """Two overlapping GET consumers pay for a single generation."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    cache = RecommendationCache()

    def complete(_prompt: str) -> str:
        entered.set()
        assert release.wait(timeout=5)
        return answer()

    llm = Mock(side_effect=complete)
    factory = Mock(return_value=llm)

    def run():
        return recommend_reading(
            vault=tmp_path,
            request=request(),
            ceiling=TierCeiling.PERSONAL,
            build_factory=lambda: factory,
            cache=cache,
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run)
        assert entered.wait(timeout=5)
        second = workers.submit(run)
        release.set()
        assert first.result() == second.result()
    llm.assert_called_once()


def test_reading_recommendation_missing_vault_stays_missing(tmp_path: Path) -> None:
    """Readiness checks precede audit logging and factory construction."""
    vault = tmp_path / "missing"
    factory = Mock()
    with client(vault_path=vault, letter_llm_factory=factory) as api:
        response = api.post(
            "/v1/reading-recommendations",
            headers=headers(minor="0.25", ceiling="personal"),
            json=request().model_dump(mode="json"),
        )
    assert response.status_code == 503
    assert not vault.exists()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "failure,status",
    [(RuntimeError("offline"), 503), (ProviderRateLimitError("busy"), 429)],
)
def test_reading_recommendation_provider_outage_is_not_empty(
    tmp_path: Path, failure: Exception, status: int
) -> None:
    """An unavailable model is distinguishable from no relevant reading."""
    factory = Mock(return_value=Mock(side_effect=failure))
    with client(
        vault_path=seed_vault(tmp_path), letter_llm_factory=lambda: factory
    ) as api:
        response = api.post(
            "/v1/reading-recommendations",
            headers=headers(minor="0.25", ceiling="personal"),
            json=request().model_dump(mode="json"),
        )
    assert response.status_code == status
    assert "offline" not in response.text


def test_reading_recommendation_real_pool_factory_provenance_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route through production factory and pinned pool; BYOK cannot buy an offer."""
    import yaml

    from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
    from creek.config import CreekConfig
    from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER
    from tests.pool_support import (
        PINNED_MODEL,
        FakePool,
        generation,
        identity_listing,
        pin_pool_manifest,
    )

    vault = seed_vault(tmp_path / "vault")
    data = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["generation"] = {
        **data["llm"]["default"],
        "provider": "pool",
        "model": PINNED_MODEL,
    }
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(yaml.dump(data))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    pin_pool_manifest(tmp_path, monkeypatch)
    pool = FakePool.routing(
        {"/v1/models": identity_listing(), "/v1/generate": generation(text=answer())}
    )
    pool.pin(monkeypatch)
    try:
        with client(vault_path=vault) as api:
            sent = {
                **headers(minor="0.25", ceiling="personal"),
                PROVIDER_HEADER: "openai",
                PROVIDER_KEY_HEADER: "synthetic-not-a-key",
            }
            response = api.post(
                "/v1/reading-recommendations",
                headers=sent,
                json=request().model_dump(mode="json"),
            )
            assert response.status_code == 200, response.text
            assert response.json()["inference_source"].startswith("pool:")
            again = api.post(
                "/v1/reading-recommendations",
                headers=sent,
                json=request().model_dump(mode="json"),
            )
            assert again.json() == response.json()
        assert len(pool.requests) == 2
    finally:
        pool.close()


def test_reading_recommendation_accepts_canonical_numeric_vendor_slug() -> None:
    """Provenance shares the standing vocabulary, including admitted numeric slugs."""
    from creek_mcp.api.models import attested_pool_source
    from creek_mcp.api.openapi import build_openapi
    from creek_mcp.api.reading_recommendations import ReadingRecommendationResponse

    source = attested_pool_source("01-ai")
    response = ReadingRecommendationResponse(status="empty", inference_source=source)
    assert response.inference_source == "pool:attested:01-ai"
    assert (
        "429"
        in build_openapi()["paths"]["/v1/reading-recommendations"]["post"]["responses"]
    )
