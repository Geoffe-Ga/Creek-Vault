"""uvicorn's own access logger must never see a ``/v1`` path (#1074).

:mod:`creek_mcp.httpapi.middleware.access_log` exists to make one promise
checkable: an access line names the route **template**
(``/v1/journal-entries/{external_id}``) and never the concrete path, because
logs are read, shipped and retained at a lower classification than the vault, so
a concrete path quietly republishes every ``external_id`` a consumer ever syncs.
``docs/api.md`` publishes that promise to consumers as an unqualified statement
about "request logging".

uvicorn ships an access logger of its own, **on by default**, that writes the
raw request line — ``127.0.0.1:59272 - "PUT /v1/journal-entries/abc HTTP/1.1"
501`` — to stdout, along with the client address. It does not *replace* the
middleware; it runs alongside it. So a server built without
``access_log=False`` keeps the promise in one log and breaks it in the other,
and the operator's log shipper collects both.

**Why this one module binds a loopback socket, and no other ``/v1`` test does.**
``tests/test_v1_api_cli.py``'s docstring states that no test in it opens a port,
and that statement stays true — which is why this module exists separately.
uvicorn's access logger is a property of *uvicorn running*, not of the
configuration object handed to it: asserting that a flag was passed proves only
that a flag was passed, and the whole question here is what uvicorn actually
writes. The only way to find that out is to let it write. The cost is one
ephemeral port on ``127.0.0.1`` for a fraction of a second.

**And it stays a unit test on purpose.** ``./scripts/test.sh`` and CI both
default to ``not integration and not e2e``, so marking this module would quietly
remove it from the very gate it exists to hold. It is fast, deterministic, binds
only loopback, and shuts the server down in a ``finally``.

**Why the capture is ``capsys``, and neither ``caplog`` nor a handler.**
``uvicorn/config.py`` turns the access log off by emptying
``logging.getLogger("uvicorn.access").handlers`` and setting ``propagate =
False``; ``uvicorn/protocols/http/h11_impl.py`` then decides whether to log at
all with ``self.access_logger.hasHandlers()``. Two consequences follow, and both
rule out the obvious approaches. ``caplog`` cannot see the line, because
propagation to the root logger is off. And attaching a handler in order to
observe the line would make ``hasHandlers()`` return ``True`` and thereby
*re-enable* the logging the test is checking is disabled — a test that
manufactures the state it then asserts against. The sink uvicorn installs for
itself is ``ext://sys.stdout``, so the honest observer is the one that watches
the process's own streams.

**The twin.** A capture that sees nothing satisfies an "absent" assertion for
free, which is exactly how a logging guard rots: rename the logger, change the
sink, break the startup poll, and the test stays green while the leak comes
back. So the identical scenario runs a second time with an explicit
``access_log=True``, pinning that the sentinel *is* captured. That test is what
makes the first one mean anything.
"""

from __future__ import annotations

import logging
import threading
from time import perf_counter, sleep
from typing import TYPE_CHECKING, Final

import httpx
import pytest
import uvicorn

from creek_mcp.httpapi import cli as cli_mod
from creek_mcp.httpapi.cli import build_uvicorn_config
from tests.v1_api_support import VALID_JOURNAL_BODY, build_app, headers, seed_vault

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.applications import Starlette

_SENTINEL_ID: Final[str] = "zz-sentinel-external-id-9x7q-zz"
"""A path identifier distinctive enough that any echo of it is unmissable.

Stands in for a real ``external_id``: consumer-side identifying material that
the caller chose and the operator never should see in a log.
"""

_UNSUPPORTED_STATUS: Final[int] = 501
"""What ``PUT /v1/journal-entries/{external_id}`` answers until #1075 lands.

Asserted so that a run which never reached the server cannot pass by simply
logging nothing.
"""

_LOOPBACK: Final[str] = "127.0.0.1"
"""The only host this module ever binds."""

_EPHEMERAL_PORT: Final[int] = 0
"""Port ``0`` asks the OS for a free port, so two parallel runs cannot collide."""

_STARTUP_TIMEOUT: Final[float] = 10.0
"""How long the server may take to report ``started`` before the test fails.

A named bound rather than a bare ``while True``: a server that never comes up
must fail the run, not hang it.
"""

_POLL_INTERVAL: Final[float] = 0.01
"""How often the startup poll checks, in seconds."""

_JOIN_TIMEOUT: Final[float] = 10.0
"""How long the server thread may take to stop, and the client to answer."""

_UVICORN_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
)
"""Every logger :data:`uvicorn.config.LOGGING_CONFIG` reconfigures.

``uvicorn.Config.__init__`` calls ``configure_logging()``, which calls
:func:`logging.config.dictConfig` — a mutation of *global* interpreter state
that outlives the test unless something puts it back.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the uvicorn-logging tests."""
    yield seed_vault(tmp_path)


@pytest.fixture(autouse=True)
def restored_uvicorn_logging() -> Iterator[None]:
    """Snapshot and restore the four ``uvicorn`` loggers around every test here.

    Constructing a :class:`uvicorn.Config` runs
    :func:`logging.config.dictConfig`, which rewrites the handlers,
    ``propagate`` flag and level of ``uvicorn``, ``uvicorn.error``,
    ``uvicorn.access`` and ``uvicorn.asgi``. Left in place, that leaks out of
    this module and changes what every later test in the session can observe —
    including the ``caplog`` assertions in ``tests/test_v1_api_hardening.py``.
    Autouse, because forgetting it in one test would poison the whole run.

    Yields:
        ``None``. The restoration happens on the way back out.
    """
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in _UVICORN_LOGGERS
    }
    try:
        yield
    finally:
        for name, (handlers, propagate, level) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)


def _args_for(port: int) -> argparse.Namespace:
    """Return the parsed CLI namespace ``main`` would hand to the serve seam.

    Built by the production parser rather than by hand. A hand-rolled
    :class:`argparse.Namespace` could make the factory pass by carrying a field
    the real parser never sets, or by omitting one it always does — and the
    factory's whole purpose is to be the thing production calls.

    Args:
        port: The bind port. :data:`_EPHEMERAL_PORT` asks the OS for a free one.

    Returns:
        The parsed namespace, carrying host, port and (absent) TLS material.
    """
    return cli_mod._build_arg_parser().parse_args(
        ["--host", _LOOPBACK, "--port", str(port)]
    )


def _bound_port(server: uvicorn.Server) -> int:
    """Return the port *server* actually bound, once it reports ``started``.

    :data:`_EPHEMERAL_PORT` means the number is only knowable after the bind,
    and ``uvicorn.Server.servers`` does not exist until startup has run — so
    the poll checks ``started`` first and reads the socket second.

    Args:
        server: The server running in the helper thread.

    Returns:
        The bound TCP port.

    Raises:
        AssertionError: When the server does not come up within
            :data:`_STARTUP_TIMEOUT`, so a broken bind fails rather than hangs.
    """
    deadline = perf_counter() + _STARTUP_TIMEOUT
    while perf_counter() < deadline:
        if server.started:
            port: int = server.servers[0].sockets[0].getsockname()[1]
            return port
        sleep(_POLL_INTERVAL)
    msg = "uvicorn never reported started"
    raise AssertionError(msg)


def _serve_one_request(config: uvicorn.Config, path: str) -> httpx.Response:
    """Run *config* on a loopback socket, issue one authenticated ``PUT``, stop.

    The server runs in a daemon thread — uvicorn supports this explicitly, its
    signal handling being a no-op off the main thread — and is stopped in a
    ``finally`` so a failing assertion cannot leave a listener behind.

    Args:
        config: The uvicorn configuration under test.
        path: The request path, appended to the bound origin.

    Returns:
        The response, so the caller can prove the request genuinely reached the
        server rather than passing because nothing was ever logged about it.
    """
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        origin = f"http://{_LOOPBACK}:{_bound_port(server)}"
        response = httpx.put(
            f"{origin}{path}",
            json=VALID_JOURNAL_BODY,
            headers=headers(ceiling="open"),
            timeout=_JOIN_TIMEOUT,
        )
    finally:
        server.should_exit = True
        thread.join(_JOIN_TIMEOUT)
    assert not thread.is_alive(), "uvicorn did not shut down"
    return response


def _sentinel_path() -> str:
    """Return the journal path carrying :data:`_SENTINEL_ID`.

    Returns:
        A concrete ``/v1/journal-entries/<id>`` path.
    """
    return f"/v1/journal-entries/{_SENTINEL_ID}"


def _built_app(vault: Path) -> Starlette:
    """Return the ``/v1`` application under test, over *vault*.

    Args:
        vault: The seeded vault root.

    Returns:
        The configured application, with the suite's verifier.
    """
    return build_app(vault_path=vault)


# --------------------------------------------------------------------------- #
# The flag, pinned at the unit level
# --------------------------------------------------------------------------- #


def test_the_served_config_disables_uvicorns_own_access_log(vault: Path) -> None:
    """The factory the CLI serves through switches uvicorn's access log off.

    Cheap, and it states the intent where a reader of ``cli.py`` will look for
    it. It is deliberately *not* sufficient on its own: a flag is not a
    behaviour, and the two socket-bound tests below are what prove uvicorn
    honours it. Both halves are kept because they fail differently — this one
    when somebody deletes the argument, those when uvicorn changes what the
    argument does.

    Args:
        vault: A seeded vault.
    """
    config = build_uvicorn_config(_built_app(vault), _args_for(_EPHEMERAL_PORT))
    assert config.access_log is False


def test_the_served_config_still_carries_the_bind_and_tls_material(
    vault: Path,
) -> None:
    """Extracting the factory must not drop what ``serve`` was already passing.

    ``host``, ``port`` and the two TLS paths are the entire reason the config
    is built from the parsed namespace; a factory that silenced the access log
    and lost the certificate would pass the test above and serve plaintext.

    Args:
        vault: A seeded vault.
    """
    args = _args_for(_EPHEMERAL_PORT)
    config = build_uvicorn_config(_built_app(vault), args)
    assert config.host == _LOOPBACK
    assert config.port == _EPHEMERAL_PORT
    assert config.ssl_certfile is None
    assert config.ssl_keyfile is None


# --------------------------------------------------------------------------- #
# What uvicorn actually writes
# --------------------------------------------------------------------------- #


def test_a_served_request_leaves_no_path_identifier_on_the_process_streams(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE INVARIANT — a real request through a real uvicorn logs no ``external_id``.

    ``docs/api.md`` promises that request logging names the route template and
    never the concrete path. uvicorn's default access logger writes the raw
    request line to stdout *in addition to* our middleware's line, so that
    promise is only true if the server is built with ``access_log=False``.

    Both streams are asserted, not just stdout: uvicorn's "default" handler
    writes to stderr, and a future change of sink must not be able to move the
    leak from a checked stream to an unchecked one.

    The status assertion is the non-vacuity half of *this* test: a run that
    never reached the server would also log nothing.

    Args:
        vault: A seeded vault.
        capsys: Captures the streams uvicorn's own handlers are bound to.
    """
    config = build_uvicorn_config(_built_app(vault), _args_for(_EPHEMERAL_PORT))
    response = _serve_one_request(config, _sentinel_path())
    captured = capsys.readouterr()
    assert response.status_code == _UNSUPPORTED_STATUS
    assert _SENTINEL_ID not in captured.out
    assert _SENTINEL_ID not in captured.err


def test_the_stream_capture_sees_the_identifier_when_uvicorn_logs_it(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE NON-VACUITY TWIN — with the access log on, the sentinel *is* captured.

    Without this, the test above would pass for any reason at all: a capture
    bound to the wrong stream, a server that never started, a poll that
    returned early, a uvicorn release that renamed the logger. Running the
    identical scenario with ``access_log=True`` proves the observer is not
    simply blind, and it simultaneously documents exactly what the default
    configuration would publish — the concrete ``external_id``, in the raw
    request line, on stdout.

    This config is built directly rather than through
    :func:`~creek_mcp.httpapi.cli.build_uvicorn_config`, because the point is to
    reproduce uvicorn's *default* behaviour, which the factory exists to
    override.

    Args:
        vault: A seeded vault.
        capsys: Captures the streams uvicorn's own handlers are bound to.
    """
    args = _args_for(_EPHEMERAL_PORT)
    config = uvicorn.Config(
        _built_app(vault),
        host=args.host,
        port=args.port,
        access_log=True,
    )
    response = _serve_one_request(config, _sentinel_path())
    captured = capsys.readouterr()
    assert response.status_code == _UNSUPPORTED_STATUS
    assert _SENTINEL_ID in captured.out
