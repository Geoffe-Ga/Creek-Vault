"""``creek-tools-api`` — the entry point, and four refusals before it binds (#1074).

A second process in this repository now listens on a socket and authenticates
with the same bearer secrets as the first, so it owes the operator the same
promises ``creek-tools-mcp --transport network`` has kept since #837 — and
literally the same code, via :mod:`creek_mcp.transport_posture`. A second copy
of the posture gate would be a second gate free to drift about, say,
``127.0.0.5``.

Startup is fail-closed three times over, all before a socket exists:

* **No consumer tokens, no service.** There is no anonymous access, and
  refusing at startup rather than at first request is the difference between an
  operator seeing the problem and an unauthenticated vault surface being live
  until somebody notices.
* **No weak token.** A configured secret below
  :data:`creek_mcp.token_policy.MIN_TOKEN_LEN` exits with the rotation recipe
  and never the token value, because a startup error lands in logs, terminals
  and process supervisors.
* **No cleartext bearer on a routable network.** A non-loopback bind without
  TLS exits naming the flags that fix it.

Having cleared those, startup **announces on stderr** any consumer holding
more than one currently-valid token, so an open rotation window (#895) is
visible rather than permanent. It goes to stderr because stdout here carries
the ``--print-openapi`` document consumers pipe into client generators; the
decision, and the notice itself, are
:func:`creek_mcp.remote_auth.announce_rotation_window`'s, shared with the MCP
adapter so it cannot hold on one entry point and lapse on the other.

**The default port is deliberately not 8000.** That is both Adepthood's own
backend port and :data:`creek_mcp.server.DEFAULT_MCP_NETWORK_PORT`, and the two
Creek adapters are expected to run side by side on one host; identical defaults
would surface as an ``address already in use`` at startup of whichever process
lost the race — an error that names a port and says nothing about the collision.

**``--host`` never defaults to the wildcard.** A wildcard default would put an
authenticated vault surface on every interface the moment somebody ran the
command to "try it out".
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

import uvicorn

from creek.config import CONFIG_PATH_ENV_VAR
from creek_mcp.api.openapi import build_openapi
from creek_mcp.httpapi import SERVER_NAME
from creek_mcp.httpapi.app import create_app
from creek_mcp.httpapi.auth import build_verifier
from creek_mcp.remote_auth import announce_rotation_window
from creek_mcp.transport_posture import require_transport_confidentiality

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from creek_mcp.remote_auth import ConsumerTokenVerifier

DEFAULT_API_PORT: Final[int] = 8823
"""Bind port for ``creek-tools-api``, chosen to differ from every neighbour."""

DEFAULT_API_HOST: Final[str] = "127.0.0.1"
"""Bind host: loopback, mirroring ``creek-tools-mcp``'s own default."""

_JSON_INDENT: Final[int] = 2
"""Indent for the printed document, matching the published bundle's rendering."""


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``creek-tools-api`` argument parser.

    Returns:
        The parser. Exposed as a helper so the flags can be exercised without
        going anywhere near a socket.
    """
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description=(
            "Serve the Adepthood /v1 HTTP application API. Requires "
            "per-consumer bearer tokens; a non-loopback bind additionally "
            "requires TLS."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_API_HOST,
        help=(
            "Bind host. A non-loopback host requires --tls-cert/--tls-key so "
            "bearer tokens never transit the network in cleartext."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_API_PORT,
        help="Bind port.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to creek_config.yaml. When supplied, sets "
            f"{CONFIG_PATH_ENV_VAR} in the process environment so the vault "
            "resolves the same way the CLI resolves it, whatever the cwd."
        ),
    )
    parser.add_argument(
        "--tls-cert",
        type=Path,
        default=None,
        help="Path to the TLS certificate (PEM). Required with --tls-key.",
    )
    parser.add_argument(
        "--tls-key",
        type=Path,
        default=None,
        help="Path to the TLS private key (PEM). Required with --tls-cert.",
    )
    parser.add_argument(
        "--print-openapi",
        action="store_true",
        help=(
            "Write the generated OpenAPI document to stdout and exit, binding "
            "no socket and reading no vault."
        ),
    )
    return parser


def _print_openapi() -> None:
    """Write the generated OpenAPI document to stdout.

    Machine-readable output on stdout only, so a pipe stays clean. It needs no
    credentials: printing the published contract is not a privileged operation,
    and requiring them would mean generating a client SDK needed production
    secrets.
    """
    print(json.dumps(build_openapi(), indent=_JSON_INDENT, sort_keys=True))


def _apply_config(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Pin ``CREEK_CONFIG`` from ``--config``, refusing a path that is not there.

    Args:
        parser: The parser, used to report errors in argparse style.
        args: The parsed arguments.
    """
    if args.config is None:
        return
    if not args.config.exists():
        parser.error(f"--config: file not found: {args.config}")
    os.environ[CONFIG_PATH_ENV_VAR] = str(args.config.resolve())


def _verifier_or_exit(parser: argparse.ArgumentParser) -> ConsumerTokenVerifier:
    """Return the configured verifier, or exit naming what the operator must fix.

    Args:
        parser: The parser, used to report errors in argparse style.

    Returns:
        The verifier over every configured consumer.
    """
    try:
        verifier = build_verifier()
    except ValueError as exc:
        # Covers "nothing configured", "a configured token is below the shared
        # length floor" (#838), and the two ambiguous registries #895 refuses:
        # a consumer named twice, and one token value configured for two
        # consumers. No such message ever carries a token value, and
        # ``parser.error`` exits non-zero rather than returning.
        parser.error(str(exc))
    return verifier


def build_uvicorn_config(app: Starlette, args: argparse.Namespace) -> uvicorn.Config:
    """Build the uvicorn configuration this CLI serves *app* under.

    Extracted from :func:`serve` so the one decision in it that is a security
    promise — ``access_log=False`` — is reachable by a test without binding a
    socket.

    **Why uvicorn's own access log is switched off.** uvicorn ships an access
    logger that is on by default and writes one line per request in the form
    ``client_addr - "METHOD /concrete/path?query HTTP/1.1" status``. It does not
    replace :class:`~creek_mcp.httpapi.logging.AccessLogMiddleware`; it runs
    alongside it. Two fields in that line are exactly what the middleware exists
    to keep out of a log: the **client address**, and the **concrete path with
    its query string** — which for ``/v1/journal-entries/{external_id}`` is a
    consumer-chosen identifier, republished on every sync. ``docs/api.md``
    publishes to consumers the unqualified promise that request logging names
    the route template and never the concrete path; left on, uvicorn's logger
    keeps that promise in one log and breaks it in the other, and the operator's
    shipper collects both.

    **Suppression, not filtering.** A ``log_config`` override or a
    :class:`logging.Filter` would leave a *second* redaction rule in the process,
    free to drift from the middleware's — the failure mode
    :mod:`creek_mcp.httpapi.logging`'s "re-export, never redefine" docstring
    argues against. Total suppression has no rule to drift: there is one access
    log, and it is the audited one.

    Args:
        app: The built application.
        args: The parsed arguments, carrying host, port and TLS material.

    Returns:
        The configuration, carrying the bind address, the TLS material when the
        operator supplied it, and no access log of uvicorn's own.
    """
    return uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=None if args.tls_cert is None else str(args.tls_cert),
        ssl_keyfile=None if args.tls_key is None else str(args.tls_key),
        access_log=False,
    )


def serve(app: Starlette, args: argparse.Namespace) -> None:  # pragma: no cover
    """Run *app* under uvicorn until the process is stopped.

    The one seam that binds a socket, and therefore the one thing the CLI tests
    replace: a guard bug then fails as ``DID NOT RAISE`` rather than hanging the
    suite on a real port. Everything decidable without a socket lives in
    :func:`build_uvicorn_config`, so that the uncovered surface here is the bind
    itself and nothing else.

    Args:
        app: The built application.
        args: The parsed arguments, carrying host, port and TLS material.
    """
    uvicorn.Server(build_uvicorn_config(app, args)).run()


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, refuse an unsafe configuration, announce, then serve.

    The announcement is the one non-refusal in the sequence: any open rotation
    window (#895) is reported on stderr before the socket exists, so it is
    visible in the same startup output as the refusals above it.

    Args:
        argv: Command-line arguments, or ``None`` to read :data:`sys.argv`.
            Tests supply an explicit list rather than mutating process state.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.print_openapi:
        _print_openapi()
        return
    _apply_config(parser, args)
    verifier = _verifier_or_exit(parser)
    require_transport_confidentiality(parser, args)
    # After every refusal, never before one: a process about to exit 2 has
    # nothing to announce, and announcing above the posture gate printed a
    # rotation notice for a server that then refused to serve — while the MCP
    # adapter announced below its own gate. Two adapters, one moment. A window
    # has to be closed again, and an operator who cannot see one is open will
    # not close it (#895). On stderr: stdout on this entry point carries the
    # ``--print-openapi`` document, which consumers pipe into code generators.
    announce_rotation_window(verifier)
    serve(create_app(verifier=verifier), args)
