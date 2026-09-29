"""Fail-closed lifecycle supervision for the single-Machine Fly pilot."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_LOGGER = logging.getLogger(__name__)
_DEFAULT_STOP_TIMEOUT_SECONDS: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class SupervisedComponent:
    """One long-running pilot component that must honor the shared stop event."""

    name: str
    run: Callable[[Event], None] = field(repr=False)
    ready: Callable[[], bool] = field(default=lambda: True, repr=False)

    def __post_init__(self) -> None:
        """Reject blank or duplicate-prone names before starting a thread."""
        if (
            not self.name
            or not self.name.isascii()
            or not self.name.replace("-", "").isalnum()
        ):
            raise ValueError("pilot component name is invalid")


class PilotSupervisor:
    """Keep API, worker, router, and scheduler in one failure domain."""

    def __init__(self, components: Sequence[SupervisedComponent]) -> None:
        """Bind a closed unique component set without starting any work."""
        names = tuple(component.name for component in components)
        if not names or len(names) != len(set(names)):
            raise ValueError("pilot components must have unique names")
        self._components = tuple(components)
        self._stop = Event()
        self._failed = Event()
        self._started = Event()
        self._lock = Lock()
        self._threads: dict[str, Thread] = {}

    @property
    def healthy(self) -> bool:
        """Return true only while every required component is still running."""
        with self._lock:
            threads = tuple(self._threads.values())
        return (
            self._started.is_set()
            and not self._stop.is_set()
            and not self._failed.is_set()
            and len(threads) == len(self._components)
            and all(
                self._threads[component.name].is_alive() and _component_ready(component)
                for component in self._components
            )
        )

    @property
    def failed(self) -> bool:
        """Return whether a component ended before coordinated shutdown."""
        return self._failed.is_set()

    def start(self) -> None:
        """Start each required component exactly once."""
        with self._lock:
            if self._started.is_set() or self._threads:
                raise RuntimeError("pilot supervisor already started")
            for component in self._components:
                thread = Thread(
                    target=self._run_component,
                    args=(component,),
                    name=f"creek-pilot-{component.name}",
                    daemon=False,
                )
                self._threads[component.name] = thread
            self._started.set()
            threads = tuple(self._threads.values())
        for thread in threads:
            thread.start()

    def request_stop(self) -> None:
        """Close admission to new component work and begin graceful shutdown."""
        self._stop.set()

    def wait_for_stop(self, timeout: float | None = None) -> bool:
        """Wait until shutdown is requested by signal or component failure."""
        return self._stop.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for every component; return false if any is still alive."""
        with self._lock:
            threads = tuple(self._threads.values())
        deadline = None if timeout is None else monotonic() + timeout
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - monotonic())
            thread.join(remaining)
        return all(not thread.is_alive() for thread in threads)

    def stop(self, timeout: float = _DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        """Request graceful stop and fail if any component misses the bound."""
        if timeout <= 0:
            raise ValueError("pilot stop timeout must be positive")
        self.request_stop()
        if not self.wait(timeout):
            raise RuntimeError("pilot component did not stop within the bound")

    def _run_component(self, component: SupervisedComponent) -> None:
        try:
            component.run(self._stop)
        except Exception:
            _LOGGER.error("pilot component failed name=%s", component.name)
            self._failed.set()
        finally:
            if not self._stop.is_set():
                self._failed.set()
                self._stop.set()


def _component_ready(component: SupervisedComponent) -> bool:
    """Contain a readiness probe failure as unhealthy, never a traceback."""
    try:
        return component.ready()
    except Exception:
        return False
