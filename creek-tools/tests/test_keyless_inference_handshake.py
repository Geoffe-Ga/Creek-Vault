"""``keyless_inference`` on the handshake: a pool-less managed vault says so (#1938).

A managed vault ships no inference runtime and refuses its operator's cloud key,
so until the shared pool is pinned in its Machine environment, the only model
call it can answer is one carrying the writer's own key (ADR-0015). Before
contract 0.20 nothing in ``GET /v1/capabilities`` said that: ``inference_pool:
false`` is equally what a self-connected vault running its own model reports.
So adepthood charged a writer, dialled, was refused, refunded, and told them to
try again — on 2026-10-21, deterministically, every time.

These tests pin three things: the field's three arms, the minor gate that keeps a
``0.19`` consumer's handshake byte-identical, and — the one that makes the field
a promise rather than a label — that a vault advertising ``false`` really does
refuse a keyless reflection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.classify.llm.pool_boundary import POOL_URL_ENV
from creek.config import CreekConfig
from creek_mcp.api.models import (
    KEYLESS_INFERENCE_SINCE_MINOR,
    SUPPORTED_CONTRACT_MINORS,
)
from creek_mcp.httpapi.capabilities import keyless_inference_available
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    REFLECTIONS_PATH,
    build_app,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.testclient import TestClient

_CONFIG_SUBPATH: Final[tuple[str, ...]] = ("00-Creek-Meta", "creek_config.yaml")
_PRIOR_MINOR: Final[str] = "0.19"
_POOL_URL: Final[str] = "http://127.0.0.1:9/"
_HTTP_SERVICE_UNAVAILABLE: Final[int] = 503
_ENTRY: Final[str] = "Walked the creek before work and felt the morning settle."


def _vault(tmp_path: Path) -> Path:
    """Scaffold a vault whose ``generation`` stage is routed to the pool."""
    vault = seed_vault(tmp_path / "vault")
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["generation"] = {**data["llm"]["default"], "provider": "pool"}
    path = vault.joinpath(*_CONFIG_SUBPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    return vault


def _client(vault: Path) -> TestClient:
    """Return a client over the ``/v1`` app serving *vault*."""
    from starlette.testclient import TestClient

    return TestClient(build_app(vault_path=vault))


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from an empty cwd with neither the managed marker nor a pool set."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv(MANAGED_VAULT_ENV, raising=False)
    monkeypatch.delenv(POOL_URL_ENV, raising=False)


class TestTheDecision:
    """The predicate is a fact about the Machine environment and nothing else."""

    def test_a_managed_vault_with_no_pool_has_no_keyless_inference(self) -> None:
        """The pilot's exact environment on 2026-10-21."""
        assert keyless_inference_available({MANAGED_VAULT_ENV: "1"}) is False

    def test_a_blank_pool_url_is_no_pool(self) -> None:
        """A blank destination is not a destination, as ``pool_url_configured`` says."""
        environ = {MANAGED_VAULT_ENV: "1", POOL_URL_ENV: "   "}

        assert keyless_inference_available(environ) is False

    def test_a_managed_vault_with_a_pool_may_answer_keyless(self) -> None:
        """Once ADR-0016's pool is pinned, a keyless call is the pool's to answer."""
        environ = {MANAGED_VAULT_ENV: "1", POOL_URL_ENV: _POOL_URL}

        assert keyless_inference_available(environ) is True

    def test_a_self_connected_vault_keeps_its_own_routing(self) -> None:
        """No marker means the operator's own config decides, exactly as before."""
        assert keyless_inference_available({}) is True


@pytest.mark.usefixtures("isolated")
class TestTheHandshake:
    """``GET /v1/capabilities`` serves the decision, gated on contract 0.20."""

    def test_a_pool_less_managed_vault_advertises_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 0.20 caller learns before dialling that only a writer key will work."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        with _client(_vault(tmp_path)) as http:
            body = http.get(CAPABILITIES_PATH, headers=headers()).json()

        assert body["keyless_inference"] is False

    def test_a_pooled_managed_vault_advertises_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same handshake flips the moment a pool is pinned."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        monkeypatch.setenv(POOL_URL_ENV, _POOL_URL)
        with _client(_vault(tmp_path)) as http:
            body = http.get(CAPABILITIES_PATH, headers=headers()).json()

        assert body["keyless_inference"] is True

    def test_a_self_connected_vault_advertises_true(self, tmp_path: Path) -> None:
        """A self-connected vault's handshake does not start refusing writers."""
        with _client(_vault(tmp_path)) as http:
            body = http.get(CAPABILITIES_PATH, headers=headers()).json()

        assert body["keyless_inference"] is True

    def test_a_prior_minor_is_never_told(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``0.19`` consumer validating its closed schema sees no new key."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        with _client(_vault(tmp_path)) as http:
            body = http.get(
                CAPABILITIES_PATH, headers=headers(minor=_PRIOR_MINOR)
            ).json()

        assert "keyless_inference" not in body
        assert "inference_pool" in body

    def test_a_caller_declaring_no_minor_is_told(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silence is answered with everything, as for every other field."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        with _client(_vault(tmp_path)) as http:
            body = http.get(CAPABILITIES_PATH, headers=headers(minor=None)).json()

        assert body["keyless_inference"] is False


@pytest.mark.usefixtures("isolated")
def test_false_is_a_promise_the_reflect_route_keeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault that advertises ``false`` really does refuse a keyless reflection.

    Without this the field would be a label: a future change could teach a
    pool-less managed vault to answer keyless after all, and adepthood would go
    on refusing writers the vault could have served.
    """
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    vault = _vault(tmp_path)
    with _client(vault) as http:
        advertised = http.get(CAPABILITIES_PATH, headers=headers()).json()
        reflected = http.post(
            REFLECTIONS_PATH,
            json={"content": _ENTRY},
            headers=headers(ceiling="personal"),
        )

    assert advertised["keyless_inference"] is False
    assert reflected.status_code == _HTTP_SERVICE_UNAVAILABLE, reflected.text
    assert reflected.json()["code"] == "temporarily_unavailable"


def test_the_field_arrived_at_its_own_minor() -> None:
    """0.20 widened the window rather than shifting it, and the gate is literal.

    Every assertion here is about **this field's** minor, which is what the
    test is named for. It used to open with ``CONTRACT_MINOR == "0.20"`` as
    well, and that line has been dropped: "0.20 is the current minor" is a
    claim about the *next* bump rather than about ``keyless_inference``, so it
    went red the first time an unrelated minor shipped (#1903 step 6, which
    moved the current minor to 0.21 without touching this field or this
    handshake). The claim it was making is not lost — the current minor is
    pinned to the ADR by ``test_adr_publishes_the_runtime_contract_version``,
    and the window around it is derived from it by
    ``test_the_immediately_previous_minor_is_always_served`` (R2-5) in
    ``tests/test_adepthood_contract_models.py``.

    ``"0.20" in SUPPORTED_CONTRACT_MINORS`` replaces it and is the assertion
    actually worth having here: it says the minor this field shipped at is
    *still served*, which is the property a consumer pinned to it depends on
    and which a later bump could silently break.
    """
    assert KEYLESS_INFERENCE_SINCE_MINOR == "0.20"
    assert "0.20" in SUPPORTED_CONTRACT_MINORS
    assert "0.19" in SUPPORTED_CONTRACT_MINORS
    assert "0.18" in SUPPORTED_CONTRACT_MINORS
