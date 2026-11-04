"""A writer's refused key is the writer's to replace, not a wait (contract 0.28).

On a managed vault a reflection or a letter can be answered on the writer's own
provider key (ADR-0015). When that vendor answers the key ``401``/``403``, the
provider already raises the typed
:class:`~creek.classify.llm.providers.RequestKeyRejectedError` (#1877). Until
0.28 both model-backed routes then caught it as a plain ``RuntimeError`` and
served ``503 temporarily_unavailable``, so the consumer told the writer to give
it a moment and try again — advice that can never clear a rejected key.

These tests pin the three sides of the fix, on both routes, through the real
vendor SDK with only its socket replaced:

- a caller declaring 0.28 is served ``424 request_key_rejected``, terminal, and
  the vendor was dialled exactly once;
- a caller declaring 0.27 is served exactly what 0.27 published for any other
  provider failure — ``503 temporarily_unavailable`` — byte-identical but for
  the correlation id;
- the operator's own key (no request key) refused by its vendor is still the
  ``503`` it always was: only the writer's key is the writer's to fix.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek.classify.llm.consent import CLOUD_CONSENT_ENV, MANAGED_VAULT_ENV
from creek_mcp.api.models import (
    CONTRACT_MINOR,
    ERROR_MESSAGES,
    ERROR_STATUS,
    RETRY_POLICY,
    SUPPORTED_CONTRACT_MINORS,
    ErrorCode,
    RetryDisposition,
)
from creek_mcp.httpapi.errors import RETRY_AFTER_HEADER
from creek_mcp.httpapi.reflect import REFUSAL_LOGGER_NAME
from tests.test_request_scoped_byok import (
    ENTRY,
    HTTP_FORBIDDEN,
    HTTP_SERVICE_UNAVAILABLE,
    HTTP_UNAUTHORIZED,
    OPERATOR_KEY,
    QUOTE,
    _assert_canary_absent,
    _client,
    _key_headers,
    _Vendor,
    _write_config,
    every_log,
    managed,
)
from tests.v1_api_support import LETTERS_TEMPLATE, REFLECTIONS_PATH, headers, seed_vault

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

__all__ = ["every_log", "managed"]
"""The two imported fixtures, re-exported so pytest and the linters see a use."""

HTTP_FAILED_DEPENDENCY: Final[int] = 424
"""RFC 4918 §11.4: the request failed because a request it depended on did."""

HTTP_BAD_GATEWAY: Final[int] = 502
"""A vendor outage: the provider failure every earlier minor already knew."""

PRIOR_MINOR: Final[str] = "0.27"
"""The minor immediately below the one that publishes ``request_key_rejected``."""

_LETTER_PATH: Final[str] = LETTERS_TEMPLATE.format(external_id="letter-key-rejected")

_LETTER_BODY: Final[dict[str, Any]] = {
    "content": ENTRY,
    "tier": "personal",
    "note": {"anchor_text": QUOTE, "kind": "pattern", "note": "It costs you."},
}

_ROUTES: Final[list[Any]] = [
    pytest.param(REFLECTIONS_PATH, {"content": ENTRY}, id="reflections"),
    pytest.param(_LETTER_PATH, _LETTER_BODY, id="letters"),
]
"""Both model-backed synchronous routes that accept a writer's request key."""

_REJECTED_CODE: Final[str] = "request_key_rejected"
_TRANSIENT_CODE: Final[str] = "temporarily_unavailable"


def _post(
    vault: Path, path: str, body: dict[str, Any], sent: dict[str, str]
) -> httpx.Response:
    """POST *body* to *path* over a fresh app serving *vault*."""
    with _client(vault) as http:
        return http.post(path, json=body, headers=sent)


def _without_correlation(response: httpx.Response) -> dict[str, Any]:
    """Return the envelope minus the one field that must differ per request."""
    body: dict[str, Any] = response.json()
    body.pop("request_id")
    return body


# --------------------------------------------------------------------------- #
# The published code
# --------------------------------------------------------------------------- #


def test_the_code_is_published_as_a_terminal_424_with_a_constant_message() -> None:
    """424, terminal, and one message about the key and nothing else."""
    code = ErrorCode(_REJECTED_CODE)

    assert ERROR_STATUS[code] == HTTP_FAILED_DEPENDENCY
    assert RETRY_POLICY[code] is RetryDisposition.TERMINAL
    assert (
        ERROR_MESSAGES[code]
        == "the writer's own provider key was refused by its vendor"
    )


def test_the_prior_minor_stays_in_the_served_window() -> None:
    """0.27 callers are served, not refused ``incompatible_version``."""
    assert CONTRACT_MINOR == "0.28"
    assert PRIOR_MINOR in SUPPORTED_CONTRACT_MINORS


# --------------------------------------------------------------------------- #
# A 0.28 caller is told to replace the key
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("path", "body"), _ROUTES)
@pytest.mark.parametrize("status", [HTTP_UNAUTHORIZED, HTTP_FORBIDDEN])
def test_a_refused_writers_key_is_request_key_rejected(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
    path: str,
    body: dict[str, Any],
    status: int,
) -> None:
    """``424 request_key_rejected``, one dial, and the key never echoed."""
    vendor = _Vendor(monkeypatch, status=status)

    response = _post(managed, path, body, _key_headers(ceiling="personal"))

    assert response.status_code == HTTP_FAILED_DEPENDENCY, response.text
    assert _without_correlation(response) == {
        "code": _REJECTED_CODE,
        "message": ERROR_MESSAGES[ErrorCode(_REJECTED_CODE)],
    }
    assert RETRY_AFTER_HEADER not in response.headers
    assert len(vendor.requests) == 1
    _assert_canary_absent(tmp_path, every_log, response)


def test_the_refusal_log_names_the_cause_and_the_provider_only(
    managed: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The operator's line says which code, why, and whose key — never the key."""
    caplog.set_level(logging.WARNING, logger=REFUSAL_LOGGER_NAME)
    _Vendor(monkeypatch, status=HTTP_UNAUTHORIZED)

    _post(managed, REFLECTIONS_PATH, {"content": ENTRY}, _key_headers())

    (line,) = (r for r in caplog.records if r.name == REFUSAL_LOGGER_NAME)
    message = line.getMessage()
    assert f"code={_REJECTED_CODE}" in message
    assert "cause=reflection request key rejected" in message
    assert "request_key=anthropic" in message


# --------------------------------------------------------------------------- #
# A 0.27 caller is served exactly what 0.27 published
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("path", "body"), _ROUTES)
def test_a_caller_below_the_bump_is_served_its_old_refusal_byte_for_byte(
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, Any],
) -> None:
    """``503 temporarily_unavailable``, identical to any other provider failure.

    The comparison is against a vendor outage on the same route at the same
    minor rather than against a transcribed body, so "byte-identical" means
    what the 0.27 consumer actually received, not what a test author recalled.
    """
    sent = _key_headers(ceiling="personal", minor=PRIOR_MINOR)
    with monkeypatch.context() as patch:
        _Vendor(patch, status=HTTP_UNAUTHORIZED)
        rejected = _post(managed, path, body, sent)
    with monkeypatch.context() as patch:
        _Vendor(patch, status=HTTP_BAD_GATEWAY)
        outage = _post(managed, path, body, sent)

    assert rejected.status_code == HTTP_SERVICE_UNAVAILABLE, rejected.text
    assert rejected.status_code == outage.status_code
    assert _without_correlation(rejected) == _without_correlation(outage)
    assert _without_correlation(rejected)["code"] == _TRANSIENT_CODE
    assert sorted(rejected.headers.keys()) == sorted(outage.headers.keys())
    assert len(rejected.content) == len(outage.content)


# --------------------------------------------------------------------------- #
# The operator's own key is not the writer's to fix
# --------------------------------------------------------------------------- #


@pytest.fixture
def self_hosted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A self-hosted vault whose ``generation`` stage is the operator's cloud key.

    Not managed and not in container mode, so the model pin is the provider's
    own availability and the operator's consented key is what answers.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv(MANAGED_VAULT_ENV, raising=False)
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", OPERATOR_KEY)

    def _cloud(data: dict[str, Any]) -> None:
        data["llm"]["generation"] = {**data["llm"]["default"], "provider": "anthropic"}

    return _write_config(seed_vault(tmp_path / "vault"), _cloud)


@pytest.mark.parametrize(("path", "body"), _ROUTES)
def test_the_operators_refused_key_is_still_temporarily_unavailable(
    self_hosted: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, Any],
) -> None:
    """No request key: a vendor ``401`` stays the ``503`` it always was."""
    vendor = _Vendor(monkeypatch, status=HTTP_UNAUTHORIZED)

    response = _post(self_hosted, path, body, headers(ceiling="personal"))

    assert vendor.requests != [], "the operator's key was never dialled"
    assert response.status_code == HTTP_SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == _TRANSIENT_CODE


# --------------------------------------------------------------------------- #
# The downgrade is one rule, shared by both routes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("serve_rate_limited", "serve_request_key_rejected", "expected"),
    [
        (True, True, _REJECTED_CODE),
        (True, False, _TRANSIENT_CODE),
        (False, False, _TRANSIENT_CODE),
    ],
    ids=["at-0.28", "at-0.27", "below-0.19"],
)
def test_served_error_code_downgrades_on_its_own_since_minor(
    *, serve_rate_limited: bool, serve_request_key_rejected: bool, expected: str
) -> None:
    """Only the key-rejected gate decides; the rate-limit gate does not leak in."""
    from creek_mcp.httpapi.generation import served_error_code

    served = served_error_code(
        ErrorCode(_REJECTED_CODE),
        serve_rate_limited=serve_rate_limited,
        serve_request_key_rejected=serve_request_key_rejected,
    )

    assert served is ErrorCode(expected)


def test_the_since_minor_is_0_28_and_the_rate_limit_downgrade_is_untouched() -> None:
    """The new gate is its own literal, and ``rate_limited`` keeps its old one."""
    from creek_mcp.api.models import REQUEST_KEY_REJECTED_SINCE_MINOR
    from creek_mcp.httpapi.generation import served_error_code

    assert REQUEST_KEY_REJECTED_SINCE_MINOR == "0.28"
    assert (
        served_error_code(
            ErrorCode.RATE_LIMITED,
            serve_rate_limited=False,
            serve_request_key_rejected=True,
        )
        is ErrorCode.TEMPORARILY_UNAVAILABLE
    )
