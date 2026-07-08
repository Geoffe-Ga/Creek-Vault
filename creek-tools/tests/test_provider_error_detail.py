"""The wrapped provider error must name the cause, without leaking secrets (#745).

A cloud provider that fails surfaces a plain ``RuntimeError`` so the
orchestrator's retry loop stays provider-agnostic. Before #745 that message
carried only the exception *type* (``BadRequestError``), which hid the real
cause — a budget/credit 400 looked identical to a malformed-request 400, so a
whole vault's worth of "over budget" failures read as an opaque wall of
``BadRequestError``.

#745 surfaces the API's own error *message* for **4xx client errors** (the
API-side description of what is wrong with the request or account — billing,
auth, model id, params), while keeping **5xx / unknown** errors status-only,
because server-error bodies are opaque and the providers deliberately redact
anything that could echo request state. In no case may the API key or the
prompt / fragment content appear.
"""

from __future__ import annotations

from creek.classify.llm.providers import (
    _chain_is_safe,
    _error_detail,
    _error_status,
)


class _FakeError(Exception):
    """A stand-in SDK error exposing the attributes the helpers read."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
    ) -> None:
        """Store the message and whichever status attribute the SDK would set."""
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


_CREDIT_MESSAGE = (
    "Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits."
)


def test_4xx_surfaces_the_api_message() -> None:
    """A 400's API message (the real cause) is surfaced after the status."""
    detail = _error_detail(_FakeError(_CREDIT_MESSAGE, status_code=400))
    assert "HTTP 400" in detail
    assert "credit balance is too low" in detail


def test_gemini_style_code_attribute_is_read() -> None:
    """Gemini exposes the status as ``code`` rather than ``status_code``."""
    assert _error_status(_FakeError("nope", code=404)) == 404
    detail = _error_detail(_FakeError("model not found", code=404))
    assert "HTTP 404" in detail
    assert "model not found" in detail


def test_5xx_stays_status_only() -> None:
    """A server error is opaque — status only, message withheld."""
    detail = _error_detail(
        _FakeError("internal detail that may echo state", status_code=503)
    )
    assert detail == " (HTTP 503)"
    assert "internal detail" not in detail


def test_unknown_status_yields_no_detail() -> None:
    """With no usable status the helper falls closed to the empty (type-only) form."""
    assert _error_detail(_FakeError("secret request payload")) == ""


def test_long_message_is_capped() -> None:
    """An over-long 4xx message is truncated with an ellipsis."""
    detail = _error_detail(_FakeError("x" * 1000, status_code=400))
    assert detail.endswith("…")
    assert len(detail) < 600


def test_non_string_message_falls_back_to_status_only() -> None:
    """A 4xx whose ``message`` is not a usable string degrades to status only."""
    err = _FakeError("ignored", status_code=400)
    err.message = None  # type: ignore[assignment]  # simulate an SDK with no message
    assert _error_detail(err) == " (HTTP 400)"


def test_chain_is_safe_only_for_4xx() -> None:
    """The exception chain may be kept only for 4xx (whose body we surface)."""
    assert _chain_is_safe(_FakeError("c", status_code=400))
    assert _chain_is_safe(_FakeError("c", code=404))
    assert _chain_is_safe(_FakeError("c", status_code=499))
    # 5xx and unknown withhold the body, so the chain must be suppressed.
    assert not _chain_is_safe(_FakeError("c", status_code=500))
    assert not _chain_is_safe(_FakeError("c", status_code=503))
    assert not _chain_is_safe(_FakeError("c"))
