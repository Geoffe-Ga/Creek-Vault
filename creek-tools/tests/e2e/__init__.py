"""End-to-end (e2e) tests — opt-in via ``pytest -m e2e``.

These tests use real disk I/O against ``tmp_path`` to exercise the full
pipeline against synthetic vaults and source directories. They are
deliberately heavier than the unit suite and are excluded from
``./scripts/test.sh`` (and CI's default job) per CI-003.

Run them locally with:

    ./scripts/test.sh --e2e
    ./scripts/test.sh --all
"""
