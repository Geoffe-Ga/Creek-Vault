"""Tests for the shared MCP token-strength policy (#907).

``CREEK_MCP_ELEVATED_TOKEN`` gates irreversible ``creek.purge.*`` vault
destruction, yet it carried no minimum-length floor while the *lower*-value
per-consumer bearer tokens already enforced one (#838). :mod:`creek_mcp.token_policy`
is the single stdlib-only home for that floor, so both auth surfaces enforce
the same number with the same wording — and neither can echo a secret back
while complaining about it.
"""

from __future__ import annotations

import pytest

from creek_mcp.token_policy import (
    MIN_TOKEN_LEN,
    meets_min_length,
    require_min_length,
)


def test_min_token_len_is_thirty_two() -> None:
    """The floor is exactly 32 characters — pinned against silent weakening."""
    assert MIN_TOKEN_LEN == 32


def test_meets_min_length_at_boundary() -> None:
    """32 chars clears the floor; 31 chars and the empty string do not."""
    assert meets_min_length("a" * 32) is True
    assert meets_min_length("a" * 31) is False
    assert meets_min_length("") is False


def test_require_min_length_returns_token_unchanged() -> None:
    """A compliant token round-trips byte-for-byte, not normalised or truncated."""
    # 45 chars, low entropy: test literal, not a real credential.
    token = "policy-test-strong-token-" + "a" * 20
    assert require_min_length("elevated token", token) == token


def test_require_min_length_rejects_short_token_without_echoing_it() -> None:
    """A sub-minimum token raises, naming everything except the token value.

    The message must be actionable (which setting, how short, how long it
    must be, how to rotate it) while never printing the secret itself —
    startup errors land in logs, terminals, and process supervisors.
    """
    # 12 chars, low entropy: test literal, not a real credential.
    weak = "weak-" + "b" * 7
    with pytest.raises(ValueError) as excinfo:
        require_min_length("elevated token", weak)
    message = str(excinfo.value)
    assert "elevated token" in message  # the subject is named
    assert "12" in message  # the observed length
    assert "32" in message  # the enforced minimum
    assert "secrets.token_urlsafe(32)" in message  # the rotation recipe
    assert weak not in message  # NEVER echo the token value
