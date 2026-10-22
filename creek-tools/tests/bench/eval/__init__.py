"""Tests for the voice-fidelity eval harness (#1898).

Every test in this package is hermetic: the judge is the deterministic
:class:`~creek_mcp.bench.eval.judge.HermeticJudge` (or a recording stub), the
corpus is synthetic and temp-confined, and no vendor or frontier API is
reached. The one real-API path lives in ``test_live_judge.py`` behind the
``live`` marker, which never runs in CI.
"""
