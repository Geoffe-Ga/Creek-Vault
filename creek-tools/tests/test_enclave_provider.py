"""Attested GPU-CC enclave provider — ``is_cloud=False`` via attest-then-mount (#760).

The enclave provider sends prompts to a remote confidential-compute (GPU-CC)
endpoint, yet is classified ``is_cloud=False`` because it **cryptographically
verifies remote attestation before any prompt or key leaves the device**. The
attestation is the security boundary that justifies the flag, so these tests
defend it directly — an attestation succeeds only when, over ``https``:

- the enclave echoes the fresh per-call nonce (freshness / anti-replay);
- the quote's signature over ``measurement || nonce`` validates under the
  operator-configured Ed25519 trust-root key (authenticity — a forger without
  the private key cannot produce it);
- the attested measurement equals the configured expectation.

A missing prerequisite, insecure transport, unreachable endpoint, replayed
nonce, forged signature, or measurement mismatch all **refuse and send no
prompt**. Because ``is_cloud=False``, the ``ModelRouter`` INTIMATE chokepoint
then admits the enclave for INTIMATE work (and it can be the local ``default``
rescue), while genuine cloud providers stay ``is_cloud=True``.

Attestation is mocked here (no live enclave in CI); the trust-root keypair is
generated per test session and the measurement is a test literal — no real
attestation material is hardcoded.
"""

from __future__ import annotations

from typing import Final
from unittest.mock import MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.completion import Completion
from creek.classify.llm.providers import (
    DEFAULT_MODELS,
    EnclaveAttestationError,
    EnclaveProvider,
    _attestation_signed_payload,
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

_UNSET: Final[object] = object()
"""Sentinel: serve the builder's well-formed default JSON body."""

# The genuine enclave's attestation keypair; its public half is the trust root.
_TRUSTED_KEY = Ed25519PrivateKey.generate()
_TRUSTED_PUBKEY_HEX = _TRUSTED_KEY.public_key().public_bytes_raw().hex()
# An unrelated key an impersonator might sign with (never the configured root).
_IMPOSTOR_KEY = Ed25519PrivateKey.generate()


def _config(
    *,
    url: str | None = _ENCLAVE_URL,
    measurement: str | None = _MEASUREMENT,
    pubkey: str | None = _TRUSTED_PUBKEY_HEX,
) -> LLMConfig:
    """Build an enclave ``LLMConfig`` with endpoint, policy, and trust root set."""
    return LLMConfig(
        provider="enclave",
        enclave_url=url,
        enclave_expected_measurement=measurement,
        enclave_attestation_pubkey=pubkey,
    )


def _patched_client(
    *,
    sign_key: Ed25519PrivateKey | None = None,
    measurement: str = _MEASUREMENT,
    tamper_nonce: bool = False,
    attest_error: Exception | None = None,
    gen_text: str = "grounded voice output",
    gen_error: Exception | None = None,
    quote_json: object = _UNSET,
    gen_json: object = _UNSET,
) -> tuple[MagicMock, MagicMock]:
    """Return a patched ``httpx.Client`` class and the shared request ctx mock.

    ``.get`` serves a **signed** attestation quote: it reads the fresh nonce from
    the request params and returns ``{measurement, nonce, signature}`` signed by
    *sign_key* (the trusted key by default) over ``measurement || echoed-nonce``.
    ``tamper_nonce`` makes the enclave echo a stale nonce (replay). ``.post``
    serves the generate response, so a test can assert ``ctx.post`` was never
    called when attestation refuses.

    ``quote_json`` and ``gen_json`` replace the decoded JSON body of the
    attestation and generate responses respectively, for the malformed-envelope
    cases; left at ``_UNSET`` the well-formed bodies above are served unchanged.
    """
    key = sign_key or _TRUSTED_KEY
    ctx = MagicMock()

    if attest_error is not None:
        ctx.get.side_effect = attest_error
    else:

        def _get(
            url: str, params: dict[str, str] | None = None, **_: object
        ) -> MagicMock:
            sent = (params or {}).get("nonce", "")
            echoed = "stale-nonce-deadbeef" if tamper_nonce else sent
            signature = key.sign(_attestation_signed_payload(measurement, echoed)).hex()
            resp = MagicMock(status_code=200)
            resp.json.return_value = (
                {
                    "measurement": measurement,
                    "nonce": echoed,
                    "signature": signature,
                }
                if quote_json is _UNSET
                else quote_json
            )
            resp.raise_for_status.return_value = None
            return resp

        ctx.get.side_effect = _get

    if gen_error is not None:
        ctx.post.side_effect = gen_error
    else:
        gen = MagicMock(status_code=200)
        gen.json.return_value = (
            {"response": gen_text} if gen_json is _UNSET else gen_json
        )
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
# Attest-then-mount: the cryptographic egress gate
# --------------------------------------------------------------------------- #


def test_complete_attests_before_sending_then_returns_completion() -> None:
    """A valid signed attestation ⇒ prompt sent, ``Completion`` returned."""
    client_cls, ctx = _patched_client(gen_text="voice in Geoff's register")
    with patch(_PATCH_TARGET, client_cls):
        result = build_provider(_config()).complete("draft this")
    assert isinstance(result, Completion)
    assert result.text == "voice in Geoff's register"
    assert ctx.get.called  # attestation challenge issued
    assert ctx.post.called  # generate happened, after attestation
    assert "/attestation" in ctx.get.call_args.args[0]


def test_forged_signature_refuses_and_sends_no_prompt() -> None:
    """A quote signed by the wrong key (impersonator) refuses — no prompt sent."""
    client_cls, ctx = _patched_client(sign_key=_IMPOSTOR_KEY)
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(RuntimeError, match="signature"),
    ):
        build_provider(_config()).complete("intimate journal entry")
    ctx.post.assert_not_called()


def test_replayed_nonce_refuses_and_sends_no_prompt() -> None:
    """A quote echoing a stale nonce (replay) refuses — no prompt sent."""
    client_cls, ctx = _patched_client(tamper_nonce=True)
    with patch(_PATCH_TARGET, client_cls), pytest.raises(RuntimeError, match="nonce"):
        build_provider(_config()).complete("intimate journal entry")
    ctx.post.assert_not_called()


def test_measurement_mismatch_refuses_and_sends_no_prompt() -> None:
    """A validly-signed quote for the wrong measurement refuses — no prompt sent."""
    client_cls, ctx = _patched_client(measurement="sha384:WRONG-untrusted")
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(RuntimeError, match="measurement"),
    ):
        build_provider(_config()).complete("intimate journal entry")
    ctx.post.assert_not_called()


def test_insecure_http_url_refuses_before_any_network() -> None:
    """A non-``https`` enclave URL refuses before any request leaves the device."""
    client_cls, ctx = _patched_client()
    with patch(_PATCH_TARGET, client_cls), pytest.raises(RuntimeError, match="https"):
        build_provider(_config(url="http://enclave.internal:8443")).complete("x")
    ctx.get.assert_not_called()
    ctx.post.assert_not_called()


def test_unreachable_attestation_endpoint_refuses_and_sends_no_prompt() -> None:
    """If attestation cannot be fetched, refuse — never fall back to egress."""
    client_cls, ctx = _patched_client(attest_error=httpx.ConnectError("no route"))
    with patch(_PATCH_TARGET, client_cls), pytest.raises(RuntimeError):
        build_provider(_config()).complete("intimate journal entry")
    ctx.post.assert_not_called()


def test_missing_attestation_policy_refuses_before_any_network() -> None:
    """With no expected measurement, the provider cannot attest → refuse."""
    client_cls, ctx = _patched_client()
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(RuntimeError, match=r"measurement|policy"),
    ):
        build_provider(_config(measurement=None)).complete("intimate journal entry")
    ctx.get.assert_not_called()
    ctx.post.assert_not_called()


def test_missing_trust_root_refuses_before_any_network() -> None:
    """With no configured trust-root key, the provider cannot attest → refuse."""
    client_cls, ctx = _patched_client()
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(RuntimeError, match=r"trust|pubkey"),
    ):
        build_provider(_config(pubkey=None)).complete("intimate journal entry")
    ctx.get.assert_not_called()
    ctx.post.assert_not_called()


def test_missing_enclave_url_refuses_before_any_network() -> None:
    """With no endpoint configured, the provider refuses before any network call."""
    client_cls, ctx = _patched_client()
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(RuntimeError, match="enclave"),
    ):
        build_provider(_config(url=None)).complete("intimate journal entry")
    ctx.get.assert_not_called()
    ctx.post.assert_not_called()


def test_generate_error_propagates_after_successful_attestation() -> None:
    """A transport error on the generate call surfaces (attestation already passed)."""
    client_cls, ctx = _patched_client(gen_error=httpx.ConnectError("gen down"))
    with patch(_PATCH_TARGET, client_cls), pytest.raises(httpx.HTTPError):
        build_provider(_config()).complete("draft this")
    assert ctx.get.called  # attestation succeeded
    assert ctx.post.called  # the failure was on the generate call, not the gate


def test_available_true_when_attestation_verifies() -> None:
    """``available`` reflects a live, verifiable attestation."""
    client_cls, _ = _patched_client()
    with patch(_PATCH_TARGET, client_cls):
        assert build_provider(_config()).available is True


def test_available_false_when_attestation_fails() -> None:
    """``available`` is False when the enclave cannot be attested."""
    client_cls, _ = _patched_client(sign_key=_IMPOSTOR_KEY)
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
        enclave_attestation_pubkey=_TRUSTED_PUBKEY_HEX,
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
                "enclave_attestation_pubkey": _TRUSTED_PUBKEY_HEX,
            },
        )
    )
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
                "enclave_attestation_pubkey": _TRUSTED_PUBKEY_HEX,
            },
            generation={"provider": "anthropic"},
        )
    )
    assert router.resolve("generation", PrivacyTier.INTIMATE).provider == "enclave"


# --------------------------------------------------------------------------- #
# Degenerate enclave envelopes: attestation shape + generate payload (#1449)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "quote",
    [[], ["not", "an", "object"], "a bare string", 42, 3.5, True],
    ids=["empty-list", "list", "string", "int", "float", "bool"],
)
def test_a_non_object_attestation_quote_refuses_and_sends_no_prompt(
    quote: object,
) -> None:
    """A quote that is not a JSON object refuses — no prompt egress.

    The message is asserted with ``==`` against the literal so the FULL refusal
    text is pinned — a later widening of the message cannot slip past. What
    pins this to the non-dict arm specifically (rather than the sibling
    ``from exc`` transport arm, which emits an identical string) is the
    fixture: ``raise_for_status`` returns ``None``, so no ``httpx.HTTPError``
    is reachable here.
    """
    client_cls, ctx = _patched_client(quote_json=quote)
    with (
        patch(_PATCH_TARGET, client_cls),
        pytest.raises(EnclaveAttestationError) as exc,
    ):
        build_provider(_config()).complete("intimate journal entry")
    assert str(exc.value) == (
        "enclave attestation could not be verified; refusing to send data"
    )
    ctx.post.assert_not_called()


def test_max_tokens_and_system_are_forwarded_in_the_generate_body() -> None:
    """Optional ``max_tokens`` / ``system`` land as keys in the posted payload."""
    client_cls, ctx = _patched_client()
    with patch(_PATCH_TARGET, client_cls):
        build_provider(_config()).complete("p", max_tokens=256, system="be terse")
    body = ctx.post.call_args.kwargs["json"]
    assert body["max_tokens"] == 256
    assert body["system"] == "be terse"


def test_omitted_options_leave_the_generate_body_minimal() -> None:
    """Neither optional key appears when the caller omits both.

    A regression guard, not a newly-closed branch: the false arms of the two
    ``is not None`` guards are already covered by the ten existing
    ``.complete()`` call sites in this module.
    """
    client_cls, ctx = _patched_client()
    with patch(_PATCH_TARGET, client_cls):
        build_provider(_config()).complete("p")
    body = ctx.post.call_args.kwargs["json"]
    assert set(body) == {"model", "prompt", "stream"}


def test_a_generate_body_without_a_response_key_yields_empty_text() -> None:
    """A generate body missing ``"response"`` degrades to "" — never a KeyError.

    This arm is a ``dict.get`` default, so it carries **no branch** and no
    coverage percentage will ever flag it as missing.
    """
    client_cls, _ = _patched_client(gen_json={"unexpected": "shape"})
    with patch(_PATCH_TARGET, client_cls):
        result = build_provider(_config()).complete("draft this")
    assert isinstance(result, Completion)
    assert result.text == ""
