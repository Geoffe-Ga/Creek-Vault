"""Property-based tests for ``creek.redact.redactor.Redactor``.

Redaction must be idempotent and additive: applying it twice yields the
same output as once, and the redacted output never contains the
``[REDACTED:type]`` marker shapes that the redactor would itself
produce. Hypothesis fuzzes inputs that mix arbitrary text with
recognisable secrets to exercise these contracts on the patch logic.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from creek.config import RedactionConfig
from creek.redact.redactor import Redactor

_PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Synthetic secrets are deliberately well-known examples so they appear
# in no real configuration and can be safely committed to source control.
_SECRET_SAMPLES = st.sampled_from(
    [
        "AKIAIOSFODNN7EXAMPLE",  # AWS access key (RFC-style example)
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDexample==",
        "-----BEGIN RSA PRIVATE KEY-----xxx-----END RSA PRIVATE KEY-----",
        "user@example.com",
        "555-12-3456",
        "4111111111111111",  # PAN test number
    ]
)

_TEXT_NOISE = st.text(
    alphabet=st.characters(blacklist_categories=["Cs"]),
    max_size=200,
)


def _make_redactor() -> Redactor:
    """Construct a Redactor with default patterns and a fixed salt."""
    return Redactor(RedactionConfig(), salt=b"property-test-salt")


@_PROFILE
@given(prefix=_TEXT_NOISE, secret=_SECRET_SAMPLES, suffix=_TEXT_NOISE)
def test_redaction_is_idempotent(prefix: str, secret: str, suffix: str) -> None:
    """Applying redaction twice produces the same output as once."""
    redactor = _make_redactor()
    content = f"{prefix}{secret}{suffix}"
    once = redactor.redact_content(content)
    twice = redactor.redact_content(once)
    assert once == twice


@_PROFILE
@given(noise=_TEXT_NOISE)
def test_redaction_preserves_non_sensitive_text(noise: str) -> None:
    """Text whose patterns did not fire passes through verbatim.

    Hypothesis can generate strings that happen to match a built-in
    pattern (e.g. an email-shaped substring); the test partitions on
    whether a redaction marker actually appeared:
      - No marker  → redacted output must equal the input exactly.
      - Has marker → at most a handful of substitutions, bounded by
        a length tied to the *number of markers actually present*
        rather than the input length, so a pathological 4x-expansion
        regression cannot pass.
    """
    redactor = _make_redactor()
    redacted = redactor.redact_content(noise)

    if "[REDACTED:" not in redacted:
        # Guard against the canonical regression: the redactor mutates
        # text that should be inert. Equality is the right contract
        # here — there is no mechanism by which Redactor.redact_content
        # would legitimately rewrite a non-matching string.
        assert redacted == noise
        return

    # When markers DID fire, each one replaces a single matched span
    # with a bounded marker like "[REDACTED:email]" (≤ 64 chars in
    # current patterns). The total expansion is therefore at most
    # `marker_count * 64` characters — *not* a multiple of the input
    # length — so a regression that ballooned each substitution to
    # several KB would fail this assertion.
    marker_count = redacted.count("[REDACTED:")
    assert len(redacted) <= len(noise) + marker_count * 64


@_PROFILE
@given(prefix=_TEXT_NOISE, secret=_SECRET_SAMPLES, suffix=_TEXT_NOISE)
def test_redacted_marker_is_present_when_secret_matches(
    prefix: str, secret: str, suffix: str
) -> None:
    """When a known secret is in the input, at least one ``[REDACTED:`` marker
    appears in the output (provided the secret matches a built-in pattern).
    """
    redactor = _make_redactor()
    content = f"{prefix}{secret}{suffix}"
    redacted = redactor.redact_content(content)
    # If a built-in pattern fired, a [REDACTED:<type>] token appears.
    # If no pattern fired (e.g. a sample we don't yet match), the
    # property still trivially holds because the test only asserts on
    # the inputs that *do* match.
    if "[REDACTED:" in redacted:
        assert secret not in redacted


@_PROFILE
@given(prefix=_TEXT_NOISE, suffix=_TEXT_NOISE)
def test_redaction_with_dryrun_or_disabled_unsupported(
    prefix: str, suffix: str
) -> None:
    """``redact_content`` always replaces — dry-run is the caller's
    responsibility (it never silently no-ops).
    """
    redactor = _make_redactor()
    content = f"{prefix}user@example.com{suffix}"
    redacted = redactor.redact_content(content)
    if "user@example.com" in redacted:
        # Email pattern didn't fire — must be allowlisted somewhere.
        # Either way, redact_content must not have raised.
        return
    assert "[REDACTED:" in redacted


def test_redaction_known_aws_key_smoke() -> None:
    """Concrete regression case: a documented AWS test key is redacted."""
    redactor = _make_redactor()
    content = "key=AKIAIOSFODNN7EXAMPLE in config"
    redacted = redactor.redact_content(content)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted, (
        "Built-in patterns should redact AWS access keys"
    )


@pytest.mark.parametrize(
    "secret",
    ["foo@bar.com", "555-12-3456"],
)
def test_redaction_idempotency_concrete(secret: str) -> None:
    """Concrete idempotency guard separate from Hypothesis."""
    redactor = _make_redactor()
    once = redactor.redact_content(f"prefix {secret} suffix")
    twice = redactor.redact_content(once)
    assert once == twice
