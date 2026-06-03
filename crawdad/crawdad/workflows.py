"""Workflow DSL (ADAPT-003 / issue #268).

CrawDad composite commands compile down to a deterministic walk over
authored YAML files. Each file declares a named workflow: a list of
:class:`WorkflowStep` entries that map one-to-one to MCP tool calls,
plus first-class constraint metadata (``phase_aware`` /
``privacy_tier_floor``) the walker enforces before any tool fires.

File format
-----------

Plain YAML. No custom grammar, no Jinja2 dependency. A workflow file
looks like::

    name: substack-draft-phase-transitions
    description: Draft a Substack post on phase transitions
    trigger: "/crawdad workflow run substack-draft-phase-transitions"
    phase_aware: true
    allowed_phases: [rising, peak]
    privacy_tier_floor: personal
    inputs: [topic]
    steps:
      - id: read
        tool: creek.state.read
        args: { period: weekly }
      - id: mine
        tool: creek.mine
        args:
          strategy: thread-terminus
          phase: "{{state.phase}}"
          topic: "{{input.topic}}"

File location
-------------

Two-source registry, decided in
``docs/adr/2026-05-24_workflow-file-location.md``:

1. Built-in reference workflows ship in
   :data:`BUILTIN_WORKFLOWS_DIR` (this package). They are checked into
   the repo and discoverable on every install.
2. User-authored workflows live in
   ``<vault>/00-Creek-Meta/Workflows/`` (see :data:`VAULT_WORKFLOWS_SUBPATH`).
   A user file with the same ``name`` as a built-in wins.

Interpolation
-------------

Step ``args`` strings may reference three namespaces inside ``{{...}}``::

    {{state.phase}}        -> phase slug extracted from latest.md (or "")
    {{state.wavelength}}   -> raw wavelength snapshot text (or "")
    {{input.<key>}}        -> caller-supplied input value (or "")
    {{step.<id>.body}}     -> previous step's tool-result body (REQUIRED)

``state`` and ``input`` references resolve to empty string when the
referent is missing — both are best-effort context and the workflow's
declared ``inputs:`` list / ``phase_aware`` flag already constrain
required values up front.

``step`` references are STRICT (Phase 2 / #326): a reference to an
unknown step id, a missing field, or any field other than ``body``
raises :class:`WorkflowStepError`. A step reference always pins a real
upstream output, so an unresolvable one is an authoring bug worth
failing loudly on.

Unknown top-level namespaces (anything other than ``state`` / ``input``
/ ``step``) are left in place verbatim — that surfaces a typo
like ``{{statee.phase}}`` to the workflow author instead of silently
swallowing it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from crawdad.dispatcher import ToolResult
from crawdad.intents import PrivacyTierCeiling

if TYPE_CHECKING:
    from crawdad.mcp_client import MCPClient
    from crawdad.skill_loader import SkillStackRegistry, VoiceSkillStack
    from crawdad.state import SessionState

_LOGGER = logging.getLogger("crawdad.workflows")

# Built-in reference workflows are co-located with the package source so
# they ship on every install and remain discoverable by the registry
# without any vault layout. The directory contains one
# ``*.workflow.yaml`` file per workflow.
BUILTIN_WORKFLOWS_DIR: Path = Path(__file__).resolve().parent / "builtin_workflows"

# Vault-relative subpath the registry scans for user-authored workflows.
# Mirrors the existing ``00-Creek-Meta/State/`` convention so workflows
# land in a single canonical Creek-Meta home.
VAULT_WORKFLOWS_SUBPATH: Path = Path("00-Creek-Meta") / "Workflows"

_WORKFLOW_FILE_SUFFIX = ".workflow.yaml"

# ``\w`` matches the YAML-friendly subset we accept inside placeholder
# tokens (alphanumerics, underscore). Dotted access is the only nesting
# syntax — anything richer requires Jinja2 and pulls in a dependency the
# v1.1 surface does not need yet.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")

# Phase regex matches FEAT-015 / skill_loader extraction so a workflow's
# ``state.phase`` reference is the same slug the voice-skill loader uses.
# The slug class accepts underscores so the canonical ``bottoming_out``
# phase value survives intact rather than being truncated to ``bottoming``
# (#528) — keep this in lockstep with ``skill_loader._PHASE_RE``.
_PHASE_RE = re.compile(r"phase[:\s]*\*{0,2}([a-z_\-]+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkflowParseError(RuntimeError):
    """A workflow YAML file could not be loaded or did not match the schema.

    Surfaces YAML syntax errors and Pydantic validation errors as a
    single error type so callers only need one ``except`` clause.
    """


class WorkflowConstraintError(RuntimeError):
    """The walker refused to run a workflow because a declared constraint failed.

    Raised for any of: missing required input, missing session-state
    phase when ``phase_aware: true``, current phase not in
    ``allowed_phases``, or a step referencing an unadvertised MCP tool.
    """


class WorkflowNotFoundError(RuntimeError):
    """No workflow with the requested name was found in the registry."""


class WorkflowStepError(RuntimeError):
    """A step failed mid-run — either interpolation or the MCP tool call.

    Raised by the walker so callers (the slash command surface, the
    high-level runner, log analysers) can identify *which* step in
    *which* workflow blew up without scraping the message string. The
    underlying exception is preserved via ``__cause__``.

    Invariant: this exception is ONLY ever raised with complete
    metadata. The interpolation helpers that lack workflow / step
    context raise the private :class:`_StepRefError` instead; the
    walker catches that and rebrands into a fully-populated
    :class:`WorkflowStepError`.

    Attributes:
        step_id: The ``id`` of the failing step.
        tool: The MCP tool name the step was about to call.
        workflow_name: The name of the workflow whose walk aborted.
    """

    def __init__(
        self,
        message: str,
        *,
        step_id: str,
        tool: str,
        workflow_name: str,
    ) -> None:
        """Record the message + structured metadata for the failing step."""
        super().__init__(message)
        self.step_id = step_id
        self.tool = tool
        self.workflow_name = workflow_name


class _StepRefError(ValueError):
    """Private — raised by :func:`_resolve_step`, wrapped by the walker.

    The interpolation layer cannot construct a meaningful
    :class:`WorkflowStepError` because it has no workflow or step
    context — only the walker has those. Raising this distinct private
    type lets the walker rebrand into a fully-populated
    :class:`WorkflowStepError` while keeping :class:`WorkflowStepError`'s
    contract (always has ``step_id`` / ``tool`` / ``workflow_name``)
    intact for direct callers.

    Subclasses :class:`ValueError` because a malformed step reference
    really is a bad value — that placement keeps it discoverable to any
    caller that already handles interpolation problems with a broad
    ``except ValueError``.
    """


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """One MCP tool call inside a workflow.

    ``id`` is the addressable handle later steps reference via
    ``{{step.<id>.body}}``. ``tool`` is the MCP tool name, validated
    against the live ``known_tools`` set at walk time. ``args`` is the
    raw arg dict; any nested string is interpolated by the walker.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinition(BaseModel):
    """An authored workflow loaded from a ``*.workflow.yaml`` file.

    ``phase_aware`` and ``privacy_tier_floor`` are the AC's first-class
    constraint attributes. The walker treats them as soft-fail gates
    rather than runtime errors so the slash-command surface can return
    a clean user-facing message instead of a stack trace.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str
    trigger: str | None = None
    phase_aware: bool = False
    allowed_phases: tuple[str, ...] = ()
    privacy_tier_floor: PrivacyTierCeiling = PrivacyTierCeiling.OPEN
    inputs: tuple[str, ...] = ()
    steps: tuple[WorkflowStep, ...]

    @field_validator("steps")
    @classmethod
    def _validate_steps(
        cls, value: tuple[WorkflowStep, ...]
    ) -> tuple[WorkflowStep, ...]:
        """Refuse empty step lists and duplicate step ids."""
        if not value:
            msg = "workflow must declare at least one step"
            raise ValueError(msg)
        seen: set[str] = set()
        for step in value:
            if step.id in seen:
                msg = f"duplicate step id {step.id!r} in workflow"
                raise ValueError(msg)
            seen.add(step.id)
        return value


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_workflow_from_yaml(path: Path) -> WorkflowDefinition:
    """Read and validate a workflow file.

    Args:
        path: Filesystem path to a ``*.workflow.yaml`` document.

    Returns:
        A frozen :class:`WorkflowDefinition`.

    Raises:
        WorkflowParseError: file missing, YAML invalid, or schema mismatch.
    """
    if not path.is_file():
        msg = f"workflow file not found: {path}"
        raise WorkflowParseError(msg)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"invalid YAML in {path}: {exc}"
        raise WorkflowParseError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"workflow document at {path} must be a mapping at the top level"
        raise WorkflowParseError(msg)
    try:
        return WorkflowDefinition.model_validate(raw)
    except ValidationError as exc:
        msg = f"workflow schema validation failed for {path}: {exc}"
        raise WorkflowParseError(msg) from exc


# ---------------------------------------------------------------------------
# Phase resolution
# ---------------------------------------------------------------------------


def resolve_phase(state: SessionState | None) -> str | None:
    """Extract the phase slug from a session state's wavelength snapshot.

    Returns ``None`` when there is no state, no wavelength snapshot, or
    no recognisable ``Phase: **<slug>**`` token. Mirrors the slug-shape
    convention used in :mod:`crawdad.skill_loader` so a workflow's
    ``{{state.phase}}`` value matches the voice-skill loader's phase.
    """
    if state is None or not state.wavelength_snapshot:
        return None
    match = _PHASE_RE.search(state.wavelength_snapshot)
    if match is None:
        return None
    return match.group(1).lower()


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------


def interpolate(
    value: Any,
    *,
    state: SessionState | None,
    inputs: dict[str, str],
    step_results: dict[str, ToolResult],
) -> Any:
    """Recursively resolve ``{{...}}`` placeholders inside *value*.

    String values are scanned with :data:`_PLACEHOLDER_RE`; each match
    is resolved against the supplied state, inputs, and prior step
    results. Dicts and lists are recursed element-wise so a deeply
    nested ``args`` block (e.g. ``{"filters": {"phase": "..."}}``) is
    interpolated through.

    Non-string scalars (int, bool, None, etc.) are returned unchanged.
    Unknown namespaces are left in place verbatim — surfacing a typo
    in MCP logs is more useful than silently swallowing it.
    """
    if isinstance(value, str):
        return _interpolate_string(
            value, state=state, inputs=inputs, step_results=step_results
        )
    if isinstance(value, dict):
        return {
            k: interpolate(v, state=state, inputs=inputs, step_results=step_results)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            interpolate(item, state=state, inputs=inputs, step_results=step_results)
            for item in value
        ]
    return value


def _interpolate_string(
    text: str,
    *,
    state: SessionState | None,
    inputs: dict[str, str],
    step_results: dict[str, ToolResult],
) -> str:
    """Scan *text* and substitute every recognised ``{{...}}`` placeholder."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        resolved = _resolve_token(
            token, state=state, inputs=inputs, step_results=step_results
        )
        if resolved is None:
            # Unknown namespace — leave placeholder untouched so the
            # author sees the typo in MCP logs rather than silently
            # getting an empty string.
            return match.group(0)
        return resolved

    return _PLACEHOLDER_RE.sub(_replace, text)


def _resolve_token(
    token: str,
    *,
    state: SessionState | None,
    inputs: dict[str, str],
    step_results: dict[str, ToolResult],
) -> str | None:
    """Return the resolved value for *token*, or ``None`` for unknown namespaces."""
    parts = token.split(".")
    namespace = parts[0]
    if namespace == "state":
        return _resolve_state(parts[1:], state)
    if namespace == "input":
        return _resolve_input(parts[1:], inputs)
    if namespace == "step":
        return _resolve_step(parts[1:], step_results)
    return None


def _resolve_state(parts: list[str], state: SessionState | None) -> str:
    """Resolve ``state.<field>`` references. Missing values → empty string."""
    if not parts or state is None:
        return ""
    field = parts[0]
    if field == "phase":
        return resolve_phase(state) or ""
    if field == "wavelength":
        return state.wavelength_snapshot or ""
    return ""


def _resolve_input(parts: list[str], inputs: dict[str, str]) -> str:
    """Resolve ``input.<key>`` references. Missing keys → empty string."""
    if not parts:
        return ""
    return inputs.get(parts[0], "")


def _resolve_step(parts: list[str], step_results: dict[str, ToolResult]) -> str:
    """Resolve ``step.<id>.<field>`` references; raise on any miss.

    Phase 2 (#326): step references are strict. A forward reference to
    a step that has not run yet, a bare ``{{step.<id>}}`` with no
    field, or any field other than ``body`` raises the private
    :class:`_StepRefError`. The walker catches the error and rebrands
    it as a fully-populated :class:`WorkflowStepError` via
    :func:`_rebrand_step_error`. Direct callers of this helper (or of
    :func:`interpolate`) see :class:`_StepRefError`, which subclasses
    :class:`ValueError`.
    """
    if not parts:
        msg = "step reference {{step}} is missing both id and field"
        raise _StepRefError(msg)
    if len(parts) < 2:
        step_id = parts[0]
        msg = (
            f"step reference {{step.{step_id}}} is missing a field; "
            "the only addressable field today is `body` "
            f"(e.g. {{{{step.{step_id}.body}}}})"
        )
        raise _StepRefError(msg)
    step_id, field = parts[0], parts[1]
    result = step_results.get(step_id)
    if result is None:
        available = sorted(step_results) or ["(none yet)"]
        msg = (
            f"step reference {{step.{step_id}.{field}}} points at an "
            f"upstream step that has not run yet; available step ids: "
            f"{available}"
        )
        raise _StepRefError(msg)
    if field != "body":
        msg = (
            f"step reference {{step.{step_id}.{field}}} uses unknown "
            "field; the only addressable field today is `body`"
        )
        raise _StepRefError(msg)
    return result.body


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class WorkflowRegistry:
    """Discover and look up workflow definitions.

    The registry scans two sources in order:

    1. :data:`BUILTIN_WORKFLOWS_DIR` — packaged reference workflows.
    2. ``<vault>/00-Creek-Meta/Workflows/`` — user-authored files.

    A user-authored workflow with the same ``name`` as a built-in wins.
    Malformed files are logged at WARNING and skipped so a single typo
    in the vault directory does not deny access to every other
    workflow.

    Caching: the first :meth:`list` / :meth:`get` call loads every file
    and pins the result. Subsequent calls reuse the cache for the
    lifetime of the registry instance, so a workflow added to the
    vault directory mid-session is NOT visible until the bot is
    restarted. This matches the FEAT-013 read-once startup pattern
    used elsewhere in CrawDad and is intentional — workflows are
    authored artifacts, not live state.
    """

    def __init__(
        self,
        *,
        vault_path: Path,
        builtin_dir: Path = BUILTIN_WORKFLOWS_DIR,
    ) -> None:
        """Cache the discovery paths. Workflows are loaded lazily on access."""
        self._vault_path = vault_path
        self._builtin_dir = builtin_dir
        self._cache: dict[str, WorkflowDefinition] | None = None

    def list(self) -> list[WorkflowDefinition]:
        """Return every discovered workflow, sorted by name."""
        return sorted(self._all().values(), key=lambda wf: wf.name)

    def get(self, name: str) -> WorkflowDefinition:
        """Return the workflow matching *name*.

        Raises:
            WorkflowNotFoundError: no workflow with that name exists.
        """
        workflows = self._all()
        if name not in workflows:
            available = ", ".join(sorted(workflows)) or "(none)"
            msg = f"no workflow named {name!r}; available: {available}"
            raise WorkflowNotFoundError(msg)
        return workflows[name]

    def _all(self) -> dict[str, WorkflowDefinition]:
        """Return ``{name: workflow}`` for every discoverable workflow.

        Built-ins first, then vault user-authored — the second write
        wins, so a vault override replaces the built-in entry.
        """
        if self._cache is not None:
            return self._cache
        collected: dict[str, WorkflowDefinition] = {}
        _absorb_dir(collected, self._builtin_dir)
        _absorb_dir(collected, self._vault_path / VAULT_WORKFLOWS_SUBPATH)
        self._cache = collected
        return collected


def _absorb_dir(target: dict[str, WorkflowDefinition], directory: Path) -> None:
    """Load every ``*.workflow.yaml`` file from *directory* into *target*.

    Missing or non-directory paths are silently ignored. Files that
    fail to parse are logged at WARNING and skipped so one typo cannot
    deny access to every other workflow.
    """
    if not directory.is_dir():
        return
    for path in sorted(directory.glob(f"*{_WORKFLOW_FILE_SUFFIX}")):
        try:
            wf = load_workflow_from_yaml(path)
        except WorkflowParseError as exc:
            _LOGGER.warning("skipping invalid workflow %s: %s", path, exc)
            continue
        target[wf.name] = wf


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


class WorkflowWalker:
    """Deterministic step walker for an authored workflow.

    The walker opens a single MCP session per workflow run, calls each
    step's tool with its interpolated args, and returns the ordered
    list of :class:`ToolResult` envelopes. The LLM router is bypassed
    entirely — the workflow file IS the routing decision.
    """

    def __init__(
        self,
        *,
        mcp_client: MCPClient | Any,
        known_tools: tuple[str, ...] | frozenset[str],
    ) -> None:
        """Cache the MCP client and the advertised tool surface."""
        self._mcp_client = mcp_client
        self._known_tools = frozenset(known_tools)

    async def run(
        self,
        workflow: WorkflowDefinition,
        *,
        state: SessionState | None,
        inputs: dict[str, str],
    ) -> list[ToolResult]:
        """Execute *workflow* against the current session.

        Args:
            workflow: The definition to run.
            state: Session state for phase resolution / ``state.*``
                interpolation. ``None`` is allowed for non-phase-aware
                workflows.
            inputs: User-supplied input values for ``{{input.<key>}}``
                references.

        Returns:
            One :class:`ToolResult` per step, in document order.

        Raises:
            WorkflowConstraintError: a declared constraint refused the
                run — missing input, wrong phase, unknown tool, etc.
            WorkflowStepError: a step failed mid-walk — either its
                ``args`` reference an unresolvable upstream step or
                its MCP tool call raised. The error names the failing
                step + tool and chains the original exception.
        """
        self._check_constraints(workflow, state=state, inputs=inputs)
        async with self._mcp_client.connect() as session:
            return await self._execute_steps(
                workflow, session=session, state=state, inputs=inputs
            )

    def _check_constraints(
        self,
        workflow: WorkflowDefinition,
        *,
        state: SessionState | None,
        inputs: dict[str, str],
    ) -> None:
        """Validate phase, inputs, and tool surface before any tool fires."""
        missing_inputs = [name for name in workflow.inputs if name not in inputs]
        if missing_inputs:
            msg = (
                f"workflow {workflow.name!r} requires inputs {missing_inputs} "
                "that were not provided"
            )
            raise WorkflowConstraintError(msg)
        unknown_tools = [
            step.tool for step in workflow.steps if step.tool not in self._known_tools
        ]
        if unknown_tools:
            msg = (
                f"workflow {workflow.name!r} references tools that are not "
                f"advertised by the MCP server: {unknown_tools}"
            )
            raise WorkflowConstraintError(msg)
        if workflow.phase_aware:
            self._check_phase(workflow, state)

    def _check_phase(
        self, workflow: WorkflowDefinition, state: SessionState | None
    ) -> None:
        """Refuse phase-aware workflows when the current phase doesn't match."""
        phase = resolve_phase(state)
        if phase is None:
            msg = (
                f"workflow {workflow.name!r} is phase-aware but no session "
                "phase is available — run `creek state` first"
            )
            raise WorkflowConstraintError(msg)
        if workflow.allowed_phases and phase not in workflow.allowed_phases:
            msg = (
                f"workflow {workflow.name!r} refuses to run in phase "
                f"{phase!r}; allowed phases: {list(workflow.allowed_phases)}"
            )
            raise WorkflowConstraintError(msg)

    async def _execute_steps(
        self,
        workflow: WorkflowDefinition,
        *,
        session: Any,
        state: SessionState | None,
        inputs: dict[str, str],
    ) -> list[ToolResult]:
        """Walk each step in order, threading prior results into later args.

        Phase 2 (#326): each step is wrapped in a single ``try/except``
        so an interpolation error OR an MCP tool failure aborts the
        workflow with a :class:`WorkflowStepError` that names the
        workflow, the failing step's id, and the tool that was about
        to be invoked. The original exception is preserved as the
        cause.
        """
        step_results: dict[str, ToolResult] = {}
        ordered: list[ToolResult] = []
        for step in workflow.steps:
            result = await self._execute_one_step(
                step,
                workflow=workflow,
                session=session,
                state=state,
                inputs=inputs,
                step_results=step_results,
            )
            step_results[step.id] = result
            ordered.append(result)
        return ordered

    async def _execute_one_step(
        self,
        step: WorkflowStep,
        *,
        workflow: WorkflowDefinition,
        session: Any,
        state: SessionState | None,
        inputs: dict[str, str],
        step_results: dict[str, ToolResult],
    ) -> ToolResult:
        """Resolve args, call the MCP tool, and wrap any failure with metadata."""
        try:
            resolved_args = interpolate(
                step.args, state=state, inputs=inputs, step_results=step_results
            )
            arguments = _build_call_arguments(
                resolved_args, workflow.privacy_tier_floor
            )
            body = await session.call_tool(step.tool, arguments)
        except (_StepRefError, WorkflowStepError) as exc:
            raise _rebrand_step_error(exc, workflow=workflow, step=step) from exc
        except Exception as exc:
            raise _wrap_step_failure(exc, workflow=workflow, step=step) from exc
        return ToolResult(
            intent_type=step.tool,
            body=body,
            privacy_tier_ceiling=workflow.privacy_tier_floor,
        )


def _rebrand_step_error(
    exc: _StepRefError | WorkflowStepError,
    *,
    workflow: WorkflowDefinition,
    step: WorkflowStep,
) -> WorkflowStepError:
    """Attach workflow + step metadata to an interpolation-time step error.

    :func:`_resolve_step` raises :class:`_StepRefError` because it has
    no workflow / step context to populate a full
    :class:`WorkflowStepError`. The walker has that context, so it
    re-raises here with the metadata filled in and the original
    (anonymous) message prefixed by the step location. The original
    exception is preserved via ``__cause__`` at the raise site so the
    caller can ``isinstance(exc.__cause__, _StepRefError)`` to
    distinguish "interpolation failed" from "MCP tool raised".

    Accepts :class:`WorkflowStepError` too as defence-in-depth: if a
    downstream helper ever surfaces one already populated, the walker
    rebrands it under the current step's identity rather than letting
    a stale identity leak through. In practice only
    :class:`_StepRefError` reaches this path today.
    """
    location = f"workflow {workflow.name!r} step {step.id!r} (tool {step.tool!r})"
    return WorkflowStepError(
        f"{location} aborted during arg interpolation: {exc}",
        step_id=step.id,
        tool=step.tool,
        workflow_name=workflow.name,
    )


def _wrap_step_failure(
    exc: BaseException,
    *,
    workflow: WorkflowDefinition,
    step: WorkflowStep,
) -> WorkflowStepError:
    """Wrap an MCP tool failure so the operator can find the offending step.

    The walker invokes one MCP tool per step; when that tool raises
    (e.g. an :class:`crawdad.mcp_client.MCPUnavailableError`, a network
    blip, a bad arg type), we re-raise as :class:`WorkflowStepError`
    with the workflow name + step id + tool name baked into the
    message and exposed as attributes. The original exception is
    chained via ``__cause__`` for introspection.
    """
    location = f"workflow {workflow.name!r} step {step.id!r} (tool {step.tool!r})"
    return WorkflowStepError(
        f"{location} failed: {exc}",
        step_id=step.id,
        tool=step.tool,
        workflow_name=workflow.name,
    )


def _build_call_arguments(
    resolved_args: Any, floor: PrivacyTierCeiling
) -> dict[str, Any]:
    """Merge interpolated args with the privacy floor, matching dispatcher semantics.

    The MCP server reads ``privacy_tier_ceiling`` as a sibling argument
    on every tool call (see :mod:`crawdad.dispatcher._build_arguments`).
    The workflow's declared floor is the contract; any colliding key
    in the user-authored args is overwritten so a workflow author
    cannot accidentally smuggle a looser tier in through the args dict.
    """
    payload: dict[str, Any] = (
        dict(resolved_args) if isinstance(resolved_args, dict) else {}
    )
    payload["privacy_tier_ceiling"] = floor.value
    return payload


# ---------------------------------------------------------------------------
# High-level runner
# ---------------------------------------------------------------------------


async def run_workflow_and_compose(
    *,
    workflow: WorkflowDefinition,
    inputs: dict[str, str],
    state: SessionState | None,
    skills: VoiceSkillStack,
    skill_registry: SkillStackRegistry | None,
    composer: Any,
    mcp_client: Any,
    known_tools: tuple[str, ...],
) -> str:
    """Walk *workflow* end-to-end and ask the composer for the user-facing reply.

    The walker bypasses the FEAT-014 router but reuses the FEAT-015
    composer so workflow output sounds like every other CrawDad reply
    — voice-faithful, phase-aware, paradox-tolerant.

    Skill-stack resolution mirrors the FEAT-029 ``AgentLoop`` pattern:
    when ``skill_registry`` is supplied its ``.stack`` wins and the
    ``skills`` argument is the fallback used only when the registry is
    ``None``. The CLI runner always provides both (with ``skills``
    defaulting to ``registry.stack``) so the registry's live, swap-
    after-startup behaviour persists across turns; callers without a
    registry (e.g. early unit tests) can hand a static stack in via
    ``skills``.

    Returns:
        The composer's text reply. Callers map this directly into a
        Discord message body.

    Raises:
        WorkflowConstraintError: a declared constraint refused the run
            (missing required input, wrong phase, unknown MCP tool).
            Callers map these to a Discord soft error rather than
            crashing the bot.
        WorkflowStepError: the walker aborted mid-walk because a step
            failed — either its interpolation references an
            unresolvable upstream step or its MCP tool call raised.
            The error names the failing step + tool + workflow and
            chains the original exception via ``__cause__``.
    """
    from crawdad.history import ConversationHistory

    walker = WorkflowWalker(mcp_client=mcp_client, known_tools=known_tools)
    results = await walker.run(workflow, state=state, inputs=inputs)
    active_skills = skills if skill_registry is None else skill_registry.stack
    pseudo_message = _synthesize_user_message(workflow, inputs)
    reply = await composer.compose(
        user_message=pseudo_message,
        tool_results=results,
        history=ConversationHistory(),
        state=state,
        skills=active_skills,
    )
    return cast("str", reply)


def _synthesize_user_message(
    workflow: WorkflowDefinition, inputs: dict[str, str]
) -> str:
    """Return the synthetic user message the composer sees for a workflow run.

    The composer expects a user message describing intent; for a
    workflow run there is no free-text turn, so we fabricate one from
    the workflow name + description + supplied inputs. This lets the
    composer condition its reply on the right context without changing
    its interface.
    """
    lines = [
        f"Run workflow: {workflow.name}.",
        workflow.description,
    ]
    if inputs:
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(inputs.items()))
        lines.append(f"Inputs: {rendered}.")
    return "\n".join(lines)
