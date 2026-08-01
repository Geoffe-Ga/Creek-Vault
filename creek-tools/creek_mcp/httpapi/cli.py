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
        # Covers both "nothing configured" and "a configured token is below the
        # shared length floor". Neither message ever carries a token value, and
        # ``parser.error`` exits non-zero rather than returning.
        parser.error(str(exc))
    return verifier


def serve(app: Starlette, args: argparse.Namespace) -> None:  # pragma: no cover
    """Run *app* under uvicorn until the process is stopped.

    The one seam that binds a socket, and therefore the one thing the CLI tests
    replace: a guard bug then fails as ``DID NOT RAISE`` rather than hanging the
    suite on a real port.

    Args:
        app: The built application.
        args: The parsed arguments, carrying host, port and TLS material.
    """
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=None if args.tls_cert is None else str(args.tls_cert),
        ssl_keyfile=None if args.tls_key is None else str(args.tls_key),
    )
    uvicorn.Server(config).run()


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, refuse an unsafe configuration, then serve.

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
    serve(create_app(verifier=verifier), args)
