"""Runnable authenticated provisioning API process for issue #1768."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from creek_mcp.httpapi.provisioning import build_provisioning_app
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    ConsumerTokenVerifier,
    load_consumer_tokens,
)
from creek_mcp.transport_posture import require_transport_confidentiality

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Return the provisioning API's non-secret command-line contract."""
    parser = argparse.ArgumentParser(
        prog="creek-provisioning-api",
        description="Serve Creek's durable asynchronous provisioning control plane.",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--consumer-tokens-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8830)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    return parser


def _load_verifier(
    token_file: Path,
    parser: argparse.ArgumentParser,
) -> ConsumerTokenVerifier:
    """Load the mounted registry without copying its values into args or env."""
    try:
        registry = token_file.read_text(encoding="utf-8")
        tokens = load_consumer_tokens({CONSUMER_TOKENS_ENV: registry})
    except (OSError, UnicodeError, ValueError):
        parser.error("--consumer-tokens-file is unreadable or invalid")
    if not tokens:
        parser.error("--consumer-tokens-file configures no consumers")
    return ConsumerTokenVerifier(tokens)


def main(argv: Sequence[str] | None = None) -> None:
    """Validate mounted inputs and serve the provider-free control-plane API."""
    parser = build_parser()
    args = parser.parse_args(argv)
    require_transport_confidentiality(parser, args)
    verifier = _load_verifier(args.consumer_tokens_file, parser)
    store = ProvisioningStore(args.database)
    uvicorn.run(
        build_provisioning_app(store, verifier),
        host=args.host,
        port=args.port,
        ssl_certfile=None if args.tls_cert is None else str(args.tls_cert),
        ssl_keyfile=None if args.tls_key is None else str(args.tls_key),
        access_log=False,
    )


if __name__ == "__main__":
    main()
