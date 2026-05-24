"""Compiled-layer routing helpers (FEAT-004).

``creek mine`` and ``creek draft`` route through the compiled layer
first — Threads, Eddies, and per-frequency index notes — falling back
to fragments only when the compiled page is missing or insufficient.
The fallback is an operational signal, not silent: every miss appends
a ``compile-needed`` entry to ``00-Creek-Meta/Processing-Log/
compile-gaps.jsonl`` so ``creek lint`` can surface the gap later.

This module is the single read-side surface for the compiled layer.
Mining and drafting both consume it; the CLI's ``--bypass-compiled``
escape hatch short-circuits the index load entirely.

Trust assumption: the scanned directories ``02-Threads/``,
``03-Eddies/``, and ``06-Frequencies/`` are treated as trusted output
written by ``creek compile`` (FEAT-003). ``_load_pages`` uses
``rglob("*.md")``, which follows symlinks; the ``type: compiled_page``
sentinel plus Pydantic validation make planted non-pages a no-op in
practice, but the routing layer should not be pointed at untrusted
content.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path  # runtime use in dataclass / type hints

import frontmatter
import yaml
from pydantic import ValidationError

from creek.models import CompiledPage

logger = logging.getLogger(__name__)


COMPILE_GAPS_RELPATH: Path = Path("00-Creek-Meta/Processing-Log/compile-gaps.jsonl")
"""Vault-relative path of the compile-gaps log."""

_THREAD_DIR: Path = Path("02-Threads")
_EDDY_DIR: Path = Path("03-Eddies")
_FREQ_DIR: Path = Path("06-Frequencies")


@dataclass(frozen=True)
class CompiledPageIndex:
    """Compiled-layer pages keyed by ``(target_kind, target_id)``.

    Attributes:
        threads: ``{thread_id: CompiledPage}`` for every page under
            ``02-Threads/``.
        eddies: ``{eddy_id: CompiledPage}`` for every page under
            ``03-Eddies/``.
        frequency_indexes: ``{frequency_id: CompiledPage}`` for every
            page under ``06-Frequencies/``.
        bypassed: ``True`` when the index was constructed in bypass
            mode — every lookup returns a miss and miss-logging is
            suppressed (the ``--bypass-compiled`` escape hatch).
    """

    threads: dict[str, CompiledPage] = field(default_factory=dict)
    eddies: dict[str, CompiledPage] = field(default_factory=dict)
    frequency_indexes: dict[str, CompiledPage] = field(default_factory=dict)
    bypassed: bool = False

    def thread(self, target_id: str) -> CompiledPage | None:
        """Return the compiled thread page for *target_id*, or ``None``."""
        if self.bypassed:
            return None
        return self.threads.get(target_id)

    def eddy(self, target_id: str) -> CompiledPage | None:
        """Return the compiled eddy page for *target_id*, or ``None``."""
        if self.bypassed:
            return None
        return self.eddies.get(target_id)

    def frequency_index(self, target_id: str) -> CompiledPage | None:
        """Return the compiled frequency-index page, or ``None``."""
        if self.bypassed:
            return None
        return self.frequency_indexes.get(target_id)

    def fragment_ids_for(self, page: CompiledPage) -> tuple[str, ...]:
        """Return the unique fragment IDs traced by *page*'s provenance.

        Uses ``dict.fromkeys`` for O(n) deduplication; insertion order
        is guaranteed by the Python 3.7+ language spec.
        """
        return tuple(
            dict.fromkeys(
                fid for entry in page.provenance for fid in entry.fragment_ids if fid
            ),
        )


def empty_index(*, bypassed: bool = False) -> CompiledPageIndex:
    """Return an empty :class:`CompiledPageIndex`.

    Args:
        bypassed: When ``True``, the returned index reports every
            lookup as a miss without recording a gap. Used by the
            ``--bypass-compiled`` escape hatch.
    """
    return CompiledPageIndex(bypassed=bypassed)


def load_compiled_pages(vault_path: Path) -> CompiledPageIndex:
    """Scan *vault_path* and return every compiled-layer page.

    Pages are loaded from ``02-Threads/``, ``03-Eddies/``, and
    ``06-Frequencies/``. Files missing a ``type: compiled_page``
    sentinel or failing schema validation are skipped silently.

    Args:
        vault_path: Vault root.

    Returns:
        A populated :class:`CompiledPageIndex`. When the relevant
        directories do not exist the index is empty (``bypassed=False``
        so misses are still logged downstream).
    """
    return CompiledPageIndex(
        threads=_load_pages(vault_path / _THREAD_DIR, "thread"),
        eddies=_load_pages(vault_path / _EDDY_DIR, "eddy"),
        frequency_indexes=_load_pages(vault_path / _FREQ_DIR, "frequency_index"),
    )


def _load_pages(root: Path, target_kind: str) -> dict[str, CompiledPage]:
    """Load every compiled page of *target_kind* under *root*."""
    if not root.exists():
        return {}
    out: dict[str, CompiledPage] = {}
    for md_file in sorted(root.rglob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except (OSError, ValueError, yaml.YAMLError):
            logger.debug("Skipping unreadable compiled page: %s", md_file)
            continue
        metadata = post.metadata.copy()
        if metadata.get("type") != "compiled_page":
            continue
        if metadata.get("target_kind") != target_kind:
            continue
        metadata.setdefault("body", post.content)
        try:
            page = CompiledPage.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid compiled page: %s", md_file)
            continue
        out[page.target_id] = page
    return out


def log_compile_gap(
    vault_path: Path,
    *,
    target_kind: str,
    target_id: str,
    surfaced_by: str,
    reason: str,
) -> None:
    """Append a ``compile-needed`` entry to ``compile-gaps.jsonl``.

    The log is plain JSONL (no chain hash) — it's an operational
    backlog ``creek lint`` reads later, not a tamper-evidence record.

    Args:
        vault_path: Vault root.
        target_kind: ``"thread"``, ``"eddy"``, or ``"frequency_index"``.
        target_id: Stable ID of the missing compiled-layer surface.
        surfaced_by: Verb that hit the gap (``"mine"`` or ``"draft"``)
            plus an optional strategy/context tag.
        reason: Short human-readable reason — typically
            ``"missing"`` or ``"insufficient"``.
    """
    record = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "target_kind": target_kind,
        "target_id": target_id,
        "surfaced_by": surfaced_by,
        "reason": reason,
    }
    log_path = vault_path / COMPILE_GAPS_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
