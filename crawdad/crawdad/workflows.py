"""Workflow DSL (ADAPT-003 / issue #268).

CrawDad composite commands compile down to a deterministic walk over
authored YAML files. Each file declares a named workflow: a list of
:class:`WorkflowStep` entries that map one-to-one to MCP tool calls,
plus first-class constraint metadata (``phase_aware`` /
``privacy_tier_ceiling``).

Pre-flight enforcement
----------------------

:meth:`WorkflowWalker._check_constraints` runs before the MCP session is
opened, so a refusal costs zero tool calls. It enforces exactly five
things, raising :class:`WorkflowConstraintError` on the first that
fails — in this order, privacy first:

1. ``privacy_tier_ceiling`` is inside :data:`WORKFLOW_ADMITTED_CEILINGS`;
2. no step's ``args`` declares a ``privacy_tier_ceiling`` of its own —
   per-step ceilings are not a supported feature;
3. every name in ``inputs:`` was supplied by the caller;
4. every step's ``tool:`` is advertised by the live MCP server;
5. when ``phase_aware: true``, a session phase exists and (if
   ``allowed_phases`` is non-empty) is one of the allowed ones.

Note what is *not* enforced here: the admitted ceiling value itself is
merely forwarded, verbatim, as a sibling argument on every tool call.
The MCP server is what actually applies it when selecting content. The
walker's only privacy job is to refuse the ceilings CrawDad must never
request at all — see :data:`WORKFLOW_ADMITTED_CEILINGS`.

File format
-----------

Plain YAML. No custom grammar, no Jinja2 dependency. A workflow file
looks like::

    name: substack-draft-phase-transitions
    description: Draft a Substack post on phase transitions
    trigger: "/crawdad workflow run substack-draft-phase-transitions"
    phase_aware: true
    allowed_phases: [rising, peak]
    privacy_tier_ceiling: personal
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
from typing import TYPE_CHECKING, Any, Final, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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

# The canonical privacy-ceiling key. The YAML key, the
# :class:`WorkflowDefinition` field, and the sibling argument the MCP
# server reads all share this one spelling by design, so the author
# reads the same word in the file, the model, and the wire call.
_CEILING_KEY = "privacy_tier_ceiling"

# The pre-#1051 spelling of that key. Still accepted, value-preserving,
# with a deprecation warning — see
# :meth:`WorkflowDefinition._migrate_privacy_tier_floor`.
_LEGACY_CEILING_KEY = "privacy_tier_floor"

# The only privacy tier ceilings an authored workflow may request.
#
# CrawDad relays every :class:`~crawdad.dispatcher.ToolResult` body to a
# cloud LLM composer and then posts the reply into a Discord message, so
# ``intimate`` / ``all`` must never be requestable by a workflow file.
# ``ALL`` subsumes ``intimate``, so the admitted set is the complement.
#
# The value coincides with ``creek_mcp.policy.REMOTE_ADMITTED_CEILINGS``
# (``{OPEN, PERSONAL}`` — "intimate content is not reachable over the
# network") but is NOT derived from it, and the two are not canonical
# for each other: that cap draws the line at the network, this one at
# the cloud composer. The reasoning above stands on its own if the
# server-side set ever changes.
#
# This walker check is the ONLY gate on this path, not a redundant second
# copy of the server's: CrawDad reaches the MCP server over **stdio**,
# which ``creek_mcp/server.py::_caller_identity`` classifies as a LOCAL
# caller (``is_remote=False``), so the server-side remote cap never fires
# here. (Prose reference only — crawdad has no Python dependency on
# creek-tools.)
#
# Membership (``in``), never a rank comparison: ``crawdad.loop`` owns the
# single tier-ordering table and a second one must not exist.
WORKFLOW_ADMITTED_CEILINGS: Final[frozenset[PrivacyTierCeiling]] = frozenset(
    {PrivacyTierCeiling.OPEN, PrivacyTierCeiling.PERSONAL}
)

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
    ``allowed_phases``, a step referencing an unadvertised MCP tool, a
    ``privacy_tier_ceiling`` outside :data:`WORKFLOW_ADMITTED_CEILINGS`,
    or a step whose ``args`` tries to declare its own
    ``privacy_tier_ceiling``.

    Every one of these is checked pre-flight, before the MCP session is
    opened. Keep this list in sync with
    :meth:`WorkflowWalker._check_constraints` — stale prose here is how
    the ceiling gap of #1051 hid in plain sight.
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

    ``phase_aware`` and ``privacy_tier_ceiling`` are the AC's
    first-class constraint attributes. Both are soft-fail gates: the
    walker raises :class:`WorkflowConstraintError`, which the
    slash-command surface turns into a clean user-facing message rather
    than a stack trace. That mechanism is unchanged.

    What they cover, precisely (#1051 — the previous wording implied the
    ceiling was enforced when nothing read it at all):

    * ``phase_aware`` / ``allowed_phases`` gate *when* the workflow may
      run.
    * ``privacy_tier_ceiling`` is capped at walk time to
      :data:`WORKFLOW_ADMITTED_CEILINGS`; an admitted value is then
      forwarded verbatim to the MCP server, which is what applies it.
      A ceiling above the cap is refused loudly at run time and
      deliberately NOT at parse time — see
      :meth:`WorkflowWalker._check_privacy_ceiling`.

    ``privacy_tier_floor`` is the deprecated spelling of
    ``privacy_tier_ceiling``. It is still accepted, value-preserving,
    with a WARNING; declaring both keys is an error. See
    :meth:`_migrate_privacy_tier_floor`.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    description: str
    trigger: str | None = None
    phase_aware: bool = False
    allowed_phases: tuple[str, ...] = ()
    privacy_tier_ceiling: PrivacyTierCeiling = PrivacyTierCeiling.OPEN
    inputs: tuple[str, ...] = ()
    steps: tuple[WorkflowStep, ...]

    @model_validator(mode="before")
    @classmethod
    def _migrate_privacy_tier_floor(cls, data: Any) -> Any:
        """Accept the deprecated ``privacy_tier_floor`` key, warning once.

        The field was misnamed (#1051): ``open`` is the MOST restrictive
        tier and ``all`` the broadest, so raising the so-called "floor"
        *widened* what the MCP server returned. The key always was a
        ceiling. The migration is therefore value-preserving — a file
        declaring ``privacy_tier_floor: personal`` keeps requesting
        exactly ``personal``, it just stops lying about the direction.

        ``mode="before"`` so both entry points are covered:
        :func:`load_workflow_from_yaml`'s ``model_validate(dict)`` and
        direct ``WorkflowDefinition(privacy_tier_floor=...)`` kwargs
        construction.

        Declaring both spellings is ambiguous — silently picking a
        winner could ship a different effective ceiling than the author
        last read — so it raises :class:`ValueError`. That type is
        deliberate: pydantic converts only ``ValueError`` /
        ``AssertionError`` into a ``ValidationError``, which
        :func:`load_workflow_from_yaml` already maps to
        :class:`WorkflowParseError`.
        """
        # Pydantic hands non-dict payloads to "before" validators in
        # some construction modes; nothing to migrate in those.
        if not isinstance(data, dict):
            return data
        if _LEGACY_CEILING_KEY not in data:
            return data
        raw_name = data.get("name")
        name = raw_name if isinstance(raw_name, str) else "<unnamed>"
        if _CEILING_KEY in data:
            msg = (
                f"workflow {name!r} declares both {_LEGACY_CEILING_KEY!r} and "
                f"{_CEILING_KEY!r}; the former is the deprecated spelling of "
                f"the latter — keep only {_CEILING_KEY!r}"
            )
            raise ValueError(msg)
        # Copy rather than mutate: the caller may still own this dict.
        # The legacy key is popped, not merely shadowed, so no stale
        # spelling survives into the model payload.
        migrated = dict(data)
        migrated[_CEILING_KEY] = migrated.pop(_LEGACY_CEILING_KEY)
        _LOGGER.warning(
            "workflow %r uses the deprecated key %r; rename it to %r. The "
            "value is unchanged: the key always was a ceiling, not a floor "
            "(%r is the most restrictive tier, %r the broadest). The alias "
            "will be removed — see issue #1151.",
            name,
            _LEGACY_CEILING_KEY,
            _CEILING_KEY,
            PrivacyTierCeiling.OPEN.value,
            PrivacyTierCeiling.ALL.value,
        )
        return migrated

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
                run — missing input, unknown tool, a privacy ceiling
                above the cap, a step-level ceiling, or the wrong
                phase. Always raised before the MCP session opens.
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
        """Validate privacy ceilings, inputs, tools, and phase pre-flight.

        Every check here runs before :meth:`run` opens the MCP session,
        so a refusal costs zero tool calls. Kept as a flat sequence of
        guards — each non-trivial rule lives in its own ``_check_*``
        helper so this stays readable as the rule set grows.

        The privacy guards run FIRST, and that order is deliberate:
        only the first failure is reported, so putting the missing-input
        or unknown-tool checks ahead of them would walk an author with
        several problems up to the privacy refusal one round trip at a
        time. The privacy rule must never be the second thing said.
        """
        self._check_privacy_ceiling(workflow)
        self._check_step_arg_ceilings(workflow)
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

    def _check_privacy_ceiling(self, workflow: WorkflowDefinition) -> None:
        """Refuse a workflow ceiling outside :data:`WORKFLOW_ADMITTED_CEILINGS`.

        The cap is enforced HERE, at walk time, and deliberately not as
        a pydantic validator. Two reasons, and the second is the one
        that must stop a future editor from "improving" this into a
        validator:

        1. :func:`_absorb_dir` catches :class:`WorkflowParseError` and
           does ``warning(); continue``, so a parse-time rejection would
           make the workflow vanish from ``/crawdad workflow list`` with
           no user-visible reason. Fail-invisible is worse than the bug
           this closes.
        2. A validator only guards *validated* construction.
           ``model_construct`` and ``model_copy(update=...)`` sail
           straight past one — both verified to produce an ``all``-tier
           definition. This check sits on the only path to
           ``session.call_tool``, so it holds for every construction
           route, not just the parsed one.

        The message lands verbatim in a Discord reply (see
        :func:`crawdad.slash_commands._reply_workflow_run`), so it names
        the workflow, the refused tier, the allowed set, and why. Every
        token it interpolates is operator-*authored* — a workflow name,
        a tier the author typed, a module constant. Keep it that way:
        interpolating a tool result or any vault-derived value would
        turn a refusal into an oracle over content the caller never had
        (the #1090 hazard), which is exactly what this refusal is free
        of today.
        """
        tier = workflow.privacy_tier_ceiling
        if tier not in WORKFLOW_ADMITTED_CEILINGS:
            allowed = ", ".join(sorted(t.value for t in WORKFLOW_ADMITTED_CEILINGS))
            msg = (
                f"workflow {workflow.name!r} declares {_CEILING_KEY} "
                f"{tier.value!r}, which CrawDad refuses to request: every tool "
                "result is relayed to a cloud LLM composer and then posted "
                "into a Discord message, so material above the cap must never "
                f"be asked for on this path. Allowed ceilings: {allowed}."
            )
            raise WorkflowConstraintError(msg)

    def _check_step_arg_ceilings(self, workflow: WorkflowDefinition) -> None:
        """Refuse any step whose ``args`` declares its own privacy ceiling.

        ANY step-level declaration is refused, not just a widening one.
        Deciding "is this wider?" would need a tier-ordering comparison
        (``crawdad.loop`` owns the only such table), and admitting a
        narrowing value would teach authors that per-step ceilings are a
        supported feature when they are not.

        Today such a key is silently overwritten by
        :func:`_build_call_arguments`. That is safe, but it gives the
        author neither the tier they asked for nor any signal their line
        was discarded — the same false-assurance defect as the headline
        bug of #1051, one level down.

        Both spellings are refused. Scoping this to :data:`_CEILING_KEY`
        alone would leave a step's ``privacy_tier_floor: all`` silently
        accepted — that name is dead everywhere else, no MCP tool reads
        it, and a line that does nothing while looking like a privacy
        control is the very shape #1051 exists to close.
        """
        offenders = [
            step.id
            for step in workflow.steps
            if _CEILING_KEY in step.args or _LEGACY_CEILING_KEY in step.args
        ]
        if offenders:
            msg = (
                f"workflow {workflow.name!r} sets {_CEILING_KEY!r} (or its "
                f"deprecated spelling {_LEGACY_CEILING_KEY!r}) in the args of "
                f"step(s) {offenders}: per-step privacy ceilings are not "
                "supported — the workflow-level ceiling applies to every step. "
                "Remove the key from those steps."
            )
            raise WorkflowConstraintError(msg)

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
                resolved_args, workflow.privacy_tier_ceiling
            )
            body = await session.call_tool(step.tool, arguments)
        except (_StepRefError, WorkflowStepError) as exc:
            raise _rebrand_step_error(exc, workflow=workflow, step=step) from exc
        except Exception as exc:
            raise _wrap_step_failure(exc, workflow=workflow, step=step) from exc
        return ToolResult(
            intent_type=step.tool,
            body=body,
            privacy_tier_ceiling=workflow.privacy_tier_ceiling,
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
    resolved_args: Any, ceiling: PrivacyTierCeiling
) -> dict[str, Any]:
    """Merge interpolated args with the privacy ceiling, dispatcher-style.

    The MCP server reads ``privacy_tier_ceiling`` as a sibling argument
    on every tool call (see :mod:`crawdad.dispatcher._build_arguments`).
    The workflow's declared ceiling is the contract, so any colliding
    key in the resolved args is overwritten rather than honoured.

    :meth:`WorkflowWalker._check_step_arg_ceilings` now refuses that
    collision up front, loudly, before the session opens. This
    overwrite stays as defence in depth for a future programmatic
    caller that reaches this helper without having gone through
    :meth:`WorkflowWalker._check_constraints`. Never remove the belt
    because braces were added.

    It is NOT a guard against a ceiling appearing during interpolation:
    :func:`interpolate` recurses over dict *values* only, so no new
    top-level key can materialise. Saying otherwise would be exactly
    the kind of overclaiming prose #1051 is a postmortem about.
    """
    payload: dict[str, Any] = (
        dict(resolved_args) if isinstance(resolved_args, dict) else {}
    )
    payload[_CEILING_KEY] = ceiling.value
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
            (missing required input, wrong phase, unknown MCP tool, a
            privacy ceiling outside :data:`WORKFLOW_ADMITTED_CEILINGS`,
            or a step declaring its own ceiling). Callers map these to
            a Discord soft error rather than crashing the bot.
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
