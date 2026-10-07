"""Hermetic model-capacity, latency and per-account cost harness (#1850).

The harness measures whether a local model can serve ``creek.reflect`` inside
the published request deadline on a given allocation, and models what that
allocation costs per account-month. It is the evidence-gathering half of the
D04 capacity decision; it never makes that decision and never changes a
runtime default.

Three properties are load-bearing:

- **Why it lives in ``creek_mcp``.** It drives the real
  :func:`creek_mcp.tools.reflect.reflect_tool` path (grounding plus generation)
  and reads the server's deadline from
  :mod:`creek_mcp.httpapi.middleware.limits`. The domain package ``creek`` may
  never import ``creek_mcp`` (#1032, pinned by
  ``tests/test_adepthood_contract_models.py``), so the harness cannot sit there
  and is run as ``python -m creek_mcp.bench`` (``scripts/bench.sh``) rather
  than as a ``creek`` subcommand.
- **Hermetic and local-only.** The default ``fake`` mode needs no model, key or
  network. The ``live`` mode targets an operator-supplied Ollama endpoint, and
  every provider is wrapped in :class:`~creek_mcp.bench.local_only.LocalOnlyFactory`,
  which refuses a cloud provider before any trial — there is no silent cloud
  fallback.
- **Content-free.** The corpus is synthetic, confined to a temp directory, and
  never a real vault. The report carries only enums, numbers and
  pattern-constrained identifiers: no prompt, no entry text, no model output.

Cost figures are a **model** — arithmetic over an operator-supplied price
sheet — never a benchmark; :class:`~creek_mcp.bench.cost.CostEstimate` says so
in its ``kind`` field.
"""

from __future__ import annotations

from typing import Final

HARNESS_VERSION: Final[str] = "1.0.0"
"""Version of the harness and its report schema, recorded in every run."""
