"""The ``/v1`` routes' half of the shared-pool contract (#1894, ADR-0016).

Three acceptance tests of ADR-0016 live here, and all three are claims about
what crosses the wire rather than about what a mock was called with:

- **T15 provenance** (Decision 1 item 11). Every model-backed answer names its
  route, and the name matches what a socket actually dialled.
- **T20 prompt cap, vault half** (Decision 1 item 4). Grounding snippets are
  dropped first; an entry that still exceeds the 4,096-token cap is refused
  ``invalid_request`` / ``prompt_too_long`` with **zero** dials, and never
  truncated. Plus ``rate_limited``: HTTP 429 with an advisory ``Retry-After``
  for a caller declaring the new minor, and ``temporarily_unavailable`` for
  one that did not.
- **T10 BYOK precedence, route half** (R9, Decision 1 item 10). With a
  writer's key the vendor answers and the pool is never dialled — on success
  **and** on a vendor 5xx, which is a refusal rather than a pool call.

Every pool here is a loopback listener this suite opened and every vendor is
an :class:`httpx.MockTransport` behind the real SDK, so nothing is billed
(owner Decision 4, no spend yet).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.pool_boundary import (
    POOL_PROMPT_TOKEN_CAP,
    POOL_URL_ENV,
    POOL_VENDOR_ENV,
    PoolDial,
    PoolRequestScope,
    pool_prompt_tokens,
    pool_prompt_within_cap,
)
from creek.config import CreekConfig
from creek_mcp.api.models import INFERENCE_SOURCE_POOL_CREEK
from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER
from creek_mcp.httpapi import pipeline as pipeline_module
from creek_mcp.httpapi.errors import RETRY_AFTER_HEADER, RETRY_AFTER_SECONDS
from creek_mcp.tools import reflect as reflect_module
from tests.pool_support import (
    PINNED_MODEL,
    FakePool,
    FakeVendor,
    SocketSpy,
    generation,
    identity_listing,
    pin_pool_manifest,
)
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    CLASSIFICATIONS_PATH,
    LETTERS_TEMPLATE,
    REFLECTIONS_PATH,
    build_app,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx
    from starlette.testclient import TestClient

ENTRY: Final[str] = "I keep saying yes to things I do not want, and I feel it later."
"""A benign entry the care guard does not escalate."""

QUOTE: Final[str] = "saying yes to things I do not want"
"""A verbatim span of :data:`ENTRY`, so a note survives quote validation."""

_TURN: Final[str] = json.dumps(
    {"notes": [{"quote": QUOTE, "kind": "pattern", "note": "It costs you."}]}
)
"""One well-formed reflection turn, as either a pool or a vendor returns it."""

_CONFIG_SUBPATH: Final[tuple[str, ...]] = ("00-Creek-Meta", "creek_config.yaml")

_PRIOR_MINOR: Final[str] = "0.18"
"""The minor immediately below the one that publishes provenance."""

HTTP_OK: Final[int] = 200
HTTP_UNPROCESSABLE: Final[int] = 422
HTTP_TOO_MANY_REQUESTS: Final[int] = 429
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503
HTTP_BAD_GATEWAY: Final[int] = 502

_SHED_TIMEOUT: Final[float] = 5.0
"""A deadline the route reaches its dial inside of, and sheds after.

Both halves matter and the lower bound is the one that bites. At 0.3 s this
test was load-flaky for a reason worth recording: on a busy machine the
reflection route had not finished the care guard, the journal write and the
prompt build — let alone opened a pool dial — before the deadline fired, so
the pool recorded ``accepted=0`` and the shed under test never happened. The
test still read as a failure of the cancel wiring. Five seconds is generous
against that floor while remaining far under the pool contract's own 12 s
request timeout, so a hanging pool is still holding the connection when the
deadline arrives.
"""

_BOUNDED_TIMEOUT: Final[float] = 2.0
"""A deadline comfortably under the pool contract's own 12 s ceiling."""

_CANCEL_DEADLINE: Final[float] = 1.0
"""ADR-0016 T23: the socket must close within one second of the cancel."""

_UNCAPPED_BUDGET: Final[float] = 60.0
"""A scope budget large enough that the boundary's own timeout cannot bind.

The one deliberate intervention in the shed test, and the thing that makes it
decisive rather than a coin toss. In production ``open_pool_dial`` caps the
boundary's ``request_timeout`` at :meth:`PoolRequestScope.remaining`, which is
read off the very deadline the route sheds on — so the worker's blocked read
and the loop's cancel are the *same instant* by construction, the socket
closes either way, and no observation at route level can attribute it. That
overlap is why the first version of this test passed with
``PoolRequestScope.cancel`` monkeypatched to a no-op, and why asserting the
refusal the dial raised merely traded a vacuous assertion for a flaky one.

Reporting a minute of remaining budget separates the two: the dial arms a
12 s read (the pool contract's own ceiling, :data:`POOL_REQUEST_TIMEOUT`,
which now binds instead), the route still sheds on its real
:data:`_SHED_TIMEOUT` deadline, and nothing but the cancel can close that
socket inside :data:`_CANCEL_DEADLINE`. The cap this sets aside has its own
test next door — ``test_the_declared_deadline_is_bounded_by_the_requests_own``
— which patches nothing.
"""

_JOB_DEADLINE: Final[float] = 5.0
"""Bounded wait for a durable pipeline job to reach a terminal state."""

_JOB_POLL: Final[float] = 0.01
"""Poll interval while awaiting a job."""

_ONE_GENERATION_DIAL: Final[int] = 2
"""The identity probe plus the one generation: a repeat adds neither."""

HTTP_ACCEPTED: Final[int] = 202


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _pool_vault(tmp_path: Path, provider: str = "pool") -> Path:
    """Scaffold a vault whose ``generation`` stage is routed to *provider*."""
    vault = seed_vault(tmp_path / "vault")
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["default"].update(model=PINNED_MODEL)
    data["llm"]["generation"] = {
        **data["llm"]["default"],
        "provider": provider,
        "model": PINNED_MODEL,
    }
    path = vault.joinpath(*_CONFIG_SUBPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    return vault


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from an empty cwd, with the pool's identity pinned and nothing else.

    The manifest is part of the baseline rather than per-test because it is
    part of the *production* baseline: a vault that reaches the pool pins the
    pool's identity, and :func:`~creek_mcp.model_pin.pinned_model_serves`
    refuses before any prompt without one (ADR-0016 T1, shipped in #1905).
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv(POOL_URL_ENV, raising=False)
    monkeypatch.delenv(POOL_VENDOR_ENV, raising=False)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    pin_pool_manifest(tmp_path, monkeypatch)


def _probe_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the identity probe succeed so a test can aim at the generate dial.

    The two dials are separate connections, and some of what this module has
    to prove is about the **second** one: a pool that answered the listing and
    then refuses admission, or accepts the prompt and dies. Standing in for
    the first is honest here precisely because #1905 already pins it against
    a real listener.
    """
    monkeypatch.setattr(
        "creek.classify.llm.providers.check_pool_available",
        lambda **_kwargs: True,
    )


@pytest.fixture
def answering_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePool]:
    """A pinned loopback pool that answers both the probe and the prompt."""
    pool = FakePool.routing(
        {
            "/v1/models": identity_listing(),
            "/v1/generate": generation(text=_TURN),
        }
    )
    pool.pin(monkeypatch)
    try:
        yield pool
    finally:
        pool.close()


def _client(vault: Path, **kwargs: Any) -> TestClient:
    """Return a client over the ``/v1`` app serving *vault*."""
    from starlette.testclient import TestClient

    return TestClient(build_app(vault_path=vault, **kwargs))


def _reflect(
    vault: Path, *, minor: str | None = None, extra: dict[str, str] | None = None
) -> httpx.Response:
    """POST one reflection over *vault* and return the response."""
    sent = (
        headers(ceiling="personal")
        if minor is None
        else headers(ceiling="personal", minor=minor)
    )
    with _client(vault) as http:
        return http.post(
            REFLECTIONS_PATH, json={"content": ENTRY}, headers={**sent, **(extra or {})}
        )


def _key_headers(**kwargs: Any) -> dict[str, str]:
    """Return the two request-key headers for an admitted writer's key."""
    return {
        PROVIDER_HEADER: "anthropic",
        PROVIDER_KEY_HEADER: "sk-ant-api03-ROUTEtestKEY1894zzPP-never-logged",
        **kwargs,
    }


# --------------------------------------------------------------------------- #
# T15 — every model-backed answer names its route
# --------------------------------------------------------------------------- #


class TestProvenance:
    """ADR-0016 T15 / Decision 1 item 11, on the reflection route."""

    @pytest.mark.usefixtures("isolated")
    def test_a_pool_answer_names_the_pool_and_matches_the_dial(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """``pool:creek``, and the only host dialled is the pinned pool."""
        response = _reflect(_pool_vault(tmp_path))

        assert response.status_code == HTTP_OK, response.text
        assert response.json()["inference_source"] == INFERENCE_SOURCE_POOL_CREEK
        # Two dials: the identity probe and the generation. Both to the one
        # pinned destination, which is what makes the label match the wire.
        assert [path for _method, path in answering_pool.requests] == [
            "/v1/models",
            "/v1/generate",
        ]

    @pytest.mark.usefixtures("isolated")
    def test_an_attesting_vendor_is_named_in_the_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answering_pool: FakePool
    ) -> None:
        """Under O5 the label carries the vendor the Machine environment names."""
        monkeypatch.setenv(POOL_VENDOR_ENV, "tinfoil")

        response = _reflect(_pool_vault(tmp_path))

        assert response.status_code == HTTP_OK, response.text
        assert response.json()["inference_source"] == "pool:attested:tinfoil"
        assert answering_pool.accepted > 0

    @pytest.mark.usefixtures("isolated")
    def test_an_unusable_vendor_name_is_unlabelled_rather_than_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answering_pool: FakePool
    ) -> None:
        """A Machine value the vocabulary cannot carry omits the field.

        Half of ``pool:attested:<vendor>`` comes from the environment, so the
        failure mode to close is publishing operator-supplied bytes inside a
        colon-separated value a consumer splits. The answer still stands; only
        its label is withheld.
        """
        monkeypatch.setenv(POOL_VENDOR_ENV, "not a slug")

        response = _reflect(_pool_vault(tmp_path))

        assert response.status_code == HTTP_OK, response.text
        assert "inference_source" not in response.json()

    @pytest.mark.usefixtures("isolated")
    def test_a_prior_minor_is_never_told_the_route(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """A ``0.18`` consumer validating its closed schema sees no new key."""
        response = _reflect(_pool_vault(tmp_path), minor=_PRIOR_MINOR)

        assert response.status_code == HTTP_OK, response.text
        assert "inference_source" not in response.json()
        assert answering_pool.accepted > 0

    @pytest.mark.usefixtures("isolated")
    def test_the_handshake_discloses_the_pool_before_any_dial(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """``inference_pool`` is the pre-flight signal, gated on the new minor."""
        vault = _pool_vault(tmp_path)
        with _client(vault) as http:
            current = http.get(CAPABILITIES_PATH, headers=headers())
            prior = http.get(CAPABILITIES_PATH, headers=headers(minor=_PRIOR_MINOR))

        assert current.json()["inference_pool"] is True
        assert "inference_pool" not in prior.json()

    @pytest.mark.usefixtures("isolated")
    def test_a_vault_with_no_pool_pinned_reports_no_pool(self, tmp_path: Path) -> None:
        """The field is a fact about this Machine, not a constant."""
        with _client(_pool_vault(tmp_path, provider="ollama")) as http:
            body = http.get(CAPABILITIES_PATH, headers=headers()).json()

        assert body["inference_pool"] is False


# --------------------------------------------------------------------------- #
# T20 — the prompt cap, and the admission refusal
# --------------------------------------------------------------------------- #


class TestPromptCap:
    """ADR-0016 T20, vault half: drop grounding, then refuse — never truncate."""

    @staticmethod
    def _over_cap_entry() -> str:
        """Return an entry whose prompt cannot fit under the cap, with a quote."""
        filler = "the river keeps its own time and I do not. " * 400
        assert pool_prompt_tokens(filler) > POOL_PROMPT_TOKEN_CAP
        return f"{QUOTE}. {filler}"

    @pytest.mark.usefixtures("isolated")
    def test_an_over_cap_entry_is_refused_before_any_dial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``invalid_request`` with zero dials — not a truncated reflection."""
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)
        vault = _pool_vault(tmp_path)

        with _client(vault) as http:
            response = http.post(
                REFLECTIONS_PATH,
                json={"content": self._over_cap_entry()},
                headers=headers(ceiling="personal"),
            )

        assert response.status_code == HTTP_UNPROCESSABLE, response.text
        assert response.json()["code"] == "invalid_request"
        # The refusal is the whole of the evidence that nothing was truncated:
        # a truncating implementation would have answered 200.
        assert spy.destinations == []

    @pytest.mark.usefixtures("isolated")
    def test_an_entry_that_fits_is_answered_and_carries_the_whole_entry(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """The prompt the pool received contains the entry in full.

        The positive control for "never truncated": the refusal above proves
        an over-cap entry is not shortened, and this proves an admitted one is
        not shortened either.
        """
        response = _reflect(_pool_vault(tmp_path))

        assert response.status_code == HTTP_OK, response.text
        sent = answering_pool.bodies[-1]
        assert isinstance(sent, dict)
        assert ENTRY in str(sent["prompt"])

    @pytest.mark.usefixtures("isolated")
    def test_the_prompt_sent_is_under_the_cap(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """Whatever reaches the pool is inside the contract's own ceiling."""
        assert _reflect(_pool_vault(tmp_path)).status_code == HTTP_OK
        sent = answering_pool.bodies[-1]
        assert isinstance(sent, dict)
        assert pool_prompt_tokens(str(sent["prompt"])) <= POOL_PROMPT_TOKEN_CAP

    @pytest.mark.usefixtures("isolated")
    def test_an_admission_refusal_is_rate_limited_at_the_new_minor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """429 with an advisory ``Retry-After``: a cap, not an outage."""
        _probe_passes(monkeypatch)
        pool = FakePool(status=HTTP_TOO_MANY_REQUESTS)
        pool.pin(monkeypatch)
        try:
            response = _reflect(_pool_vault(tmp_path))
        finally:
            pool.close()

        assert response.status_code == HTTP_TOO_MANY_REQUESTS, response.text
        assert response.json()["code"] == "rate_limited"
        assert response.headers[RETRY_AFTER_HEADER] == RETRY_AFTER_SECONDS

    @pytest.mark.usefixtures("isolated")
    def test_a_prior_minor_still_receives_temporarily_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``0.18`` caller's vendored vocabulary cannot name ``rate_limited``."""
        _probe_passes(monkeypatch)
        pool = FakePool(status=HTTP_TOO_MANY_REQUESTS)
        pool.pin(monkeypatch)
        try:
            response = _reflect(_pool_vault(tmp_path), minor=_PRIOR_MINOR)
        finally:
            pool.close()

        assert response.status_code == HTTP_SERVICE_UNAVAILABLE, response.text
        assert response.json()["code"] == "temporarily_unavailable"
        assert RETRY_AFTER_HEADER not in response.headers


# --------------------------------------------------------------------------- #
# T9 route half — a pool that accepts and then dies is a refusal, not a 500
# --------------------------------------------------------------------------- #


class TestFailClosed:
    """ADR-0016 T9, on the route: the refusal shape, never an unhandled fault."""

    @pytest.mark.usefixtures("isolated")
    def test_a_pool_that_dies_mid_request_is_the_routes_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reachable path the review found escaping as an ``OSError``.

        The identity probe succeeds and the *separate* generate dial is reset.
        Before #1894's hierarchy fix this answered ``500 internal_error``,
        because ``creek_mcp/tools/reflect.py`` catches ``RuntimeError`` and
        ``PoolBoundaryError`` was an ``OSError`` — so the one uncovered
        statement in ``pool_boundary.py`` was also an unhandled fault on a
        reachable production path.
        """
        _probe_passes(monkeypatch)
        dying = FakePool(reset=True)
        dying.pin(monkeypatch)
        try:
            response = _reflect(_pool_vault(tmp_path))
        finally:
            dying.close()

        assert response.status_code == HTTP_SERVICE_UNAVAILABLE, response.text
        assert response.json()["code"] == "temporarily_unavailable"
        assert dying.accepted == 1


# --------------------------------------------------------------------------- #
# T10 — the writer's key wins, and the pool is never dialled
# --------------------------------------------------------------------------- #


class TestRequestKeyPrecedence:
    """ADR-0016 T10, route half (R9, Decision 1 item 10)."""

    @pytest.fixture
    def managed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: None
    ) -> Path:
        """A managed vault whose ``generation`` stage is the pinned pool."""
        del isolated
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        return _pool_vault(tmp_path)

    def test_a_writers_key_answers_and_the_pool_is_never_dialled(
        self, managed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On success: the vendor answers, and zero dials leave for the pool."""
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)
        vendor = FakeVendor(monkeypatch, turn=_TURN)

        with _client(managed) as http:
            response = http.post(
                REFLECTIONS_PATH,
                json={"content": ENTRY},
                headers=_key_headers(**headers(ceiling="personal")),
            )

        assert response.status_code == HTTP_OK, response.text
        assert response.json()["inference_source"] == "request_key:anthropic"
        assert len(vendor.requests) == 1
        assert spy.destinations == []

    def test_a_vendor_failure_under_a_writers_key_is_never_a_pool_call(
        self, managed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vendor 5xx is a refusal, not a second destination (item 10)."""
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)
        vendor = FakeVendor(monkeypatch, status=HTTP_BAD_GATEWAY)

        with _client(managed) as http:
            response = http.post(
                REFLECTIONS_PATH,
                json={"content": ENTRY},
                headers=_key_headers(**headers(ceiling="personal")),
            )

        assert response.status_code == HTTP_SERVICE_UNAVAILABLE, response.text
        assert "inference_source" not in response.json()
        assert vendor.requests != []
        assert spy.destinations == []

    def test_a_vendor_rate_limit_under_a_writers_key_is_also_rate_limited(
        self, managed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A writer whose own key is throttled is shown a wait, not an outage.

        ``rate_limited`` covers both sources on purpose: the pool's admission
        cap and the vendor a request key is spending on. The advisory
        ``Retry-After`` is the published constant, never the vendor's own — a
        relayed wait would be upstream detail this surface does not relay.
        """
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)
        FakeVendor(monkeypatch, status=HTTP_TOO_MANY_REQUESTS)

        with _client(managed) as http:
            response = http.post(
                REFLECTIONS_PATH,
                json={"content": ENTRY},
                headers=_key_headers(**headers(ceiling="personal")),
            )

        assert response.status_code == HTTP_TOO_MANY_REQUESTS, response.text
        assert response.json()["code"] == "rate_limited"
        assert response.headers[RETRY_AFTER_HEADER] == RETRY_AFTER_SECONDS
        assert spy.destinations == []

    def test_without_a_key_the_pool_answers(
        self, managed: Path, answering_pool: FakePool
    ) -> None:
        """The other half of the precedence: no key, and the pool serves."""
        response = _reflect(managed)

        assert response.status_code == HTTP_OK, response.text
        assert response.json()["inference_source"] == INFERENCE_SOURCE_POOL_CREEK
        assert answering_pool.accepted > 0


# --------------------------------------------------------------------------- #
# T23 route half — the cancel handle finally has a production caller
# --------------------------------------------------------------------------- #


@dataclass
class _DialSpy:
    """What the route's scope registered, and which dials it then cancelled.

    Attributes:
        registered: Every dial handed to :meth:`PoolRequestScope.register`,
            in order. Empty means the loop was holding nothing to cancel,
            which is the #1905 defect itself.
        cancelled: Every dial :meth:`PoolDial.cancel` was called on.
    """

    registered: list[PoolDial]
    cancelled: list[PoolDial]


def _spy_on_dials(monkeypatch: pytest.MonkeyPatch) -> _DialSpy:
    """Record the route's custody of its pool dial, delegating to the real code.

    **Why a closing socket is not proof of a cancel.** ``open_pool_dial`` caps
    the boundary's own ``request_timeout`` at the scope's remaining budget, and
    that budget is read off the very deadline the route sheds on — so the
    worker's blocked read expires at the same instant the loop cancels, and
    :meth:`PoolDial.request`'s ``finally: self.close()`` closes the socket
    anyway. The pool reaches end-of-stream either way, so "the socket closed"
    is satisfied by a route that cancels nothing at all. That is exactly how
    the first version of the test below passed with
    ``PoolRequestScope.cancel`` monkeypatched to a no-op, which an independent
    review of #1920 demonstrated.

    Nor can the two co-timed closers be told apart by timing or by the
    refusal the dial raises: both are decided by which of two deadlines a
    loaded machine serves first, so an assertion either way is a race rather
    than a contract — and the dial's own timeout wins often enough that the
    reflection tool turns its ``PoolUnreachableError`` into a refusal the
    route never sheds on at all.

    The test that uses this therefore does two things. It sets the cap aside
    with :data:`_UNCAPPED_BUDGET`, so the dial's own read is twelve seconds
    away and only the cancel can close the socket inside
    :data:`_CANCEL_DEADLINE`; and it records custody here — the event loop
    must still hold a reference to the in-flight dial and must cancel it,
    which is precisely what #1905's review found missing. A no-op
    ``PoolRequestScope.cancel`` empties :attr:`_DialSpy.cancelled` and leaves
    the socket open, so it reds both halves.

    Args:
        monkeypatch: Restores both real methods when the test finishes.

    Returns:
        The live spy; ``cancelled`` fills on the event loop as the route sheds.
    """
    spy = _DialSpy(registered=[], cancelled=[])
    register = PoolRequestScope.register
    cancel = PoolDial.cancel

    def recording_register(scope: PoolRequestScope, dial: PoolDial) -> None:
        """Note the dial the scope takes custody of, then register it."""
        spy.registered.append(dial)
        register(scope, dial)

    def recording_cancel(dial: PoolDial) -> None:
        """Note the dial being cancelled, then cancel it for real."""
        spy.cancelled.append(dial)
        cancel(dial)

    monkeypatch.setattr(PoolRequestScope, "register", recording_register)
    monkeypatch.setattr(PoolDial, "cancel", recording_cancel)
    return spy


class TestShedPath:
    """ADR-0016 T23 / Decision 1 item 7, from the route rather than a unit test.

    The review of #1905 found the cancel handle had **no production caller**:
    ``pool_generate`` owned its dial inside a ``with``, and nothing on the
    event loop held a reference to cancel. So the shed path was unrealized and
    the "remaining budget" cap was inert on the only generation path.

    An independent review of #1920 then found that the first route-level test
    written for it proved less than it claimed: it asserted only that the
    pool's socket closed, which the boundary's own capped read timeout does at
    the same instant, so the test still passed with
    ``PoolRequestScope.cancel`` replaced by a no-op. It now asserts the route's
    **custody** of the dial instead — that the loop held the in-flight dial and
    cancelled it — which is the thing #1905's review found missing and the
    thing that patch destroys. See :func:`_spy_on_dials`.
    """

    @pytest.mark.usefixtures("isolated")
    def test_a_shed_reflection_cancels_the_pool_dial_the_loop_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deadline fires, the caller is answered, and the route cuts the dial.

        Without the wiring the worker thread would stay blocked for the
        boundary's full 12 s request timeout with the pool's connection open —
        a vendor call still running after the vault had already answered. That
        counterfactual is what :data:`_UNCAPPED_BUDGET` restores: with the
        remaining-budget cap set aside the dial really does hold a 12 s read,
        so an unclosed socket stays open for seconds past the answer and the
        one-second ``closed`` assertion below can only pass by way of the
        cancel.
        """
        _probe_passes(monkeypatch)
        monkeypatch.setattr(
            PoolRequestScope, "remaining", lambda _self: _UNCAPPED_BUDGET
        )
        dials = _spy_on_dials(monkeypatch)
        hanging = FakePool(hang=True)
        hanging.pin(monkeypatch)
        vault = _pool_vault(tmp_path)
        try:
            with _client(vault, timeout_seconds=_SHED_TIMEOUT) as http:
                response = http.post(
                    REFLECTIONS_PATH,
                    json={"content": ENTRY},
                    headers=headers(ceiling="personal"),
                )
            closed = hanging.held_reached_eof(_CANCEL_DEADLINE)
        finally:
            hanging.close()

        assert response.status_code == HTTP_SERVICE_UNAVAILABLE, response.text
        assert response.json()["code"] == "temporarily_unavailable"
        assert closed, (
            "the shed request left the pool dial open: "
            f"accepted={hanging.accepted} registered={len(dials.registered)} "
            f"cancelled={len(dials.cancelled)}"
        )
        # The load-bearing pair, and the whole of what #1905's review found
        # missing: the loop took custody of the generation dial, and the shed
        # cancelled that same dial rather than an empty list of them. With the
        # budget cap set aside, ``closed`` above says the same thing from the
        # pool's side of the wire.
        assert dials.registered, "the loop held no dial to cancel"
        assert dials.cancelled == dials.registered, (
            dials.cancelled,
            dials.registered,
        )
        # One dial and no retry: the shed is a refusal, not a second attempt.
        assert hanging.accepted == 1

    @pytest.mark.usefixtures("isolated")
    def test_the_declared_deadline_is_bounded_by_the_requests_own(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """``deadline_ms`` never outlives the vault request that wanted it.

        The other half of what the scope buys: the pool contract's own ceiling
        is 12,000 ms, and a route running under a two-second deadline must
        declare less than that rather than the flat maximum.
        """
        vault = _pool_vault(tmp_path)
        with _client(vault, timeout_seconds=_BOUNDED_TIMEOUT) as http:
            response = http.post(
                REFLECTIONS_PATH,
                json={"content": ENTRY},
                headers=headers(ceiling="personal"),
            )

        assert response.status_code == HTTP_OK, response.text
        sent = answering_pool.bodies[-1]
        assert isinstance(sent, dict)
        declared = sent["deadline_ms"]
        assert isinstance(declared, int)
        assert 0 < declared <= _BOUNDED_TIMEOUT * 1000


# --------------------------------------------------------------------------- #
# Decision 1 item 4 — the declared tier reaches the pool from the route
# --------------------------------------------------------------------------- #


class TestDeclaredTier:
    """The review's second finding, end to end: the tier is on the wire.

    ``providers.py`` used to send ``{"model", "prompt", "stream"}``, so the
    pool's own INTIMATE refusal could not exist — the tier was never
    transmitted. The ``ModelRouter`` chokepoint is still the first line and
    still correct; this is the defence-in-depth layer the docstrings described
    and the code did not have.
    """

    @pytest.mark.parametrize(
        "ceiling",
        [pytest.param("open", id="open"), pytest.param("personal", id="personal")],
    )
    @pytest.mark.usefixtures("isolated")
    def test_the_routes_tier_is_declared_in_the_closed_request_shape(
        self, tmp_path: Path, answering_pool: FakePool, ceiling: str
    ) -> None:
        """The tier the router keyed the call with is the tier the pool is told."""
        vault = _pool_vault(tmp_path)
        with _client(vault) as http:
            response = http.post(
                REFLECTIONS_PATH,
                json={"content": ENTRY},
                headers=headers(ceiling=ceiling),
            )

        assert response.status_code == HTTP_OK, response.text
        sent = answering_pool.bodies[-1]
        assert isinstance(sent, dict)
        assert sent["tier"] == ceiling
        assert "stream" not in sent


# --------------------------------------------------------------------------- #
# T15 on the other two model-backed routes
# --------------------------------------------------------------------------- #


def _await_terminal(
    http: TestClient, job_id: str, *, minor: str | None = None
) -> dict[str, Any]:
    """Poll one job to a terminal state with a short bounded wait."""
    sent = headers() if minor is None else headers(minor=minor)
    deadline = time.monotonic() + _JOB_DEADLINE
    while time.monotonic() < deadline:
        response = http.get(f"/v1/jobs/{job_id}", headers=sent)
        assert response.status_code == HTTP_OK, response.text
        body: dict[str, Any] = response.json()
        if body["state"] in {"succeeded", "failed"}:
            return body
        time.sleep(_JOB_POLL)
    pytest.fail("the pipeline job did not reach a terminal state")


class TestLetterProvenance:
    """``POST /v1/letters/{external_id}`` names the route that wrote it."""

    @staticmethod
    def _body() -> dict[str, Any]:
        """Return a letter request whose note anchors a verbatim span."""
        return {
            "content": ENTRY,
            "tier": "open",
            "title": "Voice draft",
            "note": {"anchor_text": QUOTE, "kind": "pattern", "note": "Look here."},
        }

    @pytest.mark.usefixtures("isolated")
    def test_a_pool_written_letter_names_the_pool(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """The letter carries ``pool:creek``; the pool saw the prompt."""
        with _client(_pool_vault(tmp_path)) as http:
            response = http.post(
                LETTERS_TEMPLATE.format(external_id="letter-1894"),
                json=self._body(),
                headers=headers(ceiling="personal"),
            )

        assert response.status_code == HTTP_OK, response.text
        assert response.json()["inference_source"] == INFERENCE_SOURCE_POOL_CREEK
        assert answering_pool.accepted > 0

    @pytest.mark.usefixtures("isolated")
    def test_a_repeat_letter_names_no_route_because_none_was_taken(
        self, tmp_path: Path, answering_pool: FakePool
    ) -> None:
        """An ``unchanged`` letter is returned as stored, with no model call.

        The field is **absent** rather than echoing the first write's route: a
        consumer must not read provenance off a response that only re-read a
        stored document.
        """
        path = LETTERS_TEMPLATE.format(external_id="letter-repeat")
        with _client(_pool_vault(tmp_path)) as http:
            first = http.post(
                path, json=self._body(), headers=headers(ceiling="personal")
            )
            again = http.post(
                path, json=self._body(), headers=headers(ceiling="personal")
            )

        assert first.status_code == HTTP_OK, first.text
        assert again.json()["action"] == "unchanged"
        assert "inference_source" not in again.json()
        assert answering_pool.accepted == _ONE_GENERATION_DIAL


def _classified_by(source: str | None) -> dict[str, Any]:
    """Return a complete counts-only classification result labelled *source*.

    The pass itself is stubbed, deliberately. What ADR-0016 T15 asks of this
    route is that the label the tool produced survives the **durable job
    record** and the nested projection into ``JobStatusResponse.result`` — and
    a real LLM pass over a pool-routed vault additionally needs a local
    runtime for its INTIMATE fragments, which is a different issue's concern
    and would make this test about Ollama. The tool's own labelling is covered
    beside the tool, by :mod:`tests.test_classify_pool_provenance`.

    Args:
        source: The provenance value the pass reports, or ``None``.

    Returns:
        The tool's success dict.
    """
    return {
        "status": "ok",
        "tool": "creek.classify",
        "tier_ceiling": "open",
        "total": 3,
        "classified": 2,
        "preserved_manual": 1,
        "preserved_llm": 0,
        "skipped_high_confidence": 0,
        "llm_call_failed": 0,
        "privacy_tiers_assigned": 1,
        "retiered": 0,
        "praxis_marked": 1,
        "tags_extracted": 2,
        "healed_unearned_llm": 0,
        "errors": [],
        "inference_source": source,
    }


class TestClassificationProvenance:
    """``inference_source`` inside ``JobStatusResponse.result`` (T15)."""

    @pytest.fixture
    def stubbed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: None
    ) -> Path:
        """A vault whose classification pass reports a pool-answered result."""
        del isolated
        monkeypatch.setattr(
            pipeline_module,
            "classify_tool",
            lambda **kwargs: _classified_by(
                INFERENCE_SOURCE_POOL_CREEK if kwargs.get("method") == "llm" else None
            ),
        )
        return _pool_vault(tmp_path)

    def test_an_llm_pass_names_its_route_inside_the_job_result(
        self, stubbed: Path
    ) -> None:
        """The durable job's counts-only result carries the pool label."""
        with _client(stubbed) as http:
            accepted = http.post(
                CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers()
            )
            assert accepted.status_code == HTTP_ACCEPTED, accepted.text
            terminal = _await_terminal(http, accepted.json()["job_id"])

        assert terminal["state"] == "succeeded", terminal
        result = terminal["result"]
        assert isinstance(result, dict)
        assert result["inference_source"] == INFERENCE_SOURCE_POOL_CREEK

    def test_a_prior_minor_is_not_told_the_route_inside_the_result(
        self, stubbed: Path
    ) -> None:
        """The nested field is withheld too, or a ``0.18`` consumer breaks."""
        with _client(stubbed) as http:
            accepted = http.post(
                CLASSIFICATIONS_PATH,
                json={"method": "llm"},
                headers=headers(minor=_PRIOR_MINOR),
            )
            assert accepted.status_code == HTTP_ACCEPTED, accepted.text
            terminal = _await_terminal(
                http, accepted.json()["job_id"], minor=_PRIOR_MINOR
            )

        assert terminal["state"] == "succeeded", terminal
        result = terminal["result"]
        assert isinstance(result, dict)
        assert "inference_source" not in result

    def test_a_rules_pass_names_no_route_because_no_model_ran(
        self, stubbed: Path
    ) -> None:
        """``method=rules`` is synchronous and calls nothing, so it is unlabelled."""
        with _client(stubbed) as http:
            response = http.post(
                CLASSIFICATIONS_PATH, json={"method": "rules"}, headers=headers()
            )

        assert response.status_code == HTTP_OK, response.text
        assert "inference_source" not in response.json()


# --------------------------------------------------------------------------- #
# T20 — the order the cap is enforced in, at the unit it is enforced in
# --------------------------------------------------------------------------- #


class TestCapOrder:
    """Grounding snippets are dropped **first**, and the entry never is.

    The route test above proves the refusal and the admission; this proves the
    step between them, which is the one ADR-0016 Decision 1 item 4 spells out
    and the one a reflection's quality depends on: an over-long neighbour must
    cost the reflection its grounding, not its entry.
    """

    @staticmethod
    def _snippet(copies: int) -> str:
        """Return a grounding snippet of roughly *copies* sentences."""
        return "a neighbouring fragment that says something adjacent. " * copies

    def test_an_oversized_snippet_is_dropped_and_the_entry_survives(self) -> None:
        """The prompt comes back under the cap, still carrying the whole entry."""
        prompt = reflect_module._capped_prompt(
            ENTRY, [self._snippet(2000)], max_notes=3
        )

        assert prompt is not None
        assert ENTRY in prompt
        assert self._snippet(2000) not in prompt
        assert pool_prompt_within_cap(prompt)

    def test_only_as_many_snippets_as_needed_are_dropped(self) -> None:
        """Dropping from the tail keeps the relevant neighbours it can.

        All-or-nothing would satisfy the letter of "dropped first" and cost a
        reflection every neighbour over one long one.
        """
        keeper = "the first and most relevant neighbour."
        prompt = reflect_module._capped_prompt(
            ENTRY, [keeper, self._snippet(2000)], max_notes=3
        )

        assert prompt is not None
        assert keeper in prompt
        assert pool_prompt_within_cap(prompt)

    def test_an_entry_over_the_cap_is_refused_rather_than_trimmed(self) -> None:
        """``None`` is the refusal; there is no shortened-entry branch to take."""
        huge = "I keep saying yes to things I do not want. " * 1000

        assert reflect_module._capped_prompt(huge, [], max_notes=3) is None

    def test_the_estimator_never_measures_a_prompt_as_smaller_than_it_is(
        self,
    ) -> None:
        """A cap must over-count tokens, or the pool refuses what the vault admitted."""
        text = "x" * 10
        assert pool_prompt_tokens(text) >= len(text) // 4
        assert pool_prompt_tokens("") == 0
