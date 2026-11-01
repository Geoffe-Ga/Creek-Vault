"""The attesting pool client for sidecar mode (ADR-0016 O5, Privatemode, #1896).

One generation, in this order, and nothing is sent unless every step passes:

1. **Closed request shape** (Decision 1 item 4). The request is built as the
   vendor-neutral shape — pinned identity, prompt, optional system text,
   ``max_tokens``, the declared tier and ``deadline_ms`` — and validated by
   :func:`validate_closed_request`, which refuses ``intimate`` and unknown
   tiers, a client-chosen model, any sampling, cache or adapter field, any
   unknown field, a prompt over the token cap and a body over 1 MiB, each with
   a content-free 4xx :class:`PoolRequestRefusedError`.
2. **Admission** (Decision 1 item 8): the vault's own limiter, refused at once.
3. **Attestation** (Decision 3.3, T18): the loopback sidecar's report must
   match the Machine's pin (:mod:`creek.classify.llm.pool_attestation`).
4. **Translation and dial**: the closed shape becomes an OpenAI-compatible
   chat completion carrying the pinned model, the messages and ``max_tokens``
   only — **no cache-control field**, no sampling field, no stream flag — sent
   to the loopback sidecar through
   :func:`~creek.classify.llm.pool_boundary.pool_chat_completion`. The random
   request id rides in a header as the only correlation key; ``deadline_ms``
   is enforced as the dial's own budget, because the vendor API has no field
   for it.
5. **Outcome mapping**: 401/403 is :class:`PoolCredentialRejectedError`; a
   429 whose error code is ``insufficient_quota`` is
   :class:`PoolQuotaExhaustedError` — **non-transient**, so the route answers
   ``temporarily_unavailable`` because waiting cannot clear a spent monthly
   cap — and any other 429 is :class:`PoolVendorRateLimitedError`. Only the
   error *code* of a 429 is read; no upstream body is relayed or logged.

The vendor key is never in this process: the sidecar holds it (the root
bootstrap gives the key file to the sidecar's own user), so code that sees a
writer's ``RequestKey`` cannot read it. Every operational record this module
writes is content-free (:func:`record_pool_outcome`, Decision 1 item 12).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from creek.classify.llm.completion import Completion
from creek.classify.llm.pool_admission import PoolAdmissionRefusedError, vault_admission
from creek.classify.llm.pool_attestation import PoolAttestationError, attested_report
from creek.classify.llm.pool_boundary import (
    POOL_MAX_DEADLINE_MS,
    POOL_MAX_REQUEST_BYTES,
    POOL_REQUEST_ID_HEADER,
    POOL_TIERS,
    PoolBoundaryError,
    pool_chat_completion,
    pool_prompt_within_cap,
    require_pool_request,
)
from creek.classify.llm.pool_meter import TELEMETRY_LOGGER
from creek.vault_alerts import owe_vault_alert

if TYPE_CHECKING:
    from collections.abc import Mapping

    from creek.classify.llm.pool_boundary import PoolResponse

POOL_MAX_OUTPUT_TOKENS: Final[int] = 1024
"""Largest ``max_tokens`` the closed shape admits on any route."""

CLOSED_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    {"identity", "prompt", "system", "max_tokens", "tier", "deadline_ms"}
)
"""Every field the closed request shape may carry (Decision 1 item 4)."""

SAMPLING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "n",
        "stop",
    }
)
"""Sampling knobs: pinned inside the served artefact set, never chosen here."""

CACHE_FIELDS: Final[frozenset[str]] = frozenset(
    {"cache_control", "cache_salt", "prompt_cache_key", "cache"}
)
"""Cache knobs: the vault sends no cache-control field (Decision 1 item 6)."""

ADAPTER_FIELDS: Final[frozenset[str]] = frozenset(
    {"adapter", "adapters", "lora", "lora_request"}
)
"""Adapter knobs: no adapter is ever loaded on the vault's behalf."""

MODEL_FIELDS: Final[frozenset[str]] = frozenset({"model"})
"""A client-chosen model: the identity is pinned, never requested."""

_HTTP_OK: Final[int] = 200
_HTTP_BAD_REQUEST: Final[int] = 400
_HTTP_PAYLOAD_TOO_LARGE: Final[int] = 413
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_CREDENTIAL_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
_INSUFFICIENT_QUOTA: Final[str] = "insufficient_quota"
_MS_PER_SECOND: Final[float] = 1000.0

_LOGGER: Final[logging.Logger] = logging.getLogger(TELEMETRY_LOGGER)

OUTCOME_ALERTS: Final[dict[str, str]] = {
    "attestation_refused": "pool_attestation_failed",
    "credential_rejected": "pool_credential_rejected",
    "admission_refused": "pool_admission_refused",
    "vendor_rate_limited": "pool_vendor_rate_limited",
    "quota_exhausted": "pool_quota_exhausted",
}
"""Which outcomes are alert-worthy, by the fleet alert kind they name.

Every value is a member of :data:`creek.vault_alerts.VAULT_ALERT_KINDS` and so
of :class:`creek_mcp.provisioning.budget.AlertKind` (asserted by the tests).
The record carries the name, and :func:`record_pool_outcome` also owes it in
the vault's outbox, from which the control plane's fleet pass pulls it to the
operator (#2049).
"""


class PoolRequestRefusedError(PoolBoundaryError):
    """The request is outside the closed shape; nothing was sent.

    Attributes:
        reason: A closed, content-free label naming which rule refused it.
        status: The 4xx this refusal corresponds to.
    """

    def __init__(self, reason: str, *, status: int = _HTTP_BAD_REQUEST) -> None:
        """Store the closed reason and status behind a fixed message.

        Args:
            reason: Which rule refused the request.
            status: The 4xx status.
        """
        super().__init__(f"the pool request is outside the closed shape: {reason}")
        self.reason = reason
        self.status = status


class PoolCredentialRejectedError(PoolBoundaryError):
    """The vendor refused the sidecar's key (401/403); the body is not read."""


class PoolQuotaExhaustedError(PoolBoundaryError):
    """The key's monthly cap is spent (``insufficient_quota``); non-transient.

    Deliberately **not** a rate-limit error: a rate limit clears by waiting,
    a spent monthly euro cap does not, so the route answers
    ``temporarily_unavailable`` rather than ``rate_limited``.
    """


class PoolVendorRateLimitedError(PoolBoundaryError):
    """The vendor rate-limited the request (a 429 other than quota)."""


class PoolAnswerUnusableError(PoolBoundaryError):
    """The answer was malformed, refused, or named a different model."""


@dataclass(frozen=True, slots=True)
class ClosedPoolRequest:
    """One validated request in the closed, vendor-neutral shape.

    Attributes:
        identity: The pinned model the vault expects.
        prompt: The prompt text.
        system: Optional system text.
        max_tokens: Optional output ceiling.
        tier: ``open`` or ``personal``.
        deadline_ms: How long the vault will wait, at most 12,000.
    """

    identity: str
    prompt: str
    system: str | None
    max_tokens: int | None
    tier: str
    deadline_ms: int


def _forbidden_field_reason(keys: frozenset[str]) -> str | None:
    """Return the closed reason a field outside the shape earns, or ``None``.

    Args:
        keys: The request's field names.

    Returns:
        The most specific reason that applies, checked in a fixed order.
    """
    named = (
        (MODEL_FIELDS, "client_model"),
        (SAMPLING_FIELDS, "sampling_field"),
        (CACHE_FIELDS, "cache_field"),
        (ADAPTER_FIELDS, "adapter_field"),
    )
    for fields, reason in named:
        if keys & fields:
            return reason
    return "unknown_field" if keys - CLOSED_REQUEST_FIELDS else None


def _positive_int(value: object, ceiling: int) -> bool:
    """Return whether *value* is an ``int`` (not a bool) in ``1..ceiling``."""
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 < value <= ceiling
    )


def _optional_text(body: Mapping[str, object], key: str) -> str | None:
    """Return the optional string at *key*, refusing a non-string value."""
    value = body.get(key)
    if value is None or isinstance(value, str):
        return value
    raise PoolRequestRefusedError("malformed_field")


def _require_tier(body: Mapping[str, object]) -> str:
    """Return the declared tier, refusing ``intimate`` and anything unknown."""
    tier = body.get("tier")
    if not isinstance(tier, str) or tier not in POOL_TIERS:
        raise PoolRequestRefusedError(
            "intimate_tier" if tier == "intimate" else "unknown_tier"
        )
    return tier


def _require_prompt(body: Mapping[str, object]) -> str:
    """Return the prompt, refusing an empty one or one over the token cap."""
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise PoolRequestRefusedError("malformed_field")
    if not pool_prompt_within_cap(prompt):
        raise PoolRequestRefusedError("prompt_too_long")
    return prompt


def _require_limits(body: Mapping[str, object]) -> tuple[int, int | None]:
    """Return ``(deadline_ms, max_tokens)``, refusing either out of range."""
    deadline_ms = body.get("deadline_ms")
    if not isinstance(deadline_ms, int) or not _positive_int(
        deadline_ms, POOL_MAX_DEADLINE_MS
    ):
        raise PoolRequestRefusedError("deadline_out_of_range")
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        return deadline_ms, None
    if not isinstance(max_tokens, int) or not _positive_int(
        max_tokens, POOL_MAX_OUTPUT_TOKENS
    ):
        raise PoolRequestRefusedError("max_tokens_out_of_range")
    return deadline_ms, max_tokens


def validate_closed_request(
    body: Mapping[str, object], *, pinned_identity: str
) -> ClosedPoolRequest:
    """Return *body* as a validated closed request, refusing anything else.

    The checks run in a fixed order — forbidden fields, size, tier, identity,
    prompt, limits — so a request breaking two rules is always named the same
    way.

    Args:
        body: The request in the closed shape.
        pinned_identity: The model the Machine pins.

    Returns:
        The validated request.

    Raises:
        PoolRequestRefusedError: With a closed reason and a 4xx status.
    """
    reason = _forbidden_field_reason(frozenset(body))
    if reason is not None:
        raise PoolRequestRefusedError(reason)
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > POOL_MAX_REQUEST_BYTES:
        raise PoolRequestRefusedError("body_too_large", status=_HTTP_PAYLOAD_TOO_LARGE)
    tier = _require_tier(body)
    if body.get("identity") != pinned_identity:
        raise PoolRequestRefusedError("identity_mismatch")
    prompt = _require_prompt(body)
    deadline_ms, max_tokens = _require_limits(body)
    return ClosedPoolRequest(
        identity=pinned_identity,
        prompt=prompt,
        system=_optional_text(body, "system"),
        max_tokens=max_tokens,
        tier=tier,
        deadline_ms=deadline_ms,
    )


def vendor_chat_body(request: ClosedPoolRequest) -> dict[str, object]:
    """Translate a closed request onto the vendor's OpenAI-compatible API.

    Exactly the pinned model, the messages and the optional ``max_tokens``:
    no cache-control field, no sampling field, no stream flag and no tier —
    the tier is the closed shape's refusal input, and the vendor has no field
    for it.

    Args:
        request: The validated closed request.

    Returns:
        The chat-completion body.
    """
    messages: list[dict[str, str]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append({"role": "user", "content": request.prompt})
    body: dict[str, object] = {"model": request.identity, "messages": messages}
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    return body


def _error_code(payload: object) -> str | None:
    """Return a 429's error code or type, the only field of it ever read."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str):
            return value
    return None


def _refusal_for(response: PoolResponse) -> PoolBoundaryError:
    """Return the content-free refusal for a non-200 sidecar answer."""
    if response.status in _CREDENTIAL_STATUSES:
        return PoolCredentialRejectedError("the vendor refused the pool key")
    if response.status == _HTTP_TOO_MANY_REQUESTS:
        if _error_code(response.payload) == _INSUFFICIENT_QUOTA:
            return PoolQuotaExhaustedError("the pool key's monthly cap is spent")
        return PoolVendorRateLimitedError("the vendor is rate limiting the pool")
    return PoolAnswerUnusableError(
        f"the attesting sidecar refused the request (HTTP {response.status})"
    )


def _usage(answer: Mapping[str, object]) -> dict[str, int] | None:
    """Return the integer token counts the vendor reported, or ``None``."""
    usage = answer.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = {
        name: value
        for name, key in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
        )
        if isinstance(value := usage.get(key), int) and not isinstance(value, bool)
    }
    return counts or None


def completion_from_answer(payload: object, model: str) -> Completion:
    """Return the :class:`Completion` an OpenAI-compatible answer carries.

    Args:
        payload: The decoded answer.
        model: The pinned model the request named.

    Returns:
        The completion.

    Raises:
        PoolAnswerUnusableError: When the answer is malformed or names another
            model — discarded, never relabelled.
    """
    unusable = PoolAnswerUnusableError("the attesting sidecar answered unusably")
    if not isinstance(payload, dict) or payload.get("model") != model:
        raise unusable
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise unusable
    message = choices[0].get("message")
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise unusable
    return Completion(text=text, usage=_usage(payload))


_OUTCOMES: Final[tuple[tuple[type[PoolBoundaryError], str], ...]] = (
    (PoolRequestRefusedError, "request_refused"),
    (PoolAdmissionRefusedError, "admission_refused"),
    (PoolAttestationError, "attestation_refused"),
    (PoolCredentialRejectedError, "credential_rejected"),
    (PoolQuotaExhaustedError, "quota_exhausted"),
    (PoolVendorRateLimitedError, "vendor_rate_limited"),
    (PoolAnswerUnusableError, "answer_unusable"),
)


def outcome_of(error: PoolBoundaryError) -> str:
    """Return the closed outcome label for *error*.

    Args:
        error: A refusal from this module or the boundary.

    Returns:
        The label; ``unreachable`` for any other boundary failure.
    """
    for kind, label in _OUTCOMES:
        if isinstance(error, kind):
            return label
    return "unreachable"


def _detail_of(error: PoolBoundaryError) -> str | None:
    """Return the closed sub-label a refusal carries, or ``None``."""
    if isinstance(error, PoolAttestationError):
        return error.verdict.value
    if isinstance(error, PoolAdmissionRefusedError):
        return error.limit
    if isinstance(error, PoolRequestRefusedError):
        return error.reason
    return None


def record_pool_outcome(
    *,
    outcome: str,
    request_id: str,
    started: float,
    detail: str | None = None,
    usage: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Write and return one content-free pool record (Decision 1 item 12).

    Only a closed outcome label, the random request id, the latency, token
    counts and a closed sub-label — never a prompt, an answer, a key, a
    destination or an upstream body.

    Args:
        outcome: The closed outcome label.
        request_id: The random correlation id.
        started: The :func:`time.monotonic` reading at the start.
        detail: A closed sub-label (verdict, limit or reason), if any.
        usage: Reported token counts, if any.

    Returns:
        The record as written.
    """
    record: dict[str, object] = {
        "event": "pool_call",
        "outcome": outcome,
        "request_id": request_id,
        "latency_ms": int((time.monotonic() - started) * _MS_PER_SECOND),
    }
    if detail is not None:
        record["detail"] = detail
    alert = OUTCOME_ALERTS.get(outcome)
    if alert is not None:
        record["alert"] = alert
    if usage:
        record.update(
            {key: value for key, value in usage.items() if key.endswith("_tokens")}
        )
    _LOGGER.info(json.dumps(record, sort_keys=True))
    if alert is not None:
        # Owed to the operator until the control plane acknowledges it (#2049).
        owe_vault_alert(alert)
    return record


def _send(request: ClosedPoolRequest, *, budget: float, request_id: str) -> Completion:
    """Run admission, attestation and the dial for one validated request."""
    with vault_admission().admit():
        attested_report(budget=budget)
        response = pool_chat_completion(
            vendor_chat_body(request),
            budget=min(budget, request.deadline_ms / _MS_PER_SECOND),
            headers={POOL_REQUEST_ID_HEADER: request_id},
        )
    if response.status != _HTTP_OK:
        raise _refusal_for(response)
    return completion_from_answer(response.payload, request.identity)


def call_attested_pool(
    *,
    model: str,
    prompt: str,
    timeout: float,
    max_tokens: int | None = None,
    system: str | None = None,
) -> Completion:
    """Send one generation through the attesting sidecar, or refuse.

    Args:
        model: The pinned model.
        prompt: The fully formatted prompt.
        timeout: Seconds left for this call.
        max_tokens: Optional output ceiling.
        system: Optional system text.

    Returns:
        The vendor's completion.

    Raises:
        PoolBoundaryError: Every refusal, each recorded content-free first.
    """
    scope = require_pool_request()
    started = time.monotonic()
    body: dict[str, object] = {
        "identity": model,
        "prompt": prompt,
        "tier": scope.tier,
        "deadline_ms": scope.deadline_ms(timeout),
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if system:
        body["system"] = system
    try:
        request = validate_closed_request(body, pinned_identity=model)
        completion = _send(request, budget=timeout, request_id=scope.request_id)
    except PoolBoundaryError as exc:
        record_pool_outcome(
            outcome=outcome_of(exc),
            request_id=scope.request_id,
            started=started,
            detail=_detail_of(exc),
        )
        raise
    record_pool_outcome(
        outcome="answered",
        request_id=scope.request_id,
        started=started,
        usage=completion.usage,
    )
    return completion
