"""Discord slash commands for CrawDad (FEAT-016 + FEAT-029 + ADAPT-003).

The `/crawdad` family bundles `reflect`, `checkin`, `surface`, `draft`,
`save`, `register`, and `workflow`. Most routes through the FEAT-015
agent loop by constructing a pre-baked user message and running it
through an injected ``loop_runner`` callable; the Sonnet composer
wraps the tool results in the user's voice. ``register`` (FEAT-029)
bypasses the loop and invokes the synchronous ``register_switcher``
directly so voice-register changes are deterministic.

``workflow`` (ADAPT-003 / issue #268) bypasses the router and uses a
deterministic step walker over an authored YAML file. The slash
command's ``action`` parameter switches between ``list`` (enumerate
available workflows) and ``run`` (execute one by name).

The handlers are free functions that take a tiny ``Replier`` callable
(``async def (str) -> None``) so they can be unit-tested without
discord.py. ``register()`` wraps each handler in a discord.py
``app_commands`` callback that ``defer()``s the interaction (LLM
calls exceed Discord's 3-second response window), runs the handler,
and ``followup.send()``s the reply.

Trust boundary: the user-supplied ``topic`` and ``content`` arguments
are interpolated directly into LLM prompts. The personal-use allowlist
(FEAT-013) is the primary gate; this surface does NOT sanitize input
because doing so would interfere with the user's ability to ask the
loop to act on raw text. Revisit this if the allowlist ever expands
beyond a single trusted user.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from crawdad.mcp_client import MCPUnavailableError

if TYPE_CHECKING:
    from crawdad.config import CrawDadConfig

_LOGGER = logging.getLogger("crawdad.slash_commands")

# Issue #468: the documented soft-error shown when the desk / creek-tools
# MCP surface is unreachable. Mirrors ``crawdad.loop._MCP_UNAVAILABLE_REPLY``
# so the author routes degrade with the same wording as the agent loop.
_MCP_UNAVAILABLE_REPLY = "creek-tools is unreachable; try again in a moment."

# A callable that drives one turn of the FEAT-015 loop and returns the
# user-facing reply string. The CLI builds one by closing over the
# per-session router, composer, MCP client, history, state, and skills
# components — see :func:`crawdad.cli._build_loop_runner`.
LoopRunner = Callable[[str], Awaitable[str]]

# FEAT-029: synchronous callable that swaps the active voice register.
# The slash-command handler invokes it directly so the user gets a
# deterministic switch (no LLM round-trip). Returns True on success,
# False on unknown / unsafe register names.
RegisterSwitcher = Callable[[str], bool]

# ADAPT-003: async callable that runs a named workflow end-to-end and
# returns the user-facing reply. The CLI builds one by closing over the
# per-session registry, walker, composer, and MCP client — see
# :func:`crawdad.cli._build_workflow_runner`. Workflow runners receive
# a ``name`` plus an ``inputs`` dict (currently always empty until the
# Discord slash command grows a free-text input parameter; the kwarg
# keeps the signature future-proof for FEAT-015-style follow-ups).
WorkflowRunner = Callable[[str, dict[str, str]], Awaitable[str]]

# Async callable that posts a message back to the Discord user.
# Wraps ``discord.Interaction.followup.send`` in production; tests
# substitute a list-appending fake.
Replier = Callable[[str], Awaitable[None]]


CRAWDAD_COMMANDS: tuple[str, ...] = (
    "reflect",
    "checkin",
    "surface",
    "draft",
    "ask",
    "save",
    "register",
    "workflow",
)

_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "reflect": "Open reflective conversation mode (FEAT-015 loop).",
    "checkin": "Wavelength check-in — read the current phase + dosage state.",
    "surface": "Surface paradoxes, liminal content, or emerging themes.",
    "draft": "Draft an essay on a topic (routes through creek.author, essay medium).",
    "ask": "Ask a question; get a cited, voiced answer (routes through creek.author).",
    "save": "File the supplied content back to the vault via creek.save.",
    "register": "Switch the active voice register (FEAT-029).",
    "workflow": "List or run named workflows (ADAPT-003).",
}

# Module-level invariant: every command name must have a description and
# vice versa. Checked at import time so a typo when adding a new command
# fails fast instead of surfacing as a Discord registration error.
if set(CRAWDAD_COMMANDS) != set(_COMMAND_DESCRIPTIONS):  # pragma: no cover
    _diff = set(CRAWDAD_COMMANDS) ^ set(_COMMAND_DESCRIPTIONS)
    _msg = (
        "CRAWDAD_COMMANDS and _COMMAND_DESCRIPTIONS must list the same names; "
        f"diff: {_diff}"
    )
    raise RuntimeError(_msg)

_REFLECT_PROMPT = (
    "Reflect with me. No specific tool request — open the loop in conversational "
    "mode and surface whatever is most alive in my current wavelength."
)
_CHECKIN_PROMPT = (
    "Give me a wavelength check-in: read my latest state via creek.state.read and "
    "summarize the current phase, dosage, and any recent transitions."
)
_SURFACE_PROMPT = (
    "Surface anything in my vault that's emerging — paradoxes, liminal items, or "
    "drift. Route paradoxes to 10-Liminal/Paradoxes/ via creek.save."
)

# ADAPT-003: workflow subaction names. Discord renders each command
# parameter as a separate input field; ``action`` defaults to ``list``
# so a bare ``/crawdad workflow`` enumerates the available workflows.
WORKFLOW_ACTION_LIST = "list"
WORKFLOW_ACTION_RUN = "run"
_WORKFLOW_ACTIONS = (WORKFLOW_ACTION_LIST, WORKFLOW_ACTION_RUN)
# Two missing-wiring strings so each action's failure mode is honest
# about WHY it cannot proceed. ``list`` only reads the registry, so its
# message talks about the registry; ``run`` drives the walker, so its
# message talks about the walker. Conflating the two would have a
# ``list`` failure claim "the walker has nothing to call" even when the
# registry is the missing piece (PR #309 review feedback).
_WORKFLOW_LISTER_MISSING_REPLY = (
    "Workflows aren't available in this session — the registry wasn't wired "
    "up (the MCP tool surface was empty at startup)."
)
_WORKFLOW_RUNNER_MISSING_REPLY = (
    "Workflows aren't wired up in this session — the MCP server probably "
    "advertised no tools, so the workflow walker has nothing to call."
)


class _TreeLike(Protocol):
    """Structural subset of ``discord.app_commands.CommandTree`` we use."""

    def command(
        self, *, name: str, description: str
    ) -> Callable[
        [Callable[..., Any]], Callable[..., Any]
    ]: ...  # pragma: no cover - structural protocol


# -----------------------------------------------------------------------------
# Free-function handlers (pure logic; testable without discord.py)
# -----------------------------------------------------------------------------


async def handle_reflect(replier: Replier, *, loop_runner: LoopRunner) -> None:
    """``/crawdad reflect`` — open reflective mode."""
    reply = await loop_runner(_REFLECT_PROMPT)
    await replier(reply)


async def handle_checkin(replier: Replier, *, loop_runner: LoopRunner) -> None:
    """``/crawdad checkin`` — wavelength check-in."""
    reply = await loop_runner(_CHECKIN_PROMPT)
    await replier(reply)


async def handle_surface(replier: Replier, *, loop_runner: LoopRunner) -> None:
    """``/crawdad surface`` — surface paradoxes and liminal content."""
    reply = await loop_runner(_SURFACE_PROMPT)
    await replier(reply)


async def _run_and_reply(
    replier: Replier, loop_runner: LoopRunner, prompt: str
) -> None:
    """Drive the loop runner and reply, degrading softly on MCP failure.

    Issue #468: the author routes (`ask`/`draft`) own a resilience
    contract — if the injected ``loop_runner`` ever lets an
    :class:`~crawdad.mcp_client.MCPUnavailableError` escape (the loop
    normally converts it to a soft-reply string, but defense-in-depth
    keeps Discord from ever seeing a stack trace), reply with the
    documented soft error instead of propagating.

    Args:
        replier: Async callable that posts the reply back to Discord.
        loop_runner: The FEAT-015 agent-loop driver.
        prompt: The pre-baked user message to run through the loop.
    """
    try:
        reply = await loop_runner(prompt)
    except MCPUnavailableError as exc:
        _LOGGER.warning("author route degraded: %s", exc)
        await replier(_MCP_UNAVAILABLE_REPLY)
        return
    await replier(reply)


async def handle_draft(
    replier: Replier, *, topic: str, loop_runner: LoopRunner
) -> None:
    """``/crawdad draft <topic>`` — author an essay on the supplied topic."""
    cleaned = topic.strip()
    if not cleaned:
        await replier(
            "I need a topic to draft on. Try `/crawdad draft <topic>` — for "
            "example, `/crawdad draft phase transitions`."
        )
        return
    prompt = (
        f"Draft an essay on the following topic: {cleaned}.\n\n"
        "Call creek.author with medium: essay to generate the essay in my "
        "voice. Voice fidelity is owned by creek.author's skill stack — "
        "don't re-draft inline. If I choose to keep the result, file it via "
        "creek.save with target ai-as-user into 11-Other-Authors/ai-as-user/ "
        "(AI-authored attribution: author=ai, representativeness=endorsed, "
        "voice_weight=0.0)."
    )
    await _run_and_reply(replier, loop_runner, prompt)


async def handle_ask(
    replier: Replier, *, question: str, loop_runner: LoopRunner
) -> None:
    """``/crawdad ask <question>`` — answer the question, cited and voiced."""
    cleaned = question.strip()
    if not cleaned:
        await replier(
            "I need a question to answer. Try `/crawdad ask <question>` — for "
            "example, `/crawdad ask how do phase transitions show up in my "
            "writing?`."
        )
        return
    prompt = (
        f"Answer the following question: {cleaned}.\n\n"
        "Call creek.author with medium: research to produce a cited answer "
        "grounded in my vault and voiced in my register. Return that answer. "
        "If I choose to keep it, file it via creek.save with target "
        "ai-as-user into 11-Other-Authors/ai-as-user/ (AI-authored "
        "attribution: author=ai, representativeness=endorsed, "
        "voice_weight=0.0)."
    )
    await _run_and_reply(replier, loop_runner, prompt)


async def handle_save(
    replier: Replier, *, content: str, loop_runner: LoopRunner
) -> None:
    """``/crawdad save <content>`` — file content back to the vault."""
    cleaned = content.strip()
    if not cleaned:
        await replier(
            "What should I save? Pass the content as the command argument: "
            "`/crawdad save <your text here>`."
        )
        return
    prompt = (
        f"Save this content back to my vault via creek.save:\n\n{cleaned}\n\n"
        "Infer the right target (fragment / paradox / draft / liminal) from "
        "the content's character."
    )
    reply = await loop_runner(prompt)
    await replier(reply)


async def handle_register(
    replier: Replier, *, name: str, register_switcher: RegisterSwitcher | None
) -> None:
    """``/crawdad register <name>`` — swap the active voice register.

    Bypasses the agent loop and calls ``register_switcher`` directly so
    the user gets a deterministic, single-step switch with a clear
    success / soft-error reply.
    """
    cleaned = name.strip().lower()
    if not cleaned:
        await replier(
            "What register should I activate? Try `/crawdad register <name>` — "
            "for example, `/crawdad register analytic`."
        )
        return
    if register_switcher is None:
        _LOGGER.info("register switcher unavailable; ignoring %r", cleaned)
        await replier(
            "Voice register switching is unavailable in this session. "
            "(The vault's `creek-skills/` tree wasn't wired up.)"
        )
        return
    if register_switcher(cleaned):
        await replier(f"Voice register switched to `{cleaned}`.")
        return
    await replier(
        f"Unknown voice register `{cleaned}` — check that "
        f"`creek-skills/registers/{cleaned}.SKILL.md` exists in your vault."
    )


async def handle_workflow(
    replier: Replier,
    *,
    action: str | None,
    name: str,
    workflow_lister: Callable[[], list[str]] | None,
    workflow_runner: WorkflowRunner | None,
) -> None:
    """``/crawdad workflow [list|run] [name]`` — ADAPT-003 dispatcher.

    Action semantics:

    * ``list`` (default) — return the registered workflow names. No
      MCP call. Always safe even when the agent loop is unwired.
    * ``run <name>`` — execute the named workflow via the supplied
      runner. ``name`` is required; the runner produces the final
      composer reply or raises one of the workflow-module errors,
      which this handler maps to a user-facing soft error.

    Unknown actions return scoped help text.
    """
    chosen = (action or WORKFLOW_ACTION_LIST).strip().lower()
    if chosen == WORKFLOW_ACTION_LIST:
        await _reply_workflow_list(replier, workflow_lister)
        return
    if chosen == WORKFLOW_ACTION_RUN:
        await _reply_workflow_run(replier, name=name, workflow_runner=workflow_runner)
        return
    await replier(
        f"unknown workflow action: {chosen!r}. "
        f"Try one of: {', '.join(_WORKFLOW_ACTIONS)}."
    )


async def _reply_workflow_list(
    replier: Replier, workflow_lister: Callable[[], list[str]] | None
) -> None:
    """Format and send the workflow registry to the user."""
    if workflow_lister is None:
        await replier(_WORKFLOW_LISTER_MISSING_REPLY)
        return
    names = workflow_lister()
    if not names:
        await replier(
            "No workflows registered yet. Drop a `*.workflow.yaml` file under "
            "`<vault>/00-Creek-Meta/Workflows/` to author one."
        )
        return
    lines = ["**Workflows**"]
    lines.extend(f"- `/crawdad workflow run {n}`" for n in names)
    await replier("\n".join(lines))


async def _reply_workflow_run(
    replier: Replier,
    *,
    name: str,
    workflow_runner: WorkflowRunner | None,
) -> None:
    """Drive a workflow run by name, mapping errors to soft replies."""
    cleaned = name.strip()
    if not cleaned:
        await replier(
            "I need a workflow name to run. Try "
            "`/crawdad workflow list` to see the registered workflows."
        )
        return
    if workflow_runner is None:
        await replier(_WORKFLOW_RUNNER_MISSING_REPLY)
        return
    try:
        reply = await workflow_runner(cleaned, {})
    except Exception as exc:
        _LOGGER.warning("workflow %r failed: %s", cleaned, exc)
        await replier(f"workflow `{cleaned}` could not run: {exc}")
        return
    await replier(reply)


async def handle_help(replier: Replier) -> None:
    """Compose a help summary listing every ``/crawdad`` command.

    Not registered as a Discord slash command — Discord's autocomplete
    UI already surfaces the registered commands and their descriptions.
    ``handle_help`` is kept as a public helper so:

    * Tests can drive the help-text format directly.
    * Future surfaces (a v1.1 ``/crawdad help`` variant, an operator
      REPL, a Discord rich-embed expansion) can reuse the canonical
      help string without duplicating the command list.
    """
    lines = ["**`/crawdad` commands**"]
    for name in CRAWDAD_COMMANDS:
        lines.append(f"- `/crawdad {name}` — {_COMMAND_DESCRIPTIONS[name]}")
    lines.append("")
    lines.append("Bare `/crawdad` (no subcommand) is equivalent to `/crawdad reflect`.")
    await replier("\n".join(lines))


# -----------------------------------------------------------------------------
# discord.py CommandTree wiring
# -----------------------------------------------------------------------------


def _interaction_allowed(interaction: Any, config: CrawDadConfig) -> bool:
    """Return True when the interaction's user AND channel are allowlisted.

    Issue #468: the author slash callbacks (`ask`/`draft`) must honour
    CrawDad's personal-use allowlist (FEAT-013) the same way free-text
    messages do in :func:`crawdad.bot._passes_allowlist`. Delegates to
    :meth:`crawdad.config.CrawDadConfig.is_allowed` so the AND-gate
    semantics live in exactly one place.

    Args:
        interaction: A ``discord.Interaction``-shaped object exposing
            ``user.id`` and ``channel_id``.
        config: The runtime config carrying the user / channel allowlists.

    Returns:
        ``True`` when the callback should proceed; ``False`` when it must
        silently ignore the interaction (no defer, no reply).
    """
    return config.is_allowed(
        user_id=interaction.user.id, channel_id=interaction.channel_id
    )


def register(
    tree: _TreeLike,
    *,
    config: CrawDadConfig,
    loop_runner: LoopRunner,
    register_switcher: RegisterSwitcher | None = None,
    workflow_lister: Callable[[], list[str]] | None = None,
    workflow_runner: WorkflowRunner | None = None,
) -> int:
    """Register every ``/crawdad`` slash command on the supplied tree.

    Each callback ``defer()``s the Discord interaction so the loop has
    its full :data:`crawdad.config.MAX_LOOP_ROUNDS` budget before the
    SDK times out at 3 seconds. The actual reply lands via
    ``interaction.followup.send``.

    ``register_switcher`` (FEAT-029) is the synchronous callback the
    ``/crawdad register`` command invokes to swap the active voice
    register. ``None`` keeps the command wired but the handler short-
    circuits to a soft-error reply explaining the feature is unwired.

    ``workflow_lister`` and ``workflow_runner`` (ADAPT-003) supply the
    ``list`` and ``run`` subactions of ``/crawdad workflow``. Both
    default to ``None`` (e.g. tests / no-tool-surface sessions); in
    that case the workflow command soft-errors instead of crashing.

    ``config`` (issue #468) supplies the personal-use allowlist. The
    author callbacks (`ask`/`draft`) gate on it BEFORE deferring so a
    non-allowlisted user / channel gets no response at all (silent
    ignore, matching :func:`crawdad.bot._passes_allowlist`). The other
    callbacks are intentionally left ungated in this change (scope
    fence); the broader gap is tracked separately.

    Returns the count of advertised commands (always
    ``len(CRAWDAD_COMMANDS)`` since the registration helpers don't
    fail). The return value is a sanity-check signal callers can pin
    in tests to catch silent additions or removals.
    """
    _register_reflect(tree, loop_runner=loop_runner)
    _register_checkin(tree, loop_runner=loop_runner)
    _register_surface(tree, loop_runner=loop_runner)
    _register_draft(tree, config=config, loop_runner=loop_runner)
    _register_ask(tree, config=config, loop_runner=loop_runner)
    _register_save(tree, loop_runner=loop_runner)
    _register_register(tree, register_switcher=register_switcher)
    _register_workflow(
        tree, workflow_lister=workflow_lister, workflow_runner=workflow_runner
    )
    return len(CRAWDAD_COMMANDS)


def _register_reflect(tree: _TreeLike, *, loop_runner: LoopRunner) -> None:
    """Wire the ``/crawdad reflect`` Discord callback onto *tree*."""

    @tree.command(name="reflect", description=_COMMAND_DESCRIPTIONS["reflect"])
    async def _callback(interaction: Any) -> None:
        """Discord callback for ``/crawdad reflect`` (deferred)."""
        await interaction.response.defer()
        await handle_reflect(interaction.followup.send, loop_runner=loop_runner)


def _register_checkin(tree: _TreeLike, *, loop_runner: LoopRunner) -> None:
    """Wire the ``/crawdad checkin`` Discord callback onto *tree*."""

    @tree.command(name="checkin", description=_COMMAND_DESCRIPTIONS["checkin"])
    async def _callback(interaction: Any) -> None:
        """Discord callback for ``/crawdad checkin`` (deferred)."""
        await interaction.response.defer()
        await handle_checkin(interaction.followup.send, loop_runner=loop_runner)


def _register_surface(tree: _TreeLike, *, loop_runner: LoopRunner) -> None:
    """Wire the ``/crawdad surface`` Discord callback onto *tree*."""

    @tree.command(name="surface", description=_COMMAND_DESCRIPTIONS["surface"])
    async def _callback(interaction: Any) -> None:
        """Discord callback for ``/crawdad surface`` (deferred)."""
        await interaction.response.defer()
        await handle_surface(interaction.followup.send, loop_runner=loop_runner)


def _register_draft(
    tree: _TreeLike, *, config: CrawDadConfig, loop_runner: LoopRunner
) -> None:
    """Wire the ``/crawdad draft`` Discord callback onto *tree*."""

    @tree.command(name="draft", description=_COMMAND_DESCRIPTIONS["draft"])
    async def _callback(interaction: Any, topic: str) -> None:
        """Discord callback for ``/crawdad draft <topic>`` (deferred).

        ``topic`` has no default value so Discord's autocomplete UI
        marks the parameter as required and disables submit until the
        user fills it in. The runtime empty-string guard in
        :func:`handle_draft` stays as defense-in-depth.

        Issue #468: a non-allowlisted user / channel is silently ignored
        (no defer, no followup) BEFORE the interaction is deferred.
        """
        if not _interaction_allowed(interaction, config):
            return
        await interaction.response.defer()
        await handle_draft(
            interaction.followup.send, topic=topic, loop_runner=loop_runner
        )


def _register_ask(
    tree: _TreeLike, *, config: CrawDadConfig, loop_runner: LoopRunner
) -> None:
    """Wire the ``/crawdad ask`` Discord callback onto *tree*."""

    @tree.command(name="ask", description=_COMMAND_DESCRIPTIONS["ask"])
    async def _callback(interaction: Any, question: str) -> None:
        """Discord callback for ``/crawdad ask <question>`` (deferred).

        ``question`` has no default value so Discord's autocomplete UI
        marks the parameter as required and disables submit until the
        user fills it in. The runtime empty-string guard in
        :func:`handle_ask` stays as defense-in-depth.

        Issue #468: a non-allowlisted user / channel is silently ignored
        (no defer, no followup) BEFORE the interaction is deferred.
        """
        if not _interaction_allowed(interaction, config):
            return
        await interaction.response.defer()
        await handle_ask(
            interaction.followup.send, question=question, loop_runner=loop_runner
        )


def _register_save(tree: _TreeLike, *, loop_runner: LoopRunner) -> None:
    """Wire the ``/crawdad save`` Discord callback onto *tree*."""

    @tree.command(name="save", description=_COMMAND_DESCRIPTIONS["save"])
    async def _callback(interaction: Any, content: str) -> None:
        """Discord callback for ``/crawdad save <content>`` (deferred).

        ``content`` has no default; Discord marks the parameter as
        required in the slash-command UI. Runtime guard in
        :func:`handle_save` stays as defense-in-depth.
        """
        await interaction.response.defer()
        await handle_save(
            interaction.followup.send, content=content, loop_runner=loop_runner
        )


def _register_register(
    tree: _TreeLike, *, register_switcher: RegisterSwitcher | None
) -> None:
    """Wire the ``/crawdad register`` Discord callback onto *tree* (FEAT-029)."""

    @tree.command(name="register", description=_COMMAND_DESCRIPTIONS["register"])
    async def _callback(interaction: Any, name: str) -> None:
        """Discord callback for ``/crawdad register <name>`` (deferred)."""
        await interaction.response.defer()
        await handle_register(
            interaction.followup.send,
            name=name,
            register_switcher=register_switcher,
        )


def _register_workflow(
    tree: _TreeLike,
    *,
    workflow_lister: Callable[[], list[str]] | None,
    workflow_runner: WorkflowRunner | None,
) -> None:
    """Wire the ``/crawdad workflow`` Discord callback onto *tree*."""

    @tree.command(name="workflow", description=_COMMAND_DESCRIPTIONS["workflow"])
    async def _callback(interaction: Any, action: str = "list", name: str = "") -> None:
        """Discord callback for ``/crawdad workflow [action] [name]`` (deferred)."""
        await interaction.response.defer()
        await handle_workflow(
            interaction.followup.send,
            action=action,
            name=name,
            workflow_lister=workflow_lister,
            workflow_runner=workflow_runner,
        )
