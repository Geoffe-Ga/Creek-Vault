"""The one place ``/v1`` decides which tier ceiling a caller may have (#1074).

This is the security core of the HTTP adapter, and it is deliberately three
statements long.

**One gate, one call site.** The decision itself belongs to
:func:`creek_mcp.policy.admitted_ceiling` — transport-neutral since #1073 and
shared with the MCP surface, which reaches it from
:meth:`creek_mcp.server._BoundedFastMCP.call_tool`. Nothing here re-derives it:
this module never names :data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS` and
never tests membership itself, because reasoning about the ingredients is the
same failure as reimplementing the rule, one step subtler. A second call site
anywhere in the package would be a second gate, reached under different
conditions, and the effective policy would be whichever one a request happened
to hit. ``tests/test_v1_api_structure.py`` AST-counts both and pins them at one.

**``is_remote`` is unconditionally ``True``.** ``/v1`` is remote by
construction: it is only ever reached over the network, and it stands up no MCP
request context to sniff. A single ``is_remote=False`` here would lift the
``personal`` cap for whatever path reached it and make ``intimate`` readable
over HTTP — the #1073 bug, written out longhand. The literal is AST-forbidden
across the package.

**Above the router, before any handler.** The ADR asks for "one edge check
before routing, before any vault read is attempted", so an inadmissible ceiling
never reaches a tool and never leaves a trace: not a fragment, not a ledger row,
and — because :meth:`creek_mcp.audit.MCPAuditLog.append` creates its own
directory tree — not an audit file either.

**The refusal is ``422``, not ``403``.** The value came from the caller's own
header and no vault object was resolved or ranked, so ``403``'s published
message — "resolved content exceeds the declared tier ceiling" — would simply be
false. And every inadmissible value gets the *same* ``422``: a caller who could
tell "that tier exists but is above you" from "that is not a tier" would have
learned the tier lattice from the outside.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import Headers

from creek_mcp.api.models import ErrorCode
from creek_mcp.api.routes import CEILING_HEADER
from creek_mcp.httpapi.context import HTTP_SCOPE, context_of
from creek_mcp.httpapi.errors import error_response
from creek_mcp.policy import Admission, CallerIdentity, admitted_ceiling
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class CeilingAdmissionMiddleware:
    """Ask policy whether this caller may run at the ceiling they declared."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap *app*.

        Args:
            app: The next application in the stack — in practice the router.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Admit or refuse, before anything below can read the vault.

        An absent ceiling header reads as ``open``: the most restrictive value,
        so an omitted header can only ever fail closed. The caller's own
        spelling is handed to policy verbatim — whitespace and case are never
        repaired into an admission, because coercion is how a typo'd or hostile
        value ends up admitted at *some* ceiling rather than at none.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != HTTP_SCOPE:
            await self.app(scope, receive, send)
            return
        context = context_of(scope)
        requested = Headers(scope=scope).get(CEILING_HEADER, TierCeiling.OPEN.value)
        identity = CallerIdentity(consumer=context.consumer, is_remote=True)
        verdict = admitted_ceiling(identity, requested)
        # Dispatch on the POSITIVE verdict, never on "not a refusal": if policy
        # grows a third verdict this falls to the refusal branch rather than
        # silently uncapping the call.
        if not isinstance(verdict, Admission):
            refusal = error_response(ErrorCode.INVALID_REQUEST, context)
            await refusal(scope, receive, send)
            return
        # A remote admission always names a member — policy admits only the two
        # ceilings it caps remote callers to, and both parse — so the fallback
        # is unreachable. It is spelled anyway because ``Admission.ceiling`` is
        # optional in the type, and ``open`` is the *most* restrictive value, so
        # even an impossible ``None`` narrows rather than widens.
        context.ceiling = verdict.ceiling or TierCeiling.OPEN
        await self.app(scope, receive, send)
