"""Public agent connection captured from one allocation's actual MCP setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.classify.llm.consent import is_managed_vault
from creek_mcp.api.models import AgentMcpDiscovery

if TYPE_CHECKING:
    from creek_mcp.agent_tokens import JwtAgentTokenVerifier


def mounted_agent_mcp(
    *,
    mounted: bool,
    verifier: JwtAgentTokenVerifier | None,
    fly_replay_state: str | None,
) -> AgentMcpDiscovery | None:
    """Snapshot a routed OAuth MCP connection, without reading any corpus.

    Managed private TLS cannot advertise this connection: its outer router
    proxies ``/v1`` alone, even though #1974 writes OAuth settings to both
    runtime modes. Fly replay explicitly mounts the public MCP route; a direct
    self-hosted app serves its own mount. Shared edge metadata is never used as
    evidence about an allocation.

    Args:
        mounted: Whether this app constructed its MCP server.
        verifier: The JWT verifier actually supplied to that MCP mount.
        fly_replay_state: This allocation's verified replay state, or ``None``.

    Returns:
        The configured public resource and issuer, or ``None`` when the mount,
        verifier, public routing or clean HTTPS configuration is absent.
    """
    if not mounted or verifier is None:
        return None
    if is_managed_vault() and not fly_replay_state:
        return None
    try:
        return AgentMcpDiscovery(
            resource=verifier.config.resource_url,
            authorization_server=verifier.config.authorization_server,
        )
    except ValueError:
        # A local HTTP development resource may be valid for MCP while not
        # being a public address an external agent can use. Keep it private.
        return None
