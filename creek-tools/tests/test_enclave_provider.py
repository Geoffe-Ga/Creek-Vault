"""Attested GPU-CC enclave provider — ``is_cloud=False`` via attest-then-mount (#760).

The enclave provider sends prompts to a remote confidential-compute (GPU-CC)
endpoint, yet is classified ``is_cloud=False`` because it **verifies remote
attestation of the enclave before any prompt or key leaves the device**. The
attestation gate is the security boundary that justifies the flag: these tests
defend it directly —

- attestation must succeed (measurement matches the configured expectation)
  *before* the generate call; a mismatch, an unreachable attestation endpoint,
  or a missing attestation policy **refuses and sends no prompt data**;
- because ``is_cloud=False``, the ``ModelRouter`` INTIMATE chokepoint admits the
  enclave for INTIMATE-tier work (and it can serve as the local ``default``
  rescue), while genuine cloud providers stay ``is_cloud=True`` and are still
  redirected away from INTIMATE.

Attestation is mocked here (no live enclave in CI); the measurement string is a
test literal, not real attestation material.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.completion import Completion
from creek.classify.llm.providers import (
    DEFAULT_MODELS,
    EnclaveProvider,
    build_provider,
    known_providers,
    provider_display_name,
    provider_is_cloud,
)
from creek.classify.llm.router import ModelRouter
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier

_ENCLAVE_URL = "https://enclave.internal:8443"
_MEASUREMENT = "sha384:attested-measurement-abc123"  # test literal, not real
_PATCH_TARGET = "creek.classify.llm.providers.httpx.Client"


def _config(
    *, url: str | None = _ENCLAVE_URL, measurement: str | None = _MEASUREMENT
) -> LLMConfig:
    """Build an enclave ``LLMConfig`` with the endpoint + attestation policy set."""
    return LLMConfig(
        provider="enclave",
        enclave_url=url,
        enclave_expected_measurement=measurement,
    )


def _patched_client(
    *,
    attest_measurement: str | None = _MEASUREMENT,
    attest_error: Exception | None = None,
    gen_text: str = "grounded voice output",
) -> tuple[MagicMock, MagicMock]:
    """Return a patched ``httpx.Client`` class and the shared request ctx mock.

    The context manager's ``.get`` serves the attestation quote and ``.post``
    serves the generate response, so a test can assert ``ctx.post`` was never
    called when attestation refuses.
    """
    ctx = MagicMock()
    if attest_error is not None:
        ctx.get.side_effect = attest_error
    else:
        att = MagicMock(status_code=200)
        att.json.return_value = {"measurement": attest_measurement}
        att.raise_for_status.return_value = None
        ctx.get.return_value = att

    gen = MagicMock(status_code=200)
    gen.json.return_value = {"response": gen_text}
    gen.raise_for_status.return_value = None
    ctx.post.return_value = gen

    client_cls = MagicMock()
    client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
    client_cls.return_value.__exit__ = MagicMock(return_value=False)
    return client_cls, ctx


# --------------------------------------------------------------------------- #
# Registration + is_cloud classification
# --------------------------------------------------------------------------- #


def test_enclave_provider_is_not_cloud() -> None:
    """The enclave is ``is_cloud=False`` — attestation makes it operator-unreadable."""
    assert EnclaveProvider.is_cloud is False
    assert provider_is_cloud("enclave") is False


def test_cloud_providers_remain_cloud() -> None:
    """Adding the enclave does not reclassify the genuine cloud backends."""
    assert provider_is_cloud("anthropic") is True
    assert provider_is_cloud("openai") is True
    assert provider_is_cloud("gemini") is True


def test_enclave_registered_and_config_accepts_it() -> None:
    """The provider registers, builds, satisfies the protocol, and validates."""
    assert "enclave" in known_providers()
    provider = build_provider(_config())
    assert isinstance(provider, EnclaveProvider)
    assert isinstance(provider, LLMProvider)
    # Registry-driven config validation accepts the name (no separate allowlist).
    assert LLMConfig(provider="enclave").provider == "enclave"
    assert provider_display_name("enclave") == EnclaveProvider.PROVIDER_NAME


def test_enclave_default_model_in_matrix() -> None:
    """The enclave's default model lives in the one-place ``DEFAULT_MODELS`` matrix."""
    assert set(DEFAULT_MODELS) == set(known_providers())
    assert DEFAULT_MODELS["enclave"] == EnclaveProvider.DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Attest-then-mount: the egress gate
# --------------------------------------------------------------------------- #


def test_complete_attests_before_sending_then_returns_completion() -> None:
    """On a valid attestation, the prompt is sent and a ``Completion`` returned."""
    client_cls, ctx = _patched_client(gen_text="voice in Geoff's register")
    with patch(_PATCH_TARGET, client_cls):
        result = build_provider(_config()).complete("draft this")
    assert isinstance(result, Completion)
    assert result.text == "voice in Geoff's register"
    # Attestation (GET) happened, and the generate (POST) happened after it.
    assert ctx.get.called
    assert ctx.post.called
    # The attestation endpoint is hit, not the prompt endpoint, for the quote.
    assert "/attestation" in ctx.get.call_args.args[0]


def test_measurement_mismatch_refuses_and_sends_no_prompt() -> None:
    """A wrong attested measurement refuses egress — the prompt is never sent."""
    client_cls, ctx = _patched_client(attest_measurement="sha384:WRONG-untrusted")
    with patch(_PATCH_TARGET, client_cls), pytest.raises(RuntimeError, match="attest"):
        build_provider(_config()).complete("intimate journal entry")
    ctx.post.assert_not_called()  # no data egress on attestation failure


def test_unreachable_attestation_endpoint_refuses_and_sends_no_prompt() -> None:
    """If attestation cannot be verified, refuse — never fall back to egress."""
    client_cls, ctx = _patched_client(attest_error=httpx.ConnectError("no route"))
    with patch(_PATCH_TARGET, client_cls), pytest.raises(RuntimeError):
        build_provider(_config()).complete("intimate journal entry")
    ctx.post.assert_not_called()


def test_missing_attestation_policy_refuses_and_sends_no_prompt() -> None:
    """With no expected measurement configured, the provider cannot attest → refuse."""
    client_cls, ctx = _patched_client()
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(RuntimeError, match=r"attest|measurement"),
    ):
        build_provider(_config(measurement=None)).complete("intimate journal entry")
    ctx.post.assert_not_called()


def test_missing_enclave_url_refuses_and_sends_no_prompt() -> None:
    """With no endpoint configured, the provider refuses before any network call."""
    client_cls, ctx = _patched_client()
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(RuntimeError, match="enclave"),
    ):
        build_provider(_config(url=None)).complete("intimate journal entry")
    ctx.get.assert_not_called()
    ctx.post.assert_not_called()


def test_available_true_when_attestation_verifies() -> None:
    """``available`` reflects a live, verifiable attestation."""
    client_cls, _ = _patched_client()
    with patch(_PATCH_TARGET, client_cls):
        assert build_provider(_config()).available is True


def test_available_false_when_attestation_fails() -> None:
    """``available`` is False when the enclave cannot be attested."""
    client_cls, _ = _patched_client(attest_measurement="sha384:WRONG")
    with patch(_PATCH_TARGET, client_cls):
        assert build_provider(_config()).available is False


def test_model_resolves_from_matrix_default() -> None:
    """The model id defaults to the matrix entry and honors an explicit override."""
    assert build_provider(_config()).model == DEFAULT_MODELS["enclave"]
    override = LLMConfig(
        provider="enclave",
        model="custom-cc-model",
        enclave_url=_ENCLAVE_URL,
        enclave_expected_measurement=_MEASUREMENT,
    )
    assert EnclaveProvider(override).model == "custom-cc-model"


# --------------------------------------------------------------------------- #
# Router: INTIMATE may use the enclave; the chokepoint still holds for cloud
# --------------------------------------------------------------------------- #


def _routing(**stages: dict[str, object]) -> LLMRoutingConfig:
    """Build a routing config from raw per-stage dicts."""
    return LLMRoutingConfig.model_validate(stages)


def test_intimate_routes_to_enclave_unredirected() -> None:
    """An INTIMATE fragment may legitimately use the attested enclave for generation."""
    router = ModelRouter(
        _routing(
            default={"provider": "ollama"},
            generation={
                "provider": "enclave",
                "enclave_url": _ENCLAVE_URL,
                "enclave_expected_measurement": _MEASUREMENT,
            },
        )
    )
    # Non-intimate and intimate both resolve to the enclave — not redirected.
    assert router.resolve("generation").provider == "enclave"
    assert router.resolve("generation", PrivacyTier.INTIMATE).provider == "enclave"


def test_cloud_generation_still_redirected_for_intimate() -> None:
    """The chokepoint still holds: a cloud provider is redirected off INTIMATE."""
    router = ModelRouter(
        _routing(
            default={"provider": "ollama"},
            generation={"provider": "anthropic"},
        )
    )
    assert router.resolve("generation", PrivacyTier.INTIMATE).provider == "ollama"


def test_enclave_can_be_local_default_rescue_for_intimate() -> None:
    """The enclave can serve as the local ``default`` that rescues an INTIMATE call."""
    router = ModelRouter(
        _routing(
            default={
                "provider": "enclave",
                "enclave_url": _ENCLAVE_URL,
                "enclave_expected_measurement": _MEASUREMENT,
            },
            generation={"provider": "anthropic"},
        )
    )
    # Cloud generation for INTIMATE is redirected to the (non-cloud) enclave default.
    assert router.resolve("generation", PrivacyTier.INTIMATE).provider == "enclave"
