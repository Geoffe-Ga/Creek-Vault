"""The seven ``/v1`` middlewares, outermost first (#1074).

Every one of them is **pure ASGI** — ``async def __call__(self, scope, receive,
send)`` — and never
:class:`starlette.middleware.base.BaseHTTPMiddleware`. That is not a style
preference. ``BaseHTTPMiddleware`` runs the downstream application inside its
own task group, and a cancel scope opened outside that group (which is exactly
what :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware`
does) cannot cancel a task inside it. The per-request timeout would silently
stop working, and the failure would look like a hang rather than a bug.

Each also relays a ``lifespan`` scope straight through — a middleware that
assumed ``http`` would break the handshake the test client performs on entry,
and the whole suite would fail at ``with client(...)``. It relays *only* that
one, via :func:`creek_mcp.httpapi.context.pass_through`, and refuses every
other scope type rather than waving it past authentication, the ceiling gate
and the access log (#1124).

The order is pinned as data by ``tests/test_v1_api_hardening.py``, because
every property the modules below promise depends on it:

1. :mod:`~creek_mcp.httpapi.middleware.access_log` — mints the correlation id
   and never refuses, so it must see every response including the boundary's.
2. :mod:`~creek_mcp.httpapi.middleware.boundary` — wraps everything below it.
3. :mod:`~creek_mcp.httpapi.middleware.limits` (concurrency), then
4. the same module's timeout — both above authentication, so a flood of
   *unauthenticated* requests is shed rather than each paying for a token
   comparison.
5. :mod:`creek_mcp.httpapi.auth` — above the router, so a ``401`` never depends
   on whether a path matched.
6. the body-size limit — below authentication, so an anonymous caller cannot
   make the server buffer a large body.
7. :mod:`~creek_mcp.httpapi.middleware.ceiling` — last before the router, above
   every handler, so no vault read can precede the admission decision.
"""
