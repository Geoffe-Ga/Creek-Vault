"""The DAST target is the production entry point plus a visible access log.

Both DAST workflows start the ``/v1`` app through :mod:`scripts.dast_serve`
rather than ``creek-tools-api`` directly, for one reason: ``creek-tools-api``
configures no logging handler, so the access middleware's INFO lines -- the only
record of what the scanner got back -- go nowhere. These tests pin that the
launcher adds exactly that and nothing else: the access logger writes to
stderr at INFO, and every argument reaches the real CLI untouched, so the
startup refusals the scan target runs under are production's own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from creek_mcp.httpapi.logging import ACCESS_LOGGER_NAME
from scripts import dast_serve

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Put the access logger back exactly as it was after each test."""
    access = logging.getLogger(ACCESS_LOGGER_NAME)
    saved_level = access.level
    saved_handlers = list(access.handlers)
    saved_propagate = access.propagate
    yield
    access.setLevel(saved_level)
    access.handlers[:] = saved_handlers
    access.propagate = saved_propagate


@pytest.mark.usefixtures("_restore_logging")
def test_the_access_log_is_emitted_to_stderr_at_info(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without this the evidence step reads an empty log and fails every run."""
    dast_serve.configure_access_log()

    logging.getLogger(ACCESS_LOGGER_NAME).info(
        "%s %s consumer=%s status=%s", "GET", "/v1/wheel", "dast", 200
    )

    assert "GET /v1/wheel consumer=dast status=200" in capsys.readouterr().err


@pytest.mark.usefixtures("_restore_logging")
def test_configuring_twice_does_not_duplicate_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A doubled line would double every count the evidence step reports."""
    dast_serve.configure_access_log()
    dast_serve.configure_access_log()

    logging.getLogger(ACCESS_LOGGER_NAME).info("once")

    assert capsys.readouterr().err.count("once") == 1


@pytest.mark.usefixtures("_restore_logging")
def test_every_argument_reaches_the_production_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan target must start under production's refusals, not a copy."""
    seen: list[list[str] | None] = []
    monkeypatch.setattr(dast_serve, "cli_main", seen.append)

    dast_serve.main(["--port", "8823", "--config", "creek_config.yaml"])

    assert seen == [["--port", "8823", "--config", "creek_config.yaml"]]
