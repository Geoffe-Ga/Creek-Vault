"""The Starlette adapter for the Adepthood ``/v1`` HTTP application API (#1074).

``creek-tools-api`` serves Creek's Adepthood-facing capabilities over
authenticated HTTP/JSON. ``creek-tools-mcp`` remains the *agent* adapter and is
unchanged by this surface: both call the same ``creek_mcp.tools.*`` functions,
so privacy admission, auditing and idempotency exist exactly once. Operator
documentation is ``creek-tools/docs/api.md``; the ratified contract is
``docs/decisions/2026-07-31-adepthood-http-application-api.md``.

**Why this package is not under** :mod:`creek_mcp.api`. That package holds the
published contract artifacts — the wire models, the route table, the fixture
bundle and the OpenAPI generator — and a test AST-sweeps it to prove none of
them imports a web framework. That invariant is what guarantees the published
schemas and the document are functions of our Pydantic models alone. The
adapter therefore lives beside it rather than under it, and the sweep stays
whole.

**One of everything.** The adapter borrows rather than rebuilds: tokens through
:mod:`creek_mcp.remote_auth`, the length floor through
:mod:`creek_mcp.token_policy`, tier admission through
:func:`creek_mcp.policy.admitted_ceiling`, transport posture through
:mod:`creek_mcp.transport_posture`. ``tests/test_v1_api_structure.py`` pins each
of those as an AST sweep, because a second copy passes the behavioural suite on
the day it is written and only then begins to drift.
"""

from __future__ import annotations

from typing import Final

SERVER_NAME: Final[str] = "creek-tools-api"
"""What this adapter is called, on the command line and in the audit trail.

One string for both, because they name the same thing: an operator reading
``creek-tools-api`` in a startup error and ``creek-tools-api`` in an audit line
is entitled to assume those are the same process. Distinct from
``creek_mcp.server.SERVER_NAME`` on purpose — the two adapters share every rule
and no identity, so an audit entry says which one acted.
"""
