"""The classify, link and upload tools record what ran, for the status route (#2124)."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest

from creek.audit.stage_log import PassStage, StageOutcome, read_stage_runs
from creek.ingest.upload_receipts import read_upload_receipts
from creek.vault.mutations import ContentMutationBusyError
from creek_mcp.stage_recording import pass_outcome
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import classify as classify_mod
from creek_mcp.tools import link as link_mod
from creek_mcp.tools.classify import classify_tool
from creek_mcp.tools.link import link_tool
from creek_mcp.tools.upload import upload_tool

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a minimal vault: the meta marker and an empty fragments tree."""
    for relparts in (("00-Creek-Meta", "State"), ("01-Fragments", "Notes")):
        tmp_path.joinpath(*relparts).mkdir(parents=True)
    return tmp_path


def _raise_busy(**_kwargs: object) -> Any:
    """Stand in for an engine whose content lock is held past its bound."""
    raise ContentMutationBusyError


def test_a_classify_pass_records_classified_and_praxis_scanned(vault: Path) -> None:
    """One rules pass appends both stages, ok, with ordered instants."""
    result = classify_tool(vault_path=vault, method="rules")

    runs = read_stage_runs(vault)

    assert result["status"] == "ok"
    assert [(run.stage, run.outcome) for run in runs] == [
        (PassStage.CLASSIFIED, StageOutcome.OK),
        (PassStage.PRAXIS_SCANNED, StageOutcome.OK),
    ]
    assert all(run.started_at <= run.finished_at for run in runs)


def test_a_classify_pass_with_errors_records_partial(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that left work for a retry is partial, not ok."""
    real = classify_mod.run_classify

    def _with_error(**kwargs: Any) -> Any:
        summary = real(**kwargs)
        return type(summary)(**{**summary.__dict__, "errors": ("boom",)})

    monkeypatch.setattr(classify_mod, "run_classify", _with_error)

    classify_tool(vault_path=vault, method="rules")

    assert {run.outcome for run in read_stage_runs(vault)} == {StageOutcome.PARTIAL}


def test_a_refused_classify_pass_records_failed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy refusal is a failed attempt, so "silently failed" is visible."""
    monkeypatch.setattr(classify_mod, "run_classify", _raise_busy)

    result = classify_tool(vault_path=vault, method="rules")

    assert result["status"] == "refused"
    assert {run.outcome for run in read_stage_runs(vault)} == {StageOutcome.FAILED}


def test_an_unknown_classify_method_records_nothing(vault: Path) -> None:
    """A method that names no pass is not a stage that ran."""
    classify_tool(vault_path=vault, method="bogus")

    assert read_stage_runs(vault) == []


def test_a_link_pass_records_its_method_stage(vault: Path) -> None:
    """Each linker method records ``linked:<method>``."""
    result = link_tool(vault_path=vault, method="temporal")

    assert result["status"] == "ok"
    assert [(r.stage, r.outcome) for r in read_stage_runs(vault)] == [
        (PassStage.LINKED_TEMPORAL, StageOutcome.OK)
    ]


def test_a_link_pass_that_discarded_fragments_records_partial(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discarded fragments are data loss, so the pass is partial."""
    real = link_mod.run_link

    def _discarding(**kwargs: Any) -> Any:
        summary = real(**kwargs)
        return type(summary)(**{**summary.__dict__, "oversized_discarded": 2})

    monkeypatch.setattr(link_mod, "run_link", _discarding)

    link_tool(vault_path=vault, method="temporal")

    assert [r.outcome for r in read_stage_runs(vault)] == [StageOutcome.PARTIAL]


def test_a_link_pass_that_skipped_a_page_records_partial(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detected page that never reached disk is left for a retry (#2125)."""
    real = link_mod.run_link

    def _skipping(**kwargs: Any) -> Any:
        summary = real(**kwargs)
        return type(summary)(**{**summary.__dict__, "skipped_materialisation": 1})

    monkeypatch.setattr(link_mod, "run_link", _skipping)

    result = link_tool(vault_path=vault, method="temporal")

    assert result["skipped_materialisation"] == 1
    assert [r.outcome for r in read_stage_runs(vault)] == [StageOutcome.PARTIAL]


def test_a_refused_link_pass_records_failed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy link refusal is recorded as a failed attempt."""
    monkeypatch.setattr(link_mod, "run_link", _raise_busy)

    link_tool(vault_path=vault, method="eddies")

    assert [(r.stage, r.outcome) for r in read_stage_runs(vault)] == [
        (PassStage.LINKED_EDDIES, StageOutcome.FAILED)
    ]


def test_an_unknown_link_method_records_nothing(vault: Path) -> None:
    """A refused unknown method is not a pass."""
    link_tool(vault_path=vault, method="bogus")

    assert read_stage_runs(vault) == []


def _upload(vault: Path, *, external_id: str, text: str) -> dict[str, Any]:
    """Upload *text* as a ``.md`` document at tier open."""
    return upload_tool(
        vault_path=vault,
        filename="on-attention.md",
        content_base64=base64.b64encode(text.encode()).decode(),
        external_id=external_id,
        tier="open",
        privacy_tier_ceiling=TierCeiling.ALL,
        consumer="adepthood",
    )


def test_an_upload_records_a_receipt_per_fragment(vault: Path) -> None:
    """A landed upload names its caller, document and fragments."""
    result = _upload(vault, external_id="up-1", text="# On attention\n\nBody.\n")

    receipts = read_upload_receipts(vault)

    assert result["status"] == "ok"
    assert [r.fragment_id for r in receipts] == result["affected_fragment_ids"]
    assert {(r.consumer, r.external_id, r.filename, r.tier) for r in receipts} == {
        ("adepthood", "up-1", "on-attention.md", "open")
    }


def test_an_unchanged_resend_records_no_new_receipt(vault: Path) -> None:
    """Re-sending identical bytes landed nothing new, so the instant stands."""
    _upload(vault, external_id="up-1", text="# On attention\n\nBody.\n")
    again = _upload(vault, external_id="up-1", text="# On attention\n\nBody.\n")

    assert again["action"] == "unchanged"
    assert len(read_upload_receipts(vault)) == 1


def test_a_refused_upload_records_no_receipt(vault: Path) -> None:
    """Nothing landed, so nothing is receipted."""
    result = upload_tool(
        vault_path=vault,
        filename="on-attention.md",
        content_base64="not base64!",
        external_id="up-1",
        tier="open",
        privacy_tier_ceiling=TierCeiling.ALL,
        consumer="adepthood",
    )

    assert result["status"] == "refused"
    assert read_upload_receipts(vault) == []


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "ok", "errors": []}, StageOutcome.OK),
        ({"status": "ok", "errors": ["boom"]}, StageOutcome.PARTIAL),
        ({"status": "ok", "llm_call_failed": 2}, StageOutcome.PARTIAL),
        ({"status": "ok", "oversized_discarded": 1}, StageOutcome.PARTIAL),
        ({"status": "ok", "skipped_materialisation": 1}, StageOutcome.PARTIAL),
        ({"status": "ok", "skipped_materialisation": 0}, StageOutcome.OK),
        (
            {"status": "refused", "reason": "vault content mutation busy"},
            StageOutcome.FAILED,
        ),
        ({"status": "refused"}, StageOutcome.FAILED),
        ({"status": "refused", "reason": "unknown method 'x'; supported: a"}, None),
        ({"status": "refused", "reason": "linking unavailable: Boom"}, None),
    ],
)
def test_the_outcome_is_read_off_the_tool_result(
    result: dict[str, Any], expected: StageOutcome | None
) -> None:
    """What the caller was told is what the log records."""
    assert pass_outcome(result, ("linking unavailable: ",)) is expected


def test_an_unloadable_config_refusal_records_nothing(vault: Path) -> None:
    """A refusal answered before the vault is read leaves no trace."""
    config = vault / "00-Creek-Meta" / "creek_config.yaml"
    config.write_text("llm: [unclosed\n", encoding="utf-8")

    result = link_tool(vault_path=vault, method="temporal")

    assert result["status"] == "refused"
    assert read_stage_runs(vault) == []
