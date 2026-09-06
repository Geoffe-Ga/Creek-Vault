"""Every third-party GitHub Action must run at an immutable commit SHA.

Issue #1380. A `uses: owner/action@v7` ref is a *moving* pointer: whoever
controls that tag can change the code it resolves to at any time. Several of
this repo's workflows run with `contents: write` and repository secrets in
scope, so a mutable ref is a standing supply-chain hole under a name the repo
already trusts.

The convention predates this test -- 21 refs were already SHA-pinned with a
trailing `# vX.Y.Z` comment -- and #1380 finished the other 32. This module is
what stops the next added step reopening the gap: the value of pinning is
uniformity, and a convention applied by memory is applied unevenly.

**Two things this checks that a naive version would miss.**

*Job-level `uses:`.* `tests/shell_command_support.py`'s workflow helpers walk
`job["steps"]`, and their docstring notes that a job delegating to a reusable
workflow is skipped entirely. A job-level `uses:` is therefore invisible to
them, so this module walks the raw YAML instead.

*What "pinned" means.* Classification is "the part after `@` is exactly 40 hex
characters", never a guess from the ref's shape. A pattern like `@v?[0-9]`
matches `actions/checkout@3d3c42e5...` because that SHA happens to start with a
digit -- which is not hypothetical: it is what the first draft of this check
did, reporting 45 mutable refs where there were 32.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from tests.shell_command_support import WORKFLOWS_DIR, load_yaml

if TYPE_CHECKING:
    from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{40}$")

#: A trailing ``# vX.Y.Z`` so the version stays greppable once the ref is a
#: SHA. Without it, "which version are we on" needs a network round trip.
_VERSION_COMMENT = re.compile(r"#\s*v\d+\.\d+(\.\d+)?")


def _workflow_files() -> list[Path]:
    """Return every workflow file in the repository.

    Returns:
        Sorted ``.yml`` / ``.yaml`` paths under ``.github/workflows``.
    """
    return sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])


def _uses_refs() -> list[tuple[Path, str, str]]:
    """Return every ``uses:`` reference, from both step and job level.

    Walks the parsed YAML rather than the step helpers in
    ``shell_command_support``: those iterate ``job["steps"]`` only, so a job
    that delegates to a reusable workflow -- which is a ``uses:`` at job level
    -- never reaches them.

    Returns:
        ``(file, location, ref)`` triples, where *location* names the job or
        step for a legible failure.
    """
    found: list[tuple[Path, str, str]] = []
    for path in _workflow_files():
        document: dict[str, Any] = load_yaml(path)
        jobs = document.get("jobs") or {}
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if isinstance(job.get("uses"), str):
                found.append((path, f"job {job_name}", job["uses"]))
            for index, step in enumerate(job.get("steps") or []):
                if isinstance(step, dict) and isinstance(step.get("uses"), str):
                    found.append((path, f"job {job_name} step {index}", step["uses"]))
    return found


def _third_party_refs() -> list[tuple[Path, str, str]]:
    """Return the ``uses:`` refs that a SHA pin actually applies to.

    Local refs (``./.github/workflows/_claude-scan.yml``) are excluded: they
    resolve inside this repository at the commit already being run, so they
    are immutable by construction and carry no ``@`` to pin. Docker refs
    (``docker://``) are excluded for the same reason -- their immutability is a
    digest question, not a git-ref one, and none are used here.

    Returns:
        ``(file, location, ref)`` triples naming a third-party action.
    """
    return [
        (path, where, ref)
        for path, where, ref in _uses_refs()
        if not ref.startswith(("./", ".\\", "docker://"))
    ]


def test_the_workflow_corpus_is_not_empty() -> None:
    """A loud precondition: an empty walk would pass every check below.

    Move or rename the workflow directory and the assertions in this module
    become vacuously true, which is the standing false-green shape on this
    repository.
    """
    files = _workflow_files()
    assert len(files) >= 5, f"only {len(files)} workflow files found: {files}"

    refs = _third_party_refs()
    assert len(refs) >= 40, (
        f"only {len(refs)} third-party `uses:` refs found; the walk is not "
        "reaching the workflows it is supposed to check"
    )


def test_every_third_party_action_is_pinned_to_a_commit_sha() -> None:
    """No workflow may run a third-party action at a moving ref (#1380).

    This is the assertion the issue turns on. A tag can be repointed by
    whoever owns it, and several of these workflows hold ``contents: write``
    plus repository secrets.
    """
    unpinned = [
        (path.name, where, ref)
        for path, where, ref in _third_party_refs()
        if "@" not in ref or not _SHA.match(ref.rsplit("@", maxsplit=1)[1])
    ]
    assert not unpinned, (
        "these actions run at a mutable ref, so the code they execute can "
        f"change without any commit to this repository: {unpinned}"
    )


def test_every_pin_keeps_its_version_legible() -> None:
    """A bare SHA is unreadable; the trailing ``# vX.Y.Z`` is the remedy.

    Pinning trades legibility for safety, and the comment buys the legibility
    back -- without it, answering "which version of checkout do we run" needs a
    network round trip per ref. It is also what makes an upgrade reviewable.
    """
    missing = []
    for path in _workflow_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped.startswith(("uses:", "- uses:")):
                continue
            ref = stripped.split("uses:", maxsplit=1)[1].split("#")[0].strip()
            if ref.startswith(("./", ".\\", "docker://")) or "@" not in ref:
                continue
            pinned = _SHA.match(ref.rsplit("@", maxsplit=1)[1])
            if pinned and not _VERSION_COMMENT.search(line):
                missing.append(f"{path.name}:{number}")
    assert not missing, (
        "these SHA pins carry no `# vX.Y.Z` comment, so the version they "
        f"represent is not greppable: {missing}"
    )


@pytest.mark.parametrize(
    ("ref", "pinned"),
    [
        ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", True),
        ("actions/checkout@v7", False),
        ("actions/checkout@v7.0.1", False),
        ("actions/checkout@main", False),
        # The exact false positive that made the first draft of this check
        # report 45 mutable refs instead of 32: a SHA beginning with a digit.
        ("actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9", True),
        # Too short, and uppercase hex -- neither is a valid pin.
        ("owner/action@3d3c42e5", False),
        ("owner/action@3D3C42E5AAC5BA805825DA76410C181273BA90B1", False),
    ],
)
def test_the_pinned_predicate_reads_each_ref_shape(ref: str, pinned: bool) -> None:
    """The classifier must key on 40-hex, not on what the ref looks like.

    A predicate that over-matches would wave through a mutable ref; one that
    under-matches would fail a correct pin and train someone to loosen it.

    Args:
        ref: The ``uses:`` reference under test.
        pinned: Whether it should count as SHA-pinned.
    """
    assert bool(_SHA.match(ref.rsplit("@", maxsplit=1)[1])) is pinned
