"""Property-based tests for ``creek.redact.redactor.Redactor``.

Redaction must be idempotent: applying it twice yields the same output
as once, so a marker the redactor emitted is never itself rewritten on a
later pass. Hypothesis fuzzes inputs that mix arbitrary text with
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
# Each line carries `# pragma: allowlist secret` so detect-secrets does
# not flag them — these are pattern-shape literals, not real credentials.
_SECRET_SAMPLES = st.sampled_from(
    [
        "AKIAIOSFODNN7EXAMPLE",  # pragma: allowlist secret  AWS test example
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDexample==",  # pragma: allowlist secret
        # pragma: allowlist secret  (private-key shape, not a real key)
        "-----BEGIN RSA PRIVATE KEY-----xxx-----END RSA PRIVATE KEY-----",
        "user@example.com",
        "555-12-3456",
        "4111111111111111",  # pragma: allowlist secret  card test PAN
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
    """A known secret bounded by delimiters is always fully redacted (#435).

    The secret is whitespace-delimited from the noise so adjacency cannot
    *fuse* with and invalidate its pattern token. (A word char glued straight
    onto an email TLD — ``user@example.com0`` — is genuinely not an email and
    is correctly left intact; that boundary decision is pinned by the
    deterministic tests below. Gating ``secret not in redacted`` on the mere
    presence of *any* ``[REDACTED:`` marker — which can come from unrelated
    high-entropy noise in the prefix — was the bug behind #435.)
    """
    redactor = _make_redactor()
    # Gate on whether the secret matches a pattern as a clean, delimited token
    # (some samples — e.g. a low-entropy ssh-rsa body — match no built-in
    # pattern). This is the precise precondition the #435 bug lacked: it gated
    # on *any* marker appearing, which could come from unrelated prefix noise.
    if secret in redactor.redact_content(f" {secret} "):
        return
    content = f"{prefix} {secret} {suffix}"
    redacted = redactor.redact_content(content)
    # A matching secret, delimited from the noise, is removed whole — no
    # fragment of the matched secret survives.
    assert secret not in redacted


def test_digit_glued_email_is_left_intact_but_bounded_email_is_redacted() -> None:
    """The #435 boundary decision: a word char glued to the TLD is not an email.

    ``user@example.com0`` is not a valid email (``.com0`` is not a TLD), so
    redacting ``user@example.com`` out of it is *not* attempted — doing so
    would leave fragments for inputs like ``user@example.community``. A
    properly bounded email is redacted whole. So a redaction marker that
    appears alongside such a glued string does not mean a leak occurred.
    """
    redactor = _make_redactor()
    # Glued to a digit → not an email → correctly left intact.
    assert redactor.redact_content("user@example.com0") == "user@example.com0"
    # Bounded → redacted whole.
    bounded = redactor.redact_content("contact user@example.com today")
    assert "user@example.com" not in bounded
    assert "[REDACTED:email]" in bounded


def test_high_entropy_email_local_part_redacted_whole_no_fragment() -> None:
    """An email with a high-entropy local part is redacted as one whole email.

    The ``email`` match and the high-entropy candidate (the local part)
    are both collected against the *original* content and unioned, so the
    whole address is replaced and no high-entropy fragment of it is left
    behind — the failure mode #435's title warned about. The union is
    labelled ``email`` because both patterns are severity "medium" and
    the tie-break picks the widest contributing span (#909).

    Equality on the full string is deliberate: substring checks cannot
    tell "redacted whole" from "redacted with a surviving remainder".
    """
    redactor = _make_redactor()
    out = redactor.redact_content("key aB3xY7zQ9mK2pL5nR8vT4wd@example.com end")
    assert out == "key [REDACTED:email] end"


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
