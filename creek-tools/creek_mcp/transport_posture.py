"""Transport-confidentiality posture, shared by every network adapter (#1074).

Two adapters now bind sockets: ``creek-tools-mcp --transport network``
(:mod:`creek_mcp.server`) and ``creek-tools-api``, the Adepthood ``/v1`` HTTP
application adapter (:mod:`creek_mcp.httpapi`). Both authenticate with the same
bearer secrets from :data:`creek_mcp.remote_auth.CONSUMER_TOKENS_ENV`, so both
owe the operator the same promise: **a bearer token never transits a routable
network in cleartext.**

That promise was implemented once, inside ``creek_mcp.server`` as
``_is_loopback`` / ``_require_transport_confidentiality`` (#837). Copying it
into the HTTP adapter would have created a second posture gate free to drift
from the first — the exact failure mode :mod:`creek_mcp.policy` was extracted to
prevent for tier admission in #1073, one layer down the stack. So the functions
move here verbatim and both adapters call *these*.

**This is a pure move.** No branch, message, exit code or ordering changes;
:mod:`creek_mcp.server` keeps module-level aliases under the old private names
so nothing that reaches for them has to change. The behaviour is pinned by the
existing ``tests/test_mcp_remote.py`` cases, which were not edited: if any of
them had needed editing, the move would not have been a move.

The module imports no web framework and no MCP SDK — it decides on an argparse
namespace and a host string, nothing else — so either adapter can depend on it
without dragging the other's transport into scope.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def is_loopback(host: str) -> bool:
    """Return whether *host* is a loopback bind (safe for plaintext transport).

    Loopback traffic never leaves the machine, so serving bearer-token auth
    without TLS is acceptable there — and only there.

    Args:
        host: The ``--host`` value: an IP literal or a hostname.

    Returns:
        ``True`` for loopback IPs (``127.0.0.0/8``, ``::1``) and the literal
        ``"localhost"`` (case-insensitive); ``False`` for everything else,
        including ``""``, wildcard binds, other hostnames, and strings that
        do not parse as an IP address at all.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal (hostname, empty, garbage): assume routable.
        return False


def require_transport_confidentiality(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Refuse network configurations that would put bearer tokens on the wire.

    Enforces, in order: ``--tls-cert``/``--tls-key`` come as a pair; both
    files exist on disk; and a non-loopback ``--host`` is only served when
    TLS is configured. Each violation exits via
    :meth:`argparse.ArgumentParser.error` (nonzero exit, message on stderr)
    *before* any socket is opened (#837).

    The messages name the flags and the offending host or path, and never a
    token: a startup error lands in logs, terminals and process supervisors.

    Args:
        parser: The CLI parser, used to report errors in argparse style.
        args: Parsed arguments carrying ``host``, ``tls_cert``, ``tls_key``.
    """
    if (args.tls_cert is None) != (args.tls_key is None):
        parser.error(
            "--tls-cert and --tls-key are required together; supply both "
            "(or neither, for a loopback-only bind)"
        )
    if args.tls_cert is not None:
        for flag, path in (("--tls-cert", args.tls_cert), ("--tls-key", args.tls_key)):
            if not path.exists():
                parser.error(f"{flag}: file not found: {path}")
        return
    if not is_loopback(args.host):
        parser.error(
            f"refusing to serve on non-loopback host {args.host!r} without TLS: "
            "bearer tokens would transit the network in cleartext. Bind "
            "127.0.0.1, terminate TLS in a reverse proxy, or pass "
            "--tls-cert/--tls-key"
        )
