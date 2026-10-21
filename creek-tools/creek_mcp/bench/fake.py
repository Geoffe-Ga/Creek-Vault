"""The deterministic models behind ``--mode fake``: the local fake and the pool.

:class:`FakeLLM` answers every prompt with one well-formed margin note whose
quote is the first words of the prompt's ENTRY section, so ``reflect_tool``
parses it, verifies the quote as verbatim, and returns ``ok`` — the full
reflect path runs with no model, key or network. It is positively local
(``is_cloud = False``), so it passes :class:`LocalOnlyFactory` like a real
local runtime would.

:class:`FakePool` answers the same note over the pool's own request and
response shape, so ``--provider pool --mode fake`` runs every sweep against
:class:`~creek_mcp.bench.pool_client.BenchPoolClient` with no vendor
contacted and nothing billed (ADR-0016 Decision 4: no spend yet). Its knobs
are the refusal paths a pool run must classify: an identity the listing does
not match, a foreign echo, an unverified attestation, ``rate_limited`` and
``insufficient_quota``. It is a responder, not a transport: :meth:`answer`
takes a method, a path and a decoded body, so the same fake serves both an
``httpx`` transport (hermetic, in process) and a real loopback listener.

Their latency is the harness's own overhead (corpus walk, prompt build,
parse), which is the useful floor a fake run measures.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx

from creek_mcp.bench.pool_client import POOL_GENERATE_PATH, POOL_MODELS_PATH

if TYPE_CHECKING:
    from creek.models import PrivacyTier

_ENTRY_MARKER: Final[str] = "ENTRY:\n"
_QUOTE_WORDS: Final[int] = 3
_NOTE: Final[str] = "You keep returning to this image."
_TOO_MANY_REQUESTS: Final[int] = 429
_OK: Final[int] = 200
_UNSERVED_PATH: Final[str] = (
    "the fake pool serves only the identity listing and the generate path"
)


def anchored_note(prompt: str) -> str:
    """Return one verbatim-anchored margin note for *prompt*, as JSON text.

    The quote is the first words of the prompt's ENTRY section, which is what
    makes ``reflect_tool`` accept the note instead of answering ``empty``.

    Args:
        prompt: The fully-formatted reflect prompt.

    Returns:
        The model-shaped JSON answer.
    """
    entry = prompt.rsplit(_ENTRY_MARKER, 1)[-1]
    quote = " ".join(entry.split()[:_QUOTE_WORDS])
    return json.dumps({"notes": [{"quote": quote, "kind": "pattern", "note": _NOTE}]})


class FakeLLM:
    """A deterministic, local completion callable."""

    is_cloud: bool = False

    def __call__(self, prompt: str) -> str:
        """Return one note quoting the start of the prompt's entry."""
        return anchored_note(prompt)


def fake_factory(tier: PrivacyTier, *, max_tokens: int) -> FakeLLM:
    """Build a :class:`FakeLLM`; tier and budget do not change its answer."""
    del tier, max_tokens
    return FakeLLM()


@dataclass
class FakePool:
    """A deterministic inference pool, with every refusal path as a knob.

    Attributes:
        identity: The identity the client pins.
        model_tag: The model the listing offers and answers echo.
        measurement_digest: The digest the attestation carries.
        served_identity: What the *listing* advertises; defaults to
            :attr:`identity`. Set it to refuse a run before a prompt is sent.
        echo_identity: The identity an answer echoes; defaults to the one the
            request named, which is what a correctly routed pool does.
        echo_model: The model an answer echoes; defaults to the request's.
        attested: Whether the listing reports a verified attestation.
        refusal_code: When set, every generation answers 429 with this code.
        status: The status a generation answers with (``200`` by default).
        prompt_tokens: The prompt size the answer reports in ``usage``.
        drop_response_text: Answer with no ``response`` field at all.
        listing_payload: Replace the whole listing body (to test a malformed
            one).
        listing_calls: How many identity listings were served.
        generate_calls: How many generations were served — the call spy that
            proves a refusal sent no prompt.
        requests: The decoded body of every request, in order.
        paths: The path of every request, in order.
    """

    identity: str
    model_tag: str
    measurement_digest: str | None = None
    served_identity: str | None = None
    echo_identity: str | None = None
    echo_model: str | None = None
    attested: bool = True
    refusal_code: str | None = None
    status: int = _OK
    prompt_tokens: int | None = None
    drop_response_text: bool = False
    listing_payload: object | None = None
    listing_calls: int = 0
    generate_calls: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def answer(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, object]:
        """Return the status and decoded body this pool answers with.

        Args:
            method: The HTTP method.
            path: The request path.
            body: The decoded request body, or ``None`` for a read.

        Returns:
            The status code and the JSON-serialisable answer.

        Raises:
            ValueError: For any other path or method. A real pool would answer
                a status, but this fake deliberately does not: a status that
                distinguishes a path that exists from one that does not is the
                existence oracle ``tests/test_v1_api_structure.py`` keeps out
                of ``creek_mcp`` (#1098). Nothing in the harness dials another
                path, so reaching this is a harness bug, not pool behaviour.
        """
        self.paths.append(path)
        if body is not None:
            self.requests.append(body)
        if path == POOL_MODELS_PATH and method == "GET":
            self.listing_calls += 1
            return _OK, self._listing()
        if path == POOL_GENERATE_PATH and method == "POST":
            self.generate_calls += 1
            return self._generation(body or {})
        raise ValueError(_UNSERVED_PATH)

    def attested_digest(self) -> str:
        """Return the measurement-set digest this pool attests.

        Derived from the identity it serves when the caller named none, so a
        hermetic run is reproducible and self-consistent without an operator
        inventing a digest for a pool that does not exist.

        Returns:
            A ``sha256:<hex>`` digest.
        """
        if self.measurement_digest is not None:
            return self.measurement_digest
        seed = f"{self.identity}\n{self.model_tag}".encode()
        return f"sha256:{hashlib.sha256(seed).hexdigest()}"

    def _listing(self) -> object:
        """Return the identity listing, or the override under test."""
        if self.listing_payload is not None:
            return self.listing_payload
        return {
            "identity": self.served_identity or self.identity,
            "models": [{"name": self.model_tag}],
            "attestation": {
                "verified": self.attested,
                "measurement_digest": self.attested_digest(),
            },
        }

    def _generation(self, body: dict[str, Any]) -> tuple[int, object]:
        """Return the generation answer for *body*, or a refusal."""
        if self.refusal_code is not None:
            return _TOO_MANY_REQUESTS, {"code": self.refusal_code}
        if self.status != _OK:
            return self.status, {"code": "unavailable"}
        prompt = str(body.get("prompt", ""))
        answer: dict[str, Any] = {
            "identity": self.echo_identity or body.get("identity"),
            "model": self.echo_model or body.get("model"),
            "usage": {"prompt_tokens": self._prompt_tokens(prompt)},
        }
        if not self.drop_response_text:
            answer["response"] = anchored_note(prompt)
        return _OK, answer

    def _prompt_tokens(self, prompt: str) -> int:
        """Return the prompt size to report: the override, or a word count."""
        return len(prompt.split()) if self.prompt_tokens is None else self.prompt_tokens

    def _respond(self, request: httpx.Request) -> httpx.Response:
        """Adapt one ``httpx`` request onto :meth:`answer`."""
        body = json.loads(request.content) if request.content else None
        status, payload = self.answer(request.method, request.url.path, body)
        return httpx.Response(status, json=payload)

    def transport(self) -> httpx.MockTransport:
        """Return an in-process transport serving this pool."""
        return httpx.MockTransport(self._respond)
