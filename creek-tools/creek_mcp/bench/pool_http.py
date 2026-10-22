"""The one place the bench's pool dial is constructed.

:mod:`creek_mcp.bench.pool_client` names the pool's API paths; this module
builds the client that reaches them. They are separate on purpose, exactly as
:mod:`creek_mcp.bench.ollama_client` keeps its client construction in
:func:`creek.classify.llm.local_boundary.ollama_client`:

- a single place pins ``trust_env=False``, so no ``HTTPS_PROXY`` in the
  environment can carry a benchmark prompt to a third host while the report
  records the endpoint the operator named (ADR-0016 R7, Decision 1 item 2);
- redirects are refused, so the measured destination is the pinned one and
  not whatever a ``Location`` header offers;
- the endpoint-invariant guard (``tests/test_ollama_endpoint_invariant.py``)
  can keep its rule that a module naming a pinned destination's paths builds
  no HTTP client of its own.

TLS is verified against the default trust store: a vendor pool presents a
publicly-chained certificate, and the bench never relaxes verification.

This is the **bench's** dial, not a production egress boundary. Production's
pool boundary is :mod:`creek.classify.llm.pool_boundary` (#1894); when the
live pool run lands (#1899) this client is the obvious thing to retire in its
favour, so the measured path and the served path are the same code.
"""

from __future__ import annotations

import httpx


def pool_http_client(
    timeout: float,
    *,
    base_url: str,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Return the bench's pool client: proxy-free and redirect-free.

    Args:
        timeout: HTTP timeout in seconds, applied to every phase of the call.
        base_url: The pool endpoint relative request paths resolve against.
        transport: Injected transport; the hermetic fake pool supplies one.

    Returns:
        A client that reads no proxy variable and follows no redirect.
    """
    return httpx.Client(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        trust_env=False,
        follow_redirects=False,
    )
