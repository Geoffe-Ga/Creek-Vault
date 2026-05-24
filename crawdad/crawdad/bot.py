"""Pure-logic Discord message handling (FEAT-013 → FEAT-015 → FEAT-027).

``handle_message`` is a free function so the test suite can drive it
with fakes for ``Message``, ``Author``, and ``Channel``. The thin
``CrawDadClient`` subclass forwards Discord events; every interesting
decision lives here.

FEAT-015 wires the full agent loop: the handler hands off to
:func:`crawdad.loop.run_one_turn`, which orchestrates router →
dispatcher → composer with a hard cap of
:data:`crawdad.config.MAX_LOOP_ROUNDS`. The handler's other
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
from crawdad.history import ConversationHistory
from crawdad.loop import run_one_turn
from crawdad.mcp_client import MCPUnavailableError
from crawdad.slash_commands import register as register_slash_commands

if TYPE_CHECKING:
    from collections.abc import Sequence

    from crawdad.attachments import _AttachmentLike
    from crawdad.composer import SonnetComposer
    from crawdad.config import CrawDadConfig
    from crawdad.mcp_client import MCPClient
    from crawdad.router import IntentRouter
    from crawdad.skill_loader import SkillStackRegistry, VoiceSkillStack
    from crawdad.slash_commands import LoopRunner, RegisterSwitcher
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
# The conversational "Reply with `ingest`" follow-up that drives the
# router into ``creek.ingest`` automatically is tracked in FEAT-028
# (issue #281). Until then the CLI escape hatch in the prompt below is
# the supported path.
_INGEST_CONSENT_PROMPT = (
    "I did **not** ingest anything. Reply with `ingest` (or run "
    "`creek ingest --type <type> --input <staging path>`) to proceed."
)

_ALREADY_STAGED_REPLY = (
    "All attachments were already staged from a prior upload — nothing new "
    "to scan or ingest."
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

    When ``router``, ``composer``, or ``mcp_client`` is ``None``, the
    handler falls back to the FEAT-013 stub reply — useful for tests
    that don't exercise the full loop.

    Gate ordering note (FEAT-027): attachments are handled *before* the
    session-state check so a user dropping a file during a degraded
    session-state condition still gets their file staged with a clear
    safety report, instead of a misleading "session unavailable" reply.
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
        )
        return
    if session_state is None:
        await message.channel.send(_STATE_UNAVAILABLE_REPLY)
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
    4. Reply with the combined download summary, the scan report, and a
       consent prompt. The bot **never** auto-ingests; the user must
       respond explicitly (per FEAT-027 §Ingestion only proceeds on
       explicit user consent — see FEAT-028 / issue #281 for the
       conversational consent follow-up).
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

    # Send the summary + scan section first (may be truncated for long
    # scan bodies) and then the consent prompt as a separate message.
    # The consent string is short and well under the Discord cap; sending
    # it on its own guarantees the "I did **not** ingest anything" trust
    # signal can never be silently dropped by truncation of the scan
    # section, even for a multi-file batch with many findings.
    body = "\n\n".join((summary, scan_section))
    await message.channel.send(_truncate_for_discord(body))
    await message.channel.send(_INGEST_CONSENT_PROMPT)


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
        self.tree = app_commands.CommandTree(self)
        if loop_runner is not None:
            # discord.py's ``CommandTree.command`` signature is wider than
            # the ``_TreeLike`` Protocol the registration function uses,
            # so cast to Any at the boundary. Verified by the structural
            # ``test_register_wires_every_command_onto_tree`` test.
            register_slash_commands(
                cast("Any", self.tree),
                loop_runner=loop_runner,
                register_switcher=register_switcher,
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
        """Forward Discord messages to :func:`handle_message`."""
        if self.user is None:
            _LOGGER.debug("on_message before client ready; dropping event")
            return
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
        )
