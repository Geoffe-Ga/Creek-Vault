"""End-to-end (e2e) tests — opt-in via ``pytest -m e2e``.

These tests use real disk I/O against ``tmp_path`` to exercise the full
pipeline against synthetic vaults and source directories. They are
deliberately heavier than the unit suite and stay out of the default
``./scripts/test.sh`` selection (and out of CI's unit job) per CI-003.

They are **not** ungated: the ``integration-e2e`` job in
``.github/workflows/ci.yml`` runs this lane on every PR and blocks the Quality
Gate on it. That makes hermeticity a hard requirement here — no network, no API
keys, no real vault. A test needing a real provider key is ``live``, not
``e2e``, and belongs in ``tests/test_live_smoke.py``.

Run them locally with:

    ./scripts/test.sh --e2e
    ./scripts/test.sh --all
"""
