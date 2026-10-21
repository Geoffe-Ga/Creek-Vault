"""The benchmark's pool client: pinned identity, attestation, admission codes.

The sibling of :class:`~creek_mcp.bench.ollama_client.BenchOllamaClient` for
the shared confidential warm-inference pool ADR-0016 selects (option O5, the
vendor-attested confidential pool; owner decision 2026-10-20). It exposes the
same ``factory`` / ``evict`` / ``resident_bytes`` / pin shape, so
:class:`~creek_mcp.bench.runner.Backend` drives the cold/warm, context,
concurrency and idle sweeps against a pool with no change to any sweep.

Three things differ from the Ollama client, because a pool is not a runtime
the harness operates:

- **Identity, not a weights digest.** There is no ``/api/tags`` to read a
  blob digest from. Every request *names* the identity it expects, a pool
  advertising a different one is refused **before a prompt is sent**, and an
  answer echoing a different identity or model is discarded rather than
  returned under the pinned name (ADR-0016 Decision 1 item 1, T1). The
  refusal reuses :class:`~creek_mcp.bench.ollama_client.DigestMismatchError`:
  it is the same failure — the thing serving is not the thing pinned.
- **Attestation gates the run.** The listing must report a verified
  attestation over the pinned measurement set (Decision 3 items 2 and 3).
  An unattested pool is refused with nothing sent.
- **Residency is the vendor's.** A client cannot evict a pool and cannot see
  its resident bytes, so :meth:`BenchPoolClient.evict` does nothing and
  :meth:`BenchPoolClient.resident_bytes` answers ``None``. That is the point
  of a pool: a cold trial measures a cold *vault* against warm weights.
  Reporting the harness host's own figures instead would be a false capacity
  fact.

``is_cloud`` on the built callable is ``False``, matching the production
``PoolProvider`` (#1894): no operator cloud key is involved and the pool is
not a readable vendor API. Where the requests actually went is recorded
honestly and separately, as ``run.endpoint_scope`` — ``remote`` for a vendor
pool, which the operator must ask for with ``--allow-remote-host``
(ADR-0016 Decision 6). ``intimate_eligible`` does not appear here at all: the
harness runs the synthetic corpus at :attr:`TierCeiling.OPEN` and acquires no
path that could route INTIMATE content (Decision 5).

Nothing a pool says reaches the report as free text. The measurement digest is
validated as hex, a 429's code is matched against a closed pair and then
dropped, and no body, header or URL is ever returned or logged.
"""

from __future__ import annotations

import json
import re
import threading
from typing import TYPE_CHECKING, Any, Final

import httpx

from creek_mcp.bench.metadata import UNKNOWN
from creek_mcp.bench.ollama_client import DigestMismatchError, PinnedModel
from creek_mcp.bench.outcome import (
    ContextOverflowError,
    PoolQuotaExhaustedError,
    PoolRateLimitedError,
    ProviderUnavailableError,
)
from creek_mcp.bench.pool_http import pool_http_client

if TYPE_CHECKING:
    from creek.models import PrivacyTier
    from creek_mcp.bench.protocols import LLMCallable, LLMFactory

POOL_MODELS_PATH: Final[str] = "/v1/models"
"""The pool's identity listing: which identities it serves, and its attestation."""

POOL_GENERATE_PATH: Final[str] = "/v1/generate"
"""The pool's one generation path."""

POOL_REQUEST_TIMEOUT: Final[float] = 12.0
"""Seconds a pool request may take, so a trial is shed when a reflection would.

The same ceiling production's pool boundary applies (#1894's
``POOL_REQUEST_TIMEOUT``). It is restated here rather than imported because
the bench measures the pool before that boundary is the path under test; the
live pool run (#1899) collapses the two.
"""

_TOO_MANY_REQUESTS: Final[int] = 429
_RATE_LIMITED: Final[str] = "rate_limited"
_INSUFFICIENT_QUOTA: Final[str] = "insufficient_quota"

_HEX_DIGEST: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")

_UNREACHABLE: Final[str] = "the benchmark's pool endpoint is unreachable"
_MALFORMED: Final[str] = "the pool's identity listing is not a usable shape"
_IDENTITY_MISMATCH: Final[str] = "the pool advertises an identity other than the pin"
_NOT_SERVED: Final[str] = "the pool does not serve the pinned model"
_NO_MEASUREMENT: Final[str] = "the pool reported no usable measurement-set digest"
_MEASUREMENT_MISMATCH: Final[str] = (
    "the pool's attested measurement set is not the pinned one"
)
_ECHO_MISMATCH: Final[str] = "the pool's answer echoes a different identity"
_UNATTESTED: Final[str] = "the pool's attestation did not verify"
_ADMISSION_REFUSED: Final[str] = "the pool refused admission for this trial"
_QUOTA_SPENT: Final[str] = "the pool key's cap is spent"
_OVERFLOW: Final[str] = "the prompt filled the context window and was truncated"


class PoolAttestationError(ValueError):
    """The pool did not prove it is running the measured, pinned image.

    A ``ValueError``, like every other pre-flight refusal in the harness, so
    the CLI reports it as a refusal (exit 2) rather than a run that failed.
    """


class _BenchPoolLLM:
    """One bounded completion callable against the pool.

    ``is_cloud`` is ``False`` for the reason the production ``PoolProvider``
    gives: no operator cloud key, no consent variable, not a readable vendor
    API. The report's ``endpoint_scope`` is what records where it dialled.
    """

    is_cloud: bool = False

    def __init__(self, client: BenchPoolClient, num_predict: int) -> None:
        """Bind *client* and this call's output ceiling."""
        self._client = client
        self._num_predict = num_predict

    def __call__(self, prompt: str) -> str:
        """Generate a completion for *prompt*."""
        return self._client.generate(prompt, num_predict=self._num_predict)


def _mapping(value: object) -> dict[str, Any]:
    """Return *value* as a mapping, or an empty one when it is not."""
    return value if isinstance(value, dict) else {}


class BenchPoolClient:
    """A minimal inference-pool client for capacity trials."""

    def __init__(
        self,
        base_url: str,
        model_tag: str,
        *,
        identity: str,
        num_ctx: int,
        num_predict: int,
        measurement_digest: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Configure the client; performs no I/O.

        Args:
            base_url: The pool endpoint, e.g. ``https://pool.example``.
            model_tag: The model the pool must serve and echo.
            identity: The pinned identity every request names.
            num_ctx: The input window the run is sized against; a pool does
                not take it as a request knob (ADR-0016 Decision 1 item 4
                admits no client-chosen sampling field), so it is used only to
                judge a reported prompt size as an overflow.
            num_predict: Run-wide ceiling on output tokens; a request's own
                ``max_tokens`` can only lower it.
            measurement_digest: The pinned measurement-set digest the pool's
                attestation must carry; ``None`` checks only the identity.
            transport: Injected transport (the fake pool supplies one).
        """
        self._base_url = base_url
        self._model_tag = model_tag
        self._identity = identity
        self._num_ctx = num_ctx
        self._num_predict = num_predict
        self._measurement_digest = measurement_digest
        self._transport = transport
        self._pinned: PinnedModel | None = None
        self._lock = threading.Lock()

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> httpx.Response:
        """Send one request and return its response, unread.

        Raises:
            ProviderUnavailableError: When the endpoint cannot be reached.
        """
        try:
            with pool_http_client(
                POOL_REQUEST_TIMEOUT, base_url=self._base_url, transport=self._transport
            ) as client:
                return client.request(method, path, json=body)
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(_UNREACHABLE) from exc

    @staticmethod
    def _decoded(response: httpx.Response) -> object:
        """Return the response's decoded JSON body, or ``None`` when unusable."""
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def verify_identity(self) -> PinnedModel:
        """Resolve and check the pool's identity once, sending no prompt.

        Returns:
            The pinned description: the attested measurement-set digest, and
            no weights details, which a pool does not publish.

        Raises:
            DigestMismatchError: When the listing is unusable, advertises
                another identity, or does not serve the pinned model.
            PoolAttestationError: When the attestation did not verify.
        """
        with self._lock:
            if self._pinned is None:
                self._pinned = self._resolve_pinned()
            return self._pinned

    def verify_pinned(self, expected: str) -> PinnedModel:
        """Refuse unless the pool attests exactly *expected* (``Backend.pin``).

        Args:
            expected: The run's pinned measurement-set digest.

        Returns:
            The pinned model description.

        Raises:
            DigestMismatchError: When the attested set is not *expected*.
        """
        pinned = self.verify_identity()
        if pinned.digest != expected:
            raise DigestMismatchError(_MEASUREMENT_MISMATCH)
        return pinned

    def _resolve_pinned(self) -> PinnedModel:
        """Read the listing and hold every pin it must satisfy."""
        response = self._request("GET", POOL_MODELS_PATH, None)
        response.raise_for_status()
        listing = _mapping(self._decoded(response))
        if not listing:
            raise DigestMismatchError(_MALFORMED)
        digest = self._attested_digest(listing)
        if listing.get("identity") != self._identity:
            raise DigestMismatchError(_IDENTITY_MISMATCH)
        if not self._serves_model(listing):
            raise DigestMismatchError(_NOT_SERVED)
        return PinnedModel(
            digest=digest,
            quantization=None,
            parameter_count=None,
            license_id=UNKNOWN,
        )

    def _attested_digest(self, listing: dict[str, Any]) -> str:
        """Return the attested measurement digest the listing proves.

        Raises:
            PoolAttestationError: When the attestation did not verify.
            DigestMismatchError: When the digest is missing, malformed, or
                not the pinned one.
        """
        attestation = _mapping(listing.get("attestation"))
        if attestation.get("verified") is not True:
            raise PoolAttestationError(_UNATTESTED)
        raw = str(attestation.get("measurement_digest", "")).removeprefix("sha256:")
        if not _HEX_DIGEST.fullmatch(raw):
            raise DigestMismatchError(_NO_MEASUREMENT)
        digest = f"sha256:{raw}"
        if self._measurement_digest is not None and digest != self._measurement_digest:
            raise DigestMismatchError(_MEASUREMENT_MISMATCH)
        return digest

    def _serves_model(self, listing: dict[str, Any]) -> bool:
        """Return whether the listing offers the pinned model by name."""
        models = listing.get("models")
        entries = models if isinstance(models, list) else []
        return any(
            isinstance(entry, dict) and entry.get("name") == self._model_tag
            for entry in entries
        )

    def generate(self, prompt: str, *, num_predict: int) -> str:
        """Return the pool's completion for *prompt*.

        Args:
            prompt: The fully-formatted prompt.
            num_predict: The caller's output ceiling, capped by the run's.

        Returns:
            The completion text; empty when the answer carries none.

        Raises:
            PoolRateLimitedError: On a 429 the pool did not call a quota.
            PoolQuotaExhaustedError: On a 429 ``insufficient_quota``.
            DigestMismatchError: When the answer echoes another identity.
            ContextOverflowError: When the prompt filled the window.
        """
        ceiling = min(num_predict, self._num_predict)
        payload: dict[str, Any] = {
            "model": self._model_tag,
            "identity": self._identity,
            "prompt": prompt,
            "stream": False,
            "max_tokens": ceiling,
        }
        response = self._request("POST", POOL_GENERATE_PATH, payload)
        self._refuse_admission(response)
        response.raise_for_status()
        answer = _mapping(self._decoded(response))
        self._refuse_foreign_echo(answer)
        self._refuse_truncation(answer, ceiling)
        text = answer.get("response")
        return text if isinstance(text, str) else ""

    def _refuse_admission(self, response: httpx.Response) -> None:
        """Raise the right refusal for a 429, reading only its closed code.

        The body's code is compared against the two ADR-0016 names and then
        dropped; nothing from it is returned, kept or logged.

        Raises:
            PoolQuotaExhaustedError: On ``insufficient_quota`` — retrying
                cannot fix a spent cap.
            PoolRateLimitedError: On ``rate_limited``, and on an unlabelled
                429, which is admission pressure until the pool says more.
        """
        if response.status_code != _TOO_MANY_REQUESTS:
            return
        code = _mapping(self._decoded(response)).get("code")
        if code == _INSUFFICIENT_QUOTA:
            raise PoolQuotaExhaustedError(_QUOTA_SPENT)
        raise PoolRateLimitedError(_ADMISSION_REFUSED)

    def _refuse_foreign_echo(self, answer: dict[str, Any]) -> None:
        """Discard an answer that names an identity or model we did not ask for.

        The echo is a consistency check against misrouting, never integrity
        evidence (ADR-0016 Decision 1 item 1): a pool that answers under
        another name has not answered this trial at all.

        Raises:
            DigestMismatchError: When either echoed name differs from the pin.
        """
        if answer.get("identity") != self._identity:
            raise DigestMismatchError(_ECHO_MISMATCH)
        if answer.get("model") != self._model_tag:
            raise DigestMismatchError(_ECHO_MISMATCH)

    def _refuse_truncation(self, answer: dict[str, Any], ceiling: int) -> None:
        """Raise when the reported prompt left no room for the output.

        The pool reports what it evaluated in ``usage.prompt_tokens``. A
        prompt at or past ``num_ctx - ceiling`` was truncated, so the trial
        timed a shorter prompt than it records and must not count as ``ok``.
        A pool reporting no usage cannot be judged, and is not flagged.

        Raises:
            ContextOverflowError: When the prompt filled the window.
        """
        evaluated = _mapping(answer.get("usage")).get("prompt_tokens")
        if isinstance(evaluated, int) and evaluated >= self._num_ctx - ceiling:
            raise ContextOverflowError(_OVERFLOW)

    def factory(self) -> LLMFactory:
        """Return a reflect-shaped factory that verifies the identity first.

        The runner builds once in its preflight, before the corpus is written
        and before any prompt exists, so a pool serving the wrong identity is
        refused with zero generation calls.
        """

        def _build(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
            del tier
            self.verify_identity()
            return _BenchPoolLLM(self, max_tokens)

        return _build

    def evict(self) -> None:
        """Do nothing: a pool's residency is the vendor's, not the client's.

        A pool holds the warm weights a 1 GB vault Machine cannot, so there is
        nothing to unload and no request that would unload it. A "cold" pool
        trial therefore measures a cold vault process — fresh grounding
        session, no cached prefix — against warm weights, which is the
        latency a managed vault actually serves.
        """

    def resident_bytes(self) -> int | None:
        """Return ``None``: a pool's resident size is not observable here.

        There is no ``/api/ps`` on a pool, and the harness host's own figures
        describe the client, not the weights. ``None`` says "unknown", which
        is the truth.
        """
        return None
