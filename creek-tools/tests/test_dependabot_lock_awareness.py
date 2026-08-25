"""Guard tests for Dependabot's ``/creek-tools`` update entry (issue #1085).

Every CI job in this repository provisions with ``uv export --locked``
(``ci.yml``, ``deslop.yml``, ``_claude-scan.yml``), which fails outright
when ``creek-tools/uv.lock`` disagrees with ``creek-tools/pyproject.toml``:

    error: The lockfile at `uv.lock` needs to be updated, but --locked
    was provided.

The ``pip`` ecosystem does not read or write ``uv.lock`` at all -- GitHub
scopes it to requirements ``.txt`` files and PEP 621 ``pyproject.toml``,
and gives uv its own ``package-ecosystem`` value. A ``pip`` entry pointed
at this directory therefore opens pull requests that edit the declared
specifier and nothing else, so *every* dependency pull request arrives
with a stale lock and reds the whole matrix before a single test runs.
Seven such pull requests were open simultaneously when this guard was
written. That is a defect in the configuration, not a style preference,
and these tests exist so a regression to a non-lock-aware ecosystem
fails the gate here rather than silently next Monday.

The fallback of keeping ``pip`` and adding
``versioning-strategy: "lockfile-only"`` does not work and is rejected
below: the strategy is supported by the ``pip`` ecosystem, but ``pip``
never looks at ``uv.lock``, so the pairing yields no pull requests at
all rather than lock refreshes.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.shell_command_support import DEPENDABOT_CONFIG, REPO_ROOT, load_yaml

#: The only ``package-ecosystem`` values this repository declares, and the
#: only ones Dependabot accepts for them. A value outside this set -- a
#: plausible typo such as ``"uv-lock"`` -- is rejected by Dependabot when
#: the schedule fires, which produces no pull requests and no CI signal
#: whatsoever, so the typo is invisible until someone notices the silence.
_ACCEPTED_ECOSYSTEMS = frozenset({"uv", "github-actions"})

#: The ecosystem that regenerates ``uv.lock``. ``pip`` is the value this
#: entry carried before issue #1085 and the one that must never return.
_LOCK_AWARE_ECOSYSTEM = "uv"

#: The label the Dependabot-to-Ralph bridge filters on. Load-bearing:
#: ``.github/workflows/dependabot-to-ralph-issue.yml`` selects on this
#: exact label for its marker dedup, and ``scripts/ralph/pr-ready.sh``
#: pairs it with the ``app/dependabot`` author.
_BRIDGE_LABEL = "dependencies"


def _updates() -> list[dict[str, Any]]:
    """Return every ``updates`` entry from the Dependabot configuration.

    Returns:
        The parsed update entries. Parsing (rather than scanning text)
        is what keeps a YAML comment from satisfying an assertion.
    """
    config = load_yaml(DEPENDABOT_CONFIG)
    entries: list[dict[str, Any]] = config["updates"]
    return entries


def _entry_for(directory: str) -> dict[str, Any]:
    """Return the single update entry whose ``directory`` is ``directory``.

    Args:
        directory: The ``directory`` value to select on, e.g. ``/creek-tools``.

    Returns:
        The matching update entry.
    """
    matches = [entry for entry in _updates() if entry.get("directory") == directory]
    if len(matches) != 1:
        pytest.fail(
            f"expected exactly one Dependabot update entry for {directory!r}, "
            f"found {len(matches)} in {DEPENDABOT_CONFIG}"
        )
    return matches[0]


@pytest.fixture(name="creek_tools_entry")
def fixture_creek_tools_entry() -> dict[str, Any]:
    """Return the Dependabot update entry for the ``/creek-tools`` package.

    Returns:
        The parsed update entry.
    """
    return _entry_for("/creek-tools")


def test_creek_tools_entry_is_lock_aware(creek_tools_entry: dict[str, Any]) -> None:
    """The ``/creek-tools`` entry must use the ecosystem that maintains uv.lock.

    ``versioning-strategy: "lockfile-only"`` is accepted only *alongside*
    the ``uv`` ecosystem: pairing it with ``pip`` reads like a fix and is
    not one, because ``pip`` never opens ``uv.lock`` in the first place.
    """
    ecosystem = creek_tools_entry["package-ecosystem"]
    assert ecosystem == _LOCK_AWARE_ECOSYSTEM, (
        f"the /creek-tools Dependabot entry declares "
        f"package-ecosystem: {ecosystem!r}, which does not maintain "
        f"creek-tools/uv.lock. Every CI job installs with "
        f"`uv export --locked`, so each pull request from a non-uv "
        f"ecosystem arrives with a stale lock and fails the whole "
        f"matrix before any test runs (issue #1085)."
    )


def test_creek_tools_entry_points_at_a_real_uv_lock(
    creek_tools_entry: dict[str, Any],
) -> None:
    """The entry's ``directory`` must be where ``uv.lock`` actually lives.

    The uv ecosystem regenerates the lock found at ``directory``; pointed
    anywhere else it is as lock-blind as ``pip`` was.
    """
    directory = creek_tools_entry["directory"]
    lock = REPO_ROOT / directory.lstrip("/") / "uv.lock"
    assert lock.is_file(), (
        f"the /creek-tools Dependabot entry points at {directory!r}, "
        f"but no uv.lock exists at {lock}"
    )


def test_creek_tools_entry_keeps_the_dependencies_label(
    creek_tools_entry: dict[str, Any],
) -> None:
    """The entry must keep the label the Ralph bridge selects on.

    ``dependabot-to-ralph-issue.yml`` and ``scripts/ralph/pr-ready.sh``
    both filter on this exact label, so dropping it during an ecosystem
    swap would strand every dependency pull request outside the loop.
    """
    labels = creek_tools_entry.get("labels", [])
    assert _BRIDGE_LABEL in labels, (
        f"the /creek-tools Dependabot entry declares labels {labels!r}, "
        f"without {_BRIDGE_LABEL!r}; the Dependabot-to-Ralph bridge and "
        f"scripts/ralph/pr-ready.sh both select on that exact label"
    )


def test_creek_tools_entry_keeps_the_minor_patch_group(
    creek_tools_entry: dict[str, Any],
) -> None:
    """The entry must keep grouping minor and patch bumps into one PR.

    Without the group, a weekly run opens one pull request per package
    and saturates ``open-pull-requests-limit``, which starves the major
    bumps that actually need a human decision. An ecosystem swap must not
    drop it silently.
    """
    groups = creek_tools_entry.get("groups", {})
    update_types = groups.get("python-minor-and-patch", {}).get("update-types")
    assert update_types == ["minor", "patch"], (
        f"the /creek-tools Dependabot entry declares groups {groups!r}; "
        f"the python-minor-and-patch group must still batch "
        f"['minor', 'patch'] into one pull request"
    )


def test_github_actions_entry_is_untouched() -> None:
    """The root ``github-actions`` entry must keep working as it does today.

    It is a separate ecosystem with its own working history (#839 merged
    from it), so the ``/creek-tools`` fix must leave it alone.
    """
    entry = _entry_for("/")
    assert entry["package-ecosystem"] == "github-actions"
    assert _BRIDGE_LABEL in entry["labels"]
    groups = entry["groups"]
    assert groups["actions-minor-and-patch"]["update-types"] == ["minor", "patch"]


def test_every_ecosystem_is_one_dependabot_accepts() -> None:
    """No entry may name an ecosystem value Dependabot does not accept.

    A rejected value fails at schedule time inside Dependabot with no CI
    run, no pull request, and no failing job -- the configuration simply
    stops producing updates, which looks exactly like a quiet week.
    """
    declared = {entry["package-ecosystem"] for entry in _updates()}
    assert declared <= _ACCEPTED_ECOSYSTEMS, (
        f"{DEPENDABOT_CONFIG} declares package-ecosystem values "
        f"{sorted(declared - _ACCEPTED_ECOSYSTEMS)!r}, which are not "
        f"among the values this repository supports "
        f"({sorted(_ACCEPTED_ECOSYSTEMS)!r})"
    )
