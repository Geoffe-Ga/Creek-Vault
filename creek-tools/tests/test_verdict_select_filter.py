"""The verdict selector must refuse a body that is no longer its author's.

Issues #1263 and #1266, which are the same defect twice over: two hand-parallel
copies of one jq filter, one in ``scripts/ralph/pr-ready.sh`` and one in
``.github/workflows/iteration-trigger.yml``, each deciding whether a PR is
cleared to merge. This module tests the shared filter both paths must consume,
``scripts/ralph/lib/verdict-select.jq``.

**#1263 — the selector trusts a body it never checked the provenance of.** It
filters on the comment's regex shape and its ``author.login`` and nothing else.
An account with write/triage access can open ``github-actions[bot]``'s genuine
``## Verdict: CHANGES_REQUESTED`` and rewrite the body to ``## Verdict: LGTM``.
The author is untouched, ``createdAt`` is untouched, and #1181's
``<!-- creek-review pr=N -->`` marker rides along inside the same body — so the
author allowlist, the currency stamp and the provenance marker all pass, and
both clearance paths clear. GraphQL's ``userContentEdits`` carries the editor's
identity and is verified reachable with the ordinary token.

**#1266 — one null body aborts the whole step.** ``iteration-trigger.yml`` feeds
``.body`` straight to ``test()`` with no ``!= null`` conjunct, under
``set -euo pipefail``. Measured at jq-1.7.1: unguarded gives
``null (null) cannot be matched, as it is not a string`` and exit 5. The author
conjunct #1199 added sits to the RIGHT of the body test and jq's ``and``
short-circuits left to right, which is exactly why it does not rescue it. The
guard must be the FIRST conjunct.

**The two polarities this file has to hold apart.** Refusing too little clears a
forged merge. Refusing too much is worse in a different way: it unmarks every
lane in the fleet at once, silently, because a filter that matches nothing looks
identical to "no verdict has been posted yet". So a self-edit must still be
admitted (an attacker holding the reviewer account can post a fresh LGTM
anyway, so refusing there buys nothing and costs the fleet), an unedited comment
must be admitted unchanged, and ``all`` over an empty edit list must stay
vacuously true.

THE FILTER CONTRACT, stated once so both consumers and this suite agree:

* input   ``{"comments": [ … ]}`` — the shape ``gh pr view --json comments``
  already produced, so existing fixtures stay valid after the GraphQL swap.
* args    ``--argjson authors`` plus ``--arg`` for ``verdict_re``,
  ``verdict_lgtm_re``, ``iter_summary_re``, ``marker_re``, ``marker_any_re``
  and ``marker_malformed``.
* output  one ``jq -r`` line, five ``|``-separated fields:
  ``createdAt|lgtm|marker|refused|databaseId``. An empty first field means
  nothing was selected. The field count is load-bearing — ``pr-ready.sh``
  splits on it and blanks the whole answer on a surplus field.

**A trap worth naming, because it fails silently in the safe-looking
direction.** ``pr-ready.sh`` currently interpolates its regex constants into a
jq *string literal*, so they are written with doubled backslashes (``\\\\s``)
that jq's own string parser collapses. ``--arg`` does no such parsing. Passing
today's constants verbatim to ``-f`` yields a regex matching a literal backslash
followed by ``s``, which selects NOTHING — exit 0, no error, every lane in the
fleet reading ``awaiting-review`` forever.
:func:`test_the_filter_matches_with_the_regexes_pr_ready_actually_defines` is
the only thing standing between that and a green suite.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.shell_command_support import RALPH_SCRIPTS_DIR, REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

VERDICT_FILTER = RALPH_SCRIPTS_DIR / "lib" / "verdict-select.jq"
PR_READY = RALPH_SCRIPTS_DIR / "pr-ready.sh"

#: The allowlist ``pr-ready.sh`` holds in ``VERDICT_AUTHORS_JQ``. The GraphQL
#: payload spells the bot bare (``github-actions``), not ``github-actions[bot]``.
_AUTHORS = ["Geoffe-Ga", "github-actions"]

#: The regexes in their *effective* form — one backslash, because ``--arg``
#: performs no jq string-literal unescaping. See the module docstring.
_VERDICT_RE = r"(?im)^\s*(?:#{1,6}\s+|\*\*)?verdict[:*\s]"
_VERDICT_LGTM_RE = _VERDICT_RE + "+lgtm"
_ITER_SUMMARY_RE = r"(?m)^<!-- iteration-trigger -->[[:space:]]*$"
_MARKER_RE = r"(?m)^<!-- creek-review pr=([0-9]+) -->[[:space:]]*$"
_MARKER_ANY_RE = "creek-review"
_MARKER_MALFORMED = "malformed"

#: A genuine reviewer verdict, marker and all.
_LGTM_BODY = "<!-- creek-review pr=100 -->\n## Verdict: LGTM\n"

_STAMP = "2026-08-30T10:00:00Z"

#: The selected comment's numeric id. Distinct per fixture where a test needs
#: to tell two same-second comments apart.
_DB_ID = 908070


def _comment(
    *,
    body: str | None = _LGTM_BODY,
    author: str = "Geoffe-Ga",
    editors: Sequence[str | None] | None = None,
    created_at: str = _STAMP,
    database_id: int = _DB_ID,
) -> dict[str, Any]:
    """Build one comment node in the shape the shared query returns.

    Args:
        body: The comment body, or ``None`` to model a deleted/absent body.
        author: The login that posted it.
        editors: One login per revision, oldest first. ``None`` inside the
            sequence models a deleted editor account (GraphQL returns
            ``editor: null``). Omit the argument entirely to model a comment
            that was never edited — the shape every existing fixture has.
        created_at: The comment's RFC3339 creation stamp.
        database_id: The numeric comment id GitHub's own URLs use.

    Returns:
        The comment mapping.
    """
    node: dict[str, Any] = {
        "databaseId": database_id,
        "body": body,
        "createdAt": created_at,
        "author": {"login": author},
    }
    if editors is not None:
        node["userContentEdits"] = {
            "nodes": [
                {"editor": None if login is None else {"login": login}}
                for login in editors
            ]
        }
    return node


def _graphql_envelope(comments: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap comment nodes the way ``pr-comments.graphql`` really answers.

    Args:
        comments: The comment nodes.

    Returns:
        The raw ``gh api graphql`` response body.
    """
    return {"data": {"repository": {"pullRequest": {"comments": {"nodes": comments}}}}}


def _run_filter(
    tmp_path: Path,
    comments: list[dict[str, Any]],
    *,
    verdict_re: str = _VERDICT_RE,
    verdict_lgtm_re: str = _VERDICT_LGTM_RE,
    raw_graphql: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the shared filter over ``comments`` from the repository root.

    Invoked from ``REPO_ROOT`` on purpose: the filter is referenced by a
    repo-relative path in both consumers, and a gate exercised from the wrong
    working directory has reported a real symbol as absent in this repo before.

    Args:
        tmp_path: pytest temporary directory for the fixture file.
        comments: The comment nodes to feed the filter.
        verdict_re: Override for the verdict-shape regex.
        verdict_lgtm_re: Override for the LGTM regex.
        raw_graphql: Feed the raw ``gh api graphql`` response body instead of
            the legacy ``{"comments": …}`` envelope. Both consumers store the
            raw answer now, so this is the shape production really parses.

    Returns:
        The completed ``jq`` process.
    """
    fixture = tmp_path / "comments.json"
    payload = _graphql_envelope(comments) if raw_graphql else {"comments": comments}
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            "jq",
            "-r",
            "--argjson",
            "authors",
            json.dumps(_AUTHORS),
            "--arg",
            "verdict_re",
            verdict_re,
            "--arg",
            "verdict_lgtm_re",
            verdict_lgtm_re,
            "--arg",
            "iter_summary_re",
            _ITER_SUMMARY_RE,
            "--arg",
            "marker_re",
            _MARKER_RE,
            "--arg",
            "marker_any_re",
            _MARKER_ANY_RE,
            "--arg",
            "marker_malformed",
            _MARKER_MALFORMED,
            "-f",
            str(VERDICT_FILTER.relative_to(REPO_ROOT)),
            str(fixture),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


def _answer(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Return the filter's five fields, failing loudly on any other shape.

    Args:
        result: The completed ``jq`` process.

    Returns:
        ``[createdAt, lgtm, marker, refused, databaseId]``.

    Raises:
        AssertionError: If ``jq`` errored or produced a different field count.
            Both are how this contract breaks silently: a non-zero ``jq`` under
            the consumers' ``set -euo pipefail`` aborts the step, and a surplus
            field shifts every value one place along.
    """
    assert result.returncode == 0, (
        f"jq exited {result.returncode}. Under both consumers' "
        "`set -euo pipefail` that aborts the step outright — no verdict is "
        f"read and no wake is posted. stderr: {result.stderr!r}"
    )
    line = result.stdout.strip("\n")
    fields = line.split("|")
    assert len(fields) == 5, (
        f"the filter answered {line!r} — {len(fields)} field(s), expected 5 "
        "(createdAt|lgtm|marker|refused|databaseId). pr-ready.sh splits this "
        "answer by field count and blanks the whole thing on a surplus field"
    )
    return fields


def _shell_constant(name: str) -> str:
    """Return a ``readonly NAME=…`` value from ``pr-ready.sh``.

    Args:
        name: The constant's shell name.

    Returns:
        The literal value, with a ``${VERDICT_RE}`` reference resolved.

    Raises:
        AssertionError: If the constant is not declared.
    """
    text = PR_READY.read_text(encoding="utf-8")
    match = re.search(rf"^readonly {name}=(['\"])(.*?)\1\s*$", text, re.MULTILINE)
    assert match is not None, (
        f"scripts/ralph/pr-ready.sh no longer declares `readonly {name}=`; "
        "this test resolves the regexes the production path actually passes"
    )
    value = match.group(2)
    if name != "VERDICT_RE":
        value = value.replace("${VERDICT_RE}", _shell_constant("VERDICT_RE"))
    return value


@pytest.fixture(autouse=True)
def _require_jq() -> None:
    """Skip loudly if ``jq`` is absent rather than passing silently."""
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; the verdict filter is a jq program")


def test_the_shared_filter_exists_as_one_file() -> None:
    """Both clearance paths must read the same bytes.

    Two hand-parallel copies is the shape that produced #1266 and #1263 at
    once: the guard was added to one file and not the other, and the ~40 lines
    of comments in the tree warning that the copies must stay parallel-but-not-
    identical are the smell, not the mitigation.
    """
    assert VERDICT_FILTER.is_file(), (
        f"{VERDICT_FILTER} does not exist. The verdict selector is duplicated "
        "between scripts/ralph/pr-ready.sh and "
        ".github/workflows/iteration-trigger.yml, which is how a null-body "
        "guard ended up in one and not the other (#1266) and how an edit "
        "provenance check would land in one and not the other (#1263)"
    )
    assert VERDICT_FILTER.read_text(encoding="utf-8").strip(), (
        f"{VERDICT_FILTER} is empty; `jq -f` on an empty program is the "
        "'gate that reports it did nothing' failure"
    )


def test_a_verdict_edited_by_another_account_is_not_selected(
    tmp_path: Path,
) -> None:
    """The #1263 attack, executable.

    An allowlisted author's genuine verdict, body rewritten by someone else.
    Everything the gate checks today still passes; only the edit history
    disagrees.
    """
    result = _run_filter(tmp_path, [_comment(editors=["mallory"])])
    created_at, lgtm, _marker, _refused, _database_id = _answer(result)
    assert created_at == "", (
        "a verdict whose body was rewritten by an account other than its "
        "author was selected. `mallory` can open the reviewer's "
        "CHANGES_REQUESTED, retype it as LGTM, and both clearance paths clear "
        f"the merge (#1263). answer: {result.stdout.strip()!r}"
    )
    assert lgtm != "true", (
        "the LGTM flag was set from a body its author did not write "
        f"(#1263): {result.stdout.strip()!r}"
    )


def test_the_refusal_names_the_account_that_edited_it(tmp_path: Path) -> None:
    """Filtering at selection makes an unmarked verdict invisible.

    That is the price of skipping rather than refusing, and it is exactly the
    correlated fleet-wide silence #1199's diagnostic exists to prevent: every
    lane reads ``awaiting-review`` — an in-flight token, so the watcher sleeps
    — with nothing anywhere saying why.
    """
    result = _run_filter(tmp_path, [_comment(editors=["mallory"])])
    _created_at, _lgtm, _marker, refused, _database_id = _answer(result)
    assert "mallory" in refused, (
        "the refusal carries no diagnostic naming the editor, so a forged or "
        "tampered verdict is skipped in total silence and the lane simply "
        f"waits forever (#1263, #1199). refused field: {refused!r}"
    )


def test_a_self_edit_is_still_admitted(tmp_path: Path) -> None:
    """The anti-unmark polarity, and the reason the naive fix is wrong.

    Refusing on ``includesCreatedEdit`` — "was this edited at all" — would
    reject the reviewer fixing their own typo, and an attacker holding that
    account can post a fresh LGTM anyway, so the refusal buys nothing there and
    costs a wedged lane. The casing differs on purpose: logins are unique
    case-insensitively and both sides must be folded.
    """
    result = _run_filter(tmp_path, [_comment(editors=["geoffe-ga"])])
    created_at, lgtm, marker, refused, _database_id = _answer(result)
    assert created_at == _STAMP, (
        "a verdict edited by its own author was refused. That is an "
        "uncorrelated fleet-wide unmark: every lane reads awaiting-review and "
        f"nothing merges (#1263). answer: {result.stdout.strip()!r}"
    )
    assert lgtm == "true", f"the LGTM flag was lost: {result.stdout.strip()!r}"
    assert marker == "100", (
        f"the #1181 provenance marker was lost: {result.stdout.strip()!r}"
    )
    assert refused == "", f"a self-edit produced a refusal diagnostic: {refused!r}"


def test_an_unedited_verdict_is_admitted_unchanged(tmp_path: Path) -> None:
    """The regression floor: the shape every existing fixture has.

    No ``userContentEdits`` key at all. ``all`` over an empty list is vacuously
    true, so the new conjunct must be a no-op here. Flipping ``all`` to ``any``
    reddens this and every other verdict case at once — which is the polarity
    coupling this gate needs.
    """
    result = _run_filter(tmp_path, [_comment()])
    created_at, lgtm, marker, refused, _database_id = _answer(result)
    assert created_at == _STAMP, (
        "an ordinary, never-edited verdict was not selected. This is the "
        "shape of every verdict comment in the repo's recent history, so this "
        f"is a total merge freeze. answer: {result.stdout.strip()!r}"
    )
    assert (lgtm, marker, refused) == ("true", "100", ""), (
        f"the answer's other fields drifted: {result.stdout.strip()!r}"
    )


def test_every_revision_is_checked_not_only_the_latest(tmp_path: Path) -> None:
    """A ``last``-only check passes every case above and still clears a forgery.

    Edit history is append-only: an attacker rewrites the body, then the
    author edits again for any reason. If only the newest revision is
    inspected the tampered text is already in the body and the check waves it
    through.
    """
    result = _run_filter(tmp_path, [_comment(editors=["mallory", "Geoffe-Ga"])])
    created_at, _lgtm, _marker, _refused, _database_id = _answer(result)
    assert created_at == "", (
        "a verdict whose history contains an edit by `mallory` was selected "
        "because a later revision was the author's own. The check must hold "
        f"over ALL revisions (#1263). answer: {result.stdout.strip()!r}"
    )


def test_a_deleted_editor_account_fails_closed(tmp_path: Path) -> None:
    """``editor: null`` must refuse, not admit, and must not throw.

    GraphQL returns a null editor for a deleted account. Defaulting it to the
    empty string makes it equal to no allowlisted author, which is the
    fail-closed answer; letting the null reach a string builtin instead aborts
    the step under ``set -euo pipefail``.
    """
    result = _run_filter(tmp_path, [_comment(editors=[None])])
    created_at, _lgtm, _marker, _refused, _database_id = _answer(result)
    assert created_at == "", (
        "a verdict edited by an unattributable account was selected. An edit "
        "whose author cannot be established must fail closed — a wait, never "
        f"a clearance (#1263). answer: {result.stdout.strip()!r}"
    )


def test_a_null_body_does_not_abort_and_the_real_verdict_still_wins(
    tmp_path: Path,
) -> None:
    """#1266, both halves.

    The first half is that the filter survives: measured at jq-1.7.1, feeding
    ``null`` to ``test()`` is ``null (null) cannot be matched, as it is not a
    string`` and exit 5, which under ``iteration-trigger.yml``'s
    ``set -euo pipefail`` aborts the step before any summary is composed — so
    the lane loses its wake, and loses it on every subsequent CI completion
    because the null-bodied comment stays in the first 100 forever.

    The second half is what stops the trivial regression
    ``select(.body != null and false)``: the surviving verdict must still be
    selected. That is acceptance criterion 1 word for word.
    """
    result = _run_filter(
        tmp_path,
        [
            _comment(body=None, created_at="2026-08-30T09:00:00Z"),
            _comment(),
        ],
    )
    assert result.returncode == 0, (
        "the filter errored on a null comment body. Under "
        "iteration-trigger.yml's `set -euo pipefail` that aborts the wake "
        f"step and the lane is never told anything (#1266). "
        f"stderr: {result.stderr!r}"
    )
    created_at, lgtm, marker, _refused, _database_id = _answer(result)
    assert (created_at, lgtm, marker) == (_STAMP, "true", "100"), (
        "surviving the null body is not enough — the genuine verdict beside "
        "it must still be selected, or the guard is a fleet-wide unmark "
        f"wearing a fix's clothes (#1266). answer: {result.stdout.strip()!r}"
    )


def test_only_null_bodies_selects_nothing_without_erroring(
    tmp_path: Path,
) -> None:
    """The degenerate input must be a wait, not a crash and not a clearance."""
    result = _run_filter(
        tmp_path, [_comment(body=None), _comment(body=None, author="mallory")]
    )
    assert result.returncode == 0, (
        f"the filter errored on an all-null-body payload: {result.stderr!r}"
    )
    created_at, lgtm, _marker, _refused, _database_id = _answer(result)
    assert created_at == "", (
        f"something was selected from null bodies: {result.stdout.strip()!r}"
    )
    assert lgtm != "true", (
        f"the LGTM flag was set with no verdict at all: {result.stdout.strip()!r}"
    )


def test_an_iteration_trigger_summary_is_never_read_as_a_verdict(
    tmp_path: Path,
) -> None:
    """The pre-existing exclusion must survive the extraction.

    ``iteration-trigger.yml`` quotes the verdict line back into its own summary
    comment. Without the exclusion the gate reads its own echo and the two
    clearance paths bootstrap each other.
    """
    echo = "<!-- iteration-trigger -->\n## Verdict: LGTM\n"
    result = _run_filter(tmp_path, [_comment(body=echo)])
    created_at, _lgtm, _marker, _refused, _database_id = _answer(result)
    assert created_at == "", (
        "the filter selected an iteration-trigger summary as though it were a "
        f"reviewer verdict: {result.stdout.strip()!r}"
    )


def test_the_filter_matches_with_the_regexes_pr_ready_actually_defines(
    tmp_path: Path,
) -> None:
    """The silent-unmark trap the ``-f`` move introduces.

    Today the regex constants are spliced into a jq *string literal*, so they
    are written with doubled backslashes that jq's string parser collapses.
    ``--arg`` does no such parsing. Handing today's ``VERDICT_RE`` verbatim to
    ``jq -f`` produces a regex matching a literal backslash followed by ``s``,
    which selects nothing — exit 0, no error, no diagnostic, and every lane in
    the fleet waiting on a verdict that was posted. Verified live at jq-1.7.1.

    So whatever ``pr-ready.sh`` declares must select a genuine verdict when
    passed to the shared filter the way ``--arg`` passes it.
    """
    result = _run_filter(
        tmp_path,
        [_comment()],
        verdict_re=_shell_constant("VERDICT_RE"),
        verdict_lgtm_re=_shell_constant("VERDICT_LGTM_RE"),
    )
    created_at, lgtm, _marker, _refused, _database_id = _answer(result)
    assert created_at == _STAMP, (
        "pr-ready.sh's VERDICT_RE does not match a plain `## Verdict: LGTM` "
        "when handed to the shared filter through `--arg`. `--arg` performs "
        "no jq string-literal unescaping, so a doubled backslash stays "
        "doubled and the regex matches nothing — silently, on every lane at "
        f"once. answer: {result.stdout.strip()!r}"
    )
    assert lgtm == "true", (
        "pr-ready.sh's VERDICT_LGTM_RE does not recognise an LGTM through "
        f"`--arg`: {result.stdout.strip()!r}"
    )


def test_the_last_verdict_line_decides_the_lgtm_flag(tmp_path: Path) -> None:
    """The final word, not "does any line say LGTM" (#1685).

    The flag used to be a WHOLE-BODY ``test(verdict_lgtm_re)``, and
    ``VERDICT_RE`` admits leading whitespace (``^\\s*``) — so an INDENTED quote
    of a verdict line counts as one. A review that quotes
    ``    ## Verdict: LGTM`` while itself concluding
    ``## Verdict: CHANGES_REQUESTED`` came back ``true``: ``pr-ready.sh`` prints
    ``ready`` and ``iteration-trigger.yml`` posts "You are cleared to squash
    merge". A PR that touches these files is exactly where such a body gets
    written.

    ``scripts/ralph/stats.py``'s ``normalize_verdict`` is the reference
    contract — it collects every verdict line and returns the LAST — and both
    consumers' comments already claimed parity with it.
    """
    body = (
        "<!-- creek-review pr=100 -->\n"
        "The reviewer under discussion posted:\n\n"
        "    ## Verdict: LGTM\n\n"
        "…which is the bug. Not merging.\n\n"
        "## Verdict: CHANGES_REQUESTED\n"
    )
    result = _run_filter(tmp_path, [_comment(body=body)])
    created_at, lgtm, marker, _refused, _database_id = _answer(result)
    assert created_at == _STAMP, (
        "the comment must still be SELECTED — refusing it outright would be a "
        f"fleet-wide unmark, not a fix. answer: {result.stdout.strip()!r}"
    )
    assert lgtm == "false", (
        "a quoted `## Verdict: LGTM` outranked the body's own closing "
        "`## Verdict: CHANGES_REQUESTED`. The LAST verdict line is the final "
        f"word (#1685). answer: {result.stdout.strip()!r}"
    )
    assert marker == "100", (
        f"the #1181 provenance marker was lost: {result.stdout.strip()!r}"
    )


def test_a_later_lgtm_line_still_wins(tmp_path: Path) -> None:
    """The other direction, so the fix is not "never LGTM".

    A reviewer who re-verdicts inside one comment ends on the token that
    counts. Inverting the rule would unmark every such lane silently, which is
    the worse of the two polarities this filter has to hold apart.
    """
    body = (
        "<!-- creek-review pr=100 -->\n"
        "## Verdict: CHANGES_REQUESTED\n\n"
        "Re-reviewed after the push:\n\n"
        "## Verdict: LGTM\n"
    )
    result = _run_filter(tmp_path, [_comment(body=body)])
    _created_at, lgtm, _marker, _refused, _database_id = _answer(result)
    assert lgtm == "true", (
        "a body whose LAST verdict line is LGTM was not read as an LGTM. The "
        "rule is last-wins, not never-LGTM — inverting it unmarks every lane "
        f"that re-verdicts in one comment. answer: {result.stdout.strip()!r}"
    )


def test_the_legacy_header_and_token_on_separate_lines_still_reads_lgtm(
    tmp_path: Path,
) -> None:
    """``[:*\\s]`` spans the newline, and the rebuilt slice must keep it.

    ``## Verdict\\nLGTM`` is a real historical shape (``stats.py`` pins it too).
    A last-line implementation that split the body on newlines would silently
    stop recognising it — a fleet-wide unmark in the safe-looking direction.
    """
    result = _run_filter(
        tmp_path, [_comment(body="<!-- creek-review pr=100 -->\n## Verdict\nLGTM\n")]
    )
    _created_at, lgtm, _marker, _refused, _database_id = _answer(result)
    assert lgtm == "true", (
        "the legacy `## Verdict` / `LGTM`-on-the-next-line shape stopped being "
        f"recognised: {result.stdout.strip()!r}"
    )


def test_the_answer_carries_the_selected_comments_database_id(
    tmp_path: Path,
) -> None:
    """So no consumer has to look the comment up again (#1685).

    ``iteration-trigger.yml`` re-found the selected comment by ``createdAt``,
    which GitHub reports to the SECOND. Two comments in the same second are
    ordinary on an active PR, and ``last`` over the UNFILTERED list can answer
    with one the filter REFUSED — mis-attributing the wake message's comment id
    and reading the displayed verdict off a body the gate rejected. The id must
    be the NUMERIC ``databaseId``, never the opaque GraphQL node id: it is
    printed for a human to follow into GitHub's own comment URLs.
    """
    refused = _comment(
        body="<!-- creek-review pr=100 -->\n## Verdict: LGTM\n",
        author="mallory",
        database_id=111111,
    )
    genuine = _comment(database_id=222222)
    result = _run_filter(tmp_path, [genuine, refused])
    created_at, _lgtm, _marker, _refused_field, database_id = _answer(result)
    assert created_at == _STAMP, (
        f"the genuine verdict was not selected: {result.stdout.strip()!r}"
    )
    assert database_id == "222222", (
        "the answer names the wrong comment. Both fixtures share a "
        "`createdAt`, so a consumer re-finding the comment by stamp would take "
        "the LAST one — `mallory`'s, which this filter refused. The id must "
        f"come off the selected comment (#1685). answer: {result.stdout.strip()!r}"
    )


def test_nothing_selected_still_answers_five_empty_shaped_fields(
    tmp_path: Path,
) -> None:
    """The wait shape must not grow or lose a field.

    ``pr-ready.sh`` blanks the WHOLE answer on a surplus field, so a filter
    that emitted four fields here and five elsewhere would fail closed in one
    branch and shift every value in the other.
    """
    result = _run_filter(tmp_path, [_comment(body="just a chat comment")])
    created_at, lgtm, marker, refused, database_id = _answer(result)
    assert (created_at, lgtm, marker, refused, database_id) == (
        "",
        "false",
        "",
        "",
        "",
    ), f"the no-selection answer changed shape: {result.stdout.strip()!r}"


def test_the_filter_reads_the_raw_graphql_answer_itself(tmp_path: Path) -> None:
    """One intake, inside the program that reads the fields (#1685).

    Both consumers used to project the GraphQL answer down to
    ``{comments: …}`` themselves — ``pr-ready.sh`` in its ``$ENV`` prelude,
    ``iteration-trigger.yml`` in a ``--jq`` of its own — two hand-parallel
    reshapes feeding one shared selector, which is the shape #1266 came out of.
    Neither was reachable from these tests, because every one of them feeds a
    hand-built envelope: narrowing either projection to
    ``{body, createdAt, author}`` strips ``userContentEdits``, reopens #1263 in
    full, and leaves the whole suite green.
    """
    result = _run_filter(tmp_path, [_comment()], raw_graphql=True)
    created_at, lgtm, marker, _refused, database_id = _answer(result)
    assert (created_at, lgtm, marker, database_id) == (
        _STAMP,
        "true",
        "100",
        str(_DB_ID),
    ), (
        "the filter could not read the raw `gh api graphql` answer. Both "
        "consumers now store it unprojected, so a filter that only understands "
        "the legacy envelope selects nothing on every lane at once: "
        f"{result.stdout.strip()!r}"
    )


def test_edit_provenance_survives_the_raw_graphql_intake(tmp_path: Path) -> None:
    """#1263, end to end through the bytes production parses.

    The refusal is only real if the field reaches the conjunct. A projection
    anywhere between ``gh`` and this program can delete ``userContentEdits``
    without any behavioural test noticing, which is exactly why the intake was
    moved into this file.
    """
    result = _run_filter(tmp_path, [_comment(editors=["mallory"])], raw_graphql=True)
    created_at, lgtm, _marker, refused, _database_id = _answer(result)
    assert created_at == "", (
        "a verdict whose body was rewritten by another account was selected "
        "when fed the RAW GraphQL answer. `userContentEdits` did not survive "
        f"the intake (#1263): {result.stdout.strip()!r}"
    )
    assert lgtm != "true", (
        f"the LGTM flag was set from a tampered body: {result.stdout.strip()!r}"
    )
    assert "mallory" in refused, (
        f"the refusal names nobody, so the skip is silent: {refused!r}"
    )
