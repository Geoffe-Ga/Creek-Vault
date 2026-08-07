"""Tests for ``crawdad.workflows`` — ADAPT-003 workflow DSL.

Covers:

* Workflow model validation (parser happy + malformed)
* Constraint enforcement (phase-aware refusal, privacy ceiling cap +
  propagation, deprecated ``privacy_tier_floor`` alias migration)
* Step-walker semantics (deterministic walk + interpolation)
* Registry file discovery (built-in + vault)
* The three reference workflows shipped with the package
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from pydantic import ValidationError

from crawdad.dispatcher import ToolResult
from crawdad.intents import PrivacyTierCeiling
from crawdad.state import SessionState
from crawdad.workflows import (
    BUILTIN_WORKFLOWS_DIR,
    VAULT_WORKFLOWS_SUBPATH,
    WORKFLOW_ADMITTED_CEILINGS,
    WorkflowConstraintError,
    WorkflowDefinition,
    WorkflowNotFoundError,
    WorkflowParseError,
    WorkflowRegistry,
    WorkflowStep,
    WorkflowStepError,
    WorkflowWalker,
    _build_call_arguments,
    _StepRefError,
    _synthesize_user_message,
    interpolate,
    load_workflow_from_yaml,
    resolve_phase,
    run_workflow_and_compose,
)

# The three reference workflows shipped in ``BUILTIN_WORKFLOWS_DIR``,
# paired with the privacy ceiling each declares. The values are the
# ones the files carried under the old ``privacy_tier_floor`` key — the
# #1051 rename is value-preserving, so a drift here means a builtin's
# effective MCP ceiling moved during the rename.
_REFERENCE_WORKFLOW_CEILINGS: tuple[tuple[str, PrivacyTierCeiling], ...] = (
    ("wavelength-checkin.workflow.yaml", PrivacyTierCeiling.OPEN),
    ("compost-surfacing.workflow.yaml", PrivacyTierCeiling.PERSONAL),
    (
        "substack-draft-phase-transitions.workflow.yaml",
        PrivacyTierCeiling.PERSONAL,
    ),
)

_REFERENCE_WORKFLOW_IDS: list[str] = [
    filename.removesuffix(".workflow.yaml")
    for filename, _ in _REFERENCE_WORKFLOW_CEILINGS
]


def _workflow_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return WARNING+ messages emitted by the ``crawdad.workflows`` logger."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "crawdad.workflows" and record.levelno >= logging.WARNING
    ]


# ---------------------------------------------------------------------------
# Models + parser
# ---------------------------------------------------------------------------


def test_workflow_definition_minimal() -> None:
    """A workflow with only the required fields parses with sane defaults."""
    wf = WorkflowDefinition(
        name="minimal",
        description="just one step",
        steps=(WorkflowStep(id="read", tool="creek.state.read"),),
    )

    assert wf.phase_aware is False
    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.OPEN
    assert wf.allowed_phases == ()
    assert wf.inputs == ()
    assert wf.trigger is None
    assert wf.steps[0].args == {}


def test_workflow_definition_is_frozen() -> None:
    """Workflows are immutable Pydantic models — re-assignment raises."""
    wf = WorkflowDefinition(
        name="frozen",
        description="d",
        steps=(WorkflowStep(id="r", tool="creek.state.read"),),
    )
    with pytest.raises(ValidationError):
        wf.name = "renamed"  # type: ignore[misc]


def test_workflow_definition_refuses_empty_step_list() -> None:
    """A workflow must declare at least one step."""
    with pytest.raises(ValidationError):
        WorkflowDefinition(name="empty", description="", steps=())


def test_workflow_definition_refuses_duplicate_step_ids() -> None:
    """Step ids must be unique inside a workflow (interpolation references)."""
    with pytest.raises(ValidationError):
        WorkflowDefinition(
            name="dupe",
            description="",
            steps=(
                WorkflowStep(id="a", tool="creek.state.read"),
                WorkflowStep(id="a", tool="creek.mine"),
            ),
        )


def test_load_workflow_from_yaml_round_trip(tmp_path: Path) -> None:
    """A YAML file with every field parses into the matching model."""
    path = tmp_path / "demo.workflow.yaml"
    path.write_text(
        dedent(
            """\
            name: demo
            description: A demo workflow
            trigger: "/crawdad workflow run demo"
            phase_aware: true
            allowed_phases: [rising, peak]
            privacy_tier_ceiling: personal
            inputs: [topic]
            steps:
              - id: read
                tool: creek.state.read
                args:
                  period: weekly
              - id: mine
                tool: creek.mine
                args:
                  strategy: thread-terminus
                  phase: "{{state.phase}}"
            """
        ),
        encoding="utf-8",
    )

    wf = load_workflow_from_yaml(path)

    assert wf.name == "demo"
    assert wf.trigger == "/crawdad workflow run demo"
    assert wf.phase_aware is True
    assert wf.allowed_phases == ("rising", "peak")
    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL
    assert wf.inputs == ("topic",)
    assert len(wf.steps) == 2
    assert wf.steps[0].id == "read"
    assert wf.steps[1].args["phase"] == "{{state.phase}}"


def test_load_workflow_from_yaml_missing_file_raises(tmp_path: Path) -> None:
    """Calling the loader on a non-existent path raises ``WorkflowParseError``."""
    with pytest.raises(WorkflowParseError):
        load_workflow_from_yaml(tmp_path / "missing.workflow.yaml")


def test_load_workflow_from_yaml_invalid_yaml_raises(tmp_path: Path) -> None:
    """A YAML syntax error surfaces as ``WorkflowParseError`` (not yaml.YAMLError)."""
    path = tmp_path / "bad.workflow.yaml"
    path.write_text("name: foo\nsteps: [: oops]\n", encoding="utf-8")

    with pytest.raises(WorkflowParseError):
        load_workflow_from_yaml(path)


def test_load_workflow_from_yaml_invalid_schema_raises(tmp_path: Path) -> None:
    """A YAML doc that doesn't match the schema surfaces as ``WorkflowParseError``."""
    path = tmp_path / "schema.workflow.yaml"
    path.write_text(
        dedent(
            """\
            name: schema
            description: missing steps key entirely
            """
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowParseError):
        load_workflow_from_yaml(path)


def test_load_workflow_from_yaml_non_dict_raises(tmp_path: Path) -> None:
    """A YAML doc whose top level is a list raises ``WorkflowParseError``."""
    path = tmp_path / "list.workflow.yaml"
    path.write_text("- name: nope\n- name: nope2\n", encoding="utf-8")

    with pytest.raises(WorkflowParseError):
        load_workflow_from_yaml(path)


def test_load_workflow_from_yaml_empty_file_raises(tmp_path: Path) -> None:
    """An empty file is not a workflow document."""
    path = tmp_path / "empty.workflow.yaml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(WorkflowParseError):
        load_workflow_from_yaml(path)


# ---------------------------------------------------------------------------
# Deprecated ``privacy_tier_floor`` alias (#1051)
# ---------------------------------------------------------------------------
#
# The field was misnamed: ``OPEN`` is the MOST restrictive tier and
# ``ALL`` the broadest, so raising the so-called "floor" WIDENED what
# the MCP server returned. The rename to ``privacy_tier_ceiling`` is
# value-preserving — an existing file keeps the exact tier it declared,
# it just stops lying about the direction.


def _ceiling_workflow_yaml(key: str | None, value: str = "personal") -> str:
    """Return a minimal workflow document declaring *key* = *value*.

    ``key`` of ``None`` omits the privacy declaration entirely so the
    default-path test exercises the same document shape as the rest.
    """
    declaration = "" if key is None else f"{key}: {value}\n"
    return (
        "name: aliased\n"
        "description: exercises the privacy tier key\n"
        f"{declaration}"
        "steps:\n"
        "  - id: read\n"
        "    tool: creek.state.read\n"
    )


def test_yaml_legacy_privacy_tier_floor_key_migrates(tmp_path: Path) -> None:
    """A file still using ``privacy_tier_floor`` loads under the new field name.

    Value-preserving: ``personal`` in, ``PERSONAL`` out. The legacy key
    must NOT survive as a second attribute — the model exposes exactly
    one privacy attribute, ``privacy_tier_ceiling``.
    """
    path = tmp_path / "legacy.workflow.yaml"
    path.write_text(
        _ceiling_workflow_yaml("privacy_tier_floor", "personal"), encoding="utf-8"
    )

    wf = load_workflow_from_yaml(path)

    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL
    assert not hasattr(wf, "privacy_tier_floor")


def test_yaml_legacy_privacy_tier_floor_key_warns_naming_workflow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The legacy key is accepted but logs a WARNING that names the workflow.

    A silent migration would leave every authored file on a deprecated
    key forever. The warning has to name the offending workflow or the
    operator cannot find the file to edit.
    """
    path = tmp_path / "legacy-warn.workflow.yaml"
    path.write_text(
        _ceiling_workflow_yaml("privacy_tier_floor", "personal"), encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="crawdad.workflows"):
        wf = load_workflow_from_yaml(path)

    messages = _workflow_warnings(caplog)
    assert len(messages) == 1
    assert "privacy_tier_floor" in messages[0]
    assert "privacy_tier_ceiling" in messages[0]
    assert "aliased" in messages[0]
    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL


def test_kwargs_legacy_privacy_tier_floor_migrates() -> None:
    """Direct kwargs construction migrates too, not just ``model_validate``.

    The alias lives in a ``mode="before"`` model validator precisely so
    both entry points are covered: :func:`load_workflow_from_yaml` goes
    through ``model_validate(dict)``, but in-process callers (and the
    walker tests) construct ``WorkflowDefinition(...)`` directly.
    """
    wf = WorkflowDefinition(
        name="kwargs-legacy",
        description="constructed with the deprecated keyword",
        privacy_tier_floor=PrivacyTierCeiling.PERSONAL,
        steps=(WorkflowStep(id="read", tool="creek.state.read"),),
    )

    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL


def test_canonical_privacy_tier_ceiling_key_emits_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file already on the canonical key is silent — no deprecation noise."""
    path = tmp_path / "canonical.workflow.yaml"
    path.write_text(
        _ceiling_workflow_yaml("privacy_tier_ceiling", "personal"), encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="crawdad.workflows"):
        wf = load_workflow_from_yaml(path)

    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL
    assert _workflow_warnings(caplog) == []


def test_absent_privacy_tier_key_defaults_to_open_without_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Omitting the key falls back to ``OPEN`` and warns about nothing.

    ``OPEN`` is the most restrictive tier, so the default is the safe
    one. A workflow that never mentioned the legacy key must not be
    accused of using it.
    """
    path = tmp_path / "defaulted.workflow.yaml"
    path.write_text(_ceiling_workflow_yaml(None), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="crawdad.workflows"):
        wf = load_workflow_from_yaml(path)

    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.OPEN
    assert _workflow_warnings(caplog) == []


def test_both_privacy_tier_keys_raise_workflow_parse_error(tmp_path: Path) -> None:
    """Declaring both the legacy and canonical keys is ambiguous — refuse it.

    Silently picking a winner would let a half-finished migration ship a
    different effective ceiling than the author last read. The
    ``ValueError`` the validator raises reaches the caller as a
    :class:`WorkflowParseError` via the loader's ``ValidationError``
    mapping.
    """
    path = tmp_path / "both.workflow.yaml"
    path.write_text(
        (
            "name: ambiguous\n"
            "description: declares the privacy tier twice\n"
            "privacy_tier_floor: open\n"
            "privacy_tier_ceiling: personal\n"
            "steps:\n"
            "  - id: read\n"
            "    tool: creek.state.read\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowParseError, match="privacy_tier_floor"):
        load_workflow_from_yaml(path)


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------


def test_interpolate_passes_through_when_no_placeholders() -> None:
    """A value without ``{{...}}`` placeholders is returned unchanged."""
    out = interpolate("plain string", state=None, inputs={}, step_results={})
    assert out == "plain string"


def test_interpolate_non_string_returns_self() -> None:
    """Non-string values are returned verbatim — interpolation is text-only."""
    out = interpolate(42, state=None, inputs={}, step_results={})
    assert out == 42


def test_interpolate_state_phase(vault_with_state: Path) -> None:
    """``{{state.phase}}`` resolves to the slug extracted from the wavelength."""
    from crawdad.state import load_session_state

    state = load_session_state(vault_with_state)

    out = interpolate(
        "phase is {{state.phase}}",
        state=state,
        inputs={},
        step_results={},
    )

    assert out == "phase is rising"


def test_interpolate_state_wavelength_returns_full_snapshot(
    vault_with_state: Path,
) -> None:
    """``{{state.wavelength}}`` resolves to the raw wavelength snapshot text."""
    from crawdad.state import load_session_state

    state = load_session_state(vault_with_state)

    out = interpolate(
        "{{state.wavelength}}",
        state=state,
        inputs={},
        step_results={},
    )

    assert "Phase: **rising**" in out


def test_interpolate_state_missing_yields_empty_string() -> None:
    """A `{{state.X}}` placeholder with no state resolves to empty string."""
    out = interpolate(
        "phase=[{{state.phase}}]",
        state=None,
        inputs={},
        step_results={},
    )
    assert out == "phase=[]"


def test_interpolate_input_lookup() -> None:
    """``{{input.<key>}}`` resolves from the supplied inputs dict."""
    out = interpolate(
        "topic={{input.topic}}",
        state=None,
        inputs={"topic": "phase transitions"},
        step_results={},
    )
    assert out == "topic=phase transitions"


def test_interpolate_input_missing_yields_empty() -> None:
    """An ``{{input.X}}`` lookup with no such key resolves to empty string."""
    out = interpolate(
        "topic={{input.missing}}",
        state=None,
        inputs={},
        step_results={},
    )
    assert out == "topic="


def test_interpolate_step_body() -> None:
    """``{{step.<id>.body}}`` resolves to the named step's result body."""
    step_results = {
        "read": ToolResult(intent_type="creek.state.read", body="phase: rising"),
    }
    out = interpolate(
        "previous: {{step.read.body}}",
        state=None,
        inputs={},
        step_results=step_results,
    )
    assert out == "previous: phase: rising"


def test_interpolate_step_unknown_id_raises_step_ref_error() -> None:
    """A reference to an unknown step id is a workflow bug — surface it loudly.

    Phase 2 (#326) tightens this from the Phase 1 stub's empty-string
    fallback: a step reference always pins a real upstream output, so a
    forward / typo'd reference is an authoring error worth failing on.

    At the interpolation layer the public :class:`WorkflowStepError`
    cannot be raised with valid metadata (interpolation has no workflow
    or step context), so the private :class:`_StepRefError` is raised
    and the walker rebrands it into a :class:`WorkflowStepError` with
    full metadata before bubbling up.
    """
    with pytest.raises(_StepRefError, match=r"step\.missing"):
        interpolate(
            "nothing here: [{{step.missing.body}}]",
            state=None,
            inputs={},
            step_results={},
        )


def test_interpolate_unknown_namespace_passes_through() -> None:
    """Placeholders in an unknown namespace are left untouched.

    This avoids silently swallowing a typo like ``{{statee.phase}}``;
    the literal placeholder lands in the args dict so the workflow
    author can spot the typo in MCP logs.
    """
    out = interpolate(
        "value={{weird.namespace}}",
        state=None,
        inputs={},
        step_results={},
    )
    assert "{{weird.namespace}}" in out


def test_interpolate_nested_dict_recurses() -> None:
    """Dict values are interpolated recursively (for step args nesting)."""
    out = interpolate(
        {"phase": "{{input.phase}}", "nested": {"k": "{{input.phase}}"}},
        state=None,
        inputs={"phase": "rising"},
        step_results={},
    )
    assert out == {"phase": "rising", "nested": {"k": "rising"}}


def test_interpolate_list_recurses() -> None:
    """List values are interpolated element-wise."""
    out = interpolate(
        ["{{input.x}}", "static", "{{input.y}}"],
        state=None,
        inputs={"x": "alpha", "y": "beta"},
        step_results={},
    )
    assert out == ["alpha", "static", "beta"]


# ---------------------------------------------------------------------------
# Phase resolution helper
# ---------------------------------------------------------------------------


def test_resolve_phase_from_session_state(vault_with_state: Path) -> None:
    """``resolve_phase`` returns the slug extracted from the wavelength block."""
    from crawdad.state import load_session_state

    state = load_session_state(vault_with_state)
    assert resolve_phase(state) == "rising"


def test_resolve_phase_keeps_underscore_in_bottoming_out() -> None:
    """``resolve_phase`` keeps the underscore in ``bottoming_out`` (#528).

    The canonical phase enum value is ``bottoming_out`` and the
    wavelength snapshot renders it verbatim. If the phase regex drops
    the underscore, ``{{state.phase}}`` interpolates to ``bottoming``
    and the loader (and any phase-aware workflow) silently misfires.
    """
    state = SessionState(
        raw_markdown="",
        wavelength_snapshot=(
            "## Wavelength snapshot\n- Phase: **bottoming_out** (confidence 0.71)"
        ),
        eddies=(),
        threads=(),
        suggested_questions=(),
    )
    assert resolve_phase(state) == "bottoming_out"


def test_resolve_phase_returns_none_on_missing_state() -> None:
    """No session state ⇒ no phase."""
    assert resolve_phase(None) is None


def test_resolve_phase_returns_none_on_blank_wavelength() -> None:
    """A state with no wavelength snapshot ⇒ no phase."""
    state = SessionState(
        raw_markdown="",
        wavelength_snapshot=None,
        eddies=(),
        threads=(),
        suggested_questions=(),
    )
    assert resolve_phase(state) is None


def test_resolve_phase_returns_none_when_wavelength_has_no_phase_token() -> None:
    """A wavelength snapshot without a recognisable phase slug ⇒ no phase."""
    state = SessionState(
        raw_markdown="",
        wavelength_snapshot="just narrative — nothing addressable here",
        eddies=(),
        threads=(),
        suggested_questions=(),
    )
    assert resolve_phase(state) is None


def test_interpolate_state_unknown_field_yields_empty() -> None:
    """``{{state.bogus}}`` (not phase/wavelength) resolves to empty string."""
    state = SessionState(
        raw_markdown="",
        wavelength_snapshot="Phase: **rising**",
        eddies=(),
        threads=(),
        suggested_questions=(),
    )
    out = interpolate(
        "x={{state.bogus}}",
        state=state,
        inputs={},
        step_results={},
    )
    assert out == "x="


def test_interpolate_input_namespace_with_no_field_yields_empty() -> None:
    """A bare ``{{input}}`` with no key resolves to empty string."""
    out = interpolate(
        "[{{input}}]",
        state=None,
        inputs={"topic": "X"},
        step_results={},
    )
    assert out == "[]"


def test_interpolate_bare_step_namespace_raises_step_ref_error() -> None:
    """``{{step}}`` with no id at all is also a malformed reference."""
    with pytest.raises(_StepRefError, match="step"):
        interpolate(
            "[{{step}}]",
            state=None,
            inputs={},
            step_results={},
        )


def test_interpolate_step_with_no_field_raises_step_ref_error() -> None:
    """``{{step.<id>}}`` with no trailing field is a malformed reference.

    Phase 2 (#326): the only valid step accessor today is ``.body``, so
    omitting the field is an authoring bug; we surface it instead of
    silently resolving to empty.
    """
    with pytest.raises(_StepRefError, match=r"step\.read"):
        interpolate(
            "[{{step.read}}]",
            state=None,
            inputs={},
            step_results={"read": ToolResult(intent_type="x", body="b")},
        )


def test_interpolate_step_with_unknown_field_raises_step_ref_error() -> None:
    """``{{step.<id>.bogus}}`` (not ``body``) is a malformed reference.

    Phase 2 (#326): the walker only exposes ``body`` on a step result;
    any other field is a typo and must fail loudly.
    """
    with pytest.raises(_StepRefError, match="bogus"):
        interpolate(
            "[{{step.read.bogus}}]",
            state=None,
            inputs={},
            step_results={"read": ToolResult(intent_type="x", body="b")},
        )


def test_step_ref_error_is_value_error_subclass() -> None:
    """``_StepRefError`` is a :class:`ValueError` so callers can catch broadly.

    Promised by the docstring: this is a private exception type, raised
    only by :func:`_resolve_step` and rebranded by the walker into a
    :class:`WorkflowStepError`. Inheriting from :class:`ValueError`
    keeps it discoverable to any caller that was already handling
    interpolation errors with a broad ``except ValueError`` clause.
    """
    assert issubclass(_StepRefError, ValueError)


def test_step_ref_error_is_not_workflow_step_error() -> None:
    """``_StepRefError`` and :class:`WorkflowStepError` are distinct types.

    The boundary matters: :class:`WorkflowStepError` always carries
    structured ``step_id`` / ``tool`` / ``workflow_name`` attributes;
    :class:`_StepRefError` is the marker for the interpolation layer
    that has none of that context yet.
    """
    assert not issubclass(_StepRefError, WorkflowStepError)
    assert not issubclass(WorkflowStepError, _StepRefError)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _write_workflow(path: Path, name: str, *, body: str | None = None) -> None:
    """Helper: write a minimal workflow YAML file to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if body is None:
        body = dedent(
            f"""\
            name: {name}
            description: test workflow {name}
            steps:
              - id: read
                tool: creek.state.read
            """
        )
    path.write_text(body, encoding="utf-8")


def test_registry_lists_builtin_workflows(tmp_path: Path) -> None:
    """The registry discovers built-in workflows shipped with the package."""
    registry = WorkflowRegistry(vault_path=tmp_path)
    listed = registry.list()
    names = {wf.name for wf in listed}
    # The three reference workflows the AC requires.
    assert "substack-draft-phase-transitions" in names
    assert "wavelength-checkin" in names
    assert "compost-surfacing" in names


def test_registry_discovers_vault_workflows(tmp_path: Path) -> None:
    """Workflows under ``<vault>/00-Creek-Meta/Workflows/`` are discovered."""
    user_dir = tmp_path / VAULT_WORKFLOWS_SUBPATH
    _write_workflow(user_dir / "custom.workflow.yaml", "custom")

    registry = WorkflowRegistry(vault_path=tmp_path)
    names = {wf.name for wf in registry.list()}

    assert "custom" in names


def test_registry_get_returns_named_workflow(tmp_path: Path) -> None:
    """``get(name)`` returns the workflow definition for a known name."""
    registry = WorkflowRegistry(vault_path=tmp_path)
    wf = registry.get("wavelength-checkin")
    assert wf.name == "wavelength-checkin"


def test_registry_get_raises_for_unknown(tmp_path: Path) -> None:
    """``get(name)`` raises ``WorkflowNotFoundError`` for unknown names."""
    registry = WorkflowRegistry(vault_path=tmp_path)
    with pytest.raises(WorkflowNotFoundError):
        registry.get("does-not-exist")


def test_registry_vault_workflow_overrides_builtin(tmp_path: Path) -> None:
    """A vault workflow with the same name as a built-in wins."""
    override_dir = tmp_path / VAULT_WORKFLOWS_SUBPATH
    _write_workflow(
        override_dir / "wavelength-checkin.workflow.yaml",
        name="wavelength-checkin",
        body=dedent(
            """\
            name: wavelength-checkin
            description: USER OVERRIDE
            steps:
              - id: r
                tool: creek.state.read
            """
        ),
    )
    registry = WorkflowRegistry(vault_path=tmp_path)
    wf = registry.get("wavelength-checkin")
    assert wf.description == "USER OVERRIDE"


def test_registry_skips_invalid_workflow_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed user workflow is logged and skipped — other workflows still load."""
    user_dir = tmp_path / VAULT_WORKFLOWS_SUBPATH
    user_dir.mkdir(parents=True)
    (user_dir / "bad.workflow.yaml").write_text("not: a: workflow:\n", encoding="utf-8")
    _write_workflow(user_dir / "good.workflow.yaml", "good")

    caplog.set_level("WARNING", logger="crawdad.workflows")
    registry = WorkflowRegistry(vault_path=tmp_path)
    names = {wf.name for wf in registry.list()}

    assert "good" in names
    assert "bad" not in names
    assert any("bad.workflow.yaml" in r.message for r in caplog.records)


def test_registry_ignores_non_workflow_files(tmp_path: Path) -> None:
    """Files without the ``.workflow.yaml`` suffix are ignored."""
    user_dir = tmp_path / VAULT_WORKFLOWS_SUBPATH
    user_dir.mkdir(parents=True)
    (user_dir / "README.md").write_text("# notes\n", encoding="utf-8")
    (user_dir / "extra.yaml").write_text("name: extra\n", encoding="utf-8")

    registry = WorkflowRegistry(vault_path=tmp_path)
    names = {wf.name for wf in registry.list()}

    assert "extra" not in names


def test_builtin_workflows_dir_exists() -> None:
    """The package's built-in workflows directory is shipped with the source tree."""
    assert BUILTIN_WORKFLOWS_DIR.is_dir()
    files = list(BUILTIN_WORKFLOWS_DIR.glob("*.workflow.yaml"))
    assert len(files) >= 3, f"expected at least 3 reference workflows, got {files}"


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


class _FakeSession:
    """An :class:`MCPSession`-shaped fake that records every tool call."""

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        """Record per-tool canned responses; missing tools return empty body."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> str:
        """Record the call and return the canned body (or empty string)."""
        self.calls.append((name, arguments or {}))
        return self._responses.get(name, "")


class _FakeMCPClient:
    """An :class:`MCPClient`-shaped fake that yields a session.

    The session is typed as :class:`typing.Any` so the same fake client
    can wrap either a :class:`_FakeSession` (happy path) or a
    :class:`_FailingSession` (error-path tests) without a ``# type:
    ignore[arg-type]`` at every call site. Both fakes implement the
    same ``call_tool`` shape; the walker only ever invokes that one
    method.

    ``connect_calls`` / ``entered`` exist so pre-flight tests can prove
    a constraint fired *before any tool could fire* (#1051). Asserting
    only that ``run()`` raised is not enough: a check placed inside the
    step loop would raise too, after the session was already open and
    (for a multi-step workflow) after earlier tools had already run.
    """

    def __init__(self, session: Any) -> None:
        """Stash the session that ``connect()`` will yield."""
        self._session = session
        self.connect_calls = 0
        self.entered = False

    def connect(self) -> Any:
        """Async-context-manager that yields the wrapped session."""
        from contextlib import asynccontextmanager

        self.connect_calls += 1

        @asynccontextmanager
        async def _ctx() -> Any:
            self.entered = True
            yield self._session

        return _ctx()


def _simple_workflow(**overrides: Any) -> WorkflowDefinition:
    """Return a baseline workflow used by walker tests; override any field."""
    base: dict[str, Any] = {
        "name": "test-flow",
        "description": "a test",
        "steps": (
            WorkflowStep(id="read", tool="creek.state.read"),
            WorkflowStep(
                id="mine",
                tool="creek.mine",
                args={"strategy": "thread-terminus", "echo": "{{step.read.body}}"},
            ),
        ),
    }
    base.update(overrides)
    return WorkflowDefinition(**base)


async def test_walker_runs_each_step_in_order() -> None:
    """The walker invokes each step's tool with its (interpolated) args."""
    session = _FakeSession(
        responses={"creek.state.read": "phase: rising", "creek.mine": "[]"}
    )
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read", "creek.mine"),
    )

    results = await walker.run(_simple_workflow(), state=None, inputs={})

    assert [name for name, _ in session.calls] == ["creek.state.read", "creek.mine"]
    # ``creek.mine`` saw the interpolated body of ``read``.
    _, mine_args = session.calls[1]
    assert mine_args["echo"] == "phase: rising"
    assert len(results) == 2
    assert results[0].body == "phase: rising"


async def test_walker_propagates_privacy_ceiling_to_each_call() -> None:
    """Every step receives ``privacy_tier_ceiling`` = the workflow's ceiling."""
    session = _FakeSession()
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(privacy_tier_ceiling=PrivacyTierCeiling.PERSONAL)

    await walker.run(wf, state=None, inputs={})

    for _name, args in session.calls:
        assert args["privacy_tier_ceiling"] == "personal"


async def test_walker_propagates_privacy_ceiling_to_each_tool_result() -> None:
    """The declared ceiling also lands on every returned ``ToolResult``.

    Downstream, :func:`crawdad.loop._max_ceiling_from` reads
    ``ToolResult.privacy_tier_ceiling`` to decide how sensitive the
    composed reply may be. If the walker stamped the wrong attribute
    the loop would under- or over-classify the whole batch.
    """
    session = _FakeSession()
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(privacy_tier_ceiling=PrivacyTierCeiling.PERSONAL)

    results = await walker.run(wf, state=None, inputs={})

    assert len(results) == 2
    for result in results:
        assert result.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL


async def test_walker_refuses_phase_aware_without_state() -> None:
    """A ``phase_aware: true`` workflow with no session state soft-errors."""
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(_FakeSession()),
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(phase_aware=True)

    with pytest.raises(WorkflowConstraintError, match="phase-aware"):
        await walker.run(wf, state=None, inputs={})


async def test_walker_refuses_phase_aware_with_disallowed_phase(
    vault_with_state: Path,
) -> None:
    """A workflow with an ``allowed_phases`` list refuses unmatched phases."""
    from crawdad.state import load_session_state

    state = load_session_state(vault_with_state)  # phase: rising
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(_FakeSession()),
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(
        phase_aware=True, allowed_phases=("bottoming_out", "withdrawal")
    )

    with pytest.raises(WorkflowConstraintError, match="refuses to run in phase"):
        await walker.run(wf, state=state, inputs={})


async def test_walker_runs_phase_aware_when_phase_matches(
    vault_with_state: Path,
) -> None:
    """A workflow whose ``allowed_phases`` includes the current phase runs."""
    from crawdad.state import load_session_state

    state = load_session_state(vault_with_state)  # phase: rising
    session = _FakeSession()
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(phase_aware=True, allowed_phases=("rising",))

    results = await walker.run(wf, state=state, inputs={})

    assert len(results) == 2


async def test_walker_refuses_step_with_unknown_tool() -> None:
    """A step referencing an unadvertised tool soft-errors with constraint."""
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(_FakeSession()),
        known_tools=("creek.state.read",),  # 'creek.mine' missing
    )
    wf = _simple_workflow()  # references creek.mine

    with pytest.raises(WorkflowConstraintError):
        await walker.run(wf, state=None, inputs={})


async def test_walker_refuses_missing_required_input() -> None:
    """An unsupplied required input soft-errors before any tool call."""
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(_FakeSession()),
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(inputs=("topic",))

    with pytest.raises(WorkflowConstraintError):
        await walker.run(wf, state=None, inputs={})


async def test_walker_supplies_required_input() -> None:
    """When all required inputs are present the walker proceeds."""
    session = _FakeSession()
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(inputs=("topic",))

    results = await walker.run(wf, state=None, inputs={"topic": "phase transitions"})

    assert len(results) == 2


# ---------------------------------------------------------------------------
# Walker — error propagation (ADAPT-003 Phase 2 / #326)
# ---------------------------------------------------------------------------


class _FailingSession:
    """An :class:`MCPSession`-shaped fake that fails on a specific tool name."""

    def __init__(self, failing_tool: str, *, error: Exception) -> None:
        """Configure which tool name raises and what exception to raise."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._failing_tool = failing_tool
        self._error = error

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> str:
        """Record the call; raise the canned exception when *name* matches."""
        self.calls.append((name, arguments or {}))
        if name == self._failing_tool:
            raise self._error
        return ""


async def test_walker_aborts_when_step_references_missing_upstream_step() -> None:
    """A reference to a step id that has not run yet halts the walker loudly.

    Phase 2 acceptance: a forward / typo'd ``{{step.<id>.body}}`` is an
    authoring error. The walker raises :class:`WorkflowStepError` naming
    the offending step so the operator can locate the typo.
    """
    wf = WorkflowDefinition(
        name="bad-ref",
        description="references a step that does not exist",
        steps=(
            WorkflowStep(
                id="mine",
                tool="creek.mine",
                args={"echo": "{{step.notthere.body}}"},
            ),
        ),
    )
    session = _FakeSession()
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.mine",),
    )

    with pytest.raises(WorkflowStepError, match="mine") as excinfo:
        await walker.run(wf, state=None, inputs={})

    # The error message names BOTH the failing step id (so the operator
    # can find it in the workflow file) AND the missing upstream id (so
    # they know what to fix).
    assert "notthere" in str(excinfo.value)
    # The tool was never invoked — interpolation failed first.
    assert session.calls == []
    # The rebranded walker error preserves the original interpolation
    # error as ``__cause__`` so log analysers and tests can pin on the
    # interpolation-layer marker without scraping the message string.
    assert isinstance(excinfo.value.__cause__, _StepRefError)


async def test_walker_rebrands_interpolation_error_with_full_metadata() -> None:
    """A `_StepRefError` is rebranded into a fully-populated WorkflowStepError.

    The reviewer's blocker fix: the public :class:`WorkflowStepError`
    must NEVER be raised with empty ``step_id`` / ``tool`` /
    ``workflow_name`` attributes. When interpolation raises
    :class:`_StepRefError`, the walker catches it and re-raises with
    the workflow's name and the failing step's id + tool baked in.
    """
    wf = WorkflowDefinition(
        name="rebrand-flow",
        description="missing upstream reference triggers rebrand",
        steps=(
            WorkflowStep(
                id="mine",
                tool="creek.mine",
                args={"echo": "{{step.ghost.body}}"},
            ),
        ),
    )
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(_FakeSession()),
        known_tools=("creek.mine",),
    )

    with pytest.raises(WorkflowStepError) as excinfo:
        await walker.run(wf, state=None, inputs={})

    # All three metadata attributes are populated — never empty strings.
    assert excinfo.value.step_id == "mine"
    assert excinfo.value.tool == "creek.mine"
    assert excinfo.value.workflow_name == "rebrand-flow"
    # The cause chain preserves the private interpolation-layer marker
    # so callers can disambiguate "interpolation aborted" from "MCP tool
    # raised" by inspecting ``__cause__`` instead of the message text.
    assert isinstance(excinfo.value.__cause__, _StepRefError)


async def test_walker_aborts_when_step_references_unknown_field() -> None:
    """``{{step.<id>.bogus}}`` halts the walker with a clear error.

    Only ``.body`` is exposed on a step result today; any other field is
    a typo worth failing loudly on.
    """
    wf = WorkflowDefinition(
        name="bad-field",
        description="references a field that does not exist",
        steps=(
            WorkflowStep(id="read", tool="creek.state.read"),
            WorkflowStep(
                id="mine",
                tool="creek.mine",
                args={"echo": "{{step.read.bogus}}"},
            ),
        ),
    )
    session = _FakeSession(responses={"creek.state.read": "ok"})
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read", "creek.mine"),
    )

    with pytest.raises(WorkflowStepError, match="mine") as excinfo:
        await walker.run(wf, state=None, inputs={})

    assert "bogus" in str(excinfo.value)
    # The first step ran successfully; the second step never reached
    # the tool call because interpolation failed.
    assert [name for name, _ in session.calls] == ["creek.state.read"]


async def test_walker_aborts_on_mcp_tool_failure_mid_workflow() -> None:
    """If an MCP tool raises mid-workflow, the walker halts and names the step.

    The second step's ``creek.mine`` call fails — the walker must
    surface that with a :class:`WorkflowStepError` naming the failing
    step (``mine``) and embedding the upstream error message.
    """
    failing = _FailingSession(
        "creek.mine", error=RuntimeError("mcp boom: tool exploded")
    )
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(failing),
        known_tools=("creek.state.read", "creek.mine"),
    )

    with pytest.raises(WorkflowStepError, match="mine") as excinfo:
        await walker.run(_simple_workflow(), state=None, inputs={})

    message = str(excinfo.value)
    assert "creek.mine" in message
    assert "mcp boom" in message
    # The original exception is preserved as the chained cause so
    # callers can introspect it (or surface the original type).
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    # The first step succeeded; the second step ran (failed) and the
    # third step was never attempted.
    assert [name for name, _ in failing.calls] == [
        "creek.state.read",
        "creek.mine",
    ]


async def test_walker_step_error_carries_step_and_tool_metadata() -> None:
    """``WorkflowStepError`` exposes step id, tool name, and workflow name as attrs.

    A structured error is more useful to callers than message-only
    surfacing — the slash command may want to format the step id
    differently, and tests/log analysers can pin on attributes rather
    than scraping the message string.
    """
    failing = _FailingSession("creek.mine", error=ValueError("nope"))
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(failing),
        known_tools=("creek.state.read", "creek.mine"),
    )

    with pytest.raises(WorkflowStepError) as excinfo:
        await walker.run(_simple_workflow(), state=None, inputs={})

    assert excinfo.value.step_id == "mine"
    assert excinfo.value.tool == "creek.mine"
    assert excinfo.value.workflow_name == "test-flow"


# ---------------------------------------------------------------------------
# Walker — privacy ceiling cap (#1051)
# ---------------------------------------------------------------------------
#
# CrawDad relays every ToolResult body to a cloud LLM composer and then
# posts it to Discord. ``intimate`` / ``all`` content must therefore
# never be *requestable* by an authored workflow file, no matter what
# the file declares. The cap is enforced at WALK time, not parse time,
# deliberately: a parse error would make the workflow vanish from
# ``/crawdad workflow list`` via ``_absorb_dir``'s warn-and-skip, which
# is a worse silent failure than a loud refusal at run time.

_ADMITTED_TIERS = (PrivacyTierCeiling.OPEN, PrivacyTierCeiling.PERSONAL)
_ADMITTED_TIER_IDS = [tier.value for tier in _ADMITTED_TIERS]
_REFUSED_TIERS = (PrivacyTierCeiling.INTIMATE, PrivacyTierCeiling.ALL)
_REFUSED_TIER_IDS = [tier.value for tier in _REFUSED_TIERS]


def test_workflow_admitted_ceilings_is_open_and_personal() -> None:
    """The cap is exactly ``{open, personal}`` — an immutable frozenset.

    Mirrors ``creek_mcp.policy.REMOTE_ADMITTED_CEILINGS``. Pinned as an
    exact set (not a membership spot-check) so widening the cap is a
    deliberate, reviewable edit to this assertion rather than a silent
    addition nobody notices.
    """
    assert (
        frozenset({PrivacyTierCeiling.OPEN, PrivacyTierCeiling.PERSONAL})
        == WORKFLOW_ADMITTED_CEILINGS
    )
    assert isinstance(WORKFLOW_ADMITTED_CEILINGS, frozenset)
    assert PrivacyTierCeiling.INTIMATE not in WORKFLOW_ADMITTED_CEILINGS
    assert PrivacyTierCeiling.ALL not in WORKFLOW_ADMITTED_CEILINGS


@pytest.mark.parametrize("tier", _REFUSED_TIERS, ids=_REFUSED_TIER_IDS)
async def test_walker_refuses_ceiling_above_cap_before_any_tool_fires(
    tier: PrivacyTierCeiling,
) -> None:
    """A workflow declaring ``intimate`` / ``all`` is refused pre-flight.

    "Pre-flight" is the load-bearing word: the MCP session must never be
    opened, so no tool has a chance to return over-broad content that
    the walker would then have to discard. Asserting only on the raised
    exception would pass even if the check lived inside the step loop.
    """
    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(
        mcp_client=client,
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(privacy_tier_ceiling=tier)

    with pytest.raises(WorkflowConstraintError):
        await walker.run(wf, state=None, inputs={})

    assert client.connect_calls == 0
    assert client.entered is False
    assert session.calls == []


@pytest.mark.parametrize("tier", _ADMITTED_TIERS, ids=_ADMITTED_TIER_IDS)
async def test_walker_admits_ceilings_within_cap(tier: PrivacyTierCeiling) -> None:
    """``open`` and ``personal`` still walk to completion — the cap is not a ban."""
    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(
        mcp_client=client,
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(privacy_tier_ceiling=tier)

    results = await walker.run(wf, state=None, inputs={})

    assert len(results) == 2
    assert client.entered is True
    assert [name for name, _ in session.calls] == ["creek.state.read", "creek.mine"]
    for _name, args in session.calls:
        assert args["privacy_tier_ceiling"] == tier.value


async def test_walker_ceiling_refusal_names_workflow_tier_and_allowed_set() -> None:
    """The refusal message is actionable: which workflow, which tier, what's allowed.

    The operator sees this text in Discord. Without the workflow name
    they cannot find the file; without the allowed set they cannot tell
    what to change it to.
    """
    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(
        mcp_client=client,
        known_tools=("creek.state.read", "creek.mine"),
    )
    wf = _simple_workflow(
        name="leaky-flow", privacy_tier_ceiling=PrivacyTierCeiling.ALL
    )

    with pytest.raises(WorkflowConstraintError) as excinfo:
        await walker.run(wf, state=None, inputs={})

    message = str(excinfo.value)
    assert "'leaky-flow'" in message
    assert "'all'" in message
    assert "open" in message
    assert "personal" in message
    assert session.calls == []


async def test_ceiling_refusal_reported_before_other_constraint_failures() -> None:
    """The privacy refusal wins when several constraints fail at once.

    ``_check_constraints`` reports the FIRST failure it finds, so check
    order decides what the operator sees. A workflow that is over the
    cap *and* names an unadvertised tool *and* is missing an input must
    report the ceiling: fix-the-tool-then-rerun would otherwise walk the
    author up to the privacy refusal one round trip at a time, and the
    privacy rule is the one that must never be the second thing said.
    """
    wf = WorkflowDefinition(
        name="multi-fail",
        description="over the cap, unknown tool, and missing an input",
        privacy_tier_ceiling=PrivacyTierCeiling.INTIMATE,
        inputs=("topic",),
        steps=(WorkflowStep(id="read", tool="creek.not.a.real.tool"),),
    )
    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(mcp_client=client, known_tools=("creek.state.read",))

    with pytest.raises(WorkflowConstraintError) as excinfo:
        await walker.run(wf, state=None, inputs={})

    message = str(excinfo.value)
    assert "'intimate'" in message
    assert "not.a.real.tool" not in message
    assert "topic" not in message
    assert client.connect_calls == 0


async def test_workflow_over_cap_stays_visible_in_registry(tmp_path: Path) -> None:
    """An over-cap workflow is refused at WALK time, not hidden at PARSE time.

    This is the whole reason the cap is not a validator. If the parse
    rejected it, ``_absorb_dir`` would log-and-skip and the workflow
    would simply be absent from ``/crawdad workflow list`` — the author
    would see no workflow and no reason. Instead it stays listed and
    gettable, and refuses loudly when run.
    """
    user_dir = tmp_path / VAULT_WORKFLOWS_SUBPATH
    _write_workflow(
        user_dir / "too-private.workflow.yaml",
        name="too-private",
        body=dedent(
            """\
            name: too-private
            description: declares a ceiling above the cap
            privacy_tier_ceiling: intimate
            steps:
              - id: read
                tool: creek.state.read
            """
        ),
    )
    registry = WorkflowRegistry(vault_path=tmp_path)

    # Still discoverable — the file parsed, it just cannot be walked.
    assert "too-private" in {wf.name for wf in registry.list()}
    wf = registry.get("too-private")
    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.INTIMATE

    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(mcp_client=client, known_tools=("creek.state.read",))

    with pytest.raises(WorkflowConstraintError, match="'too-private'"):
        await walker.run(wf, state=None, inputs={})

    assert client.connect_calls == 0
    assert session.calls == []


# ---------------------------------------------------------------------------
# Walker — step-arg ceiling smuggling (#1051)
# ---------------------------------------------------------------------------
#
# ``_build_call_arguments`` overwrites any ``privacy_tier_ceiling`` key
# a step's ``args`` carries. That backstop stays, but a silent
# overwrite means an author who wrote ``privacy_tier_ceiling: all`` in
# a step gets neither the tier they asked for nor any indication that
# their line was discarded. Refuse it up front instead.


async def test_walker_refuses_step_arg_ceiling_naming_the_step() -> None:
    """A step ``args`` block declaring ``privacy_tier_ceiling`` is refused.

    The message must name the offending step id — a workflow can have
    many steps and the author needs to know which line to delete.
    """
    wf = WorkflowDefinition(
        # Deliberately does NOT contain the step id as a substring, so
        # the "names the step" assertion cannot be satisfied by the
        # workflow name leaking into the message.
        name="tier-override-flow",
        description="a step tries to set its own ceiling",
        privacy_tier_ceiling=PrivacyTierCeiling.OPEN,
        steps=(
            WorkflowStep(id="benign", tool="creek.state.read"),
            WorkflowStep(
                id="smuggle",
                tool="creek.mine",
                args={"strategy": "thread-terminus", "privacy_tier_ceiling": "all"},
            ),
        ),
    )
    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(
        mcp_client=client,
        known_tools=("creek.state.read", "creek.mine"),
    )

    with pytest.raises(WorkflowConstraintError) as excinfo:
        await walker.run(wf, state=None, inputs={})

    message = str(excinfo.value)
    assert "smuggle" in message
    assert "privacy_tier_ceiling" in message
    # Pre-flight: even the *earlier*, innocent step never ran.
    assert client.connect_calls == 0
    assert session.calls == []


async def test_walker_refuses_step_arg_legacy_ceiling_key() -> None:
    """The DEPRECATED spelling in a step's ``args`` is refused too.

    Scoping the step-arg check to the canonical key alone would leave
    ``privacy_tier_floor: all`` in a step silently accepted — the exact
    false-assurance shape #1051 exists to close, one key over. No MCP
    tool reads the legacy name, so nothing leaks either way; what is at
    stake is that the author's line does something or says so.
    """
    wf = WorkflowDefinition(
        name="tier-override-flow",
        description="a step tries to set its own ceiling, old spelling",
        privacy_tier_ceiling=PrivacyTierCeiling.OPEN,
        steps=(
            WorkflowStep(
                id="smuggle",
                tool="creek.mine",
                args={"privacy_tier_floor": "all"},
            ),
        ),
    )
    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(mcp_client=client, known_tools=("creek.mine",))

    with pytest.raises(WorkflowConstraintError) as excinfo:
        await walker.run(wf, state=None, inputs={})

    message = str(excinfo.value)
    assert "smuggle" in message
    assert "privacy_tier_floor" in message
    assert client.connect_calls == 0
    assert session.calls == []


async def test_walker_allows_steps_without_a_ceiling_arg() -> None:
    """Ordinary steps are untouched by the step-arg refusal.

    Guards against an over-broad check that trips on any arg key, or on
    the walker-injected ``privacy_tier_ceiling`` it adds itself.
    """
    session = _FakeSession()
    client = _FakeMCPClient(session)
    walker = WorkflowWalker(
        mcp_client=client,
        known_tools=("creek.state.read", "creek.mine"),
    )

    results = await walker.run(_simple_workflow(), state=None, inputs={})

    assert len(results) == 2
    assert client.entered is True
    assert [name for name, _ in session.calls] == ["creek.state.read", "creek.mine"]


def test_build_call_arguments_still_overwrites_colliding_key() -> None:
    """The ``_build_call_arguments`` backstop survives the pre-flight refusal.

    Defence in depth: the constraint check is the loud gate, but the
    merge helper must still clamp any ``privacy_tier_ceiling`` that
    reaches it — e.g. from a future programmatic caller that never ran
    ``_check_constraints``. (It is NOT reachable via interpolation:
    ``interpolate`` recurses over dict values only, so no new top-level
    key can appear.)

    The tier parameter is passed positionally on purpose: it is being
    renamed ``floor`` -> ``ceiling`` and this test pins behaviour, not
    the parameter's spelling.
    """
    payload = _build_call_arguments(
        {"strategy": "thread-terminus", "privacy_tier_ceiling": "all"},
        PrivacyTierCeiling.OPEN,
    )

    assert payload["privacy_tier_ceiling"] == "open"
    assert payload["strategy"] == "thread-terminus"


# ---------------------------------------------------------------------------
# Reference workflows
# ---------------------------------------------------------------------------


def test_substack_draft_reference_workflow_parses() -> None:
    """The substack-draft reference workflow loads and is well-formed."""
    path = BUILTIN_WORKFLOWS_DIR / "substack-draft-phase-transitions.workflow.yaml"
    wf = load_workflow_from_yaml(path)
    assert wf.phase_aware is True
    assert any(step.tool == "creek.draft" for step in wf.steps)
    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL


def test_wavelength_checkin_reference_workflow_parses() -> None:
    """The wavelength-checkin reference workflow loads and is well-formed."""
    path = BUILTIN_WORKFLOWS_DIR / "wavelength-checkin.workflow.yaml"
    wf = load_workflow_from_yaml(path)
    assert any(step.tool == "creek.state.read" for step in wf.steps)
    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.OPEN


@pytest.mark.parametrize(
    ("filename", "expected_ceiling"),
    _REFERENCE_WORKFLOW_CEILINGS,
    ids=_REFERENCE_WORKFLOW_IDS,
)
def test_reference_workflows_use_canonical_ceiling_key(
    filename: str,
    expected_ceiling: PrivacyTierCeiling,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every shipped workflow is migrated: canonical key, same tier, no warning.

    Three things at once, because they are one property:

    * the file parses under ``privacy_tier_ceiling``;
    * its tier is *unchanged* by the #1051 rename (value-preserving —
      a shifted value here would silently move what the MCP server
      returns for a workflow nobody edited);
    * loading it emits no deprecation warning, i.e. the builtins do not
      ride the legacy alias they are meant to replace.
    """
    path = BUILTIN_WORKFLOWS_DIR / filename

    with caplog.at_level(logging.WARNING, logger="crawdad.workflows"):
        wf = load_workflow_from_yaml(path)

    assert wf.privacy_tier_ceiling == expected_ceiling
    assert _workflow_warnings(caplog) == []


@pytest.mark.parametrize(
    ("filename", "expected_ceiling"),
    _REFERENCE_WORKFLOW_CEILINGS,
    ids=_REFERENCE_WORKFLOW_IDS,
)
def test_reference_workflows_sit_inside_the_ceiling_cap(
    filename: str, expected_ceiling: PrivacyTierCeiling
) -> None:
    """No shipped workflow declares a tier the walker would refuse.

    A builtin that trips the cap would ship broken out of the box —
    listed by ``/crawdad workflow list`` and guaranteed to fail on run.
    """
    wf = load_workflow_from_yaml(BUILTIN_WORKFLOWS_DIR / filename)

    assert wf.privacy_tier_ceiling in WORKFLOW_ADMITTED_CEILINGS
    assert expected_ceiling in WORKFLOW_ADMITTED_CEILINGS


def test_compost_surfacing_reference_workflow_parses() -> None:
    """The compost-surfacing reference workflow loads and is well-formed.

    Beyond the basic parse check, this pins three semantic invariants
    that ``compost-surfacing`` must hold so a future edit cannot revert
    the post-review fix:

    * It MUST be ``phase_aware: true`` — the workflow interpolates
      ``{{state.phase}}`` into ``creek.mine``'s ``phase`` arg, so
      running without a session state would silently pass ``phase=""``.
      The phase-aware flag turns that silent-bad-input path into a
      clean ``WorkflowConstraintError`` at the walker's constraint
      check.
    * It MUST call ``creek.mine`` — the whole purpose is to surface
      ranked seeds for the user's compost folder.
    * The mine step MUST reference ``{{state.phase}}`` — otherwise the
      ``phase_aware: true`` declaration is decorative.
    """
    path = BUILTIN_WORKFLOWS_DIR / "compost-surfacing.workflow.yaml"
    wf = load_workflow_from_yaml(path)

    assert wf.phase_aware is True, (
        "compost-surfacing must be phase_aware: true to refuse cleanly when "
        "session state is unavailable (the workflow interpolates state.phase)"
    )
    mine_steps = [step for step in wf.steps if step.tool == "creek.mine"]
    assert mine_steps, "compost-surfacing must surface seeds via creek.mine"
    assert any(
        "{{state.phase}}" in str(value) for value in mine_steps[0].args.values()
    ), "compost-surfacing's creek.mine step must scope to the current phase"
    assert wf.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL


async def test_compost_surfacing_refuses_when_session_state_unavailable() -> None:
    """End-to-end: compost-surfacing refuses cleanly with no session state.

    Closes the gap the Claude reviewer flagged on PR #309: before
    flipping ``phase_aware: true`` the workflow would silently pass
    ``phase: ""`` to ``creek.mine`` when state was missing. With
    ``phase_aware: true`` the walker's constraint check raises before
    any MCP call fires.
    """
    path = BUILTIN_WORKFLOWS_DIR / "compost-surfacing.workflow.yaml"
    wf = load_workflow_from_yaml(path)
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(_FakeSession()),
        known_tools=("creek.state.read", "creek.mine"),
    )

    with pytest.raises(WorkflowConstraintError, match="phase-aware"):
        await walker.run(wf, state=None, inputs={})


# ---------------------------------------------------------------------------
# End-to-end: all reference workflows run against a mocked MCP client (#326)
# ---------------------------------------------------------------------------


# Canned responses for every MCP tool any reference workflow calls. The
# values are placeholders — the e2e test asserts on the call sequence
# (the wiring contract) not on the body text (which the composer owns).
_REFERENCE_TOOL_RESPONSES: dict[str, str] = {
    "creek.state.read": (
        "## Wavelength snapshot\n- Phase: **rising** (confidence 0.84)\n"
    ),
    "creek.mine": "[]",
    "creek.draft": "draft body",
}


@pytest.mark.parametrize(
    ("workflow_filename", "expected_calls"),
    [
        (
            "wavelength-checkin.workflow.yaml",
            ["creek.state.read"],
        ),
        (
            "compost-surfacing.workflow.yaml",
            ["creek.state.read", "creek.mine"],
        ),
        (
            "substack-draft-phase-transitions.workflow.yaml",
            ["creek.state.read", "creek.mine", "creek.draft"],
        ),
    ],
    ids=["wavelength-checkin", "compost-surfacing", "substack-draft"],
)
async def test_reference_workflows_walk_end_to_end_against_mocked_mcp(
    workflow_filename: str,
    expected_calls: list[str],
    vault_with_state: Path,
) -> None:
    """Every shipped reference workflow walks to completion on a mocked MCP.

    The mocked MCP client is the integration boundary called out in
    issue #326's e2e recipe: we never spawn a real subprocess but we
    DO exercise the full walker → tool dispatch → result chain →
    constraint enforcement path for every step the reference workflow
    declares. The phase-aware workflows are run with the populated
    ``vault_with_state`` fixture so the ``state.phase`` interpolation
    has a real ``rising`` slug to substitute.
    """
    from crawdad.state import load_session_state

    path = BUILTIN_WORKFLOWS_DIR / workflow_filename
    wf = load_workflow_from_yaml(path)
    state = load_session_state(vault_with_state)
    session = _FakeSession(responses=dict(_REFERENCE_TOOL_RESPONSES))
    walker = WorkflowWalker(
        mcp_client=_FakeMCPClient(session),
        known_tools=tuple(_REFERENCE_TOOL_RESPONSES),
    )

    results = await walker.run(wf, state=state, inputs={})

    # Every declared step ran and yielded a ToolResult, in document order.
    assert [name for name, _ in session.calls] == expected_calls
    assert [r.intent_type for r in results] == expected_calls
    # Phase-aware workflows must have had ``{{state.phase}}`` resolved
    # against the live session (not left as a literal placeholder).
    for _name, args in session.calls:
        for value in args.values():
            if isinstance(value, str):
                assert "{{" not in value, (
                    f"unresolved placeholder leaked into MCP call args: {args!r}"
                )


# ---------------------------------------------------------------------------
# High-level runner
# ---------------------------------------------------------------------------


class _FakeComposer:
    """Records the args the run_workflow_and_compose call passes through."""

    def __init__(self, reply: str = "composed!") -> None:
        """Record the canned reply the composer's ``compose`` will return."""
        self._reply = reply
        self.last_call: dict[str, Any] = {}

    async def compose(self, **kwargs: Any) -> str:
        """Record kwargs and return the canned reply string."""
        self.last_call = kwargs
        return self._reply


async def test_run_workflow_and_compose_returns_composer_reply() -> None:
    """The high-level runner walks the workflow and forwards the composer reply."""
    from crawdad.skill_loader import VoiceSkillStack

    composer = _FakeComposer(reply="all done")
    session = _FakeSession(responses={"creek.state.read": "phase: rising"})
    wf = WorkflowDefinition(
        name="solo",
        description="a one-step workflow",
        steps=(WorkflowStep(id="read", tool="creek.state.read"),),
    )

    reply = await run_workflow_and_compose(
        workflow=wf,
        inputs={},
        state=None,
        skills=VoiceSkillStack(skills=()),
        skill_registry=None,
        composer=composer,
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read",),
    )

    assert reply == "all done"
    # Composer saw the synthesized user message and the walker's result.
    assert composer.last_call["tool_results"][0].body == "phase: rising"
    assert "Run workflow: solo" in composer.last_call["user_message"]


async def test_run_workflow_and_compose_uses_registry_stack_when_provided(
    vault_with_state: Path,
) -> None:
    """When a ``SkillStackRegistry`` is provided, the runner uses ``.stack``."""
    from crawdad.skill_loader import SkillStackRegistry, VoiceSkillStack
    from crawdad.state import load_session_state

    state = load_session_state(vault_with_state)
    registry = SkillStackRegistry(
        stack=VoiceSkillStack(skills=()), vault_path=vault_with_state, state=state
    )
    composer = _FakeComposer()
    session = _FakeSession()
    wf = WorkflowDefinition(
        name="solo",
        description="d",
        steps=(WorkflowStep(id="r", tool="creek.state.read"),),
    )

    await run_workflow_and_compose(
        workflow=wf,
        inputs={},
        state=state,
        skills=VoiceSkillStack(skills=()),
        skill_registry=registry,
        composer=composer,
        mcp_client=_FakeMCPClient(session),
        known_tools=("creek.state.read",),
    )

    # The composer saw the registry's stack, not the passed ``skills`` argument.
    assert composer.last_call["skills"] is registry.stack


def test_synthesize_user_message_includes_workflow_name_and_description() -> None:
    """The synthetic message names the workflow + its description."""
    wf = WorkflowDefinition(
        name="solo",
        description="a one-line description",
        steps=(WorkflowStep(id="r", tool="creek.state.read"),),
    )

    msg = _synthesize_user_message(wf, inputs={})

    assert "solo" in msg
    assert "a one-line description" in msg
    assert "Inputs" not in msg


def test_synthesize_user_message_renders_inputs() -> None:
    """When inputs are supplied, the synthetic message lists them."""
    wf = WorkflowDefinition(
        name="solo",
        description="d",
        steps=(WorkflowStep(id="r", tool="creek.state.read"),),
    )

    msg = _synthesize_user_message(wf, inputs={"topic": "phase transitions"})

    assert "topic" in msg
    assert "phase transitions" in msg


def test_workflow_registry_cache_returns_same_dict(tmp_path: Path) -> None:
    """A second registry call returns the cached dict without re-scanning."""
    registry = WorkflowRegistry(vault_path=tmp_path)
    first = registry.list()
    second = registry.list()
    assert {wf.name for wf in first} == {wf.name for wf in second}
