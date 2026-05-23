"""Compiled-layer page factories for tests (FEAT-004 follow-up #215).

Three independent test modules (``test_compile_routing``,
``test_mining``, ``test_drafts``) previously each defined their own
``_write_compiled_*_page`` helpers with subtly different signatures and
target paths. When ``CompiledPage`` grows a new field the per-module
copies all need to learn it; consolidating them here keeps the on-disk
shape in one place.

The factory exposes two layers:

* :func:`build_compiled_page` — pure constructor that returns a
  :class:`~creek.models.CompiledPage` with deterministic provenance.
* :func:`write_compiled_page` (plus the per-kind ``write_compiled_*``
  convenience wrappers) — persists a page to a vault path the way
  ``creek compile`` does (frontmatter + body).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003  # runtime use in signatures

import frontmatter

from creek.compile.provenance import ProvenanceEntry
from creek.models import CompiledPage

_DEFAULT_COMPILED_AT = datetime(2026, 4, 1, tzinfo=UTC)
_DEFAULT_COMPILED_SUBFOLDER = "Compiled"


def build_compiled_page(
    *,
    target_kind: str,
    target_id: str,
    title: str | None = None,
    body: str | None = None,
    fragment_ids: tuple[str, ...] = (),
    compiled_at: datetime = _DEFAULT_COMPILED_AT,
) -> CompiledPage:
    """Return a :class:`CompiledPage` with deterministic provenance.

    Args:
        target_kind: One of ``"thread" | "eddy" | "frequency_index"``.
        target_id: Stable ID of the compiled surface.
        title: Page title; defaults to ``target_id`` when omitted.
        body: Page body; defaults to a ``# {title}\\n`` heading.
        fragment_ids: Each ID becomes its own
            :class:`~creek.compile.provenance.ProvenanceEntry`, so the
            length determines how many claims the page carries.
        compiled_at: Timestamp stamped on every provenance entry.
    """
    page_title = title if title is not None else target_id
    page_body = body if body is not None else f"# {page_title}\n"
    provenance = [
        ProvenanceEntry(
            claim_id=f"claim-{i}",
            claim_excerpt=f"excerpt {i}",
            fragment_ids=[fid],
            compiled_at=compiled_at,
            compile_method="llm",
        )
        for i, fid in enumerate(fragment_ids, start=1)
    ]
    return CompiledPage(
        target_kind=target_kind,
        target_id=target_id,
        title=page_title,
        body=page_body,
        provenance=provenance,
    )


def write_compiled_page(page: CompiledPage, target: Path) -> Path:
    """Persist *page* at *target* in the same shape ``creek compile`` writes."""
    metadata = page.model_dump(mode="json", exclude={"body"})
    target.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content=page.body, **metadata)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def write_compiled_thread_page(
    vault: Path,
    *,
    target_id: str,
    title: str | None = None,
    body: str | None = None,
    fragment_ids: tuple[str, ...] = (),
    subfolder: str = _DEFAULT_COMPILED_SUBFOLDER,
) -> Path:
    """Persist a compiled thread page under ``02-Threads/<subfolder>/``.

    The default ``subfolder="Compiled"`` keeps the compiled page from
    colliding with a thread-frontmatter file that lives at the canonical
    ``02-Threads/Active/`` path in the same fixture.
    """
    page = build_compiled_page(
        target_kind="thread",
        target_id=target_id,
        title=title,
        body=body,
        fragment_ids=fragment_ids,
    )
    target = vault / "02-Threads" / subfolder / f"{target_id}.md"
    return write_compiled_page(page, target)


def write_compiled_eddy_page(
    vault: Path,
    *,
    target_id: str,
    title: str | None = None,
    body: str | None = None,
    fragment_ids: tuple[str, ...] = (),
    subfolder: str = _DEFAULT_COMPILED_SUBFOLDER,
) -> Path:
    """Persist a compiled eddy page under ``03-Eddies/<subfolder>/``."""
    page = build_compiled_page(
        target_kind="eddy",
        target_id=target_id,
        title=title,
        body=body,
        fragment_ids=fragment_ids,
    )
    target = vault / "03-Eddies" / subfolder / f"{target_id}.md"
    return write_compiled_page(page, target)


def write_compiled_frequency_page(
    vault: Path,
    *,
    target_id: str,
    title: str | None = None,
    body: str | None = None,
    fragment_ids: tuple[str, ...] = (),
) -> Path:
    """Persist a compiled frequency-index page under ``06-Frequencies/``."""
    page = build_compiled_page(
        target_kind="frequency_index",
        target_id=target_id,
        title=title if title is not None else f"{target_id} index",
        body=body,
        fragment_ids=fragment_ids,
    )
    target = vault / "06-Frequencies" / f"{target_id}.md"
    return write_compiled_page(page, target)
