"""Shared test fixtures for creek-tools.

Provides auto-use fixtures that:

- mock the sentence-transformer model loading so tests never download
  models or require GPU access, and
- pin the terminal width so Rich/Typer CLI output renders deterministically
  regardless of the ambient terminal or whether stdout is a TTY (GAP-013).

Also provides the opt-in :func:`short_write` fixture (issue #987), which
simulates partial ``os.write`` returns so the vault/save writers can be
pinned against silent truncation.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Pin a wide terminal so Rich/Typer never wrap CLI output mid-word. Rich
# defaults to 80 columns whenever stdout is not a TTY (pipes, fresh
# containers, CI), which wraps substrings the tests assert on (GAP-013).
_TEST_TERMINAL_COLUMNS = "200"
_TEST_TERMINAL_LINES = "50"

_DIMS = 384


@pytest.fixture(autouse=True)
def _pin_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin terminal width/height so Rich CLI output never wraps (GAP-013).

    Rich resolves its render width from the ``COLUMNS`` environment variable
    on every render (not just at ``Console`` construction), so setting it here
    deterministically controls output width for the module-level ``Console``
    in :mod:`creek.cli` and any other Rich/Typer output -- whether stdout is a
    real TTY or a pipe. ``monkeypatch`` restores the prior environment after
    each test.
    """
    monkeypatch.setenv("COLUMNS", _TEST_TERMINAL_COLUMNS)
    monkeypatch.setenv("LINES", _TEST_TERMINAL_LINES)


def _make_mock_model(dims: int = _DIMS) -> MagicMock:
    """Create a mock SentenceTransformer returning deterministic embeddings."""
    model = MagicMock()

    def _encode(
        sentences: str | list[str],
        show_progress_bar: bool = False,
        batch_size: int = 32,
        **kwargs: Any,
    ) -> np.ndarray:
        if isinstance(sentences, str):
            rng = np.random.default_rng(hash(sentences) % 2**32)
            return rng.standard_normal(dims).astype(np.float32)
        rng_batch = [
            np.random.default_rng(hash(s) % 2**32).standard_normal(dims)
            for s in sentences
        ]
        return np.array(rng_batch, dtype=np.float32)

    model.encode = MagicMock(side_effect=_encode)
    return model


@pytest.fixture(autouse=True)
def mock_sentence_transformer() -> Iterator[MagicMock]:
    """Auto-mock sentence-transformer loading to prevent model downloads."""
    mock_model = _make_mock_model()
    with patch(
        "creek.link.embeddings._load_sentence_transformer",
        return_value=mock_model,
    ) as mock_load:
        yield mock_load


class ShortWriteController:
    """Stand-in for ``os.write`` that simulates partial (short) writes.

    Installed by the :func:`short_write` fixture. Only descriptors handed
    out by ``os.open`` *while the fixture is active* are shortened; every
    other descriptor is delegated verbatim to the pristine ``os.write``,
    so pytest's capture pipes and coverage's data files are untouched.

    Until one of the installer methods runs, the shim is a pure
    pass-through — requesting the fixture therefore never perturbs
    fixture set-up or unrelated I/O.

    Attributes:
        calls: How many ``os.write`` calls have targeted a tracked fd.
    """

    def __init__(
        self,
        real_write: Callable[[int, bytes | memoryview], int],
        tracked: set[int],
    ) -> None:
        """Store the pristine ``os.write`` and the shared tracked-fd set.

        Args:
            real_write: The unpatched ``os.write``.
            tracked: Mutable set of descriptors opened through the
                patched ``os.open``; shared with (and filled by) the
                :func:`short_write` fixture.
        """
        self._real_write = real_write
        self._tracked = tracked
        self._strategy: Callable[[int, bytes | memoryview], int] | None = None
        self.calls = 0

    def __call__(self, fd: int, data: bytes | memoryview) -> int:
        """Serve as the monkeypatched ``os.write``.

        Args:
            fd: Target file descriptor.
            data: Payload buffer. Callers under test hand over a
                ``memoryview``, so only ``len()`` and slicing are used.

        Returns:
            The number of bytes actually written.
        """
        if fd not in self._tracked:
            return self._real_write(fd, data)
        self.calls += 1
        if self._strategy is None:
            return self._real_write(fd, data)
        return self._strategy(fd, data)

    def halve(self) -> None:
        """Write only ``max(1, len(data) // 2)`` bytes per call."""

        def _halve(fd: int, data: bytes | memoryview) -> int:
            """Write the first half of *data* and report that count."""
            return self._real_write(fd, data[: max(1, len(data) // 2)])

        self._strategy = _halve

    def one_byte(self) -> None:
        """Write exactly one byte per call."""

        def _one_byte(fd: int, data: bytes | memoryview) -> int:
            """Write the first byte of *data* and report that count."""
            return self._real_write(fd, data[:1])

        self._strategy = _one_byte

    def passthrough(self) -> None:
        """Delegate every write verbatim again, clearing any strategy.

        Restores the controller's initial (pure pass-through) state so a
        test can stage a failure, observe the damage it left behind, and
        then let a follow-up write succeed *in full* — without reaching
        into private attributes. The ``calls`` counter deliberately
        keeps running across the reset, so the total number of tracked
        writes stays observable.
        """
        self._strategy = None

    def stall(self) -> None:
        """Report zero bytes written, having written nothing."""

        def _stall(_fd: int, _data: bytes | memoryview) -> int:
            """Make no forward progress at all."""
            return 0

        self._strategy = _stall

    def fail_after_half(self, error_number: int) -> None:
        """Write half of the first payload, then fail on every later call.

        Args:
            error_number: ``errno`` value carried by the raised
                :class:`OSError`.
        """

        def _fail_after_half(fd: int, data: bytes | memoryview) -> int:
            """Half-write once, then raise ``OSError(error_number)``."""
            if self.calls == 1:
                return self._real_write(fd, data[: max(1, len(data) // 2)])
            raise OSError(error_number, os.strerror(error_number))

        self._strategy = _fail_after_half


@pytest.fixture
def short_write(monkeypatch: pytest.MonkeyPatch) -> ShortWriteController:
    """Shorten ``os.write`` for descriptors opened during the test (#987).

    Deliberately **not** auto-use: a test opts in and then selects a
    failure mode via one of the controller's installers
    (:meth:`~ShortWriteController.halve`,
    :meth:`~ShortWriteController.one_byte`,
    :meth:`~ShortWriteController.stall`,
    :meth:`~ShortWriteController.fail_after_half`);
    :meth:`~ShortWriteController.passthrough` resets to plain
    delegation. ``os.open`` is wrapped only to record the descriptors it
    hands out, so the shortening stays confined to files the code under
    test opened itself, and ``os.close`` is wrapped only to *forget*
    them again: the kernel recycles descriptor numbers, so a stale entry
    in the tracked set would silently shorten writes to an unrelated
    file that happened to inherit the number. Both wrappers are total
    pass-throughs — every call reaches the pristine function with the
    same arguments and the same exceptions propagate — so the rest of
    the suite's descriptor traffic is unaffected.

    Args:
        monkeypatch: Restores ``os.open`` / ``os.write`` / ``os.close``
            at teardown.

    Returns:
        The controller, exposing the installers and a ``calls`` counter.
    """
    real_open = os.open
    real_write = os.write
    real_close = os.close
    tracked: set[int] = set()

    def _tracking_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """Open via the pristine ``os.open`` and record the new fd."""
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        tracked.add(fd)
        return fd

    def _tracking_close(fd: int) -> None:
        """Close via the pristine ``os.close`` and stop tracking the fd.

        Args:
            fd: Descriptor to close. Descriptors this fixture never
                handed out pass through untouched — ``set.discard``
                cannot raise on a member that was never recorded — and
                any error from the real ``os.close`` propagates
                unchanged.
        """
        try:
            real_close(fd)
        finally:
            # Untrack even when the close failed: the number is no
            # longer a descriptor this fixture can claim either way, and
            # leaving it behind is exactly the recycling hazard above.
            tracked.discard(fd)

    controller = ShortWriteController(real_write, tracked)
    monkeypatch.setattr(os, "open", _tracking_open)
    monkeypatch.setattr(os, "write", controller)
    monkeypatch.setattr(os, "close", _tracking_close)
    return controller
