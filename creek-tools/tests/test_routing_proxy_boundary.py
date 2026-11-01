"""The router-to-vault dial ignores ambient proxy settings (#2045).

The edge router forwards a user's MCP requests — journal content — to their
vault over Fly's private network. The destination is part of the privacy
argument: the request must reach *that vault's private address*, not whatever
an inherited ``HTTPS_PROXY`` names. These tests pin the property two ways: a
dial spy that records where the composed client actually connects with every
proxy variable set to a fake host, and a structural rule over
``routing_cli.py`` so a later edit cannot build the vault client anywhere but
its one builder. That the builder itself is proxy-free (``trust_env=False``,
no proxy, mounts or ``**kwargs``) is #2044's rule 4, extended to it in
``tests/test_ollama_endpoint_invariant.py``.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import httpx
import pytest

from creek_mcp.provisioning import routing_cli
from tests.test_provisioning_production_adapters import _ca_files
from tests.test_provisioning_routing_cli import _NOW, _arguments

if TYPE_CHECKING:
    from collections.abc import Callable

_ROUTING_CLI: Final[Path] = (
    Path(__file__).resolve().parent.parent
    / "creek_mcp"
    / "provisioning"
    / "routing_cli.py"
)

_PROXY_ENV: Final[tuple[str, ...]] = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)
"""Proxy variables a trusting ``httpx`` client would read."""

_NO_PROXY_ENV: Final[tuple[str, ...]] = ("NO_PROXY", "no_proxy")
"""Cleared so an inherited exemption cannot mask a proxied dial."""

_FAKE_PROXY: Final[str] = "http://proxy.invalid:3128"
"""An obviously fake proxy; a dial aimed at it shows up in the spy."""

_FAKE_PROXY_DESTINATION: Final[tuple[str, int]] = ("proxy.invalid", 3128)

_VAULT_PORT: Final[int] = 8823

_PRIVATE_DNS_VAULT: Final[str] = "3d8d1f2e.vm.creek-vault-a1.internal"
"""A synthetic Fly ``.internal`` Machine name, the shape the router targets."""

_SIX_PN_VAULT: Final[str] = "fdaa:0:1:a7b:1::3"
"""A synthetic Fly 6PN IPv6 address, what that private name resolves to."""

_REQUEST_TIMEOUT_SECONDS: Final[float] = 5.0

_VAULT_CLIENT_BUILDER: Final[str] = "private_vault_client"


class _AsyncDialSpy:
    """Record every TCP destination an ``httpx.AsyncClient`` attempts.

    ``httpcore``'s asyncio backend connects through :func:`anyio.connect_tcp`
    (not :func:`socket.create_connection`, which the synchronous socket spy
    patches), so this spy replaces that call, records the destination before
    any DNS lookup, and refuses the connection.

    Attributes:
        destinations: The ``(host, port)`` pairs that were attempted.
    """

    def __init__(self, patcher: pytest.MonkeyPatch) -> None:
        """Replace :func:`anyio.connect_tcp` with the recorder.

        Args:
            patcher: Restores the real function when its scope ends.
        """
        self.destinations: list[tuple[str, int]] = []
        patcher.setattr(anyio, "connect_tcp", self._connect)

    async def _connect(
        self, remote_host: str, remote_port: int, **_kwargs: object
    ) -> anyio.abc.SocketStream:
        """Record the destination and refuse the connection.

        Args:
            remote_host: The host the caller aimed at.
            remote_port: The port the caller aimed at.

        Returns:
            Never; the call always raises.

        Raises:
            OSError: Always, so the client's connect-failure path runs.
        """
        self.destinations.append((str(remote_host), int(remote_port)))
        msg = "the dial spy refuses every connection"
        raise OSError(msg)


def _set_proxy_environment(patcher: pytest.MonkeyPatch, *, proxied: bool) -> None:
    """Point every proxy variable at the fake proxy, or clear them all.

    Args:
        patcher: Scope for the environment changes.
        proxied: Whether the proxy variables are set.
    """
    for variable in _NO_PROXY_ENV:
        patcher.delenv(variable, raising=False)
    for variable in _PROXY_ENV:
        if proxied:
            patcher.setenv(variable, _FAKE_PROXY)
        else:
            patcher.delenv(variable, raising=False)


def _origin(host: str) -> str:
    """Return the HTTPS origin for *host* on the vault port, bracketing IPv6."""
    authority = f"[{host}]" if ":" in host else host
    return f"https://{authority}:{_VAULT_PORT}"


async def _forward_once(client: httpx.AsyncClient, url: str) -> str:
    """Send one synthetic MCP request through *client*; return the refusal.

    Args:
        client: The client under test; it is closed before returning.
        url: The destination.

    Returns:
        The connect error's message, so callers can check it names no proxy.
    """
    try:
        await client.post(url, content=b'{"jsonrpc":"2.0","method":"synthetic"}')
    except httpx.ConnectError as exc:
        return str(exc)
    finally:
        await client.aclose()
    msg = "the dial spy should have refused the connection"
    raise AssertionError(msg)


def _dial(
    build: Callable[[], httpx.AsyncClient], host: str, *, proxied: bool
) -> tuple[list[tuple[str, int]], str]:
    """Build a client under a proxy environment and record where it dials.

    The client is built *inside* the patched environment because ``httpx``
    reads the proxy variables when the client is constructed.

    Args:
        build: Returns the client under test.
        host: The vault host the request is addressed to.
        proxied: Whether every proxy variable points at the fake proxy.

    Returns:
        The destinations attempted, then the refusal message.
    """
    with pytest.MonkeyPatch.context() as patcher:
        _set_proxy_environment(patcher, proxied=proxied)
        spy = _AsyncDialSpy(patcher)
        message = asyncio.run(_forward_once(build(), f"{_origin(host)}/mcp"))
    return spy.destinations, message


@pytest.mark.parametrize("host", [_PRIVATE_DNS_VAULT, _SIX_PN_VAULT])
def test_an_ambient_proxy_does_not_move_the_router_to_vault_dial(
    tmp_path: Path, host: str
) -> None:
    """#2045: the environment cannot choose where journal content goes.

    With every proxy variable set to a fake host, the composed router-to-vault
    client still dials only the vault's private address, both by Fly
    ``.internal`` name and by 6PN IPv6 literal. The negative control
    (variables unset) records the identical destination, so this proves the
    property rather than the absence of a proxy; and the refusal never names
    the proxy.
    """
    ca_certificate, _ = _ca_files(tmp_path)

    def build() -> httpx.AsyncClient:
        return routing_cli.private_vault_client(
            ca_certificate, timeout_seconds=_REQUEST_TIMEOUT_SECONDS
        )

    recorded = []
    for proxied in (True, False):
        destinations, message = _dial(build, host, proxied=proxied)
        recorded.append(destinations)
        assert "proxy.invalid" not in message

    expected = [(host, _VAULT_PORT)]
    assert recorded == [expected, expected]


def test_the_dial_spy_sees_a_proxied_dial() -> None:
    """Positive control: a proxy-honouring client is caught by the same spy.

    Without this, the ambient-proxy test could pass vacuously — say, if the
    spy stopped intercepting the asyncio backend. A default ``AsyncClient``
    built under the same environment dials the fake proxy instead of the vault.
    """
    destinations, _ = _dial(
        lambda: httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS),
        _PRIVATE_DNS_VAULT,
        proxied=True,
    )

    assert destinations == [_FAKE_PROXY_DESTINATION]


def test_compose_builds_the_vault_client_through_the_proxy_free_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production composition wires the proxy-free client, not its own."""
    built: list[httpx.AsyncClient] = []
    real_builder = routing_cli.private_vault_client

    def recording_builder(
        ca_certificate_file: Path,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> httpx.AsyncClient:
        client = real_builder(
            ca_certificate_file, timeout_seconds=timeout_seconds, transport=transport
        )
        built.append(client)
        return client

    monkeypatch.setattr(routing_cli, _VAULT_CLIENT_BUILDER, recording_builder)
    parser = routing_cli.build_parser()
    bundle = routing_cli.compose(
        parser.parse_args(_arguments(tmp_path)),
        parser,
        clock=lambda: _NOW,
        provider_transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
    )
    asyncio.run(bundle.aclose())

    assert len(built) == 1
    assert built[0].is_closed


# --------------------------------------------------------------------------
# Structural rule: the vault client is built in one place, with explicit trust.
# --------------------------------------------------------------------------


def _is_async_client(call: ast.Call) -> bool:
    """Return whether *call* constructs ``httpx.AsyncClient``."""
    target = call.func
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "AsyncClient"
        and isinstance(target.value, ast.Name)
        and target.value.id == "httpx"
    ) or (isinstance(target, ast.Name) and target.id == "AsyncClient")


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """Map each node id inside a top-level function to that function's name."""
    owners: dict[int, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(node):
                owners[id(inner)] = node.name
    return owners


def _vault_client_violations(source: str) -> list[str]:
    """Return every way *source* could build the vault client off the builder.

    The rule: every ``httpx.AsyncClient`` in the routing composition — the
    only async client there is the router-to-vault one — is built inside
    :func:`~creek_mcp.provisioning.routing_cli.private_vault_client` with an
    explicit ``verify=``, and ``compose`` builds its client through it. So
    the builder's proxy-free rule covers every vault client there is.

    Args:
        source: The module source to check.

    Returns:
        One message per violation; empty when the module obeys the rule.
    """
    tree = ast.parse(source)
    owners = _enclosing_functions(tree)
    clients = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_async_client(node)
    ]
    found = [] if clients else ["no router-to-vault client is constructed"]
    for call in clients:
        if owners.get(id(call)) != _VAULT_CLIENT_BUILDER:
            found.append(f"{call.lineno}: vault client built outside the builder")
        if "verify" not in {keyword.arg for keyword in call.keywords}:
            found.append(f"{call.lineno}: private CA trust is not explicit")
    compose_calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and owners.get(id(node)) == "compose"
    }
    if _VAULT_CLIENT_BUILDER not in compose_calls:
        found.append("compose does not build its vault client through the builder")
    return found


def test_the_router_builds_its_vault_client_in_one_place() -> None:
    """#2045: the real routing composition obeys the one-builder rule."""
    source = _ROUTING_CLI.read_text(encoding="utf-8")

    assert _vault_client_violations(source) == []


_CLEAN: Final[str] = """
import httpx

def private_vault_client(context):
    return httpx.AsyncClient(trust_env=False, verify=context)

def compose(context):
    return private_vault_client(context)
"""


@pytest.mark.parametrize(
    ("planted", "expected"),
    [
        (
            _CLEAN.replace(", verify=context", ""),
            "private CA trust is not explicit",
        ),
        (
            _CLEAN.replace(
                "return private_vault_client(context)",
                "return httpx.AsyncClient(trust_env=False, verify=context)",
            ),
            "vault client built outside the builder",
        ),
        (
            _CLEAN.replace("return private_vault_client(context)", "return context"),
            "compose does not build its vault client through the builder",
        ),
        ("import httpx\n", "no router-to-vault client is constructed"),
    ],
)
def test_planted_vault_client_violations_are_caught(
    planted: str, expected: str
) -> None:
    """Each way of building a vault client off the builder is flagged."""
    assert any(expected in found for found in _vault_client_violations(planted))


def test_a_builder_built_vault_client_passes() -> None:
    """The rule's clean fixture produces no violation."""
    assert _vault_client_violations(_CLEAN) == []
