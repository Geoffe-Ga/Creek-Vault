"""Shared test fixtures for creek-tools.

Provides auto-use fixtures that:

- mock the sentence-transformer model loading so tests never download
  models or require GPU access,
- pin the terminal width so Rich/Typer CLI output renders deterministically
  regardless of the ambient terminal or whether stdout is a TTY (GAP-013).
  The width is pinned in :func:`pytest_configure` as well as in the fixture,
  because a Rich console caches its width at construction and creek's console
  is constructed at collection time -- see that hook's docstring (#1141), and
- clear the process-global elevated-authorization failure budget so the
  #914 purge lockout cannot leak from one test into the next, and
- hide an ambient ``CREEK_CONFIG`` so a config file the operator exported in
  their own shell cannot decide the outcome of a vault-driven test (#1354).

Also provides the opt-in :func:`short_write` fixture (issue #987), which
simulates partial ``os.write`` returns so the vault/save writers can be
pinned against silent truncation.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from creek.config import CONFIG_PATH_ENV_VAR

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Pin a wide terminal so Rich/Typer never wrap CLI output mid-word. Rich
# defaults to 80 columns whenever stdout is not a TTY (pipes, fresh
# containers, CI), which wraps substrings the tests assert on (GAP-013).
_TEST_TERMINAL_COLUMNS = "200"
_TEST_TERMINAL_LINES = "50"

_DIMS = 384


def pytest_configure() -> None:
    """Pin the terminal width *before* any test module is imported (#1141).

    The autouse :func:`_pin_terminal_width` fixture below is not sufficient on
    its own, and the gap is only visible under ``pytest-xdist``.

    A ``rich.console.Console`` resolves ``COLUMNS`` **twice**: once in
    ``Console.__init__``, which caches the value in the private ``_width``, and
    again per render in ``Console.size`` -- but ``size`` returns the cached
    ``_width`` verbatim whenever it is not ``None``. So a console constructed
    while ``COLUMNS`` is set has its width frozen for the life of the process,
    and no later ``setenv`` can widen it. :mod:`creek.cli` builds exactly such
    a module-level console, at import time, which is *collection* time -- long
    before any fixture runs.

    That only bites in a worker. On Linux, GNU readline (pulled in
    transitively by pytest via :mod:`pdb`) calls ``putenv("COLUMNS=80")`` at
    the C level when it finds no TTY. C-level ``putenv`` does not show up in
    the parent's :data:`os.environ` snapshot, but it *is* inherited by every
    subprocess -- so each xdist worker starts life with ``COLUMNS=80`` already
    in its environment, freezes every module-level console at 80 columns, and
    hard-wraps CLI output mid-phrase. The serial run never sees it because
    nothing re-execs. macOS never sees it either, because its Python links
    libedit rather than GNU readline.

    Setting the variable here closes that window: ``pytest_configure`` runs
    before collection, so every console built during collection caches the
    pinned width instead of the inherited 80.
    """
    os.environ["COLUMNS"] = _TEST_TERMINAL_COLUMNS
    os.environ["LINES"] = _TEST_TERMINAL_LINES


@pytest.fixture(autouse=True)
def _pin_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep terminal width/height pinned for every test (GAP-013).

    :func:`pytest_configure` above is what binds for consoles constructed at
    import time; this fixture is the per-test backstop for everything built
    later -- consoles Typer creates on demand, and any code that reads
    ``COLUMNS`` itself -- and it restores whatever a test changed.
    """
    monkeypatch.setenv("COLUMNS", _TEST_TERMINAL_COLUMNS)
    monkeypatch.setenv("LINES", _TEST_TERMINAL_LINES)


@pytest.fixture(autouse=True)
def _isolate_creek_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide an ambient ``CREEK_CONFIG`` from every test (#1354).

    The HARD privacy leak gate now reads ``author.max_reproduced_tier`` from
    the vault's own ``creek_config.yaml``, resolved through
    :func:`creek.config.resolve_config_path`. That resolver consults
    ``CREEK_CONFIG`` *before* it looks inside the vault, and
    :func:`creek.config.load_config` does the same when handed no path. So an
    operator who exports ``CREEK_CONFIG`` in their shell silently redirects
    every vault-driven privacy test at their own config file — a file that may
    raise a ceiling the test expects to be ``open``, or, if the path no longer
    exists, make the resolver raise ``FileNotFoundError`` from inside a check
    that is supposed to be total.

    Either way the local run and CI disagree, and the direction of the
    disagreement is the dangerous one: locally green, and green for a reason
    that has nothing to do with the code under test. The tests that drive a
    real vault through the gate — and so inherit that ambient state — are
    ``tests/test_reflection.py``, ``tests/test_chat_medium.py``,
    ``tests/test_essay_medium.py``, ``tests/test_how_to_medium.py``,
    ``tests/test_book_report_medium.py``,
    ``tests/test_research_piece_medium.py`` and ``tests/test_author_desk.py``.

    Deleting the variable here does not take the capability away from tests
    that want it: fixture set-up runs before the test body, so a test that
    calls ``monkeypatch.setenv(CONFIG_PATH_ENV_VAR, ...)`` itself still sets it
    on a clean slate and still has it restored at teardown.

    Args:
        monkeypatch: Restores the variable (if the environment had one) when
            the test finishes.
    """
    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)


@pytest.fixture(autouse=True)
def _reset_elevated_attempt_budget() -> None:
    """Clear the elevated-auth failed-attempt budget before every test (#914).

    ``creek_mcp.auth._ELEVATED_BUDGET`` is module-level state, so failed
    ``is_elevated`` calls accumulate across the whole process. Five of them
    anywhere arm a lockout that every later test inherits, and *which* tests
    those are depends on collection order: ``tests/test_mcp_auth.py`` and
    ``tests/test_mcp_remote.py`` each spend deliberate failures, while
    ``tests/test_wiring_contract.py`` and the purge happy paths then present a
    correct token and expect it to work. Without this reset that combination
    is an order-dependent flake rather than a defect in either test, and the
    suite has to pass under ``-p no:randomly`` *and* under a shuffle.

    ``tests/test_mcp_attempt_policy.py`` additionally hard-asserts that the
    budget exists and is resettable, in
    ``test_the_auth_module_owns_a_budget_the_conftest_hook_can_reset``, so a
    rename cannot quietly turn this hook into a no-op from the other side.

    The import is deferred into the body rather than hoisted to module scope
    on purpose: anything imported while this file is *read* runs before
    :func:`pytest_configure`, and that is exactly the window #1141 needs kept
    clear of import-time Rich consoles.
    """
    from creek_mcp import auth

    # Accessed directly, not via getattr: a missing budget must fail loudly
    # here rather than let this fixture decay into a silent no-op and hand
    # back the order-dependent flake it exists to prevent.
    auth._ELEVATED_BUDGET.reset()


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


if TYPE_CHECKING:  # pragma: no cover - typing-only, kept beside its consumers
    from pathlib import Path


# ---- Unreadable / schema-invalid notes (issues #1450, #1451, #996, #871, #1448)

CORRUPT_NOTE_SHAPES: Final[tuple[str, ...]] = (
    "malformed-yaml",
    "undecodable",
    "nonstring-key",
)
"""The three ways a vault note defeats ``frontmatter.load``.

Each shape lands on a *different* limb of
:data:`creek.vault.reader.FRONTMATTER_LOAD_ERRORS`, which is why one shape
is not enough to exercise a guard:

``malformed-yaml``
    Unparseable header -> :class:`yaml.YAMLError`.
``undecodable``
    Bytes that are not UTF-8 -> :class:`UnicodeDecodeError` (a
    :class:`ValueError`), raised while *reading*, before any parse.
``nonstring-key``
    A header mapping with a non-string key -> a builtin
    :class:`TypeError` out of ``Post(content, handler, **metadata)``.
    This limb is absent from the older ``(OSError, ValueError,
    yaml.YAMLError)`` tuple, so a fixture set that omits it silently
    under-tests every guard that widened to catch it (#1475, #924).
"""


def _corrupt_note_bytes(shape: str) -> bytes:
    """Return the raw bytes of an unreadable note of the given *shape*.

    Args:
        shape: One of :data:`CORRUPT_NOTE_SHAPES`.

    Returns:
        Bytes to write verbatim; deliberately not ``str`` because the
        ``undecodable`` shape has no valid UTF-8 spelling.

    Raises:
        ValueError: If *shape* is not a known shape. Guarding here turns a
            typo in a ``parametrize`` list into a loud error instead of a
            note that quietly loads fine and makes the test vacuous.
    """
    if shape == "malformed-yaml":
        return b"---\n:\n  - [unclosed\n---\ncorrupt body\n"
    if shape == "undecodable":
        return b"---\ntitle: caf\xff\xfe\n---\ncorrupt body\n"
    if shape == "nonstring-key":
        # Reuse the single definition of this poison rather than minting a
        # fourth copy of it; the bool/int key variants live beside it and
        # are swept by that module's own loader battery.
        from tests.test_frontmatter_nonstring_key_guard import (
            NONSTRING_KEY_HEADERS,
        )

        header = NONSTRING_KEY_HEADERS["date_key"]
        return f"---\n{header}---\ncorrupt body\n".encode()
    msg = f"Unknown corrupt-note shape: {shape!r}"
    raise ValueError(msg)


@pytest.fixture
def corrupt_note() -> Callable[[Path, str], Path]:
    """Return a factory writing one *unreadable* note at a caller-chosen path.

    The factory emits **only the poison**: it never writes the valid
    sibling note a non-vacuous test needs beside it. That is deliberate.
    A valid record's frontmatter is schema-specific (a Fragment's
    ``source`` must be a mapping carrying ``platform``, its
    ``frequency.primary`` an ``F1``..``F10`` value), so baking one into
    this shared file would couple it to the models and duplicate the
    per-suite builders that already exist. Each test supplies its own
    good note and takes only the poison from here.

    Returns:
        ``factory(path, shape) -> path``, where *shape* is one of
        :data:`CORRUPT_NOTE_SHAPES`. Parent directories are created.
    """

    def _write(path: Path, shape: str) -> Path:
        """Write the *shape* poison to *path* and return *path*."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_corrupt_note_bytes(shape))
        return path

    return _write


@pytest.fixture
def pydantic_invalid_note() -> Callable[[Path], Path]:
    """Return a factory writing a *readable* note that fails model validation.

    The distinction matters: this note's YAML parses, and it carries
    ``type: fragment`` so it passes the type gate and actually reaches
    ``Fragment.model_validate``. Exactly one field is broken
    (``created``), so the note exercises a caller's
    ``except ValidationError`` arm — a different arm from the load
    guards :func:`corrupt_note` targets, and one a corrupt note can
    never reach.

    Returns:
        ``factory(path) -> path``. Parent directories are created.
    """

    def _write(path: Path) -> Path:
        """Write the schema-invalid fragment note to *path*."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "type: fragment\n"
            "id: frag-schema-invalid\n"
            "title: Schema-invalid fragment\n"
            "privacy_tier: open\n"
            "source:\n"
            "  platform: journal\n"
            # Valid in every other field; only `created` is unparseable,
            # so the ValidationError is about the schema and not about a
            # half-written stub.
            "created: not-a-timestamp\n"
            "ingested: '2026-01-15T12:00:00+00:00'\n"
            "---\n"
            "schema-invalid body\n",
            encoding="utf-8",
        )
        return path

    return _write
