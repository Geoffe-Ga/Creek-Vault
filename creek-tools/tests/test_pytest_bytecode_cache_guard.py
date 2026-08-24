"""Static and behavioural guards ensuring scripts/test.sh prevents stale bytecode false greens.

Issue #1187: CPython's .pyc invalidation is keyed on (mtime, size). When a source
file changes while both are preserved (e.g. tar -x, rsync -t, same-length edits),
the import system reads the stale cached bytecode instead of the live source, and
pytest can report a green run on code that actually fails in CI.

PYTHONDONTWRITEBYTECODE=1 alone does not close this: it only stops *new* .pyc
files from being written, it does not stop an *existing* stale .pyc from being
read. The fix in scripts/test.sh instead points PYTHONPYCACHEPREFIX at a fresh
per-run temp directory, so there is never a stale entry available to read.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.shell_command_support import SCRIPTS_DIR, non_comment_lines

_ORIGINAL_SOURCE = "VALUE = 1\n"
_REWRITTEN_SOURCE = "VALUE = 9\n"
assert len(_ORIGINAL_SOURCE) == len(_REWRITTEN_SOURCE)


def test_test_script_redirects_pycache_to_a_fresh_tmpdir() -> None:
    """scripts/test.sh must redirect the bytecode cache to a fresh per-run directory.

    A per-run mktemp'd PYTHONPYCACHEPREFIX is what actually prevents stale reads
    (issue #1187); PYTHONDONTWRITEBYTECODE alone is not sufficient.
    """
    script = SCRIPTS_DIR / "test.sh"
    lines = non_comment_lines(script)
    text = "\n".join(line.strip() for line in lines)

    assert "mktemp -d" in text, (
        "scripts/test.sh must create a fresh temp directory per run for the "
        "bytecode cache (issue #1187)"
    )
    assert "PYTHONPYCACHEPREFIX" in text, (
        "scripts/test.sh must set PYTHONPYCACHEPREFIX to the fresh temp "
        "directory so stale .pyc files are never read (issue #1187)"
    )


def _seed_and_reimport(module_dir: Path, pycache_dir: Path | None) -> subprocess.CompletedProcess[str]:
    """Import target_mod once (seeding any bytecode cache), then import it again
    after the source has been rewritten in place with mtime and size preserved,
    returning the result of the second import's assertion on the observed value.
    """
    module_path = module_dir / "target_mod.py"
    module_path.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    stat_before = module_path.stat()

    seed_env = dict(os.environ)
    seed_env.pop("PYTHONDONTWRITEBYTECODE", None)
    if pycache_dir is not None:
        seed_env["PYTHONPYCACHEPREFIX"] = str(pycache_dir)
    else:
        seed_env.pop("PYTHONPYCACHEPREFIX", None)

    seed_cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, r'{module_dir}'); import target_mod",
    ]
    seed_result = subprocess.run(seed_cmd, env=seed_env, capture_output=True, text=True)
    assert seed_result.returncode == 0, seed_result.stderr

    # Rewrite the source in place, preserving mtime and size -- the exact
    # trigger for stale bytecode described in issue #1187.
    module_path.write_text(_REWRITTEN_SOURCE, encoding="utf-8")
    os.utime(module_path, (stat_before.st_atime, stat_before.st_mtime))
    assert module_path.stat().st_size == stat_before.st_size

    run_cmd = [
        sys.executable,
        "-c",
        "import sys; sys.path.insert(0, r'{}'); import target_mod; print(target_mod.VALUE)".format(
            module_dir
        ),
    ]
    return subprocess.run(run_cmd, env=seed_env, capture_output=True, text=True)


def test_reused_cache_reads_stale_bytecode_without_the_fix(tmp_path: Path) -> None:
    """Baseline: without redirecting to a fresh cache dir, the stale value is read.

    This confirms the failure mode issue #1187 describes actually occurs, so this
    guard fails against the unfixed behaviour rather than passing unconditionally.
    """
    result = _seed_and_reimport(tmp_path, pycache_dir=None)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1", (
        "expected the stale cached bytecode to be read when the cache is "
        f"reused across the source rewrite, got: {result.stdout!r}"
    )


def test_fresh_pycacheprefix_per_run_avoids_stale_bytecode(tmp_path: Path) -> None:
    """Fixed behaviour: seeding and re-importing under distinct, fresh
    PYTHONPYCACHEPREFIX directories -- as scripts/test.sh does via `mktemp -d`
    on every invocation -- means there is nothing stale to read, so the live
    source value is always observed.
    """
    module_dir = tmp_path / "project"
    module_dir.mkdir()
    module_path = module_dir / "target_mod.py"
    module_path.write_text(_ORIGINAL_SOURCE, encoding="utf-8")
    stat_before = module_path.stat()

    seed_pycache = tmp_path / "pycache-run-1"
    seed_env = dict(os.environ)
    seed_env.pop("PYTHONDONTWRITEBYTECODE", None)
    seed_env["PYTHONPYCACHEPREFIX"] = str(seed_pycache)
    seed_cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, r'{module_dir}'); import target_mod",
    ]
    seed_result = subprocess.run(seed_cmd, env=seed_env, capture_output=True, text=True)
    assert seed_result.returncode == 0, seed_result.stderr
    assert any(seed_pycache.rglob("*.pyc")), "expected a cached .pyc to be seeded"

    # Rewrite the source, preserving mtime and size.
    module_path.write_text(_REWRITTEN_SOURCE, encoding="utf-8")
    os.utime(module_path, (stat_before.st_atime, stat_before.st_mtime))
    assert module_path.stat().st_size == stat_before.st_size

    # A second, distinct fresh cache dir -- exactly what a new `mktemp -d`
    # per invocation of scripts/test.sh produces.
    run_pycache = tmp_path / "pycache-run-2"
    run_env = dict(seed_env)
    run_env["PYTHONPYCACHEPREFIX"] = str(run_pycache)
    run_cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, r'{module_dir}'); import target_mod; "
        "assert target_mod.VALUE == 9, target_mod.VALUE",
    ]
    result = subprocess.run(run_cmd, env=run_env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
