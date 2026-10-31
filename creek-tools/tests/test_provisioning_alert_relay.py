"""Alert-channel failure keeps routing and closes admission (R2-4, #1999)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from creek_mcp.provisioning.alert_relay import AdmissionGate, AlertRelay
from creek_mcp.provisioning.pilot_alerts import AlertDeliveryError
from creek_mcp.provisioning.store import ProvisioningStore

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_INTERVAL = 300.0
_START = datetime(2026, 10, 31, 12, tzinfo=UTC)
_ALERT_URL = "https://adepthood.example/internal/vault-provisioning/alerts"
_BEARER = "synthetic-mounted-bearer"


class _Sink:
    """A synthetic alert channel: never sends anything anywhere."""

    def __init__(self) -> None:
        self.failing = True
        self.attempts: list[tuple[str, ...]] = []
        self.delivered: list[tuple[str, ...]] = []

    def deliver(self, kinds: Iterable[str]) -> None:
        batch = tuple(kinds)
        self.attempts.append(batch)
        if self.failing:
            raise AlertDeliveryError("fleet alert delivery failed")
        self.delivered.append(batch)


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _relay(
    store: ProvisioningStore,
    gate: AdmissionGate,
    sink: _Sink,
    clock: _Clock,
) -> AlertRelay:
    return AlertRelay(
        store,
        gate,
        sink.deliver,
        backoff_seconds=_INTERVAL,
        clock=clock,
        utc_clock=lambda: _START + timedelta(seconds=clock.now),
    )


@pytest.fixture
def store(tmp_path: Path) -> ProvisioningStore:
    """Return a real durable store; the relay's second channel is SQLite."""
    return ProvisioningStore(tmp_path / "provisioning.sqlite3")


def test_admission_gate_opens_closes_and_reports_as_a_callable() -> None:
    """The control plane consults the gate as a plain ``() -> bool``."""
    gate = AdmissionGate()

    assert gate.is_open is True
    assert gate() is True
    gate.close()
    assert gate.is_open is False
    assert gate() is False
    gate.open()
    assert gate() is True


def test_failed_delivery_is_contained_and_closes_admission(
    store: ProvisioningStore,
) -> None:
    """A sink failure never escapes the relay; it records a gap and refuses."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    relay = _relay(store, gate, sink, clock)

    relay.collect(("orphan_resource", "orphan_resource"))
    relay.flush()

    assert sink.attempts == [("orphan_resource", "orphan_resource")]
    assert gate.is_open is False
    assert store.alert_delivery_failures() == 1
    gap = store.open_alert_delivery_gap()
    assert gap is not None
    assert gap.opened_at == _START
    assert gap.failed_deliveries == 1
    assert dict(gap.pending) == {"orphan_resource": 2}


def test_undelivered_kinds_are_retried_with_capped_doubling_backoff(
    store: ProvisioningStore,
) -> None:
    """Retries wait one, two, then at most four intervals between attempts."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    relay = _relay(store, gate, sink, clock)
    relay.collect(("stuck_deletion",))
    relay.flush()

    attempt_times = [0.0]
    for _ in range(40):
        clock.now += _INTERVAL / 2
        before = len(sink.attempts)
        relay.flush()
        if len(sink.attempts) > before:
            attempt_times.append(clock.now)
        if len(attempt_times) == 5:
            break

    gaps = [later - earlier for earlier, later in pairwise(attempt_times)]
    assert gaps == [_INTERVAL, 2 * _INTERVAL, 4 * _INTERVAL, 4 * _INTERVAL]
    assert all(batch == ("stuck_deletion",) for batch in sink.attempts)
    assert store.alert_delivery_failures() == 5


def test_new_kinds_fold_into_pending_without_unbounded_growth(
    store: ProvisioningStore,
) -> None:
    """A recurring alert re-sends its peak count, never a running sum."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    relay = _relay(store, gate, sink, clock)

    for kinds in (
        ("orphan_resource",),
        ("orphan_resource", "orphan_resource"),
        ("orphan_resource", "stuck_deletion"),
    ):
        relay.collect(kinds)
        relay.flush()
        clock.now += 4 * _INTERVAL

    assert sink.attempts[-1] == (
        "orphan_resource",
        "orphan_resource",
        "stuck_deletion",
    )
    gap = store.open_alert_delivery_gap()
    assert gap is not None
    assert dict(gap.pending) == {"orphan_resource": 2, "stuck_deletion": 1}


def test_kinds_collected_during_backoff_wait_for_the_next_due_attempt(
    store: ProvisioningStore,
) -> None:
    """A pass inside the backoff window keeps new kinds without sending them."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    relay = _relay(store, gate, sink, clock)
    relay.collect(("orphan_resource",))
    relay.flush()

    clock.now += _INTERVAL / 2
    relay.collect(("stuck_deletion",))
    relay.flush()
    assert len(sink.attempts) == 1

    sink.failing = False
    clock.now += _INTERVAL
    relay.flush()

    assert sink.delivered == [("orphan_resource", "stuck_deletion")]


def test_recovery_delivers_the_earlier_kinds_and_reopens_admission(
    store: ProvisioningStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first successful delivery closes the gap and reopens admission."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    relay = _relay(store, gate, sink, clock)
    relay.collect(("monthly_budget_departure",))
    relay.flush()
    clock.now += _INTERVAL
    relay.flush()
    assert store.alert_delivery_failures() == 2

    sink.failing = False
    clock.now += 2 * _INTERVAL
    with caplog.at_level(logging.INFO, logger="creek_mcp.provisioning.alert_relay"):
        relay.flush()

    assert sink.delivered == [("monthly_budget_departure",)]
    assert gate.is_open is True
    assert store.open_alert_delivery_gap() is None
    assert store.alert_delivery_failures() == 2
    assert any("recovered" in record.getMessage() for record in caplog.records)

    relay.flush()
    assert len(sink.attempts) == 3


def test_a_clean_pass_with_nothing_pending_sends_nothing(
    store: ProvisioningStore,
) -> None:
    """No alert means no delivery attempt and no change to admission."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()

    _relay(store, gate, sink, clock).flush()

    assert sink.attempts == []
    assert gate.is_open is True
    assert store.alert_delivery_failures() == 0


def test_an_alert_delivered_first_time_leaves_admission_open(
    store: ProvisioningStore,
) -> None:
    """A healthy channel never touches the gap record."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    sink.failing = False
    relay = _relay(store, gate, sink, clock)

    relay.collect(("continuous_running",))
    relay.flush()

    assert sink.delivered == [("continuous_running",)]
    assert gate.is_open is True
    assert store.open_alert_delivery_gap() is None


def test_each_failed_delivery_logs_one_content_free_error_line(
    store: ProvisioningStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log channel carries counts only: no URL, bearer, body, or kind."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    relay = _relay(store, gate, sink, clock)
    relay.collect(("orphan_resource", "stuck_deletion"))

    with caplog.at_level(logging.ERROR, logger="creek_mcp.provisioning.alert_relay"):
        relay.flush()

    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(records) == 1
    record = records[0]
    assert record.args == (1, 2, int(_INTERVAL))
    message = record.getMessage()
    assert "new activations refused" in message
    for forbidden in (_ALERT_URL, "adepthood.example", _BEARER, "Bearer"):
        assert forbidden not in message
    for kind in ("orphan_resource", "stuck_deletion"):
        assert kind not in message
    assert record.exc_info is None


def test_an_open_gap_survives_restart_and_keeps_admission_closed(
    store: ProvisioningStore,
) -> None:
    """A restarted pilot reloads the gap: admission stays shut until delivery."""
    first_gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    first = _relay(store, first_gate, sink, clock)
    first.collect(("duplicate_resource",))
    first.flush()

    restarted_gate = AdmissionGate()
    restarted = _relay(store, restarted_gate, sink, clock)
    assert restarted_gate.is_open is False

    sink.failing = False
    restarted.flush()

    assert sink.delivered == [("duplicate_resource",)]
    assert restarted_gate.is_open is True
    assert store.open_alert_delivery_gap() is None


def test_gaps_are_recorded_separately_and_failures_accumulate(
    store: ProvisioningStore,
) -> None:
    """A second outage opens a fresh gap; the counter is lifetime-durable."""
    gate, sink, clock = AdmissionGate(), _Sink(), _Clock()
    relay = _relay(store, gate, sink, clock)
    for failing in (True, False, True):
        sink.failing = failing
        relay.collect(("orphan_resource",))
        relay.flush()
        clock.now += 4 * _INTERVAL

    gap = store.open_alert_delivery_gap()
    assert gap is not None
    assert gap.opened_at == _START + timedelta(seconds=8 * _INTERVAL)
    assert gap.failed_deliveries == 1
    assert store.alert_delivery_failures() == 2


@pytest.mark.parametrize("error", [ValueError("bad kind"), RuntimeError("other")])
def test_only_alert_delivery_errors_are_contained(
    store: ProvisioningStore,
    error: Exception,
) -> None:
    """Every other sink failure keeps today's fail-stop behavior."""
    gate = AdmissionGate()

    def broken(_kinds: Iterable[str]) -> None:
        raise error

    relay = AlertRelay(store, gate, broken, backoff_seconds=_INTERVAL)
    relay.collect(("orphan_resource",))

    with pytest.raises(type(error)):
        relay.flush()
    assert gate.is_open is True
    assert store.alert_delivery_failures() == 0


def test_relay_refuses_unknown_kinds_and_nonpositive_backoff(
    store: ProvisioningStore,
) -> None:
    """Only the closed alert vocabulary is ever retained durably."""
    sink = _Sink()
    relay = AlertRelay(store, AdmissionGate(), sink.deliver, backoff_seconds=1)

    with pytest.raises(ValueError, match="kind"):
        relay.collect(("requester-subject-1",))
    for backoff in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="backoff"):
            AlertRelay(store, AdmissionGate(), sink.deliver, backoff_seconds=backoff)
