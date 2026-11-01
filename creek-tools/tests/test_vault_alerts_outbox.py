"""The vault-side owed-alert outbox: closed, counts-only, durable (#2049)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm import pool_attested
from creek.vault_alerts import (
    OUTBOX_RELPATH,
    SCHEMA,
    VAULT_ALERT_KINDS,
    Acknowledgement,
    OutboxUnreadableError,
    VaultAlertDocumentError,
    VaultAlertOutbox,
    VaultAlertState,
    active_vault_alert_outbox,
    installed_vault_alert_outbox,
    owe_vault_alert,
    parse_acknowledgement,
    parse_state,
    parse_summary,
)
from creek_mcp.provisioning.budget import AlertKind

if TYPE_CHECKING:
    from pathlib import Path

_EPOCH = "e" * 32
_OTHER_EPOCH = "f" * 32
_SPEND = "monthly_budget_departure"
_ATTESTATION = "pool_attestation_failed"


def _outbox(vault: Path) -> VaultAlertOutbox:
    """Return an outbox over *vault* with a fixed epoch."""
    return VaultAlertOutbox(vault, epoch_factory=lambda: _EPOCH)


def test_vault_kinds_are_a_closed_subset_of_the_fleet_vocabulary() -> None:
    """Only kinds adepthood's receiver already admits can be owed."""
    assert {kind.value for kind in AlertKind} >= VAULT_ALERT_KINDS
    assert set(pool_attested.OUTCOME_ALERTS.values()) <= VAULT_ALERT_KINDS
    assert _SPEND in VAULT_ALERT_KINDS


def test_an_empty_vault_owes_nothing_and_writes_nothing(tmp_path: Path) -> None:
    """Reading a vault that never raised an alert creates no file."""
    assert _outbox(tmp_path).summary() is None
    assert not (tmp_path / OUTBOX_RELPATH).exists()


def test_owed_alerts_survive_a_restart_until_acknowledged(tmp_path: Path) -> None:
    """A second outbox over the same volume sees what the first recorded."""
    _outbox(tmp_path).owe(_SPEND)
    _outbox(tmp_path).owe(_ATTESTATION)
    _outbox(tmp_path).owe(_ATTESTATION)

    restarted = VaultAlertOutbox(tmp_path, epoch_factory=lambda: _OTHER_EPOCH)
    state = restarted.summary()

    assert state is not None
    assert state.epoch == _EPOCH
    assert state.owed == {_ATTESTATION: 2, _SPEND: 1}


def test_acknowledgement_is_monotonic_clamped_and_idempotent(tmp_path: Path) -> None:
    """Repeating, lowering or overshooting a mark never un-owes or over-owes."""
    outbox = _outbox(tmp_path)
    for _ in range(3):
        outbox.owe(_ATTESTATION)

    first = outbox.acknowledge(Acknowledgement(_EPOCH, {_ATTESTATION: 2}))
    again = outbox.acknowledge(Acknowledgement(_EPOCH, {_ATTESTATION: 2}))
    lower = outbox.acknowledge(Acknowledgement(_EPOCH, {_ATTESTATION: 1}))
    over = outbox.acknowledge(Acknowledgement(_EPOCH, {_ATTESTATION: 99}))

    assert first is not None
    assert first.owed == {_ATTESTATION: 1}
    assert again == first
    assert lower == first
    assert over is not None
    assert over.owed == {}
    assert over.acknowledged == {_ATTESTATION: 3}


def test_an_acknowledgement_for_another_epoch_changes_nothing(tmp_path: Path) -> None:
    """A late ack for an outbox this volume no longer holds is ignored."""
    outbox = _outbox(tmp_path)
    outbox.owe(_SPEND)

    after = outbox.acknowledge(Acknowledgement(_OTHER_EPOCH, {_SPEND: 1}))

    assert after is not None
    assert after.owed == {_SPEND: 1}
    assert outbox.acknowledge(Acknowledgement(_EPOCH, {_SPEND: 1})) is not None
    assert (
        _outbox(tmp_path / "empty").acknowledge(Acknowledgement(_EPOCH, {_SPEND: 1}))
        is None
    )


def test_the_stored_document_is_counts_only(tmp_path: Path) -> None:
    """The file on the volume holds the schema, an epoch and counts: nothing else."""
    _outbox(tmp_path).owe(_SPEND)

    document = json.loads((tmp_path / OUTBOX_RELPATH).read_text(encoding="utf-8"))

    assert document == {
        "schema": SCHEMA,
        "epoch": _EPOCH,
        "raised": {_SPEND: 1},
        "acknowledged": {},
    }


def test_an_unknown_kind_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """Only the closed set can be owed, installed or not."""
    with pytest.raises(ValueError, match="closed set"):
        _outbox(tmp_path).owe("free text about a person")
    with pytest.raises(ValueError, match="closed set"):
        owe_vault_alert("duplicate_resource")
    assert not (tmp_path / OUTBOX_RELPATH).exists()


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        json.dumps({"schema": SCHEMA, "epoch": _EPOCH, "raised": {}}),
        json.dumps(
            {
                "schema": SCHEMA,
                "epoch": _EPOCH,
                "raised": {},
                "acknowledged": {_SPEND: 1},
            }
        ),
    ],
)
def test_an_unreadable_outbox_is_reported_never_reset(
    tmp_path: Path, stored: str
) -> None:
    """A damaged document is an error, not a silent restart at zero."""
    path = tmp_path / OUTBOX_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text(stored, encoding="utf-8")

    with pytest.raises(OutboxUnreadableError):
        _outbox(tmp_path).summary()
    with pytest.raises(OutboxUnreadableError):
        _outbox(tmp_path).owe(_SPEND)
    assert path.read_text(encoding="utf-8") == stored


def test_counts_saturate_at_the_closed_maximum(tmp_path: Path) -> None:
    """A runaway raiser cannot grow the document past its bound."""
    path = tmp_path / OUTBOX_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "epoch": _EPOCH,
                "raised": {_SPEND: 1_000_000_000},
                "acknowledged": {},
            }
        ),
        encoding="utf-8",
    )

    assert _outbox(tmp_path).owe(_SPEND).raised == {_SPEND: 1_000_000_000}


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {"schema": "other", "epoch": _EPOCH, "raised": {}, "acknowledged": {}},
        {"schema": SCHEMA, "epoch": "ABC", "raised": {}, "acknowledged": {}},
        {"schema": SCHEMA, "epoch": _EPOCH, "raised": [], "acknowledged": {}},
        {"schema": SCHEMA, "epoch": _EPOCH, "raised": {"x": 1}, "acknowledged": {}},
        {"schema": SCHEMA, "epoch": _EPOCH, "raised": {_SPEND: 0}, "acknowledged": {}},
        {
            "schema": SCHEMA,
            "epoch": _EPOCH,
            "raised": {_SPEND: True},
            "acknowledged": {},
        },
        {
            "schema": SCHEMA,
            "epoch": _EPOCH,
            "raised": {_SPEND: 1.0},
            "acknowledged": {},
        },
        {
            "schema": SCHEMA,
            "epoch": _EPOCH,
            "raised": {_SPEND: 1},
            "acknowledged": {},
            "note": "free text",
        },
    ],
)
def test_the_state_parser_refuses_anything_outside_the_closed_shape(
    document: object,
) -> None:
    """Every field is closed: schema, epoch, kinds, positive integer counts."""
    with pytest.raises(VaultAlertDocumentError):
        parse_state(document)


def test_the_summary_parser_reads_empty_and_full_documents() -> None:
    """The bare schema means "never raised"; anything else must be a state."""
    state = VaultAlertState(_EPOCH, {_SPEND: 2}, {_SPEND: 1})

    assert parse_summary({"schema": SCHEMA}) is None
    assert parse_summary(state.to_document()) == state
    with pytest.raises(VaultAlertDocumentError):
        parse_summary({"schema": SCHEMA, "epoch": _EPOCH})


@pytest.mark.parametrize(
    "document",
    [
        {"schema": SCHEMA, "epoch": _EPOCH, "through": {}},
        {"schema": SCHEMA, "epoch": _EPOCH},
        {"schema": SCHEMA, "epoch": _EPOCH, "through": {_SPEND: -1}},
        {"schema": SCHEMA, "epoch": "x", "through": {_SPEND: 1}},
        {"schema": SCHEMA, "epoch": _EPOCH, "through": {_SPEND: 1}, "extra": 1},
        "ack",
    ],
)
def test_the_acknowledgement_parser_is_closed(document: object) -> None:
    """An acknowledgement carries an epoch and positive closed marks only."""
    with pytest.raises(VaultAlertDocumentError):
        parse_acknowledgement(document)


def test_an_acknowledgement_round_trips() -> None:
    """What the control plane writes is exactly what the vault parses."""
    acknowledgement = Acknowledgement(_EPOCH, {_SPEND: 1, _ATTESTATION: 2})

    assert parse_acknowledgement(acknowledgement.to_document()) == acknowledgement


def test_owing_without_an_installed_outbox_does_nothing(tmp_path: Path) -> None:
    """Outside a managed runtime an alert stays in the content-free log only."""
    assert active_vault_alert_outbox() is None
    owe_vault_alert(_SPEND)
    assert not any(tmp_path.iterdir())


def test_the_installed_outbox_is_restored_after_the_block(tmp_path: Path) -> None:
    """Nested installs restore the previous outbox, then none."""
    with installed_vault_alert_outbox(tmp_path / "outer") as outer:
        with installed_vault_alert_outbox(tmp_path / "inner") as inner:
            assert active_vault_alert_outbox() is inner
        assert active_vault_alert_outbox() is outer
    assert active_vault_alert_outbox() is None


def test_a_storage_failure_never_breaks_the_raiser(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A volume that cannot be written is logged content-free and swallowed."""
    blocker = tmp_path / "00-Creek-Meta"
    blocker.write_text("not a directory", encoding="utf-8")

    with (
        caplog.at_level(logging.WARNING, logger="creek.vault_alerts"),
        installed_vault_alert_outbox(tmp_path),
    ):
        owe_vault_alert(_SPEND)

    assert "vault alert could not be recorded kind=monthly_budget_departure" in (
        caplog.text
    )


def test_a_pool_outcome_alert_is_owed_in_the_installed_outbox(
    tmp_path: Path,
) -> None:
    """``record_pool_outcome`` owes exactly the alert its record names."""
    with installed_vault_alert_outbox(tmp_path) as outbox:
        pool_attested.record_pool_outcome(
            outcome="attestation_refused", request_id="r" * 32, started=0.0
        )
        pool_attested.record_pool_outcome(
            outcome="ok", request_id="r" * 32, started=0.0
        )

    state = outbox.summary()
    assert state is not None
    assert state.owed == {_ATTESTATION: 1}
