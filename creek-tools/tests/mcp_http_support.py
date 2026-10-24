"""Drive the MCP streamable-HTTP surface at ``/mcp`` from a test client (#1962).

Shared by the mount tests and the container-runtime tests, so "an MCP
handshake succeeded" means the same wire exchange everywhere it is asserted.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from creek_mcp.mcp_path import MCP_PATH
from tests.v1_api_support import STRONG_TOKEN

if TYPE_CHECKING:
    from starlette.testclient import TestClient

_ACCEPT: Final[str] = "application/json, text/event-stream"
"""What a streamable-HTTP client must accept; the SDK refuses anything else."""

_PROTOCOL_VERSION: Final[str] = "2025-06-18"
"""An MCP protocol revision the pinned SDK negotiates."""

HTTP_OK: Final[int] = 200
"""A JSON-RPC request answered."""

HTTP_ACCEPTED: Final[int] = 202
"""A JSON-RPC notification accepted."""


def mcp_headers(token: str | None = STRONG_TOKEN) -> dict[str, str]:
    """Return streamable-HTTP request headers, with *token* as the bearer.

    No ``X-Creek-Contract-Version`` header, on purpose: MCP negotiates its own
    protocol version in ``initialize``, and the ``/v1`` gate must not apply.

    Args:
        token: The bearer, or ``None`` to send no ``Authorization`` header.

    Returns:
        The headers.
    """
    built = {"Accept": _ACCEPT, "Content-Type": "application/json"}
    if token is not None:
        built["Authorization"] = f"Bearer {token}"
    return built


def initialize_body() -> dict[str, Any]:
    """Return the JSON-RPC ``initialize`` request.

    Returns:
        The request body.
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mount-test", "version": "0"},
        },
    }


def sse_payload(body: str) -> dict[str, Any]:
    """Return the last JSON-RPC message of a streamable-HTTP SSE body.

    Args:
        body: The response text.

    Returns:
        The decoded JSON-RPC message.
    """
    data = [
        line.removeprefix("data:").strip()
        for line in body.splitlines()
        if line.startswith("data:")
    ]
    message: dict[str, Any] = json.loads(data[-1])
    return message


def open_session(test_client: TestClient, token: str = STRONG_TOKEN) -> dict[str, Any]:
    """Run ``initialize`` + ``notifications/initialized``; return the session.

    Args:
        test_client: A client whose lifespan is running.
        token: The bearer to present.

    Returns:
        ``{"headers": ..., "initialize": <result>}`` — the headers carry the
        session id every later request must present.
    """
    init = test_client.post(
        MCP_PATH, json=initialize_body(), headers=mcp_headers(token)
    )
    assert init.status_code == HTTP_OK, init.text
    session_headers = {
        **mcp_headers(token),
        "mcp-session-id": init.headers["mcp-session-id"],
    }
    ack = test_client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=session_headers,
    )
    assert ack.status_code == HTTP_ACCEPTED
    return {"headers": session_headers, "initialize": sse_payload(init.text)["result"]}


def rpc(
    test_client: TestClient,
    session_headers: dict[str, str],
    method: str,
    params: dict[str, Any],
) -> Any:
    """Send one JSON-RPC request over the open session; return its ``result``.

    Args:
        test_client: A client whose lifespan is running.
        session_headers: The headers :func:`open_session` returned.
        method: The JSON-RPC method.
        params: Its parameters.

    Returns:
        The decoded ``result`` member.
    """
    response = test_client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
        headers=session_headers,
    )
    assert response.status_code == HTTP_OK, response.text
    return sse_payload(response.text)["result"]


def call_tool(
    test_client: TestClient,
    session_headers: dict[str, str],
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Call tool *name* over the open session; return its structured result.

    Args:
        test_client: A client whose lifespan is running.
        session_headers: The headers :func:`open_session` returned.
        name: The tool name.
        arguments: Its arguments.

    Returns:
        The tool's ``structuredContent``.
    """
    result = rpc(
        test_client,
        session_headers,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    return result["structuredContent"]


def listed_tools(test_client: TestClient, session_headers: dict[str, str]) -> set[str]:
    """Return the tool names ``tools/list`` shows this session.

    Args:
        test_client: A client whose lifespan is running.
        session_headers: The headers :func:`open_session` returned.

    Returns:
        The listed names.
    """
    result = rpc(test_client, session_headers, "tools/list", {})
    return {tool["name"] for tool in result["tools"]}


def call_handshake(test_client: TestClient, session_headers: dict[str, str]) -> Any:
    """Call ``creek.handshake`` over the open session; return its structured result.

    Args:
        test_client: A client whose lifespan is running.
        session_headers: The headers :func:`open_session` returned.

    Returns:
        The tool's ``structuredContent``.
    """
    return call_tool(test_client, session_headers, "creek.handshake", {})
