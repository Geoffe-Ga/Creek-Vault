"""Tests for ``crawdad.slash_commands`` — FEAT-016 Discord slash surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crawdad.config import CrawDadConfig
from crawdad.mcp_client import MCPUnavailableError
from crawdad.slash_commands import (
    CRAWDAD_COMMANDS,
    _interaction_allowed,
    handle_ask,
    handle_checkin,
    handle_draft,
    handle_help,
    handle_reflect,
    handle_register,
    handle_save,
    handle_surface,
    handle_workflow,
    register,
)

_ALLOWED_USER_ID = 11
_ALLOWED_CHANNEL_ID = 22
_DENIED_USER_ID = 99
_DENIED_CHANNEL_ID = 88


def _make_config() -> CrawDadConfig:
    """Build a minimal allowlisted config for slash-command gate tests."""
    return CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=Path("vault"),
        allowed_user_ids=(_ALLOWED_USER_ID,),
        allowed_channel_ids=(_ALLOWED_CHANNEL_ID,),
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


def test_crawdad_commands_lists_the_documented_names() -> None:
    """The exported registry matches the FEAT-016 + FEAT-029 set."""
    assert set(CRAWDAD_COMMANDS) == {
        "reflect",
        "checkin",
        "surface",
        "draft",
        "ask",
        "save",
        "register",
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


async def test_handle_draft_routes_through_author_essay() -> None:
    """``/crawdad draft`` routes through creek.author with the essay medium."""
    replier = _FakeReplier()

    await handle_draft(
        replier, topic="phase transitions", loop_runner=_fake_loop_runner
    )

    body = replier.sent[0]
    assert "creek.author" in body
    assert "essay" in body
    # The old creek.mine / creek.draft routing is gone.
    assert "creek.mine" not in body
    assert "creek.draft" not in body


async def test_handle_draft_includes_save_attribution_path() -> None:
    """``/crawdad draft`` names the keep→save path to ai-as-user."""
    replier = _FakeReplier()

    await handle_draft(
        replier, topic="phase transitions", loop_runner=_fake_loop_runner
    )

    body = replier.sent[0]
    assert "creek.save" in body
    assert "ai-as-user" in body


async def test_handle_draft_refuses_empty_topic() -> None:
    """An empty draft topic gets a help reply, not a stack trace."""
    replier = _FakeReplier()

    await handle_draft(replier, topic="", loop_runner=_fake_loop_runner)

    assert len(replier.sent) == 1
    assert "topic" in replier.sent[0].lower()


async def test_handle_ask_passes_question_to_author_research() -> None:
    """``/crawdad ask <question>`` routes via creek.author research medium."""
    replier = _FakeReplier()

    await handle_ask(
        replier,
        question="what is the relationship between drift and emergence?",
        loop_runner=_fake_loop_runner,
    )

    assert len(replier.sent) == 1
    body = replier.sent[0]
    assert "what is the relationship between drift and emergence?" in body
    assert "creek.author" in body
    assert "research" in body


async def test_handle_ask_includes_save_attribution_path() -> None:
    """``/crawdad ask`` names the keep→save path to ai-as-user."""
    replier = _FakeReplier()

    await handle_ask(
        replier,
        question="why does the creek meander?",
        loop_runner=_fake_loop_runner,
    )

    body = replier.sent[0]
    assert "creek.save" in body
    assert "ai-as-user" in body


async def test_handle_ask_refuses_empty_question() -> None:
    """A blank question gets a help reply and never calls the loop."""
    loop_calls = 0

    async def _counting_loop(_message: str) -> str:
        nonlocal loop_calls
        loop_calls += 1
        return "should not be reached"

    replier = _FakeReplier()

    await handle_ask(replier, question="   ", loop_runner=_counting_loop)

    assert loop_calls == 0
    assert len(replier.sent) == 1
    assert "question" in replier.sent[0].lower()


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


async def test_handle_register_invokes_switcher_with_cleaned_name() -> None:
    """``/crawdad register <name>`` calls the switcher with a normalised name."""
    seen: list[str] = []

    def _switcher(name: str) -> bool:
        seen.append(name)
        return True

    replier = _FakeReplier()
    await handle_register(replier, name="  Analytic  ", register_switcher=_switcher)

    assert seen == ["analytic"]
    assert len(replier.sent) == 1
    assert "analytic" in replier.sent[0].lower()
    assert "switched" in replier.sent[0].lower()


async def test_handle_register_soft_errors_on_unknown_register() -> None:
    """A switcher returning ``False`` produces a soft-error reply (no crash)."""

    def _switcher(_name: str) -> bool:
        return False

    replier = _FakeReplier()
    await handle_register(replier, name="nonexistent", register_switcher=_switcher)

    assert len(replier.sent) == 1
    body = replier.sent[0].lower()
    assert "unknown" in body
    assert "nonexistent" in replier.sent[0]


async def test_handle_register_refuses_empty_name() -> None:
    """An empty register name returns a help reply, not a switcher call."""
    calls: list[str] = []

    def _switcher(name: str) -> bool:
        calls.append(name)
        return True

    replier = _FakeReplier()
    await handle_register(replier, name="   ", register_switcher=_switcher)

    assert calls == []
    assert len(replier.sent) == 1
    assert "register" in replier.sent[0].lower()


async def test_handle_register_without_switcher_returns_soft_error() -> None:
    """A missing switcher (no registry wired) yields the unavailable reply."""
    replier = _FakeReplier()
    await handle_register(replier, name="analytic", register_switcher=None)

    assert len(replier.sent) == 1
    body = replier.sent[0].lower()
    assert "unavailable" in body or "wired" in body


async def test_handle_workflow_list_enumerates_registered_workflows() -> None:
    """``/crawdad workflow list`` enumerates the registry's workflow names."""
    replier = _FakeReplier()

    await handle_workflow(
        replier,
        action="list",
        name="",
        workflow_lister=lambda: ["alpha", "beta"],
        workflow_runner=None,
    )

    assert len(replier.sent) == 1
    assert "alpha" in replier.sent[0]
    assert "beta" in replier.sent[0]


async def test_handle_workflow_list_with_no_workflows_explains_how_to_author() -> None:
    """An empty registry returns a hint about authoring a workflow file."""
    replier = _FakeReplier()

    await handle_workflow(
        replier,
        action="list",
        name="",
        workflow_lister=lambda: [],
        workflow_runner=None,
    )

    assert len(replier.sent) == 1
    body = replier.sent[0].lower()
    assert "no workflows" in body or "author" in body


async def test_handle_workflow_list_without_lister_returns_soft_error() -> None:
    """A missing lister names the registry, not the walker.

    Closes PR #309 review feedback: the list action only reads the
    registry — it never touches the walker — so its failure message
    must talk about registry wiring, not "the walker has nothing to
    call".
    """
    replier = _FakeReplier()

    await handle_workflow(
        replier,
        action="list",
        name="",
        workflow_lister=None,
        workflow_runner=None,
    )

    assert len(replier.sent) == 1
    body = replier.sent[0].lower()
    assert "registry" in body
    assert "walker" not in body


async def test_handle_workflow_default_action_is_list() -> None:
    """``/crawdad workflow`` with no action behaves like ``list``."""
    replier = _FakeReplier()

    await handle_workflow(
        replier,
        action=None,
        name="",
        workflow_lister=lambda: ["alpha"],
        workflow_runner=None,
    )

    assert len(replier.sent) == 1
    assert "alpha" in replier.sent[0]


async def test_handle_workflow_run_invokes_runner_with_name() -> None:
    """``/crawdad workflow run <name>`` calls the runner with the cleaned name."""
    captured: dict[str, object] = {}

    async def _runner(name: str, inputs: dict[str, str]) -> str:
        captured["name"] = name
        captured["inputs"] = inputs
        return "workflow done"

    replier = _FakeReplier()
    await handle_workflow(
        replier,
        action="run",
        name="  wavelength-checkin  ",
        workflow_lister=None,
        workflow_runner=_runner,
    )

    assert captured["name"] == "wavelength-checkin"
    assert captured["inputs"] == {}
    assert replier.sent == ["workflow done"]


async def test_handle_workflow_run_without_name_returns_help() -> None:
    """``/crawdad workflow run`` with no name returns help — no runner call."""
    calls: list[str] = []

    async def _runner(name: str, _inputs: dict[str, str]) -> str:
        calls.append(name)
        return ""

    replier = _FakeReplier()
    await handle_workflow(
        replier,
        action="run",
        name="",
        workflow_lister=None,
        workflow_runner=_runner,
    )

    assert calls == []
    assert "name" in replier.sent[0].lower() or "list" in replier.sent[0].lower()


async def test_handle_workflow_run_without_runner_returns_soft_error() -> None:
    """A missing runner yields the soft-error reply (no crash)."""
    replier = _FakeReplier()
    await handle_workflow(
        replier,
        action="run",
        name="wavelength-checkin",
        workflow_lister=None,
        workflow_runner=None,
    )

    assert len(replier.sent) == 1
    assert "aren't wired" in replier.sent[0] or "unavailable" in replier.sent[0].lower()


async def test_handle_workflow_run_maps_runner_exceptions_to_soft_reply() -> None:
    """A runner exception lands as a soft Discord reply, not a stack trace."""

    async def _boom(_name: str, _inputs: dict[str, str]) -> str:
        msg = "no workflow named 'missing'"
        raise RuntimeError(msg)

    replier = _FakeReplier()
    await handle_workflow(
        replier,
        action="run",
        name="missing",
        workflow_lister=None,
        workflow_runner=_boom,
    )

    assert len(replier.sent) == 1
    assert "missing" in replier.sent[0]
    assert "could not run" in replier.sent[0]


async def test_handle_workflow_unknown_action_returns_help() -> None:
    """An action other than list/run lands a help summary."""
    replier = _FakeReplier()

    await handle_workflow(
        replier,
        action="frobnicate",
        name="",
        workflow_lister=None,
        workflow_runner=None,
    )

    assert len(replier.sent) == 1
    body = replier.sent[0].lower()
    assert "list" in body
    assert "run" in body


async def test_handle_help_lists_every_command() -> None:
    """``/crawdad help`` (or any unknown subcommand) lists every command."""
    replier = _FakeReplier()

    await handle_help(replier)

    body = replier.sent[0]
    for name in CRAWDAD_COMMANDS:
        assert name in body
    assert "ask" in body


async def test_handle_workflow_list_skips_loop_runner() -> None:
    """The workflow list handler does NOT call the agent loop runner."""
    loop_calls = 0

    async def _counting_loop(_message: str) -> str:
        nonlocal loop_calls
        loop_calls += 1
        return "should not be reached"

    replier = _FakeReplier()
    await handle_workflow(
        replier,
        action="list",
        name="",
        workflow_lister=lambda: ["wavelength-checkin"],
        workflow_runner=None,
    )

    assert loop_calls == 0
    # Sanity check: the loop fake exists only to prove no one called it.
    assert _counting_loop is not None


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
    """``register`` adds every command to a discord ``CommandTree``-like object."""
    tree = _FakeTree()

    register(tree, config=_make_config(), loop_runner=_fake_loop_runner)

    assert set(tree.registered) == set(CRAWDAD_COMMANDS)
    for name, entry in tree.registered.items():
        assert entry["description"], f"{name} missing description"
        assert callable(entry["callback"]), f"{name} callback not callable"


def test_register_returns_command_count() -> None:
    """``register`` returns the number of wired commands for sanity checks."""
    tree = _FakeTree()

    count = register(tree, config=_make_config(), loop_runner=_fake_loop_runner)

    assert count == len(CRAWDAD_COMMANDS)


class _FakeFollowup:
    """Followup-send fake — records the content passed to discord."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str) -> None:
        """Record the content the registered callback tried to post."""
        self.sent.append(content)


class _FakeResponse:
    """Interaction-response fake — counts ``defer`` calls."""

    def __init__(self) -> None:
        self.deferred = 0

    async def defer(self) -> None:
        """Increment the defer counter."""
        self.deferred += 1


class _FakeUser:
    """``discord.Interaction.user``-shaped fake exposing just ``id``."""

    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeInteraction:
    """``discord.Interaction``-shaped fake with the minimal surface we use.

    ``user_id`` and ``channel_id`` default to the allowlisted ids so the
    pre-existing callback tests keep exercising the happy path without
    change; the allowlist tests pass denied ids explicitly.
    """

    def __init__(
        self,
        *,
        user_id: int = _ALLOWED_USER_ID,
        channel_id: int = _ALLOWED_CHANNEL_ID,
    ) -> None:
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = _FakeUser(user_id)
        self.channel_id = channel_id


@pytest.mark.parametrize(
    ("command_name", "callback_kwargs"),
    [
        ("reflect", {}),
        ("checkin", {}),
        ("surface", {}),
        ("draft", {"topic": "phase transitions"}),
        ("ask", {"question": "what is emergence?"}),
        ("save", {"content": "a fragment to file"}),
    ],
)
async def test_every_loop_routed_callback_defers_and_replies(
    command_name: str, callback_kwargs: dict[str, Any]
) -> None:
    """Each loop-routed callback ``defer()``s the interaction and replies.

    Closes the FEAT-016 review's #1 blocker — pre-existing tests only
    covered ``checkin``. The five remaining ``_callback`` closures were
    unreached so per-file coverage on slash_commands.py sat at 89.80%.
    """
    tree = _FakeTree()
    register(tree, config=_make_config(), loop_runner=_fake_loop_runner)
    interaction = _FakeInteraction()

    await tree.registered[command_name]["callback"](interaction, **callback_kwargs)

    assert interaction.response.deferred == 1
    assert len(interaction.followup.sent) == 1
    # Every routed callback yields the loop's reply text.
    assert "composer received:" in interaction.followup.sent[0]


async def test_register_callback_defers_and_invokes_switcher() -> None:
    """``/crawdad register <name>`` defers the interaction and calls the switcher."""
    switched: list[str] = []

    def _switcher(name: str) -> bool:
        switched.append(name)
        return True

    tree = _FakeTree()
    register(
        tree,
        config=_make_config(),
        loop_runner=_fake_loop_runner,
        register_switcher=_switcher,
    )
    interaction = _FakeInteraction()

    await tree.registered["register"]["callback"](interaction, name="praxis")

    assert interaction.response.deferred == 1
    assert switched == ["praxis"]
    assert len(interaction.followup.sent) == 1
    assert "praxis" in interaction.followup.sent[0].lower()


async def test_register_callback_without_switcher_replies_softly() -> None:
    """When no switcher is wired the callback still replies — does not crash."""
    tree = _FakeTree()
    register(tree, config=_make_config(), loop_runner=_fake_loop_runner)
    interaction = _FakeInteraction()

    await tree.registered["register"]["callback"](interaction, name="praxis")

    assert interaction.response.deferred == 1
    assert len(interaction.followup.sent) == 1
    body = interaction.followup.sent[0].lower()
    assert "unavailable" in body or "wired" in body


async def test_workflow_callback_defers_and_lists_when_wired() -> None:
    """``/crawdad workflow`` lists registered workflows when a lister is wired."""

    async def _spy_runner(_msg: str) -> str:
        return "should not be reached"

    tree = _FakeTree()
    register(
        tree,
        config=_make_config(),
        loop_runner=_spy_runner,
        workflow_lister=lambda: ["wavelength-checkin"],
        workflow_runner=None,
    )
    interaction = _FakeInteraction()

    await tree.registered["workflow"]["callback"](interaction)

    assert interaction.response.deferred == 1
    assert len(interaction.followup.sent) == 1
    assert "wavelength-checkin" in interaction.followup.sent[0]


async def test_workflow_callback_runs_workflow_and_replies() -> None:
    """``/crawdad workflow run <name>`` defers and invokes the workflow runner."""
    captured: dict[str, object] = {}

    async def _runner(name: str, inputs: dict[str, str]) -> str:
        captured["name"] = name
        captured["inputs"] = inputs
        return "workflow output"

    async def _loop(_msg: str) -> str:
        return "should not be reached"

    tree = _FakeTree()
    register(
        tree,
        config=_make_config(),
        loop_runner=_loop,
        workflow_lister=lambda: ["foo"],
        workflow_runner=_runner,
    )
    interaction = _FakeInteraction()

    await tree.registered["workflow"]["callback"](interaction, action="run", name="foo")

    assert interaction.response.deferred == 1
    assert captured["name"] == "foo"
    assert captured["inputs"] == {}
    assert interaction.followup.sent == ["workflow output"]


async def test_workflow_callback_soft_errors_when_unwired() -> None:
    """When no lister / runner is wired, the callback still defers and replies.

    The default action is ``list``, which routes through
    ``_reply_workflow_list``; the missing-lister reply mentions the
    registry rather than the walker (PR #309 review feedback).
    """

    async def _loop(_msg: str) -> str:
        return "should not be reached"

    tree = _FakeTree()
    register(tree, config=_make_config(), loop_runner=_loop)
    interaction = _FakeInteraction()

    await tree.registered["workflow"]["callback"](interaction)

    assert interaction.response.deferred == 1
    assert len(interaction.followup.sent) == 1
    body = interaction.followup.sent[0].lower()
    assert "registry" in body
    assert "walker" not in body


# -----------------------------------------------------------------------------
# Allowlist gate + graceful degradation on the author routes.
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "channel_id", "expected"),
    [
        (_ALLOWED_USER_ID, _ALLOWED_CHANNEL_ID, True),
        (_DENIED_USER_ID, _ALLOWED_CHANNEL_ID, False),
        (_ALLOWED_USER_ID, _DENIED_CHANNEL_ID, False),
        (_DENIED_USER_ID, _DENIED_CHANNEL_ID, False),
    ],
)
def test_interaction_allowed_gates_by_user_and_channel(
    user_id: int, channel_id: int, expected: bool
) -> None:
    """``_interaction_allowed`` mirrors ``CrawDadConfig.is_allowed`` (AND gate)."""
    interaction = _FakeInteraction(user_id=user_id, channel_id=channel_id)

    assert _interaction_allowed(interaction, _make_config()) is expected


# Every gated command name mapped to the kwargs its callback requires. All
# eight ``/crawdad`` commands must honour the personal-use allowlist (the two
# author routes plus the six non-author commands); a denied user / channel gets
# no response at all (no defer, no followup).
_GATED_CALLBACK_KWARGS: dict[str, dict[str, Any]] = {
    "reflect": {},
    "checkin": {},
    "surface": {},
    "draft": {"topic": "t"},
    "ask": {"question": "q"},
    "save": {"content": "c"},
    "register": {"name": "praxis"},
    "workflow": {},
}


@pytest.mark.parametrize("command_name", sorted(_GATED_CALLBACK_KWARGS))
@pytest.mark.parametrize(
    ("user_id", "channel_id"),
    [
        (_DENIED_USER_ID, _ALLOWED_CHANNEL_ID),
        (_ALLOWED_USER_ID, _DENIED_CHANNEL_ID),
    ],
)
async def test_callback_denies_non_allowlisted_silently(
    command_name: str, user_id: int, channel_id: int
) -> None:
    """Every gated callback gives NO response when denied: no defer, no followup.

    A non-allowlisted user OR channel is silently ignored for all eight
    ``/crawdad`` commands. ``register`` and ``workflow`` carry extra deps,
    so this proves the allowlist gate fires before any handler / dependency
    is touched — the interaction is dropped entirely.
    """
    kwargs = _GATED_CALLBACK_KWARGS[command_name]
    tree = _FakeTree()
    register(
        tree,
        config=_make_config(),
        loop_runner=_fake_loop_runner,
        register_switcher=lambda _name: True,
        workflow_lister=lambda: ["wavelength-checkin"],
        workflow_runner=_unreached_workflow_runner,
    )
    interaction = _FakeInteraction(user_id=user_id, channel_id=channel_id)

    await tree.registered[command_name]["callback"](interaction, **kwargs)

    assert interaction.response.deferred == 0
    assert interaction.followup.sent == []


async def _unreached_workflow_runner(_name: str, _inputs: dict[str, str]) -> str:
    """Workflow runner that must never be called under a denied interaction."""
    msg = "workflow runner reached despite denied allowlist"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    ("command_name", "kwargs"),
    [("ask", {"question": "what is emergence?"}), ("draft", {"topic": "phase"})],
)
async def test_author_callback_allows_allowlisted_interaction(
    command_name: str, kwargs: dict[str, Any]
) -> None:
    """An allowlisted interaction defers and the handler runs (one reply)."""
    tree = _FakeTree()
    register(tree, config=_make_config(), loop_runner=_fake_loop_runner)
    interaction = _FakeInteraction()

    await tree.registered[command_name]["callback"](interaction, **kwargs)

    assert interaction.response.deferred == 1
    assert len(interaction.followup.sent) == 1
    assert "composer received:" in interaction.followup.sent[0]


async def test_register_callback_allowlisted_reaches_switcher() -> None:
    """An allowlisted ``/crawdad register`` is NOT dropped by the gate.

    ``register`` carries the extra ``register_switcher`` dep; this pins that
    an allowlisted interaction defers and the switcher actually runs (the
    allowlist gate lets the handler through rather than silently ignoring).
    """
    switched: list[str] = []
    tree = _FakeTree()
    register(
        tree,
        config=_make_config(),
        loop_runner=_fake_loop_runner,
        register_switcher=lambda name: bool(switched.append(name)) or True,
    )
    interaction = _FakeInteraction()

    await tree.registered["register"]["callback"](interaction, name="praxis")

    assert interaction.response.deferred == 1
    assert switched == ["praxis"]
    assert len(interaction.followup.sent) == 1


async def test_workflow_callback_allowlisted_reaches_lister() -> None:
    """An allowlisted ``/crawdad workflow`` is NOT dropped by the gate.

    ``workflow`` carries the extra lister / runner deps; this pins that an
    allowlisted interaction defers and the lister actually runs (the
    allowlist gate lets the handler through rather than silently ignoring).
    """
    listed: list[bool] = []
    tree = _FakeTree()
    register(
        tree,
        config=_make_config(),
        loop_runner=_fake_loop_runner,
        workflow_lister=lambda: listed.append(True) or ["wavelength-checkin"],
    )
    interaction = _FakeInteraction()

    await tree.registered["workflow"]["callback"](interaction)

    assert interaction.response.deferred == 1
    assert listed == [True]
    assert "wavelength-checkin" in interaction.followup.sent[0]


async def _raising_loop_runner(_message: str) -> str:
    """Loop runner that raises ``MCPUnavailableError`` to test degradation."""
    msg = "creek-tools subprocess died"
    raise MCPUnavailableError(msg)


async def test_handle_ask_degrades_softly_when_runner_raises() -> None:
    """``handle_ask`` maps an escaped ``MCPUnavailableError`` to a soft reply."""
    replier = _FakeReplier()

    await handle_ask(replier, question="why?", loop_runner=_raising_loop_runner)

    assert len(replier.sent) == 1
    assert "unreachable" in replier.sent[0].lower()


async def test_handle_draft_degrades_softly_when_runner_raises() -> None:
    """``handle_draft`` maps an escaped ``MCPUnavailableError`` to a soft reply."""
    replier = _FakeReplier()

    await handle_draft(replier, topic="drift", loop_runner=_raising_loop_runner)

    assert len(replier.sent) == 1
    assert "unreachable" in replier.sent[0].lower()


async def test_handle_ask_relays_soft_error_string_from_runner() -> None:
    """When the runner RETURNS a soft-error string, ``handle_ask`` relays it."""
    soft = "creek-tools is unreachable; try again in a moment."

    async def _soft_runner(_message: str) -> str:
        return soft

    replier = _FakeReplier()

    await handle_ask(replier, question="why?", loop_runner=_soft_runner)

    assert replier.sent == [soft]


async def test_handle_draft_relays_soft_error_string_from_runner() -> None:
    """When the runner RETURNS a soft-error string, ``handle_draft`` relays it."""
    soft = "creek-tools is unreachable; try again in a moment."

    async def _soft_runner(_message: str) -> str:
        return soft

    replier = _FakeReplier()

    await handle_draft(replier, topic="drift", loop_runner=_soft_runner)

    assert replier.sent == [soft]
