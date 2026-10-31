"""The pool boundary's embeddings dial and its response cap (#1904, #1906).

ADR-0016 Decision 1 item 13 (Q2 option (i)) puts grounding embeddings on the
same one pinned destination as generation, so the dial is owned by the same
module: :func:`~creek.classify.llm.pool_boundary.pool_embed` is the only place
an embeddings request is turned into bytes on a socket, and it inherits every
property the generation dial has — the stdlib transport that reads no proxy
variable, the 1 MiB request cap, no retries.

Two properties are new here, and both are pinned at their boundary:

- **The batch cap.** More than :data:`POOL_MAX_EMBED_BATCH` inputs is refused
  before any socket opens (Decision 1 items 4 and 13).
- **The response cap (#1906).** Every pool answer is read incrementally up to a
  ceiling and refused one byte over it, so a misbehaving pool cannot grow the
  vault's memory for twelve seconds on a 1 GB Machine. The refusal is its own
  type, distinguishable from a timeout and from an undecodable answer.

Hermetic: every host dialled is a loopback listener this module opened.
"""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING, Final

import pytest

from creek.classify.llm.pool_boundary import (
    POOL_EMBEDDINGS_PATH,
    POOL_GENERATE_PATH,
    POOL_MAX_EMBED_BATCH,
    POOL_MAX_EMBED_RESPONSE_BYTES,
    POOL_MAX_RESPONSE_BYTES,
    POOL_MODELS_PATH,
    POOL_URL_ENV,
    PoolBoundaryError,
    PoolRequestTooLargeError,
    PoolResponseTooLargeError,
    PoolUnreachableError,
    pool_embed,
    pool_generate,
    pool_list_models,
)
from tests.pool_support import FakePool, SocketSpy

if TYPE_CHECKING:
    from collections.abc import Iterator

_JSON_PADDING: Final[int] = len(json.dumps({"p": ""}).encode("utf-8"))
"""Bytes a ``{"p": "..."}`` body adds around its string value."""

_FAKE_PROXY: Final[str] = "http://proxy.invalid:3128"
"""An obviously fake proxy; a dial aimed at it would show up in the spy."""


def _pin(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Pin the Machine-environment destination to *url*."""
    monkeypatch.setenv(POOL_URL_ENV, url)


def _body_of_size(size: int) -> bytes:
    """Return a JSON document of exactly *size* bytes."""
    return json.dumps({"p": "x" * (size - _JSON_PADDING)}).encode("utf-8")


@pytest.fixture
def embeddings_pool() -> Iterator[FakePool]:
    """Serve a loopback pool answering any path with a tiny embeddings body."""
    pool = FakePool.serving({"model": "m", "data": []})
    try:
        yield pool
    finally:
        pool.close()


class TestEmbeddingsDial:
    """``pool_embed`` is the one embeddings dial, and it is the pinned one."""

    def test_the_embeddings_path_is_owned_by_the_boundary(self) -> None:
        """A third rooted path, distinct from the other two."""
        assert POOL_EMBEDDINGS_PATH.startswith("/")
        assert len({POOL_EMBEDDINGS_PATH, POOL_GENERATE_PATH, POOL_MODELS_PATH}) == 3

    def test_an_embeddings_request_posts_to_the_pinned_destination(
        self, monkeypatch: pytest.MonkeyPatch, embeddings_pool: FakePool
    ) -> None:
        """One POST, to the embeddings path, carrying exactly the payload given."""
        _pin(monkeypatch, embeddings_pool.url)
        payload = {"model": "m", "input": ["a title"], "tier": "open"}

        response = pool_embed(payload, headers={"X-Test": "1"})

        assert response.status == 200
        assert embeddings_pool.requests == [("POST", POOL_EMBEDDINGS_PATH)]
        assert embeddings_pool.bodies == [payload]

    def test_a_batch_over_the_cap_is_refused_before_any_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """257 inputs: refused, and the socket spy records nothing."""
        _pin(monkeypatch, "http://pool.internal:8443")
        spy = SocketSpy(monkeypatch)
        inputs = ["t"] * (POOL_MAX_EMBED_BATCH + 1)

        with pytest.raises(PoolRequestTooLargeError) as caught:
            pool_embed({"model": "m", "input": inputs, "tier": "open"})

        assert spy.destinations == []
        assert "t" not in str(caught.value).split()

    def test_a_batch_at_the_cap_is_sent(
        self, monkeypatch: pytest.MonkeyPatch, embeddings_pool: FakePool
    ) -> None:
        """Exactly 256 inputs is the boundary, and it is admitted."""
        _pin(monkeypatch, embeddings_pool.url)
        inputs = ["t"] * POOL_MAX_EMBED_BATCH

        pool_embed({"model": "m", "input": inputs, "tier": "open"})

        assert embeddings_pool.accepted == 1

    def test_a_body_over_one_mebibyte_is_refused_before_any_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One enormous title: refused by the request cap, nothing dialled."""
        _pin(monkeypatch, "http://pool.internal:8443")
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolRequestTooLargeError):
            pool_embed({"model": "m", "input": ["x" * (1 << 20)], "tier": "open"})

        assert spy.destinations == []

    def test_an_ambient_proxy_does_not_move_the_embeddings_dial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1933's question, asked of this dial: the environment cannot choose.

        With every proxy variable pointing at a fake host, the only TCP
        destination attempted is still the pinned pool host — the stdlib
        transport reads none of them. The negative control (variables unset)
        records the identical destination, so the assertion proves the
        property rather than the absence of a proxy.
        """
        _pin(monkeypatch, "http://pool.internal:8443")
        payload = {"model": "m", "input": ["t"], "tier": "open"}
        recorded: list[list[tuple[str, int]]] = []
        for proxied in (True, False):
            for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy"):
                if proxied:
                    monkeypatch.setenv(name, _FAKE_PROXY)
                else:
                    monkeypatch.delenv(name, raising=False)
            spy = SocketSpy(monkeypatch)
            with pytest.raises(PoolUnreachableError) as caught:
                pool_embed(payload)
            recorded.append(spy.destinations)
            assert "proxy.invalid" not in str(caught.value)

        assert recorded == [[("pool.internal", 8443)], [("pool.internal", 8443)]]


class TestResponseCap:
    """#1906: the answer is bounded the way the request already was."""

    def test_the_caps_are_ordered_and_the_generation_cap_mirrors_the_request(
        self,
    ) -> None:
        """Generation and listing get 1 MiB; an embeddings batch gets more."""
        assert POOL_MAX_RESPONSE_BYTES == 1 << 20
        assert POOL_MAX_EMBED_RESPONSE_BYTES > POOL_MAX_RESPONSE_BYTES

    def test_a_response_exactly_at_the_cap_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The boundary case: ``cap`` bytes is admitted."""
        pool = FakePool(body=_body_of_size(POOL_MAX_RESPONSE_BYTES))
        _pin(monkeypatch, pool.url)
        try:
            response = pool_generate({"prompt": "p"})
        finally:
            pool.close()

        assert response.status == 200

    def test_one_byte_over_the_cap_is_refused_content_free(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``cap + 1`` bytes is refused, and the refusal carries no body."""
        pool = FakePool(body=_body_of_size(POOL_MAX_RESPONSE_BYTES + 1))
        url = pool.url
        _pin(monkeypatch, url)
        try:
            with pytest.raises(PoolResponseTooLargeError) as caught:
                pool_generate({"prompt": "p"})
        finally:
            pool.close()

        message = str(caught.value)
        assert "xxxx" not in message
        assert url not in message
        assert str(POOL_MAX_RESPONSE_BYTES) not in message

    def test_the_refusal_is_distinct_from_unreachable_and_undecodable(
        self,
    ) -> None:
        """An operator can tell the three failures apart from the type alone."""
        assert issubclass(PoolResponseTooLargeError, PoolBoundaryError)
        assert not issubclass(PoolResponseTooLargeError, PoolUnreachableError)
        assert not issubclass(PoolUnreachableError, PoolResponseTooLargeError)

    def test_the_identity_listing_inherits_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--check pool`` reads through the same dial, so the same cap."""
        pool = FakePool(body=_body_of_size(POOL_MAX_RESPONSE_BYTES + 1))
        _pin(monkeypatch, pool.url)
        try:
            with pytest.raises(PoolResponseTooLargeError):
                pool_list_models()
        finally:
            pool.close()

    def test_an_embeddings_answer_may_exceed_the_generation_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full batch of vectors is legitimately larger than a reflection."""
        pool = FakePool(body=_body_of_size(POOL_MAX_RESPONSE_BYTES + 1))
        _pin(monkeypatch, pool.url)
        try:
            response = pool_embed({"model": "m", "input": ["t"], "tier": "open"})
        finally:
            pool.close()

        assert response.status == 200

    def test_the_read_is_incremental_and_stops_at_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body with no declared length is read in chunks, not whole.

        The listener streams far more than the cap with no ``Content-Length``
        and never closes; a ``read()``-then-check would block on it until the
        socket timeout. Refusing promptly is only possible if the read stops
        at the cap.
        """
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        _pin(monkeypatch, f"http://127.0.0.1:{port}")

        import threading

        def _serve() -> None:
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 X\r\nContent-Type: application/json\r\n\r\n")
            chunk = b"x" * 65536
            sent = 0
            try:
                while sent <= 4 * POOL_MAX_RESPONSE_BYTES:
                    conn.sendall(chunk)
                    sent += len(chunk)
            except OSError:
                return
            finally:
                conn.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            with pytest.raises(PoolResponseTooLargeError):
                pool_generate({"prompt": "p"})
        finally:
            listener.close()
            thread.join(timeout=5)
