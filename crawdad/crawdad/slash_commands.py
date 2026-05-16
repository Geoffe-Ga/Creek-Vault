"""Discord slash commands for CrawDad (FEAT-016).

Six commands form the `/crawdad` family — `reflect`, `checkin`,
`surface`, `draft`, `save`, `workflow`. Each routes through the
FEAT-015 agent loop by constructing a pre-baked user message and
running it through an injected ``loop_runner`` callable. The Sonnet
composer wraps the tool results in the user's voice.

The handlers are free functions that take a tiny ``Replier`` callable
(``async def (str) -> None``) so they can be unit-tested without
discord.py. ``register()`` wraps each handler in a discord.py
``app_commands`` callback that ``defer()``s the interaction (LLM
calls exceed Discord's 3-second response window), runs the handler,
and ``followup.send()``s the reply.

``/crawdad workflow`` is a v1.0 stub. Full workflow DSL ships with the
next FEAT; see ``plans/2026-05-05_comparative-analysis/candidates/
ADAPT-003-*.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

_LOGGER = logging.getLogger("crawdad.slash_commands")

# A callable that drives one turn of the FEAT-015 loop and returns the
# user-facing reply string. The CLI builds one by closing over the
# per-session router, composer, MCP client, history, state, and skills
# components — see :func:`crawdad.cli._build_loop_runner`.
LoopRunner = Callable[[str], Awaitable[str]]

# Async callable that posts a message back to the Discord user.
# Wraps ``discord.Interaction.followup.send`` in production; tests
# substitute a list-appending fake.
Replier = Callable[[str], Awaitable[None]]


CRAWDAD_COMMANDS: tuple[str, ...] = (
    "reflect",
    "checkin",
    "surface",
    "draft",
    "save",
    "workflow",
)

_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "reflect": "Open reflective conversation mode (FEAT-015 loop).",
    "checkin": "Wavelength check-in — read the current phase + dosage state.",
    "surface": "Surface paradoxes, liminal content, or emerging themes.",
    "draft": "Draft an essay on a topic (routes through creek.mine + creek.draft).",
    "save": "File the supplied content back to the vault via creek.save.",
    "workflow": "List or run named workflows (v1.0 stub — full DSL in v1.1).",
}

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
_WORKFLOW_STUB_REPLY = (
    "no workflows yet — coming in v1.1.\n\n"
    "The workflow DSL (ADAPT-003) will let you compose multi-step plans like "
    "`/crawdad workflow run weekly-review`. v1.0 ships only this `list` stub."
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


async def handle_draft(
    replier: Replier, *, topic: str, loop_runner: LoopRunner
) -> None:
    """``/crawdad draft <topic>`` — mine + draft on the supplied topic."""
    cleaned = topic.strip()
    if not cleaned:
        await replier(
            "I need a topic to draft on. Try `/crawdad draft <topic>` — for "
            "example, `/crawdad draft phase transitions`."
        )
        return
    prompt = (
        f"Draft an essay on the following topic: {cleaned}.\n\n"
        "Route through creek.mine to surface relevant seeds first, then "
        "creek.draft to generate the essay. Voice fidelity is owned by "
        "creek.draft's skill stack — don't re-draft inline."
    )
    reply = await loop_runner(prompt)
    await replier(reply)


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


async def handle_workflow(replier: Replier, *, subaction: str | None) -> None:
    """``/crawdad workflow [list]`` — v1.0 stub.

    ``list`` (the default) returns the documented placeholder. Any
    other subaction returns scoped help — no MCP call, no stack trace.
    """
    if subaction in (None, "list"):
        await replier(_WORKFLOW_STUB_REPLY)
        return
    await replier(
        f"unknown workflow subcommand: {subaction!r}. "
        "Only `/crawdad workflow list` is available in v1.0."
    )


async def handle_help(replier: Replier) -> None:
    """``/crawdad help`` — list every command with a one-line description."""
    lines = ["**`/crawdad` commands**"]
    for name in CRAWDAD_COMMANDS:
        lines.append(f"- `/crawdad {name}` — {_COMMAND_DESCRIPTIONS[name]}")
    lines.append("")
    lines.append("Bare `/crawdad` (no subcommand) is equivalent to `/crawdad reflect`.")
    await replier("\n".join(lines))


# -----------------------------------------------------------------------------
# discord.py CommandTree wiring
# -----------------------------------------------------------------------------


def register(tree: _TreeLike, *, loop_runner: LoopRunner) -> int:
    """Register every `/crawdad` slash command on the supplied tree.

    Each callback ``defer()``s the Discord interaction so the loop has
    its full :data:`crawdad.config.MAX_LOOP_ROUNDS` budget before the
    SDK times out at 3 seconds. The actual reply lands via
    ``interaction.followup.send``.

    Returns the number of registered commands so callers can sanity-check
    the wiring.
    """
    _register_reflect(tree, loop_runner=loop_runner)
    _register_checkin(tree, loop_runner=loop_runner)
    _register_surface(tree, loop_runner=loop_runner)
    _register_draft(tree, loop_runner=loop_runner)
    _register_save(tree, loop_runner=loop_runner)
    _register_workflow(tree)
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


def _register_draft(tree: _TreeLike, *, loop_runner: LoopRunner) -> None:
    """Wire the ``/crawdad draft`` Discord callback onto *tree*."""

    @tree.command(name="draft", description=_COMMAND_DESCRIPTIONS["draft"])
    async def _callback(interaction: Any, topic: str = "") -> None:
        """Discord callback for ``/crawdad draft <topic>`` (deferred)."""
        await interaction.response.defer()
        await handle_draft(
            interaction.followup.send, topic=topic, loop_runner=loop_runner
        )


def _register_save(tree: _TreeLike, *, loop_runner: LoopRunner) -> None:
    """Wire the ``/crawdad save`` Discord callback onto *tree*."""

    @tree.command(name="save", description=_COMMAND_DESCRIPTIONS["save"])
    async def _callback(interaction: Any, content: str = "") -> None:
        """Discord callback for ``/crawdad save <content>`` (deferred)."""
        await interaction.response.defer()
        await handle_save(
            interaction.followup.send, content=content, loop_runner=loop_runner
        )


def _register_workflow(tree: _TreeLike) -> None:
    """Wire the ``/crawdad workflow`` Discord callback onto *tree*."""

    @tree.command(name="workflow", description=_COMMAND_DESCRIPTIONS["workflow"])
    async def _callback(interaction: Any, subaction: str = "list") -> None:
        """Discord callback for ``/crawdad workflow [subaction]`` (deferred)."""
        await interaction.response.defer()
        await handle_workflow(interaction.followup.send, subaction=subaction)
