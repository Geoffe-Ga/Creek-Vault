"""The attesting loopback sidecar: T18, T20, T7 (vault half), T3, T21 (#1896).

ADR-0016 O5 with Privatemode runs the vendor's attesting proxy as a loopback
sidecar. Every test here dials a loopback listener this module opened, or
nothing: no vendor is contacted, no inference runs, nothing is billed, and
every key is a synthetic canary (Decision 4).

**What is tested, and what is vendor-asserted.** The vendor's proxy performs
the attestation; Creek cannot run it here. These tests exercise the seam Creek
controls — the sidecar's attestation report contract — and prove that a
tampered measurement, an unpinned measurement, a measurement under another
signing identity, a missing transparency-log entry and a plain-TLS channel are
each refused **with nothing sent**, and that a matching report proceeds. That
the report is *true* (the hardware quote, the Sigstore signature, the
transparency-log inclusion, and the vendor's binding of the attested
measurement to the live session key) is vendor-asserted: Decision 3.3
condition (f) is waived by the owner, not verified.

**T4 and T24 are not applicable under O5.** There is no Creek-run engine and
no Creek-run gateway, so there is no engine log, captured engine output,
``/dev/shm`` entry or pool host to canary (T4), and no mutual-TLS binding or
engine port reachability to assert (T24). They become live again only if O3 is
built instead.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek.classify.llm import pool_admission, pool_attestation, pool_embeddings
from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.classify.llm.pool_admission import (
    DEFAULT_POOL_RATE_PER_HOUR,
    HOURLY_LIMIT,
    IN_FLIGHT_LIMIT,
    POOL_RATE_ENV,
    PoolAdmission,
    PoolAdmissionRefusedError,
    configured_rate,
    vault_admission,
)
from creek.classify.llm.pool_attestation import (
    POOL_EGRESS_ENV,
    POOL_MEASUREMENTS_ENV,
    POOL_MIN_VERSION_ENV,
    POOL_SIGNING_IDENTITY_ENV,
    AttestationVerdict,
    PoolAttestationError,
    attested_report,
    judge_report,
    load_pool_pin,
)
from creek.classify.llm.pool_attested import (
    OUTCOME_ALERTS,
    TELEMETRY_LOGGER,
    PoolAnswerUnusableError,
    PoolCredentialRejectedError,
    PoolQuotaExhaustedError,
    PoolRequestRefusedError,
    PoolVendorRateLimitedError,
    call_attested_pool,
    completion_from_answer,
    validate_closed_request,
)
from creek.classify.llm.pool_boundary import (
    POOL_ATTESTATION_PATH,
    POOL_CHAT_COMPLETIONS_PATH,
    POOL_EMBEDDINGS_PATH,
    POOL_MAX_REQUEST_BYTES,
    POOL_PROMPT_CHARS_PER_TOKEN,
    POOL_PROMPT_TOKEN_CAP,
    POOL_REQUEST_ID_HEADER,
    POOL_URL_ENV,
    POOL_VENDOR_ENV,
    PoolDestinationError,
    declared_pool_request,
    pool_destination,
)
from creek.classify.llm.pool_embeddings import PoolEmbedder, PoolEmbeddingPin
from creek.classify.llm.providers import (
    PoolProvider,
    ProviderRateLimitError,
    check_pool_available,
    pool_identity_digest,
)
from creek.config import LLMConfig
from creek.models import PrivacyTier
from creek_mcp import container_health
from creek_mcp.container_health import ProbeStatus
from creek_mcp.provenance import stage_inference_source
from creek_mcp.provisioning.budget import AlertKind

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

MODEL: Final[str] = "gpt-oss-120b"
RUNTIME: Final[str] = "privatemode"
VERSION: Final[str] = "1.30.2"
IDENTITY: Final[str] = (
    "https://github.com/example/privatemode/.github/workflows/release.yml"
)
MEASUREMENT: Final[str] = "a" * 64
REVIEWED: Final[str] = "e" * 64
EGRESS: Final[str] = "api.privatemode.example:443"
PROMPT_CANARY: Final[str] = "journal-canary-7f3e plaintext entry"
KEY_CANARY: Final[str] = "pm-key-canary-91c2"


def _report(**overrides: object) -> dict[str, object]:
    """Return a sidecar report that matches the pin, with *overrides* applied."""
    report: dict[str, object] = {
        "status": "verified",
        "channel": "attested_tls",
        "signing_identity": IDENTITY,
        "measurement": MEASUREMENT,
        "transparency_log_entry": "rekor:108822",
        "version": VERSION,
        "egress": EGRESS,
        "runtime": {"name": RUNTIME, "version": VERSION},
        "models": [{"name": MODEL}],
    }
    report.update(overrides)
    return {key: value for key, value in report.items() if value is not _DROP}


_DROP: Final[object] = object()


def _answer(**overrides: object) -> dict[str, object]:
    """Return an OpenAI-compatible answer naming the pinned model."""
    answer: dict[str, object] = {
        "model": MODEL,
        "choices": [{"message": {"role": "assistant", "content": "a reflection"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }
    answer.update(overrides)
    return answer


class FakeSidecar:
    """A loopback listener standing in for the attesting sidecar.

    Attributes:
        routes: Path to ``(status, payload)`` it answers.
        seen: ``(method, path, headers, body)`` of each request, in order.
    """

    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        """Serve *routes* on an ephemeral loopback port."""
        self.routes = routes
        self.seen: list[tuple[str, str, dict[str, str], object]] = []
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            """Answer each request from the route table and record it."""

            def log_message(self, *_args: object) -> None:
                """Stay silent."""

            def _answer(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                sidecar.seen.append((method, self.path, dict(self.headers), body))
                status, payload = sidecar.routes.get(self.path, (404, {}))
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # stdlib handler name
                self._answer("GET")

            def do_POST(self) -> None:  # stdlib handler name
                self._answer("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The loopback origin it answers on."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def paths(self) -> list[str]:
        """The paths requested, in order."""
        return [path for _method, path, _headers, _body in self.seen]

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()


def _pin(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Put the Machine into sidecar mode against *url*, with the pin."""
    monkeypatch.setenv(POOL_URL_ENV, url)
    monkeypatch.setenv(POOL_VENDOR_ENV, "privatemode")
    monkeypatch.setenv(POOL_SIGNING_IDENTITY_ENV, IDENTITY)
    monkeypatch.setenv(POOL_MIN_VERSION_ENV, "1.30.0")
    monkeypatch.setenv(POOL_MEASUREMENTS_ENV, f"{MEASUREMENT},{REVIEWED}")
    monkeypatch.setenv(POOL_EGRESS_ENV, EGRESS)


@pytest.fixture(autouse=True)
def _fresh_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test its own vault limiter."""
    monkeypatch.setattr(pool_admission, "_VAULT_ADMISSION", {})


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSidecar]:
    """A matching sidecar, pinned."""
    fake = FakeSidecar(
        {
            POOL_ATTESTATION_PATH: (200, _report()),
            POOL_CHAT_COMPLETIONS_PATH: (200, _answer()),
        }
    )
    _pin(monkeypatch, fake.url)
    yield fake
    fake.close()


def _generate(prompt: str = PROMPT_CANARY, **kwargs: Any) -> Any:
    """Run one attested generation under a declared ``personal`` request."""
    with declared_pool_request("personal"):
        return call_attested_pool(model=MODEL, prompt=prompt, timeout=5.0, **kwargs)


# --------------------------------------------------------------------------
# T18: attestation — each refused with nothing sent; a matching one proceeds.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "verdict"),
    [
        pytest.param(
            _report(measurement=MEASUREMENT[:-1] + "b"),
            AttestationVerdict.MEASUREMENT_UNPINNED,
            id="tampered-measurement",
        ),
        pytest.param(
            _report(measurement="d" * 64),
            AttestationVerdict.MEASUREMENT_UNPINNED,
            id="unpinned-measurement",
        ),
        pytest.param(
            _report(signing_identity="https://github.com/someone-else/release.yml"),
            AttestationVerdict.FOREIGN_IDENTITY,
            id="foreign-signing-identity",
        ),
        pytest.param(
            _report(transparency_log_entry=_DROP),
            AttestationVerdict.NO_TRANSPARENCY_LOG,
            id="missing-transparency-log-entry",
        ),
        pytest.param(
            _report(transparency_log_entry="  "),
            AttestationVerdict.NO_TRANSPARENCY_LOG,
            id="blank-transparency-log-entry",
        ),
        pytest.param(
            _report(channel="tls"), AttestationVerdict.PLAIN_CHANNEL, id="plain-tls"
        ),
        pytest.param(
            _report(channel="plaintext"),
            AttestationVerdict.PLAIN_CHANNEL,
            id="plaintext",
        ),
        pytest.param(
            _report(status="failed"), AttestationVerdict.NOT_VERIFIED, id="failed"
        ),
        pytest.param(
            _report(version="1.29.9"),
            AttestationVerdict.VERSION_BELOW_MINIMUM,
            id="below-minimum-version",
        ),
        pytest.param(
            _report(version="latest"), AttestationVerdict.MALFORMED, id="bad-version"
        ),
        pytest.param(
            _report(egress="api.elsewhere.example:443"),
            AttestationVerdict.EGRESS_MISMATCH,
            id="foreign-egress",
        ),
        pytest.param(["not", "a", "report"], AttestationVerdict.MALFORMED, id="shape"),
    ],
)
def test_a_report_off_the_pin_is_refused_with_nothing_sent(
    sidecar: FakeSidecar, report: object, verdict: AttestationVerdict
) -> None:
    """T18: the prompt never leaves the vault process; no chat dial is made."""
    sidecar.routes[POOL_ATTESTATION_PATH] = (200, report)
    with pytest.raises(PoolAttestationError) as caught:
        _generate()
    assert caught.value.verdict is verdict
    assert sidecar.paths == [POOL_ATTESTATION_PATH]
    assert PROMPT_CANARY not in json.dumps([seen[3] for seen in sidecar.seen])


def test_a_matching_report_proceeds_with_the_closed_shape(sidecar: FakeSidecar) -> None:
    """T18 and T20: report first, then exactly one translated generation."""
    completion = _generate(max_tokens=128, system="be brief")

    assert completion.text == "a reflection"
    assert completion.usage == {"input_tokens": 12, "output_tokens": 5}
    assert sidecar.paths == [POOL_ATTESTATION_PATH, POOL_CHAT_COMPLETIONS_PATH]
    _method, _path, headers, body = sidecar.seen[1]
    assert body == {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": PROMPT_CANARY},
        ],
        "max_tokens": 128,
    }
    lowered = {name.lower(): value for name, value in headers.items()}
    assert "authorization" not in lowered
    assert "cache-control" not in lowered
    assert len(lowered[POOL_REQUEST_ID_HEADER.lower()]) == 32


def test_the_reviewed_measurement_is_admitted_inside_the_re_pin_window(
    sidecar: FakeSidecar,
) -> None:
    """Decision 3.2: both pinned measurements are served during the window."""
    sidecar.routes[POOL_ATTESTATION_PATH] = (200, _report(measurement=REVIEWED))
    assert _generate().text == "a reflection"


def test_an_unreachable_sidecar_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sidecar that is absent or dead is a refusal, never a fallback."""
    gone = FakeSidecar({})
    url = gone.url
    gone.close()
    _pin(monkeypatch, url)
    with pytest.raises(PoolAttestationError) as caught:
        _generate()
    assert caught.value.verdict is AttestationVerdict.UNREACHABLE


def test_a_sidecar_answering_no_report_refuses(sidecar: FakeSidecar) -> None:
    """A proxy that does not serve the report contract is refused, not trusted."""
    sidecar.routes[POOL_ATTESTATION_PATH] = (404, {})
    with pytest.raises(PoolAttestationError) as caught:
        _generate()
    assert caught.value.verdict is AttestationVerdict.UNREACHABLE
    assert POOL_CHAT_COMPLETIONS_PATH not in sidecar.paths


@pytest.mark.parametrize(
    "missing",
    [
        POOL_SIGNING_IDENTITY_ENV,
        POOL_MIN_VERSION_ENV,
        POOL_MEASUREMENTS_ENV,
        POOL_EGRESS_ENV,
    ],
)
def test_a_partial_pin_dials_nothing(
    sidecar: FakeSidecar, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """No complete pin, no dial at all — not even the report."""
    monkeypatch.delenv(missing)
    with pytest.raises(PoolAttestationError) as caught:
        _generate()
    assert caught.value.verdict is AttestationVerdict.UNPINNED
    assert sidecar.seen == []


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (POOL_MEASUREMENTS_ENV, "a" * 64 + "," + "b" * 64 + "," + "c" * 64),
        (POOL_MEASUREMENTS_ENV, "not-a-digest"),
        (POOL_MIN_VERSION_ENV, "latest"),
        (POOL_EGRESS_ENV, "https://api.privatemode.example"),
        (POOL_EGRESS_ENV, "10.0.0.1:443"),
        (POOL_EGRESS_ENV, "api.privatemode.example:0"),
    ],
)
def test_a_malformed_pin_is_no_pin(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """A three-entry window, a non-digest, a floating version or a URL egress."""
    _pin(monkeypatch, "http://127.0.0.1:1")
    monkeypatch.setenv(variable, value)
    assert load_pool_pin() is None
    assert judge_report(_report(), None) is AttestationVerdict.UNPINNED


@pytest.mark.parametrize(
    "url",
    [
        "https://api.privatemode.example",
        "http://localhost:8085",
        "http://10.0.0.5:8085",
        "https://127.0.0.1:8085",
    ],
)
def test_sidecar_mode_admits_only_a_plain_loopback_literal(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """T2 under a sidecar: the vault dials loopback; the sidecar owns egress."""
    _pin(monkeypatch, url)
    with pytest.raises(PoolDestinationError):
        pool_destination()


def test_sidecar_mode_admits_ipv6_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """``::1`` is loopback too."""
    _pin(monkeypatch, "http://[::1]:8085")
    assert pool_destination() == "http://[::1]:8085"


def test_attested_report_returns_the_verified_listing(sidecar: FakeSidecar) -> None:
    """The verified report doubles as the identity listing."""
    assert attested_report()["runtime"] == {"name": RUNTIME, "version": VERSION}


# --------------------------------------------------------------------------
# T20: the closed request shape — each refused 4xx with a content-free body.
# --------------------------------------------------------------------------


def _closed(**overrides: object) -> dict[str, object]:
    """Return a closed request that validates, with *overrides* applied."""
    body: dict[str, object] = {
        "identity": MODEL,
        "prompt": PROMPT_CANARY,
        "tier": "personal",
        "deadline_ms": 12_000,
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("body", "reason", "status"),
    [
        (_closed(model="another-model"), "client_model", 400),
        (_closed(temperature=0.2), "sampling_field", 400),
        (_closed(seed=7), "sampling_field", 400),
        (_closed(cache_control={"type": "ephemeral"}), "cache_field", 400),
        (_closed(prompt_cache_key="k"), "cache_field", 400),
        (_closed(lora="adapter-1"), "adapter_field", 400),
        (_closed(stream=True), "unknown_field", 400),
        (_closed(tier="intimate"), "intimate_tier", 400),
        (_closed(tier="secret"), "unknown_tier", 400),
        (_closed(tier=None), "unknown_tier", 400),
        (_closed(identity="another-model"), "identity_mismatch", 400),
        (
            _closed(
                prompt="x" * (POOL_PROMPT_TOKEN_CAP * POOL_PROMPT_CHARS_PER_TOKEN + 1)
            ),
            "prompt_too_long",
            400,
        ),
        (_closed(system="s" * (POOL_MAX_REQUEST_BYTES + 1)), "body_too_large", 413),
        (_closed(deadline_ms=12_001), "deadline_out_of_range", 400),
        (_closed(deadline_ms=0), "deadline_out_of_range", 400),
        (_closed(deadline_ms=True), "deadline_out_of_range", 400),
        (_closed(max_tokens=0), "max_tokens_out_of_range", 400),
        (_closed(max_tokens=10_000), "max_tokens_out_of_range", 400),
        (_closed(prompt=""), "malformed_field", 400),
        (_closed(system=7), "malformed_field", 400),
    ],
)
def test_a_request_outside_the_closed_shape_is_refused(
    body: dict[str, object], reason: str, status: int
) -> None:
    """Each refusal names its rule and nothing the caller sent."""
    with pytest.raises(PoolRequestRefusedError) as caught:
        validate_closed_request(body, pinned_identity=MODEL)
    assert caught.value.reason == reason
    assert caught.value.status == status
    assert 400 <= caught.value.status < 500
    assert PROMPT_CANARY not in str(caught.value)


def test_the_closed_shape_validates(sidecar: FakeSidecar) -> None:
    """A request inside the shape round-trips."""
    request = validate_closed_request(
        _closed(max_tokens=128, system="sys"), pinned_identity=MODEL
    )
    assert (request.tier, request.max_tokens, request.system) == (
        "personal",
        128,
        "sys",
    )
    assert sidecar.seen == []


def test_an_undeclared_request_is_refused_before_any_dial(
    sidecar: FakeSidecar,
) -> None:
    """INTIMATE cannot be declared, so it reaches this refusal with zero dials."""
    with declared_pool_request("intimate"), pytest.raises(PoolRequestRefusedError):
        call_attested_pool(model=MODEL, prompt=PROMPT_CANARY, timeout=5.0)
    assert sidecar.seen == []


def test_an_over_cap_prompt_is_refused_before_any_dial(sidecar: FakeSidecar) -> None:
    """The prompt cap is enforced in the vault before the report is read."""
    with pytest.raises(PoolRequestRefusedError):
        _generate("y" * (POOL_PROMPT_TOKEN_CAP * POOL_PROMPT_CHARS_PER_TOKEN + 1))
    assert sidecar.seen == []


# --------------------------------------------------------------------------
# Vendor outcomes: 401/403, insufficient_quota (non-transient), other 429s.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (401, {"error": {"code": "invalid_api_key"}}, PoolCredentialRejectedError),
        (403, {}, PoolCredentialRejectedError),
        (429, {"error": {"code": "insufficient_quota"}}, PoolQuotaExhaustedError),
        (429, {"error": {"type": "insufficient_quota"}}, PoolQuotaExhaustedError),
        (429, {"error": {"code": "rate_limit_exceeded"}}, PoolVendorRateLimitedError),
        (429, "busy", PoolVendorRateLimitedError),
        (500, {"error": {"message": PROMPT_CANARY}}, PoolAnswerUnusableError),
    ],
)
def test_a_vendor_refusal_maps_to_its_closed_outcome(
    sidecar: FakeSidecar, status: int, payload: object, error: type[Exception]
) -> None:
    """No upstream body is relayed: only a 429's error code is read."""
    sidecar.routes[POOL_CHAT_COMPLETIONS_PATH] = (status, payload)
    with pytest.raises(error) as caught:
        _generate()
    assert PROMPT_CANARY not in str(caught.value)


@pytest.fixture
def provider() -> PoolProvider:
    """A pool provider whose model is the pinned one."""
    return PoolProvider(LLMConfig(provider="pool", model=MODEL))


def test_a_spent_cap_is_not_a_rate_limit_at_the_provider(
    sidecar: FakeSidecar, provider: PoolProvider
) -> None:
    """``insufficient_quota`` is non-transient: ``temporarily_unavailable``.

    The route maps ``ProviderRateLimitError`` to ``rate_limited`` and every
    other ``RuntimeError`` to its refusal; a spent monthly cap must land on
    the second, because waiting cannot clear it.
    """
    sidecar.routes[POOL_CHAT_COMPLETIONS_PATH] = (
        429,
        {"error": {"code": "insufficient_quota"}},
    )
    with declared_pool_request("open"), pytest.raises(RuntimeError) as caught:
        provider.complete(PROMPT_CANARY)
    assert not isinstance(caught.value, ProviderRateLimitError)


def test_a_vendor_rate_limit_is_a_rate_limit_at_the_provider(
    sidecar: FakeSidecar, provider: PoolProvider
) -> None:
    """A transient vendor 429 is ``rate_limited``, which waiting clears."""
    sidecar.routes[POOL_CHAT_COMPLETIONS_PATH] = (429, {})
    with declared_pool_request("open"), pytest.raises(ProviderRateLimitError):
        provider.complete(PROMPT_CANARY)


def test_the_provider_answers_through_the_sidecar(
    sidecar: FakeSidecar, provider: PoolProvider
) -> None:
    """The O5 path end to end, at the provider seam."""
    with declared_pool_request("open"):
        assert provider.complete(PROMPT_CANARY).text == "a reflection"


@pytest.mark.parametrize(
    "answer",
    [
        _answer(model="another-model"),
        _answer(choices=[]),
        _answer(choices=[{"message": {"content": 7}}]),
        "not json object",
    ],
)
def test_an_unusable_answer_is_discarded(answer: object) -> None:
    """A different model's prose is never relabelled as the pinned one."""
    with pytest.raises(PoolAnswerUnusableError):
        completion_from_answer(answer, MODEL)


def test_usage_ignores_non_integer_counts() -> None:
    """A bogus count never reaches a caller as a number."""
    answer = _answer(usage={"prompt_tokens": True, "completion_tokens": "5"})
    assert completion_from_answer(answer, MODEL).usage is None


# --------------------------------------------------------------------------
# Identity and readiness in sidecar mode.
# --------------------------------------------------------------------------


def test_identity_is_judged_from_the_attested_report(sidecar: FakeSidecar) -> None:
    """T1 under O5: the manifest digest is compared with attested facts."""
    digest = pool_identity_digest(RUNTIME, VERSION, MODEL)
    assert check_pool_available(model_name=MODEL, inventory_digest=digest)
    assert not check_pool_available(model_name=MODEL, inventory_digest="0" * 64)
    sidecar.routes[POOL_ATTESTATION_PATH] = (200, _report(channel="tls"))
    assert not check_pool_available(model_name=MODEL, inventory_digest=digest)
    assert POOL_CHAT_COMPLETIONS_PATH not in sidecar.paths


@pytest.mark.parametrize(
    ("route", "status"),
    [
        ((200, _report()), ProbeStatus.POOL_READY),
        ((200, _report(measurement="d" * 64)), ProbeStatus.POOL_ATTESTATION_FAILED),
        ((404, {}), ProbeStatus.POOL_UNREACHABLE),
    ],
)
def test_the_pool_probe_reports_attestation(
    sidecar: FakeSidecar,
    monkeypatch: pytest.MonkeyPatch,
    route: tuple[int, object],
    status: ProbeStatus,
) -> None:
    """T13 under O5: ready is the cached verification; no generation is made."""
    manifest = type(
        "M",
        (),
        {
            "model_name": MODEL,
            "runtime_inventory_digest": pool_identity_digest(RUNTIME, VERSION, MODEL),
        },
    )()
    monkeypatch.setattr(container_health, "configured_manifest", lambda: manifest)
    sidecar.routes[POOL_ATTESTATION_PATH] = route
    assert container_health._pool_status() is status
    assert POOL_CHAT_COMPLETIONS_PATH not in sidecar.paths


def test_attestation_failure_has_its_own_exit_code() -> None:
    """Exit 34 is distinct from every other pool state."""
    codes = container_health._EXIT_CODES
    assert codes[ProbeStatus.POOL_ATTESTATION_FAILED] == 34
    assert list(codes.values()).count(34) == 1


# --------------------------------------------------------------------------
# T7, vault half: admission.
# --------------------------------------------------------------------------


def test_a_third_concurrent_generation_is_refused_at_once() -> None:
    """At most two in flight; the third is refused within 1 s."""
    admission = PoolAdmission()
    with admission.admit(), admission.admit():
        started = time.monotonic()
        with pytest.raises(PoolAdmissionRefusedError) as caught, admission.admit():
            pass
        assert time.monotonic() - started < 1.0
    assert caught.value.limit == IN_FLIGHT_LIMIT
    with admission.admit():
        pass


def test_the_sixty_first_in_an_hour_is_refused_and_the_window_rolls() -> None:
    """Sixty starts an hour by default; the window frees as it rolls."""
    now = [0.0]
    admission = PoolAdmission(clock=lambda: now[0])
    for _ in range(DEFAULT_POOL_RATE_PER_HOUR):
        with admission.admit():
            pass
    with pytest.raises(PoolAdmissionRefusedError) as caught, admission.admit():
        pass
    assert caught.value.limit == HOURLY_LIMIT
    now[0] = 3600.0
    with admission.admit():
        pass


def test_another_vault_is_unaffected() -> None:
    """One limiter per vault process: exhausting one leaves another whole."""
    first, second = PoolAdmission(rate_per_hour=1), PoolAdmission(rate_per_hour=1)
    with first.admit():
        pass
    with pytest.raises(PoolAdmissionRefusedError), first.admit():
        pass
    with second.admit():
        pass


@pytest.mark.parametrize(
    ("raw", "rate"), [("", 60), ("abc", 60), ("0", 60), ("-5", 60), ("90", 90)]
)
def test_the_rate_is_configurable_and_never_unlimited(raw: str, rate: int) -> None:
    """A bad override falls back to the default, never to unlimited."""
    assert configured_rate({POOL_RATE_ENV: raw}) == rate


def test_admission_limits_must_be_positive() -> None:
    """A zero limit would admit nothing silently."""
    with pytest.raises(ValueError, match="positive"):
        PoolAdmission(rate_per_hour=0)


def test_the_vault_limiter_is_one_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built once, from the Machine environment."""
    monkeypatch.setenv(POOL_RATE_ENV, "3")
    assert vault_admission() is vault_admission()


def test_an_admission_refusal_sends_nothing_and_is_rate_limited(
    sidecar: FakeSidecar, provider: PoolProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rate_limited`` at the provider, with zero dials to the sidecar."""
    monkeypatch.setenv(POOL_RATE_ENV, "1")
    with declared_pool_request("open"):
        provider.complete("first")
        sidecar.seen.clear()
        with pytest.raises(ProviderRateLimitError):
            provider.complete(PROMPT_CANARY)
    assert sidecar.seen == []


# --------------------------------------------------------------------------
# T3 credential canary and T21 telemetry.
# --------------------------------------------------------------------------

_ALLOWED_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "event",
        "outcome",
        "request_id",
        "latency_ms",
        "detail",
        "alert",
        "input_tokens",
        "output_tokens",
    }
)


def _records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return every decoded ``creek.pool`` record."""
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == TELEMETRY_LOGGER
    ]


@pytest.mark.parametrize("chat_status", [200, 401])
def test_no_key_and_no_content_in_any_record(
    sidecar: FakeSidecar,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    chat_status: int,
) -> None:
    """T3 and T21, on success and on a vendor 401.

    The vault never holds the key, so it cannot leak it; the records carry
    only the fields Decision 1 item 12 allows.
    """
    (tmp_path / "creek_pool_key").write_text(KEY_CANARY)
    sidecar.routes[POOL_CHAT_COMPLETIONS_PATH] = (
        chat_status,
        _answer() if chat_status == 200 else {"error": {"message": KEY_CANARY}},
    )
    caplog.set_level(logging.DEBUG)
    expectation = (
        pytest.raises(PoolCredentialRejectedError)
        if chat_status == 401
        else contextlib.nullcontext()
    )
    with expectation:
        _generate()
    records = _records(caplog)
    assert len(records) == 1
    assert set(records[0]) <= _ALLOWED_RECORD_KEYS
    text = caplog.text + json.dumps([seen[2] for seen in sidecar.seen])
    assert KEY_CANARY not in text
    assert PROMPT_CANARY not in caplog.text
    expected = "answered" if chat_status == 200 else "credential_rejected"
    assert records[0]["outcome"] == expected


def test_an_attestation_refusal_records_its_verdict_and_alert(
    sidecar: FakeSidecar, caplog: pytest.LogCaptureFixture
) -> None:
    """The record names the verdict and the alert kind, nothing else."""
    sidecar.routes[POOL_ATTESTATION_PATH] = (200, _report(channel="tls"))
    caplog.set_level(logging.INFO)
    with pytest.raises(PoolAttestationError):
        _generate()
    (record,) = _records(caplog)
    assert record["outcome"] == "attestation_refused"
    assert record["detail"] == "plain_channel"
    assert record["alert"] == "pool_attestation_failed"


def test_every_named_alert_is_in_the_closed_alert_set() -> None:
    """adepthood's receiver list must be widened by exactly these names."""
    assert set(OUTCOME_ALERTS.values()) <= {kind.value for kind in AlertKind}


# --------------------------------------------------------------------------
# T18 on the embeddings path (#1904 under #1896): every batch is gated too.
# --------------------------------------------------------------------------

EMBED_MODEL: Final[str] = "qwen3-embedding-4b"
EMBED_DIMENSIONS: Final[int] = 4
EMBED_PIN: Final[PoolEmbeddingPin] = PoolEmbeddingPin(
    model_name=EMBED_MODEL,
    dimensions=EMBED_DIMENSIONS,
    similarity_threshold=0.5,
    runtime_inventory_digest=pool_identity_digest(RUNTIME, VERSION, EMBED_MODEL),
)
_EMBED_TEXTS: Final[tuple[str, ...]] = ("embed-canary-one", "embed-canary-two")


def _embed_answer(count: int) -> dict[str, object]:
    """Return an OpenAI-compatible embeddings answer for *count* texts."""
    return {
        "model": EMBED_MODEL,
        "data": [
            {"index": index, "embedding": [0.1] * EMBED_DIMENSIONS}
            for index in range(count)
        ],
    }


@pytest.fixture
def embed_sidecar(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSidecar]:
    """A matching sidecar that also answers embeddings, pinned."""
    fake = FakeSidecar(
        {
            POOL_ATTESTATION_PATH: (
                200,
                _report(models=[{"name": MODEL}, {"name": EMBED_MODEL}]),
            ),
            POOL_EMBEDDINGS_PATH: (200, _embed_answer(len(_EMBED_TEXTS))),
        }
    )
    _pin(monkeypatch, fake.url)
    yield fake
    fake.close()


def _embed() -> list[list[float] | None]:
    """Embed the canary texts as ``personal`` items."""
    items = [(text, PrivacyTier.PERSONAL) for text in _EMBED_TEXTS]
    return PoolEmbedder(EMBED_PIN).embed(items)


def test_a_matching_report_lets_the_embeddings_batch_proceed(
    embed_sidecar: FakeSidecar,
) -> None:
    """Matching: the batch goes out as ``model`` and ``input`` only."""
    vectors = _embed()

    assert vectors == [[0.1] * EMBED_DIMENSIONS] * len(_EMBED_TEXTS)
    assert embed_sidecar.paths[-1] == POOL_EMBEDDINGS_PATH
    # Attestation is read for the identity check and again before the batch.
    assert embed_sidecar.paths[-2] == POOL_ATTESTATION_PATH
    assert embed_sidecar.seen[-1][3] == {
        "model": EMBED_MODEL,
        "input": list(_EMBED_TEXTS),
    }


@pytest.mark.parametrize(
    "report",
    [
        pytest.param(
            _report(
                measurement=MEASUREMENT[:-1] + "b",
                models=[{"name": EMBED_MODEL}],
            ),
            id="tampered-measurement",
        ),
        pytest.param(
            _report(measurement="d" * 64, models=[{"name": EMBED_MODEL}]),
            id="unpinned-measurement",
        ),
        pytest.param(
            _report(channel="tls", models=[{"name": EMBED_MODEL}]),
            id="plain-tls",
        ),
    ],
)
def test_a_report_off_the_pin_sends_no_embeddings_text(
    embed_sidecar: FakeSidecar, report: object
) -> None:
    """T18: no request reaches ``/v1/embeddings``; the pass is ungrounded."""
    embed_sidecar.routes[POOL_ATTESTATION_PATH] = (200, report)

    assert _embed() == [None] * len(_EMBED_TEXTS)
    assert POOL_EMBEDDINGS_PATH not in embed_sidecar.paths
    assert "embed-canary" not in json.dumps([seen[3] for seen in embed_sidecar.seen])


def test_a_report_that_turns_bad_after_the_identity_check_stops_the_batch(
    embed_sidecar: FakeSidecar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate runs before each batch, not only once per pass, like generation."""
    reports = iter([_report(models=[{"name": EMBED_MODEL}]), _report(status="failed")])
    real = pool_attestation.pool_attestation_report

    def flip(**kwargs: Any) -> Any:
        embed_sidecar.routes[POOL_ATTESTATION_PATH] = (200, next(reports))
        return real(**kwargs)

    monkeypatch.setattr(pool_attestation, "pool_attestation_report", flip)

    assert _embed() == [None] * len(_EMBED_TEXTS)
    assert POOL_EMBEDDINGS_PATH not in embed_sidecar.paths


def test_outside_sidecar_mode_the_closed_envelope_is_sent_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The O3 shape keeps its tier and deadline; no report is read."""
    monkeypatch.delenv(POOL_VENDOR_ENV, raising=False)
    envelope: dict[str, object] = {"model": EMBED_MODEL, "input": ["x"], "tier": "open"}
    assert pool_embeddings._attested_payload(envelope, EMBED_MODEL, ["x"]) is envelope


def test_a_managed_vault_answers_through_the_sidecar_labelled_attested(
    sidecar: FakeSidecar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2040: sidecar mode is the one route that earns ``pool:attested:``.

    On a managed Machine — where a named vendor without this gate is refused
    before any dial — the attested sidecar still answers, after its report,
    and the stage is labelled with the sidecar vendor.
    """
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")

    assert _generate().text == "a reflection"
    assert sidecar.paths == [POOL_ATTESTATION_PATH, POOL_CHAT_COMPLETIONS_PATH]
    assert stage_inference_source("pool") == "pool:attested:privatemode"
