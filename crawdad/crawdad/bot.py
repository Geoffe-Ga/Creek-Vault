"""Pure-logic Discord message handling (FEAT-013 → FEAT-015 → FEAT-027).

``handle_message`` is a free function so the test suite can drive it
with fakes for ``Message``, ``Author``, and ``Channel``. The thin
``CrawDadClient`` subclass forwards Discord events; every interesting
decision lives here.

FEAT-015 wires the full agent loop: the handler hands off to
:func:`crawdad.loop.run_one_turn`, which orchestrates router →
dispatcher → composer with a hard cap of
:attr:`CrawDadConfig.max_loop_rounds` (default
:data:`crawdad.config.MAX_LOOP_ROUNDS`; FEAT-036). The handler's other
responsibilities are the allowlist gate, the session-state fallback,
posting the loop's reply, and (FEAT-027) processing Discord
attachments through the safety pass before any ingest.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

import discord
from discord import app_commands

from crawdad.attachments import (
    ProcessedAttachments,
    format_attachment_summary,
    process_attachments,
)
from crawdad.consent import (
    PendingBatch,
    PendingBatchStore,
    PendingFile,
    build_pending_batch,
    classify_followup_message,
    format_type_question,
)
from crawdad.history import ConversationHistory
from crawdad.loop import run_one_turn
from crawdad.mcp_client import MCPUnavailableError
from crawdad.slash_commands import register as register_slash_commands

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from crawdad.attachments import _AttachmentLike
    from crawdad.capture import MessageCapture
    from crawdad.composer import SonnetComposer
    from crawdad.config import CrawDadConfig
    from crawdad.mcp_client import MCPClient
    from crawdad.router import IntentRouter
    from crawdad.skill_loader import SkillStackRegistry, VoiceSkillStack
    from crawdad.slash_commands import LoopRunner, RegisterSwitcher, WorkflowRunner
    from crawdad.state import SessionState, StateUnavailableError

_LOGGER = logging.getLogger("crawdad.bot")

_STATE_UNAVAILABLE_REPLY = (
    "no audit report yet — run `creek state` in the vault and try again."
)
_MCP_UNAVAILABLE_REPLY = "creek-tools is unreachable; try again in a moment."
_DISCORD_REPLY_LIMIT = 1900

# FEAT-027: name of the MCP tool the bot invokes for the safety pass on
# Discord attachments. Sourced from
# ``creek_mcp.tools.redact.TOOL_NAME``; duplicated here so the bot has
# no Python-level dependency on creek-tools beyond the MCP contract.
_REDACT_SCAN_TOOL = "creek.redact.scan"

_REDACT_TOOL_MISSING_REPLY = (
    "I downloaded the file(s), but the `creek.redact.scan` tool isn't advertised "
    "by creek-tools yet. Run `creek redact --scan <staging path>` from the "
    "terminal before ingesting."
)

# FEAT-027 ships the safety-pass + staging half of the consent flow.
# FEAT-034 (issue #281) closes the loop by wiring the conversational
# "Reply with `ingest`" follow-up directly through to ``creek.ingest``
# via the per-channel :class:`PendingBatchStore` threaded through this
# module. The CLI escape hatch in the prompt below is preserved for
# users who prefer the deterministic terminal path.
_INGEST_CONSENT_PROMPT = (
    "I did **not** ingest anything. Reply with `ingest` (or run "
    "`creek ingest --type <type> --input <staging path>`) to proceed. "
    "Reply `cancel` to drop the batch."
)

_ALREADY_STAGED_REPLY = (
    "All attachments were already staged from a prior upload — nothing new "
    "to scan or ingest."
)

# FEAT-034: name of the MCP tool the consent flow dispatches when the
# user gives an affirmative reply. Mirrors the ``creek.ingest`` server
# tool registered in ``creek_mcp/server.py``.
_INGEST_TOOL = "creek.ingest"

_INGEST_TOOL_MISSING_REPLY = (
    "I staged the file(s), but `creek.ingest` isn't advertised by "
    "creek-tools yet. Run `creek ingest --type <type> --input <staging path>` "
    "from the terminal to complete the ingest."
)

_BATCH_ABANDONED_REPLY = (
    "Cleared the pending batch — nothing was ingested. The staged files "
    "are still on disk under the same path; re-upload to start a new batch."
)

_ALREADY_INGESTED_REPLY = (
    "This batch was already ingested — nothing more to do. Upload a new "
    "file to start a fresh batch."
)


class _MessageLike(Protocol):
    """Structural protocol covering the bits of ``discord.Message`` we use."""

    @property
    def id(self) -> int: ...  # pragma: no cover - protocol stub

    @property
    def author(self) -> _AuthorLike: ...  # pragma: no cover - protocol stub

    @property
    def channel(self) -> _ChannelLike: ...  # pragma: no cover - protocol stub

    @property
    def content(self) -> str: ...  # pragma: no cover - protocol stub

    @property
    def attachments(
        self,
    ) -> Sequence[_AttachmentLike]: ...  # pragma: no cover - protocol stub


class _AuthorLike(Protocol):
    """Subset of ``discord.User`` / ``discord.Member`` used by the handler."""

    @property
    def id(self) -> int: ...  # pragma: no cover - protocol stub

    @property
    def bot(self) -> bool: ...  # pragma: no cover - protocol stub


class _ChannelLike(Protocol):
    """Subset of ``discord.abc.Messageable`` used by the handler."""

    @property
    def id(self) -> int: ...  # pragma: no cover - protocol stub

    async def send(self, content: str) -> None: ...  # pragma: no cover


async def handle_message(
    message: _MessageLike,
    *,
    config: CrawDadConfig,
    session_state: SessionState | None,
    bot_user_id: int,
    router: IntentRouter | None = None,
    composer: SonnetComposer | None = None,
    mcp_client: MCPClient | None = None,
    known_tools: tuple[str, ...] = (),
    history: ConversationHistory | None = None,
    skills: VoiceSkillStack | None = None,
    skill_registry: SkillStackRegistry | None = None,
    pending_batches: PendingBatchStore | None = None,
    workflow_runner: WorkflowRunner | None = None,
) -> None:
    """Process one Discord message.

    Args:
        message: The inbound Discord (or fake) message.
        config: Runtime config — supplies the allowlist.
        session_state: Result of :func:`crawdad.state.load_session_state`,
            or ``None`` if missing.
        bot_user_id: The bot's own Discord user id (self-suppression).
        router: FEAT-014 Haiku router.
        composer: FEAT-015 Sonnet composer.
        mcp_client: Factory for opening an MCP session per message.
        known_tools: MCP tool names snapshotted at session start.
        history: Conversation transcript appended in place.
        skills: Voice-skill stack loaded at session start.
        skill_registry: FEAT-029 mutable registry. When provided it
            takes precedence over ``skills`` and routes
            ``crawdad.activate_register`` intents through to a live
            stack swap.
        pending_batches: FEAT-034 per-channel pending-batch store. When
            provided, a successful attachment turn records a batch on
            the store and the next non-attachment message in the same
            channel is inspected for a consent / abandonment token
            *before* the agent loop runs.
        workflow_runner: ADAPT-003 async callback backing
            ``crawdad.run_workflow`` intents. Threaded through to
            :func:`run_one_turn` so a free-text request to run an
            authored workflow dispatches the same walk the
            ``/crawdad workflow run`` slash command uses (#527). When
            ``None``, those intents soft-error.

    When ``router``, ``composer``, or ``mcp_client`` is ``None``, the
    handler falls back to the FEAT-013 stub reply — useful for tests
    that don't exercise the full loop.

    Session-state note (#527): the loop and composer tolerate
    ``session_state=None`` everywhere (e.g. ``_wavelength_block``,
    ``_phase_hint_for``), so a free-text turn runs even when
    ``latest.md`` is absent — matching the slash-command contract,
    which never gates on session state. The bot no longer dead-ends
    free-text on a "session unavailable" reply; ``creek state`` guidance
    is surfaced only at startup (see ``cli._safe_load_state``) and via
    :func:`render_state_unavailable_reply`.

    Gate ordering note (FEAT-027): attachments are handled *before* the
    loop so a user dropping a file during a degraded session-state
    condition still gets their file staged with a clear safety report.
    The attachment path does not need ``session_state``.
    """
    if not _passes_allowlist(message, config=config, bot_user_id=bot_user_id):
        return
    if message.attachments:
        await _handle_attachments(
            message=message,
            config=config,
            mcp_client=mcp_client,
            known_tools=known_tools,
            pending_batches=pending_batches,
        )
        return
    if await _handle_consent_followup(
        message=message,
        config=config,
        mcp_client=mcp_client,
        known_tools=known_tools,
        pending_batches=pending_batches,
    ):
        return
    if router is None or composer is None or mcp_client is None:
        await message.channel.send(_stub_reply())
        return

    outcome = await run_one_turn(
        message=message.content,
        router=router,
        composer=composer,
        mcp_client=mcp_client,
        known_tools=known_tools,
        history=history or ConversationHistory(),
        session_state=session_state,
        skills=_resolve_skills(skill_registry, skills),
        skill_registry=skill_registry,
        max_rounds=config.max_loop_rounds,
        workflow_runner=workflow_runner,
    )
    _LOGGER.info("loop outcome: %s", outcome.kind)
    await message.channel.send(_truncate_for_discord(outcome.reply))


def _passes_allowlist(
    message: _MessageLike,
    *,
    config: CrawDadConfig,
    bot_user_id: int,
) -> bool:
    """Pure (non-side-effecting) self-suppression + allowlist gate.

    Returns ``True`` when the caller should keep processing the
    message, ``False`` when the message should be silently dropped
    (per FEAT-013 §non-allowlisted callers get no response).
    """
    if message.author.id == bot_user_id or message.author.bot:
        return False
    return config.is_allowed(user_id=message.author.id, channel_id=message.channel.id)


def _empty_skills() -> VoiceSkillStack:
    """Return an empty :class:`VoiceSkillStack` for callers that opt out."""
    from crawdad.skill_loader import VoiceSkillStack

    return VoiceSkillStack(skills=())


async def _handle_attachments(
    *,
    message: _MessageLike,
    config: CrawDadConfig,
    mcp_client: MCPClient | None,
    known_tools: tuple[str, ...],
    pending_batches: PendingBatchStore | None = None,
) -> None:
    """FEAT-027 attachment processing: download → safety scan → reply.

    The handler short-circuits the regular agent loop on turns that
    carry attachments. The flow is deliberately deterministic so users
    get a predictable safety report before any ingest call is dispatched:

    1. Download every attachment to the per-channel / per-message
       staging directory under ``00-Creek-Meta/Inbound/``. Size and
       extension limits are enforced before bytes are read.
    2. If every accepted file was an idempotent re-upload (same content
       hash already on disk), reply "already staged" and stop — no
       redundant scan or ingest.
    3. Otherwise invoke ``creek.redact.scan`` on the staging directory
       via MCP. If MCP is unreachable or the tool is not advertised,
       fall back to a clear soft error rather than silently ingesting.
    4. Record the staged batch on the per-channel
       :class:`PendingBatchStore` (FEAT-034) so a follow-up
       ``ingest`` / ``yes`` / ``proceed`` message in the same channel
       dispatches ``creek.ingest`` for the batch.
    5. Reply with the combined download summary, the scan report, and a
       consent prompt. The bot **never** auto-ingests; the user must
       respond explicitly.
    """
    channel_id = message.channel.id
    processed = await process_attachments(
        attachments=message.attachments,
        vault_path=config.vault_path,
        channel_id=channel_id,
        message_id=message.id,
        config=config.attachments,
    )

    summary = format_attachment_summary(processed, vault_path=config.vault_path)

    if not processed.accepted:
        # Every attachment was rejected at the boundary — say so and stop.
        await message.channel.send(_truncate_for_discord(summary))
        return

    if processed.all_already_present:
        await message.channel.send(
            _truncate_for_discord(f"{summary}\n\n{_ALREADY_STAGED_REPLY}")
        )
        return

    scan_section = await _run_safety_scan(
        processed=processed,
        config=config,
        mcp_client=mcp_client,
        known_tools=known_tools,
        channel_id=channel_id,
    )

    # Hold the per-channel consent lock during the record so a racing
    # ``ingest`` follow-up cannot observe a half-built batch. The lock
    # is uncontended unless the consent flow is mid-dispatch — see the
    # ``PendingBatchStore.lock_for`` docstring for the TOCTOU rationale.
    if pending_batches is not None:
        async with pending_batches.lock_for(channel_id):
            _record_pending_batch(
                pending_batches=pending_batches,
                processed=processed,
                channel_id=channel_id,
                config=config,
            )

    # Send the summary + scan section first (may be truncated for long
    # scan bodies) and then the consent prompt as a separate message.
    # The consent string is short and well under the Discord cap; sending
    # it on its own guarantees the "I did **not** ingest anything" trust
    # signal can never be silently dropped by truncation of the scan
    # section, even for a multi-file batch with many findings.
    body = "\n\n".join((summary, scan_section))
    await message.channel.send(_truncate_for_discord(body))
    await message.channel.send(_INGEST_CONSENT_PROMPT)


def _record_pending_batch(
    *,
    pending_batches: PendingBatchStore | None,
    processed: ProcessedAttachments,
    channel_id: int,
    config: CrawDadConfig,
) -> None:
    """Record *processed* on the pending-batch store, superseding prior state.

    Skipped (no-op) when the store wasn't wired (test paths that don't
    exercise the consent flow) or when no files landed on disk.
    """
    if pending_batches is None or not processed.accepted:
        return
    files = tuple(
        PendingFile(
            filename=a.filename,
            original_filename=a.original_filename,
            staged_path=a.staged_path,
            content_hash=a.content_hash,
            inferred_type=a.inferred_type,
        )
        for a in processed.accepted
    )
    batch = build_pending_batch(
        channel_id=channel_id,
        staging_dir=processed.staging_dir,
        accepted_files=files,
        privacy_tier_ceiling=_channel_tier(channel_id=channel_id, config=config),
        now=pending_batches.now(),
    )
    pending_batches.record(batch)


async def _handle_consent_followup(
    *,
    message: _MessageLike,
    config: CrawDadConfig,
    mcp_client: MCPClient | None,
    known_tools: tuple[str, ...],
    pending_batches: PendingBatchStore | None,
) -> bool:
    """Try to handle *message* as a follow-up to a pending consent batch.

    Returns ``True`` when the message was consumed by the consent flow
    (a reply was sent and the regular agent loop should be skipped) and
    ``False`` when the caller should continue with normal processing.

    Concurrency note: the entire read-classify-dispatch cycle runs
    under the per-channel lock from :meth:`PendingBatchStore.lock_for`
    so two rapid ``ingest`` messages on the same channel can't both
    observe ``awaiting_consent`` and double-fire ``creek.ingest`` for
    the same batch (PR #308 review HIGH).

    Trust boundary: the batch is keyed by ``channel.id``, not by user
    id, so any user the operator has allowlisted into a channel can
    consent to that channel's pending batch. For the documented
    personal-use deployment target this is acceptable — see FEAT-013
    §5.1; broaden the consent key before adding multi-user channels.
    """
    if pending_batches is None:
        return False
    async with pending_batches.lock_for(message.channel.id):
        batch = pending_batches.get(message.channel.id)
        if batch is None:
            return False

        kind, payload = classify_followup_message(
            message.content,
            consent_tokens=config.consent.consent_tokens,
            abandon_tokens=config.consent.abandon_tokens,
        )
        if kind == "none":
            return False
        if kind == "abandon":
            pending_batches.clear(message.channel.id)
            await message.channel.send(_BATCH_ABANDONED_REPLY)
            return True
        if kind == "type":
            return await _apply_disambiguation_reply(
                batch=batch,
                ingest_type=payload or "",
                message=message,
                mcp_client=mcp_client,
                known_tools=known_tools,
                pending_batches=pending_batches,
                config=config,
            )
        # kind == "consent"
        return await _apply_consent_reply(
            batch=batch,
            message=message,
            mcp_client=mcp_client,
            known_tools=known_tools,
            pending_batches=pending_batches,
            config=config,
        )


async def _apply_consent_reply(
    *,
    batch: PendingBatch,
    message: _MessageLike,
    mcp_client: MCPClient | None,
    known_tools: tuple[str, ...],
    pending_batches: PendingBatchStore,
    config: CrawDadConfig,
) -> bool:
    """Handle a ``consent``-kind follow-up against *batch*.

    Branches on batch state:

    * ``"ingested"``: re-consent is a clear no-op.
    * unresolved types: transition to ``"awaiting_type"`` and ask
      which type to apply to the unresolved subset.
    * everything resolved: dispatch ingest immediately.
    """
    if batch.state == "ingested":
        await message.channel.send(_ALREADY_INGESTED_REPLY)
        return True
    if batch.needs_type_disambiguation:
        updated = batch.with_state("awaiting_type")
        pending_batches.record(updated)
        await message.channel.send(format_type_question(updated))
        return True
    await _dispatch_ingest_for_batch(
        batch=batch,
        message=message,
        mcp_client=mcp_client,
        known_tools=known_tools,
        pending_batches=pending_batches,
        config=config,
    )
    return True


async def _apply_disambiguation_reply(
    *,
    batch: PendingBatch,
    ingest_type: str,
    message: _MessageLike,
    mcp_client: MCPClient | None,
    known_tools: tuple[str, ...],
    pending_batches: PendingBatchStore,
    config: CrawDadConfig,
) -> bool:
    """Handle a ``type``-kind follow-up against *batch*.

    Only acts when the batch is in the ``"awaiting_type"`` state — a
    type word landing while the batch is still ``"awaiting_consent"``
    or already ``"ingested"`` falls through to the regular agent loop
    so the user is never trapped inside the consent state machine.
    """
    if batch.state != "awaiting_type":
        return False
    resolved = batch.with_resolved_types(ingest_type)
    pending_batches.record(resolved)
    await _dispatch_ingest_for_batch(
        batch=resolved,
        message=message,
        mcp_client=mcp_client,
        known_tools=known_tools,
        pending_batches=pending_batches,
        config=config,
    )
    return True


async def _dispatch_ingest_for_batch(
    *,
    batch: PendingBatch,
    message: _MessageLike,
    mcp_client: MCPClient | None,
    known_tools: tuple[str, ...],
    pending_batches: PendingBatchStore,
    config: CrawDadConfig,
) -> None:
    """Invoke ``creek.ingest`` for every file in *batch* and reply.

    The batch is mutated in the store to track ingested hashes (so a
    follow-up consent in the same channel reports "already ingested")
    and to transition state to ``"ingested"`` only once every file in
    the batch has landed. A partial failure (mid-batch
    :class:`MCPUnavailableError`) leaves the batch in
    ``"awaiting_consent"`` so the user can retry; the already-landed
    files are then skipped on the retry via their content hashes.
    """
    if mcp_client is None or _INGEST_TOOL not in known_tools:
        await message.channel.send(_INGEST_TOOL_MISSING_REPLY)
        return
    groups = batch.resolved_groups()
    reply, ingested = await _run_ingest_dispatch(
        batch=batch,
        groups=groups,
        mcp_client=mcp_client,
        config=config,
    )
    if ingested:
        updated = batch.with_ingested(ingested)
        # Only mark the batch ingested when every file landed — a partial
        # failure (mid-batch MCPUnavailableError) keeps the batch in
        # ``awaiting_consent`` so the user can retry; the already-landed
        # files are skipped on the retry via their content hashes.
        if updated.all_ingested:
            updated = updated.with_state("ingested")
        pending_batches.record(updated)
    await message.channel.send(_truncate_for_discord(reply))


async def _run_ingest_dispatch(
    *,
    batch: PendingBatch,
    groups: tuple[tuple[str, tuple[PendingFile, ...]], ...],
    mcp_client: MCPClient,
    config: CrawDadConfig,
) -> tuple[str, frozenset[str]]:
    """Open one MCP session and call ``creek.ingest`` per file.

    Returns the user-facing reply text and the set of content hashes
    that landed successfully. A mid-batch
    :class:`MCPUnavailableError` short-circuits remaining files and
    surfaces the documented soft error. Any other unexpected exception
    (timeout, protocol error, malformed JSON, …) is also caught and
    surfaced as the same soft error rather than left to bubble up into
    the Discord ``on_message`` handler, where it would log a stack
    trace and leave the user staring at silence.
    """
    lines: list[str] = ["**Ingest results**"]
    ingested: set[str] = set()
    try:
        async with mcp_client.connect() as session:
            for ingest_type, files in groups:
                for file in files:
                    if file.content_hash in batch.ingested_hashes:
                        lines.append(
                            f"- `{file.filename}` ({ingest_type}): already ingested"
                        )
                        continue
                    rel_path = _relative_to_vault(file.staged_path, config=config)
                    body = await session.call_tool(
                        _INGEST_TOOL,
                        {
                            "source_type": ingest_type,
                            "input_path": str(rel_path),
                            "privacy_tier_ceiling": batch.privacy_tier_ceiling,
                        },
                    )
                    ingested.add(file.content_hash)
                    lines.append(f"- `{file.filename}` ({ingest_type}): {body}")
    except MCPUnavailableError as exc:
        _LOGGER.warning("creek.ingest failed: %s", exc)
        return _MCP_UNAVAILABLE_REPLY, frozenset(ingested)
    except Exception:
        # Match the safety-pass handler in ``_run_safety_scan``: log the
        # full traceback for the operator, but surface a soft error to
        # the user. Without this the exception would propagate out of
        # ``handle_message`` and the user would be left in silence.
        _LOGGER.exception("unexpected error during creek.ingest call")
        return _MCP_UNAVAILABLE_REPLY, frozenset(ingested)
    return "\n".join(lines), frozenset(ingested)


def _relative_to_vault(path: Path, *, config: CrawDadConfig) -> Path:
    """Render *path* relative to the vault when possible, else verbatim."""
    try:
        return path.relative_to(config.vault_path)
    except ValueError:
        return path


async def _run_safety_scan(
    *,
    processed: ProcessedAttachments,
    config: CrawDadConfig,
    mcp_client: MCPClient | None,
    known_tools: tuple[str, ...],
    channel_id: int,
) -> str:
    """Invoke ``creek.redact.scan`` on the staging dir, return Discord-safe text.

    Returns a soft-error message instead of raising when MCP is
    unavailable, the tool is not advertised, or any other unexpected
    exception occurs during the call. The user always gets the
    download summary and a clear next step — the bot never goes silent
    on attachment turns.
    """
    if mcp_client is None or _REDACT_SCAN_TOOL not in known_tools:
        return _REDACT_TOOL_MISSING_REPLY

    try:
        rel_staging = processed.staging_dir.relative_to(config.vault_path)
    except ValueError:
        rel_staging = processed.staging_dir

    try:
        async with mcp_client.connect() as session:
            body = await session.call_tool(
                _REDACT_SCAN_TOOL,
                {
                    "input_path": str(rel_staging),
                    "privacy_tier_ceiling": _channel_tier(
                        channel_id=channel_id, config=config
                    ),
                },
            )
    except MCPUnavailableError as exc:
        _LOGGER.warning("creek.redact.scan failed: %s", exc)
        return _MCP_UNAVAILABLE_REPLY
    except Exception:
        # Any other failure (timeout, protocol error, bad JSON, etc.)
        # must not bubble up — the bot would go silent on the user's
        # attachment turn. Log a full traceback for the operator and
        # surface the same soft error.
        _LOGGER.exception("unexpected error during creek.redact.scan call")
        return _MCP_UNAVAILABLE_REPLY

    return _format_scan_section(body)


def _format_scan_section(body: str) -> str:
    """Wrap the MCP scan tool's text response into a Discord-safe section.

    The MCP transport already concatenates the tool's text content into
    a single string; ``creek.redact.scan`` puts a Markdown summary in
    that text. If the body looks like a raw JSON dict (e.g. because a
    future tool revision returns structured content directly), pull the
    ``report_markdown`` field out so users do not see a JSON blob.
    """
    stripped = body.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("report_markdown"), str):
            return f"**Safety scan**\n\n{parsed['report_markdown']}"
    return f"**Safety scan**\n```\n{body}\n```"


def _channel_tier(*, channel_id: int, config: CrawDadConfig) -> str:
    """Return the privacy tier ceiling for *channel_id*.

    Falls back to ``personal`` when the channel is absent from
    :attr:`AttachmentConfig.channel_privacy_tiers` — ingest writes
    default to personal per FEAT-011, so a missing entry never
    silently relaxes the ceiling.
    """
    return config.attachments.channel_privacy_tiers.get(channel_id, "personal")


def _resolve_skills(
    registry: SkillStackRegistry | None, fallback: VoiceSkillStack | None
) -> VoiceSkillStack:
    """Return the active skill stack, preferring the registry when wired.

    Extracted out of :func:`handle_message` so the message-handling
    branch stays under the project's cyclomatic-complexity cap.
    """
    if registry is not None:
        return registry.stack
    return fallback or _empty_skills()


def _truncate_for_discord(text: str) -> str:
    """Cap reply at Discord's soft limit so very long composer outputs land."""
    if len(text) <= _DISCORD_REPLY_LIMIT:
        return text
    return text[: _DISCORD_REPLY_LIMIT - 3] + "..."


def render_state_unavailable_reply(error: StateUnavailableError) -> str:
    """Map :class:`StateUnavailableError` to the user-facing message."""
    _LOGGER.info("state unavailable: %s", error)
    return _STATE_UNAVAILABLE_REPLY


def render_mcp_unavailable_reply(error: MCPUnavailableError) -> str:
    """Map :class:`MCPUnavailableError` to the user-facing message."""
    _LOGGER.warning("MCP unavailable: %s", error)
    return _MCP_UNAVAILABLE_REPLY


def _stub_reply() -> str:
    """Fallback used when the loop components aren't wired (test-only path)."""
    return (
        "crawdad here — wiring scaffold is up. "
        "(Agent loop components not configured this session.)"
    )


class CrawDadClient(discord.Client):
    """``discord.Client`` subclass that delegates to :func:`handle_message`."""

    def __init__(
        self,
        *,
        config: CrawDadConfig,
        session_state: SessionState | None,
        router: IntentRouter | None = None,
        composer: SonnetComposer | None = None,
        mcp_client: MCPClient | None = None,
        known_tools: tuple[str, ...] = (),
        history: ConversationHistory | None = None,
        skills: VoiceSkillStack | None = None,
        skill_registry: SkillStackRegistry | None = None,
        loop_runner: LoopRunner | None = None,
        register_switcher: RegisterSwitcher | None = None,
        pending_batches: PendingBatchStore | None = None,
        workflow_lister: Callable[[], list[str]] | None = None,
        workflow_runner: WorkflowRunner | None = None,
        message_capture: MessageCapture | None = None,
        intents: discord.Intents | None = None,
    ) -> None:
        """Store config + agent-loop components; init the parent ``Client``.

        ``loop_runner`` (FEAT-016) is the closure the slash command
        callbacks invoke to enter the FEAT-015 loop with a pre-baked
        user message. When ``None``, slash commands are not registered.

        ``skill_registry`` and ``register_switcher`` (FEAT-029) thread
        the mutable voice-register state through the free-text handler
        and the ``/crawdad register`` slash command respectively, so
        register switches survive across turns within the bot's
        lifetime.

        ``pending_batches`` (FEAT-034) is the per-channel pending-batch
        store the consent flow reads on every non-attachment turn and
        writes on every successful attachment turn. When ``None``, the
        consent flow is disabled and the user must fall back to the
        ``creek ingest`` CLI path.

        ``workflow_lister`` and ``workflow_runner`` (ADAPT-003) are the
        closures the ``/crawdad workflow`` command invokes for its
        ``list`` and ``run`` subactions. Both default to ``None`` —
        sessions without an MCP tool surface still register the
        command but the handler soft-errors instead of crashing.
        ``workflow_runner`` is additionally forwarded to the free-text
        :func:`handle_message` path (#527) so a router-emitted
        ``crawdad.run_workflow`` intent on a natural-language turn
        dispatches the same walk as the slash command.
        """
        super().__init__(intents=intents or self._default_intents())
        self._config = config
        self._session_state = session_state
        self._router = router
        self._composer = composer
        self._mcp_client = mcp_client
        self._known_tools = known_tools
        self._history = history
        self._skills = skills
        self._skill_registry = skill_registry
        self._loop_runner = loop_runner
        self._pending_batches = pending_batches
        self._workflow_runner = workflow_runner
        self._message_capture = message_capture
        self.tree = app_commands.CommandTree(self)
        if loop_runner is not None:
            # discord.py's ``CommandTree.command`` signature is wider than
            # the ``_TreeLike`` Protocol the registration function uses,
            # so cast to Any at the boundary. Verified by the structural
            # ``test_register_wires_every_command_onto_tree`` test.
            register_slash_commands(
                cast("Any", self.tree),
                config=config,
                loop_runner=loop_runner,
                register_switcher=register_switcher,
                workflow_lister=workflow_lister,
                workflow_runner=workflow_runner,
            )

    @staticmethod
    def _default_intents() -> discord.Intents:
        """Return the minimum intents the v1.0 bot needs."""
        intents = discord.Intents.default()
        intents.message_content = True
        return intents

    async def setup_hook(self) -> None:
        """Sync slash commands with Discord once the client is ready.

        ``setup_hook`` runs after login but before the gateway is
        ready, which is the documented home for ``CommandTree.sync()``.
        Skipped when the loop runner wasn't provided (test wiring).
        """
        if self._loop_runner is not None:
            await self.tree.sync()
            _LOGGER.info("synced /crawdad slash commands with Discord")

    async def on_ready(self) -> None:
        """Log connection details. Real session-start work happens in CLI."""
        _LOGGER.info("crawdad connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        """Capture the message (best-effort), then forward to :func:`handle_message`."""
        if self.user is None:
            _LOGGER.debug("on_message before client ready; dropping event")
            return
        await self._capture_message(message)
        await handle_message(
            cast("_MessageLike", message),
            config=self._config,
            session_state=self._session_state,
            bot_user_id=self.user.id,
            router=self._router,
            composer=self._composer,
            mcp_client=self._mcp_client,
            known_tools=self._known_tools,
            history=self._history,
            skills=self._skills,
            skill_registry=self._skill_registry,
            pending_batches=self._pending_batches,
            workflow_runner=self._workflow_runner,
        )

    async def _capture_message(self, message: discord.Message) -> None:
        """Append the message to the bot-capture log, never disturbing commands.

        No-op when capture is disabled. The bot's own messages are skipped (it
        would only re-capture its own replies). A capture write failure is logged
        and swallowed — capture is best-effort and must never break the command
        path (#687).
        """
        if self._message_capture is None:
            return
        if self.user is not None and message.author.id == self.user.id:
            return
        try:
            await self._message_capture.on_message(message)
        except OSError:
            _LOGGER.exception("bot-capture write failed; continuing command path")
