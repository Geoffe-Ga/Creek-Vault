"""Agent-token revocation checked on every call, behind a short cache (#2005, R3-2).

:class:`~creek_mcp.agent_revocation.RevocationCache` decides whether a verified
token's grant still stands: a fresh ``active`` answer is reused for at most
:data:`~creek_mcp.agent_revocation.REVOCATION_CACHE_SECONDS`, a ``revoked``
answer until the token expires, and an unanswered lookup refuses (fail closed).
:class:`~creek_mcp.agent_revocation.HttpRevocationChecker` asks Adepthood's
``/internal/vault-provisioning/agent-token-status`` (adepthood#3630) with the
mounted handoff bearer. Every answer here comes from an in-process fake or an
``httpx.MockTransport``; nothing dials anywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from creek_mcp.agent_revocation import (
    AGENT_TOKEN_STATUS_PATH,
    MAX_ISSUED_AT,
    REVOCATION_CACHE_MAX_ENTRIES,
    REVOCATION_CACHE_SECONDS,
    STATUS_BEARER_FILE_ENV,
    STATUS_URL_ENV,
    HttpRevocationChecker,
    Revocation,
    RevocationCache,
    RevocationUnavailableError,
    TokenStatusQuery,
    load_revocation_cache,
)
from tests.agent_token_support import FakeClock

if TYPE_CHECKING:
    from pathlib import Path

_STATUS_URL: Final[str] = (
    "https://api.adepthood.example/internal/vault-provisioning/agent-token-status"
)
_BEARER: Final[str] = "unit-test-handoff-bearer-" + "h" * 20
"""The mounted handoff bearer. Test literal, not a real credential."""

_NOW: Final[float] = 1_800_000_000.0
_EXPIRES_AT: Final[int] = int(_NOW) + 600
_QUERY: Final[TokenStatusQuery] = TokenStatusQuery("acct_91", "cl_8a", int(_NOW))


class FakeChecker:
    """Answer from a settable flag, count the lookups, or fail like an outage."""

    def __init__(self, *, active: bool = True) -> None:
        """Start answering *active*.

        Args:
            active: The answer to give.
        """
        self.active = active
        self.failing = False
        self.calls: list[TokenStatusQuery] = []

    async def is_active(self, query: TokenStatusQuery) -> bool:
        """Return the configured answer, or raise while failing.

        Args:
            query: The claims asked about.

        Returns:
            :attr:`active`.

        Raises:
            RevocationUnavailableError: While :attr:`failing` is set.
        """
        self.calls.append(query)
        await asyncio.sleep(0)
        if self.failing:
            raise RevocationUnavailableError
        return self.active


def _cache(
    checker: FakeChecker,
    clock: FakeClock,
    **kwargs: int,
) -> RevocationCache:
    """Return a cache over *checker* on *clock*.

    Args:
        checker: The fake status source.
        clock: The injected wall clock.
        kwargs: ``ttl_seconds`` / ``max_entries`` overrides.

    Returns:
        The cache.
    """
    return RevocationCache(checker, clock=clock, **kwargs)


def _status(cache: RevocationCache, query: TokenStatusQuery = _QUERY) -> Revocation:
    """Ask *cache* about *query* for a token expiring at :data:`_EXPIRES_AT`.

    Args:
        cache: The cache.
        query: The claims.

    Returns:
        The answer.
    """
    return asyncio.run(cache.status(query, expires_at=_EXPIRES_AT))


# --------------------------------------------------------------------------- #
# The query: exactly the claims the contract admits
# --------------------------------------------------------------------------- #


def test_the_defaults_are_the_issue_values() -> None:
    """Thirty seconds of positive cache, the contract's ``iat`` ceiling."""
    assert REVOCATION_CACHE_SECONDS == 30
    assert MAX_ISSUED_AT == 32_503_680_000
    assert REVOCATION_CACHE_MAX_ENTRIES > 0
    assert AGENT_TOKEN_STATUS_PATH == "/internal/vault-provisioning/agent-token-status"


def test_a_query_is_built_from_verified_claims() -> None:
    """``sub``, ``client_id`` and ``iat`` pass through verbatim."""
    query = TokenStatusQuery.from_claims("acct_91", "cl_8a", 1_700_000_000)

    assert query == TokenStatusQuery("acct_91", "cl_8a", 1_700_000_000)
    assert query is not None
    assert query.body() == {
        "sub": "acct_91",
        "client_id": "cl_8a",
        "iat": 1_700_000_000,
    }


@pytest.mark.parametrize(
    ("subject", "client_id", "issued_at"),
    [
        ("acct_91", "cl_8a", 1_700_000_000.5),
        ("acct_91", "cl_8a", "1700000000"),
        ("acct_91", "cl_8a", True),
        ("acct_91", "cl_8a", -1),
        ("acct_91", "cl_8a", MAX_ISSUED_AT + 1),
        ("acct_91", "cl_8a", None),
        ("", "cl_8a", 1),
        ("a" * 201, "cl_8a", 1),
        ("acct_91", "", 1),
        ("acct_91", "c" * 2049, 1),
        (None, "cl_8a", 1),
        ("acct_91", None, 1),
    ],
)
def test_claims_outside_the_contract_make_no_query(
    subject: object, client_id: object, issued_at: object
) -> None:
    """Adepthood would answer ``400``; the vault never asks.

    Args:
        subject: A ``sub`` value.
        client_id: A ``client_id`` value.
        issued_at: An ``iat`` value.
    """
    assert TokenStatusQuery.from_claims(subject, client_id, issued_at) is None


def test_the_contract_bounds_themselves_make_a_query() -> None:
    """The edges of each bound are inside it."""
    query = TokenStatusQuery.from_claims("a" * 200, "c" * 2048, MAX_ISSUED_AT)

    assert query is not None
    assert TokenStatusQuery.from_claims("a", "c", 0) is not None


def test_a_query_never_shows_its_identities() -> None:
    """``repr`` names no subject and no client, so no log line can."""
    shown = repr(TokenStatusQuery("acct_secret_91", "cl_secret_8a", 1))

    assert "acct_secret_91" not in shown
    assert "cl_secret_8a" not in shown


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #


def test_an_active_answer_is_reused_within_the_ttl() -> None:
    """Thirty seconds of calls cost one lookup."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock)

    first = _status(cache)
    clock.advance(REVOCATION_CACHE_SECONDS - 1)
    second = _status(cache)

    assert first is second is Revocation.ACTIVE
    assert len(checker.calls) == 1


def test_a_revocation_is_seen_after_the_ttl_without_a_restart() -> None:
    """The grant is withdrawn; the next call past the TTL is refused."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock)

    assert _status(cache) is Revocation.ACTIVE
    checker.active = False
    clock.advance(REVOCATION_CACHE_SECONDS)

    assert _status(cache) is Revocation.REVOKED
    assert len(checker.calls) == 2


def test_a_revoked_answer_is_kept_until_the_token_expires() -> None:
    """A withdrawn grant never comes back for the same token, so no re-ask."""
    checker, clock = FakeChecker(active=False), FakeClock(_NOW)
    cache = _cache(checker, clock)

    assert _status(cache) is Revocation.REVOKED
    clock.advance(_EXPIRES_AT - _NOW - 1)
    assert _status(cache) is Revocation.REVOKED
    assert len(checker.calls) == 1

    clock.advance(1)
    assert _status(cache) is Revocation.REVOKED
    assert len(checker.calls) == 2


def test_a_revoked_answer_without_an_expiry_is_not_kept() -> None:
    """No expiry, nothing to bound a negative entry by: ask again."""
    checker, clock = FakeChecker(active=False), FakeClock(_NOW)
    cache = _cache(checker, clock)

    for _ in range(2):
        assert asyncio.run(cache.status(_QUERY, expires_at=None)) is Revocation.REVOKED
    assert len(checker.calls) == 2


def test_a_zero_ttl_asks_on_every_call() -> None:
    """``REVOCATION_CACHE_SECONDS = 0`` is the strict per-call lookup."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock, ttl_seconds=0)

    for _ in range(3):
        assert _status(cache) is Revocation.ACTIVE
    assert len(checker.calls) == 3


def test_the_cache_key_is_subject_client_and_issued_at() -> None:
    """Another agent, person or token of the same pair is its own entry."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock)
    queries = [
        _QUERY,
        TokenStatusQuery("acct_other", "cl_8a", int(_NOW)),
        TokenStatusQuery("acct_91", "cl_other", int(_NOW)),
        TokenStatusQuery("acct_91", "cl_8a", int(_NOW) + 1),
    ]

    for query in [*queries, *queries]:
        assert _status(cache, query) is Revocation.ACTIVE

    assert checker.calls == queries


def test_an_unreachable_issuer_with_no_fresh_entry_fails_closed() -> None:
    """No answer is not an ``active`` answer."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    checker.failing = True

    assert _status(_cache(checker, clock)) is Revocation.UNAVAILABLE


def test_a_stale_active_entry_does_not_survive_an_outage() -> None:
    """Past the TTL the old ``active`` is not fresh, so an outage refuses."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock)

    assert _status(cache) is Revocation.ACTIVE
    checker.failing = True
    clock.advance(REVOCATION_CACHE_SECONDS - 1)
    assert _status(cache) is Revocation.ACTIVE
    clock.advance(1)
    assert _status(cache) is Revocation.UNAVAILABLE


def test_a_recovery_admits_again() -> None:
    """A failed lookup is never cached: the next one after recovery admits."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock)
    checker.failing = True

    assert _status(cache) is Revocation.UNAVAILABLE
    checker.failing = False
    assert _status(cache) is Revocation.ACTIVE


def test_a_clock_that_steps_backwards_does_not_stretch_an_active_entry() -> None:
    """An entry read before it was written is not trusted: ask again."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock)

    assert _status(cache) is Revocation.ACTIVE
    clock.advance(-3_600)
    assert _status(cache) is Revocation.ACTIVE
    assert len(checker.calls) == 2


def test_the_cache_is_bounded_and_evicts_the_least_recently_used() -> None:
    """A flood of distinct tokens cannot grow it past its bound."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock, max_entries=2)
    first, second, third = (
        TokenStatusQuery("acct_91", "cl_8a", int(_NOW) + offset) for offset in range(3)
    )

    _status(cache, first)
    _status(cache, second)
    _status(cache, first)
    _status(cache, third)
    assert len(cache) == 2
    _status(cache, first)
    _status(cache, second)

    assert checker.calls == [first, second, third, second]


def test_a_non_positive_bound_is_refused() -> None:
    """A cache that can hold nothing is a configuration mistake."""
    with pytest.raises(ValueError, match="max_entries"):
        RevocationCache(FakeChecker(), max_entries=0)
    with pytest.raises(ValueError, match="ttl_seconds"):
        RevocationCache(FakeChecker(), ttl_seconds=-1)


def test_concurrent_lookups_for_one_token_share_one_request() -> None:
    """A session's first burst spends one request of Adepthood's budget."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    cache = _cache(checker, clock)

    async def burst() -> list[Revocation]:
        return list(
            await asyncio.gather(
                *(cache.status(_QUERY, expires_at=_EXPIRES_AT) for _ in range(5))
            )
        )

    assert asyncio.run(burst()) == [Revocation.ACTIVE] * 5
    assert len(checker.calls) == 1


def test_concurrent_lookups_during_an_outage_all_fail_closed() -> None:
    """A shared failed request refuses every waiter, and is not remembered."""
    checker, clock = FakeChecker(), FakeClock(_NOW)
    checker.failing = True
    cache = _cache(checker, clock)

    async def burst() -> list[Revocation]:
        return list(
            await asyncio.gather(
                *(cache.status(_QUERY, expires_at=_EXPIRES_AT) for _ in range(3))
            )
        )

    assert asyncio.run(burst()) == [Revocation.UNAVAILABLE] * 3
    assert len(checker.calls) == 1
    checker.failing = False
    assert _status(cache) is Revocation.ACTIVE


# --------------------------------------------------------------------------- #
# The HTTP checker
# --------------------------------------------------------------------------- #


def _bearer_file(tmp_path: Path, value: str = _BEARER) -> Path:
    """Write the mounted handoff bearer, owner-only.

    Args:
        tmp_path: Pytest's per-test directory.
        value: The bearer.

    Returns:
        The file.
    """
    path = tmp_path / "handoff-bearer"
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


class _Recorder:
    """A mock transport handler that records requests and answers one response."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        """Answer every request with *response*, or raise it.

        Args:
            response: The answer, or a transport failure.
        """
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer.

        Args:
            request: The outgoing request.

        Returns:
            The configured response.

        Raises:
            Exception: The configured transport failure.
        """
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _checker(tmp_path: Path, recorder: _Recorder) -> HttpRevocationChecker:
    """Return an HTTP checker answered by *recorder*.

    Args:
        tmp_path: Pytest's per-test directory.
        recorder: The transport handler.

    Returns:
        The checker.
    """
    return HttpRevocationChecker(
        _STATUS_URL,
        _bearer_file(tmp_path),
        transport=httpx.MockTransport(recorder),
    )


@pytest.mark.parametrize("active", [True, False])
def test_the_checker_posts_the_claims_with_the_handoff_bearer(
    tmp_path: Path, active: bool
) -> None:
    """The contract's request, and its one-key answer read as a boolean.

    Args:
        tmp_path: Pytest's per-test directory.
        active: Adepthood's answer.
    """
    recorder = _Recorder(httpx.Response(200, json={"active": active}))

    answer = asyncio.run(_checker(tmp_path, recorder).is_active(_QUERY))

    assert answer is active
    (request,) = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == _STATUS_URL
    assert request.headers["authorization"] == f"Bearer {_BEARER}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "sub": "acct_91",
        "client_id": "cl_8a",
        "iat": int(_NOW),
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": "invalid_provisioning_handoff"}),
        httpx.Response(400, json={"detail": "invalid_agent_token_status"}),
        httpx.Response(429, json={"detail": "rate limited"}),
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(302, headers={"location": "https://elsewhere.example/"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["active"]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"active": "true"}),
        httpx.Response(200, json={"active": 1}),
        httpx.Response(200, json={"active": True, "sub": "acct_91"}),
        httpx.Response(200, content=b'{"active": true}' + b" " * 2048),
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("refused"),
    ],
)
def test_every_other_answer_is_unavailable(
    tmp_path: Path, response: httpx.Response | Exception
) -> None:
    """401, 400, 429, 5xx, a redirect, a malformed body or a timeout: no answer.

    Args:
        tmp_path: Pytest's per-test directory.
        response: What Adepthood (or the network) answers.
    """
    checker = _checker(tmp_path, _Recorder(response))

    with pytest.raises(RevocationUnavailableError) as raised:
        asyncio.run(checker.is_active(_QUERY))

    assert "adepthood" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "url",
    [
        "http://api.adepthood.example/internal/vault-provisioning/agent-token-status",
        "https://api.adepthood.example/internal/vault-provisioning/alerts",
        "https://api.adepthood.example/internal/vault-provisioning/agent-token-status/",
        f"{_STATUS_URL}?x=1",
        f"{_STATUS_URL}#frag",
        _STATUS_URL.replace("https://", "https://operator@"),
        "https:///internal/vault-provisioning/agent-token-status",
        "https://[::1/internal/vault-provisioning/agent-token-status",
    ],
)
def test_the_checker_accepts_only_the_canonical_https_route(
    tmp_path: Path, url: str
) -> None:
    """The bearer goes to the one route that expects it, over TLS.

    Args:
        tmp_path: Pytest's per-test directory.
        url: A wrong status URL.
    """
    with pytest.raises(ValueError, match="canonical HTTPS route") as raised:
        HttpRevocationChecker(url, _bearer_file(tmp_path))

    assert "adepthood" not in str(raised.value)


def test_the_checker_refuses_an_unreadable_bearer(tmp_path: Path) -> None:
    """No mounted bearer, no checker: startup refuses."""
    with pytest.raises(ValueError, match="bearer"):
        HttpRevocationChecker(_STATUS_URL, _bearer_file(tmp_path, "two words"))


def test_the_checker_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the request nor the answer reaches a log.

    Args:
        tmp_path: Pytest's per-test directory.
        caplog: Pytest's log capture.
    """
    recorder = _Recorder(httpx.Response(200, json={"active": True}))
    with caplog.at_level(logging.DEBUG, logger="creek_mcp"):
        asyncio.run(_checker(tmp_path, recorder).is_active(_QUERY))

    assert not [r for r in caplog.records if r.name.startswith("creek_mcp")]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_no_status_settings_is_no_check() -> None:
    """A vault with neither setting behaves as before #2005."""
    assert load_revocation_cache({}) is None
    assert load_revocation_cache({STATUS_URL_ENV: " ", STATUS_BEARER_FILE_ENV: ""}) is (
        None
    )


def test_both_status_settings_build_the_check(tmp_path: Path) -> None:
    """The URL and the mounted bearer file together turn the check on."""
    cache = load_revocation_cache(
        {
            STATUS_URL_ENV: _STATUS_URL,
            STATUS_BEARER_FILE_ENV: str(_bearer_file(tmp_path)),
        }
    )

    assert isinstance(cache, RevocationCache)


@pytest.mark.parametrize("present", [STATUS_URL_ENV, STATUS_BEARER_FILE_ENV])
def test_half_the_status_settings_are_refused(tmp_path: Path, present: str) -> None:
    """One without the other is a startup refusal naming the settings only.

    Args:
        tmp_path: Pytest's per-test directory.
        present: The one setting given.
    """
    value = _STATUS_URL if present == STATUS_URL_ENV else str(_bearer_file(tmp_path))

    with pytest.raises(ValueError, match=STATUS_URL_ENV) as raised:
        load_revocation_cache({present: value})

    assert value not in str(raised.value)
