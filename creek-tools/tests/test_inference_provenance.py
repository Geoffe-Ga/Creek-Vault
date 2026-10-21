"""Which route answered, decided in one place (#1894, ADR-0016 D1.11).

:mod:`creek_mcp.provenance` is the only module that turns a resolved stage
into a published ``inference_source``, and this module pins every branch of
that decision — including the two that publish **nothing**, which are the ones
a later edit is most likely to "fix" into a guess:

- an operator's own cloud key on a self-hosted vault is none of ADR-0016's
  four values, so the answer goes out unlabelled rather than mislabelled;
- a pass that called no model has no route to name at all.

The classification pass's own labelling is pinned here too, beside the
decision rather than behind the HTTP job surface, because what the route test
can show is only that a label *survives* the job record.
"""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml
from starlette.requests import Request

from creek.classify.classify_engine import _PreparedFragment
from creek.classify.llm.pool_boundary import (
    POOL_URL_ENV,
    POOL_VENDOR_ENV,
    pool_wire_tier,
)
from creek.classify.llm.request_key import RequestKey
from creek.config import CreekConfig, load_vault_config
from creek.models import PrivacyTier
from creek_mcp.api.models import (
    INFERENCE_SOURCE_LOCAL,
    INFERENCE_SOURCE_POOL_CREEK,
    PROVENANCE_SINCE_MINOR,
    attested_pool_source,
    request_key_source,
)
from creek_mcp.api.routes import CONTRACT_VERSION_HEADER
from creek_mcp.httpapi.context import RequestContext
from creek_mcp.httpapi.generation import declared_minor_at_least
from creek_mcp.provenance import (
    InferenceProvenance,
    request_key_inference_source,
    stage_inference_source,
)
from creek_mcp.tools import classify as classify_module

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG_SUBPATH: Final[tuple[str, ...]] = ("00-Creek-Meta", "creek_config.yaml")
_VENDOR: Final[str] = "tinfoil"
_KEY: Final[str] = "sk-ant-api03-PROVENANCEtestKEY-never-logged"


class TestStageLabelling:
    """One resolved stage in, one published value — or none — out."""

    def test_a_creek_run_pool_is_named_without_a_vendor(self) -> None:
        """``CREEK_POOL_VENDOR`` unset is ADR-0016 O3: the Creek-run pool."""
        assert stage_inference_source("pool", {}) == INFERENCE_SOURCE_POOL_CREEK

    def test_an_attested_pool_carries_the_vendor_the_machine_names(self) -> None:
        """O5's label is parameterised, which is why the wire takes a pattern."""
        source = stage_inference_source("pool", {POOL_VENDOR_ENV: _VENDOR})
        assert source == attested_pool_source(_VENDOR)
        assert source == f"pool:attested:{_VENDOR}"

    @pytest.mark.parametrize(
        "offered",
        [
            pytest.param("not a slug", id="spaces"),
            pytest.param("vendor:extra", id="colon"),
            pytest.param("-leading", id="leading-dash"),
        ],
    )
    def test_an_unusable_vendor_name_publishes_nothing(self, offered: str) -> None:
        """Half this value comes from the environment, so it is not echoed raw.

        A colon above all: the published value is colon-separated and a
        consumer splits it, so a vendor name carrying one would silently
        change the shape the consumer parses.
        """
        assert stage_inference_source("pool", {POOL_VENDOR_ENV: offered}) is None

    @pytest.mark.parametrize(
        "provider",
        [pytest.param("ollama", id="ollama"), pytest.param("enclave", id="enclave")],
    )
    def test_a_provider_inside_the_vaults_boundary_is_local(
        self, provider: str
    ) -> None:
        """``local`` is about custody, not about the process the model runs in."""
        assert stage_inference_source(provider, {}) == INFERENCE_SOURCE_LOCAL

    @pytest.mark.parametrize(
        "provider",
        [
            pytest.param("anthropic", id="anthropic"),
            pytest.param("openai", id="openai"),
            pytest.param("gemini", id="gemini"),
        ],
    )
    def test_an_operator_cloud_key_is_left_unlabelled(self, provider: str) -> None:
        """None of the four values names it, so the field is omitted.

        **Do not "fix" this into a label.** The consumer's whole use for
        ``inference_source`` is to notice a route it did not expect, so a
        value the server cannot stand behind is worse than an absence. If this
        route needs a name, it needs a contract minor that publishes one.
        """
        assert stage_inference_source(provider, {}) is None


class TestRequestKeyLabelling:
    """A writer's own key names its vendor, and only a usable one."""

    def test_an_admitted_key_names_its_vendor(self) -> None:
        """``request_key:<vendor>``, built from the key's own provider."""
        key = RequestKey("anthropic", _KEY)
        assert request_key_inference_source(key) == request_key_source("anthropic")
        assert request_key_inference_source(key) == "request_key:anthropic"

    def test_a_provider_the_vocabulary_cannot_carry_publishes_nothing(self) -> None:
        """Fail closed here too; the provider string reaches this from a header."""
        key = RequestKey("Not A Vendor", _KEY)
        assert request_key_inference_source(key) is None


class TestRecorder:
    """The recorder is deliberately overwritable, and starts empty."""

    def test_a_fresh_recorder_names_no_route(self) -> None:
        """An answer nobody recorded a route for is unlabelled, not mislabelled."""
        assert InferenceProvenance().source is None

    def test_a_second_resolution_replaces_the_first(self) -> None:
        """Two model calls in one request must not leave the first's label.

        ``None`` over a value is the case that matters: a request whose second
        resolution is an unlabelled route must not keep reporting the first's.
        """
        recorder = InferenceProvenance()
        recorder.record(INFERENCE_SOURCE_POOL_CREEK)
        recorder.record(None)
        assert recorder.source is None


class TestClassificationPassLabelling:
    """One pass, one configured stage, one label — or none for ``rules``."""

    @staticmethod
    def _vault(tmp_path: Path, provider: str) -> Path:
        """Scaffold a vault whose ``classification`` stage is *provider*."""
        vault = tmp_path / "vault"
        (vault / "01-Fragments").mkdir(parents=True)
        (vault / "00-Creek-Meta").mkdir(parents=True)
        data: dict[str, Any] = CreekConfig().model_dump(mode="json")
        data["vault_path"] = str(vault)
        data["llm"]["classification"] = {
            **data["llm"]["default"],
            "provider": provider,
        }
        vault.joinpath(*_CONFIG_SUBPATH).write_text(
            yaml.dump(data, sort_keys=False), encoding="utf-8"
        )
        return vault

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            pytest.param("pool", INFERENCE_SOURCE_POOL_CREEK, id="pool"),
            pytest.param("ollama", INFERENCE_SOURCE_LOCAL, id="ollama"),
            pytest.param("anthropic", None, id="operator-cloud"),
        ],
    )
    def test_an_llm_pass_is_labelled_by_its_configured_stage(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
        expected: str | None,
    ) -> None:
        """The route the pass was admitted on, read off the vault's own config."""
        monkeypatch.delenv(POOL_VENDOR_ENV, raising=False)
        monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")
        config = load_vault_config(self._vault(tmp_path, provider))

        source = classify_module._pass_inference_source(config, "llm", None, None)

        assert source == expected

    def test_a_rules_pass_is_unlabelled_whatever_the_stage_says(
        self, tmp_path: Path
    ) -> None:
        """No model ran, so there is no route — the stage is irrelevant."""
        config = load_vault_config(self._vault(tmp_path, "pool"))

        assert (
            classify_module._pass_inference_source(config, "rules", None, None) is None
        )


class TestMinorGateEdges:
    """The two fail-closed edges of the field-level minor gate.

    Both are reachable only in principle from ``/v1`` — the version gate
    refuses a caller declaring nothing or declaring a minor this server does
    not serve, before any route runs — and both are pinned here anyway,
    because the gate's job is to be correct *without* relying on that
    ordering. A reordering of the middleware stack must not turn a withheld
    field into a served one.
    """

    @staticmethod
    def _request(headers: dict[str, str]) -> Request:
        """Return a bare ASGI request carrying *headers* and nothing else."""
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/reflections",
                "headers": [
                    (name.lower().encode(), value.encode())
                    for name, value in headers.items()
                ],
            }
        )

    def test_a_caller_declaring_no_minor_is_told_nothing(self) -> None:
        """No header is not "the newest client"; it is the oldest."""
        assert not declared_minor_at_least(self._request({}), PROVENANCE_SINCE_MINOR)

    def test_an_unparseable_minor_is_below_every_requirement(self) -> None:
        """``minor_at_least`` fails closed on junk, and this inherits that."""
        request = self._request({CONTRACT_VERSION_HEADER: "zero point nine"})
        assert not declared_minor_at_least(request, PROVENANCE_SINCE_MINOR)

    def test_the_current_minor_is_told(self) -> None:
        """The positive control, so the two refusals above are not vacuous."""
        request = self._request({CONTRACT_VERSION_HEADER: PROVENANCE_SINCE_MINOR})
        assert declared_minor_at_least(request, PROVENANCE_SINCE_MINOR)


class TestRequestBudget:
    """The deadline a handler bounds an out-of-process call by."""

    def test_a_context_with_no_deadline_layer_bounds_nothing(self) -> None:
        """``None`` rather than zero: no deadline is not an expired one."""
        assert RequestContext(request_id="r").remaining() is None

    def test_a_recorded_deadline_is_reported_as_the_time_left(self) -> None:
        """An instant in, a duration out, measured from now."""
        context = RequestContext(request_id="r", deadline=monotonic() + 5.0)
        remaining = context.remaining()
        assert remaining is not None
        assert 0 < remaining <= 5.0

    def test_a_spent_deadline_reports_a_non_positive_budget(self) -> None:
        """Which is what the pool boundary turns into a refusal to dial."""
        context = RequestContext(request_id="r", deadline=monotonic() - 1.0)
        remaining = context.remaining()
        assert remaining is not None
        assert remaining <= 0


class TestFragmentTierDeclaration:
    """A classification pass declares each fragment's own tier, or none.

    The pool contract's request shape carries the declared tier, so a
    pool-routed classification stage would otherwise refuse at the dial.
    :class:`creek.classify.classify_engine._PreparedFragment` carries
    ``pool_wire_tier(tier_of(fragment))`` and the classify call runs inside
    that declaration — per fragment, so the tier declared is the fragment's
    true one rather than the stricter of the two a shared classifier serves.
    """

    @pytest.mark.parametrize(
        "tier",
        list(PrivacyTier),
        ids=[tier.value for tier in PrivacyTier],
    )
    def test_every_privacy_tier_either_has_one_wire_spelling_or_none(
        self, tier: PrivacyTier
    ) -> None:
        """``intimate`` and ``unclassified`` have none, and that is the point.

        A fragment the pool contract cannot name leaves the declaration empty,
        so a dial from its classifier refuses rather than carrying a guess.
        The chokepoint has already routed such a fragment locally; this is the
        defence-in-depth layer.
        """
        spelled = pool_wire_tier(tier)
        if tier in {PrivacyTier.OPEN, PrivacyTier.PERSONAL}:
            assert spelled == tier.value
        else:
            assert spelled is None

    def test_a_prepared_fragment_declares_nothing_by_default(self) -> None:
        """The default is the fail-closed one, not ``open``.

        A rules pass builds no classifier and declares no tier, and a future
        caller that forgets the keyword inherits "declare nothing" rather than
        "declare the most permissive tier".
        """
        assert _PreparedFragment.__dataclass_fields__["pool_tier"].default is None
