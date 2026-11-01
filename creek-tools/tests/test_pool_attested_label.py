"""``pool:attested:<vendor>`` means an attestation gate passed (#2040).

The label used to be the vendor name from ``CREEK_POOL_VENDOR`` interpolated
into a fixed prefix, so a vendor nothing attests was published as attested —
a false receipt, which release brief R4 forbids ("No false local/operator-blind
label"). The label is now derived from the attestation path itself:
:func:`~creek.classify.llm.pool_boundary.attested_pool_vendor` returns a vendor
only in **sidecar mode** (#1896), the one mode in which every content dial is
preceded by the sidecar's attestation report and refused, with nothing sent,
when the report is off the pin. Nothing else — no other vendor name, no
near-miss spelling — can set it.

A named vendor outside sidecar mode is handled two ways, by vault kind:

- **Managed vault: refused.** ADR-0016 admits two pools on a managed Machine —
  the Creek-run O3 pool (vendor unset, ``pool:creek``) and the vendor-attested
  O5 pool, which is only O5 *with* its attestation gate (Decision 3.3; D1.10
  "the attestation fails → the vault refuses ... and dials nothing else";
  ADR-0018 item 5 "no embedding ever leaves by an unattested route"). A named
  vendor with no gate is neither, so every dial is refused before a socket
  opens.
- **Self-hosted vault: served, unlabelled.** The operator chose the
  destination; the honest receipt omits ``inference_source``, which the
  published contract already allows ("absent rather than guessed when no route
  this vocabulary names served", contract 0.19). No wire value changes.

Each receipt the issue names is pinned: the reflection's (the route suites),
the classification pass's (here), and the embeddings' — which publish no label
of their own and so are pinned as "refused with nothing sent" on the only
vault kind that embeds through the pool. The sidecar's own answer, labelled
attested on a managed vault, is pinned in ``tests/test_pool_attested_sidecar.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.classify.llm.pool_boundary import (
    POOL_SIDECAR_VENDOR,
    POOL_URL_ENV,
    POOL_VENDOR_ENV,
    PoolUnattestedVendorError,
    PoolUnreachableError,
    attested_pool_vendor,
    pool_embed,
    pool_generate,
    pool_list_models,
)
from creek.config import CreekConfig, load_vault_config
from creek_mcp.api.models import INFERENCE_SOURCE_POOL_CREEK
from creek_mcp.provenance import stage_inference_source
from creek_mcp.tools import classify as classify_module
from tests.pool_support import SocketSpy

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.classify.llm.pool_boundary import PoolResponse

_UNGATED_VENDORS: Final[tuple[str, ...]] = (
    "tinfoil",
    "example-unattested-vendor",
    "privatemode-proxy",
    "private-mode",
)
"""Vendor names no attestation gate covers, near-misses of the sidecar's included."""

_SIDECAR_LOOPBACK: Final[tuple[str, int]] = ("127.0.0.1", 18443)
"""Sidecar mode admits only a plain-HTTP loopback literal as the destination."""

_POOL_HOST: Final[str] = "pool.internal"
_POOL_PORT: Final[int] = 8443
_POOL_URL: Final[str] = f"http://{_POOL_HOST}:{_POOL_PORT}"
_CONFIG_SUBPATH: Final[tuple[str, ...]] = ("00-Creek-Meta", "creek_config.yaml")


def _list() -> PoolResponse:
    """Dial the pool's identity listing."""
    return pool_list_models()


def _generate() -> PoolResponse:
    """Dial one generation."""
    return pool_generate({"model": "m", "prompt": "a synthetic prompt"})


def _embed() -> PoolResponse:
    """Dial one embeddings batch."""
    return pool_embed({"model": "m", "input": ["a synthetic title"]})


_DIALS: Final[list[Any]] = [
    pytest.param(_list, id="listing"),
    pytest.param(_generate, id="generation"),
    pytest.param(_embed, id="embeddings"),
]


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a synthetic destination; no vendor, no managed marker."""
    monkeypatch.setenv(POOL_URL_ENV, _POOL_URL)
    monkeypatch.delenv(POOL_VENDOR_ENV, raising=False)
    monkeypatch.delenv(MANAGED_VAULT_ENV, raising=False)


class TestTheLabelFollowsTheGate:
    """``attested`` is a fact about a gate, never about a name."""

    @pytest.mark.parametrize("vendor", _UNGATED_VENDORS)
    def test_a_vendor_nothing_attests_is_never_labelled_attested(
        self, vendor: str
    ) -> None:
        """The #2040 defect: the name alone used to yield the label."""
        source = stage_inference_source("pool", {POOL_VENDOR_ENV: vendor})

        assert source is None
        assert attested_pool_vendor({POOL_VENDOR_ENV: vendor}) is None

    def test_sidecar_mode_is_labelled_attested(self) -> None:
        """The attestation path's own mode is the one that earns the label."""
        environ = {POOL_VENDOR_ENV: POOL_SIDECAR_VENDOR}

        assert attested_pool_vendor(environ) == POOL_SIDECAR_VENDOR
        assert stage_inference_source("pool", environ) == (
            f"pool:attested:{POOL_SIDECAR_VENDOR}"
        )

    def test_the_creek_run_pool_is_unchanged(self) -> None:
        """O3 has no vendor, makes no attestation claim, and keeps its value."""
        assert stage_inference_source("pool", {}) == INFERENCE_SOURCE_POOL_CREEK


class TestTheClassificationReceipt:
    """``ClassificationResponse.inference_source`` reads the same decision."""

    @staticmethod
    def _config(tmp_path: Path) -> Any:
        """Load a vault config whose ``classification`` stage is the pool."""
        vault = tmp_path / "vault"
        (vault / "01-Fragments").mkdir(parents=True)
        (vault / "00-Creek-Meta").mkdir(parents=True)
        data: dict[str, Any] = CreekConfig().model_dump(mode="json")
        data["vault_path"] = str(vault)
        data["llm"]["classification"] = {**data["llm"]["default"], "provider": "pool"}
        vault.joinpath(*_CONFIG_SUBPATH).write_text(
            yaml.dump(data, sort_keys=False), encoding="utf-8"
        )
        return load_vault_config(vault)

    @pytest.mark.parametrize("vendor", _UNGATED_VENDORS)
    @pytest.mark.usefixtures("pinned")
    def test_an_ungated_vendor_classification_is_unlabelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vendor: str
    ) -> None:
        """No gate, no ``attested``: the pass goes out with the field absent."""
        monkeypatch.setenv(POOL_VENDOR_ENV, vendor)

        source = classify_module._pass_inference_source(
            self._config(tmp_path), "llm", None, None
        )

        assert source is None

    @pytest.mark.usefixtures("pinned")
    def test_a_sidecar_classification_is_labelled_attested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the same pass in sidecar mode names the sidecar vendor."""
        monkeypatch.setenv(POOL_VENDOR_ENV, POOL_SIDECAR_VENDOR)

        source = classify_module._pass_inference_source(
            self._config(tmp_path), "llm", None, None
        )

        assert source == f"pool:attested:{POOL_SIDECAR_VENDOR}"


@pytest.mark.usefixtures("pinned")
class TestAManagedVaultRefusesAnUngatedVendor:
    """Every pool dial — listing, generation, embeddings — sends nothing."""

    @pytest.mark.parametrize("dial", _DIALS)
    @pytest.mark.parametrize("vendor", _UNGATED_VENDORS)
    def test_no_socket_opens(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        dial: Callable[[], PoolResponse],
        vendor: str,
    ) -> None:
        """Refused before any connection; the message names no host or vendor."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        monkeypatch.setenv(POOL_VENDOR_ENV, vendor)
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolUnattestedVendorError) as caught:
            dial()

        assert spy.destinations == []
        message = str(caught.value)
        assert vendor not in message
        assert _POOL_HOST not in message
        assert _POOL_HOST not in caplog.text

    def test_the_attesting_sidecar_is_still_dialled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the refusal is about the gate, not about naming a vendor.

        Only the content-free listing is dialled here: a content dial in
        sidecar mode goes through the attestation report first, which
        ``tests/test_pool_attested_sidecar.py`` drives against a fake sidecar.
        """
        host, port = _SIDECAR_LOOPBACK
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        monkeypatch.setenv(POOL_VENDOR_ENV, POOL_SIDECAR_VENDOR)
        monkeypatch.setenv(POOL_URL_ENV, f"http://{host}:{port}")
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolUnreachableError):
            pool_list_models()

        assert spy.destinations == [_SIDECAR_LOOPBACK]

    @pytest.mark.parametrize("dial", _DIALS)
    def test_the_creek_run_pool_still_dials(
        self, monkeypatch: pytest.MonkeyPatch, dial: Callable[[], PoolResponse]
    ) -> None:
        """O3 on a managed Machine is admitted by ADR-0016 and is untouched."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolUnreachableError):
            dial()

        assert spy.destinations == [(_POOL_HOST, _POOL_PORT)]


@pytest.mark.usefixtures("pinned")
class TestASelfHostedVaultServesAnUngatedVendorUnlabelled:
    """The operator chose the destination; only the label is withheld."""

    @pytest.mark.parametrize("dial", _DIALS)
    def test_the_dial_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, dial: Callable[[], PoolResponse]
    ) -> None:
        """No managed marker: the pinned pool is dialled as before."""
        monkeypatch.setenv(POOL_VENDOR_ENV, _UNGATED_VENDORS[0])
        spy = SocketSpy(monkeypatch)

        with pytest.raises(PoolUnreachableError):
            dial()

        assert spy.destinations == [(_POOL_HOST, _POOL_PORT)]
