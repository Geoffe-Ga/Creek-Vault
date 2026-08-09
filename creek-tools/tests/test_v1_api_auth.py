"""``/v1`` fails closed before it says anything at all (#1074).

The ADR's rule is short: *missing or invalid credentials fail closed with no
capability disclosure*. The ``401`` body is ``{code, message, request_id}`` and
nothing else — no versions, no ``capabilities``, no ``vault``, no ``tier``.

What makes that hard to hold is not the body of any single refusal; it is that
**every** refusal has to be the same one. An unauthenticated caller who can
tell ``GET /v1/wheel`` from ``GET /v1/nonsense`` has learned the route table.
One who can tell ``PUT /v1/journal-entries/abc`` from ``DELETE
/v1/journal-entries/abc`` has learned which methods this deployment serves. One
who can tell a *malformed* ``Authorization`` header from an *unknown* token has
learned that some token shapes are recognised — a probe that narrows a search
space without ever presenting a valid credential. So the load-bearing test in
this module is not "a 401 has three fields": it is that the refusal is
**byte-identical across the whole cross-product**, once ``request_id`` is
blanked, headers included.

The rest of the module pins the credential boundary itself. ``/v1`` reuses
:class:`creek_mcp.remote_auth.ConsumerTokenVerifier` and
:data:`creek_mcp.token_policy.MIN_TOKEN_LEN` — the ADR forbids a second token
registry or a second length floor, because both are places where the MCP and
HTTP surfaces would drift out of lock-step. The floor is therefore
parametrized on ``MIN_TOKEN_LEN`` itself rather than on a copy of ``32``: a
test that hard-coded the number would go green against an adapter that had
grown its own, weaker floor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.httpapi.auth import build_verifier
from creek_mcp.remote_auth import CONSUMER_TOKENS_ENV
from creek_mcp.token_policy import MIN_TOKEN_LEN
from tests.v1_api_support import (
    CONSUMER,
    HEALTH_PATH,
    OTHER_CONSUMER,
    OTHER_TOKEN,
    STRONG_TOKEN,
    UNKNOWN_TOKEN,
    WWW_AUTHENTICATE,
    blank_request_id,
    client,
    contains_a_path,
    envelope,
    header_items,
    headers,
    seed_vault,
    spy_admitted_ceiling,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_UNAUTHENTICATED_STATUS: Final[int] = 401

# The whole reachable surface, plus two paths that are not routes at all. An
# unauthenticated caller must not be able to tell any of them apart.
_PROBE_PATHS: Final[tuple[str, ...]] = (
    "/v1/capabilities",
    "/v1/health",
    "/v1/wheel",
    "/v1/reflections",
    "/v1/journal-entries/abc",
    "/v1/nonsense",
    "/",
)

_PROBE_METHODS: Final[tuple[str, ...]] = ("GET", "POST", "PUT", "DELETE")

# Every word a capability-disclosing 401 would carry. ``0.4``, ``0.3`` and
# ``0.2`` cover all three served contract minors, ``capabilit`` covers both
# spellings, ``tier`` covers the ceiling model.
_FORBIDDEN_IN_401: Final[tuple[str, ...]] = (
    "0.4",
    "0.3",
    "0.2",
    "contract_version",
    "ontology",
    "capabilit",
    "vault",
    "tier",
)

# Every ``Authorization`` value that is not a valid bearer. The lowercase
# ``bearer`` spelling is deliberately in this list: see
# ``test_malformed_authorization_values_are_the_same_refusal``.
# The non-ASCII entry is ``bytes`` rather than ``str`` because it has to be:
# httpx encodes a ``str`` header value as ASCII and raises ``UnicodeEncodeError``
# before the request is ever built, so a ``str`` here would test the client
# library instead of the server. Sending the raw UTF-8 bytes puts the header on
# the wire exactly as a hostile caller would, which is the case that matters —
# ``hmac.compare_digest`` raises ``TypeError`` on a non-ASCII ``str`` (#776), so
# a comparison done on ``str`` would turn a refusal into a 500.
_MALFORMED_AUTHORIZATION: Final[tuple[str | bytes, ...]] = (
    "",
    "Bearer",
    "Bearer ",
    "Basic abc",
    f"bearer {STRONG_TOKEN}",
    f"Bearer {STRONG_TOKEN}x",
    "Bearer nön-ascii-tökén-that-is-long-enough-to-be-a-token".encode(),
    f"Bearer {STRONG_TOKEN} {OTHER_TOKEN}",
)

_MALFORMED_IDS: Final[tuple[str, ...]] = (
    "empty",
    "scheme-only",
    "scheme-and-space",
    "basic-scheme",
    "lowercase-scheme",
    "valid-token-plus-suffix",
    "non-ascii",
    "two-tokens",
)

# 45 chars. The token that replaces ``STRONG_TOKEN`` during a rotation window
# (#895). Test literal, not a real credential.
_ROTATION_TOKEN: Final[str] = "unit-test-rotation-token-" + "d" * 20


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the auth tests."""
    yield seed_vault(tmp_path)


# --------------------------------------------------------------------------- #
# The load-bearing one: every unauthenticated refusal is the same refusal
# --------------------------------------------------------------------------- #


def test_every_unauthenticated_refusal_is_byte_identical(vault: Path) -> None:
    """One refusal, whatever was probed and however it was probed.

    Fifty-six probes: seven paths (five real routes, one unrouted ``/v1``
    path, and ``/``) times four methods times two credential states (absent,
    and a well-formed-but-unknown bearer). If any two of them differ in the
    body, or in a header **name or value**, an unauthenticated caller has a
    distinguishing oracle over the route table.

    Header *values* are in the comparison, not only names (#1117 review). The
    header block of a ``401`` carries the bearer challenge, the standing
    ``Vary``, the content type and the content length — and the last of those
    is a byte count over the body, so a refusal that varied with the route
    would show up there even if every name matched. Comparing names alone
    would have called that identical.

    The sweep is one test rather than a parametrize on purpose: the property
    is cross-case identity, which no per-case assertion can express.

    Args:
        vault: A seeded vault, so nothing here fails for want of one.
    """
    bodies: set[str] = set()
    header_sets: set[frozenset[tuple[str, str]]] = set()
    with client(vault_path=vault) as test_client:
        for path in _PROBE_PATHS:
            for method in _PROBE_METHODS:
                for token in (None, UNKNOWN_TOKEN):
                    response = test_client.request(
                        method, path, headers=headers(token=token, minor=None)
                    )
                    assert response.status_code == _UNAUTHENTICATED_STATUS, (
                        f"{method} {path} token={token!r}"
                    )
                    bodies.add(repr(blank_request_id(envelope(response))))
                    header_sets.add(header_items(response))
    assert len(bodies) == 1
    assert len(header_sets) == 1


def test_the_unauthenticated_probe_matrix_is_not_vacuous() -> None:
    """The sweep above really does cover the whole cross-product.

    A guard built from a loop fails open when the loop shrinks: if
    ``_PROBE_PATHS`` were emptied, every assertion inside would simply never
    run and the test would stay green. Pin the arity.
    """
    expected = len(_PROBE_PATHS) * len(_PROBE_METHODS) * 2
    assert expected == 56
    assert "/v1/nonsense" in _PROBE_PATHS
    assert "DELETE" in _PROBE_METHODS


def test_the_unauthenticated_envelope_is_exactly_three_fields(vault: Path) -> None:
    """The ``401`` body is the published envelope and nothing more.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get("/v1/capabilities", headers=headers(token=None))
    body = envelope(response)
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == ErrorCode.UNAUTHENTICATED.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.UNAUTHENTICATED]
    assert body["request_id"]


@pytest.mark.parametrize("forbidden", _FORBIDDEN_IN_401)
def test_the_unauthenticated_body_discloses_nothing(
    vault: Path, forbidden: str
) -> None:
    """No version, ontology, capability, vault or tier word reaches a ``401``.

    Anything on this list is a fact about the *server*, and the caller has not
    yet proved they are entitled to any fact about the server.

    Args:
        vault: A seeded vault.
        forbidden: A substring that must not appear.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get("/v1/capabilities", headers=headers(token=None))
    assert forbidden.lower() not in response.text.lower()


def test_the_unauthenticated_body_carries_no_path(vault: Path) -> None:
    """Neither the vault path nor the probed request path is echoed back.

    Echoing the request path would defeat the uniformity above one probe at a
    time; echoing the vault path hands a remote caller the server's filesystem
    layout.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(
            "/v1/journal-entries/abc", headers=headers(token=None)
        )
    assert str(vault) not in response.text
    assert not contains_a_path(response.text)


# --------------------------------------------------------------------------- #
# The challenge header
# --------------------------------------------------------------------------- #


def test_www_authenticate_is_the_published_challenge(vault: Path) -> None:
    """``WWW-Authenticate`` is exactly ``Bearer realm="creek"``.

    The realm names the *service*, never the vault: a realm carrying a path or
    a vault name would leak through the one header a ``401`` is obliged to
    send.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get("/v1/capabilities", headers=headers(token=None))
    challenge = response.headers["WWW-Authenticate"]
    assert challenge == WWW_AUTHENTICATE
    assert str(vault) not in challenge
    assert "vault" not in challenge.lower()
    assert not contains_a_path(challenge)


@pytest.mark.parametrize("authorization", _MALFORMED_AUTHORIZATION, ids=_MALFORMED_IDS)
def test_malformed_authorization_values_are_the_same_refusal(
    vault: Path, authorization: str | bytes
) -> None:
    """Every malformed credential gets the identical ``401``.

    Including ``bearer <valid token>``. **The scheme is matched
    case-sensitively**, deliberately: RFC 7235 makes it case-insensitive, but
    every normalisation step is a step where two equal-looking headers stop
    being equal, and ``/v1`` has exactly one client whose one spelling is
    ``Bearer``. Refusing the other spellings costs that client nothing and
    keeps the parse total. This is a narrowing, so it is pinned rather than
    left to whichever way the parser happens to fall.

    ``Bearer <valid>x``, the two-token value and the non-ASCII value are here
    for the same reason: a near-miss must be indistinguishable from a
    nonsense one, or the refusal becomes a suffix oracle.

    Args:
        vault: A seeded vault.
        authorization: The raw header value under test.
    """
    with client(vault_path=vault) as test_client:
        refused = test_client.get(
            "/v1/capabilities", headers={"Authorization": authorization}
        )
        baseline = test_client.get("/v1/capabilities", headers=headers(token=None))
    assert refused.status_code == _UNAUTHENTICATED_STATUS
    assert blank_request_id(envelope(refused)) == blank_request_id(envelope(baseline))
    assert header_items(refused) == header_items(baseline)


# --------------------------------------------------------------------------- #
# The other side of the boundary: a valid credential
# --------------------------------------------------------------------------- #


def test_a_valid_bearer_is_not_refused(vault: Path) -> None:
    """The suite's own token really does authenticate.

    Without this the whole module could be green against an adapter that
    refuses *everything*, which is fail-closed but useless.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())
    assert response.status_code != _UNAUTHENTICATED_STATUS
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("token", "expected_consumer"),
    [(STRONG_TOKEN, CONSUMER), (OTHER_TOKEN, OTHER_CONSUMER)],
    ids=[CONSUMER, OTHER_CONSUMER],
)
def test_each_token_authenticates_as_its_own_consumer(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    expected_consumer: str,
) -> None:
    """The authenticated ``client_id`` *becomes* the caller identity.

    Observed where it matters: the identity handed to
    :func:`creek_mcp.policy.admitted_ceiling`. The ADR's rule is that ``/v1``
    derives ``consumer`` from the token unconditionally and accepts no
    client-supplied ``consumer`` anywhere — the fix for Adepthood sending its
    own env-var *name* as an identity.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
        token: The bearer under test.
        expected_consumer: The ``client_id`` it must resolve to.
    """
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault) as test_client:
        test_client.get(HEALTH_PATH, headers=headers(token=token))
    assert len(calls) == 1
    identity = calls[0][0]
    assert identity.consumer == expected_consumer
    assert identity.is_remote is True


def test_one_consumers_token_never_authenticates_as_the_other(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two configured consumers stay two identities on one server.

    The verifier compares against every configured token in constant time, so
    a bug that returned the *first* match rather than the matching one would
    still authenticate — under the wrong name, forging every audit line the
    call produced.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
    """
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault) as test_client:
        test_client.get(HEALTH_PATH, headers=headers(token=STRONG_TOKEN))
        test_client.get(HEALTH_PATH, headers=headers(token=OTHER_TOKEN))
    assert [call[0].consumer for call in calls] == [CONSUMER, OTHER_CONSUMER]


def test_a_successful_response_never_echoes_the_bearer(vault: Path) -> None:
    """The credential does not come back out in the body or the headers.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())
    assert STRONG_TOKEN not in response.text
    assert all(STRONG_TOKEN not in value for value in response.headers.values())


# --------------------------------------------------------------------------- #
# build_verifier — no anonymous access, one shared length floor
# --------------------------------------------------------------------------- #


def test_build_verifier_refuses_when_no_tokens_are_configured() -> None:
    """An unconfigured environment yields no verifier, ever.

    ``load_consumer_tokens`` answers "no tokens" with an empty map, which is
    the *safe* answer only if the caller treats it as "network mode denied".
    An adapter that turned the empty map into an empty verifier would serve
    every request unauthenticated: the verifier would reject every bearer, so
    the 401 path would look correct in tests, while a deployment that forgot
    the env var would have no auth at all if the wiring ever short-circuits.
    So the refusal is raised at build time, loudly, naming the setting the
    operator must set.
    """
    with pytest.raises(ValueError, match=CONSUMER_TOKENS_ENV) as excinfo:
        build_verifier({})
    message = str(excinfo.value)
    assert CONSUMER_TOKENS_ENV in message
    assert "authentication" in message.lower()


def test_build_verifier_accepts_a_configured_consumer() -> None:
    """The refusal above is a refusal, not a permanent failure."""
    verifier = build_verifier({CONSUMER_TOKENS_ENV: f"{CONSUMER}={STRONG_TOKEN}"})
    assert verifier is not None


@pytest.mark.parametrize(
    "length", [MIN_TOKEN_LEN - 1, MIN_TOKEN_LEN], ids=["below-floor", "on-floor"]
)
def test_build_verifier_enforces_the_shared_length_floor(length: int) -> None:
    """The floor is :data:`creek_mcp.token_policy.MIN_TOKEN_LEN`, not a copy.

    Parametrized on the constant rather than on ``31``/``32`` on purpose: a
    hard-coded pair would keep passing against an adapter that had grown its
    own floor and then lowered it, which is precisely the second-length-floor
    drift the ADR forbids.

    Args:
        length: The configured token's length.
    """
    token = "z" * length  # test literal, not a real credential
    environ = {CONSUMER_TOKENS_ENV: f"{CONSUMER}={token}"}
    if length >= MIN_TOKEN_LEN:
        assert build_verifier(environ) is not None
        return
    with pytest.raises(ValueError) as excinfo:
        build_verifier(environ)
    message = str(excinfo.value)
    assert CONSUMER in message
    assert str(MIN_TOKEN_LEN) in message
    assert "secrets.token_urlsafe(32)" in message
    assert token not in message  # NEVER echo the token value


# --------------------------------------------------------------------------- #
# build_verifier — the rotation window reaches /v1 through the same env (#895)
# --------------------------------------------------------------------------- #


def test_build_verifier_opens_a_rotation_window_for_one_consumer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old and new both authenticate as one consumer, straight from the env.

    Driven end-to-end through the adapter rather than against ``verify_token``
    directly, because the point of the window is what a *consumer* experiences:
    one still holding the retired secret keeps being served while it redeploys,
    and it is served under its own name. A window that changed the caller's
    identity for its duration would rewrite every access line it produced, so
    the identity handed to :func:`creek_mcp.policy.admitted_ceiling` is what is
    asserted.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
    """
    built = build_verifier(
        {CONSUMER_TOKENS_ENV: f"{CONSUMER}={STRONG_TOKEN},{_ROTATION_TOKEN}"}
    )
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault, verifier=built) as test_client:
        for token in (STRONG_TOKEN, _ROTATION_TOKEN):
            response = test_client.get(HEALTH_PATH, headers=headers(token=token))
            assert response.status_code == 200
    assert [call[0].consumer for call in calls] == [CONSUMER, CONSUMER]


def test_closing_the_window_revokes_the_retired_token(vault: Path) -> None:
    """Once the operator drops the old secret, presenting it is a ``401`` again.

    The half of #895 that keeps it a rotation rather than a permanent widening:
    without this, "both tokens work" is indistinguishable from "tokens are never
    revoked".

    Args:
        vault: A seeded vault.
    """
    built = build_verifier({CONSUMER_TOKENS_ENV: f"{CONSUMER}={_ROTATION_TOKEN}"})
    with client(vault_path=vault, verifier=built) as test_client:
        retired = test_client.get(HEALTH_PATH, headers=headers(token=STRONG_TOKEN))
        current = test_client.get(HEALTH_PATH, headers=headers(token=_ROTATION_TOKEN))
    assert retired.status_code == _UNAUTHENTICATED_STATUS
    assert current.status_code == 200


def test_build_verifier_refuses_one_token_configured_for_two_consumers() -> None:
    """A shared secret is refused before ``/v1`` has an identity to attribute to.

    The verifier scans every consumer without breaking, so a value configured
    twice resolves to whichever consumer the scan saw last — and ``/v1`` derives
    the audited ``consumer`` from exactly that ``client_id``. Refusing at build
    time is the only point at which the ambiguity is still visible.
    """
    shared = f"{CONSUMER}={STRONG_TOKEN};{OTHER_CONSUMER}={STRONG_TOKEN}"
    with pytest.raises(ValueError) as excinfo:
        build_verifier({CONSUMER_TOKENS_ENV: shared})
    message = str(excinfo.value)
    assert CONSUMER in message
    assert OTHER_CONSUMER in message
    assert STRONG_TOKEN not in message  # NEVER echo the token value
