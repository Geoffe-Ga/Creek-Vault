"""Tests for the MCP elevated-authorization gate (FEAT-012).

The gate decides whether a destructive tool call (``creek.purge.*``)
may proceed. The expected token lives in the ``CREEK_MCP_ELEVATED_TOKEN``
environment variable at server startup; callers present a matching
token via the ``auth_token`` tool argument. The comparison MUST use
:func:`hmac.compare_digest` so a hostile client cannot infer the token
byte-by-byte via timing.

Constant-time comparison alone only stops the attacker *learning* the token
one byte at a time; it does nothing about guessing it whole, at machine
speed, over a surface that answers in microseconds. #914 adds the missing
half — a failed-attempt budget in :mod:`creek_mcp.attempt_policy` that
``is_elevated`` routes every verification through — so this module also pins
where the comparison is allowed to live, and that the throttle is inherited
rather than reimplemented.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from creek_mcp import auth
from tests.elevated_attempt_support import FakeMonotonicClock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType


# 40 chars — clears the 32-char floor (#907). Low-entropy test literal,
# not a real credential.
_STRONG_TOKEN = "elevated-test-token-" + "a" * 20

# 39 chars — clears the floor and matches nothing. Test literal, not a real
# credential.
_WRONG_TOKEN = "wrong-elevated-token-" + "z" * 18

# 31 chars — one under the floor. Test literal, not a real credential.
_WEAK_TOKEN = "weak-elevated-" + "a" * 17

# 36 chars of unpaired surrogates — clears the floor by length but cannot be
# encoded to UTF-8 (#914). Test literal, not a real credential.
_SURROGATE_TOKEN = "\ud800" * 36


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


# ---------------------------------------------------------------------------
# Static invariants over the gate's source
# ---------------------------------------------------------------------------


def _auth_tree() -> ast.Module:
    """Parse ``creek_mcp/auth.py`` from disk.

    Returns:
        The module's AST, read from the file rather than from
        :func:`inspect.getsource` so the assertion is about what ships.
    """
    return ast.parse(Path(auth.__file__).read_text(encoding="utf-8"))


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every function definition in *tree*, nested ones included.

    Args:
        tree: The parsed module (or any subtree) to scan.

    Returns:
        Each ``def`` and ``async def``. Coroutines are included so a future
        ``async def`` cannot slip past a guard that only knew about
        :class:`ast.FunctionDef`.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield the nodes belonging to *node* itself, not to a nested ``def``.

    Lambdas *are* descended into — a lambda body is code the enclosing
    function runs, and ``is_elevated`` is expected to hand one to the attempt
    budget — while ``def``/``async def`` bodies are not, so a helper defined
    inside another function is attributed to itself alone and never
    double-counted against its parent.

    Args:
        node: The subtree root to walk.

    Yields:
        Every descendant that is part of *node*'s own code.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        yield child
        yield from _own_nodes(child)


def _calls_attribute(function: ast.AST, attribute: str) -> bool:
    """Return whether *function*'s own body calls ``<something>.<attribute>``.

    Args:
        function: The function node to inspect.
        attribute: The attribute name of the callee, e.g. ``compare_digest``.

    Returns:
        ``True`` when at least one such call site is in the function's own
        code.
    """
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        for node in _own_nodes(function)
    )


def _reads_name(function: ast.AST, name: str) -> bool:
    """Return whether *function*'s own body mentions the bare symbol *name*.

    Args:
        function: The function node to inspect.
        name: The symbol to look for, e.g. ``ELEVATED_TOKEN_ENV``.

    Returns:
        ``True`` when the symbol is loaded (or stored) anywhere in the
        function's own code.
    """
    return any(
        isinstance(node, ast.Name) and node.id == name for node in _own_nodes(function)
    )


def _dotted_module_name(path: Path, package_root: Path) -> str:
    """Return the dotted import path of *path* inside the ``creek_mcp`` package.

    Args:
        path: A ``.py`` file somewhere under *package_root*.
        package_root: The ``creek_mcp`` package directory.

    Returns:
        e.g. ``creek_mcp.tools.purge``; a package's ``__init__.py`` maps to
        the package itself.
    """
    parts = list(path.relative_to(package_root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _elevated_env_readers() -> set[tuple[str, str]]:
    """Return every ``(module, function)`` in ``creek_mcp`` naming the env constant.

    Returns:
        One entry per function whose own code mentions
        :data:`creek_mcp.auth.ELEVATED_TOKEN_ENV`. Module-scope statements
        (the definition itself, and ``server.py``'s import of it) are not
        functions and so are not reported.
    """
    package_root = Path(auth.__file__).parent
    readers: set[tuple[str, str]] = set()
    for source_file in sorted(package_root.rglob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        module = _dotted_module_name(source_file, package_root)
        readers.update(
            (module, function.name)
            for function in _functions(tree)
            if _reads_name(function, "ELEVATED_TOKEN_ENV")
        )
    return readers


_ELEVATED_ENV_ALLOWED_MODULES = frozenset({"creek_mcp.auth"})
"""Modules whose functions may read ``CREEK_MCP_ELEVATED_TOKEN`` freely.

Module-wide rather than function-wide on purpose: :mod:`creek_mcp.auth` owns
the throttled gate, and its internals must stay free to move between private
helpers without a rename turning the ratchet below red.
"""

_ELEVATED_ENV_ALLOWLIST = frozenset(
    {("creek_mcp.server", "_require_strong_elevated_token")},
)
"""The one function outside :mod:`creek_mcp.auth` allowed to read the env var.

It reads the configured value at startup to refuse a weak one (#907) and
never compares it against anything a caller supplied, so it is not a
guessing surface and needs no attempt budget.
"""


def test_auth_module_source_uses_compare_digest_not_equality() -> None:
    """Static check: ``auth.py`` must not compare token strings with ``==``.

    ADAPT-004 / FEAT-012: elevated-auth comparisons MUST be constant
    time. Walking the AST is more robust than a textual search — it
    won't false-positive on string literals like ``"=="`` in docstrings.

    The walk covers **every** function in the module rather than only
    ``is_elevated`` (#914). The throttle moves the comparison out of
    ``is_elevated`` and into a helper the attempt budget calls; a guard
    scoped to that one name would have gone quietly vacuous the moment the
    body moved — which is exactly the edit during which a hand-rolled ``==``
    is easiest to introduce.
    """
    for function in _functions(_auth_tree()):
        for node in _own_nodes(function):
            if not isinstance(node, ast.Compare):
                continue
            for op in node.ops:
                if isinstance(op, ast.Eq | ast.NotEq):
                    msg = (
                        f"{function.name} must not use ==/!= on tokens; "
                        "use hmac.compare_digest"
                    )
                    raise AssertionError(msg)


def test_elevated_compare_is_unreachable_outside_the_attempt_budget() -> None:
    """Exactly one function may evaluate the token, and it is not ``is_elevated``.

    The #914 throttle is a bound on guessing only if the comparison is
    unreachable except through :meth:`AttemptBudget.attempt`. That is a
    structural claim — no behavioural test can see a second, unthrottled path
    that no caller happens to take *yet* — so it is asserted structurally:
    one function in ``auth.py`` calls ``hmac.compare_digest``, ``is_elevated``
    is not that function, and ``is_elevated`` delegates through a call to
    ``.attempt``.

    Asserted by count and location rather than by the private helper's name,
    so renaming it is not a spurious red.
    """
    functions = _functions(_auth_tree())

    comparing = sorted(
        f.name for f in functions if _calls_attribute(f, "compare_digest")
    )
    assert len(comparing) == 1, (
        f"exactly one function may compare the elevated token; found {comparing}"
    )
    assert "is_elevated" not in comparing, (
        "is_elevated must delegate the comparison, never perform it — an "
        "in-place compare is an unthrottled guessing surface (#914)"
    )

    gates = [f for f in functions if f.name == "is_elevated"]
    assert len(gates) == 1, "auth.py must define exactly one is_elevated"
    assert _calls_attribute(gates[0], "attempt"), (
        "is_elevated must route every verification through the attempt budget"
    )


def test_only_the_gate_and_the_startup_check_read_the_elevated_env() -> None:
    """No second, unthrottled reader of ``CREEK_MCP_ELEVATED_TOKEN`` (#914).

    Honest framing: **this passes at HEAD.** It is a ratchet, not a red test.
    The throttle lives inside :func:`creek_mcp.auth.is_elevated`, so it bounds
    only the guessing that goes through that function; a second call site that
    read the env var and compared it itself would be an unthrottled oracle
    with nothing to notice it. Two locations are sanctioned — the gate's own
    module, and the startup strength check, which never compares the value to
    caller input.

    The allowlist is also asserted to be *live*, so a rename on the server
    side forces a deliberate review here instead of quietly leaving a stale
    entry that exempts nothing.
    """
    readers = _elevated_env_readers()

    assert readers >= _ELEVATED_ENV_ALLOWLIST, (
        "the allowlist has gone stale — these no longer exist: "
        f"{sorted(_ELEVATED_ENV_ALLOWLIST - readers)}"
    )
    offenders = {
        (module, function)
        for module, function in readers
        if module not in _ELEVATED_ENV_ALLOWED_MODULES
        and (module, function) not in _ELEVATED_ENV_ALLOWLIST
    }
    assert offenders == set(), (
        f"these read the elevated token outside the throttled gate: {sorted(offenders)}"
    )


# ---------------------------------------------------------------------------
# The failed-attempt budget, seen from the gate (#914)
# ---------------------------------------------------------------------------


def _attempt_policy() -> ModuleType:
    """Import :mod:`creek_mcp.attempt_policy` at call time.

    Deferred rather than imported at module scope so this file still
    *collects* while the module does not exist yet: the behavioural throttle
    tests below must fail on their own assertions during the RED window, and
    a top-level ``ModuleNotFoundError`` would turn every test in the file —
    including the eight that predate #914 — into a collection error.

    Returns:
        The attempt-policy module.
    """
    from creek_mcp import attempt_policy

    return attempt_policy


def _arm_the_elevated_lockout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend the whole failure budget, so the gate is locked on return.

    Args:
        monkeypatch: Used to configure a floor-clearing server token first,
            so the failures are genuine mismatches rather than the #907
            weak-configuration deny. The caller is free to reconfigure the
            environment afterwards — the budget does not re-read it.
    """
    policy = _attempt_policy()
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)
    for _ in range(policy.MAX_FAILED_ATTEMPTS):
        assert auth.is_elevated(_WRONG_TOKEN) is False


def test_the_correct_token_is_refused_inside_the_lockout_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five wrong guesses shut the gate — even against the right token (#914).

    Stated on the *authorized* caller because that is the strongest form of
    the bound: if a valid token is refused inside the window, an invalid one
    provably was never evaluated either.

    The count is the literal ``5`` rather than
    ``attempt_policy.MAX_FAILED_ATTEMPTS`` on purpose. During the RED window
    that name does not exist, and reaching for it would make this test die on
    a ``ModuleNotFoundError`` instead of on the assertion that describes the
    defect. ``test_the_gate_reopens_after_the_lockout_window_expires`` below
    is what ties the behaviour back to the named constants.
    """
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)
    for _ in range(5):
        assert auth.is_elevated(_WRONG_TOKEN) is False

    assert auth.is_elevated(_STRONG_TOKEN) is False


def test_one_failure_short_of_the_limit_leaves_the_gate_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator gets every guess the policy promises, then locks.

    An off-by-one that shut the gate at ``MAX_FAILED_ATTEMPTS - 1`` would be
    invisible from the outside — the refusal payload is byte-identical either
    way — and would cost the operator a guess on a surface where the next
    thing they can do is wait a minute.
    """
    policy = _attempt_policy()
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)

    for _ in range(policy.MAX_FAILED_ATTEMPTS - 1):
        assert auth.is_elevated(_WRONG_TOKEN) is False

    assert auth.is_elevated(_STRONG_TOKEN) is True


def test_the_gate_reopens_after_the_lockout_window_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lockout is a delay of exactly ``LOCKOUT_SECONDS``, not a ban.

    Bricking the operator's own gate would be a worse outcome than the
    brute-force channel #914 closes, so the reopen is asserted rather than
    assumed — and both edges of the window are pinned, so its length cannot
    drift in either direction.

    This is also what proves the two named constants are the ones actually in
    force: the gate locks after ``MAX_FAILED_ATTEMPTS`` failures and reopens
    ``LOCKOUT_SECONDS`` later. Time is driven by hand through the module's
    ``_now`` seam, so the test costs microseconds and cannot be made to pass
    by sleeping.
    """
    policy = _attempt_policy()
    clock = FakeMonotonicClock()
    monkeypatch.setattr(policy, "_now", clock)
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _STRONG_TOKEN)
    armed_at = clock.now

    for _ in range(policy.MAX_FAILED_ATTEMPTS):
        assert auth.is_elevated(_WRONG_TOKEN) is False
    assert auth.is_elevated(_STRONG_TOKEN) is False

    clock.now = armed_at + policy.LOCKOUT_SECONDS - 1.0
    assert auth.is_elevated(_STRONG_TOKEN) is False

    clock.now = armed_at + policy.LOCKOUT_SECONDS
    assert auth.is_elevated(_STRONG_TOKEN) is True


_NEVER_RAISES_CASES = [
    pytest.param(None, _STRONG_TOKEN, id="env-unset"),
    pytest.param("", _STRONG_TOKEN, id="env-empty"),
    pytest.param("", None, id="env-empty-and-no-client-token"),
    pytest.param(_WEAK_TOKEN, _WEAK_TOKEN, id="env-weak-exact-match"),
    pytest.param(_STRONG_TOKEN, _WRONG_TOKEN, id="wrong-client-token"),
    pytest.param(_STRONG_TOKEN, None, id="no-client-token"),
    pytest.param(_STRONG_TOKEN, _SURROGATE_TOKEN, id="unencodable-client-token"),
]
"""Every way the gate is expected to say no, as ``(configured, provided)``.

The ``unencodable`` row is the #914 regression case. A lone surrogate
survives ``json.loads``, so it reaches ``auth_token`` straight off the
wire, and ``str.encode`` then raises :exc:`UnicodeEncodeError`. A raise
here is not cosmetic: :func:`creek_mcp.tools.purge._gate` does not catch,
so the exception escapes into the FastMCP tool surface and skips the audit
entry every purge call must write — an unaudited probe of a destructive
tool. Worse, once the budget exists the raise becomes *state-dependent*:
inside a lockout ``verify`` is never called, so the identical input returns
a clean ``False``. That difference is an exact, free oracle for whether the
global budget is armed, and it costs the attacker nothing because the
failure counter is never reached.

Only the caller's token is exercised here. The server side can hold a
surrogate too — ``os.environ`` decodes invalid UTF-8 with
``surrogateescape`` — but ``monkeypatch.setenv`` cannot install one (the
raise lands in ``os.putenv``, not in the code under test), so a row for it
would be testing the harness. The fix guards both encodes regardless.
"""


@pytest.mark.parametrize(
    "inside_window",
    [pytest.param(False, id="unthrottled"), pytest.param(True, id="locked-out")],
)
@pytest.mark.parametrize(("configured", "provided"), _NEVER_RAISES_CASES)
def test_is_elevated_never_raises_and_stays_closed_in_every_state(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    provided: str | None,
    inside_window: bool,
) -> None:
    """The gate answers ``False`` rather than raising, throttled or not (#914).

    :func:`creek_mcp.tools.purge._gate` does not catch, so anything raised
    here escapes into the FastMCP tool surface and skips the audit entry every
    purge call is required to write — a hostile caller could then probe the
    gate and leave no trail. The lockout adds a whole new family of states to
    get that wrong in, so each failing configuration is exercised twice: once
    with the budget untouched, and once from inside an armed window.

    Args:
        monkeypatch: Sets the server-side token for each case.
        configured: ``CREEK_MCP_ELEVATED_TOKEN``, or ``None`` to unset it.
        provided: The caller's ``auth_token``.
        inside_window: Whether to arm the lockout before the call.
    """
    if inside_window:
        _arm_the_elevated_lockout(monkeypatch)
    if configured is None:
        monkeypatch.delenv("CREEK_MCP_ELEVATED_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", configured)

    assert auth.is_elevated(provided) is False
