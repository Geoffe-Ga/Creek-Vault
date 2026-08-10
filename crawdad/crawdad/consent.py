"""Conversational consent flow for staged Discord attachments (FEAT-034).

After :func:`crawdad.bot._handle_attachments` stages a batch under
``<vault>/00-Creek-Meta/Inbound/<channel>/<message>/``, this module
records the batch on a per-channel :class:`PendingBatchStore`. The next
non-attachment message in the same channel is inspected for an
affirmative token (``ingest``, ``yes``, ``go ahead`` …) or an
abandonment token (``drop``, ``cancel`` …). On a consent match, the
bot's handler dispatches ``creek.ingest`` for every staged file,
grouped by inferred type so files that align share a single call.

Pending batches:

* expire after :data:`DEFAULT_PENDING_BATCH_TTL_SECONDS` so a stale
  "yes" cannot dispatch an unexpected ingest;
* are superseded by a new attachment turn (a fresh batch overwrites
  the prior one for that channel);
* survive *after* a successful ingest in the ``"ingested"`` state so
  that re-running consent against the same batch returns a clear
  "already ingested" reply instead of re-dispatching.

Type disambiguation: when one or more accepted files have
``inferred_type is None``, the bot asks for a type once the user
consents (rather than guessing). The next message naming a valid
:data:`VALID_INGEST_TYPES` value applies that type to every
previously-unresolved file and completes ingestion.
"""

from __future__ import annotations

import asyncio
import string
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Default consent-token set. ``ingest`` is the canonical word documented
# in :data:`crawdad.bot._INGEST_CONSENT_PROMPT`; the others cover common
# natural affirmations a personal-use Discord user is likely to type.
DEFAULT_CONSENT_TOKENS: frozenset[str] = frozenset(
    {"ingest", "yes", "go ahead", "proceed", "ok", "okay", "sure"}
)

# Default abandonment tokens. Hitting one of these clears the pending
# batch without dispatching ingest.
DEFAULT_ABANDON_TOKENS: frozenset[str] = frozenset(
    {"drop", "cancel", "nevermind", "never mind", "abort", "stop"}
)

# Pending batches expire 30 minutes after they were recorded. A stale
# "yes" in the channel after that window falls through to the regular
# agent loop instead of dispatching an unexpected ingest.
DEFAULT_PENDING_BATCH_TTL_SECONDS: float = 30 * 60.0

# Valid ``creek.ingest`` source_type values. Mirrors the registry types
# referenced by :data:`crawdad.attachments._EXTENSION_TO_INGESTOR_TYPE`.
# A type-disambiguation reply must normalise into one of these strings
# for the consent flow to apply it.
VALID_INGEST_TYPES: frozenset[str] = frozenset(
    {"markdown", "document", "generic", "spreadsheet", "presentation", "image"}
)


BatchState = Literal["awaiting_consent", "awaiting_type", "ingested"]
MessageKind = Literal["consent", "abandon", "type", "none"]


@dataclass(frozen=True)
class PendingFile:
    """One staged attachment awaiting ingest consent.

    Attributes:
        filename: Sanitised on-disk filename (relative to the staging
            dir). Mirrors :attr:`crawdad.attachments.AcceptedAttachment.filename`.
        original_filename: Raw filename from the Discord attachment;
            surfaced in the type-disambiguation question so the user
            recognises which file the bot is asking about.
        staged_path: Absolute path where the file lives on disk.
        content_hash: SHA-256 hex digest of the file contents. Used to
            track which files within the batch have already been
            successfully ingested (idempotent re-consent).
        inferred_type: ``creek.ingest`` registry type inferred from the
            file extension, or ``None`` when the extension is unknown.
            ``None`` triggers the type-disambiguation flow before
            ingestion can proceed.
    """

    filename: str
    original_filename: str
    staged_path: Path
    content_hash: str
    inferred_type: str | None


@dataclass(frozen=True)
class PendingBatch:
    """The most recently staged batch for a single Discord channel.

    Attributes:
        channel_id: Discord channel id; the store keys batches by this.
        staging_dir: Per-message staging directory under the vault.
        files: Accepted attachments awaiting consent, in upload order.
        privacy_tier_ceiling: Per-channel ``privacy_tier_ceiling``
            forwarded verbatim to every ``creek.ingest`` call so the
            MCP server can enforce the policy at its boundary.
        created_at: Monotonic timestamp the batch was recorded at;
            consumed by :meth:`is_expired`.
        scanned: Whether ``creek.redact.scan`` actually ran for this
            batch (FEAT-027, #1054). ``False`` means the scan could not
            run — MCP unreachable, the tool unadvertised, no MCP client
            at all, or an unexpected error — and the batch must never be
            dispatched to ``creek.ingest``. Enforced by the single guard
            in :func:`crawdad.bot._dispatch_ingest_for_batch`. Required
            with **no default** so no construction site can silently
            inherit a permissive value. Note the narrow claim: ``True``
            means the scan ran, *not* that it came back clean — see
            crawdad/CLAUDE.md §5.3.
        state: Lifecycle stage (``"awaiting_consent"`` →
            ``"awaiting_type"`` → ``"ingested"``). Abandonment removes
            the batch entirely instead of transitioning the state.
        ingested_hashes: Content hashes of files already successfully
            ingested. The handler skips re-dispatch for these on
            re-consent.
    """

    channel_id: int
    staging_dir: Path
    files: tuple[PendingFile, ...]
    privacy_tier_ceiling: str
    created_at: float
    # Placement is load-bearing: ``scanned`` has no default, so it must
    # precede every defaulted field or the class raises
    # ``TypeError: non-default argument follows default argument`` at
    # import time.
    scanned: bool
    state: BatchState = "awaiting_consent"
    ingested_hashes: frozenset[str] = field(default_factory=frozenset)

    @property
    def unresolved_files(self) -> tuple[PendingFile, ...]:
        """Return the subset of files whose ingestor type is still unknown."""
        return tuple(f for f in self.files if f.inferred_type is None)

    @property
    def needs_type_disambiguation(self) -> bool:
        """Return ``True`` when any file's inferred type is still ``None``."""
        return any(f.inferred_type is None for f in self.files)

    @property
    def all_ingested(self) -> bool:
        """Return ``True`` once every file's content hash has been recorded."""
        return bool(self.files) and all(
            f.content_hash in self.ingested_hashes for f in self.files
        )

    def is_expired(self, *, now: float, ttl_seconds: float) -> bool:
        """Return ``True`` when the batch has aged past *ttl_seconds*."""
        return (now - self.created_at) > ttl_seconds

    def with_resolved_types(self, ingest_type: str) -> PendingBatch:
        """Return a copy with *ingest_type* applied to every unresolved file.

        Files whose ``inferred_type`` was already set are preserved
        unchanged — v1 disambiguates with a single type for the whole
        unresolved subset (multi-turn per-file refinement is out of
        scope per the FEAT-034 issue body).
        """
        new_files = tuple(
            f if f.inferred_type is not None else replace(f, inferred_type=ingest_type)
            for f in self.files
        )
        return replace(self, files=new_files)

    def with_state(self, state: BatchState) -> PendingBatch:
        """Return a copy with *state* applied."""
        return replace(self, state=state)

    def with_ingested(self, hashes: frozenset[str]) -> PendingBatch:
        """Return a copy with *hashes* unioned into ``ingested_hashes``."""
        return replace(self, ingested_hashes=self.ingested_hashes | hashes)

    def resolved_groups(self) -> tuple[tuple[str, tuple[PendingFile, ...]], ...]:
        """Group files by ``inferred_type``; unresolved files are skipped.

        Returns ``(type, files)`` tuples in deterministic alphabetical
        order so the dispatch order — and the surfaced summary order —
        is predictable across runs. Files within each group preserve
        their original upload order.
        """
        groups: dict[str, list[PendingFile]] = {}
        for file in self.files:
            if file.inferred_type is None:
                continue
            groups.setdefault(file.inferred_type, []).append(file)
        return tuple((t, tuple(groups[t])) for t in sorted(groups))


class PendingBatchStore:
    """In-memory per-channel pending-batch store with TTL eviction.

    The store is keyed by Discord channel id. A new attachment turn
    supersedes any prior batch for that channel — :meth:`record` simply
    overwrites the existing entry. :meth:`get` evicts and returns
    ``None`` when the stored batch has aged past the configured TTL,
    so stale state cannot accidentally drive a fresh ingest.

    Lifetime: the store lives for the bot's process lifetime and is
    rebuilt fresh on restart. No persistence — the consent flow is
    intentionally session-scoped (per the FEAT-034 issue scope).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_PENDING_BATCH_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialise an empty store.

        Args:
            ttl_seconds: Maximum age of a stored batch before it is
                treated as expired and discarded on the next lookup.
            clock: Callable returning the current monotonic-style
                timestamp. Tests inject a controllable clock so timeout
                behaviour is deterministic; production uses
                :func:`time.monotonic`.
        """
        self._batches: dict[int, PendingBatch] = {}
        self._ttl_seconds = ttl_seconds
        self._clock: Callable[[], float] = clock or time.monotonic
        # Per-channel asyncio.Lock so concurrent ``ingest`` follow-ups on
        # the same channel can't race past the read-classify-dispatch
        # critical section and double-fire ``creek.ingest`` for the same
        # batch (PR #308 review HIGH). Locks live for the bot's lifetime
        # and are bounded by the channel count, which is itself bounded
        # by the operator's allowlist.
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def ttl_seconds(self) -> float:
        """Return the configured TTL in seconds."""
        return self._ttl_seconds

    def now(self) -> float:
        """Return the current clock value (test seam)."""
        return self._clock()

    def record(self, batch: PendingBatch) -> None:
        """Record *batch* as the channel's most recent pending batch.

        Any prior batch for the same channel is overwritten — the new
        attachment turn supersedes it.
        """
        self._batches[batch.channel_id] = batch

    def get(self, channel_id: int) -> PendingBatch | None:
        """Return the active batch for *channel_id*, or ``None`` if absent or stale.

        Expired batches are evicted as a side effect so a later
        :meth:`get` does not resurrect them.
        """
        batch = self._batches.get(channel_id)
        if batch is None:
            return None
        if batch.is_expired(now=self._clock(), ttl_seconds=self._ttl_seconds):
            del self._batches[channel_id]
            return None
        return batch

    def clear(self, channel_id: int) -> None:
        """Drop any batch recorded for *channel_id*. No-op when absent."""
        self._batches.pop(channel_id, None)

    def lock_for(self, channel_id: int) -> asyncio.Lock:
        """Return the per-channel :class:`asyncio.Lock`, lazy-creating on first use.

        Callers must hold this lock around the multi-step
        read-classify-dispatch cycle to make the consent flow atomic
        per channel — without it, two rapid ``ingest`` messages for the
        same batch can suspend on different ``await`` points and both
        observe ``state == "awaiting_consent"``, double-dispatching
        ``creek.ingest``. Lazy creation is safe under :mod:`asyncio`'s
        cooperative scheduling: the dict ``get``-then-set sequence
        contains no awaits, so two coroutines cannot interleave and
        produce different lock instances for the same channel.
        """
        lock = self._locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[channel_id] = lock
        return lock


def classify_followup_message(
    content: str,
    *,
    consent_tokens: frozenset[str] = DEFAULT_CONSENT_TOKENS,
    abandon_tokens: frozenset[str] = DEFAULT_ABANDON_TOKENS,
    valid_types: frozenset[str] = VALID_INGEST_TYPES,
) -> tuple[MessageKind, str | None]:
    """Classify *content* as consent / abandon / type / none.

    Matching is intentionally strict: the message is lowercased and
    stripped of surrounding whitespace + ASCII punctuation, then
    compared verbatim against each token set. Multi-word tokens like
    ``"go ahead"`` keep their internal space and so still match
    ``"Go ahead!"`` after normalisation. Anything that does not match
    a known token returns ``("none", None)`` so the caller can fall
    through to the regular agent loop without trapping the user in the
    consent state machine.

    Args:
        content: The user's raw message body.
        consent_tokens: Set of affirmative tokens that count as consent.
        abandon_tokens: Set of tokens that clear the pending batch.
        valid_types: Set of valid ``creek.ingest`` source_type values
            the disambiguation flow accepts.

    Returns:
        A ``(kind, payload)`` tuple. ``payload`` carries the resolved
        ingest type when ``kind == "type"`` and is ``None`` otherwise.
    """
    # Two strips on purpose: the first removes outer whitespace before
    # the ``.lower()`` so the second strip can also clean up internal
    # whitespace that ``.lower()`` would otherwise expose adjacent to
    # punctuation (e.g. ``"  Ingest!  "`` → ``"ingest"``, ``"go ahead."``
    # → ``"go ahead"``). Internal whitespace inside multi-word tokens
    # like ``"go ahead"`` is preserved because only outer characters
    # are stripped.
    norm = content.strip().lower().strip(string.punctuation + string.whitespace)
    if not norm:
        return "none", None
    if norm in valid_types:
        return "type", norm
    if norm in abandon_tokens:
        return "abandon", None
    if norm in consent_tokens:
        return "consent", None
    return "none", None


def format_type_question(batch: PendingBatch) -> str:
    """Return the inline question the bot asks for unresolved file types.

    The question lists the original filenames of every unresolved file
    so the user recognises which uploads need typing, plus the full
    set of valid ingest types so they know what answers are accepted.
    """
    unresolved = batch.unresolved_files
    names = ", ".join(f"`{f.original_filename}`" for f in unresolved)
    valid = ", ".join(sorted(VALID_INGEST_TYPES))
    plural = "are" if len(unresolved) > 1 else "is"
    return (
        f"What ingest type {plural} {names}? Reply with one of: {valid}. "
        "Or reply `cancel` to drop the batch."
    )


def build_pending_batch(
    *,
    channel_id: int,
    staging_dir: Path,
    accepted_files: tuple[PendingFile, ...],
    privacy_tier_ceiling: str,
    now: float,
    scanned: bool,
) -> PendingBatch:
    """Construct a fresh :class:`PendingBatch` in the ``awaiting_consent`` state.

    Thin factory so the bot handler doesn't have to remember the
    initial state — the only valid starting point for a freshly staged
    batch is ``awaiting_consent``.

    Args:
        channel_id: Discord channel the batch belongs to.
        staging_dir: Per-message staging directory under the vault.
        accepted_files: Staged attachments awaiting consent.
        privacy_tier_ceiling: Channel ceiling forwarded to every
            ``creek.ingest`` call.
        now: Timestamp to stamp the batch with.
        scanned: Whether ``creek.redact.scan`` actually ran. Required —
            see :attr:`PendingBatch.scanned`; a caller that cannot say
            must pass ``False``.
    """
    return PendingBatch(
        channel_id=channel_id,
        staging_dir=staging_dir,
        files=accepted_files,
        privacy_tier_ceiling=privacy_tier_ceiling,
        created_at=now,
        scanned=scanned,
    )
