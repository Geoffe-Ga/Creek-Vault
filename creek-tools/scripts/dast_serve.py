"""Start the ``/v1`` app for a DAST run: production's CLI, plus its access log.

Both DAST workflows (``.github/workflows/dast-contract.yml`` and
``dast-deep.yml``) scan a throwaway instance of the vault API started on
loopback. They start it through this module instead of ``creek-tools-api``
for one reason: ``creek-tools-api`` attaches no logging handler, so the INFO
lines :mod:`creek_mcp.httpapi.middleware.access_log` writes -- route template,
authenticated consumer and status for every request -- go nowhere. Those lines
are the only record of what a scanner actually got back, and
:mod:`scripts.dast_scan_evidence` reads them to prove the scan crossed the
bearer gate.

Everything else is delegated, argument for argument, to
:func:`creek_mcp.httpapi.cli.main`: the consumer-token refusals, the weak-token
floor and the transport-confidentiality gate all run exactly as they do in
production. A launcher with its own argument handling would be a second front
door with its own chance to forget a startup guard.

Usage (from ``creek-tools/``)::

    python -m scripts.dast_serve --config <vault>/00-Creek-Meta/creek_config.yaml
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Final

from creek_mcp.httpapi.cli import main as cli_main
from creek_mcp.httpapi.logging import ACCESS_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import Sequence

_FORMAT: Final[str] = "%(levelname)s %(name)s %(message)s"
"""One access line per request, prefixed so a reader can tell it from uvicorn's."""


def configure_access_log() -> None:
    """Send the access logger's INFO lines to stderr, exactly once.

    Idempotent: a second call replaces the handler rather than adding another,
    because a doubled line would double every count the evidence step reports.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    access = logging.getLogger(ACCESS_LOGGER_NAME)
    access.handlers[:] = [handler]
    access.setLevel(logging.INFO)
    # Not propagated: a root handler added by anything else in the process
    # would print every line a second time.
    access.propagate = False


def main(argv: Sequence[str] | None = None) -> None:
    """Enable the access log, then run the production CLI with *argv*.

    Args:
        argv: Command-line arguments for ``creek-tools-api``, or ``None`` to
            read :data:`sys.argv`.
    """
    configure_access_log()
    cli_main(None if argv is None else list(argv))


if __name__ == "__main__":  # pragma: no cover - exercised by the DAST workflows
    main()
