"""The local ruff gate must not answer a check from a stale ``.ruff_cache``.

Issue #1119. Ruff's per-file cache key is the file's **mtime** -- not its
size, and not a content hash. Any workflow that changes a file's bytes
while leaving its mtime unchanged or restored (``cp -p``, ``rsync -t``,
``tar -x``, an mtime-preserving editor, a restored backup) therefore
leaves ruff answering from a verdict it recorded for *different* content.
CI never has a ``.ruff_cache``, so it re-reads the file and rejects the
tree that ``./scripts/check-all.sh`` just cleared. That is not
hypothetical: it cost a real CI failure on PR #1117.

Each test here builds a throwaway project tree, seeds the cache with a
clean verdict, rewrites the fixture module with dirty content while
restoring the original mtime, and then runs the **real** gate script
against that tree. The script is exposed to the fixture through a symlink
so the file under test is byte-identical to ``creek-tools/scripts/<name>``:
``lint.sh`` and ``format.sh`` both derive ``PROJECT_ROOT`` from the parent
of ``${BASH_SOURCE[0]}``'s directory, so a symlink at
``<tmp>/scripts/<name>`` makes the fixture tree the project root and ruff
runs over the fixture instead of over this repository.

The fix is ``--no-cache`` on every ruff invocation in those scripts.
``--no-cache`` suppresses cache *writes* as well as reads, so no assertion
here may depend on a ``.ruff_cache`` existing *after* a script has run.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.shell_command_support import SCRIPTS_DIR

if TYPE_CHECKING:
    from pathlib import Path

# A frozen mtime, in nanoseconds, restamped after every write to the
# fixture module -- that restamping is the whole mechanism under test.
# ``os.utime(..., ns=...)`` is deliberate: ruff compares full-precision
# mtimes, and the float ``times=`` form can round and miss the cache.
_STAMP_NS = 1_700_000_000_000_000_000

_SOURCE_NAME = "sample.py"
_RUFF_CONFIG_NAME = "ruff.toml"
_CACHE_NAME = ".ruff_cache"

# A ``ruff.toml`` (rather than a ``pyproject.toml``) anchors ruff's config
# discovery inside the fixture tree, so no ancestor configuration can leak
# in and no repository setting can change what these fixtures mean.
_RUFF_CONFIG = '[lint]\nselect = ["F", "I"]\n'

_LINT_CLEAN = "import os\n\nprint(os.name)\n"
# F401: ``sys`` is imported and never used. The imports stay correctly
# sorted, so this unambiguously exercises the lint gate, not the sort gate.
_LINT_POISONED = "import os\nimport sys\n\nprint(os.name)\n"

_IMPORTS_CLEAN = "import os\nimport sys\n\nprint(os.name, sys.platform)\n"
# I001: same bytes, same length, imports now out of order.
_IMPORTS_POISONED = "import sys\nimport os\n\nprint(os.name, sys.platform)\n"

_FORMAT_CLEAN = 'X = "a"\n'
# Formatter-dirty under ruff's double-quote default, exactly as long as the
# clean text, and import-clean so ``format.sh --check`` gets past its
# import-sorting step and actually reaches the formatter.
_FORMAT_POISONED = "X = 'a'\n"

_CACHE_PREMISE = (
    "ruff's cached verdict is no longer poisonable — if this fails after a "
    "ruff version bump, upstream fixed the cache key and this test's premise "
    "(not the repo) needs revisiting."
)


@dataclass(frozen=True)
class _RuffTree:
    """A throwaway project tree the real gate scripts can be pointed at.

    Attributes:
        root: The fixture project root; the scripts' ``PROJECT_ROOT``.
        source: The single Python module ruff sees inside the tree.
        env: Environment shared by every subprocess this test runs, so the
            seeding run and the script run agree on the cache location.
    """

    root: Path
    source: Path
    env: dict[str, str]


def _stamp(tree: _RuffTree, text: str) -> None:
    """Write ``text`` to the fixture module and restore its frozen mtime.

    Args:
        tree: The fixture tree to write into.
        text: New contents of the fixture module.
    """
    tree.source.write_text(text, encoding="utf-8")
    os.utime(tree.source, ns=(_STAMP_NS, _STAMP_NS))


def _make_tree(tmp_path: Path, *, script: str, source_text: str) -> _RuffTree:
    """Assemble a fixture tree whose ``scripts/`` holds the real gate script.

    ``scripts/`` is a real directory containing a *symlink* to the script
    under test, so the file that executes is byte-identical to the one this
    repository ships -- a copy could drift from it.

    Args:
        tmp_path: An empty directory to use as the project root.
        script: File name under ``creek-tools/scripts`` to expose, e.g.
            ``"lint.sh"``.
        source_text: Initial (clean) contents of the fixture module.

    Returns:
        The assembled tree, with the fixture module already stamped.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / script).symlink_to(SCRIPTS_DIR / script)
    (tmp_path / _RUFF_CONFIG_NAME).write_text(_RUFF_CONFIG, encoding="utf-8")

    env = dict(os.environ)
    env.pop("RUFF_NO_CACHE", None)
    env.pop("RUFF_OUTPUT_FORMAT", None)
    # Pins the cache inside the fixture: it keeps the seeding run and the
    # script run on one cache, and guarantees this test can never touch the
    # developer's real creek-tools/.ruff_cache.
    env["RUFF_CACHE_DIR"] = str(tmp_path / _CACHE_NAME)

    tree = _RuffTree(root=tmp_path, source=tmp_path / _SOURCE_NAME, env=env)
    _stamp(tree, source_text)
    return tree


def _run_ruff(tree: _RuffTree, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a bare ``ruff`` command inside the fixture tree.

    These direct ``ruff`` calls are **fixture seeding and control probes,
    not gate invocations**. ``creek-tools/CLAUDE.md`` §1.1 ("always use
    ``./scripts/*``, never the tool directly") governs how this project's
    quality gates are run; the point of these calls is to observe what the
    cache does *underneath* a gate. The gate itself is only ever invoked
    through the real script, by :func:`_run_script`.

    The argv must mirror the script's own argv exactly: ruff's cache key
    includes the settings hash, so seeding with ``ruff check --select I .``
    and then running a script that issues ``ruff check .`` is a cache miss,
    which would silently turn the whole demonstration vacuous.

    Args:
        tree: The fixture tree to run in.
        argv: Full argument vector, starting with ``"ruff"``.

    Returns:
        The completed process.
    """
    return subprocess.run(
        argv,
        cwd=tree.root,
        env=tree.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _run_script(tree: _RuffTree, script: str) -> subprocess.CompletedProcess[str]:
    """Run ``scripts/<script> --check`` against the fixture tree.

    Args:
        tree: The fixture tree to run in.
        script: File name of the script exposed by :func:`_make_tree`.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["bash", str(tree.root / "scripts" / script), "--check"],
        cwd=tree.root,
        env=tree.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _assert_seeded(tree: _RuffTree, argv: list[str]) -> None:
    """Seed the cache with ``argv`` and assert the seeding really happened.

    A non-zero seed means the premise never held: the clean fixture was not
    clean, or ``ruff`` is not on ``PATH`` at all (exit 127), either of which
    would otherwise green the test for entirely the wrong reason.

    Args:
        tree: The fixture tree to seed.
        argv: The ruff argv to seed with; must mirror the script's argv.
    """
    seed = _run_ruff(tree, argv)
    assert seed.returncode == 0, (
        f"seeding {argv!r} did not report a clean tree (exit {seed.returncode}); "
        "the fixture premise never held.\n"
        f"stdout:\n{seed.stdout}\nstderr:\n{seed.stderr}"
    )
    cache = tree.root / _CACHE_NAME
    assert cache.is_dir(), (
        f"{argv!r} wrote no cache directory at {cache}; there is nothing to "
        "poison and the test below would pass vacuously."
    )
    assert any(entry.is_file() for entry in cache.rglob("*")), (
        f"cache directory {cache} is empty after seeding with {argv!r}"
    )


def test_lint_check_fails_on_a_tree_whose_ruff_cache_says_clean(
    tmp_path: Path,
) -> None:
    """``scripts/lint.sh --check`` must re-read a file the cache calls clean.

    The fixture module gains an unused import (F401) at an unchanged mtime.
    Cold ruff rejects it; ruff with the seeded cache does not; the gate must
    side with cold ruff, because CI is always cold.
    """
    tree = _make_tree(tmp_path, script="lint.sh", source_text=_LINT_CLEAN)
    argv = ["ruff", "check", "."]
    _assert_seeded(tree, argv)

    _stamp(tree, _LINT_POISONED)

    control = _run_ruff(tree, argv)
    assert control.returncode == 0, _CACHE_PREMISE
    cold = _run_ruff(tree, [*argv, "--no-cache"])
    assert cold.returncode != 0, (
        "the poisoned fixture is not actually dirty; `ruff check --no-cache .` "
        f"passed it.\nstdout:\n{cold.stdout}\nstderr:\n{cold.stderr}"
    )

    result = _run_script(tree, "lint.sh")
    assert result.returncode != 0, (
        "scripts/lint.sh --check cleared a tree carrying an unused import: it "
        "answered from .ruff_cache instead of re-reading the file, so Gate 2 "
        "can pass a tree CI rejects (#1119).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_format_check_fails_on_unsorted_imports_hidden_by_the_cache(
    tmp_path: Path,
) -> None:
    """``scripts/format.sh --check`` must re-read imports the cache cleared.

    ``format.sh --check`` runs ``ruff check --select I .`` first, so this
    test owns its own tree: it short-circuits before the formatter step and
    could not share a tree with the formatter test.
    """
    tree = _make_tree(tmp_path, script="format.sh", source_text=_IMPORTS_CLEAN)
    argv = ["ruff", "check", "--select", "I", "."]
    _assert_seeded(tree, argv)

    _stamp(tree, _IMPORTS_POISONED)

    control = _run_ruff(tree, argv)
    assert control.returncode == 0, _CACHE_PREMISE
    cold = _run_ruff(tree, [*argv, "--no-cache"])
    assert cold.returncode != 0, (
        "the poisoned fixture's imports are not actually unsorted; "
        "`ruff check --select I --no-cache .` passed it.\n"
        f"stdout:\n{cold.stdout}\nstderr:\n{cold.stderr}"
    )

    result = _run_script(tree, "format.sh")
    assert result.returncode != 0, (
        "scripts/format.sh --check cleared a tree whose imports are unsorted "
        "(I001): it answered from .ruff_cache instead of re-reading the file "
        "(#1119).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_format_check_fails_on_misformatted_code_hidden_by_the_cache(
    tmp_path: Path,
) -> None:
    """``scripts/format.sh --check`` must re-read formatting the cache cleared.

    Both of the script's ``--check`` steps are seeded: the import-sorting
    check runs first and must pass for execution to reach the formatter, and
    the formatter's own cached verdict is the one being poisoned.
    """
    tree = _make_tree(tmp_path, script="format.sh", source_text=_FORMAT_CLEAN)
    imports_argv = ["ruff", "check", "--select", "I", "."]
    format_argv = ["ruff", "format", "--check", "."]
    _assert_seeded(tree, imports_argv)
    _assert_seeded(tree, format_argv)

    _stamp(tree, _FORMAT_POISONED)

    control = _run_ruff(tree, format_argv)
    assert control.returncode == 0, _CACHE_PREMISE
    cold = _run_ruff(tree, [*format_argv, "--no-cache"])
    assert cold.returncode != 0, (
        "the poisoned fixture is already formatted; "
        "`ruff format --check . --no-cache` passed it.\n"
        f"stdout:\n{cold.stdout}\nstderr:\n{cold.stderr}"
    )

    result = _run_script(tree, "format.sh")
    assert result.returncode != 0, (
        "scripts/format.sh --check cleared a tree ruff would reformat: it "
        "answered from .ruff_cache instead of re-reading the file (#1119).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
