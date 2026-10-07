"""``scripts/bench.sh`` runs the harness and propagates its exit codes.

The wrapper's one job beyond ``exec python -m creek_mcp.bench`` is to inject
the checkout's git sha into a live run that did not pass one, so an operator
cannot produce an unreproducible live report by forgetting a flag.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[2]
_SCRIPT = _PROJECT / "scripts" / "bench.sh"
_DIGEST = "sha256:" + "c" * 64
_TIMEOUT_S = 120


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run bench.sh with this interpreter first on PATH."""
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=_TIMEOUT_S,
        check=False,
    )


def _live(out: Path, *extra: str) -> tuple[str, ...]:
    """Argv for a live run against an unroutable endpoint."""
    return (
        "reflect",
        "--mode",
        "live",
        "--ollama-url",
        "http://127.0.0.1:9",
        "--model",
        "mistral:7b",
        "--out",
        str(out),
        *extra,
    )


def test_bench_sh_propagates_refusal(tmp_path: Path) -> None:
    """A live run with no digest exits 2 through the wrapper."""
    result = _run(*_live(tmp_path / "r.json"))
    assert result.returncode == 2, result.stderr
    assert "--digest" in result.stderr


def test_bench_sh_injects_git_sha_for_live_runs(tmp_path: Path) -> None:
    """With a digest but no --git-sha, the wrapper supplies HEAD's sha.

    Without injection this would be refused (2); with it, validation passes
    and the run fails only on the unreachable endpoint (1).
    """
    result = _run(*_live(tmp_path / "r.json", "--digest", _DIGEST))
    assert result.returncode == 1, result.stderr
    assert "ProviderUnavailableError" in result.stderr


def test_bench_sh_fake_ok(tmp_path: Path) -> None:
    """A small fake run succeeds and writes its report."""
    out = tmp_path / "r.json"
    result = _run(
        "reflect",
        "--out",
        str(out),
        "--entries",
        "1",
        "--cold-trials",
        "0",
        "--warm-trials",
        "1",
        "--concurrency",
        "1",
    )
    assert result.returncode == 0, result.stderr
    assert out.is_file()
    assert result.stdout.startswith("verdict=fits ")
