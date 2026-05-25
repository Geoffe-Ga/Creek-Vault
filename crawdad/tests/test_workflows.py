"""Tests for ``crawdad.workflows`` — ADAPT-003 Phase 1.

Covers the Pydantic workflow file model, the YAML parser, the registry,
and the dry-run constraint-enforcement walker. The walker itself is a
stub in Phase 1 — it only inspects each step's ``tool`` / ``args`` and
prints them. Phase 2 wires the real MCP dispatcher in.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from crawdad.workflows import (
    PHASE_AWARE_WITHOUT_CURRENT_PHASE,
    PRIVACY_FLOOR_INTIMATE_WITHOUT_OVERRIDE,
    PrivacyTierFloor,
    Workflow,
    WorkflowConstraintError,
    WorkflowNotFoundError,
    WorkflowRegistry,
    WorkflowStep,
    dry_run_workflow,
    load_workflow,
)


def _write_yaml(path: Path, body: str) -> Path:
    """Write *body* (dedented) to *path* and return it for chaining."""
    path.write_text(dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------


def test_workflow_step_defaults_args_to_empty_dict() -> None:
    """A step with no ``args:`` defaults to ``{}`` rather than ``None``."""
    step = WorkflowStep(id="read", tool="creek.state.read")

    assert step.args == {}


def test_workflow_accepts_minimal_valid_payload() -> None:
    """The smallest valid workflow has name, description, and at least one step."""
    workflow = Workflow(
        name="checkin",
        description="Quick check-in.",
        steps=[WorkflowStep(id="state", tool="creek.state.read")],
    )

    assert workflow.name == "checkin"
    assert workflow.phase_aware is False
    assert workflow.privacy_tier_floor == PrivacyTierFloor.OPEN
    assert workflow.trigger is None


def test_workflow_rejects_uppercase_name() -> None:
    """``name`` must match ``[a-z][a-z0-9-]*`` — uppercase is a typo."""
    with pytest.raises(ValidationError):
        Workflow(
            name="Checkin",
            description="x",
            steps=[WorkflowStep(id="s", tool="t")],
        )


def test_workflow_rejects_name_starting_with_digit() -> None:
    """Workflow names must start with a lowercase letter."""
    with pytest.raises(ValidationError):
        Workflow(
            name="1-checkin",
            description="x",
            steps=[WorkflowStep(id="s", tool="t")],
        )


def test_workflow_rejects_empty_steps_list() -> None:
    """A zero-step workflow has nothing to walk — refuse at load time."""
    with pytest.raises(ValidationError):
        Workflow(name="empty", description="x", steps=[])


def test_workflow_rejects_duplicate_step_ids() -> None:
    """Two steps with the same ``id`` would break Phase 2's interpolation."""
    with pytest.raises(ValidationError):
        Workflow(
            name="dup",
            description="x",
            steps=[
                WorkflowStep(id="a", tool="t1"),
                WorkflowStep(id="a", tool="t2"),
            ],
        )


def test_workflow_rejects_unknown_privacy_floor() -> None:
    """Only ``open`` / ``personal`` / ``intimate`` are valid tier floors."""
    with pytest.raises(ValidationError):
        Workflow(
            name="x",
            description="x",
            privacy_tier_floor="public",  # type: ignore[arg-type]
            steps=[WorkflowStep(id="s", tool="t")],
        )


# ---------------------------------------------------------------------------
# YAML parser
# ---------------------------------------------------------------------------


def test_load_workflow_parses_minimal_yaml(tmp_path: Path) -> None:
    """A round-trip YAML file produces the expected ``Workflow`` instance."""
    path = _write_yaml(
        tmp_path / "checkin.yaml",
        """\
        name: checkin
        description: A simple check-in.
        steps:
          - id: read
            tool: creek.state.read
        """,
    )

    workflow = load_workflow(path)

    assert workflow.name == "checkin"
    assert len(workflow.steps) == 1
    assert workflow.steps[0].tool == "creek.state.read"


def test_load_workflow_parses_full_yaml(tmp_path: Path) -> None:
    """Every supported field round-trips through YAML."""
    path = _write_yaml(
        tmp_path / "full.yaml",
        """\
        name: full-example
        description: Every field exercised.
        trigger: "/draft phase-transitions"
        phase_aware: true
        privacy_tier_floor: personal
        steps:
          - id: read
            tool: creek.state.read
            args:
              period: weekly
          - id: mine
            tool: creek.mine
            args:
              strategy: thread-terminus
        """,
    )

    workflow = load_workflow(path)

    assert workflow.trigger == "/draft phase-transitions"
    assert workflow.phase_aware is True
    assert workflow.privacy_tier_floor == PrivacyTierFloor.PERSONAL
    assert workflow.steps[0].args == {"period": "weekly"}


def test_load_workflow_surfaces_validation_error(tmp_path: Path) -> None:
    """A YAML file that fails Pydantic validation raises a clear error."""
    path = _write_yaml(
        tmp_path / "bad.yaml",
        """\
        name: BadName
        description: x
        steps:
          - id: s
            tool: t
        """,
    )

    with pytest.raises(ValidationError):
        load_workflow(path)


def test_load_workflow_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    """A YAML list at the document root is not a workflow."""
    path = _write_yaml(
        tmp_path / "list.yaml",
        """\
        - just a list
        - not a mapping
        """,
    )

    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_workflow(path)


def test_load_workflow_rejects_missing_file(tmp_path: Path) -> None:
    """Loading a nonexistent path raises ``FileNotFoundError``."""
    with pytest.raises(FileNotFoundError):
        load_workflow(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------


def test_registry_discovers_yaml_files(tmp_path: Path) -> None:
    """``from_directory`` returns every ``*.yaml`` workflow in the tree."""
    _write_yaml(
        tmp_path / "one.yaml",
        """\
        name: one
        description: First.
        steps:
          - id: s
            tool: t
        """,
    )
    _write_yaml(
        tmp_path / "two.yaml",
        """\
        name: two
        description: Second.
        steps:
          - id: s
            tool: t
        """,
    )

    registry = WorkflowRegistry.from_directory(tmp_path)

    assert {w.name for w in registry.workflows()} == {"one", "two"}


def test_registry_ignores_non_yaml_files(tmp_path: Path) -> None:
    """Plain ``.md`` / ``.txt`` siblings are skipped — they're not workflows."""
    _write_yaml(
        tmp_path / "real.yaml",
        """\
        name: real
        description: Y.
        steps:
          - id: s
            tool: t
        """,
    )
    (tmp_path / "README.md").write_text("not a workflow", encoding="utf-8")
    (tmp_path / "scratch.txt").write_text("nope", encoding="utf-8")

    registry = WorkflowRegistry.from_directory(tmp_path)

    assert {w.name for w in registry.workflows()} == {"real"}


def test_registry_get_returns_workflow_by_name(tmp_path: Path) -> None:
    """``get`` looks up a workflow by its declared ``name``."""
    _write_yaml(
        tmp_path / "x.yaml",
        """\
        name: target
        description: Find me.
        steps:
          - id: s
            tool: t
        """,
    )
    registry = WorkflowRegistry.from_directory(tmp_path)

    workflow = registry.get("target")

    assert workflow.name == "target"


def test_registry_get_raises_for_unknown_workflow(tmp_path: Path) -> None:
    """``get`` on a missing name raises ``WorkflowNotFoundError``."""
    registry = WorkflowRegistry.from_directory(tmp_path)

    with pytest.raises(WorkflowNotFoundError, match="missing"):
        registry.get("missing")


def test_registry_rejects_duplicate_workflow_names(tmp_path: Path) -> None:
    """Two YAML files declaring the same ``name`` is a configuration error."""
    _write_yaml(
        tmp_path / "a.yaml",
        """\
        name: clash
        description: First.
        steps:
          - id: s
            tool: t
        """,
    )
    _write_yaml(
        tmp_path / "b.yaml",
        """\
        name: clash
        description: Second.
        steps:
          - id: s
            tool: t
        """,
    )

    with pytest.raises(ValueError, match="duplicate workflow name"):
        WorkflowRegistry.from_directory(tmp_path)


def test_registry_missing_directory_yields_empty_registry(tmp_path: Path) -> None:
    """A nonexistent directory produces an empty registry (not an error)."""
    registry = WorkflowRegistry.from_directory(tmp_path / "does-not-exist")

    assert list(registry.workflows()) == []


# ---------------------------------------------------------------------------
# Constraint enforcement (dry-run walker)
# ---------------------------------------------------------------------------


def _build(
    *,
    phase_aware: bool = False,
    privacy_tier_floor: PrivacyTierFloor = PrivacyTierFloor.OPEN,
) -> Workflow:
    """Return a small workflow with tunable constraint flags."""
    return Workflow(
        name="sample",
        description="Sample.",
        phase_aware=phase_aware,
        privacy_tier_floor=privacy_tier_floor,
        steps=[
            WorkflowStep(id="read", tool="creek.state.read", args={"period": "weekly"}),
            WorkflowStep(id="respond", tool="crawdad.respond", args={}),
        ],
    )


def test_dry_run_refuses_phase_aware_workflow_without_current_phase() -> None:
    """A ``phase_aware: true`` workflow refuses if no current phase is supplied."""
    workflow = _build(phase_aware=True)

    with pytest.raises(
        WorkflowConstraintError, match=PHASE_AWARE_WITHOUT_CURRENT_PHASE
    ):
        dry_run_workflow(workflow, current_phase=None, allow_intimate=False)


def test_dry_run_accepts_phase_aware_workflow_with_current_phase() -> None:
    """A current phase satisfies the phase-aware constraint."""
    workflow = _build(phase_aware=True)

    lines = dry_run_workflow(workflow, current_phase="Withdrawal", allow_intimate=False)

    assert any("Withdrawal" in line for line in lines)


def test_dry_run_refuses_intimate_workflow_without_override() -> None:
    """A ``privacy_tier_floor: intimate`` workflow refuses without override."""
    workflow = _build(privacy_tier_floor=PrivacyTierFloor.INTIMATE)

    with pytest.raises(
        WorkflowConstraintError, match=PRIVACY_FLOOR_INTIMATE_WITHOUT_OVERRIDE
    ):
        dry_run_workflow(workflow, current_phase=None, allow_intimate=False)


def test_dry_run_accepts_intimate_workflow_with_override() -> None:
    """``allow_intimate=True`` satisfies the privacy floor constraint."""
    workflow = _build(privacy_tier_floor=PrivacyTierFloor.INTIMATE)

    lines = dry_run_workflow(workflow, current_phase=None, allow_intimate=True)

    # The walker should emit at least one line per step plus a header.
    assert any("creek.state.read" in line for line in lines)


def test_dry_run_open_workflow_with_no_overrides() -> None:
    """An ``open`` workflow with no phase-awareness runs without overrides."""
    workflow = _build()

    lines = dry_run_workflow(workflow, current_phase=None, allow_intimate=False)

    body = "\n".join(lines)
    assert "Phase 1 dry-run" in body
    assert "creek.state.read" in body
    assert "crawdad.respond" in body
    assert "period" in body  # args rendered too


def test_dry_run_personal_workflow_runs_without_overrides() -> None:
    """A ``personal``-floor workflow dry-runs without any override flags.

    Phase 1 only gates the ``intimate`` floor; ``personal`` is
    advisory until Phase 2 wires real privacy enforcement. This test
    pins the current intent so that a future contributor doesn't
    silently add a half-built gate or wonder if the omission is a bug.
    """
    workflow = _build(privacy_tier_floor=PrivacyTierFloor.PERSONAL)

    lines = dry_run_workflow(workflow, current_phase=None, allow_intimate=False)

    body = "\n".join(lines)
    assert "Phase 1 dry-run" in body
    assert "personal" in body  # tier surfaced in the header
    assert "creek.state.read" in body


def test_dry_run_labels_output_as_phase_1_dry_run() -> None:
    """Every dry-run output is explicitly labelled — no one mistakes it for live."""
    workflow = _build()

    lines = dry_run_workflow(workflow, current_phase=None, allow_intimate=False)

    assert "Phase 1 dry-run" in lines[0]


# ---------------------------------------------------------------------------
# Reference workflows shipped in this PR
# ---------------------------------------------------------------------------


def test_reference_workflows_load_cleanly() -> None:
    """The three reference YAMLs validate as ``Workflow`` instances."""
    workflows_dir = Path(__file__).resolve().parents[1] / "workflows"
    registry = WorkflowRegistry.from_directory(workflows_dir)

    names = {w.name for w in registry.workflows()}

    assert names == {
        "substack-draft-phase-transitions",
        "wavelength-checkin",
        "compost-surfacing",
    }
