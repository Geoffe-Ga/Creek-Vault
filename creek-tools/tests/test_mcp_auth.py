"""Tests for the MCP elevated-authorization gate (FEAT-012).

The gate decides whether a destructive tool call (``creek.purge.*``)
may proceed. The expected token lives in the ``CREEK_MCP_ELEVATED_TOKEN``
environment variable at server startup; callers present a matching
token via the ``auth_token`` tool argument. The comparison MUST use
:func:`hmac.compare_digest` so a hostile client cannot infer the token
byte-by-byte via timing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from creek_mcp import auth

if TYPE_CHECKING:
    import pytest


# 40 chars — clears the 32-char floor (#907). Low-entropy test literal,
# not a real credential.
_STRONG_TOKEN = "elevated-test-token-" + "a" * 20


def test_is_elevated_returns_false_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an elevated token configured the gate fails closed."""
    monkeypatch.delenv("CREEK_MCP_ELEVATED_TOKEN", raising=False)
    assert auth.is_elevated("anything") is False


def test_is_elevated_returns_false_when_env_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty env token is treated as unset; the gate must not match ``""``."""
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", "")
    assert auth.is_elevated("") is False
    assert auth.is_elevated("anything") is False


def test_is_elevated_returns_false_when_provided_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing client token fails closed even when env token is configured."""
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)
    assert auth.is_elevated(None) is False
    assert auth.is_elevated("") is False


def test_is_elevated_returns_false_for_mismatched_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-matching client token is rejected."""
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)
    assert auth.is_elevated("not-the-secret") is False


def test_is_elevated_returns_true_for_matching_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correct client token unlocks elevated tools."""
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)
    assert auth.is_elevated(_STRONG_TOKEN) is True


def test_is_elevated_uses_hmac_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate routes the comparison through :func:`hmac.compare_digest`.

    Plain ``==`` on string tokens is timing-vulnerable. The test
    monkey-patches ``hmac.compare_digest`` and asserts ``is_elevated``
    calls it with the expected and actual tokens as bytes.
    """
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)
    calls: list[tuple[object, object]] = []

    def _spy(a: object, b: object) -> bool:
        calls.append((a, b))
        return a == b  # only inside the test spy, never inside auth.py

    monkeypatch.setattr(auth.hmac, "compare_digest", _spy)
    assert auth.is_elevated(_STRONG_TOKEN) is True
    assert calls, "is_elevated must route through hmac.compare_digest"
    expected, actual = calls[0]
    assert expected == _STRONG_TOKEN.encode("utf-8")
    assert actual == _STRONG_TOKEN.encode("utf-8")


def test_is_elevated_denies_sub_minimum_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A weak *server* secret denies even an exact match, silently (#907).

    Defense in depth behind the startup check: if a sub-32-char
    ``CREEK_MCP_ELEVATED_TOKEN`` ever reaches a running process, the gate
    refuses to authorize anything — a guessable secret must not guard
    irreversible vault destruction. The deny is silent: ``is_elevated``
    returns a ``bool`` rather than raising, so no configuration detail
    (not even "the server secret is weak") reaches the caller.
    """
    # 31 chars — one under the floor. Test literal, not a real credential.
    weak_token = "weak-elevated-" + "a" * 17
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", weak_token)
    assert auth.is_elevated(weak_token) is False


def test_is_elevated_accepts_exact_boundary_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured token of exactly 32 chars sits on the floor and authorizes.

    Guards the length comparison against an off-by-one (``<=`` instead of
    ``<``) that would reject the very tokens the rotation recipe produces.
    """
    boundary_token = "a" * 32  # test literal, not a real credential
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", boundary_token)
    assert auth.is_elevated(boundary_token) is True


def test_auth_module_source_uses_compare_digest_not_equality() -> None:
    """Static check: ``auth.py`` must not compare token strings with ``==``.

    ADAPT-004 / FEAT-012: elevated-auth comparisons MUST be constant
    time. Walking the AST is more robust than a textual search — it
    won't false-positive on string literals like ``"=="`` in docstrings.
    """
    source = Path(auth.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "is_elevated":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Compare):
                for op in sub.ops:
                    if isinstance(op, ast.Eq | ast.NotEq):
                        msg = (
                            "is_elevated must not use ==/!= on tokens; "
                            "use hmac.compare_digest"
                        )
                        raise AssertionError(msg)
