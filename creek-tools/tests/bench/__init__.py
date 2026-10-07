"""Tests for the hermetic model-capacity benchmark harness (#1850).

Everything here is hermetic: a deterministic fake provider stands in for the
model, the corpus is synthetic and confined to a temp directory, and no test
reaches a network. The single opt-in exception is ``test_live_ollama.py``,
marked ``live`` so the default lane and CI never select it.
"""
