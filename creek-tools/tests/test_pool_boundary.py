"""The shared inference pool's one pinned destination beyond loopback (#1894).

:mod:`creek.classify.llm.pool_boundary` is the pool's half of what
:mod:`creek.classify.llm.local_boundary` is for Ollama: the only module that
turns an inference destination into a request. ADR-0016 Decision 1 item 2
allows exactly one destination beyond loopback, read from the Machine
environment and never from vault config, and item 7 requires a cancel handle
that closes the socket rather than abandoning the worker thread.

Every test here is hermetic: the only host dialled is a loopback socket this
module opened, so no vendor is contacted and nothing is billed (owner
Decision 4, no spend yet).
"""

from __future__ import annotations

import datetime
import inspect
import ssl
import threading
import time
from typing import TYPE_CHECKING, Final

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from creek.classify.llm import pool_boundary
from creek.classify.llm.pool_boundary import (
    POOL_CONNECT_TIMEOUT,
    POOL_GENERATE_PATH,
    POOL_MAX_REQUEST_BYTES,
    POOL_MODELS_PATH,
    POOL_REQUEST_TIMEOUT,
    POOL_URL_ENV,
    PoolDestinationError,
    PoolDial,
    PoolDialCancelledError,
    PoolRequestTooLargeError,
    PoolUnreachableError,
    admit_pool_url,
    open_pool_dial,
    pool_destination,
    pool_generate,
    pool_list_models,
    pool_url_configured,
)
from tests.pool_support import FakePool, SocketSpy

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CANCEL_DEADLINE: Final[float] = 1.0
"""ADR-0016 T23: the socket must close within one second of the cancel."""

_CA_VALID_DAYS: Final[int] = 1


def _self_signed_ca() -> bytes:
    """Return a throwaway self-signed CA, PEM encoded.

    Built here rather than borrowed from a trust bundle so the test owns
    exactly one certificate and can assert that the configured file — and
    nothing else — became the trust store. It is never presented to anything:
    no TLS handshake happens anywhere in this suite.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "creek-test-ca")])
    now = datetime.datetime.now(tz=datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def fake_pool() -> Iterator[FakePool]:
    """Serve a canned-response loopback pool for the duration of a test."""
    pool = FakePool(body=b'{"ok": true}')
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def hanging_pool() -> Iterator[FakePool]:
    """Serve a loopback pool that accepts and then never answers."""
    pool = FakePool(hang=True)
    try:
        yield pool
    finally:
        pool.close()


def _pin(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Pin the Machine-environment destination to *url*."""
    monkeypatch.setenv(POOL_URL_ENV, url)


class TestDestination:
    """The destination is a Machine-environment fact, and only one host."""

    def test_an_unconfigured_destination_refuses_without_a_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset variable refuses, and the message names no URL."""
        monkeypatch.delenv(POOL_URL_ENV, raising=False)

        assert pool_url_configured() is False
        with pytest.raises(PoolDestinationError) as caught:
            pool_destination()
        assert "127.0.0.1" not in str(caught.value)

    def test_a_configured_destination_is_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Surrounding whitespace and a trailing slash are stripped."""
        _pin(monkeypatch, "  https://pool.vendor.test/  ")

        assert pool_url_configured() is True
        assert pool_destination() == "https://pool.vendor.test"

    @pytest.mark.parametrize(
        "configured",
        [
            pytest.param("", id="blank"),
            pytest.param("   ", id="whitespace"),
            pytest.param("ftp://pool.vendor.test", id="scheme"),
            pytest.param("pool.vendor.test", id="no-scheme"),
            pytest.param("https://", id="no-host"),
            pytest.param("https://user:pw@pool.vendor.test", id="userinfo"),
            pytest.param("https://pool.vendor.test?x=1", id="query"),
            pytest.param("https://pool.vendor.test#f", id="fragment"),
            pytest.param("https://pool.vendor.test:99999", id="bad-port"),
            pytest.param("https://pool.vendor.test:", id="empty-port"),
            pytest.param("https://pool.vendor.test/v1", id="path"),
        ],
    )
    def test_a_malformed_destination_is_refused_url_free(
        self, monkeypatch: pytest.MonkeyPatch, configured: str
    ) -> None:
        """Every malformed destination refuses, echoing none of its text."""
        _pin(monkeypatch, configured)

        with pytest.raises(PoolDestinationError) as caught:
            pool_destination()
        assert "pool.vendor.test" not in str(caught.value)

    def test_only_the_pinned_host_is_admitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another hostname, an IP literal or a config URL is refused."""
        _pin(monkeypatch, "https://pool.vendor.test")

        assert admit_pool_url("https://pool.vendor.test") == (
            "https://pool.vendor.test"
        )
        for offered in (
            "https://other.vendor.test",
            "https://203.0.113.9",
            "http://127.0.0.1:11434",
            "https://pool.vendor.test.evil.test",
        ):
            with pytest.raises(PoolDestinationError) as caught:
                admit_pool_url(offered)
            assert offered not in str(caught.value)

    def test_a_loopback_sidecar_destination_is_admitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vendor sidecar's loopback proxy is the pinned destination."""
        _pin(monkeypatch, "http://127.0.0.1:8080")

        assert admit_pool_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"

    def test_the_boundary_accepts_no_vault_configuration(self) -> None:
        """No entry point takes an ``LLMConfig``, so config cannot redirect it."""
        for entry in (open_pool_dial, pool_generate, pool_list_models, admit_pool_url):
            names = set(inspect.signature(entry).parameters)
            assert not names & {"config", "cfg", "llm", "url_from_config"}, entry


class TestBudget:
    """The dial never outlives the vault request's remaining budget."""

    def test_timeouts_are_capped_by_the_boundary_and_by_the_budget(
        self, monkeypatch: pytest.MonkeyPatch, fake_pool: FakePool
    ) -> None:
        """A generous budget keeps the boundary's ceiling; a tight one wins."""
        _pin(monkeypatch, fake_pool.url)

        generous = open_pool_dial(budget=600.0)
        tight = open_pool_dial(budget=0.25)
        try:
            assert generous.request_timeout == POOL_REQUEST_TIMEOUT
            assert generous.connect_timeout == POOL_CONNECT_TIMEOUT
            assert tight.request_timeout == pytest.approx(0.25)
            assert tight.connect_timeout == pytest.approx(0.25)
        finally:
            generous.close()
            tight.close()

    @pytest.mark.parametrize("budget", [0.0, -1.5])
    def test_an_exhausted_budget_refuses_before_any_dial(
        self, monkeypatch: pytest.MonkeyPatch, budget: float
    ) -> None:
        """A deadline already past refuses with zero dials to any host."""
        _pin(monkeypatch, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolDialCancelledError):
            pool_generate({"prompt": "x"}, budget=budget)
        assert spy.destinations == []

    def test_an_oversized_payload_is_refused_before_any_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body past the boundary's cap never reaches a socket."""
        _pin(monkeypatch, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolRequestTooLargeError):
            pool_generate({"prompt": "x" * (POOL_MAX_REQUEST_BYTES + 1)})
        assert spy.destinations == []


class TestDialling:
    """One attempt, one destination, and a refusal on anything else."""

    def test_a_pool_response_carries_its_status_and_decoded_payload(
        self, monkeypatch: pytest.MonkeyPatch, fake_pool: FakePool
    ) -> None:
        """The happy path reads the body once and decodes it."""
        _pin(monkeypatch, fake_pool.url)

        response = pool_list_models()

        assert response.status == 200
        assert response.payload == {"ok": True}

    def test_the_generate_path_posts_to_the_pinned_destination(
        self, monkeypatch: pytest.MonkeyPatch, fake_pool: FakePool
    ) -> None:
        """A generate carries a body and still lands on the one destination."""
        _pin(monkeypatch, fake_pool.url)

        response = pool_generate({"prompt": "hello"})

        assert response.status == 200
        assert fake_pool.accepted == 1

    def test_a_failed_dial_is_never_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused connection yields one attempt, not a retry loop."""
        _pin(monkeypatch, "https://pool.vendor.test")
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolUnreachableError):
            pool_generate({"prompt": "x"})
        assert len(spy.destinations) == 1
        assert spy.destinations[0][0] == "pool.vendor.test"

    def test_an_undecodable_body_is_a_refusal_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body that is not JSON refuses rather than raising ``ValueError``."""
        pool = FakePool(body=b"not json")
        _pin(monkeypatch, pool.url)
        try:
            with pytest.raises(PoolUnreachableError):
                pool_list_models()
        finally:
            pool.close()

    def test_the_two_published_paths_are_distinct_and_rooted(self) -> None:
        """The boundary owns both pool paths; no caller spells one itself."""
        assert POOL_MODELS_PATH.startswith("/")
        assert POOL_GENERATE_PATH.startswith("/")
        assert POOL_MODELS_PATH != POOL_GENERATE_PATH


class TestCancellation:
    """ADR-0016 T23: the handle closes the socket, it does not abandon it."""

    def test_cancel_closes_the_socket_and_the_worker_thread_exits(
        self, monkeypatch: pytest.MonkeyPatch, hanging_pool: FakePool
    ) -> None:
        """A held connection is interrupted within the one-second deadline."""
        _pin(monkeypatch, hanging_pool.url)
        dial = open_pool_dial(budget=600.0)
        outcome: list[str] = []

        def worker() -> None:
            try:
                dial.request(POOL_GENERATE_PATH, payload={"prompt": "x"})
            except BaseException as exc:
                outcome.append(type(exc).__name__)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while hanging_pool.accepted == 0:
            time.sleep(0.01)

        dial.cancel()
        thread.join(_CANCEL_DEADLINE)

        assert not thread.is_alive()
        assert outcome == [PoolDialCancelledError.__name__]

    def test_cancel_before_the_dial_refuses_without_connecting(
        self, monkeypatch: pytest.MonkeyPatch, fake_pool: FakePool
    ) -> None:
        """A handle cancelled first never opens a socket at all."""
        _pin(monkeypatch, fake_pool.url)
        dial = open_pool_dial()
        dial.cancel()

        with pytest.raises(PoolDialCancelledError):
            dial.request(POOL_MODELS_PATH, payload=None)
        assert fake_pool.accepted == 0

    def test_cancel_is_idempotent_and_safe_after_close(
        self, monkeypatch: pytest.MonkeyPatch, fake_pool: FakePool
    ) -> None:
        """Cancelling twice, or after a close, raises nothing."""
        _pin(monkeypatch, fake_pool.url)
        dial = open_pool_dial()
        dial.request(POOL_MODELS_PATH, payload=None)

        dial.close()
        dial.cancel()
        dial.cancel()

        assert isinstance(dial, PoolDial)

    def test_the_dial_is_a_context_manager_that_closes_itself(
        self, monkeypatch: pytest.MonkeyPatch, fake_pool: FakePool
    ) -> None:
        """``with`` closes the connection even when the body raises."""
        _pin(monkeypatch, fake_pool.url)

        with pytest.raises(RuntimeError), open_pool_dial() as dial:
            dial.request(POOL_MODELS_PATH, payload=None)
            msg = "caller failed after the dial"
            raise RuntimeError(msg)


class TestTlsTrust:
    """``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` widen trust, never redirect.

    The pool boundary rebuilds the same CA mapping the local boundary does, so
    an operator's private CA is honoured identically on both. Neither variable
    can move the destination — only change which certificate the pinned host
    may present.
    """

    def test_a_ca_file_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A readable bundle becomes the trust store, and only that bundle."""
        bundle = tmp_path / "private-ca.pem"
        bundle.write_bytes(_self_signed_ca())
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
        context = pool_boundary._pool_verify()

        assert context.verify_mode is ssl.CERT_REQUIRED
        assert len(context.get_ca_certs()) == 1

    def test_a_ca_directory_alone_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With only the directory set, that is the trust store."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))

        assert pool_boundary._pool_verify().verify_mode is ssl.CERT_REQUIRED

    def test_with_neither_set_the_default_roots_apply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No operator CA means the bundled roots, never no verification."""
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        context = pool_boundary._pool_verify()

        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True
