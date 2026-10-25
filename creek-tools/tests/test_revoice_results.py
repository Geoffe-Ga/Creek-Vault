"""The detached rewrite cache bounds memory and preserves caller/tier isolation."""

from __future__ import annotations

from creek_mcp.revoice_results import RevoiceResults
from creek_mcp.tier_ceiling import TierCeiling


def test_only_original_agent_at_adequate_ceiling_can_read() -> None:
    """An authenticated agent cannot reuse another agent's handle or downgrade it."""
    cache = RevoiceResults()
    cache.put(
        "one",
        consumer="agent-one",
        ceiling=TierCeiling.PERSONAL,
        response={"text": "private rewrite"},
    )
    assert cache.get("one", consumer="agent-two", ceiling=TierCeiling.ALL) is None
    assert cache.get("one", consumer="agent-one", ceiling=TierCeiling.OPEN) is None
    assert cache.get("missing", consumer="agent-one", ceiling=TierCeiling.ALL) is None
    assert cache.get("one", consumer="agent-one", ceiling=TierCeiling.PERSONAL) == {
        "text": "private rewrite"
    }


def test_results_expire_without_read_renewal_and_oldest_is_evicted() -> None:
    """Count and time bounds both discard bodies; reading does not prolong storage."""
    now = [0.0]
    cache = RevoiceResults(limit=1, lifetime=10, clock=lambda: now[0])
    cache.put(
        "first", consumer="agent", ceiling=TierCeiling.OPEN, response={"text": "a"}
    )
    cache.put(
        "second", consumer="agent", ceiling=TierCeiling.OPEN, response={"text": "b"}
    )
    assert cache.get("first", consumer="agent", ceiling=TierCeiling.OPEN) is None
    now[0] = 9
    assert cache.get("second", consumer="agent", ceiling=TierCeiling.OPEN) == {
        "text": "b"
    }
    now[0] = 10
    assert cache.get("second", consumer="agent", ceiling=TierCeiling.OPEN) is None
    cache.put(
        "third", consumer="agent", ceiling=TierCeiling.OPEN, response={"text": "c"}
    )
    now[0] = 21
    cache.put(
        "fourth", consumer="agent", ceiling=TierCeiling.OPEN, response={"text": "d"}
    )
    assert cache.get("third", consumer="agent", ceiling=TierCeiling.OPEN) is None


def test_result_copies_cannot_mutate_retained_response() -> None:
    """Writers and readers cannot mutate retained containers."""
    cache = RevoiceResults()
    response = {"voice_skills": ["voice-core"]}
    cache.put("one", consumer="agent", ceiling=TierCeiling.OPEN, response=response)
    response["voice_skills"].clear()
    read = cache.get("one", consumer="agent", ceiling=TierCeiling.OPEN)
    assert read is not None
    read["voice_skills"].clear()
    assert cache.get("one", consumer="agent", ceiling=TierCeiling.OPEN) == {
        "voice_skills": ["voice-core"]
    }
