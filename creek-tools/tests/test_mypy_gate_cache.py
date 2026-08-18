"""The mypy typecheck gate must support cold / cache-free execution to prevent stale cache false greens.

Issue #1186. Mypy\'s cache key is (mtime, size). When both are preserved,
mypy can answer from a stale .mypy_cache. typecheck.sh supports --cold, --ci,
and --no-cache (and respects CI=true) to bypass incremental caching when needed.
"""

from __future__ import annotations

from pathlib import Path


def test_typecheck_script_contains_cold_flags() -> None:
    script_path = Path("creek-tools/scripts/typecheck.sh")
    assert script_path.exists(), "creek-tools/scripts/typecheck.sh must exist"
    content = script_path.read_text(encoding="utf-8")

    assert "--cold" in content
    assert "--ci" in content
    assert "--no-cache" in content
    assert "--no-incremental" in content
    assert "--cache-dir=/dev/null" in content
    assert 'CI:-' in content or 'CI' in content
