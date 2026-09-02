"""Every path that clears a merge must apply the same verdict rule.

Issues #1263 and #1266. Three places decide whether a PR is cleared to merge,
and each holds its own copy of the rule:

* ``scripts/ralph/pr-ready.sh`` — the token the orchestrator and the watcher
  both act on;
* ``.github/workflows/iteration-trigger.yml`` — the mobile wake path, which
  posts "You are cleared to squash merge";
* ``.claude/skills/await-claude-review/SKILL.md`` and
  ``.claude/skills/address-feedback/SKILL.md`` — the agent-driven path which,
  per await-claude-review's own Step 3, WINS when it and ``pr-ready.sh``
  disagree.

Duplication is the defect, not a side effect of it. #1266 exists because a
``.body != null`` guard was written into one copy and not the other; #1199's
author allowlist had to be added to each copy separately for exactly the same
reason; and the tree currently spends around forty lines of comments warning
that the two shell copies must stay parallel-but-not-identical. That comment
burden IS the smell.

**Why the fix forces one shared file rather than a second careful edit.**
``gh pr view --json comments`` cannot return edit provenance — measured, its
keys are ``author``, ``authorAssociation``, ``body``, ``createdAt``, ``id``,
``includesCreatedEdit``, ``isMinimized``, ``minimizedReason``,
``reactionGroups``, ``url``, ``viewerDidAuthor`` — and the REST endpoint
``iteration-trigger.yml`` reads carries no editor identity at all. Neither
consumer can reach #1263's requirement by tightening the jq it already has, so
both must move to ``gh api graphql``. Once both read the same payload, the
``.user.login`` / ``.author.login`` and ``github-actions[bot]`` /
``github-actions`` divergence trap that file documents at length simply ceases
to exist, and sharing one filter is nearly free.

**The partial-delegation failure this module exists to catch.** If
``pr-ready.sh`` moves to the shared filter and ``iteration-trigger.yml`` keeps
its inline copy, every behavioural test in ``test_verdict_select_filter.py``
passes, a sweep table reads clean, and the hole simply sits in the file nobody
migrated. Only an assertion that BOTH files name the shared path catches it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.shell_command_support import (
    RALPH_SCRIPTS_DIR,
    REPO_ROOT,
    WORKFLOWS_DIR,
    load_yaml,
    non_comment_lines,
)

ITERATION_TRIGGER = WORKFLOWS_DIR / "iteration-trigger.yml"
PR_READY = RALPH_SCRIPTS_DIR / "pr-ready.sh"
#: The wake summary's implementation. It used to be the workflow's ``run:``
#: body, where no test could EXECUTE it — so every guard on it was a per-line
#: grep, and a static guard on code nobody runs is evadable by keeping the
#: guarded line alive and making it dead. Three such mutants survived a fully
#: green suite (#1685). ``scripts/ralph/test_verdict_wake.sh`` drives this file
#: end to end against fixtures with a stubbed ``gh``; the assertions in this
#: module stay as a cheap second line and follow the code into it.
VERDICT_WAKE = RALPH_SCRIPTS_DIR / "verdict-wake.sh"
VERDICT_FILTER = RALPH_SCRIPTS_DIR / "lib" / "verdict-select.jq"
COMMENTS_QUERY = RALPH_SCRIPTS_DIR / "lib" / "pr-comments.graphql"

#: The repo-relative path both consumers must name.
_FILTER_PATH = "scripts/ralph/lib/verdict-select.jq"

#: The skills that decide a merge in prose. Named individually rather than
#: globbed: a glob would silently pass on the day one is renamed.
_CLEARANCE_SKILLS = (
    REPO_ROOT / ".claude" / "skills" / "await-claude-review" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "address-feedback" / "SKILL.md",
)


def _iteration_trigger_steps() -> list[dict[str, Any]]:
    """Return every step of the iteration-trigger workflow.

    Returns:
        The step mappings, in job-then-step order.
    """
    document = load_yaml(ITERATION_TRIGGER)
    steps: list[dict[str, Any]] = []
    for job in document["jobs"].values():
        steps.extend(job.get("steps") or [])
    return steps


def _summary_script() -> str:
    """Return the shell source that composes and posts the wake summary.

    It lives in ``scripts/ralph/verdict-wake.sh`` rather than in the workflow's
    ``run:`` body, and that is the whole point of #1685: a ``run:`` body is
    never executed by a test, so grep was the only available defence and grep
    cannot tell a live line from a load-bearing one.

    Returns:
        The script's shell source.

    Raises:
        AssertionError: If the script is missing, which would make every
            assertion about it vacuous.
    """
    assert VERDICT_WAKE.is_file(), (
        f"{VERDICT_WAKE} does not exist. The wake summary's clearance chain "
        "has to live in an executable script, or its only possible defence is "
        "a grep over YAML that a decoy invocation walks straight past (#1685)"
    )
    return VERDICT_WAKE.read_text(encoding="utf-8")


def test_the_workflow_step_is_a_thin_call_to_the_extracted_script() -> None:
    """Extracting the logic and not wiring it up is half a delegation.

    Every behavioural case in ``scripts/ralph/test_verdict_wake.sh`` would pass
    against a script production never runs, and the workflow would go on
    clearing merges from its own inline copy — which is exactly the shape this
    module already exists to catch one level down.
    """
    runs = [
        str(step["run"])
        for step in _iteration_trigger_steps()
        if "run" in step and "verdict-wake.sh" in str(step["run"])
    ]
    assert len(runs) == 1, (
        f"expected exactly one step in {ITERATION_TRIGGER.name} that calls "
        f"scripts/ralph/verdict-wake.sh, found {len(runs)}. The wake logic was "
        "extracted so it could be executed by a test; a workflow that does not "
        "call it makes that suite assert about dead code (#1685)"
    )
    # …AND NO STEP MAY STILL DO THE WORK ITSELF. A leftover inline selector is
    # worse than no extraction, because it looks extracted.
    leftovers = [
        str(step["run"])
        for step in _iteration_trigger_steps()
        if "run" in step
        and re.search(r"verdict-select\.jq|check-runs|gh pr comment", str(step["run"]))
    ]
    assert not leftovers, (
        "a step of iteration-trigger.yml still runs part of the wake logic "
        f"inline instead of delegating to verdict-wake.sh: {leftovers!r}"
    )


def _live_lines(script: str) -> list[str]:
    """Return a shell script's non-comment lines.

    ``iteration-trigger.yml`` habitually quotes the very expression it runs
    inside an explanatory comment, so a naive substring scan asserts against
    prose rather than against the code that executes.

    Args:
        script: The shell source.

    Returns:
        Lines that are not shell comments, stripped.
    """
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _command_lines(script: str) -> list[str]:
    """Return the live lines that are commands rather than log statements.

    Stripping comments is not enough. ``iteration-trigger.yml`` prints the
    filter's path in an operator-facing diagnostic::

        echo "::warning::… See scripts/ralph/lib/verdict-select.jq for the
              two refusal shapes."

    That is a LIVE line naming the filter while invoking nothing, and it alone
    satisfied every "is the shared filter referenced" assertion in this repo
    while the real ``-f scripts/ralph/lib/verdict-select.jq`` invocation was
    replaced by an inline four-line selector with no ``.body != null`` guard,
    no author allowlist and no edit check — #1266, #1199 and #1263 all reopened
    behind a fully green suite.

    Args:
        script: The shell source.

    Returns:
        Live lines that do not begin with ``echo``/``printf``.
    """
    return [
        line for line in _live_lines(script) if not re.match(r"(echo|printf)\s", line)
    ]


def test_the_shared_comment_query_requests_edit_provenance() -> None:
    """Neither consumer can answer #1263 from the payload it fetches today.

    ``gh pr view --json comments`` has no ``userContentEdits`` field and the
    REST issue-comments endpoint has no editor identity, so the query is not a
    refactor — it is the enabling change. Keeping it in one file is what stops
    the two paths drifting in what they FETCH, which is the precondition for
    them not drifting in what they CHECK.
    """
    assert COMMENTS_QUERY.is_file(), (
        f"{COMMENTS_QUERY} does not exist. Both clearance paths need edit "
        "provenance and neither payload they read today carries it (#1263)"
    )
    query = COMMENTS_QUERY.read_text(encoding="utf-8")
    assert "userContentEdits" in query, (
        "the shared query does not request `userContentEdits`, which is the "
        "only field carrying who actually wrote the body being trusted (#1263)"
    )
    assert re.search(r"editor\s*\{[^}]*login", query), (
        "the shared query requests edit history without the editor's `login`. "
        "'was this edited at all' is the naive check the issue rejects: it "
        "refuses the reviewer fixing their own typo, which wedges every lane"
    )
    assert "author" in query and "body" in query, (
        "the shared query must still return the author and body the existing "
        f"predicates read: {query!r}"
    )


@pytest.mark.parametrize(
    "consumer", [PR_READY, VERDICT_WAKE], ids=lambda p: Path(p).name
)
def test_both_clearance_paths_read_the_same_filter_file(consumer: Path) -> None:
    """Half a delegation looks fixed and keeps the defect.

    Parametrised so the two consumers fail independently: migrating one and
    not the other is the realistic mistake, and a combined assertion would
    report it as one ambiguous failure.
    """
    text = consumer.read_text(encoding="utf-8")
    assert _FILTER_PATH in text, (
        f"{consumer.name} does not reference {_FILTER_PATH}. It still holds "
        "its own copy of the verdict selector, so the null-body guard (#1266) "
        "and the edit-provenance check (#1263) live in one file and not the "
        "other — which is precisely how #1266 came to exist"
    )


def test_pr_ready_invokes_the_filter_in_code_not_in_a_comment() -> None:
    """``pr-ready.sh`` documents its own commands in prose constantly.

    The bytes have to be on a line that runs, or the assertion above is
    satisfied by a comment while the selector goes on unguarded — the same
    defence ``iteration-trigger.yml``'s own header already claims for its
    allowlist.
    """
    executable = [line for line in non_comment_lines(PR_READY) if _FILTER_PATH in line]
    assert executable, (
        f"{_FILTER_PATH} appears in {PR_READY.name} only inside comments; no "
        "line that executes actually invokes the shared filter (#1263)"
    )


#: How each consumer actually RUNS the shared filter. Per-file on purpose:
#: ``pr-ready.sh`` splices it into ``gh --jq`` so the API answer and its parse
#: stay in one process under the production regex engine, while
#: ``iteration-trigger.yml`` has already written the payload to disk and runs
#: system ``jq -f`` over it. One pattern loose enough to match both would stop
#: discriminating, which is the whole failure this guards against.
_INVOCATIONS = {
    PR_READY: re.compile(r'--jq.*cat "\$VERDICT_FILTER"'),
    VERDICT_WAKE: re.compile(r'-f "\$VERDICT_FILTER"'),
}


@pytest.mark.parametrize(
    "consumer", [PR_READY, VERDICT_WAKE], ids=lambda p: Path(p).name
)
def test_both_clearance_paths_actually_invoke_the_shared_filter(
    consumer: Path,
) -> None:
    """Naming the filter is not running it, and only one of those is a gate.

    The sibling assertion above is satisfied by any mention on a line that is
    not a comment — including ``iteration-trigger.yml``'s ``::warning::``
    diagnostic, which prints the filter's path to tell an operator where the
    two refusal shapes are documented. MEASURED: swapping the real
    ``-f scripts/ralph/lib/verdict-select.jq comments.json`` for an inline
    selector emitting a hard-coded clearance left that echo in place and the
    entire suite green.
    """
    script = (
        _summary_script()
        if consumer == VERDICT_WAKE
        else "\n".join(non_comment_lines(consumer))
    )
    pattern = _INVOCATIONS[consumer]
    invocations = [line for line in _command_lines(script) if pattern.search(line)]
    assert invocations, (
        f"{consumer.name} never invokes {_FILTER_PATH}: no command line "
        f"matches {pattern.pattern!r}. A log line that NAMES the filter "
        "satisfies a substring grep while running nothing, and an inline "
        "selector hidden behind one reopens #1266, #1199 and #1263 at once"
    )


def test_iteration_trigger_keeps_no_inline_verdict_selector() -> None:
    """A leftover copy is worse than no migration: it looks migrated.

    The inline selector is the site of #1266 — ``.body`` fed straight to
    ``test()`` with no null guard, under this step's ``set -euo pipefail``, so
    one null-bodied comment aborts the step and the lane's wake is lost on
    every subsequent CI completion.
    """
    script = _summary_script()
    offenders = [
        line for line in _live_lines(script) if ".body" in line and "test(" in line
    ]
    assert not offenders, (
        "the wake step still applies its own `.body | test(...)` selector "
        "instead of the shared filter. That inline copy is where #1266 lives, "
        f"and a second copy is how the guard goes missing again: {offenders!r}"
    )
    # …AND A MULTI-LINE jq PROGRAM EVADES A PER-LINE CONJUNCTION. `.body` and
    # `test(` sitting on separate lines of one `jq -c '…'` heredoc satisfies the
    # check above while being exactly the inline selector it forbids. Regex
    # testing is the shared filter's job in its entirety, so no live line in
    # this step may call `test(` at all.
    testers = [line for line in _live_lines(script) if "test(" in line]
    assert not testers, (
        "the wake step calls jq's `test()` itself. Every regex decision about "
        "a verdict comment belongs to scripts/ralph/lib/verdict-select.jq; a "
        "second copy here is how #1266's null-body guard came to exist in one "
        f"clearance path and not the other: {testers!r}"
    )


def test_iteration_trigger_no_longer_reads_the_rest_comment_payload() -> None:
    """The REST payload cannot carry edit provenance, so it has to go.

    It is also the source of the ``.user.login`` / ``github-actions[bot]``
    spelling divergence this file spends forty lines warning about. Moving both
    consumers onto one payload deletes the trap rather than documenting it.
    """
    script = _summary_script()
    offenders = [
        line
        for line in _live_lines(script)
        if re.search(r"issues/\$?\{?\w*PR\w*\}?/comments", line)
    ]
    assert not offenders, (
        "the wake step still fetches comments over the REST issue-comments "
        "endpoint, which exposes neither editor identity nor isMinimized. "
        "Parity with pr-ready.sh is unreachable from that payload by any "
        f"tightening of the jq (#1263): {offenders!r}"
    )


def test_iteration_trigger_checks_out_the_repository_safely() -> None:
    """Sharing a repo file with this workflow means checking one out.

    This workflow holds ``GEOFFE_GA_PAT``, so the checkout it grows is new
    attack surface and its mitigations are load-bearing, not hygiene. On a
    ``workflow_run`` trigger checkout resolves the DEFAULT BRANCH, never the PR
    head — which is exactly why a fork PR cannot supply its own
    ``verdict-select.jq`` and clear its own merge. Pinning ``ref:`` to
    ``workflow_run.head_branch`` would hand that away.
    """
    checkouts = [
        step
        for step in _iteration_trigger_steps()
        if "actions/checkout" in str(step.get("uses", ""))
    ]
    assert len(checkouts) == 1, (
        f"expected exactly one actions/checkout step in "
        f"{ITERATION_TRIGGER.name} (the shared filter has to be on disk to be "
        f"read), found {len(checkouts)}"
    )
    with_block = checkouts[0].get("with") or {}
    assert with_block.get("persist-credentials") is False, (
        "the checkout must set `persist-credentials: false`; this workflow "
        "runs with a PAT and has no reason to leave it in the git config"
    )
    ref = str(with_block.get("ref", ""))
    assert "head_branch" not in ref, (
        f"the checkout pins ref={ref!r}, which resolves to the PR's own "
        "branch. A fork PR could then supply its own verdict-select.jq and "
        "clear its own merge. Leave `ref` unset so workflow_run's default "
        "(the base repo's default branch) applies"
    )


def test_the_wake_step_has_no_dead_comment_id_extraction() -> None:
    """A dead read is where a payload-shape change hides.

    ``ID=$(jq -r '.id' …)`` has zero references in the file. Under REST that
    ``.id`` is a numeric database id; under GraphQL it is an opaque node id.
    Carrying an unused assignment across the swap leaves a line whose meaning
    silently changed and whose wrongness nothing can detect.
    """
    script = _summary_script()
    assigns_id = [line for line in _live_lines(script) if re.match(r"ID=\s*\$\(", line)]
    if not assigns_id:
        return
    uses_id = [
        line
        for line in _live_lines(script)
        if re.search(r"\$\{?ID\}?\b", line) and not line.startswith("ID=")
    ]
    assert uses_id, (
        f"{ITERATION_TRIGGER.name} assigns ID but never reads it "
        f"({assigns_id!r}). Delete it rather than carrying a dead extraction "
        "through the REST-to-GraphQL swap, where `.id` changes meaning"
    )


@pytest.mark.parametrize("skill", _CLEARANCE_SKILLS, ids=lambda p: p.parent.name)
def test_the_agent_clearance_skills_name_the_edit_check(skill: Path) -> None:
    """The third path, and the one that wins a disagreement.

    ``await-claude-review`` Step 3 says its summary short-circuits per-event
    classification, so fixing the two shell paths and leaving these alone moves
    the hole one file over — the exact criticism #1199 was raised to answer.

    The API field is named rather than the rule paraphrased, because these are
    executed by an agent: a prose rule with no field to read is not a rule.
    """
    text = skill.read_text(encoding="utf-8")
    assert "userContentEdits" in text, (
        f"{skill.parent.name}/SKILL.md matches the verdict comment's author "
        "but never checks who last wrote its body. An account with write "
        "access can retype a CHANGES_REQUESTED as an LGTM and this path — the "
        "one that wins when it and pr-ready.sh disagree — clears the merge "
        "(#1263)"
    )


@pytest.mark.parametrize(
    "consumer", [PR_READY, VERDICT_WAKE], ids=lambda p: Path(p).name
)
def test_neither_consumer_reshapes_the_comment_payload(consumer: Path) -> None:
    """A reshape is a place to lose a field, and there were two of them (#1685).

    ``pr-ready.sh``'s ``$ENV`` prelude ended with
    ``{comments: (.data.repository.pullRequest.comments.nodes // [])}`` and
    ``iteration-trigger.yml`` carried a byte-parallel copy in a ``--jq`` of its
    own: two hand-mirrored projections feeding one shared selector, which is
    precisely the shape #1266 came out of.

    Neither was reachable from ``test_verdict_select_filter.py``, because every
    case there feeds the filter a hand-built ``{"comments": …}`` envelope and so
    runs past both preludes. MEASURED: narrowing ``pr-ready.sh``'s projection to
    ``{body, createdAt, author}`` strips ``userContentEdits``, reopens #1263 in
    full — the tampered verdict clears the merge — and leaves 362 shell plus 21
    pytest cases green.

    The filter takes the raw answer itself now, so there is ONE intake and it
    lives in the program that reads the fields.
    """
    script = (
        _summary_script()
        if consumer == VERDICT_WAKE
        else "\n".join(non_comment_lines(consumer))
    )
    offenders = [
        line for line in _command_lines(script) if re.search(r"\{\s*comments\s*:", line)
    ]
    assert not offenders, (
        f"{consumer.name} projects the GraphQL answer down to a "
        "`{comments: …}` envelope before the shared filter sees it. That "
        "projection is untestable from where the filter's own tests stand — "
        "narrow it to `{body, createdAt, author}` and #1263 reopens behind a "
        f"green suite. Let verdict-select.jq do the intake: {offenders!r}"
    )


def test_the_wake_step_addresses_the_comment_by_database_id() -> None:
    """``createdAt`` is second-granular, so it does not identify a comment.

    The wake step re-found the selected comment with
    ``[.comments[] | select(.createdAt == $ts)] | last`` over the UNFILTERED
    list. Two comments landing in the same second are ordinary on an active PR,
    and ``last`` can therefore answer with a comment the shared filter REFUSED
    — mis-attributing the "pull comment N" id and reading the DISPLAYED verdict
    off a body the gate rejected. That contradicts the filter's own doctrine:
    every field is read off the one selected comment.
    """
    script = _summary_script()
    lines = _command_lines(script)
    by_stamp = [line for line in lines if "createdAt" in line and "select(" in line]
    assert not by_stamp, (
        "the wake step still re-selects the verdict comment by `createdAt`, "
        "which GitHub reports only to the second. Address it by the "
        f"`databaseId` the shared filter returns: {by_stamp!r}"
    )
    by_id = [line for line in lines if "databaseId" in line and "select(" in line]
    assert by_id, (
        "the wake step never addresses the selected comment by `databaseId`. "
        "The id it prints as 'pull comment N to see in-depth feedback' must "
        "name the comment the gate ADMITTED, and must stay the numeric "
        "databaseId rather than the opaque GraphQL node id"
    )


def _connection_arguments(query: str, field: str) -> dict[str, str]:
    """Parse one GraphQL connection's arguments into ``{name: value}``.

    Comment lines are stripped FIRST. The query file's own prose discusses both
    ``last: 100`` and ``first: 100`` at length — it has to, since the choice is
    the non-obvious part — so a scan that reads the explanation instead of the
    code would pass on a query that says the opposite of its comment.

    Args:
        query: The GraphQL document.
        field: The connection field name, e.g. ``comments``.

    Returns:
        The field's arguments, values as written.

    Raises:
        AssertionError: If the field is absent or takes no arguments, which
            would make the caller's assertion vacuous.
    """
    code = "\n".join(
        line for line in query.splitlines() if not line.lstrip().startswith("#")
    )
    match = re.search(rf"(?<![A-Za-z_]){re.escape(field)}\s*\(([^)]*)\)", code)
    assert match is not None, (
        f"the shared query has no `{field}(…)` connection with arguments. "
        "Its pagination is the only thing standing between the selectors and a "
        "payload that does not contain the verdict at all"
    )
    arguments: dict[str, str] = {}
    for pair in match.group(1).split(","):
        name, _, value = pair.partition(":")
        if name.strip():
            arguments[name.strip()] = value.strip()
    return arguments


def test_the_comment_window_is_paginated_from_the_end_of_the_thread() -> None:
    """``first: 100`` fetches the OLDEST hundred, which is the silent unmark.

    An active PR's verdict is at the END of its thread and both selectors take
    the LAST admissible comment, so a window opened from the front simply does
    not contain the verdict on any busy PR. Every lane then reads "no verdict
    posted yet" — forever, fleet-wide, with nothing anywhere saying why, which
    is the worse of the two polarities this pipeline has to hold at once.

    PARSED, NOT GREPPED. Every fixture in every suite in this repo is
    hand-built JSON that never reaches the query, so nothing else can see this
    mutation: `comments(first: 100)` leaves 374 shell and 55 pytest cases green
    (#1685).
    """
    query = COMMENTS_QUERY.read_text(encoding="utf-8")
    arguments = _connection_arguments(query, "comments")
    assert "first" not in arguments, (
        "the shared query pages the comment thread with "
        f"`first: {arguments.get('first')}`. That fetches the OLDEST comments; "
        "the verdict is at the END of an active thread and both selectors take "
        "the LAST admissible comment, so on a busy PR the verdict is not in the "
        "payload at all and every lane unmarks silently"
    )
    assert arguments.get("last") == "100", (
        "the shared query must page the comment thread with `last: 100` — from "
        f"the END, one hundred deep — not {arguments!r}. A shallower window "
        "loses the verdict under a burst of CI summaries; a window opened from "
        "the front never contains it"
    )


def test_the_edit_history_window_is_not_truncated() -> None:
    """``first: 1`` shows only the ORIGINAL revision, so #1263 reopens.

    Edit history is append-only and the filter tests EVERY revision with
    ``all`` for exactly that reason: an attacker rewrites the body and the
    author then edits again for any innocent reason, at which point a truncated
    window shows only revisions that predate the tampering and the forged LGTM
    is waved through. The failure is invisible — a clearance, not an error.

    Parsed rather than grepped, for the same reason as the sibling above: the
    query is reached by no fixture anywhere, so its arguments have no
    behavioural coverage and this assertion is the whole gate.
    """
    query = COMMENTS_QUERY.read_text(encoding="utf-8")
    arguments = _connection_arguments(query, "userContentEdits")
    assert "last" not in arguments, (
        "the shared query pages edit history with "
        f"`last: {arguments.get('last')}`. `all` over a tail-only window still "
        "misses an early foreign edit that a later self-edit pushed out of it"
    )
    assert arguments.get("first") == "100", (
        "the shared query must request `userContentEdits(first: 100)`, not "
        f"{arguments!r}. Anything smaller truncates an append-only history to "
        "its OLDEST revisions, so a foreign edit that is not the first one is "
        "invisible and the tampered verdict clears the merge (#1263)"
    )
    # …AND THE FILTER MUST ACTUALLY SPEND THAT WINDOW. A hundred revisions buy
    # nothing if the selector only inspects the newest one; the two halves of
    # this guard are in different files and neither is checkable from the other.
    filter_text = "\n".join(non_comment_lines(VERDICT_FILTER))
    assert "all(" in filter_text, (
        "the shared filter no longer tests the edit list with `all`. A "
        "`last`-only check waves through a body an attacker rewrote before the "
        "author's own most recent edit (#1263)"
    )


def test_the_shared_filter_reads_the_fetch_answer_untransformed() -> None:
    """A reshape is a place to lose a field, whatever it is spelled as.

    The sibling above forbids the ``{comments: …}`` projection by its literal,
    and that literal is exactly what a mutant avoids: writing the projection IN
    PLACE — a ``map()`` over the nodes keeping ``body``, ``createdAt``,
    ``author`` and ``databaseId`` — strips ``userContentEdits`` while
    preserving the envelope the filter parses, and leaves every suite green
    (#1685).

    So the structural claim is stronger than "no projection is spelled this
    way": the file the filter reads must be the file the fetch WROTE, written
    once and modified by nothing in between. The behavioural pin lives in
    ``scripts/ralph/test_verdict_wake.sh``, whose foreign-editor case flips from
    a refusal to a clearance under any reshape at all; this is the cheap second
    line that names the drift.
    """
    lines = _command_lines(_summary_script())
    writers = [line for line in lines if re.search(r"(?<!>)>\s*comments\.json\b", line)]
    assert len(writers) == 1, (
        f"expected exactly one live line to write comments.json, found "
        f"{writers!r}. A second writer is a reshape with an extra step"
    )
    rewriters = [
        line
        for line in lines
        if "comments.json" in line and re.search(r"\b(mv|cp|sponge|tee)\b", line)
    ]
    assert not rewriters, (
        "a live line replaces comments.json after it was fetched, which is a "
        f"reshape however it is spelled: {rewriters!r}"
    )
    readers = [line for line in lines if "$VERDICT_FILTER" in line]
    assert readers, (
        "no live line invokes the shared filter by its resolved path; the "
        "delegation assertions above are satisfied by a mention"
    )
