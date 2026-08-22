"""Transport-neutral admission policy for the MCP/HTTP surface (#1073).

:mod:`creek_mcp.policy` is the one place that answers two questions for
*every* caller, no matter which transport carried the call:

1. *May this caller request this tier ceiling?* --
   :func:`~creek_mcp.policy.admitted_ceiling`;
2. *Whose identity does this call get audited under?* --
   :func:`~creek_mcp.policy.effective_consumer`.

Before #1073 both answers lived inside ``creek_mcp.server``, keyed off the
MCP SDK's request-scoped ``get_access_token()``. That coupling is the bug:
"remote" meant "carries an MCP access token", so a caller arriving over the
``/v1`` HTTP application API -- which has no MCP request context at all and
therefore no access token -- read as *local*, and the ``personal`` cap that
makes intimate content unreachable over the network silently did not apply.

So the load-bearing property of this module is what it does **not** touch:
``is_remote`` is a plain bool the adapter asserts, not something policy
sniffs out of a transport. These tests hold that line two ways --

- **not a single test here constructs a transport.** There is no
  ``monkeypatch`` of ``get_access_token``, no ``FastMCP`` instance, no vault
  fixture. A remote caller is one line: ``CallerIdentity(consumer=...,
  is_remote=True)``. If policy ever grows a hidden dependency on the MCP
  request context, these tests stop passing, because there is no context
  here to satisfy it;
- **the import lists are the proof.** Two AST guards pin that
  ``creek_mcp/policy.py`` imports no ``mcp`` / ``fastmcp`` / ``starlette``
  root, and that *this* module imports neither the SDK nor
  ``creek_mcp.server``. Each guard is paired with a negative case so it
  cannot rot into a green no-op.

This module must never import ``creek_mcp.server``: importing it would drag
in FastMCP, the tool registry and the SDK's request context, and a test that
can reach the transport can no longer testify that policy does not.

Companion pins for the *server-side* behaviour of the same cap live in
``tests/test_mcp_remote.py`` and stay there; nothing here duplicates them.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
from dataclasses import MISSING, FrozenInstanceError, fields
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from creek_mcp import policy
from creek_mcp.policy import (
    REMOTE_ADMITTED_CEILINGS,
    REMOTE_CEILING_REFUSAL_REASON,
    Admission,
    CallerIdentity,
    Refusal,
    admitted_ceiling,
    effective_consumer,
)
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from collections.abc import Callable


# --------------------------------------------------------------------------- #
# Fixtures-by-construction and the request tables
# --------------------------------------------------------------------------- #

# A remote caller is a value, not a transport: one dataclass, no MCP context.
_REMOTE = CallerIdentity(consumer="adepthood", is_remote=True)
_LOCAL = CallerIdentity(consumer=None, is_remote=False)

# The consumer a *local* stdio call is audited under (the process-global
# CREEK_MCP_CONSUMER, passed in by the adapter rather than read here).
_LOCAL_DEFAULT = "creek-cli"

# Hard-coded on purpose. Asserting REMOTE_CEILING_REFUSAL_REASON against an
# import of itself proves nothing; this literal is the published wording, and
# a reword has to be a deliberate edit in two places.
_PUBLISHED_REFUSAL_REASON = (
    "remote consumers may not request a ceiling above 'personal'; "
    "intimate content is not reachable over the network"
)

# Values that do not parse as a TierCeiling at all. Wrong case, surrounding
# whitespace and every non-string scalar or container included -- the last two
# entries are unhashable on purpose (see the hashing test below).
_UNPARSEABLE_REQUESTS: tuple[object, ...] = (
    None,
    "",
    "OPEN",
    "Personal",
    " open",
    "open ",
    "bogus-tier",
    "unclassified",
    0,
    1,
    True,
    False,
    3.5,
    [],
    {},
)
_UNPARSEABLE_IDS: tuple[str, ...] = (
    "none",
    "empty-string",
    "upper-open",
    "title-personal",
    "leading-space-open",
    "trailing-space-open",
    "bogus-tier",
    "unclassified",
    "int-zero",
    "int-one",
    "true",
    "false",
    "float",
    "empty-list",
    "empty-dict",
)

# Everything a remote caller is refused: the two over-ceiling tiers (as strings
# and as enum members) plus every unparseable value.
_REMOTE_REFUSED_REQUESTS: tuple[object, ...] = (
    "intimate",
    "all",
    TierCeiling.INTIMATE,
    TierCeiling.ALL,
    *_UNPARSEABLE_REQUESTS,
)
_REMOTE_REFUSED_IDS: tuple[str, ...] = (
    "intimate-str",
    "all-str",
    "intimate-member",
    "all-member",
    *_UNPARSEABLE_IDS,
)

# The admitted half of the remote table, in both accepted spellings.
_REMOTE_ADMITTED_CASES: tuple[tuple[object, TierCeiling], ...] = (
    ("open", TierCeiling.OPEN),
    ("personal", TierCeiling.PERSONAL),
    (TierCeiling.OPEN, TierCeiling.OPEN),
    (TierCeiling.PERSONAL, TierCeiling.PERSONAL),
)
_REMOTE_ADMITTED_IDS: tuple[str, ...] = (
    "open-str",
    "personal-str",
    "open-member",
    "personal-member",
)

# A local caller may request every ceiling. Derived from the enum so a new
# member is covered the day it is added; the coverage test below pins that the
# derivation actually reaches INTIMATE and ALL.
_LOCAL_ADMITTED_CASES: tuple[tuple[object, TierCeiling], ...] = tuple(
    (requested_spelling, member)
    for member in TierCeiling
    for requested_spelling in (member.value, member)
)
_LOCAL_ADMITTED_IDS: tuple[str, ...] = tuple(
    f"{member.name.lower()}-{spelling}"
    for member in TierCeiling
    for spelling in ("str", "member")
)


# --------------------------------------------------------------------------- #
# AST helpers -- each paired with a not-vacuous companion test further down
# --------------------------------------------------------------------------- #


def _imported_roots(source: str) -> set[str]:
    """Return the top-level module names *source* imports, by AST alone.

    Mirrors the sweep in ``tests/test_adepthood_contract_models.py``: walk
    every ``import x.y`` and ``from x.y import z`` and keep ``x``. Reading the
    source rather than the imported module's ``__dict__`` is deliberate -- a
    lazily-imported SDK inside a function body is still an import, and this
    finds it.

    Args:
        source: Python source text.

    Returns:
        The distinct top-level module roots the source imports.
    """
    tree = ast.parse(source)
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    return roots


def _imported_module_paths(source: str) -> set[str]:
    """Return the fully dotted module paths *source* imports, by AST alone.

    Where :func:`_imported_roots` answers "which packages?", this answers
    "which modules?" -- needed because ``creek_mcp.policy`` and
    ``creek_mcp.server`` share a root. ``from creek_mcp import server``
    contributes both ``creek_mcp`` and ``creek_mcp.server``, so a submodule
    pulled in through its package is caught too.

    Args:
        source: Python source text.

    Returns:
        Every dotted path named by an ``import`` or ``from ... import ...``.
    """
    tree = ast.parse(source)
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            paths.add(node.module)
            paths.update(f"{node.module}.{alias.name}" for alias in node.names)
    return paths


def _policy_source() -> str:
    """Return the on-disk source of :mod:`creek_mcp.policy`."""
    return inspect.getsource(policy)


def _this_module_source() -> str:
    """Return the on-disk source of this test module."""
    return Path(__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Group 1 -- the bypass: a remote caller with no MCP context whatsoever
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "requested",
    ["intimate", "all", TierCeiling.INTIMATE, TierCeiling.ALL, "bogus-tier"],
    ids=["intimate-str", "all-str", "intimate-member", "all-member", "garbage"],
)
def test_remote_caller_with_no_mcp_context_is_refused_over_ceiling(
    requested: object,
) -> None:
    """The test that would have caught the ``/v1`` bypass.

    This simulates an HTTP ``/v1`` caller. Such a caller arrives over the
    Adepthood application API and carries **no MCP access token** -- there is
    no MCP request context in scope at all, which is exactly what the absence
    of any monkeypatch, any ``get_access_token`` stub and any
    ``creek_mcp.server`` import in this file represents.

    Under the pre-#1073 code the remote cap was keyed off
    ``_current_access_token() is not None``, so for this caller it read
    ``None``, concluded "local", and never engaged: ``intimate`` and ``all``
    would have been *admitted over the network*, which is the one thing the
    cap exists to prevent. Here remoteness is asserted by the caller
    (``is_remote=True``), not sniffed from a transport, so the refusal holds
    with no context to sniff.

    Args:
        requested: The ceiling the remote caller asked for.
    """
    result = admitted_ceiling(_REMOTE, requested)
    assert isinstance(result, Refusal)
    assert not isinstance(result, Admission)
    assert result.reason == REMOTE_CEILING_REFUSAL_REASON


# --------------------------------------------------------------------------- #
# Group 2 -- the remote admission table, refused half
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "requested",
    _REMOTE_REFUSED_REQUESTS,
    ids=_REMOTE_REFUSED_IDS,
)
def test_remote_refuses_every_non_admitted_request(requested: object) -> None:
    """Pin the whole refused half of the remote table, value for value.

    Only the exact strings ``open`` and ``personal`` (or their enum members)
    get through. The over-ceiling tiers, wrong case, leading or trailing
    whitespace, ``None``, the empty string, unknown tier names and every
    non-string scalar or container are all one outcome: a
    :class:`~creek_mcp.policy.Refusal` carrying the single published reason.
    Equality against a freshly built ``Refusal`` also pins that the refusal
    carries nothing else -- no echo of the rejected request.

    Args:
        requested: The value the remote caller supplied as its ceiling.
    """
    expected = Refusal(reason=REMOTE_CEILING_REFUSAL_REASON)
    assert admitted_ceiling(_REMOTE, requested) == expected


def test_remote_refused_table_covers_every_non_admitted_ceiling() -> None:
    """The refused table is total over the ceilings the cap excludes.

    A table of hand-written literals rots the moment ``TierCeiling`` grows a
    member: the sweep above would keep passing while the new tier went
    untested. This derives the expectation from the enum and the admitted set
    instead, so adding a fifth ceiling fails here until someone lists it --
    and, because the comparison is an equality, it also catches a table that
    wrongly starts refusing ``open`` or ``personal``.
    """
    covered = {
        member
        for member in TierCeiling
        if any(request == member for request in _REMOTE_REFUSED_REQUESTS)
    }
    assert covered == set(TierCeiling) - set(REMOTE_ADMITTED_CEILINGS)


def test_every_remote_refusal_carries_the_identical_constant_reason() -> None:
    """One reason for every refused input -- the refusal cannot be a channel.

    If the reason were derived from the request (interpolated, echoed,
    "``bogus-tier`` is not a valid ceiling"), a remote caller could read its
    own rejected input back out, and a refusal could start leaking which tier
    a piece of content actually is. Collapsing the whole refused table to a
    one-element set is the assertion that no such variation exists.
    """
    reasons: set[str] = set()
    for requested in _REMOTE_REFUSED_REQUESTS:
        result = admitted_ceiling(_REMOTE, requested)
        assert isinstance(result, Refusal)
        reasons.add(result.reason)
    assert reasons == {_PUBLISHED_REFUSAL_REASON}


# --------------------------------------------------------------------------- #
# Group 3 -- the remote admission table, admitted half
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("requested", "expected"),
    _REMOTE_ADMITTED_CASES,
    ids=_REMOTE_ADMITTED_IDS,
)
def test_remote_admits_open_and_personal(
    requested: object,
    expected: TierCeiling,
) -> None:
    """``open`` and ``personal`` pass the cap, in either spelling.

    The cap is a ceiling, not a blanket denial: the two tiers a network
    consumer is entitled to keep working, and the admission names the parsed
    enum member so the caller downstream never re-parses the raw value.

    Args:
        requested: The ceiling as the caller spelled it (string or member).
        expected: The ``TierCeiling`` member the admission must carry.
    """
    result = admitted_ceiling(_REMOTE, requested)
    assert result == Admission(ceiling=expected)
    assert isinstance(result, Admission)
    assert result.ceiling is expected


# --------------------------------------------------------------------------- #
# Group 4 -- the local (stdio) half: uncapped, and not policy's to validate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("requested", "expected"),
    _LOCAL_ADMITTED_CASES,
    ids=_LOCAL_ADMITTED_IDS,
)
def test_local_admits_every_ceiling_including_intimate_and_all(
    requested: object,
    expected: TierCeiling,
) -> None:
    """A local stdio caller is not capped -- ``intimate`` and ``all`` included.

    The cap exists because the *network* is the boundary, not because
    ``intimate`` is unreadable. The operator at their own machine keeps full
    reach, so every member parses to an admission naming itself.

    Args:
        requested: The ceiling as the caller spelled it (string or member).
        expected: The ``TierCeiling`` member the admission must carry.
    """
    result = admitted_ceiling(_LOCAL, requested)
    assert result == Admission(ceiling=expected)
    assert isinstance(result, Admission)
    assert result.ceiling is expected


def test_local_admitted_cases_cover_intimate_and_all() -> None:
    """The local table is derived from the enum and really does reach the top.

    ``_LOCAL_ADMITTED_CASES`` is a comprehension over ``TierCeiling``; if the
    derivation ever narrowed, the sweep above would shrink silently and still
    pass. Naming ``INTIMATE`` and ``ALL`` explicitly keeps the test saying
    something even then -- they are the two the remote cap withholds and the
    two whose local admission is the point.
    """
    covered = {expected for _, expected in _LOCAL_ADMITTED_CASES}
    assert covered == set(TierCeiling)
    assert TierCeiling.INTIMATE in covered
    assert TierCeiling.ALL in covered


@pytest.mark.parametrize("requested", _UNPARSEABLE_REQUESTS, ids=_UNPARSEABLE_IDS)
def test_local_unparseable_request_is_an_uncapped_admission_not_a_refusal(
    requested: object,
) -> None:
    """Garbage from a local caller is *not* policy's to refuse.

    ``Admission(ceiling=None)`` states "policy imposes no cap; this value is
    not policy's to validate". The value then falls through to the adapter's
    schema layer -- FastMCP's pydantic coercion of the
    ``privacy_tier_ceiling: TierCeiling`` parameter -- which is what raises on
    it, and which is where argument-shape validation belongs.

    This asymmetry is deliberate and load-bearing. Turning it into a
    ``Refusal`` would move schema validation into the security policy and
    change today's local behaviour; turning it into a *ceiling* would be
    worse. The ``not isinstance(..., Refusal)`` assertion is what stops a
    future "tidy-up" from collapsing the two halves of the table into one.

    Args:
        requested: A value that does not parse as a ``TierCeiling``.
    """
    result = admitted_ceiling(_LOCAL, requested)
    assert isinstance(result, Admission)
    assert not isinstance(result, Refusal)
    assert result.ceiling is None
    assert result == Admission(ceiling=None)


@pytest.mark.parametrize(
    "requested",
    [[], {}],
    ids=["empty-list", "empty-dict"],
)
def test_unhashable_requests_are_decided_without_hashing(requested: object) -> None:
    """An unhashable request must not blow up the admission lookup.

    ``REMOTE_ADMITTED_CEILINGS`` is a ``frozenset``, and ``[] in frozenset()``
    raises ``TypeError``. So the implementation may not test membership on the
    caller's raw value -- it has to parse first (``TierCeiling(...)``, which
    handles unhashable input and raises ``ValueError``) and only then consult
    the admitted set. A caller must never be able to turn a refusal into a
    500 by passing a list.

    Args:
        requested: An unhashable value posing as a ceiling.
    """
    refusal = Refusal(reason=REMOTE_CEILING_REFUSAL_REASON)
    assert admitted_ceiling(_REMOTE, requested) == refusal
    assert admitted_ceiling(_LOCAL, requested) == Admission(ceiling=None)


# --------------------------------------------------------------------------- #
# Group 5 -- the published constants
# --------------------------------------------------------------------------- #


def test_remote_admitted_ceilings_is_exactly_open_and_personal() -> None:
    """The admitted set is an equality, so it cannot be widened quietly."""
    expected = frozenset({TierCeiling.OPEN, TierCeiling.PERSONAL})
    assert expected == REMOTE_ADMITTED_CEILINGS


def test_intimate_and_all_are_never_remote_admitted() -> None:
    """Name the two exclusions, so widening by one member still fails a test.

    The equality above is the strict check, but it fails identically for a
    harmless reordering and for the one change that actually matters. These
    two memberships say *which* widening is the security regression.
    """
    assert TierCeiling.INTIMATE not in REMOTE_ADMITTED_CEILINGS
    assert TierCeiling.ALL not in REMOTE_ADMITTED_CEILINGS


def test_refusal_reason_is_the_exact_published_literal() -> None:
    """The refusal wording is a contract with the cross-repo consumer.

    Compared against a literal spelled out in this file, never against an
    import of the constant itself -- a test that compares a constant to itself
    passes no matter how the constant is reworded.
    """
    assert REMOTE_CEILING_REFUSAL_REASON == _PUBLISHED_REFUSAL_REASON


def test_refusal_reason_is_not_a_format_string() -> None:
    """The reason may never be a template waiting for a caller's value (#1090).

    A brace or a percent placeholder in this constant is how an echo gets
    introduced later: someone adds ``.format(requested)`` at one call site and
    the refusal starts carrying the caller's input -- or, worse, a fragment's
    tier.
    """
    assert "{" not in REMOTE_CEILING_REFUSAL_REASON
    assert "}" not in REMOTE_CEILING_REFUSAL_REASON
    assert "%" not in REMOTE_CEILING_REFUSAL_REASON


def test_refusal_reason_names_no_specific_content() -> None:
    """The reason describes the *rule*, never a piece of content.

    It may say the word ``intimate`` -- that is the rule's name. It may not
    carry a fragment id, a vault path, a filename or a number, because a
    refusal that names what it refused has already leaked it.
    """
    reason = REMOTE_CEILING_REFUSAL_REASON
    assert "/" not in reason
    assert "\\" not in reason
    assert ".md" not in reason
    assert not any(char.isdigit() for char in reason)


# --------------------------------------------------------------------------- #
# Group 6 -- CallerIdentity is fail-closed by construction
# --------------------------------------------------------------------------- #


def test_caller_identity_requires_both_fields_explicitly() -> None:
    """Constructing an identity with no arguments raises ``TypeError``.

    The whole design rests on ``is_remote`` being asserted by the adapter. A
    default -- ``is_remote: bool = False`` -- would make a forgotten field
    fail **open**: a new caller wired up by someone who did not know about the
    flag would be treated as local and uncapped, which is precisely the #1073
    bug reintroduced by omission.

    The call goes through a ``Callable[..., object]`` alias so the omission is
    a runtime fact rather than a static error mypy would reject at the call
    site (no ``# type: ignore`` is permitted here).
    """
    constructor: Callable[..., object] = CallerIdentity
    with pytest.raises(TypeError):
        constructor()


def test_caller_identity_fields_declare_no_defaults() -> None:
    """Neither field carries a default or a default factory.

    More precise than the ``TypeError`` above, which would still be raised if
    only *one* field lost its default. This reads the dataclass metadata
    directly, so a default added to either field is caught by name.
    """
    declared = {field.name: field for field in fields(CallerIdentity)}
    assert set(declared) == {"consumer", "is_remote"}
    for field in declared.values():
        assert field.default is MISSING
        assert field.default_factory is MISSING


def test_caller_identity_is_frozen() -> None:
    """An identity cannot be mutated after construction.

    A mutable identity is a time-of-check/time-of-use hole: a handler could
    flip ``is_remote`` to ``False`` between the adapter's assertion and the
    ceiling decision. The attribute name is held in a variable so the
    assignment is dynamic -- mypy rejects a literal assignment to a frozen
    dataclass field, and suppressions are not allowed here.
    """
    identity = CallerIdentity(consumer="adepthood", is_remote=True)
    attribute = "is_remote"
    with pytest.raises(FrozenInstanceError):
        setattr(identity, attribute, False)
    assert identity.is_remote is True


# --------------------------------------------------------------------------- #
# Group 7 -- effective_consumer: who the call is audited under
# --------------------------------------------------------------------------- #


def test_effective_consumer_is_the_token_identity_when_remote() -> None:
    """A remote call is audited under the identity its credential names.

    Not the process-global default: the audit log has to be able to say
    *which* network consumer made the call.
    """
    identity = CallerIdentity(consumer="adepthood", is_remote=True)
    assert effective_consumer(identity, _LOCAL_DEFAULT) == "adepthood"


def test_effective_consumer_is_the_process_default_when_local() -> None:
    """A local call keeps the process default even if an identity is attached.

    This is the direction that could go wrong silently. If a stale or
    carried-over ``consumer`` on a *local* identity started winning, local
    stdio calls would begin appearing in the audit log attributed to a network
    consumer -- misattribution nobody would notice until it mattered. So the
    identity here deliberately carries a non-``None`` consumer that must be
    ignored.
    """
    identity = CallerIdentity(consumer="adepthood", is_remote=False)
    assert effective_consumer(identity, _LOCAL_DEFAULT) == _LOCAL_DEFAULT


def test_effective_consumer_never_falls_back_to_the_default_when_remote() -> None:
    """A remote call may never be attributed to the local process default.

    ``is_remote=True`` with no consumer should not be constructible in
    practice -- the adapter has a credential before it asserts remoteness --
    so this pins only the property that matters rather than a specific
    outcome: raising ``ValueError`` (fail closed) and returning ``None`` are
    both acceptable answers, and the assertion runs either way. The one
    forbidden answer is the quiet ``consumer or default`` fallback, which
    would stamp a network call with the operator's own local identity.
    """
    identity = CallerIdentity(consumer=None, is_remote=True)
    outcomes: list[object] = []
    with contextlib.suppress(ValueError):
        outcomes.append(effective_consumer(identity, _LOCAL_DEFAULT))
    assert _LOCAL_DEFAULT not in outcomes


def test_effective_consumer_still_raises_only_on_a_missing_consumer() -> None:
    """The blank-name refusal stays at the verifier boundary, not here (#1100).

    ``effective_consumer`` is called from inside the tool wrapper, at every
    ``consumer=_effective_consumer(...)`` site, so raising here escapes into
    the FastMCP tool surface and *skips the audit entry the call was going to
    write* — losing the trail for precisely the call whose attribution is
    suspect. #1100 therefore refuses a nameless credential where it is issued
    (``creek_mcp.remote_auth``) and where it is presented
    (``creek_mcp.httpapi.auth``), and leaves this function's contract exactly
    as it was: ``None`` raises, ``""`` does not.

    Pinned as a test because the tempting one-line "fix" is to widen the
    ``is None`` here, and that would be a regression dressed as a hardening.
    """
    with pytest.raises(ValueError, match="must name the consumer"):
        effective_consumer(
            CallerIdentity(consumer=None, is_remote=True), _LOCAL_DEFAULT
        )
    unnamed = CallerIdentity(consumer="", is_remote=True)
    assert effective_consumer(unnamed, _LOCAL_DEFAULT) == ""


# --------------------------------------------------------------------------- #
# Group 8 -- the transport-neutrality guards (and their negative cases)
# --------------------------------------------------------------------------- #


def test_policy_module_imports_no_mcp_sdk() -> None:
    """``creek_mcp/policy.py`` may not import the MCP SDK or a web framework.

    This is the structural half of #1073. Policy that can reach
    ``get_access_token`` will eventually be *tempted* to, and the resulting
    "is this remote?" answer is exactly the one that was wrong for ``/v1``.
    Keeping the SDK out of the module's import list makes the temptation
    unavailable: ``is_remote`` can only come from the caller.

    ``creek_mcp`` is a fine root to see here -- the module lives in that
    package and reads ``TierCeiling`` from it. The excluded root is the SDK's
    own ``mcp``. ``dataclasses`` is asserted present as a non-vacuity anchor:
    it proves the source was actually parsed and its imports found, rather
    than an empty set trivially satisfying every exclusion.
    """
    roots = _imported_roots(_policy_source())
    assert "dataclasses" in roots
    assert "mcp" not in roots
    assert "fastmcp" not in roots
    assert "starlette" not in roots


def test_imported_roots_detects_an_sdk_import() -> None:
    """The SDK guard above is not vacuous.

    A reflection guard fails open when its discovery stops matching: the sweep
    finds nothing, every ``not in`` holds, and the test stays green while the
    invariant rots. So feed the helper a source that *does* import the SDK --
    both spellings -- and pin that it is found.
    """
    source = "import mcp.server.fastmcp\nfrom mcp.types import ContentBlock\n"
    assert _imported_roots(source) == {"mcp"}


def test_this_test_module_does_not_import_the_mcp_server() -> None:
    """This module's own import list is the proof of transport-neutrality.

    Every assertion in this file about a "remote" caller is only meaningful if
    no transport is in scope to have been consulted. ``creek_mcp.server``
    carries the FastMCP instance, the tool registry and the access-token
    wrapper; importing it -- even to borrow one constant -- would put a
    request context within reach and hollow out the group-1 test.
    """
    imported = _imported_module_paths(_this_module_source())
    assert "creek_mcp.policy" in imported
    assert "creek_mcp.server" not in imported
    assert not any(name.startswith("creek_mcp.server.") for name in imported)


def test_imported_module_paths_detects_a_server_import() -> None:
    """The server guard above is not vacuous either.

    Both ways of reaching the module are pinned: the direct ``import
    creek_mcp.server`` and the package-relative ``from creek_mcp import
    server``, which is how it would most likely creep back in.
    """
    source = "import creek_mcp.server\nfrom creek_mcp import server\n"
    assert "creek_mcp.server" in _imported_module_paths(source)


def test_this_test_module_imports_no_transport_machinery() -> None:
    """Nor does this module import the SDK or a web framework at all.

    Not even for a type annotation. A test file that can build an
    ``AccessToken`` or a ``TestClient`` is one refactor away from reaching for
    one, at which point it stops testing policy and starts testing transport
    again -- which ``tests/test_mcp_remote.py`` already does, deliberately, on
    the other side of this boundary.
    """
    roots = _imported_roots(_this_module_source())
    assert "creek_mcp" in roots
    assert "mcp" not in roots
    assert "fastmcp" not in roots
    assert "starlette" not in roots
    assert "uvicorn" not in roots
