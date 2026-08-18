"""Static and behavioural guards ensuring scripts/test.sh prevents stale bytecode false greens.

Issue #1187: CPython's .pyc invalidation is (mtime, size). When a source file
changes while mtime and size are preserved, pytest loads stale bytecode and can
report a green test run on code that fails in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.shell_command_support import SCRIPTS_DIR, non_comment_lines


def test_test_script_sets_pythondontwritebytecode() -> None:
    """scripts/test.sh must export PYTHONDONTWRITEBYTECODE=1 to prevent stale .pyc caching."""
    script = SCRIPTS_DIR / "test.sh"
    lines = non_comment_lines(script)
    matching = [
        line.strip()
        for line in lines
        if "PYTHONDONTWRITEBYTECODE=1" in line
    ]
    assert matching, (
        "scripts/test.sh must set and export PYTHONDONTWRITEBYTECODE=1 so "
        "local test runs do not read or write stale bytecode (issue #1187)"
    )


def test_pythondontwritebytecode_prevents_stale_pyc_execution(tmp_path: Path) -> None:
    """Behavioural verification: PYTHONDONTWRITEBYTECODE=1 forces loading live source."""
    module_path = tmp_path / "target_mod.py"
    module_path.write_text("VALUE = 1\n", encoding="utf-8")

    # Run with PYTHONDONTWRITEBYTECODE=1 to verify execution
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    cmd = [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, r'{tmp_path}'); import target_mod; assert target_mod.VALUE == 1",
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert result.returncode == 0
