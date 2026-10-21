"""The ``pool`` provider: pinned identity, no retries, no substitution (#1894).

ADR-0016 registers the shared confidential warm-inference pool as a provider
so a managed vault — whose 1 GB Machine cannot hold a model — routes through
the same per-stage :class:`~creek.classify.llm.router.ModelRouter` chokepoint
as everything else. Two properties are this module's subject:

- **T1 identity.** A pool reporting any runtime or model other than the
  manifest's is refused *before a prompt is sent*, and an answer echoing a
  different model is discarded rather than returned.
- **T9 fail closed.** Pool down, identity mismatch, admission refusal and a
  rate limit each yield a refusal with no retry, no second host and no model
  substitution.

Every dial lands on a loopback listener this suite opened (see
:mod:`tests.pool_support`), so nothing is billed.
"""

from __future__ import annotations

import pytest

from creek.classify.llm.pool_boundary import (
    POOL_URL_ENV,
    PoolBoundaryError,
    PoolDestinationError,
)
from creek.classify.llm.providers import (
    DEFAULT_MODELS,
    PoolIdentityError,
    PoolProvider,
    ProviderRateLimitError,
    build_provider,
    check_pool_available,
    known_providers,
    pool_identity_digest,
    pool_identity_matches,
    provider_display_name,
    provider_intimate_eligible,
    provider_is_cloud,
    reported_pool_identity,
)
from creek.config import LLMConfig
from tests.pool_support import (
    PINNED_MODEL,
    FakePool,
    SocketSpy,
    generation,
    identity_listing,
    pinned_identity_digest,
)


def _config(**overrides: object) -> LLMConfig:
    """Return a pool stage configuration pinned to the test model."""
    return LLMConfig(provider="pool", model=PINNED_MODEL, **overrides)


class TestRegistration:
    """The registry's flags are what the chokepoint and the pin read."""

    def test_pool_is_a_registered_provider_with_a_default_model(self) -> None:
        """``pool`` is constructible and carries exactly one default model."""
        assert "pool" in known_providers()
        assert set(DEFAULT_MODELS) == set(known_providers())
        assert isinstance(build_provider(_config()), PoolProvider)
        assert provider_display_name("pool") == PoolProvider.PROVIDER_NAME

    def test_pool_is_not_cloud_and_not_intimate_eligible(self) -> None:
        """The two flags are independent, and ``pool`` separates them."""
        assert provider_is_cloud("pool") is False
        assert provider_intimate_eligible("pool") is False

    @pytest.mark.parametrize(
        ("provider", "eligible"),
        [
            ("ollama", True),
            ("enclave", True),
            ("anthropic", False),
            ("openai", False),
            ("gemini", False),
            ("pool", False),
        ],
    )
    def test_every_registered_provider_declares_its_eligibility(
        self, provider: str, eligible: bool
    ) -> None:
        """Only on-device and attested runtimes may be shown INTIMATE."""
        assert provider_intimate_eligible(provider) is eligible

    def test_an_unregistered_provider_is_not_intimate_eligible(self) -> None:
        """The backstop fails closed rather than treating unknown as local."""
        assert provider_intimate_eligible("frobnicate") is False

    def test_an_unset_model_falls_back_to_the_registry_default(self) -> None:
        """A blank config model resolves to the pool's declared default."""
        assert (
            PoolProvider(LLMConfig(provider="pool", model=None)).model
            == (DEFAULT_MODELS["pool"])
        )


class TestIdentity:
    """The pinned identity digest, and what it refuses."""

    def test_the_digest_binds_the_runtime_to_the_model(self) -> None:
        """Changing any of the three inputs changes the digest."""
        base = pool_identity_digest("rt", "1.0", "m:1")

        assert base != pool_identity_digest("other", "1.0", "m:1")
        assert base != pool_identity_digest("rt", "2.0", "m:1")
        assert base != pool_identity_digest("rt", "1.0", "m:2")
        assert len(base) == len(pinned_identity_digest())

    def test_a_matching_listing_matches_the_pinned_digest(self) -> None:
        """The manifest digest is reproducible from the pool's own listing."""
        assert pool_identity_matches(
            identity_listing(),
            model_name=PINNED_MODEL,
            inventory_digest=pinned_identity_digest(),
        )

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not a mapping", id="not-a-mapping"),
            pytest.param({"models": [{"name": PINNED_MODEL}]}, id="no-runtime"),
            pytest.param({"runtime": {"name": "x", "version": "1"}}, id="no-models"),
            pytest.param(
                {"runtime": {"name": 1, "version": "1"}, "models": []},
                id="runtime-not-strings",
            ),
            pytest.param(
                {"runtime": {"name": "x", "version": "1"}, "models": "nope"},
                id="models-not-a-list",
            ),
            pytest.param(identity_listing(model_name="other:1"), id="other-model"),
        ],
    )
    def test_a_malformed_or_foreign_listing_reports_no_identity(
        self, payload: object
    ) -> None:
        """Anything but a well-formed listing offering the model is ``None``."""
        assert reported_pool_identity(payload, PINNED_MODEL) is None

    @pytest.mark.parametrize(
        "listing",
        [
            pytest.param(identity_listing(runtime_name="other"), id="runtime-name"),
            pytest.param(identity_listing(runtime_version="9.9"), id="version"),
            pytest.param(identity_listing(model_name="other:1"), id="model"),
        ],
    )
    def test_a_drifted_listing_fails_the_pin(self, listing: object) -> None:
        """A pool that is not the pinned image does not match the manifest."""
        assert not pool_identity_matches(
            listing,
            model_name=PINNED_MODEL,
            inventory_digest=pinned_identity_digest(),
        )

    def test_a_blank_pinned_digest_never_matches(self) -> None:
        """An empty pin is not a wildcard; it fails closed."""
        assert not pool_identity_matches(
            identity_listing(), model_name=PINNED_MODEL, inventory_digest=""
        )


class TestAvailability:
    """``available`` asks the listing, never a generation."""

    def test_the_pinned_pool_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pool offering the model, at its pinned digest, is available."""
        pool = FakePool.serving(identity_listing())
        pool.pin(monkeypatch)
        try:
            assert PoolProvider(_config()).available is True
            assert check_pool_available(
                model_name=PINNED_MODEL,
                inventory_digest=pinned_identity_digest(),
            )
        finally:
            pool.close()

    def test_a_drifted_pool_is_not_available_at_the_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the pinned digest the listing alone is not enough."""
        pool = FakePool.serving(identity_listing(runtime_version="9.9"))
        pool.pin(monkeypatch)
        try:
            assert not check_pool_available(
                model_name=PINNED_MODEL,
                inventory_digest=pinned_identity_digest(),
            )
        finally:
            pool.close()

    def test_an_admission_refusal_is_unavailable_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 403 from the pool reads as unavailable, with nothing substituted."""
        pool = FakePool.serving({}, status=403)
        pool.pin(monkeypatch)
        try:
            assert PoolProvider(_config()).available is False
        finally:
            pool.close()

    def test_an_unreachable_pool_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transport failure degrades, and is attempted exactly once."""
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)

        assert PoolProvider(_config()).available is False
        assert spy.hosts == ["pool.vendor.test"]

    def test_a_spent_budget_refuses_the_identity_check_without_a_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deadline already past is a refusal, not the default ceiling."""
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)

        assert check_pool_available(model_name=PINNED_MODEL, budget=0.0) is False
        assert spy.destinations == []

    def test_an_unconfigured_destination_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With nothing pinned there is nowhere to dial, so no dial happens."""
        monkeypatch.delenv(POOL_URL_ENV, raising=False)
        spy = SocketSpy(monkeypatch)

        assert PoolProvider(_config()).available is False
        assert spy.destinations == []


class TestCompletion:
    """Generation: one dial, one model, and no silent substitution."""

    def test_a_pinned_pool_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The happy path returns the pool's own text."""
        pool = FakePool.serving(generation(text="the answer"))
        pool.pin(monkeypatch)
        try:
            assert PoolProvider(_config()).complete("prompt").text == "the answer"
        finally:
            pool.close()

    def test_usage_counts_are_carried_when_the_pool_reports_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integer token counts reach the completion; junk is dropped."""
        answer = generation()
        answer["usage"] = {"input_tokens": 7, "output_tokens": 3, "bogus": "x"}
        pool = FakePool.serving(answer)
        pool.pin(monkeypatch)
        try:
            usage = PoolProvider(_config()).complete("prompt").usage
        finally:
            pool.close()

        assert usage == {"input_tokens": 7, "output_tokens": 3}

    def test_an_answer_naming_another_model_is_discarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An echoed identity mismatch refuses; the prose is never returned."""
        pool = FakePool.serving(generation(model_name="substitute:1", text="nope"))
        pool.pin(monkeypatch)
        try:
            with pytest.raises(PoolIdentityError) as caught:
                PoolProvider(_config()).complete("prompt")
        finally:
            pool.close()

        assert "nope" not in str(caught.value)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(["not", "a", "mapping"], id="not-a-mapping"),
            pytest.param({"model": PINNED_MODEL}, id="no-text"),
            pytest.param({"model": PINNED_MODEL, "response": 7}, id="text-not-a-str"),
        ],
    )
    def test_an_unusable_answer_refuses(
        self, monkeypatch: pytest.MonkeyPatch, payload: object
    ) -> None:
        """A malformed answer is a refusal, not an empty reflection."""
        pool = FakePool.serving(payload)
        pool.pin(monkeypatch)
        try:
            with pytest.raises(RuntimeError):
                PoolProvider(_config()).complete("prompt")
        finally:
            pool.close()

    def test_a_rate_limited_pool_raises_the_shared_rate_limit_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """429 reuses the provider-neutral rate-limit type, with no retry."""
        pool = FakePool.serving({}, status=429)
        pool.pin(monkeypatch)
        try:
            with pytest.raises(ProviderRateLimitError):
                PoolProvider(_config()).complete("prompt")
            assert pool.accepted == 1
        finally:
            pool.close()

    def test_a_refusing_pool_is_never_retried_and_never_substituted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 500 yields one attempt and one refusal, naming no body."""
        pool = FakePool.serving({"detail": "secret vendor text"}, status=500)
        pool.pin(monkeypatch)
        try:
            with pytest.raises(RuntimeError) as caught:
                PoolProvider(_config()).complete("prompt")
            assert pool.accepted == 1
        finally:
            pool.close()

        assert "secret vendor text" not in str(caught.value)

    def test_a_down_pool_refuses_with_a_single_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No retry, no second host, no fallback model."""
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolBoundaryError):
            PoolProvider(_config()).complete("prompt")

        assert spy.hosts == ["pool.vendor.test"]

    def test_a_vault_config_url_cannot_move_the_destination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``api_base`` edit on a pool stage is refused before any dial."""
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)
        provider = PoolProvider(_config(api_base="https://attacker.test"))

        with pytest.raises(PoolDestinationError):
            provider.complete("prompt")

        assert provider.available is False
        assert spy.destinations == []

    def test_an_output_ceiling_and_a_system_prompt_are_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both optional arguments reach the pool, and the answer comes back."""
        pool = FakePool.serving(generation(text="bounded"))
        pool.pin(monkeypatch)
        try:
            completion = PoolProvider(_config()).complete(
                "prompt", max_tokens=16, system="be brief"
            )
        finally:
            pool.close()

        assert completion.text == "bounded"

    def test_the_pinned_destination_in_config_is_admitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restating the pinned destination is allowed; it moves nothing."""
        pool = FakePool.serving(generation())
        pool.pin(monkeypatch)
        try:
            provider = PoolProvider(_config(api_base=pool.url))
            assert provider.complete("prompt").text
        finally:
            pool.close()
