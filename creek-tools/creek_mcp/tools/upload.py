"""``creek.upload`` MCP tool — Adepthood document bytes → fragment (#1023).

Lets Adepthood hand one document's bytes (base64 + a filename + a STABLE
external id) to the vault, exactly as :mod:`creek_mcp.tools.journal` does for
text. The bytes are staged verbatim under
:data:`creek.ingest.journal_staging.UPLOAD_STAGING_RELDIR`, dispatched by
extension through :func:`creek.ingest.route_to_ingestor`, and ingested by the
ordinary ledger-backed :func:`creek.ingest.pipeline.run_ingest` — so
idempotent re-send, edit-in-place, ``source.origin_key`` and therefore RTBF
coverage all come from the shared pipeline rather than a parallel one.

Three limits are stated here rather than left for a reader to discover:

1. **Staged upload bytes are NOT redacted before ingest.** The ingest
   pipeline offers no redaction hook — ``creek.redact`` is a separate,
   scan-only surface, and ``creek redact --apply`` would corrupt a ``.docx``
   or ``.xlsx`` ZIP container outright — so a document's plaintext sits under
   the staging dir until it is purged. Issue #1228 tracks the hook; until it
   lands, the mitigation is the privacy tier plus the RTBF sweep, not
   redaction.
2. **A binary fragment's tier arrives only through**
   ``run_ingest(privacy_tier=...)``. No ingestor emits ``privacy_tier`` and a
   ``.docx`` / ``.pdf`` / ``.xlsx`` has no frontmatter to carry one, so the
   caller's declared tier is the only channel — and it is escalate-only, so
   an uploaded ``.md`` that declares ``intimate`` in its own frontmatter is
   never lowered to the caller's ``open``. It is also effectively a
   **create-time** channel: :meth:`creek.vault.writer.VaultWriter.update_fragment`
   preserves the on-disk frontmatter by design and re-derives the tier
   escalate-only, so re-uploading at a *higher* tier does not rewrite the
   persisted tier through this path.
3. **Image uploads are dispatched but not tested end to end.** ``.png`` and
   friends route to the ``image`` ingestor, whose OCR engine needs the
   tesseract system binary and has no injection seam through ``run_ingest``,
   so an end-to-end test would be environment-dependent. Routing is tested;
   the OCR leg is not.

Gate ordering, all of it load-bearing and asserted from outside:

1. malformed calls (blank ``filename`` / ``external_id`` / ``content_base64``,
   unknown ``tier``) refuse without an audit entry — there is no meaningful
   tier to record yet;
2. ``write_tier_allowed`` on the *incoming* tier — audited, then refused;
3. the encoded-length cap, then the decode, then the decoded-size cap;
4. the #970 overwrite gate on the tier of the *resolved existing* fragment —
   audited, then refused;
5. **only then** :func:`_stage_upload` writes anything.

Gate 2 sitting above gate 3 is the sharp edge: an ``intimate`` upload at
``ceiling=open`` must never decode a byte, and a refused overwrite must leave
the previously staged bytes untouched — a gate placed after the staging write
returns a correct refusal over an already-destroyed staged document.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from creek.classify.privacy_filter import max_source_tier, source_tiers
from creek.ingest import INGESTOR_REGISTRY, route_to_ingestor
from creek.ingest.journal_staging import UPLOAD_LEDGER_SOURCE, UPLOAD_STAGING_RELDIR
from creek.ingest.ledger import SourceLedger
from creek.ingest.pipeline import derive_source_key, run_ingest
from creek.ingest.source_unit import split_source_unit
from creek.models import PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.read_gate import refuse_above_ceiling
from creek_mcp.staged_names import safe_stem, safe_suffix
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, write_tier_allowed

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek.ingest.pipeline import IngestRunResult

    # The ingest runner seam — production is ``run_ingest``; tests inject a
    # stub to exercise the failure path.
    _Runner = Callable[..., IngestRunResult]

TOOL_NAME = "creek.upload"

MAX_UPLOAD_BYTES: Final[int] = 10 * 1024 * 1024
"""Largest decoded document this tool will stage."""

_MAX_ENCODED_CHARS: Final[int] = 4 * ((MAX_UPLOAD_BYTES + 2) // 3)
"""Pre-decode guard on the base64 string.

Base64 of *N* bytes is exactly ``4 * ceil(N / 3)`` characters, and
``validate=True`` rejects embedded whitespace, so any longer string MUST
decode above :data:`MAX_UPLOAD_BYTES`. Checking the encoded length first is
what stops a remote caller forcing a 100 MB allocation to learn that its
upload was too big.
"""

# Where uploaded fragments are routed by the writer — recorded as the audit
# ``created_path``. Deliberately dir-level (matching ingest_tool) rather than
# per-platform: a per-platform table here would duplicate and drift from
# ``creek/vault/writer.py``'s own routing map.
_FRAGMENT_ROUTING_DIR = Path("01-Fragments")


def _upload_staged_path(vault_path: Path, external_id: str, filename: str) -> Path:
    """Return the stable staged path this ``(external_id, filename)`` maps to.

    Pure — it computes, and never creates. Split out for the same reason
    :func:`creek_mcp.tools.journal._staged_path` was (#970): the overwrite
    gate has to derive the ledger source key *before* deciding whether the
    caller may write anything at all.

    Args:
        vault_path: Vault root.
        external_id: The caller's idempotency key.
        filename: The caller's original filename; only its extension survives.

    Returns:
        ``<vault>/00-Creek-Meta/adepthood/uploads/<safe stem><safe suffix>``.
    """
    stem = safe_stem(external_id)
    return vault_path / UPLOAD_STAGING_RELDIR / f"{stem}{safe_suffix(filename)}"


def _stage_upload(
    vault_path: Path, external_id: str, filename: str, raw: bytes
) -> Path:
    """Write *raw* to its stable staged path and return it, skipping no-op writes.

    The identical-bytes short-circuit avoids a pointless write on an
    idempotent re-send: an ingestor without an embedded date derives its parse
    timestamp from the file's ``st_mtime``, and
    :func:`creek.ingest.base.generate_fragment_id` hashes that timestamp, so
    rewriting the same bytes would bump the mtime and derive a different id.

    It is no longer load-bearing against *duplication*, and this docstring
    should not be read as claiming otherwise. Uploads run under a borrowed
    ledger (``UPLOAD_LEDGER_SOURCE``), and since #1329
    ``write_fragment_idempotent`` assigns ``fragment.id = record.fragment_id``
    on **every** branch — including the unchanged one, which previously wrote
    whatever id had just been derived. A shifted derivation is therefore
    absorbed by the ledger rather than turning into an orphan. Keeping the
    short-circuit is still worth it; keeping a comment that asserts a hazard
    the ledger now closes is how the hazard gets reopened.

    Args:
        vault_path: Vault root.
        external_id: The caller's idempotency key.
        filename: The caller's original filename.
        raw: The decoded document bytes, written verbatim in binary mode.

    Returns:
        The staged path, whose bytes now equal *raw*.
    """
    staged = _upload_staged_path(vault_path, external_id, filename)
    staged.parent.mkdir(parents=True, exist_ok=True)
    if staged.is_file() and staged.read_bytes() == raw:
        return staged
    staged.write_bytes(raw)
    return staged


def _resolve_fragment_ids(vault_path: Path, staged: Path) -> list[str]:
    """Return every fragment id the upload ledger maps this staged file to.

    Plural since #1305. One staged document can hold several independently
    identified units — an uploaded ``.xlsx`` routes to ``SpreadsheetIngestor``,
    which emits one fragment per sheet and ledgers each under its own
    ``<staged path>#<sheet>`` key. The singular predecessor asked
    :meth:`~creek.ingest.ledger.SourceLedger.get` for the bare staged path
    and got ``None`` for exactly those uploads, and every one of this
    module's four callers reads a ``None`` as *this id owns nothing*:

    * :func:`_refuse_unadmitted_overwrite` would compute ``existing=None``
      and admit a caller at ``ceiling=open`` to overwrite an intimate
      workbook — the #970 gate defeated;
    * :func:`_discard_unreferenced` would unlink a staged workbook that N
      live fragments still reach through ``origin_key``, which is the RTBF
      sweep's only route to those bytes;
    * :func:`_refuse_extension_conflict` would report no conflict and fork
      one ``external_id`` onto two live fragments;
    * the response and audit entry would name one sheet out of N.

    Args:
        vault_path: Vault root.
        staged: The staged file's path.

    Returns:
        The mapped fragment ids in ledger-key order, or ``[]`` when the
        ledger knows nothing about this staged file.
    """
    ledger = SourceLedger.load(vault_path, source=UPLOAD_LEDGER_SOURCE)
    base_key = derive_source_key(str(staged), vault_path)
    return [record.fragment_id for _key, record in ledger.records_for(base_key)]


def _existing_tier(vault_path: Path, fragment_ids: list[str]) -> PrivacyTier:
    """Return the most sensitive CURRENT vault tier across *fragment_ids*.

    A deliberate local twin of :func:`creek_mcp.tools.journal._existing_tier`,
    whose docstring carries the full rationale (read the tier out of the
    vault, not off the caller's argument; reduce an empty tier set to
    ``INTIMATE``; rely on the shared walk's constant cost so the refusal
    cannot leak *where* the protected fragment sits through timing). It is not
    imported from there because each tool module must itself call
    :func:`creek_mcp.read_gate.refuse_above_ceiling` for the read-gate
    manifest's AST check; de-duplicating the pair into a shared helper is a
    follow-up, not a change to make while the gate ordering is in flight.

    Takes the whole set an ``external_id`` owns rather than one id, because
    the same id can hold staged files under several extensions and the gate
    must rank against the most sensitive of them.

    Args:
        vault_path: Vault root.
        fragment_ids: Every id the upload ledger resolved for this
            ``external_id``.

    Returns:
        The most sensitive current tier, or ``INTIMATE`` when none of the ids
        resolves to a readable fragment.
    """
    return max_source_tier(source_tiers(vault_path, fragment_ids))


def _ledgered_names_for_id(vault_path: Path, external_id: str) -> dict[str, list[str]]:
    """Return the fragment ids the upload LEDGER holds for *external_id* (#1305).

    Grouped by staged filename, so a caller can ask both "does this id own
    anything" and "does it own something under a *different* extension".

    The ledger, not the staging directory, is the authority on what an
    ``external_id`` owns — and after #1305 the difference is a privacy
    ratchet rather than a nicety.

    Purging **one** sheet of an N-sheet upload deletes the shared staged
    workbook (that is the RTBF contract: the source bytes go) while the
    other N-1 sheet fragments stay live, still ledgered, still possibly
    ``intimate``. A survey keyed on :meth:`~pathlib.Path.iterdir` then sees
    an empty staging dir, reports that the id owns nothing, and lets a
    caller at ``ceiling=open`` through as a *creation*. The re-upload
    restages to the same deterministic path, ``write_fragment_idempotent``
    matches the surviving siblings by their intact ledger keys, and
    overwrites intimate fragments with the low-ceiling caller's bytes —
    reachable through two individually legitimate operations.

    A ledger record survives the file it points at, so this survey does not
    go blind at exactly the moment it matters.

    Tombed records are counted too (:meth:`~creek.ingest.ledger.SourceLedger.all_keys`,
    not ``live_keys``): a soft-tombed fragment still exists, under
    ``10-Liminal/Orphaned/``, and for a gate whose failure mode is
    *admitting* a caller, over-counting is the safe error.

    Args:
        vault_path: Vault root.
        external_id: The caller's idempotency key.

    Returns:
        Staged filename mapped to the fragment ids ledgered under it (the
        bare key and every ``…#<unit>`` child), ordered by ledger key.
        Empty when the ledger knows nothing about this id.
    """
    stem = safe_stem(external_id)
    staging = UPLOAD_STAGING_RELDIR.as_posix()
    grouped: dict[str, list[str]] = {}
    ledger = SourceLedger.load(vault_path, source=UPLOAD_LEDGER_SOURCE)
    for key in sorted(ledger.all_keys()):
        base, _unit = split_source_unit(key)
        parent, _, name = base.rpartition("/")
        if parent != staging:
            continue
        if name != stem and not name.startswith(f"{stem}."):
            continue
        record = ledger.get(key)
        if record is not None:
            grouped.setdefault(name, []).append(record.fragment_id)
    return grouped


def _staged_for_id(vault_path: Path, external_id: str) -> list[Path]:
    """Return every staged upload bound to *external_id*, whatever its extension.

    The staged filename is ``<stem><suffix>``, so the ledger — which keys on
    that path — actually keys identity on ``(external_id, extension)`` rather
    than on ``external_id`` alone. This function recovers the id-only view the
    tool's contract promises, and is what stops a re-send under a changed
    extension from silently forking into a second, unrelated fragment while
    orphaning the first one's staged bytes (a hole in the RTBF coverage this
    tool exists to provide).

    Matching is on the full name, not :attr:`Path.stem`: ``safe_stem`` permits
    ``.`` in its slug, so ``Path("a.b-<digest>.pdf").stem`` is ``a.b-<digest>``
    and a stem comparison would mis-group unrelated ids.

    Args:
        vault_path: Vault root.
        external_id: The caller's idempotency key.

    Returns:
        Sorted staged paths, or ``[]`` when the staging dir does not exist.
    """
    stem = safe_stem(external_id)
    staging = vault_path / UPLOAD_STAGING_RELDIR
    if not staging.is_dir():
        return []
    return sorted(
        path
        for path in staging.iterdir()
        if path.is_file() and (path.name == stem or path.name.startswith(f"{stem}."))
    )


def _refuse_unadmitted_overwrite(
    vault_path: Path, external_id: str, filename: str, ceiling: TierCeiling
) -> dict[str, Any] | None:
    """Refuse an upload that would destroy content *ceiling* cannot read (#970).

    The tier surveyed is the **most sensitive** across every fragment this
    ``external_id`` already owns, not merely the one at the incoming
    extension's path. Surveying only the exact target path would let a caller
    at ``ceiling=open`` reach an id whose intimate fragment is staged under a
    different extension, and be admitted as a creation.

    Args:
        vault_path: Vault root.
        external_id: The caller's idempotency key.
        filename: Accepted for signature symmetry with the staging helpers;
            the survey is id-wide and deliberately does not depend on it.
        ceiling: The caller's admission ceiling.

    Returns:
        ``None`` when the write is admitted — including when the id maps to
        no ledger record at all, which is a *creation* and must keep working
        at every ceiling. Otherwise the canonical four-key refusal, which
        names neither the resolved fragment nor its tier.
    """
    del filename
    # Surveyed from the LEDGER, not from a staging-directory listing
    # (#1305). One staged workbook owns one fragment per sheet, so the
    # survey is a flat union over units; and a ledger record outlives the
    # staged file, so purging one sheet — which deletes the shared workbook
    # while its siblings stay live and intimate — cannot blind this gate.
    ids = [
        fragment_id
        for fragment_ids in _ledgered_names_for_id(vault_path, external_id).values()
        for fragment_id in fragment_ids
    ]
    existing = None if not ids else _existing_tier(vault_path, ids)
    return refuse_above_ceiling(tool=TOOL_NAME, content_tier=existing, ceiling=ceiling)


def _discard_unreferenced(vault_path: Path, staged: Path) -> None:
    """Delete *staged* when the ingest it was written for produced no fragment.

    An ingest can refuse after the bytes are on disk — ``run_ingest`` reports
    ``written=0`` for content the binary heuristic drops or an extension no
    ingestor claims, and can report ``errors`` for a malformed document. The
    staged file is then left with **no ledger record**, and therefore no
    ``source.origin_key`` on any fragment.

    That is not merely untidy: the RTBF sweep finds staged bytes *through*
    ``origin_key`` (:meth:`creek.purge.engine.PurgeEngine._purge_staged_source_entry`),
    so an unreferenced staged file is unreachable by every purge the vault
    offers — a copy of the caller's document that survives the erasure meant
    to remove it. Deleting it here is what keeps "every staged upload is
    purgeable" true for the refusal paths as well as the happy one.

    Keyed on the ledger rather than on "did this call create the file", so a
    re-send whose ingest fails over an *already-recorded* staged file leaves
    that file alone — its fragment still references it.

    Args:
        vault_path: Vault root.
        staged: The staged path the failed ingest was pointed at.
    """
    # Deleted only when the ledger maps this staged path to NOTHING. A
    # workbook whose sheets are ledgered under `<path>#<sheet>` keys has an
    # empty *exact*-key lookup but a non-empty unit list, and unlinking it
    # would destroy the bytes N live fragments still reach through
    # `origin_key` (#1305).
    if not _resolve_fragment_ids(vault_path, staged):
        staged.unlink(missing_ok=True)


def _refuse_extension_conflict(
    vault_path: Path, external_id: str, filename: str, ceiling: TierCeiling
) -> dict[str, Any] | None:
    """Refuse a re-send that would fork *external_id* onto a second extension.

    The contract is that one ``external_id`` maps to one fragment. Because the
    ingestor is chosen from the staged file's suffix, the staged path — and so
    the ledger key — must carry that suffix, so a re-send under a different
    extension lands on a path with no ledger record and reads as a *creation*.
    The result would be two live fragments for one id, with the earlier one's
    staged bytes orphaned: still on disk, but no longer reachable from the
    ``fragment_id`` the caller holds, so a purge by that id leaves them behind.

    Refusing is the honest resolution. Genuinely re-typing a document means
    purging the existing fragment or choosing a new id, and either is one
    explicit call the caller can make.

    Runs **after** the #970 gate so it cannot answer a question that gate would
    have refused: the existence of a conflicting upload is disclosed only to a
    caller already admitted to that id's content.

    Args:
        vault_path: Vault root.
        external_id: The caller's idempotency key.
        filename: The caller's original filename, whose extension decides the
            staged path.
        ceiling: The caller's admission ceiling, echoed in the refusal.

    Returns:
        ``None`` when no conflicting extension is staged, otherwise a
        structured refusal naming the extension already bound to the id.
    """
    target = _upload_staged_path(vault_path, external_id, filename)
    # Ledger-backed only, and now read straight off the ledger rather than
    # off a directory listing (#1305). An unreferenced staged file is not a
    # fragment anyone can purge, so treating it as a conflict would refuse
    # with a remedy — "purge the existing fragment" — that cannot be carried
    # out. Conversely a ledgered fragment whose staged bytes a sibling's
    # purge already removed IS still a conflict, and a listing-based survey
    # would miss exactly that case.
    conflicting = [
        name
        for name in _ledgered_names_for_id(vault_path, external_id)
        if name != target.name
    ]
    if not conflicting:
        return None
    bound = ", ".join(
        sorted({Path(name).suffix or "(no extension)" for name in conflicting})
    )
    return refusal_response(
        tool=TOOL_NAME,
        ceiling=ceiling,
        reason=(
            f"external_id is already bound to {bound}; re-sending it as "
            f"{target.suffix or '(no extension)'} would create a second fragment "
            "and orphan the first one's staged bytes. Purge the existing fragment "
            "or use a new external_id."
        ),
    )


def _refuse_with_audit(
    *,
    vault_path: Path,
    audit_args: dict[str, Any],
    ceiling: TierCeiling,
    consumer: str,
    reason: str,
    created_tier: str | None = None,
) -> dict[str, Any]:
    """Record the attempt in the audit trail, then return its refusal.

    Every gate that fires *after* the call was well-formed enough to carry a
    tier audits before refusing, because the audit trail is how a
    privacy-tier violation would later be investigated.

    Args:
        vault_path: Vault root holding the MCP audit log.
        audit_args: The already-sanitised argument summary; the document
            bytes are never among its values.
        ceiling: The caller's admission ceiling, echoed in the refusal.
        consumer: Free-form consumer id for the audit log.
        reason: The refusal reason.
        created_tier: Tier of the content the call was attempting to write,
            recorded only once the attempt got as far as an ingest.

    Returns:
        The canonical four-key refusal payload.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args=audit_args,
        tier_ceiling=ceiling,
        consumer=consumer,
        created_tier=created_tier,
    )
    return refusal_response(tool=TOOL_NAME, ceiling=ceiling, reason=reason)


def _validated_upload(
    *,
    filename: str,
    external_id: str,
    content_base64: str,
    tier: str,
    ceiling: TierCeiling,
) -> PrivacyTier | dict[str, Any]:
    """Return the parsed upload tier, or the refusal a malformed call earns.

    Malformed calls refuse **without** an audit entry, mirroring ``save_tool``
    and :func:`creek_mcp.tools.journal._validated_entry_tier`: there is no
    meaningful tier to record yet.

    Args:
        filename: The caller's original filename.
        external_id: The caller's idempotency key.
        content_base64: The base64 payload, checked here only for emptiness.
        tier: The caller's declared tier, as the raw string it arrived as.
        ceiling: The caller's admission ceiling, echoed in a refusal.

    Returns:
        The parsed :class:`~creek.models.PrivacyTier`, or a structured
        refusal naming which malformation was found. Neither reason is
        derived from vault content, so neither is an oracle.
    """
    if not filename.strip() or not external_id.strip() or not content_base64.strip():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=ceiling,
            reason="filename, external_id and content_base64 are required",
        )
    try:
        return PrivacyTier(tier)
    except ValueError:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=ceiling,
            reason=f"unknown tier {tier!r}",
        )


def _admit_payload(
    *,
    vault_path: Path,
    audit_args: dict[str, Any],
    content_base64: str,
    upload_tier: PrivacyTier,
    ceiling: TierCeiling,
    consumer: str,
) -> bytes | dict[str, Any]:
    """Return the decoded document bytes, or the refusal that stops them.

    The order is the feature. ``write_tier_allowed`` runs **before** the
    decode, so an ``intimate`` upload at ``ceiling=open`` never turns a byte
    of above-ceiling content into memory; the encoded-length guard runs
    before it too, so an oversize payload is refused without the allocation
    it was trying to force.

    Args:
        vault_path: Vault root holding the MCP audit log.
        audit_args: The sanitised argument summary for the audit trail.
        content_base64: The caller's base64 payload.
        upload_tier: The parsed tier the caller declared.
        ceiling: The caller's admission ceiling.
        consumer: Free-form consumer id for the audit log.

    Returns:
        The decoded bytes, or a structured refusal. A malformed payload
        refuses *without* an audit entry, like the other malformations.
    """
    if not write_tier_allowed(upload_tier, ceiling):
        return _refuse_with_audit(
            vault_path=vault_path,
            audit_args=audit_args,
            ceiling=ceiling,
            consumer=consumer,
            reason=f"upload tier {upload_tier.value} exceeds the ceiling",
        )
    if len(content_base64) > _MAX_ENCODED_CHARS:
        return _refuse_with_audit(
            vault_path=vault_path,
            audit_args=audit_args,
            ceiling=ceiling,
            consumer=consumer,
            reason=f"encoded upload exceeds the {MAX_UPLOAD_BYTES}-byte cap",
        )
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except binascii.Error:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=ceiling,
            reason="content_base64 is not valid base64",
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        return _refuse_with_audit(
            vault_path=vault_path,
            audit_args=audit_args,
            ceiling=ceiling,
            consumer=consumer,
            reason=f"decoded upload exceeds the {MAX_UPLOAD_BYTES}-byte cap",
        )
    return raw


def _ingest_outcome(
    *,
    vault_path: Path,
    audit_args: dict[str, Any],
    staged: Path,
    source_type: str,
    upload_tier: PrivacyTier,
    ceiling: TierCeiling,
    consumer: str,
    run: _Runner | None,
) -> IngestRunResult | dict[str, Any]:
    """Run the ingest for the staged file, or return the refusal it earned.

    ``written == 0`` is a refusal, not a success. ``GenericIngestor`` returns
    no fragments for content its binary heuristic flags, and discovers
    nothing at all for an extension claimed by another ingestor (``.json``);
    ``run_ingest`` then reports ``written=0, errors=[]``, which is
    indistinguishable from success unless ``written`` is inspected. Reporting
    ``ok`` there would tell a caller its document is in the vault when the
    pipeline silently dropped it.

    Args:
        vault_path: Vault root.
        audit_args: The sanitised argument summary for the audit trail.
        staged: The staged file to ingest.
        source_type: Registry key chosen by ``route_to_ingestor``.
        upload_tier: The tier declared for this content.
        ceiling: The caller's admission ceiling.
        consumer: Free-form consumer id for the audit log.
        run: Ingest-runner seam; ``None`` selects
            :func:`creek.ingest.pipeline.run_ingest`.

    Returns:
        The :class:`~creek.ingest.pipeline.IngestRunResult` on success, or a
        structured refusal.
    """
    runner = run if run is not None else run_ingest
    try:
        result = runner(
            ingestor_cls=INGESTOR_REGISTRY[source_type],
            source_type=source_type,
            input_path=staged,
            vault_path=vault_path,
            ledger_source=UPLOAD_LEDGER_SOURCE,
            privacy_tier=upload_tier,
        )
    except FileNotFoundError:
        return refusal_response(
            tool=TOOL_NAME, ceiling=ceiling, reason="vault unavailable"
        )
    if result.errors:
        return _refuse_with_audit(
            vault_path=vault_path,
            audit_args=audit_args,
            ceiling=ceiling,
            consumer=consumer,
            reason=f"ingest failed: {result.errors[0]}",
            created_tier=upload_tier.value,
        )
    if result.written == 0:
        return _refuse_with_audit(
            vault_path=vault_path,
            audit_args=audit_args,
            ceiling=ceiling,
            consumer=consumer,
            reason=f"{source_type} ingest produced no fragment from this file",
            created_tier=upload_tier.value,
        )
    return result


def _action_of(result: IngestRunResult) -> str:
    """Collapse the single-file run tally into one action word."""
    if result.created:
        return "created"
    if result.updated:
        return "updated"
    return "unchanged"


def upload_tool(
    *,
    vault_path: Path,
    filename: str,
    content_base64: str,
    external_id: str,
    timestamp: str | None = None,
    tier: str = "open",
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    run: _Runner | None = None,
) -> dict[str, Any]:
    """Ingest one uploaded document as a vault fragment (idempotently).

    There is deliberately **no** ``source_type`` override. The ``chatgpt``,
    ``claude``, ``discord`` and ``substack`` ingestors are directory-only —
    each bails on a non-directory input — so an override would let a caller
    name an ingestor that discovers nothing from a single staged file and
    reports it as a silent no-op. Dispatch is by extension, through
    :func:`creek.ingest.route_to_ingestor`, and nothing else.

    Args:
        vault_path: Vault root.
        filename: The caller's original filename. Only its extension is
            trusted, and only to choose an ingestor.
        content_base64: The document's bytes, base64-encoded. Never written
            to the audit log — see :func:`creek_mcp.audit.summarise_args`,
            which passes any string of 64 characters or fewer through
            verbatim, so a small document handed to it would be logged whole.
        external_id: The Adepthood-side stable id — the idempotency key. The
            same id updates in place; a new id creates a new fragment. One id
            maps to exactly one fragment: because the ingestor is chosen from
            the staged file's extension, re-sending an id under a *different*
            extension is refused rather than allowed to fork into a second
            fragment that would orphan the first one's staged bytes.
        timestamp: ISO-8601 upload time, accepted for contract symmetry with
            ``creek.journal`` and echoed in the response only. It is **not**
            written into the staged file — a binary document has nowhere to
            put it — so the fragment's timestamp comes from the ingestor.
        tier: The document's privacy tier (``open``/``personal``/``intimate``).
        privacy_tier_ceiling: The caller's admission ceiling — an upload whose
            tier exceeds it is refused, not downgraded, and so is an update
            that would overwrite a fragment the ceiling could not read.
        consumer: Free-form consumer id for the audit log.
        run: Ingest-runner seam; production passes ``None`` and gets
            :func:`creek.ingest.pipeline.run_ingest`.

    Returns:
        ``{status, tool, tier_ceiling, external_id, filename, timestamp,
        source_type, fragment_id, affected_fragment_ids, action, tier,
        size_bytes}`` on
        success (``action`` ∈ ``created``/``updated``/``unchanged``), or a
        structured four-key refusal — carrying
        :data:`~creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON`, naming
        neither the protected fragment nor its tier, when the overwrite gate
        fires (#970).
    """
    upload_tier = _validated_upload(
        filename=filename,
        external_id=external_id,
        content_base64=content_base64,
        tier=tier,
        ceiling=privacy_tier_ceiling,
    )
    # A non-tier answer is the refusal a malformed call earned; from here down
    # ``upload_tier`` is narrowed to the parsed tier.
    if not isinstance(upload_tier, PrivacyTier):
        return upload_tier

    # A missing vault is refused BEFORE anything can recreate it. ``run_ingest``
    # would eventually raise ``FileNotFoundError`` and yield the same refusal,
    # but not before ``_stage_upload``'s ``mkdir(parents=True)`` had rebuilt the
    # staging tree and written the caller's bytes into a vault this very
    # response calls unavailable — ``SourceLedger.load`` returns an EMPTY ledger
    # rather than raising when the vault is gone, so the #970 gate reads the
    # call as a creation and admits it. Every other refusal here is
    # side-effect-free; this one was not. It is also above the audit append,
    # which would otherwise mint ``00-Creek-Meta/audit/`` in the same way.
    if not vault_path.is_dir():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason="vault unavailable",
        )

    # The payload is represented by its LENGTH and nothing else. Putting
    # ``content_base64`` in here would put the document in the audit log.
    audit_args = {
        "filename": filename[:64],
        "external_id": external_id[:64],
        "tier": tier,
        "encoded_len": len(content_base64),
    }
    raw = _admit_payload(
        vault_path=vault_path,
        audit_args=audit_args,
        content_base64=content_base64,
        upload_tier=upload_tier,
        ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    if not isinstance(raw, bytes):
        return raw

    # The #970 overwrite gate, above the staging write below it: a refusal
    # must leave the previously staged bytes intact as well as the fragment.
    # Its audit entry carries the caller's own arguments and nothing else —
    # no created_tier, no ids — because the resolved fragment id and the
    # protected tier must not enter a trail served onward through other
    # surfaces.
    refusal = _refuse_unadmitted_overwrite(
        vault_path, external_id, filename, privacy_tier_ceiling
    )
    if refusal is not None:
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args=audit_args,
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return refusal

    # Below the #970 gate, so a conflicting extension is disclosed only to a
    # caller already admitted to this id's content; above the staging write,
    # so the refusal leaves no second staged file behind.
    conflict = _refuse_extension_conflict(
        vault_path, external_id, filename, privacy_tier_ceiling
    )
    if conflict is not None:
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args=audit_args,
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
        )
        return conflict

    staged = _stage_upload(vault_path, external_id, filename, raw)
    source_type = route_to_ingestor(staged)
    result = _ingest_outcome(
        vault_path=vault_path,
        audit_args=audit_args,
        staged=staged,
        source_type=source_type,
        upload_tier=upload_tier,
        ceiling=privacy_tier_ceiling,
        consumer=consumer,
        run=run,
    )
    if isinstance(result, dict):
        _discard_unreferenced(vault_path, staged)
        return result

    # Every fragment the staged document produced, in ledger-key order, so
    # an RTBF request answered from this audit entry reaches all of them
    # (#1305). ``fragment_id`` keeps its singular contract by naming the
    # first in that order — deterministic across runs, and for the
    # one-fragment-per-file majority it is still simply *the* fragment.
    affected = _resolve_fragment_ids(vault_path, staged)
    fragment_id = affected[0] if affected else None
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args=audit_args,
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=str(_FRAGMENT_ROUTING_DIR),
        created_tier=upload_tier.value,
        affected_fragment_ids=affected,
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "external_id": external_id,
        "filename": filename,
        "timestamp": timestamp,
        "source_type": source_type,
        "fragment_id": fragment_id,
        "affected_fragment_ids": affected,
        "action": _action_of(result),
        "tier": upload_tier.value,
        "size_bytes": len(raw),
    }
