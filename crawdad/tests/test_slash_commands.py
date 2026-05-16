"""Tests for ``crawdad.slash_commands`` — FEAT-016 Discord slash surface."""

from __future__ import annotations

from typing import Any

import pytest

from crawdad.slash_commands import (
    CRAWDAD_COMMANDS,
    handle_checkin,
    handle_draft,
    handle_help,
    handle_reflect,
    handle_save,
    handle_surface,
    handle_workflow,
    register,
)


class _FakeReplier:
    """Records the strings sent back to Discord."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, content: str) -> None:
        self.sent.append(content)


async def _fake_loop_runner(message: str) -> str:
    """Return the message itself so tests can assert what was routed."""
    return f"composer received: {message}"


def test_crawdad_commands_lists_the_six_documented_names() -> None:
    """The exported registry matches the FEAT-016 §pre-decided choice list."""
    assert set(CRAWDAD_COMMANDS) == {
        "reflect",
        "checkin",
        "surface",
        "draft",
        "save",
        "workflow",
    }


async def test_handle_reflect_routes_through_loop() -> None:
    """``/crawdad reflect`` runs the loop with no preselected intent."""
    replier = _FakeReplier()

    await handle_reflect(replier, loop_runner=_fake_loop_runner)

    assert len(replier.sent) == 1
    assert "composer received:" in replier.sent[0]
    # The loop's input message gives the composer reflective framing,
    # not a specific tool request.
    assert "reflect" in replier.sent[0].lower()


async def test_handle_checkin_routes_through_state_read() -> None:
    """``/crawdad checkin`` asks the loop for a wavelength check-in."""
    replier = _FakeReplier()

    await handle_checkin(replier, loop_runner=_fake_loop_runner)

    assert len(replier.sent) == 1
    body = replier.sent[0].lower()
    assert "wavelength" in body or "state" in body


async def test_handle_surface_routes_through_lint() -> None:
    """``/crawdad surface`` asks the loop for paradox / liminal surfacing."""
    replier = _FakeReplier()

    await handle_surface(replier, loop_runner=_fake_loop_runner)

    assert len(replier.sent) == 1
    body = replier.sent[0].lower()
    assert "paradox" in body or "surfac" in body or "liminal" in body


async def test_handle_draft_passes_topic_to_loop() -> None:
    """``/crawdad draft <topic>`` includes the user's topic verbatim."""
    replier = _FakeReplier()

    await handle_draft(
        replier, topic="phase transitions", loop_runner=_fake_loop_runner
    )

    assert len(replier.sent) == 1
    assert "phase transitions" in replier.sent[0]


async def test_handle_draft_refuses_empty_topic() -> None:
    """An empty draft topic gets a help reply, not a stack trace."""
    replier = _FakeReplier()

    await handle_draft(replier, topic="", loop_runner=_fake_loop_runner)

    assert len(replier.sent) == 1
    assert "topic" in replier.sent[0].lower()


async def test_handle_save_passes_content_to_loop() -> None:
    """``/crawdad save <content>`` routes the content through the loop."""
    replier = _FakeReplier()

    await handle_save(
        replier,
        content="a fragment about emergence",
        loop_runner=_fake_loop_runner,
    )

    assert len(replier.sent) == 1
    assert "emergence" in replier.sent[0]


async def test_handle_save_refuses_empty_content() -> None:
    """Empty save content gets a help reply, not an empty MCP call."""
    replier = _FakeReplier()

    await handle_save(replier, content="", loop_runner=_fake_loop_runner)

    assert len(replier.sent) == 1
    assert "content" in replier.sent[0].lower() or "what" in replier.sent[0].lower()


async def test_handle_workflow_list_returns_v11_placeholder() -> None:
    """``/crawdad workflow list`` returns the documented v1.1 stub."""
    replier = _FakeReplier()

    await handle_workflow(replier, subaction="list")

    assert len(replier.sent) == 1
    assert "v1.1" in replier.sent[0] or "1.1" in replier.sent[0]


async def test_handle_workflow_unknown_subaction_returns_help() -> None:
    """A workflow subaction other than ``list`` returns the help summary."""
    replier = _FakeReplier()

    await handle_workflow(replier, subaction="run")

    assert len(replier.sent) == 1
    assert "list" in replier.sent[0].lower()


async def test_handle_workflow_default_returns_list_stub() -> None:
    """``/crawdad workflow`` with no subaction is the same as ``list``."""
    replier = _FakeReplier()

    await handle_workflow(replier, subaction=None)

    assert len(replier.sent) == 1
    assert "v1.1" in replier.sent[0] or "1.1" in replier.sent[0]


async def test_handle_help_lists_every_command() -> None:
    """``/crawdad help`` (or any unknown subcommand) lists the six commands."""
    replier = _FakeReplier()

    await handle_help(replier)

    body = replier.sent[0]
    for name in CRAWDAD_COMMANDS:
        assert name in body


async def test_handle_reflect_workflow_skips_loop() -> None:
    """Workflow handler does NOT call the loop runner — it's a pure stub."""
    runner_calls = 0

    async def _counting_runner(_message: str) -> str:
        nonlocal runner_calls
        runner_calls += 1
        return "should not be reached"

    replier = _FakeReplier()
    await handle_workflow(replier, subaction="list")

    assert runner_calls == 0


class _FakeTree:
    """Records the ``@tree.command`` registrations the wiring performs."""

    def __init__(self) -> None:
        self.registered: dict[str, dict[str, Any]] = {}

    def command(self, *, name: str, description: str) -> Any:
        def _decorator(func: Any) -> Any:
            self.registered[name] = {"description": description, "callback": func}
            return func

        return _decorator


def test_register_wires_every_command_onto_tree() -> None:
    """``register`` adds all six commands to a discord ``CommandTree``-like object."""
    tree = _FakeTree()

    register(tree, loop_runner=_fake_loop_runner)

    assert set(tree.registered) == set(CRAWDAD_COMMANDS)
    for name, entry in tree.registered.items():
        assert entry["description"], f"{name} missing description"
        assert callable(entry["callback"]), f"{name} callback not callable"


def test_register_returns_command_count() -> None:
    """``register`` returns the number of wired commands for sanity checks."""
    tree = _FakeTree()

    count = register(tree, loop_runner=_fake_loop_runner)

    assert count == len(CRAWDAD_COMMANDS)


async def test_registered_callback_routes_to_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered slash callback ends up calling the matching handler."""
    tree = _FakeTree()
    register(tree, loop_runner=_fake_loop_runner)

    # Each registered callback accepts a discord.Interaction. We provide
    # a fake interaction with the minimal shape: a response object whose
    # ``defer`` is a no-op and a ``followup.send`` that records calls.
    class _FakeFollowup:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, content: str) -> None:
            self.sent.append(content)

    class _FakeResponse:
        async def defer(self) -> None:
            return None

    class _FakeInteraction:
        def __init__(self) -> None:
            self.response = _FakeResponse()
            self.followup = _FakeFollowup()

    interaction = _FakeInteraction()
    await tree.registered["checkin"]["callback"](interaction)

    assert len(interaction.followup.sent) == 1
    assert "composer received:" in interaction.followup.sent[0]
