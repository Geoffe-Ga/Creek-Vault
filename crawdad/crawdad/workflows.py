"""Jig-style workflow DSL for CrawDad (ADAPT-003 Phase 1).

The workflow surface is intentionally tiny: a YAML file declares a
named pipeline of MCP tool calls (each step names its ``tool`` and
``args``), the registry discovers every workflow under a directory,
and the runner walks the steps. Phase 1 ships the file format,
registry, constraint enforcement, and a *dry-run* walker that prints
each step instead of dispatching it. Phase 2 replaces the dry-run
walker with a real MCP-tool walker; Phase 3 wires the walker into the
Haiku router as a ``run-workflow`` intent.

Workflows live in ``crawdad/workflows/`` under the bot's own config —
see ``crawdad/docs/adr/2026-05-24-jig-workflows-location.md`` for the
ADR documenting that decision.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Reason strings exported so the test suite (and any future reviewer)
# can match the exact text without re-typing it.
PHASE_AWARE_WITHOUT_CURRENT_PHASE: str = (
    "workflow is phase_aware but no --current-phase was supplied"
)
PRIVACY_FLOOR_INTIMATE_WITHOUT_OVERRIDE: str = (
    "workflow's privacy_tier_floor is intimate; pass --allow-intimate to run it"
)

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


class PrivacyTierFloor(StrEnum):
    """Minimum tier a workflow may operate at.

    Mirrors :class:`creek_mcp.tier_ceiling.TierCeiling` (minus the
    catch-all ``all`` value, which makes no sense as a *floor*) so the
    workflow DSL stays in lock-step with the MCP boundary.
    """

    OPEN = "open"
    PERSONAL = "personal"
    INTIMATE = "intimate"


class WorkflowStep(BaseModel):
    """One MCP tool call in a workflow pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class Workflow(BaseModel):
    """A named, composable pipeline of MCP tool calls.

    See :mod:`crawdad.workflows` and the ADAPT-003 plan for the rationale.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    trigger: str | None = None
    phase_aware: bool = False
    privacy_tier_floor: PrivacyTierFloor = PrivacyTierFloor.OPEN
    steps: list[WorkflowStep]

    @field_validator("name")
    @classmethod
    def _validate_name_pattern(cls, value: str) -> str:
        """Reject names that do not match ``[a-z][a-z0-9-]*``."""
        if not _NAME_PATTERN.match(value):
            msg = (
                f"workflow name {value!r} must match [a-z][a-z0-9-]* "
                "(lowercase, start with a letter, hyphens allowed)"
            )
            raise ValueError(msg)
        return value

    @field_validator("steps")
    @classmethod
    def _validate_steps_non_empty(cls, value: list[WorkflowStep]) -> list[WorkflowStep]:
        """A zero-step workflow has nothing to walk."""
        if not value:
            msg = "workflow must declare at least one step"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _validate_unique_step_ids(self) -> Workflow:
        """Step IDs must be unique so Phase 2 interpolation works."""
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
            msg = f"duplicate step ids in workflow {self.name!r}: {duplicates}"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkflowConstraintError(Exception):
    """Raised when a workflow's declared constraints aren't satisfied."""


class WorkflowNotFoundError(Exception):
    """Raised when a registry lookup misses.

    Inherits from :class:`Exception` rather than :class:`KeyError` so the
    error message renders cleanly when interpolated into user output —
    ``str(KeyError("x"))`` wraps the message in extra quotes.
    """


# ---------------------------------------------------------------------------
# Parser + registry
# ---------------------------------------------------------------------------


def load_workflow(path: Path) -> Workflow:
    """Load a YAML workflow file and validate it as a :class:`Workflow`.

    Args:
        path: Absolute or relative path to a ``.yaml`` workflow file.

    Returns:
        The parsed :class:`Workflow`.

    Raises:
        FileNotFoundError: When *path* does not exist.
        ValueError: When the YAML document is not a mapping.
        pydantic.ValidationError: When Pydantic rejects the payload.
    """
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        msg = f"workflow file {path} must be a YAML mapping at the document root"
        raise ValueError(msg)
    return Workflow.model_validate(raw)


class WorkflowRegistry:
    """In-memory registry of workflows discovered from a directory."""

    def __init__(self, workflows: dict[str, Workflow]) -> None:
        """Wrap *workflows* keyed by ``Workflow.name``."""
        self._workflows = dict(workflows)

    @classmethod
    def from_directory(cls, directory: Path) -> WorkflowRegistry:
        """Discover every ``*.yaml`` workflow under *directory*.

        A nonexistent directory yields an empty registry rather than an
        error — this lets the CLI surface a friendly empty-list message
        on first run instead of crashing on a missing path.
        """
        if not directory.is_dir():
            return cls({})
        workflows: dict[str, Workflow] = {}
        for path in sorted(directory.glob("*.yaml")):
            workflow = load_workflow(path)
            if workflow.name in workflows:
                msg = (
                    f"duplicate workflow name {workflow.name!r} in {directory}: "
                    f"already registered from a sibling YAML file"
                )
                raise ValueError(msg)
            workflows[workflow.name] = workflow
        return cls(workflows)

    def workflows(self) -> tuple[Workflow, ...]:
        """Return every registered workflow, sorted by name."""
        return tuple(self._workflows[name] for name in sorted(self._workflows))

    def get(self, name: str) -> Workflow:
        """Look up a workflow by ``name`` or raise :class:`WorkflowNotFoundError`."""
        try:
            return self._workflows[name]
        except KeyError as exc:
            msg = f"unknown workflow {name!r} — check `workflows list`"
            raise WorkflowNotFoundError(msg) from exc


# ---------------------------------------------------------------------------
# Phase 1 dry-run walker
# ---------------------------------------------------------------------------


def dry_run_workflow(
    workflow: Workflow,
    *,
    current_phase: str | None,
    allow_intimate: bool,
) -> list[str]:
    """Return the human-readable Phase 1 dry-run trace for *workflow*.

    Enforces the two Phase 1 constraints before walking:

    * ``phase_aware: true`` workflows refuse to run without a current
      phase (Phase 1 accepts any non-empty string as satisfying — phase
      *matching* lands in Phase 2/3 alongside the real walker).
    * ``privacy_tier_floor: intimate`` workflows refuse without an
      explicit ``allow_intimate`` override.

    Returns:
        A list of output lines explicitly labelled as a Phase 1 dry-run
        so the caller can print or log them without ambiguity.

    Raises:
        WorkflowConstraintError: When a declared constraint isn't met.
    """
    _enforce_constraints(
        workflow, current_phase=current_phase, allow_intimate=allow_intimate
    )
    return _render_dry_run(workflow, current_phase=current_phase)


def _enforce_constraints(
    workflow: Workflow,
    *,
    current_phase: str | None,
    allow_intimate: bool,
) -> None:
    """Reject the workflow if a declared constraint isn't satisfied.

    Phase 1 enforces the ``intimate`` privacy-tier floor only;
    ``personal`` is advisory and runs without an override, because
    real privacy enforcement (consent prompts, audit log) lands with
    the live walker. Treat the ``personal`` branch as a deliberate
    no-op, not a forgotten check.
    """
    if workflow.phase_aware and not current_phase:
        raise WorkflowConstraintError(PHASE_AWARE_WITHOUT_CURRENT_PHASE)
    if workflow.privacy_tier_floor is PrivacyTierFloor.INTIMATE and not allow_intimate:
        raise WorkflowConstraintError(PRIVACY_FLOOR_INTIMATE_WITHOUT_OVERRIDE)


def _render_dry_run(workflow: Workflow, *, current_phase: str | None) -> list[str]:
    """Format the dry-run trace as a list of plain-text lines."""
    phase_marker = current_phase or "n/a"
    lines = [
        (
            f"[Phase 1 dry-run] workflow={workflow.name} "
            f"phase_aware={workflow.phase_aware} "
            f"privacy_tier_floor={workflow.privacy_tier_floor.value} "
            f"current_phase={phase_marker}"
        )
    ]
    for index, step in enumerate(workflow.steps, start=1):
        lines.append(f"  step {index}: id={step.id} tool={step.tool} args={step.args}")
    lines.append(
        "[Phase 1 dry-run] no MCP tools dispatched — Phase 2 wires the real walker."
    )
    return lines


# ---------------------------------------------------------------------------
# Bundled-workflows directory
# ---------------------------------------------------------------------------


def bundled_workflows_dir() -> Path:
    """Return the absolute path to the bundled ``crawdad/workflows/`` directory.

    The CLI uses this as its default registry source. Tests use it to
    sanity-check the three reference workflows that ship in the repo.
    """
    return Path(__file__).resolve().parents[1] / "workflows"
