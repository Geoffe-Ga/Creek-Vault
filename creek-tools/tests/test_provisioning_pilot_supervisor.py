"""Single-Machine lifecycle contract for the managed Fly pilot."""

from __future__ import annotations

from threading import Event
from typing import TYPE_CHECKING

import pytest

from creek_mcp.provisioning.pilot_supervisor import (
    PilotSupervisor,
    SupervisedComponent,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _waiter(started: Event) -> Callable[[Event], None]:
    def run(stop: Event) -> None:
        started.set()
        stop.wait()

    return run


def test_supervisor_is_healthy_only_while_every_component_is_running() -> None:
    """API, worker, routing, and scheduling share one visible failure domain."""
    started = {name: Event() for name in ("edge", "worker", "scheduler")}
    supervisor = PilotSupervisor(
        [SupervisedComponent(name, _waiter(started[name])) for name in started]
    )

    supervisor.start()
    assert all(event.wait(1) for event in started.values())
    assert supervisor.healthy is True

    supervisor.stop(timeout=1)
    assert supervisor.healthy is False
    assert supervisor.failed is False


def test_early_component_exit_marks_unhealthy_and_stops_its_peers() -> None:
    """A silently dead worker cannot leave the public edge reporting healthy."""
    peer_stopped = Event()

    def exit_early(_stop: Event) -> None:
        return

    def peer(stop: Event) -> None:
        stop.wait()
        peer_stopped.set()

    supervisor = PilotSupervisor(
        [
            SupervisedComponent("worker", exit_early),
            SupervisedComponent("edge", peer),
        ]
    )

    supervisor.start()

    assert peer_stopped.wait(1)
    assert supervisor.wait(1)
    assert supervisor.failed is True
    assert supervisor.healthy is False


def test_supervisor_stays_unhealthy_until_each_component_reports_ready() -> None:
    """A live thread cannot hide an unfinished first fleet report."""
    started = Event()
    ready = Event()
    supervisor = PilotSupervisor(
        [SupervisedComponent("scheduler", _waiter(started), ready=ready.is_set)]
    )

    supervisor.start()
    assert started.wait(1)
    assert supervisor.healthy is False

    ready.set()
    assert supervisor.healthy is True
    supervisor.stop(timeout=1)


def test_component_exception_is_contained_without_secret_payload(caplog) -> None:
    """The supervisor logs only the component name, never its input arguments."""
    payload_canary = "provider-private-input-canary"

    def fail(_stop: Event) -> None:
        raise RuntimeError(payload_canary)

    supervisor = PilotSupervisor([SupervisedComponent("worker", fail)])
    with caplog.at_level("ERROR"):
        supervisor.start()
        assert supervisor.wait(1)

    assert supervisor.failed is True
    assert payload_canary not in caplog.text


@pytest.mark.parametrize("name", ["", "has space", "bad/slash", "bad;field"])
def test_component_names_are_closed_shape(name: str) -> None:
    """Operator-controlled names cannot inject structured lifecycle logs."""
    with pytest.raises(ValueError, match="name"):
        SupervisedComponent(name, lambda _stop: None)
