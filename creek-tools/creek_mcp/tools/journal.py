"""``creek.journal`` MCP tool — Adepthood journal entry → fragment (#754).

Lets an Adepthood journal entry (raw content + timestamp + tier + a STABLE
external id) flow into the vault as a fragment so it is classified, linked, and
available to ``reflect``/``wheel`` — reusing the existing ledger-backed markdown
ingest rather than a parallel pipeline.

Idempotency + edit-in-place come for free from the shared
:func:`creek.ingest.pipeline.run_ingest`: the entry is staged as a markdown file
at a **stable path derived from the external id** (under a ``…/journal/…`` dir so
it is classified as :class:`~creek.models.SourcePlatform.JOURNAL`), so the source
ledger keys on that stable path. Re-sending the same entry is a no-op; an edited
entry with the same external id rewrites the fragment in place (preserving its id
and classifications), never orphaning a duplicate.

Tier is honored end-to-end: the tier is written into the staged entry's
frontmatter (which the markdown ingestor carries onto the fragment), and the
write-side ceiling refuses an entry whose tier exceeds the caller's ceiling — so
an INTIMATE entry is stored intimate and never admitted under a lower ceiling.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

from creek.ingest.markdown import MarkdownIngestor
from creek.ingest.pipeline import derive_source_key, ledger_for_source, run_ingest
from creek.models import PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, write_tier_allowed

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.ingest.pipeline import IngestRunResult

    # The ingest runner seam — production is ``run_ingest``; tests inject a stub
    # to exercise the failure path.
    _Runner = Callable[..., IngestRunResult]

TOOL_NAME = "creek.journal"
_SOURCE_TYPE = "markdown"
# A stable per-vault home for staged Adepthood entries. The ``journal`` segment
# makes the markdown ingestor classify them as SourcePlatform.JOURNAL; the
# ``adepthood`` segment identifies the source in each fragment's origin_key.
_JOURNAL_INBOX = Path("00-Creek-Meta/adepthood/journal")
# Where JOURNAL-platform fragments are routed by the writer — recorded as the
# audit ``created_path`` (mirrors ingest_tool's dir-level created_path).
_FRAGMENT_ROUTING_DIR = Path("01-Fragments/Journal")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_stem(external_id: str) -> str:
    """Return a filesystem-safe, *collision-free*, stable stem for *external_id*.

    A readable slug prefix aids debugging, but the trailing hash of the RAW
    external id is what guarantees the mapping is injective — two distinct ids
    that slug to the same string (e.g. ``"a/b"`` and ``"a-b"``) still get
    distinct stems, so the idempotency key never collides. Deterministic, so the
    same external id always resolves to the same staged path (hence the same
    ledger source-key → idempotent re-send / edit-in-place).
    """
    slug = _SLUG_RE.sub("-", external_id).strip("-")[:80]
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}" if slug else digest


def _stage_entry(
    vault_path: Path, external_id: str, content: str, timestamp: str, tier: PrivacyTier
) -> Path:
    """Write the entry to its stable staged markdown path and return it."""
    staged = vault_path / _JOURNAL_INBOX / f"{_safe_stem(external_id)}.md"
    staged.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content,
        privacy_tier=tier.value,
        date=timestamp,
        source_id=external_id,
    )
    staged.write_text(frontmatter.dumps(post), encoding="utf-8")
    return staged


def _resolve_fragment_id(vault_path: Path, staged: Path) -> str | None:
    """Return the fragment id the ledger mapped this staged entry to."""
    ledger = ledger_for_source(_SOURCE_TYPE, vault_path)
    if ledger is None:
        return None
    record = ledger.get(derive_source_key(str(staged), vault_path))
    return record.fragment_id if record is not None else None


def _action_of(result: IngestRunResult) -> str:
    """Collapse the single-entry run tally into one action word."""
    if result.created:
        return "created"
    if result.updated:
        return "updated"
    return "unchanged"


def journal_ingest_tool(
    *,
    vault_path: Path,
    content: str,
    external_id: str,
    timestamp: str | None = None,
    tier: str = "open",
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    run: _Runner | None = None,
) -> dict[str, Any]:
    """Ingest one Adepthood journal entry as a vault fragment (idempotently).

    Args:
        vault_path: Vault root.
        content: The journal entry body.
        external_id: The Adepthood-side stable id — the idempotency key. The same
            id updates in place; a new id creates a new fragment.
        timestamp: ISO-8601 entry time; defaults to now (UTC) when absent.
        tier: The entry's privacy tier (``open``/``personal``/``intimate``).
        privacy_tier_ceiling: The caller's admission ceiling — an entry whose
            tier exceeds it is refused, not downgraded.
        consumer: Free-form consumer id for the audit log.

    Returns:
        ``{status, tool, tier_ceiling, external_id, fragment_id, action, tier}``
        on success (``action`` ∈ ``created``/``updated``/``unchanged``), or a
        structured refusal.
    """
    # Malformed calls (blank content/id, unknown tier) refuse without an audit
    # entry, mirroring save_tool/ingest_tool — there is no meaningful tier to
    # record yet. The tier-ceiling refusal and success DO audit (below), since
    # the audit trail is how a privacy-tier violation would be investigated.
    if not content.strip() or not external_id.strip():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason="content and external_id are required",
        )
    try:
        entry_tier = PrivacyTier(tier)
    except ValueError:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"unknown tier {tier!r}",
        )

    audit_args = {"external_id": external_id[:64], "tier": tier}
    if not write_tier_allowed(entry_tier, privacy_tier_ceiling):
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args=audit_args,
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"entry tier {entry_tier.value} exceeds the ceiling",
        )

    ts = timestamp or datetime.now(UTC).isoformat()
    staged = _stage_entry(vault_path, external_id, content, ts, entry_tier)
    runner = run if run is not None else run_ingest
    try:
        result = runner(
            ingestor_cls=MarkdownIngestor,
            source_type=_SOURCE_TYPE,
            input_path=staged,
            vault_path=vault_path,
        )
    except FileNotFoundError:
        return refusal_response(
            tool=TOOL_NAME, ceiling=privacy_tier_ceiling, reason="vault unavailable"
        )
    if result.errors:
        # Content was staged and tier-allowed but the write failed — audit the
        # attempt (with its tier) so the failure leaves a trace, like ingest_tool.
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args=audit_args,
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
            created_tier=entry_tier.value,
        )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"ingest failed: {result.errors[0]}",
        )

    fragment_id = _resolve_fragment_id(vault_path, staged)
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args=audit_args,
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=str(_FRAGMENT_ROUTING_DIR),
        created_tier=entry_tier.value,
        affected_fragment_ids=[fragment_id] if fragment_id else [],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "external_id": external_id,
        "fragment_id": fragment_id,
        "action": _action_of(result),
        "tier": entry_tier.value,
    }
