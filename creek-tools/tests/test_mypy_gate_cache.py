"""Guards for the stale-mypy-cache false green in ``scripts/typecheck.sh`` (#1186).

Mypy validates a cache entry on ``(mtime, size)``: when both match it trusts the
stored result without re-reading the source. So an edit that preserves both --
``cp -p``, ``tar -x``, ``rsync -t``, a same-length change, a branch switch onto a
file of identical length -- is answered from ``.mypy_cache``, and the gate
reports clean on a tree CI rejects. That is the same class of defect ruff's cache
turned out to have in #1119.

The tests below never assert on the *text* of the script. They run it with the
type checker stubbed out, take the argument vector it really builds, and then run
real mypy with exactly those arguments against a project whose cache has been
deliberately poisoned. A guard that only grepped for ``--no-incremental`` would
pass on a script that named the flag in a comment.

The invocation under test is the **bare** ``./scripts/typecheck.sh`` -- the one a
developer actually runs. A cold path reachable only via an explicit flag leaves
the default uncovered, which is the exact shape #1186 is about.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING

from tests.shell_command_support import SCRIPTS_DIR

if TYPE_CHECKING:
    from pathlib import Path

TYPECHECK_SCRIPT = SCRIPTS_DIR / "typecheck.sh"
MODULE_NAME = "typed_module"

# Byte-for-byte the same length: mypy's (mtime, size) validation is only
# defeated when the rewrite preserves the size, so sources of differing length
# would make these tests pass for the wrong reason.
_CLEAN_SOURCE = "def f() -> int:\n    return 111\n"
_BROKEN_SOURCE = 'def f() -> int:\n    return "1"\n'


def _install_mypy_stub(bin_dir: Path, capture_path: Path) -> None:
    """Install a ``python`` shim that records the argv of ``python -m mypy``.

    Any other ``python`` call the script makes is delegated to the real
    interpreter, so the script runs exactly as it does for a developer; only the
    type checker itself is replaced by a snapshot of how it was invoked.

    Args:
        bin_dir: Directory prepended to ``PATH``; created if absent.
        capture_path: File the shim writes the argv JSON to.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = shlex.quote(sys.executable)
    destination = shlex.quote(str(capture_path))
    dump = 'import json, sys; open(sys.argv[1], "w").write(json.dumps(sys.argv[2:]))'
    stub = bin_dir / "python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "mypy" ]]; then\n'
        f'    exec {interpreter} -c {shlex.quote(dump)} {destination} "$@"\n'
        "fi\n"
        f'exec {interpreter} "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_typecheck_script(
    run_dir: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run ``scripts/typecheck.sh`` with the given arguments.

    ``CI`` is cleared so the result describes what a developer sees locally,
    which is the environment #1186 is about.

    Args:
        run_dir: Scratch directory for the stub and its capture file.
        *args: Arguments to pass to the script.

    Returns:
        The completed process.
    """
    env = dict(os.environ)
    env.pop("CI", None)
    env["PATH"] = f"{run_dir / 'bin'}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [str(TYPECHECK_SCRIPT), *args], env=env, capture_output=True, text=True
    )


def _gate_mypy_flags(run_dir: Path, *args: str) -> list[str]:
    """Return the flags ``scripts/typecheck.sh`` really passes to mypy.

    Args:
        run_dir: Scratch directory for the stub and its capture file.
        *args: Arguments to pass to the script.

    Returns:
        The option arguments of the ``python -m mypy`` command line, with the
        ``-m mypy`` prefix and the path targets removed.
    """
    capture_path = run_dir / "mypy-argv.json"
    _install_mypy_stub(run_dir / "bin", capture_path)

    result = _run_typecheck_script(run_dir, *args)
    assert result.returncode == 0, result.stderr or result.stdout
    assert capture_path.exists(), (
        "scripts/typecheck.sh never reached `python -m mypy`; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    argv: list[str] = json.loads(capture_path.read_text(encoding="utf-8"))
    assert argv[:2] == ["-m", "mypy"], argv
    return [token for token in argv[2:] if token.startswith("-")]


def _run_mypy(project: Path, flags: list[str]) -> subprocess.CompletedProcess[str]:
    """Run real mypy over the throwaway module inside ``project``.

    Args:
        project: Working directory; mypy's default ``.mypy_cache`` lives here.
        flags: Options to pass, e.g. those recovered from the gate.

    Returns:
        The completed process; a non-zero return code means errors were found.
    """
    return subprocess.run(
        [sys.executable, "-m", "mypy", *flags, f"{MODULE_NAME}.py"],
        cwd=project,
        capture_output=True,
        text=True,
    )


def _poisoned_project(tmp_path: Path) -> Path:
    """Build a project whose ``.mypy_cache`` records a result that is now wrong.

    A clean module is checked (seeding the cache), then rewritten to a version
    with a type error while ``(mtime, size)`` are preserved -- the exact trigger
    #1186 describes.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The project directory, holding a poisoned cache and broken source.
    """
    project = tmp_path / "project"
    project.mkdir()
    module = project / f"{MODULE_NAME}.py"
    module.write_text(_CLEAN_SOURCE, encoding="utf-8")

    seed = _run_mypy(project, [])
    assert seed.returncode == 0, seed.stdout or seed.stderr
    assert (project / ".mypy_cache").is_dir(), (
        "the seeding run must leave a cache behind, or nothing is poisoned"
    )

    before = module.stat()
    module.write_text(_BROKEN_SOURCE, encoding="utf-8")
    os.utime(module, (before.st_atime, before.st_mtime))
    after = module.stat()
    assert after.st_size == before.st_size, "rewrite must preserve the size"
    assert int(after.st_mtime) == int(before.st_mtime), (
        "rewrite must preserve the mtime"
    )
    return project


def test_a_reused_mypy_cache_answers_from_stale_state(tmp_path: Path) -> None:
    """The hazard is real: incremental mypy calls the broken tree clean.

    This is the false green #1186 reports, reproduced directly. Without it the
    tests below would be consistent with a world where the stale answer never
    happens, and would prove nothing about the flags they check.
    """
    project = _poisoned_project(tmp_path)

    stale = _run_mypy(project, [])

    assert stale.returncode == 0, (
        "expected the poisoned cache to yield a false green; "
        f"got {stale.returncode} / {stale.stdout!r}"
    )
    assert "Success" in stale.stdout


def test_the_default_invocation_does_not_trust_a_stale_cache(tmp_path: Path) -> None:
    """The bare ``./scripts/typecheck.sh`` must find the error anyway.

    This is the point of #1186. The flags come from executing the script with no
    arguments and no ``CI`` in the environment -- the invocation a developer runs
    before pushing -- so a cold path reachable only behind an explicit flag fails
    here rather than passing on a technicality.
    """
    project = _poisoned_project(tmp_path)
    flags = _gate_mypy_flags(tmp_path / "gate")

    checked = _run_mypy(project, flags)

    assert checked.returncode != 0, (
        "the default `./scripts/typecheck.sh` answered from the stale cache "
        f"(#1186); flags were {flags!r}, output {checked.stdout!r}"
    )
    assert "Incompatible return value type" in checked.stdout


def test_fast_opts_out_and_is_the_cached_path(tmp_path: Path) -> None:
    """``--fast`` is the opt-*out*, and it really does reuse the cache.

    The escape hatch has to be honest in both directions: it must drop the cold
    flag, and a reader must be able to see that choosing it is what re-exposes
    the staleness the default prevents.
    """
    project = _poisoned_project(tmp_path)
    flags = _gate_mypy_flags(tmp_path / "gate", "--fast")

    assert "--no-incremental" not in flags, (
        f"--fast must opt out of the cold default; got {flags!r}"
    )

    stale = _run_mypy(project, flags)

    assert stale.returncode == 0, (
        "--fast is documented as the cached, potentially-stale path; "
        f"it now rejects the poisoned tree, so the help text is wrong: {stale.stdout!r}"
    )


def test_incremental_is_an_alias_for_fast(tmp_path: Path) -> None:
    """``--incremental`` selects the same opt-out as ``--fast``."""
    fast = _gate_mypy_flags(tmp_path / "fast", "--fast")
    incremental = _gate_mypy_flags(tmp_path / "incremental", "--incremental")

    assert fast == incremental, (
        f"--incremental must behave as --fast; {incremental!r} != {fast!r}"
    )


def test_the_gate_passes_one_cache_defeating_flag_not_two(tmp_path: Path) -> None:
    """``--no-incremental`` and ``--cache-dir`` together are redundant.

    Either alone defeats the cache. Passing both invites the reader to think one
    of them is doing something the other is not.
    """
    flags = _gate_mypy_flags(tmp_path / "gate")

    assert "--no-incremental" in flags, flags
    assert not any(flag.startswith("--cache-dir") for flag in flags), (
        f"--no-incremental already defeats the cache; drop --cache-dir: {flags!r}"
    )


def test_help_documents_the_cold_default_and_its_opt_out(tmp_path: Path) -> None:
    """``--help`` must name the opt-out and say the default reads live source.

    The claim is checked against the script's real output, and the behaviour it
    claims is pinned by :func:`test_the_default_invocation_does_not_trust_a_stale_cache`
    -- so this cannot become a doc that asserts a default nothing enforces.
    """
    result = _run_typecheck_script(tmp_path / "gate", "--help")

    assert result.returncode == 0, result.stderr
    assert "--fast" in result.stdout, "the opt-out must be discoverable"
    assert "--no-incremental" in result.stdout, (
        "the help must name the flag the default run passes"
    )
    assert "#1186" in result.stdout, (
        "the help must point at the issue that explains why the default is cold"
    )
