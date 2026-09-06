"""Shared same-byte-length rewrite helper for the audit-log tamper tests.

Placement facts verified before this file was written:

* ``tests/__init__.py`` exists, so ``tests`` is a real package.
* Seven sibling modules already establish the non-``test_`` helper
  convention: ``adapter_parity.py``, ``archive_export_support.py``,
  ``elevated_attempt_support.py``, ``markdown_integrity_support.py``,
  ``shell_command_support.py``, ``synchronicity_support.py`` and
  ``v1_api_support.py``.
* ``from tests.<module> import ...`` is the established spelling
  (``tests/test_v1_api_conformance.py``, ``tests/test_mcp_auth.py``).
* ``scripts/lint-vulture.sh`` scans ``creek/`` and ``creek_mcp/`` while
  treating ``tests/`` as a *reference source*, so a helper module here is
  not a dead-code target.

Why one shared helper rather than one per test module: a byte-length
preserving rewrite is the only rewrite ``AuditLog``'s size-only cache
check cannot see. Every test of that residual is silently vacuous unless
the rewrite really preserves the byte length — a one-byte drift
invalidates the cache *legitimately*, the next append rescans, and the
test then passes for a reason unrelated to what it claims. Keeping the
byte-length assertion inside one helper makes that failure mode
unrepresentable across both test modules instead of relying on every
author remembering to re-assert it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def rewrite_last_line_preserving_size(
    path: Path,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Rewrite *path*'s last JSONL line in place without changing its size.

    Simulates an out-of-band tamper: the file is edited by something other
    than the ``AuditLog`` instance that wrote it, so no cache is updated.

    ``json.dumps(..., sort_keys=True)`` matches ``AuditLog.append``'s own
    serialisation, so a transform that swaps equal-length values round-trips
    to exactly the same byte count.

    Args:
        path: The JSONL log to rewrite.
        transform: Maps the decoded last entry to its replacement. It must
            preserve the serialised byte length; the assertion below is what
            enforces that, and it is the point of this helper.

    Raises:
        AssertionError: If the rewrite changed the file's byte length, which
            would make the calling test vacuous.
    """
    size_before = path.stat().st_size
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = json.dumps(transform(json.loads(lines[-1])), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    size_after = path.stat().st_size
    assert size_after == size_before, (
        f"rewrite must preserve byte length or the test is vacuous: "
        f"{size_before} bytes before, {size_after} bytes after"
    )
