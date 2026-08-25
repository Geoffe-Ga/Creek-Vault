"""Guards for the stale-bytecode false green in ``scripts/test.sh`` (issue #1187).

CPython invalidates a cached ``.pyc`` on ``(mtime, size)``. A source rewrite
that preserves both -- ``tar -x``, ``rsync -t``, ``cp -p``, a same-length edit,
a branch switch onto an older file of identical length -- leaves the stale
bytecode looking valid, so the suite runs code that is no longer on disk and
reports green on a tree CI rejects.

``PYTHONDONTWRITEBYTECODE=1`` (equivalently ``python -B``) does **not** close
that hole: it stops Python *writing* new ``.pyc`` files, not *reading* the
stale one already in the cache. That is asserted here rather than asserted in
prose, because it is the remedy a reader reaches for first.

What does close it is pointing ``PYTHONPYCACHEPREFIX`` at a fresh per-run
directory, so the cache lookup lands somewhere that starts empty. The tests
below take the environment ``scripts/test.sh`` actually hands pytest -- by
running the real script with pytest stubbed out -- and prove that a stale
``.pyc`` seeded by one gate run cannot be read by the next.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from tests.shell_command_support import SCRIPTS_DIR, non_comment_lines

TEST_SCRIPT = SCRIPTS_DIR / "test.sh"
MODULE_NAME = "target_mod"

# Same length by construction: the (mtime, size) invalidation key is only
# defeated when the rewrite keeps the size, so a differing length would make
# these tests pass for the wrong reason.
_ORIGINAL_SOURCE = "VALUE = 1\n"
_REWRITTEN_SOURCE = "VALUE = 9\n"


def _seed_module(module_dir: Path) -> Path:
    """Write the throwaway module these tests import, at its original value.

    Args:
        module_dir: Directory to create the module in; it is placed on
            ``sys.path`` by the import helpers below.

    Returns:
        Path to the written module.
    """
    module_dir.mkdir(parents=True, exist_ok=True)
    module_path = module_dir / f"{MODULE_NAME}.py"
    module_path.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    return module_path


def _rewrite_preserving_mtime_and_size(module_path: Path) -> None:
    """Rewrite the module's source while preserving its ``(mtime, size)``.

    This is the exact trigger issue #1187 describes: to the import system the
    cached bytecode still looks current, so it is reused.

    Args:
        module_path: The module written by :func:`_seed_module`.
    """
    before = module_path.stat()
    module_path.write_text(_REWRITTEN_SOURCE, encoding="utf-8")
    os.utime(module_path, (before.st_atime, before.st_mtime))
    after = module_path.stat()
    assert after.st_size == before.st_size, "rewrite must preserve the size"
    assert int(after.st_mtime) == int(before.st_mtime), (
        "rewrite must preserve the mtime"
    )


def _import_module(
    module_dir: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Import the throwaway module in a fresh interpreter and print its value.

    Args:
        module_dir: Directory holding the module; prepended to ``sys.path``.
        env: Environment for the child interpreter.

    Returns:
        The completed process; ``stdout`` carries the observed ``VALUE``.
    """
    code = (
        f"import sys; sys.path.insert(0, {str(module_dir)!r}); "
        f"import {MODULE_NAME}; print({MODULE_NAME}.VALUE)"
    )
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )


def _base_env() -> dict[str, str]:
    """Return an environment with both bytecode-cache variables cleared.

    The suite itself runs under ``scripts/test.sh``, which exports
    ``PYTHONPYCACHEPREFIX``. Inheriting it would let the gate under test supply
    the very isolation these tests are meant to prove, so it is dropped and
    each test sets what it means to exercise.

    Returns:
        A copy of ``os.environ`` without either variable.
    """
    env = dict(os.environ)
    env.pop("PYTHONPYCACHEPREFIX", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return env


def _install_pytest_stub(bin_dir: Path, capture_path: Path) -> None:
    """Install a ``python`` shim that captures the env of ``python -m pytest``.

    Every other ``python`` invocation the script makes (the toolchain probe in
    ``scripts/_lib.sh``) is delegated to the real interpreter, so the script
    runs to completion exactly as it does for a developer -- only the suite
    itself is replaced by a snapshot of the environment it would have run in.

    Args:
        bin_dir: Directory prepended to ``PATH``; created if absent.
        capture_path: File the shim writes the environment JSON to.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = shlex.quote(sys.executable)
    destination = shlex.quote(str(capture_path))
    dump = (
        "import json, os, sys; "
        'open(sys.argv[1], "w").write(json.dumps(dict(os.environ)))'
    )
    stub = bin_dir / "python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "pytest" ]]; then\n'
        f"    exec {interpreter} -c {shlex.quote(dump)} {destination}\n"
        "fi\n"
        f'exec {interpreter} "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _gate_environment(run_dir: Path, tmpdir: Path) -> dict[str, str]:
    """Run ``scripts/test.sh`` for real and return the env it hands pytest.

    ``tmpdir`` is deliberately *shared* between the runs a caller compares:
    handing each run its own ``TMPDIR`` would supply the isolation the script
    is supposed to provide, and a gate that pinned one fixed cache directory
    would still look fresh.

    Args:
        run_dir: Per-invocation scratch directory for the stub and its capture.
        tmpdir: ``TMPDIR`` for the run; the script mints its cache dir inside it.

    Returns:
        The environment mapping ``scripts/test.sh`` would have run pytest with.
    """
    bin_dir = run_dir / "bin"
    capture_path = run_dir / "pytest-env.json"
    tmpdir.mkdir(parents=True, exist_ok=True)
    _install_pytest_stub(bin_dir, capture_path)

    env = _base_env()
    env["TMPDIR"] = str(tmpdir)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    result = subprocess.run([str(TEST_SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert capture_path.exists(), (
        "scripts/test.sh never reached `python -m pytest`; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    captured: dict[str, str] = json.loads(capture_path.read_text(encoding="utf-8"))
    return captured


def _cached_module_bytecode(prefix: str) -> list[Path]:
    """Return the cached ``.pyc`` files for the throwaway module under a prefix.

    Args:
        prefix: A ``PYTHONPYCACHEPREFIX`` value.

    Returns:
        Matching ``.pyc`` paths; empty when the module was never cached there.
    """
    return [
        path for path in Path(prefix).rglob(f"{MODULE_NAME}.*.pyc") if path.is_file()
    ]


def test_a_reused_bytecode_cache_serves_stale_code(tmp_path: Path) -> None:
    """The hazard is real: one shared cache dir and the old value survives.

    This is the failure #1187 reports, reproduced directly. Without it the
    passing tests below would prove nothing -- they would be consistent with a
    world where the stale read never happens.
    """
    module_dir = tmp_path / "project"
    module_path = _seed_module(module_dir)
    env = _base_env()
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "shared-cache")

    seed = _import_module(module_dir, env)
    assert seed.returncode == 0, seed.stderr
    assert seed.stdout.strip() == "1"

    _rewrite_preserving_mtime_and_size(module_path)

    observed = _import_module(module_dir, env)
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip() == "1", (
        "expected the reused cache to serve the stale bytecode; "
        f"got {observed.stdout.strip()!r}"
    )


def test_dontwritebytecode_does_not_defeat_a_stale_pyc(tmp_path: Path) -> None:
    """``PYTHONDONTWRITEBYTECODE=1`` still executes the stale bytecode.

    It suppresses writes, not reads, so on any tree that has run the suite once
    it changes nothing about which code executes. Pinned here so the variable is
    never reinstated as the remedy for #1187.
    """
    module_dir = tmp_path / "project"
    module_path = _seed_module(module_dir)
    env = _base_env()
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "shared-cache")

    seed = _import_module(module_dir, env)
    assert seed.returncode == 0, seed.stderr

    _rewrite_preserving_mtime_and_size(module_path)

    env["PYTHONDONTWRITEBYTECODE"] = "1"
    observed = _import_module(module_dir, env)
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip() == "1", (
        "PYTHONDONTWRITEBYTECODE is documented as insufficient for #1187; if it "
        "now prevents the stale read, scripts/test.sh's comment needs revisiting"
    )


def test_gate_environment_defeats_a_stale_pyc_between_runs(tmp_path: Path) -> None:
    """Two real ``scripts/test.sh`` runs cannot share a stale ``.pyc``.

    The environments come from executing the script itself, so this fails if
    the gate stops exporting a fresh ``PYTHONPYCACHEPREFIX`` -- which is the
    whole claim #1187 needs held.
    """
    module_dir = tmp_path / "project"
    module_path = _seed_module(module_dir)

    gate_tmpdir = tmp_path / "gate-tmp"
    first = _gate_environment(tmp_path / "run-1", gate_tmpdir)
    second = _gate_environment(tmp_path / "run-2", gate_tmpdir)

    first_prefix = first.get("PYTHONPYCACHEPREFIX", "")
    assert first_prefix, "scripts/test.sh must export PYTHONPYCACHEPREFIX (#1187)"
    assert second.get("PYTHONPYCACHEPREFIX"), (
        "scripts/test.sh must export PYTHONPYCACHEPREFIX (#1187)"
    )

    seed = _import_module(module_dir, first)
    assert seed.returncode == 0, seed.stderr
    assert seed.stdout.strip() == "1"
    assert _cached_module_bytecode(first_prefix), (
        "the first run must actually have cached bytecode, or the second run "
        "has nothing stale to trip over and this test proves nothing"
    )

    _rewrite_preserving_mtime_and_size(module_path)

    observed = _import_module(module_dir, second)
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip() == "9", (
        "the second gate run executed stale bytecode from the first (#1187); "
        f"expected the rewritten value 9, got {observed.stdout.strip()!r}"
    )


def test_gate_mints_a_fresh_cache_directory_per_run(tmp_path: Path) -> None:
    """Each ``scripts/test.sh`` run gets its own cache dir, and reclaims it.

    Two runs sharing one directory is the stale-read hazard itself; a directory
    left behind is ~50MB of bytecode per run.
    """
    gate_tmpdir = tmp_path / "gate-tmp"
    first_prefix = _gate_environment(tmp_path / "run-1", gate_tmpdir)[
        "PYTHONPYCACHEPREFIX"
    ]
    second_prefix = _gate_environment(tmp_path / "run-2", gate_tmpdir)[
        "PYTHONPYCACHEPREFIX"
    ]

    assert first_prefix != second_prefix, (
        "each run must mint its own cache directory; two runs sharing one is "
        f"exactly the stale-read hazard (#1187): {first_prefix!r}"
    )
    assert Path(first_prefix).is_relative_to(gate_tmpdir), (
        "the prefix must be minted by the script's own mktemp -d under TMPDIR, "
        f"not inherited or hard-coded: {first_prefix!r}"
    )
    assert not Path(first_prefix).exists(), (
        "the script's EXIT trap must reclaim the cache directory; without it "
        "every run leaves ~50MB of bytecode behind"
    )


def test_test_script_does_not_rely_on_dontwritebytecode() -> None:
    """Belt and braces: the script must mint a fresh cache dir, not suppress writes.

    The behavioural test above is the real guard; this one names the mechanism
    so a future edit that swaps it for the variable proven insufficient in
    :func:`test_dontwritebytecode_does_not_defeat_a_stale_pyc` fails loudly
    rather than silently reopening #1187. Comment-blind, so the prose in
    ``scripts/test.sh`` explaining why ``PYTHONDONTWRITEBYTECODE`` is wrong
    cannot itself satisfy or violate the assertions.
    """
    executed = "\n".join(non_comment_lines(TEST_SCRIPT))

    assert "PYTHONPYCACHEPREFIX" in executed
    assert "mktemp -d" in executed
    assert "PYTHONDONTWRITEBYTECODE" not in executed, (
        "PYTHONDONTWRITEBYTECODE does not stop a stale .pyc being read; "
        "the fix for #1187 is a fresh PYTHONPYCACHEPREFIX per run"
    )
