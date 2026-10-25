"""A writer's request-scoped key powers a managed vault, and goes nowhere else (#1873).

ADR-0015: on a managed vault, a ``/v1`` request carrying ``X-Creek-Provider``
and ``X-Creek-Provider-Key`` is answered by the writer's own Anthropic or
OpenAI key for OPEN and PERSONAL work. These tests pin the four things that
must stay true together:

1. **It works.** A managed, pinned (container-mode) vault with no local model
   reflects, writes letters and classifies with the writer's key.
2. **The tier gate is the router's, unchanged.** INTIMATE with a key is forced
   onto the local route or refused; the writer's provider is never built for
   it.
3. **The refusals that stood still stand.** No key, a self-connected vault, or
   the operator's own configured cloud key: the pre-ADR-0015 refusal, byte for
   byte.
4. **The key goes nowhere.** A canary key is swept from every place a request
   can leave a trace — the response, the logs, the vault and its audit trail,
   the job record — on the success path and on a vendor error that echoes it.
"""

from __future__ import annotations

import json
import logging
import pickle
from functools import partial
from typing import TYPE_CHECKING, Any, Final

import anthropic
import httpx
import openai
import pytest
import yaml

from creek.classify.llm.consent import (
    CLOUD_CONSENT_ENV,
    MANAGED_VAULT_ENV,
    has_cloud_consent,
    is_managed_vault,
)
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.providers import AnthropicProvider, OpenAIProvider
from creek.classify.llm.request_key import (
    MAX_REQUEST_KEY_LENGTH,
    REQUEST_KEY_PROVIDERS,
    InvalidRequestKeyError,
    RequestKey,
    parse_request_key,
)
from creek.classify.llm.router import IntimateRoutingError
from creek.config import CreekConfig, LLMConfig
from creek.models import PrivacyTier
from creek_mcp.api.models import RequestKeyProvider
from creek_mcp.api.openapi import build_openapi
from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER, ROUTES
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from creek_mcp.model_pin import request_key_admitted
from tests.test_container_model_probe import _MODEL, _pin, _served
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

    from starlette.testclient import TestClient

CANARY_KEY: Final[str] = "sk-ant-api03-CANARYrequestKEY1873zzQQ-do-not-log"
"""The writer's key. Distinctive, so any substring sweep is unambiguous."""

CANARY_TAIL: Final[str] = CANARY_KEY[-8:]
"""The suffix a vendor's 401 message quotes (``...g-do-not-log``-style)."""

OPENAI_CANARY: Final[str] = "sk-proj-CANARYopenaiKEY1873zzRR-never-stored"
"""The writer's OpenAI key, swept the same way."""

OPERATOR_KEY: Final[str] = "sk-ant-api03-OPERATORkeyMUSTneverCARRYjournal"
"""The operator's own key, present in the environment and never used."""

ENTRY: Final[str] = "I keep saying yes to things I do not want, and I feel it later."
"""A benign entry the care guard does not escalate."""

QUOTE: Final[str] = "saying yes to things I do not want"
"""A verbatim span of :data:`ENTRY`, so the vendor's note survives validation."""

_CONFIG_SUBPATH: Final[tuple[str, ...]] = ("00-Creek-Meta", "creek_config.yaml")
_REFUSAL: Final[str] = "LLM provider unavailable for reflection"
HTTP_OK: Final[int] = 200
HTTP_ACCEPTED: Final[int] = 202
HTTP_UNPROCESSABLE: Final[int] = 422
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503
HTTP_UNAUTHORIZED: Final[int] = 401


# --------------------------------------------------------------------------- #
# Fixtures and fakes
# --------------------------------------------------------------------------- #


def _key_headers(
    provider: str = "anthropic", key: str = CANARY_KEY, **kwargs: Any
) -> dict[str, str]:
    """Return ``/v1`` headers carrying a request-scoped key."""
    return {
        **headers(**kwargs),
        PROVIDER_HEADER: provider,
        PROVIDER_KEY_HEADER: key,
    }


def _write_config(vault: Path, mutate: Any = None) -> Path:
    """Write a vault config whose local model is the pinned tag."""
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["default"].update(model=_MODEL)
    if mutate is not None:
        mutate(data)
    path = vault.joinpath(*_CONFIG_SUBPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    return vault


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A managed vault in container mode with no local model served.

    The production state #1873 was filed against: the operator's own cloud key
    and consent are present in the environment and must stay refused, and
    nothing answers on the loopback Ollama port.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    return _write_config(seed_vault(tmp_path / "vault"))


class _Vendor:
    """A fake vendor API behind the real SDK, recording every request.

    The real ``anthropic`` / ``openai`` client classes run end to end — their
    auth headers, their debug logging, their error mapping — with only the
    socket replaced by an :class:`httpx.MockTransport`.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, status: int = HTTP_OK):
        self.requests: list[httpx.Request] = []
        self._status = status
        real_anthropic = anthropic.Anthropic
        real_openai = openai.OpenAI
        transport = httpx.MockTransport(self._answer)

        def _anthropic(**kwargs: Any) -> anthropic.Anthropic:
            return real_anthropic(
                http_client=httpx.Client(transport=transport), **kwargs
            )

        def _openai(**kwargs: Any) -> openai.OpenAI:
            return real_openai(http_client=httpx.Client(transport=transport), **kwargs)

        monkeypatch.setattr(anthropic, "Anthropic", _anthropic)
        monkeypatch.setattr(openai, "OpenAI", _openai)

    @staticmethod
    def _turn() -> str:
        return json.dumps(
            {"notes": [{"quote": QUOTE, "kind": "pattern", "note": "It costs you."}]}
        )

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._status != HTTP_OK:
            # A vendor 401 that quotes the rejected key, as OpenAI's does.
            quoted = f"Incorrect API key provided: {CANARY_KEY[:6]}***{CANARY_TAIL}"
            return httpx.Response(
                self._status,
                json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": quoted},
                },
            )
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                HTTP_OK,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": self._turn()},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
        return httpx.Response(
            HTTP_OK,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": self._turn()}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    def keys_sent(self) -> list[str]:
        """Return the credential each recorded request authenticated with."""
        sent = []
        for request in self.requests:
            bearer = request.headers.get("authorization", "")
            sent.append(
                request.headers.get("x-api-key") or bearer.removeprefix("Bearer ")
            )
        return sent


def _swept(root: Path, *needles: str) -> list[str]:
    """Return every file under *root* whose bytes contain any of *needles*."""
    hits = []
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if any(needle.encode() in data for needle in needles):
                hits.append(str(path))
    return hits


@pytest.fixture
def every_log(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture every logger at DEBUG, the SDKs' and httpx's included."""
    caplog.set_level(logging.DEBUG)
    for name in ("anthropic", "openai", "httpx", "httpcore", "creek", "creek_mcp"):
        caplog.set_level(logging.DEBUG, logger=name)
    yield caplog


def _client(vault: Path) -> TestClient:
    from starlette.testclient import TestClient

    return TestClient(build_app(vault_path=vault))


# --------------------------------------------------------------------------- #
# The key type and its parse
# --------------------------------------------------------------------------- #


def test_a_request_key_never_renders_its_value() -> None:
    """``repr``, ``str`` and f-strings name the provider and redact the key."""
    key = parse_request_key("anthropic", CANARY_KEY)

    assert key is not None
    for rendered in (repr(key), str(key), f"{key}", f"{key!r}", repr([key])):
        assert CANARY_KEY not in rendered
        assert "anthropic" in rendered
    assert key.reveal() == CANARY_KEY


def test_a_request_key_refuses_to_be_pickled() -> None:
    """No job record, cache or copy can carry the key to disk by pickling."""
    key = RequestKey(provider="openai", _secret=OPENAI_CANARY)

    with pytest.raises(TypeError, match="never serialised"):
        pickle.dumps(key)


def test_no_headers_is_no_key() -> None:
    """A request without the headers carries nothing and changes nothing."""
    assert parse_request_key(None, None) is None


@pytest.mark.parametrize("provider", REQUEST_KEY_PROVIDERS)
def test_each_named_provider_parses_with_surrounding_whitespace(provider: str) -> None:
    """Both vendors parse, case-insensitively, with the key stripped."""
    key = parse_request_key(f" {provider.upper()} ", f"  {CANARY_KEY}\t")

    assert key is not None
    assert key.provider == provider
    assert key.reveal() == CANARY_KEY
    assert key.stage_config() == LLMConfig(provider=provider)


@pytest.mark.parametrize(
    ("provider", "key"),
    [
        ("anthropic", None),
        (None, CANARY_KEY),
        ("gemini", CANARY_KEY),
        ("ollama", CANARY_KEY),
        ("anthropic", ""),
        ("anthropic", "   "),
        ("anthropic", "sk ant with spaces"),
        ("anthropic", "sk-ant\r\nX-Injected: 1"),
        ("anthropic", "sk-ant-é"),
        ("anthropic", "k" * (MAX_REQUEST_KEY_LENGTH + 1)),
    ],
    ids=[
        "key-header-missing",
        "provider-header-missing",
        "not-a-byok-vendor",
        "a-local-provider",
        "empty",
        "blank",
        "contains-spaces",
        "header-injection",
        "non-ascii",
        "too-long",
    ],
)
def test_a_malformed_pair_is_refused_without_echoing_either_value(
    provider: str | None, key: str | None
) -> None:
    """Every malformed pair raises one fixed message carrying neither value."""
    with pytest.raises(InvalidRequestKeyError) as caught:
        parse_request_key(provider, key)

    assert CANARY_KEY not in str(caught.value)
    assert "sk ant" not in str(caught.value)


def test_the_longest_admitted_key_parses() -> None:
    """The bound is inclusive: a maximum-length key is not refused."""
    key = parse_request_key("openai", "k" * MAX_REQUEST_KEY_LENGTH)

    assert key is not None


def test_the_wire_enum_matches_the_parser() -> None:
    """The handshake's vendor list and the parser's admit the same names."""
    assert tuple(member.value for member in RequestKeyProvider) == REQUEST_KEY_PROVIDERS


# --------------------------------------------------------------------------- #
# Providers: the key is used, the environment is not
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cls", "provider", "secret"),
    [
        (AnthropicProvider, "anthropic", CANARY_KEY),
        (OpenAIProvider, "openai", OPENAI_CANARY),
    ],
    ids=["anthropic", "openai"],
)
def test_a_request_keyed_provider_sends_the_writers_key_not_the_operators(
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    cls: type[AnthropicProvider] | type[OpenAIProvider],
    provider: str,
    secret: str,
) -> None:
    """Managed consent is latched off, yet the writer's own key is used.

    The operator's key and consent sit in the environment (the ``managed``
    fixture). The SDK must authenticate with the request key and nothing else.
    """
    del managed
    vendor = _Vendor(monkeypatch)
    assert has_cloud_consent() is False

    built = cls(LLMConfig(provider=provider), request_key=RequestKey(provider, secret))
    built.complete("hello")

    assert built.available is True
    assert vendor.keys_sent() == [secret]


@pytest.mark.parametrize("cls", [AnthropicProvider, OpenAIProvider])
def test_without_a_request_key_a_managed_vault_still_refuses_the_operator_key(
    managed: Path, cls: type[AnthropicProvider] | type[OpenAIProvider]
) -> None:
    """#1864's latch is untouched: the operator's key builds nothing."""
    del managed
    provider = "openai" if cls is OpenAIProvider else "anthropic"

    with pytest.raises(RuntimeError, match="consent"):
        cls(LLMConfig(provider=provider))


@pytest.mark.parametrize(
    ("cls", "provider"),
    [(AnthropicProvider, "anthropic"), (OpenAIProvider, "openai")],
    ids=["anthropic", "openai"],
)
def test_an_empty_request_key_is_refused_without_reading_the_environment(
    managed: Path,
    cls: type[AnthropicProvider] | type[OpenAIProvider],
    provider: str,
) -> None:
    """An empty in-memory key never falls back to the operator's env key."""
    del managed

    with pytest.raises(RuntimeError, match="request-scoped") as caught:
        cls(LLMConfig(provider=provider), request_key=RequestKey(provider, ""))
    assert OPERATOR_KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("cls", "provider"),
    [(AnthropicProvider, "anthropic"), (OpenAIProvider, "openai")],
    ids=["anthropic", "openai"],
)
def test_a_vendor_error_quoting_the_key_is_reduced_to_its_status(
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    cls: type[AnthropicProvider] | type[OpenAIProvider],
    provider: str,
) -> None:
    """A 401 body that quotes the key never reaches the error or its chain."""
    del managed
    _Vendor(monkeypatch, status=HTTP_UNAUTHORIZED)
    built = cls(
        LLMConfig(provider=provider), request_key=RequestKey(provider, CANARY_KEY)
    )

    with pytest.raises(RuntimeError) as caught:
        built.complete("hello")

    assert str(caught.value).endswith(f"(HTTP {HTTP_UNAUTHORIZED})")
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert CANARY_TAIL not in str(caught.value)
    assert _chain_leaks(caught.value, CANARY_KEY, CANARY_TAIL) == []


def _chain_leaks(exc: BaseException, *needles: str) -> list[str]:
    """Return every rendering in *exc*'s whole chain that carries a needle.

    Walks ``__cause__`` and ``__context__`` recursively, checking each link's
    ``str``/``repr`` and, for a vendor error, its request's headers.
    """
    leaks: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        request = getattr(link, "request", None)
        headers = getattr(request, "headers", {})
        rendered = [str(link), repr(link), *(str(v) for v in dict(headers).values())]
        leaks.extend(
            text for text in rendered if any(needle in text for needle in needles)
        )
        pending.extend(
            nxt for nxt in (link.__cause__, link.__context__) if nxt is not None
        )
    return leaks


def test_an_operator_keyed_4xx_keeps_its_actionable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The redaction is request-key-only: the self-hoster's 4xx detail stays."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    _Vendor(monkeypatch, status=HTTP_UNAUTHORIZED)

    with pytest.raises(RuntimeError, match="Incorrect API key provided") as caught:
        AnthropicProvider(LLMConfig(provider="anthropic")).complete("hello")
    assert caught.value.__cause__ is not None


# --------------------------------------------------------------------------- #
# Admission and the generation chokepoint
# --------------------------------------------------------------------------- #


def test_admission_requires_a_managed_vault_and_a_matching_byok_provider() -> None:
    """Every condition of the one exception is necessary."""
    key = RequestKey("anthropic", CANARY_KEY)
    managed_env = {MANAGED_VAULT_ENV: "1"}
    anthropic_stage = LLMConfig(provider="anthropic")

    assert request_key_admitted(anthropic_stage, key, managed_env) is True
    assert request_key_admitted(anthropic_stage, None, managed_env) is False
    assert request_key_admitted(anthropic_stage, key, {}) is False
    assert request_key_admitted(LLMConfig(provider="ollama"), key, managed_env) is False
    assert request_key_admitted(LLMConfig(provider="openai"), key, managed_env) is False
    gemini_key = RequestKey("gemini", CANARY_KEY)
    gemini_stage = LLMConfig(provider="gemini")
    assert request_key_admitted(gemini_stage, gemini_key, managed_env) is False


@pytest.mark.parametrize("marker", ["0", "", "false", "no", "off"])
def test_a_non_truthy_marker_admits_no_request_key(marker: str) -> None:
    """Only an affirmative marker opens BYOK egress; presence alone does not."""
    key = RequestKey("anthropic", CANARY_KEY)
    stage = LLMConfig(provider="anthropic")

    assert request_key_admitted(stage, key, {MANAGED_VAULT_ENV: marker}) is False


@pytest.mark.parametrize("marker", ["1", "true", "YES", " True "])
def test_a_truthy_marker_admits_a_request_key(marker: str) -> None:
    """The affirmative spellings, case- and whitespace-insensitive, admit."""
    key = RequestKey("anthropic", CANARY_KEY)
    stage = LLMConfig(provider="anthropic")

    assert request_key_admitted(stage, key, {MANAGED_VAULT_ENV: marker}) is True


@pytest.mark.parametrize("marker", ["0", ""])
def test_a_non_truthy_marker_still_restricts_like_any_marker(
    monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    """#1864's latch is unchanged: any presence still withholds cloud consent."""
    monkeypatch.setenv(MANAGED_VAULT_ENV, marker)
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")

    assert is_managed_vault() is True
    assert has_cloud_consent() is False


class _Inventory:
    """Count every Ollama inventory request, answering with *response*."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, response: httpx.Response):
        self.urls: list[str] = []
        self._response = response
        monkeypatch.setattr(httpx, "Client", self._client)

    def _client(self, **_kwargs: object) -> _Inventory:
        return self

    def __enter__(self) -> _Inventory:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        return self._response


@pytest.mark.parametrize("tier", [PrivacyTier.OPEN, PrivacyTier.PERSONAL])
@pytest.mark.parametrize(
    ("provider", "cls"),
    [("anthropic", AnthropicProvider), ("openai", OpenAIProvider)],
    ids=["anthropic", "openai"],
)
def test_a_managed_vault_builds_the_writers_provider_for_open_and_personal(
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier: PrivacyTier,
    provider: str,
    cls: type[AnthropicProvider] | type[OpenAIProvider],
) -> None:
    """Container mode, no local model: the key alone makes reflection possible."""
    from creek_mcp.server import _build_reflect_llm_factory

    inventory = _Inventory(monkeypatch, _served())
    key = RequestKey(provider, CANARY_KEY)

    llm = _build_reflect_llm_factory(managed, key)(tier, max_tokens=16)

    assert isinstance(llm, partial)
    assert isinstance(llm.args[0], cls)
    assert inventory.urls == []


def test_without_a_key_the_managed_refusal_is_unchanged(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key, no local model: the same fixed refusal as before ADR-0015."""
    from creek_mcp.server import _build_reflect_llm_factory

    _Inventory(monkeypatch, _served())

    with pytest.raises(RuntimeError, match=_REFUSAL):
        _build_reflect_llm_factory(managed)(PrivacyTier.OPEN, max_tokens=16)


def test_the_operators_configured_cloud_stage_is_still_refused(
    tmp_path: Path, managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault config routing generation to the operator's key stays refused.

    With the operator's key and consent in the environment, and no request key,
    the pin refuses the stage from its config — the #1858 decision.
    """
    from creek_mcp.server import _build_reflect_llm_factory

    del tmp_path
    _Inventory(monkeypatch, _served())

    def _cloud(data: dict[str, Any]) -> None:
        data["llm"]["generation"] = {**data["llm"]["default"], "provider": "anthropic"}

    _write_config(managed, _cloud)

    with pytest.raises(RuntimeError, match=_REFUSAL):
        _build_reflect_llm_factory(managed)(PrivacyTier.OPEN, max_tokens=16)


def test_a_self_connected_vault_ignores_a_request_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the managed marker a key is never used: the pin refusal stands."""
    from creek_mcp.server import _build_reflect_llm_factory

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    vault = _write_config(seed_vault(tmp_path / "self"))
    _Inventory(monkeypatch, _served())

    with pytest.raises(RuntimeError, match=_REFUSAL):
        _build_reflect_llm_factory(vault, RequestKey("anthropic", CANARY_KEY))(
            PrivacyTier.OPEN, max_tokens=16
        )


def test_intimate_with_a_key_is_forced_onto_the_pinned_local_model(
    tmp_path: Path, managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router redirects INTIMATE off the key's cloud stage to local."""
    from creek.classify.llm.providers import OllamaProvider
    from creek_mcp import model_pin
    from creek_mcp.server import _build_reflect_llm_factory

    _pin(tmp_path, monkeypatch)
    _Inventory(monkeypatch, _served())
    monkeypatch.setattr(
        model_pin,
        "build_request_scoped_provider",
        lambda _key: pytest.fail("the writer's provider was built for INTIMATE"),
    )

    llm = _build_reflect_llm_factory(managed, RequestKey("anthropic", CANARY_KEY))(
        PrivacyTier.INTIMATE, max_tokens=16
    )

    assert isinstance(llm, partial)
    assert isinstance(llm.args[0], OllamaProvider)


def test_intimate_with_a_key_and_no_local_model_is_refused(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pinned local model: INTIMATE with a key is refused, never sent."""
    from creek_mcp.server import _build_reflect_llm_factory

    _Inventory(monkeypatch, _served())
    vendor = _Vendor(monkeypatch)

    with pytest.raises(RuntimeError, match=_REFUSAL):
        _build_reflect_llm_factory(managed, RequestKey("anthropic", CANARY_KEY))(
            PrivacyTier.INTIMATE, max_tokens=16
        )
    assert vendor.requests == []


def test_intimate_with_a_key_and_a_cloud_default_raises_the_routing_error(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even the key's provider as the vault default cannot carry INTIMATE."""
    from creek_mcp.server import _build_reflect_llm_factory

    def _cloud_default(data: dict[str, Any]) -> None:
        data["llm"]["default"]["provider"] = "anthropic"

    _write_config(managed, _cloud_default)
    vendor = _Vendor(monkeypatch)

    with pytest.raises(IntimateRoutingError):
        _build_reflect_llm_factory(managed, RequestKey("anthropic", CANARY_KEY))(
            PrivacyTier.INTIMATE, max_tokens=16
        )
    assert vendor.requests == []


# --------------------------------------------------------------------------- #
# Over /v1: it works, and the canary goes nowhere
# --------------------------------------------------------------------------- #


def _assert_canary_absent(
    root: Path, logs: pytest.LogCaptureFixture, *responses: httpx.Response
) -> None:
    """Sweep the response, every log record and every file under *root*."""
    for response in responses:
        assert CANARY_KEY not in response.text
        assert CANARY_TAIL not in response.text
        assert CANARY_KEY not in json.dumps(dict(response.headers))
    assert CANARY_KEY not in logs.text
    assert CANARY_TAIL not in logs.text
    for record in logs.records:
        assert CANARY_KEY not in repr(record.args)
    assert _swept(root, CANARY_KEY, CANARY_TAIL) == []


@pytest.mark.parametrize(
    ("provider", "secret"),
    [("anthropic", CANARY_KEY), ("openai", OPENAI_CANARY)],
    ids=["anthropic", "openai"],
)
def test_a_managed_reflection_is_written_with_the_writers_key(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
    provider: str,
    secret: str,
) -> None:
    """The verified-broken pilot path: ``200`` with grounded notes, key unseen."""
    vendor = _Vendor(monkeypatch)

    with _client(managed) as http:
        response = http.post(
            REFLECTIONS_PATH,
            json={"content": ENTRY},
            headers=_key_headers(provider, secret, ceiling="personal"),
        )

    assert response.status_code == HTTP_OK, response.text
    assert response.json()["notes"][0]["quote"] == QUOTE
    assert vendor.keys_sent() == [secret]
    assert OPERATOR_KEY not in vendor.keys_sent()
    for needle in (secret, secret[-8:]):
        assert needle not in response.text
        assert needle not in every_log.text
        assert _swept(tmp_path, needle) == []


def test_a_managed_reflection_without_a_key_is_still_unavailable(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``503 temporarily_unavailable``, unchanged, and no vendor is dialled."""
    vendor = _Vendor(monkeypatch)

    with _client(managed) as http:
        response = http.post(
            REFLECTIONS_PATH, json={"content": ENTRY}, headers=headers()
        )

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert response.json()["code"] == "temporarily_unavailable"
    assert vendor.requests == []


def test_a_rejected_key_is_unavailable_and_never_echoed(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """A vendor 401 quoting the key surfaces as the fixed refusal only."""
    _Vendor(monkeypatch, status=HTTP_UNAUTHORIZED)

    with _client(managed) as http:
        response = http.post(
            REFLECTIONS_PATH, json={"content": ENTRY}, headers=_key_headers()
        )

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    _assert_canary_absent(tmp_path, every_log, response)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", REFLECTIONS_PATH, {"content": ENTRY}),
        ("POST", CLASSIFICATIONS_PATH, {"method": "llm"}),
        (
            "POST",
            LETTERS_TEMPLATE.format(external_id="letter-1"),
            {
                "content": ENTRY,
                "tier": "open",
                "note": {"anchor_text": QUOTE, "kind": "pattern", "note": "Look."},
            },
        ),
    ],
    ids=["reflections", "classifications", "letters"],
)
def test_a_malformed_pair_is_invalid_request_on_every_keyed_route(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    """A key for a vendor the vault does not take is refused before any work."""
    vendor = _Vendor(monkeypatch)

    with _client(managed) as http:
        response = http.request(
            method, path, json=body, headers=_key_headers("gemini", CANARY_KEY)
        )

    assert response.status_code == HTTP_UNPROCESSABLE, response.text
    assert response.json()["code"] == "invalid_request"
    assert vendor.requests == []
    _assert_canary_absent(tmp_path, every_log, response)


def test_a_managed_letter_is_written_with_the_writers_key(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """The vault's letter generation runs on the writer's key, which is not stored."""
    vendor = _Vendor(monkeypatch)
    body = {
        "content": ENTRY,
        "tier": "personal",
        "note": {"anchor_text": QUOTE, "kind": "pattern", "note": "It costs you."},
    }

    with _client(managed) as http:
        response = http.post(
            LETTERS_TEMPLATE.format(external_id="letter-1"),
            json=body,
            headers=_key_headers(ceiling="personal"),
        )

    assert response.status_code == HTTP_OK, response.text
    assert vendor.keys_sent() == [CANARY_KEY]
    _assert_canary_absent(tmp_path, every_log, response)


# --------------------------------------------------------------------------- #
# Classification: the writer's key for OPEN/PERSONAL, never for INTIMATE
# --------------------------------------------------------------------------- #

_OPEN_BODY: Final[str] = "zz-open-1873 a plain note about the garden and the weather"
_INTIMATE_BODY: Final[str] = "zz-intimate-1873 the thing I have never told anyone"


def _fragment(vault: Path, name: str, body: str, tier: str) -> None:
    """Write one tiered fragment straight to disk."""
    import frontmatter

    notes = vault / "01-Fragments" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content=body,
        type="fragment",
        id=name,
        title=name,
        source={"platform": "journal", "author": "self"},
        privacy_tier=tier,
    )
    (notes / f"{name}.md").write_text(frontmatter.dumps(post), encoding="utf-8")


def _await_job(http: TestClient, job_id: str) -> dict[str, Any]:
    """Poll one job to a terminal state."""
    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        body: dict[str, Any] = http.get(f"/v1/jobs/{job_id}", headers=headers()).json()
        if body["state"] in {"succeeded", "failed"}:
            return body
        time.sleep(0.02)
    pytest.fail("classification job did not finish")


def test_an_llm_classification_job_uses_the_key_for_open_and_never_intimate(
    tmp_path: Path,
    managed: Path,
    monkeypatch: pytest.MonkeyPatch,
    every_log: pytest.LogCaptureFixture,
) -> None:
    """Onboarding classifies with the writer's key; INTIMATE never leaves.

    The job record is persisted inside the vault before ``202``; the sweep
    proves the key rode to the worker in memory and was never written there.
    """
    vendor = _Vendor(monkeypatch)
    _fragment(managed, "open-note", _OPEN_BODY, "open")
    _fragment(managed, "intimate-note", _INTIMATE_BODY, "intimate")

    with _client(managed) as http:
        accepted = http.post(
            CLASSIFICATIONS_PATH,
            json={"method": "llm"},
            headers=_key_headers(ceiling="personal"),
        )
        assert accepted.status_code == HTTP_ACCEPTED, accepted.text
        terminal = _await_job(http, accepted.json()["job_id"])

    assert terminal["state"] == "succeeded", terminal
    sent = [request.content.decode() for request in vendor.requests]
    assert any(_OPEN_BODY in content for content in sent)
    assert not any(_INTIMATE_BODY in content for content in sent)
    assert set(vendor.keys_sent()) == {CANARY_KEY}
    _assert_canary_absent(tmp_path, every_log, accepted)


def test_a_keyless_llm_classification_on_a_managed_vault_dials_no_vendor(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key, no pinned model: no vendor is dialled; the rules pass runs.

    Until the 2026-10-24 amendment to ADR-0015 this job failed outright, which
    left every keyless managed vault permanently unclassified. The invariant
    that matters -- nothing leaves for a vendor without the writer's key -- is
    unchanged: the fallback is the model-free ``rules`` pass, and the job says
    so by naming ``rules`` as the method that ran.
    """
    vendor = _Vendor(monkeypatch)
    _fragment(managed, "open-note", _OPEN_BODY, "open")

    with _client(managed) as http:
        accepted = http.post(
            CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers()
        )
        terminal = _await_job(http, accepted.json()["job_id"])

    assert terminal["state"] == "succeeded"
    assert terminal["result"]["method"] == "rules"
    assert vendor.requests == []


def test_the_classify_tool_never_audits_the_key(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit record a keyed pass appends names no key and no vendor."""
    from creek_mcp.tools.classify import classify_tool

    _Vendor(monkeypatch)
    _fragment(managed, "open-note", _OPEN_BODY, "open")

    result = classify_tool(
        vault_path=managed,
        method="llm",
        request_key=RequestKey("anthropic", CANARY_KEY),
    )

    assert result["status"] == "ok"
    audit = (managed / "00-Creek-Meta" / "audit").rglob("*")
    assert all(CANARY_KEY not in p.read_text() for p in audit if p.is_file())


# --------------------------------------------------------------------------- #
# The published contract
# --------------------------------------------------------------------------- #


def test_only_the_model_calling_routes_publish_the_key_headers() -> None:
    """Reflections, letters and classifications; nothing that calls no model."""
    keyed = {(spec.method, spec.path) for spec in ROUTES if spec.accepts_request_key}

    assert keyed == {
        ("POST", "/v1/reflections"),
        ("POST", "/v1/letters/{external_id}"),
        ("POST", "/v1/classifications"),
    }
    document = build_openapi()["paths"]
    for spec in ROUTES:
        names = {
            parameter["name"]
            for parameter in document[spec.path][spec.method.lower()]["parameters"]
        }
        published = {PROVIDER_HEADER, PROVIDER_KEY_HEADER} <= names
        assert published is spec.accepts_request_key, spec.operation_id
        if published:
            optional = [
                parameter["required"]
                for parameter in document[spec.path][spec.method.lower()]["parameters"]
                if parameter["name"] in {PROVIDER_HEADER, PROVIDER_KEY_HEADER}
            ]
            assert optional == [False, False]


@pytest.mark.parametrize(
    ("marker", "expected"),
    [("1", ["anthropic", "openai"]), (None, []), ("0", []), ("", [])],
    ids=["managed", "self-connected", "marker-zero", "marker-empty"],
)
def test_capabilities_report_request_key_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker: str | None,
    expected: list[str],
) -> None:
    """A 0.18 caller can tell, before forwarding a key, whether it is used."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    if marker is None:
        monkeypatch.delenv(MANAGED_VAULT_ENV, raising=False)
    else:
        monkeypatch.setenv(MANAGED_VAULT_ENV, marker)
    vault = seed_vault(tmp_path / "vault")

    with _client(vault) as http:
        current = http.get(CAPABILITIES_PATH, headers=headers()).json()
        silent = http.get(CAPABILITIES_PATH, headers=headers(minor=None)).json()
        prior = http.get(CAPABILITIES_PATH, headers=headers(minor="0.17")).json()

    assert current["request_key_providers"] == expected
    assert silent["request_key_providers"] == expected
    assert "request_key_providers" not in prior
