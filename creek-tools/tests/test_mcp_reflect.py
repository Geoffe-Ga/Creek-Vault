"""``creek.reflect`` MCP tool — anchored Higher-Self margin notes (#751).

The tool takes one journal entry plus a privacy-tier ceiling and returns
``{notes: [{quote, kind, note}], essay?}`` grounded in the user's corpus. The
contract this suite pins, in order of stakes:

1. **INTIMATE never egresses** — the LLM callable is obtained from a tier-keyed
   factory; an INTIMATE entry must request the factory with
   ``PrivacyTier.INTIMATE`` (the router then forces local), and an
   ``IntimateRoutingError`` must surface as a structured refusal, never a crash
   or a cloud call.
2. **Quotes are verbatim** — every returned ``quote`` is a substring of the
   input; model-supplied spans that are not are dropped, never trusted.
3. Corpus-grounded retrieval, audit logging, read-only wrt the corpus, clean
   degradation with no provider, and the care seam (#753).

The LLM and retrieval are injected — no live calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from creek.classify.llm.router import IntimateRoutingError
from creek.models import PrivacyTier
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.reflect import TOOL_NAME, reflect_tool

if TYPE_CHECKING:
    from pathlib import Path

_ENTRY = (
    "I keep circling the same fear: that if I rest, everything I built quietly "
    "falls apart. But today I noticed the garden grew while I slept."
)


class _RecordingFactory:
    """A tier-keyed LLM factory that records the tier it was asked for.

    Mirrors the production factory's shape ``(PrivacyTier) -> (str) -> str`` so
    a test can assert which tier drove routing without a live provider.
    """

    def __init__(self, response: str) -> None:
        """Store the canned LLM response and init the recorded tier."""
        self.response = response
        self.asked_tier: PrivacyTier | None = None

    def __call__(self, tier: PrivacyTier):
        """Record *tier* and return a constant LLM callable."""
        self.asked_tier = tier
        return lambda prompt: self.response


def _notes_payload(*notes: dict[str, str], essay: str | None = None) -> str:
    """Render an LLM JSON response carrying *notes* (and an optional essay)."""
    payload: dict[str, Any] = {"notes": list(notes)}
    if essay is not None:
        payload["essay"] = essay
    return json.dumps(payload)


def _vault(tmp_path: Path) -> Path:
    """Create a minimal Creek vault dir."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    return vault


def _no_retrieval(query: str, vault: Path, override: object) -> list[str]:
    """A retrieval stub returning no grounding fragments."""
    del query, vault, override
    return []


def _audit(vault: Path) -> list[dict[str, object]]:
    """Return parsed MCP audit-log entries under *vault*."""
    log = vault / MCP_AUDIT_RELPATH
    assert log.exists()
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# --- 1. INTIMATE-never-cloud routing -------------------------------------------------


def test_intimate_ceiling_requests_the_intimate_tier_from_the_factory(
    tmp_path: Path,
) -> None:
    """An INTIMATE ceiling must drive the factory with ``PrivacyTier.INTIMATE``."""
    factory = _RecordingFactory(
        _notes_payload({"quote": "I rest", "kind": "fear", "note": "ok"})
    )
    reflect_tool(
        vault_path=_vault(tmp_path),
        content="I rest sometimes.",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert factory.asked_tier is PrivacyTier.INTIMATE


def test_open_ceiling_requests_the_open_tier(tmp_path: Path) -> None:
    """An OPEN ceiling routes at the OPEN tier (cloud allowed)."""
    factory = _RecordingFactory(_notes_payload())
    reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert factory.asked_tier is PrivacyTier.OPEN


def test_all_ceiling_fails_closed_to_intimate_routing(tmp_path: Path) -> None:
    """``ALL`` admits intimate content, so routing must fail closed to INTIMATE."""
    factory = _RecordingFactory(_notes_payload())
    reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert factory.asked_tier is PrivacyTier.INTIMATE


def test_intimate_routing_error_becomes_a_refusal_not_a_crash(tmp_path: Path) -> None:
    """If the router refuses to route INTIMATE locally, reflect refuses cleanly."""

    def _raising_factory(tier: PrivacyTier):
        raise IntimateRoutingError("default is cloud")

    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content="something tender",
        llm_factory=_raising_factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert result["status"] == "refused"
    assert result["tool"] == TOOL_NAME


# --- 2. Verbatim-quote validation ----------------------------------------------------


def test_only_verbatim_quotes_survive(tmp_path: Path) -> None:
    """A note whose quote is not a substring of the entry is dropped."""
    factory = _RecordingFactory(
        _notes_payload(
            {
                "quote": "the garden grew while I slept",
                "kind": "reframe",
                "note": "yours",
            },
            {"quote": "a line the model invented", "kind": "reframe", "note": "nope"},
        )
    )
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    quotes = [n["quote"] for n in result["notes"]]
    assert "the garden grew while I slept" in quotes
    assert "a line the model invented" not in quotes


def test_note_shape_is_quote_kind_note(tmp_path: Path) -> None:
    """Each surviving note exposes exactly the contracted keys."""
    factory = _RecordingFactory(
        _notes_payload(
            {"quote": "the garden grew while I slept", "kind": "reframe", "note": "y"}
        )
    )
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    note = result["notes"][0]
    assert set(note) == {"quote", "kind", "note"}


def test_no_verbatim_notes_yields_empty_status(tmp_path: Path) -> None:
    """If every quote is hallucinated, the tool returns no notes (not garbage)."""
    factory = _RecordingFactory(
        _notes_payload({"quote": "entirely invented", "kind": "x", "note": "y"})
    )
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["notes"] == []


# --- 3. Grounding, audit, care, degradation ------------------------------------------


def test_retrieval_is_grounded_in_the_entry_and_ceiling(tmp_path: Path) -> None:
    """Retrieval is called with the entry text and a ceiling-derived override."""
    seen: dict[str, object] = {}

    def _recording_retrieve(query: str, vault: Path, override: object) -> list[str]:
        seen["query"] = query
        seen["override"] = override
        return ["a related fragment"]

    factory = _RecordingFactory(_notes_payload())
    reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_recording_retrieve,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert seen["query"] == _ENTRY
    assert seen["override"] is not None


def test_reflect_is_audit_logged(tmp_path: Path) -> None:
    """The call appends one audit entry tagged with the tool + consumer."""
    vault = _vault(tmp_path)
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="adepthood",
    )
    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["consumer"] == "adepthood"


def test_care_guard_escalation_skips_the_llm(tmp_path: Path) -> None:
    """When the care guard flags the entry, reflect escalates and skips the LLM."""
    factory = _RecordingFactory(_notes_payload())
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content="a hard entry",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
        care_guard=lambda text: "crisis markers present",
    )
    assert result["status"] == "escalate"
    assert "reason" in result
    assert factory.asked_tier is None  # the LLM was never reached


def test_empty_content_is_refused(tmp_path: Path) -> None:
    """No content and no entry_ref is a structured refusal, not a crash."""
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content="   ",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] in {"refused", "empty"}


def test_reflect_is_read_only_wrt_corpus(tmp_path: Path) -> None:
    """Only the audit log is written; no fragments are created."""
    vault = _vault(tmp_path)
    before = {p for p in vault.rglob("*") if p.is_file()}
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    created = {p for p in vault.rglob("*") if p.is_file()} - before
    assert all("audit" in str(p) for p in created)


def test_entry_ref_resolves_a_fragment_body(tmp_path: Path) -> None:
    """An ``entry_ref`` loads the fragment's body as the entry to reflect on."""
    vault = _vault(tmp_path)
    frag_dir = vault / "01-Fragments" / "Notes"
    frag_dir.mkdir(parents=True)
    body = "the garden grew while I slept"
    (frag_dir / "f.md").write_text(
        f"---\nid: frag-xyz\n---\n{body}\n", encoding="utf-8"
    )
    factory = _RecordingFactory(
        _notes_payload({"quote": body, "kind": "reframe", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-xyz",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["notes"][0]["quote"] == body


def test_fenced_json_response_is_parsed(tmp_path: Path) -> None:
    """A response wrapped in a ```json fence is unwrapped and parsed."""
    inner = _notes_payload(
        {"quote": "the garden grew while I slept", "kind": "reframe", "note": "y"}
    )
    factory = _RecordingFactory(f"```json\n{inner}\n```")
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"


def test_yaml_response_falls_back_from_json(tmp_path: Path) -> None:
    """A non-JSON but valid-YAML response is parsed via the YAML fallback + essay."""
    yaml_text = (
        "notes:\n"
        "  - quote: the garden grew while I slept\n"
        "    kind: reframe\n"
        "    note: a gentle truth\n"
        "essay: A short reflection.\n"
    )
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=_RecordingFactory(yaml_text),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["essay"] == "A short reflection."


def test_unparseable_response_yields_no_notes(tmp_path: Path) -> None:
    """A response that is neither JSON nor a YAML mapping degrades to no notes."""
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=_RecordingFactory("- just\n- a\n- list"),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["notes"] == []


def test_entry_ref_intimate_fragment_routes_local_despite_open_ceiling(
    tmp_path: Path,
) -> None:
    """An INTIMATE fragment routes at INTIMATE even under an OPEN ceiling (#751 review).

    The routing tier follows the entry's *actual* classified ``privacy_tier``,
    never the (possibly lower) caller-declared ceiling — so genuinely intimate
    journal content can never be reflected through a cloud model.
    """
    vault = _vault(tmp_path)
    frag_dir = vault / "01-Fragments" / "Notes"
    frag_dir.mkdir(parents=True)
    body = "the garden grew while I slept"
    (frag_dir / "f.md").write_text(
        f"---\nid: frag-intimate\nprivacy_tier: intimate\n---\n{body}\n",
        encoding="utf-8",
    )
    factory = _RecordingFactory(_notes_payload())
    reflect_tool(
        vault_path=vault,
        entry_ref="frag-intimate",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert factory.asked_tier is PrivacyTier.INTIMATE


def test_malformed_yaml_response_does_not_crash(tmp_path: Path) -> None:
    """A non-JSON, non-YAML response degrades to no notes (catches YAMLError)."""
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=_RecordingFactory("{notes: [oops, unbalanced"),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["notes"] == []


def test_entry_ref_not_found_has_a_distinct_reason(tmp_path: Path) -> None:
    """A missing ``entry_ref`` refuses with a debuggable, distinct reason."""
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        entry_ref="does-not-exist",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert result["reason"] == "entry_ref not found"
