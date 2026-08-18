"""Framework-free contract layer for the Adepthood HTTP application API (#1072).

This package holds the *vocabulary* of the ``/v1`` surface Adepthood consumes,
and nothing else. Two modules:

- :mod:`creek_mcp.api.models` — the Pydantic v2 wire models, enums and the
  three error tables (status / message / retry disposition).
- :mod:`creek_mcp.api.bundle` — the deterministic, byte-reproducible fixture
  bundle (JSON Schemas + a 5x7 example matrix + a manifest) published under
  ``docs/contracts/adepthood-v1/`` for the cross-repo consumer.

**No web framework may be imported anywhere under this package.** #1074 picks
the framework and mounts these models behind it; the contract must not
presuppose that choice, so ``fastapi``/``starlette``/``uvicorn``/``httpx`` are
all forbidden here and a test AST-checks every module for them. Pure Pydantic
v2 plus the standard library, plus ``creek.*`` / ``creek_mcp.*``.

The layering rule of #1032 runs the other way and still holds: ``creek/`` may
never import ``creek_mcp``. This package sits inside ``creek_mcp``, so reading
``creek.*`` constants is fine; adding an import in the opposite direction is
not.

Deliberately *not* here: any reader of a fragment's ``privacy_tier``. #1079
settled that Creek has exactly two tier readers —
:mod:`creek_mcp.tier_ceiling` for admission at the MCP boundary and
:mod:`creek.classify.privacy_filter` for body-level filtering — and this
package adds no third. Everything below is declarative.
"""
