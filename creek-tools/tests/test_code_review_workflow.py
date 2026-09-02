"""``code-review.yml`` must be re-runnable without manufacturing a push.

Issue #1201. ``.github/workflows/code-review.yml`` triggers on ``pull_request``
only, so the single way to obtain a fresh Claude review is to produce one of
``opened``/``synchronize``/``ready_for_review``/``reopened``. In practice that
means an empty commit, which costs a full CI round (~14 min) and — because
every push invalidates the previous verdict under ``pr-ready.sh``'s
stale-verdict guard — is the very act that destroys the review a lane was
waiting on. ``gh run rerun`` is not a substitute: it replays the run's own
workflow file, so it cannot exercise a fixed workflow.

The un-wedge that matters most is ``ready-unreviewed``. ``code-review.yml:42``
skips runs whose *actor* is Dependabot, because GitHub withholds
``CLAUDE_CODE_OAUTH_TOKEN`` from those runs. Secrets ARE available on a
``workflow_dispatch``, and ``github.actor`` there is the dispatching human, so a
manual trigger is exactly the path that lets a bot PR earn a real verdict.

**Why these assertions are on the parsed document and on the raw text, not on
one or the other.** Parsing is the only honest way to check that a trigger sits
at the right nesting level or that ``ref:`` is on the checkout step rather than
somewhere that merely looks similar — a grep passes against a trigger nested
under the wrong key. But parsing cannot count: the real failure mode here is
five copies of ``${{ github.event.pull_request.number }}`` collapsing back to
more than one, each of which resolves to the EMPTY STRING under a dispatch. The
prompt would then read "pull request #", and the ``reviewed_pr_number``
cross-check would compare against an empty ``$PR_NUMBER`` and ``exit 1`` — a
silent, review-shaped nothing rather than an obvious break. So the count is
asserted over the raw file, exactly once, and never as ``>= 0``.
"""

from __future__ import annotations

import re
from typing import Any

from tests.shell_command_support import WORKFLOWS_DIR, load_yaml

REVIEW_WORKFLOW = WORKFLOWS_DIR / "code-review.yml"

#: The bare interpolation that a second trigger empties. PyYAML resolves the
#: unquoted ``on`` key to :data:`True` under YAML 1.1, so the triggers are read
#: through :func:`_triggers` rather than by subscripting ``"on"``.
_BARE_PR_NUMBER = re.compile(r"\$\{\{\s*github\.event\.pull_request\.number\s*\}\}")

#: The four ``pull_request`` activity types the workflow reviews on today.
_EXPECTED_PR_TYPES = ["opened", "synchronize", "ready_for_review", "reopened"]


def _document() -> dict[Any, Any]:
    """Return the parsed workflow document.

    Returns:
        The top-level mapping of ``code-review.yml``.
    """
    return load_yaml(REVIEW_WORKFLOW)


def _triggers() -> dict[str, Any]:
    """Return the workflow's ``on:`` mapping.

    YAML 1.1 resolves a bare ``on`` key to the boolean ``True``, which is what
    PyYAML hands back; accept either spelling so this helper cannot silently
    return an empty mapping and make every assertion below vacuous.

    Returns:
        The trigger mapping.

    Raises:
        AssertionError: If the workflow declares no triggers at all.
    """
    document = _document()
    triggers = document.get(True, document.get("on"))
    assert isinstance(triggers, dict) and triggers, (
        f"{REVIEW_WORKFLOW.name} parsed with no `on:` mapping; every trigger "
        "assertion below would be checking nothing"
    )
    return triggers


def _review_job() -> dict[str, Any]:
    """Return the ``claude-review`` job mapping.

    Returns:
        The job that runs the reviewer.

    Raises:
        AssertionError: If the job key has been renamed. ``pr-ready.sh`` keys
            its check lookup on that name, so a rename is itself a defect.
    """
    jobs = _document()["jobs"]
    assert "claude-review" in jobs, (
        f"{REVIEW_WORKFLOW.name} no longer defines a `claude-review` job; "
        "pr-ready.sh resolves the review check by that key"
    )
    job: dict[str, Any] = jobs["claude-review"]
    return job


def _checkout_step() -> dict[str, Any]:
    """Return the ``actions/checkout`` step of the review job.

    Returns:
        The checkout step mapping.

    Raises:
        AssertionError: If the job has no checkout step.
    """
    steps: list[dict[str, Any]] = _review_job()["steps"]
    checkouts: list[dict[str, Any]] = [
        step for step in steps if "actions/checkout" in str(step.get("uses", ""))
    ]
    assert len(checkouts) == 1, (
        f"expected exactly one actions/checkout step in {REVIEW_WORKFLOW.name}, "
        f"found {len(checkouts)}"
    )
    return checkouts[0]


def test_the_workflow_accepts_a_manual_dispatch() -> None:
    """A re-review must be requestable without producing a commit.

    ``workflow_dispatch`` is write-gated by GitHub, so this widens nothing —
    and it is the only trigger under which the secrets a Dependabot-actor run
    is denied are actually present.
    """
    triggers = _triggers()
    assert "workflow_dispatch" in triggers, (
        f"{REVIEW_WORKFLOW.name} has no `workflow_dispatch` trigger, so the "
        "only way to re-run a review is an empty commit — which invalidates "
        "the verdict the lane was waiting for (#1201)"
    )


def test_the_dispatch_requires_the_pr_number_it_will_review() -> None:
    """The dispatch must name its PR, because nothing else can supply it.

    On a dispatch there is no ``github.event.pull_request`` at all. An optional
    input would let a run start with an empty number and review ``main`` while
    posting onto nothing.
    """
    dispatch = _triggers().get("workflow_dispatch")
    assert isinstance(dispatch, dict), (
        "`workflow_dispatch` carries no `inputs:` block, so a dispatched run "
        "has no PR number to review (#1201)"
    )
    inputs = dispatch.get("inputs") or {}
    assert "pr_number" in inputs, (
        f"`workflow_dispatch.inputs` is {sorted(inputs)}; it must declare "
        "`pr_number` — the dispatched run has no event payload to derive it from"
    )
    assert inputs["pr_number"].get("required") is True, (
        "`pr_number` must be `required: true`; an omitted number resolves to "
        "the empty string and the run reviews the default branch"
    )


def test_the_existing_pull_request_trigger_is_untouched() -> None:
    """Adding a trigger must not narrow the automatic one.

    Dropping ``ready_for_review`` (say) would silently stop reviewing PRs that
    leave draft, and nothing else in the repo would notice.
    """
    pull_request = _triggers().get("pull_request")
    assert isinstance(pull_request, dict), (
        "the `pull_request` trigger disappeared; automatic review is the "
        "primary path and #1201 only adds a second one"
    )
    assert pull_request.get("types") == _EXPECTED_PR_TYPES, (
        f"`pull_request.types` is {pull_request.get('types')!r}, expected "
        f"{_EXPECTED_PR_TYPES!r}"
    )


def test_the_concurrency_group_names_the_pr_not_the_ref() -> None:
    """Two reviews of one PR must never run side by side.

    ``github.ref`` on a dispatch is the dispatched ref (``refs/heads/main``),
    not ``refs/pull/N/merge``. A ref-keyed group therefore puts the manual run
    in a DIFFERENT group from the automatic one: neither cancels the other, and
    both post a verdict comment onto the same thread.

    Both halves are asserted separately on purpose. A single "contains
    ``inputs.pr_number``" check passes against a group that kept ``github.ref``
    alongside it, which is precisely the shape that still splits the group.
    """
    group = str(_document()["concurrency"]["group"])
    assert "inputs.pr_number" in group, (
        f"concurrency group is {group!r}; it must resolve to the PR number "
        "under both triggers, or a dispatched review lands in its own group "
        "and races the automatic one (#1201)"
    )
    assert "github.ref" not in group, (
        f"concurrency group is {group!r}; `github.ref` is the dispatched ref "
        "on a manual run, so it does not identify the PR"
    )
    assert "github.head_ref" not in group, (
        f"concurrency group is {group!r}; `github.head_ref` is empty outside "
        "a pull_request event"
    )


def test_the_checkout_pins_the_pull_request_merge_ref() -> None:
    """A dispatched run must review the PR, not the branch it was fired from.

    ``pull_request`` supplies ``refs/pull/N/merge`` implicitly; a dispatch does
    not, so the reviewer would read ``main``'s tree while being told it is on
    the PR. The prompt asserts that checkout ref unconditionally, so leaving it
    implicit makes the workflow lie to the model.
    """
    with_block = _checkout_step().get("with") or {}
    ref = str(with_block.get("ref", ""))
    assert ref.startswith("refs/pull/"), (
        f"the checkout step declares ref={ref!r}; it must pin "
        "`refs/pull/<N>/merge` so a dispatched run reviews the PR rather than "
        "the dispatched branch (#1201)"
    )
    assert "/merge" in ref, (
        f"the checkout ref {ref!r} is not the merge ref the prompt claims to "
        "be running against"
    )


def test_the_pr_number_is_resolved_exactly_once() -> None:
    """Five event-shaped copies is how the second trigger breaks silently.

    Every bare ``${{ github.event.pull_request.number }}`` resolves to the
    empty string on a dispatch. The one that survives is the job-level
    ``PR_NUMBER:`` resolution carrying the ``|| inputs.pr_number`` fallback;
    every other site must read that resolved value.

    Counted over the raw file, and asserted ``== 1`` rather than ``>= 0``: the
    failure this catches is a reintroduced copy, and a lower-bound assertion
    cannot see one.
    """
    text = REVIEW_WORKFLOW.read_text(encoding="utf-8")
    hits = _BARE_PR_NUMBER.findall(text)
    assert len(hits) == 1, (
        f"{REVIEW_WORKFLOW.name} interpolates "
        "`${{ github.event.pull_request.number }}` "
        f"{len(hits)} time(s); exactly one is allowed (the job-level PR_NUMBER "
        "resolution). Every other copy resolves to the empty string under "
        "`workflow_dispatch`, which makes the prompt read 'pull request #' and "
        "makes the reviewed_pr_number cross-check exit 1 on an empty value "
        "(#1201)"
    )


def test_the_job_resolves_pr_number_with_a_dispatch_fallback() -> None:
    """The single resolution point must cover both triggers.

    A job-level ``env`` that still reads only the event payload would satisfy
    the count above while leaving every consumer empty on a dispatch.
    """
    env = _review_job().get("env") or {}
    assert "PR_NUMBER" in env, (
        f"the claude-review job declares env {sorted(env)}; it must resolve "
        "`PR_NUMBER` once at job level so the prompt, the post step and the "
        "checkout all read the same value (#1201)"
    )
    expression = str(env["PR_NUMBER"])
    assert "github.event.pull_request.number" in expression, (
        f"PR_NUMBER is {expression!r}; it must still resolve from the event "
        "payload on the automatic path"
    )
    assert "inputs.pr_number" in expression, (
        f"PR_NUMBER is {expression!r}; without the `|| inputs.pr_number` "
        "fallback a dispatched run resolves it to the empty string"
    )
