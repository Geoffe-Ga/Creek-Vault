"""``creek.reflect`` MCP tool — anchored Higher-Self margin notes (#751).

The tool takes one journal entry plus a privacy-tier ceiling and returns
``{notes: [{quote, kind, note}], essay?}`` grounded in the user's corpus. The
contract this suite pins, in order of stakes:

1. **The ceiling is enforced on read (#846)** — an ``entry_ref`` whose
   classified tier exceeds the caller's ``privacy_tier_ceiling`` is refused
   *before* the care guard and the model, with the tier-neutral reason
   ``"entry_ref tier exceeds ceiling"``; not one span of that fragment may
   appear in the response. Raw inline ``content`` is the caller's own text and
   is never gated; a fragment carrying no ``privacy_tier`` fails closed.
2. **INTIMATE never egresses** — the LLM callable is obtained from a tier-keyed
   factory; an INTIMATE entry must request the factory with
   ``PrivacyTier.INTIMATE`` (the router then forces local), and an
   ``IntimateRoutingError`` must surface as a structured refusal, never a crash
   or a cloud call. Pinned end-to-end through ``reflect_tool``, plus directly on
   ``_routing_tier`` for the defense-in-depth fold-in that the #846 gate above
   it makes unreachable from the public API.
3. **Quotes are verbatim** — every returned ``quote`` is a substring of the
   input; model-supplied spans that are not are dropped, never trusted.
4. Corpus-grounded retrieval, audit logging, read-only wrt the corpus, clean
   degradation with no provider, and the care seam (#753).

The LLM and retrieval are injected — no live calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from creek.care.guardrail import CARE_SIGNAL
from creek.classify.llm.router import IntimateRoutingError
from creek.models import PrivacyTier
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling, to_privacy_override
from creek_mcp.tools.reflect import TOOL_NAME, _routing_tier, reflect_tool

if TYPE_CHECKING:
    from pathlib import Path

_ENTRY = (
    "I keep circling the same fear: that if I rest, everything I built quietly "
    "falls apart. But today I noticed the garden grew while I slept."
)


class _RecordingFactory:
    """A tier-keyed LLM factory recording the tier *and prompt* it was given.

    Mirrors the production factory's shape ``(PrivacyTier) -> (str) -> str`` so
    a test can assert which tier drove routing without a live provider.

    ``prompt`` captures what actually crossed to the model, which the response
    alone cannot show: a regression that folded above-ceiling fragment text
    into the prompt while leaving the returned notes clean would egress that
    text to the provider and still look correct from the outside.
    """

    def __init__(self, response: str) -> None:
        """Store the canned LLM response and init the recorded tier + prompt."""
        self.response = response
        self.asked_tier: PrivacyTier | None = None
        self.prompt: str | None = None

    def __call__(self, tier: PrivacyTier):
        """Record *tier* and return an LLM callable that records its prompt."""
        self.asked_tier = tier

        def _llm(prompt: str) -> str:
            self.prompt = prompt
            return self.response

        return _llm


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


def _write_fragment(
    vault: Path, frag_id: str, body: str, tier: str | None = None
) -> None:
    """Write a fragment markdown file that an ``entry_ref`` can resolve.

    Args:
        vault: Vault root as created by :func:`_vault`.
        frag_id: The ``id`` front-matter value, i.e. the ``entry_ref`` to pass.
        body: The fragment body, which becomes the entry being reflected on.
        tier: The ``privacy_tier`` front-matter value. ``None`` omits the key
            entirely, which the tool must treat as INTIMATE (fail closed) —
            distinct from an explicit ``"unclassified"``.
    """
    frag_dir = vault / "01-Fragments" / "Notes"
    frag_dir.mkdir(parents=True, exist_ok=True)
    tier_line = "" if tier is None else f"privacy_tier: {tier}\n"
    (frag_dir / f"{frag_id}.md").write_text(
        f"---\nid: {frag_id}\n{tier_line}---\n{body}\n", encoding="utf-8"
    )


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


def test_personal_ceiling_requests_the_personal_tier(tmp_path: Path) -> None:
    """A PERSONAL ceiling routes at the PERSONAL tier (pins the middle mapping)."""
    factory = _RecordingFactory(_notes_payload())
    reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert factory.asked_tier is PrivacyTier.PERSONAL


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
    """Retrieval is called with the entry text and *this* ceiling's override.

    The override is asserted to equal ``to_privacy_override(PERSONAL)``, not
    merely to be non-``None``: grounding retrieval is the second read path into
    the corpus, so passing the wrong (broader) override would pull
    above-ceiling fragments into the prompt while every other assertion here
    still held.
    """
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
    assert seen["override"] == to_privacy_override(TierCeiling.PERSONAL)


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


def test_escalation_carries_the_structured_care_signal(tmp_path: Path) -> None:
    """Escalation never dead-ends: it returns the human-support care signal (#753)."""
    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content="a hard entry",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
        care_guard=lambda text: "acute_distress_markers",
    )
    assert result["status"] == "escalate"
    assert result["care_signal"] == CARE_SIGNAL
    assert result["care_signal"]["kind"] == "acute_distress"
    assert result["care_signal"]["resources"]  # points to human/professional support


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
    """An ``entry_ref`` loads the fragment's body as the entry to reflect on.

    The fixture is classified ``open`` so this stays a pure resolution test:
    under the #846 read-side gate an *untiered* fragment fails closed to
    INTIMATE and would be refused before resolution could be observed.
    """
    vault = _vault(tmp_path)
    body = "the garden grew while I slept"
    _write_fragment(vault, "frag-xyz", body, tier="open")
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


# --- Read-side tier ceiling (#846) ---------------------------------------------------

# A phrase that appears nowhere else, so its presence in a response can only mean
# the fragment body leaked through the gate.
_INTIMATE_BODY = "the ultramarine dovecote unlatched at midnight"
_INTIMATE_SPAN = "ultramarine dovecote"
_REFUSAL_REASON = "entry_ref tier exceeds ceiling"


class _RecordingGuard:
    """A care guard that records being consulted and always flags distress.

    Lets a test tell "the guard declined" apart from "the guard was never
    asked", which is exactly what pinning the ceiling-before-care order needs.
    """

    def __init__(self) -> None:
        """Init the consultation counter."""
        self.calls = 0

    def __call__(self, text: str) -> str | None:
        """Record the consultation and flag *text* as acute distress."""
        del text
        self.calls += 1
        return "acute_distress_markers"


def test_entry_ref_above_ceiling_is_refused_not_reflected(tmp_path: Path) -> None:
    """An INTIMATE ``entry_ref`` under an OPEN ceiling leaks nothing (#846).

    The caller declared ``privacy_tier_ceiling=open``; the fragment is
    classified ``intimate``. Reflecting it hands that caller *verified-verbatim*
    spans of intimate content in ``notes[].quote`` plus free model prose in
    ``essay`` — the exact leak #846 reports. The refusal must land before the
    model, and no span of the body may survive anywhere in the response.

    The audit assertion pins ordering, not logging: ``reflect_tool`` appends to
    the MCP audit log *above* the gate, which is the only reason a refused read
    is auditable at all. A refactor that moved the append below
    ``_resolve_entry`` would silently stop recording exactly the attempts an
    operator most needs to see — probes at fragments the caller is not admitted
    to — with no other test failing. The count is pinned alongside it because
    ``reflect.py`` promises *exactly one* append per call: a refusal-path
    append added "for visibility" would make refused reads distinguishable
    from admitted ones by record count alone, re-opening the read oracle the
    tier-neutral reason closes.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-intimate", _INTIMATE_BODY, tier="intimate")
    factory = _RecordingFactory(
        _notes_payload(
            {"quote": _INTIMATE_BODY, "kind": "fear", "note": "a tender read"},
            essay=f"You wrote that {_INTIMATE_BODY}.",
        )
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-intimate",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert _INTIMATE_SPAN not in json.dumps(result), "intimate content egressed"
    assert result["status"] == "refused"
    assert result["tool"] == TOOL_NAME
    assert result["reason"] == _REFUSAL_REASON
    assert factory.asked_tier is None  # the model was never reached
    # The refused attempt is still audited — the append sits above the gate —
    # and exactly once: the refusal path adds no second record.
    entries = _audit(vault)
    assert len(entries) == 1
    last = entries[-1]
    assert last["tool"] == TOOL_NAME
    assert last["args_summary"] == {"has_entry_ref": True}


def test_personal_entry_ref_is_refused_under_open_ceiling(tmp_path: Path) -> None:
    """A PERSONAL fragment is refused under an OPEN ceiling.

    Pins the gate as a *rank comparison* against the ceiling rather than an
    INTIMATE-only special case: ``personal`` outranks ``open``, so it refuses
    with the same tier-neutral reason.
    """
    vault = _vault(tmp_path)
    body = "the ledger of small refusals I keep"
    _write_fragment(vault, "frag-personal", body, tier="personal")
    factory = _RecordingFactory(
        _notes_payload({"quote": body, "kind": "pattern", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-personal",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert result["reason"] == _REFUSAL_REASON
    assert factory.asked_tier is None


def test_personal_entry_ref_is_allowed_under_personal_ceiling(tmp_path: Path) -> None:
    """Equal rank is admitted: a PERSONAL fragment under a PERSONAL ceiling reflects.

    The other side of the gate. An over-broad check that refused equal-rank
    entries would silently break reflection for its primary caller, so the
    admitted path asserts real notes came back, not merely a non-refusal.
    """
    vault = _vault(tmp_path)
    body = "the ledger of small refusals I keep"
    _write_fragment(vault, "frag-personal", body, tier="personal")
    factory = _RecordingFactory(
        _notes_payload({"quote": body, "kind": "pattern", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-personal",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "ok"
    assert result["notes"][0]["quote"] == body


def test_intimate_entry_ref_is_allowed_under_intimate_ceiling_and_routes_local(
    tmp_path: Path,
) -> None:
    """An admitted INTIMATE fragment still routes at ``PrivacyTier.INTIMATE``.

    Replaces the former
    ``test_entry_ref_intimate_fragment_routes_local_despite_open_ceiling``:
    under #846 a *low*-ceiling call on an INTIMATE fragment is refused
    outright, which is strictly stronger than "it routes local". The
    routes-local guarantee still has to hold for the calls that *are* admitted,
    so it is pinned here at the ceiling that admits them.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-intimate", _INTIMATE_BODY, tier="intimate")
    factory = _RecordingFactory(
        _notes_payload({"quote": _INTIMATE_BODY, "kind": "fear", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-intimate",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert result["status"] == "ok"
    assert factory.asked_tier is PrivacyTier.INTIMATE


@pytest.mark.parametrize(
    ("ceiling", "entry_tier", "expected"),
    [
        # The fold-in proper: the entry outranks the ceiling, so the entry wins.
        (TierCeiling.OPEN, PrivacyTier.INTIMATE, PrivacyTier.INTIMATE),
        (TierCeiling.OPEN, PrivacyTier.PERSONAL, PrivacyTier.PERSONAL),
        (TierCeiling.PERSONAL, PrivacyTier.INTIMATE, PrivacyTier.INTIMATE),
        # The ceiling wins when it is the more sensitive of the two...
        (TierCeiling.INTIMATE, PrivacyTier.OPEN, PrivacyTier.INTIMATE),
        (TierCeiling.ALL, PrivacyTier.OPEN, PrivacyTier.INTIMATE),
        # ...and on a rank tie, which keeps ``unclassified`` from downgrading
        # the routing tier below the ceiling's own.
        (TierCeiling.OPEN, PrivacyTier.UNCLASSIFIED, PrivacyTier.OPEN),
        # Raw inline ``content`` has no classification: the ceiling alone routes.
        (TierCeiling.OPEN, None, PrivacyTier.OPEN),
        (TierCeiling.PERSONAL, None, PrivacyTier.PERSONAL),
        (TierCeiling.INTIMATE, None, PrivacyTier.INTIMATE),
        # ``ALL`` admits intimate content, so it must route as INTIMATE.
        (TierCeiling.ALL, None, PrivacyTier.INTIMATE),
    ],
)
def test_routing_tier_takes_the_more_sensitive_of_entry_and_ceiling(
    ceiling: TierCeiling, entry_tier: PrivacyTier | None, expected: PrivacyTier
) -> None:
    """``_routing_tier`` routes at the more sensitive of entry tier and ceiling.

    Asserted against the helper directly rather than through
    :func:`reflect_tool`, and that is the whole point. Since the #846 gate
    refuses every ``entry_ref`` whose classified tier exceeds the ceiling, no
    call through the public API can still reach ``_routing_tier`` with an
    above-ceiling *entry_tier*: every admitted ``entry_tier`` is by
    construction ``<= _CEILING_ROUTING_TIER[ceiling]``, so through the tool the
    fold-in is behaviourally identical to ``return ceiling_tier``. Every
    end-to-end test in this file would therefore still pass if the ``max`` were
    "simplified" away.

    The fold-in is nonetheless the load-bearing INTIMATE-never-egresses
    guarantee that ``reflect.py`` says must not be removed: it is the layer
    that keeps an INTIMATE entry off a cloud provider if the read-side gate
    above it ever regresses, is reordered, or is bypassed by a future caller
    that resolves a fragment without going through the gate. Defense in depth
    is unreachable by construction from outside; only a direct test can
    falsify it.

    Args:
        ceiling: The caller's declared ceiling.
        entry_tier: The entry's classified tier, or ``None`` for raw content.
        expected: The tier the router must be asked for.
    """
    assert _routing_tier(ceiling, entry_tier) is expected


def test_intimate_entry_ref_is_allowed_under_all_ceiling(tmp_path: Path) -> None:
    """``ALL`` admits every tier, so an INTIMATE ``entry_ref`` reflects under it.

    ``ALL`` sits above ``INTIMATE`` in the ceiling ranking and short-circuits
    :func:`creek_mcp.tier_ceiling.tier_allowed` to ``True``; the gate must not
    turn that into an off-by-one refusal.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-intimate", _INTIMATE_BODY, tier="intimate")
    factory = _RecordingFactory(
        _notes_payload({"quote": _INTIMATE_BODY, "kind": "fear", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-intimate",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "ok"


def test_explicitly_unclassified_entry_ref_is_allowed_under_open_ceiling(
    tmp_path: Path,
) -> None:
    """An explicit ``privacy_tier: unclassified`` is admitted at the OPEN ceiling.

    ``unclassified`` shares rank 0 with ``open`` in the canonical ranking, so a
    fragment that was *classified as* unclassified is admissible to the most
    restrictive ceiling. Paired with the fail-closed test below.
    """
    vault = _vault(tmp_path)
    body = "a note I never got round to sorting"
    _write_fragment(vault, "frag-unclassified", body, tier="unclassified")
    factory = _RecordingFactory(
        _notes_payload({"quote": body, "kind": "pattern", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-unclassified",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["notes"][0]["quote"] == body


@pytest.mark.parametrize("ceiling", [TierCeiling.OPEN, TierCeiling.PERSONAL])
def test_untiered_entry_ref_is_refused_fail_closed(
    tmp_path: Path, ceiling: TierCeiling
) -> None:
    """A fragment with **no** ``privacy_tier`` key is refused (fail closed).

    Both ceilings are exercised because the OPEN case alone under-pins the
    default: at OPEN *any* fail-closed value above rank 0 refuses, so a
    weakening of ``_fragment_tier`` from INTIMATE to PERSONAL would slip
    through. The PERSONAL ceiling admits ``personal`` and refuses only
    ``intimate``, so it is what actually pins the default at the
    most-restrictive tier.

    Deliberately paired with
    :func:`test_explicitly_unclassified_entry_ref_is_allowed_under_open_ceiling`:
    an *explicit* ``unclassified`` is a classification decision and ranks 0,
    whereas a *missing* key means the fragment was never classified at all.
    ``_fragment_tier`` treats that as INTIMATE, so the gate must refuse it.

    Scope, stated precisely: this fail-closed covers fragments missing the key
    *entirely* — hand-edited, legacy, or otherwise not written by the pipeline.
    It is **not** the guard for a half-ingested vault: ``creek/vault/writer.py``
    serialises via ``model_dump(mode="json")`` and ``Fragment.privacy_tier``
    defaults to ``PrivacyTier.UNCLASSIFIED``, so every pipeline-written
    pre-classification fragment carries an *explicit* ``privacy_tier:
    unclassified``, which ranks 0 and is admitted at the OPEN ceiling (the test
    above). Those fragments are governed by the separate, systemic
    "``unclassified`` ranks as open" policy in :mod:`creek_mcp.tier_ceiling` /
    :mod:`creek.classify.privacy_filter` — deliberate, and out of scope here.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: A ceiling that must refuse the untiered fragment. ``PERSONAL``
            is the discriminating case; ``OPEN`` keeps the original coverage.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-untiered", _INTIMATE_BODY)
    factory = _RecordingFactory(
        _notes_payload({"quote": _INTIMATE_BODY, "kind": "fear", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-untiered",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=ceiling,
    )
    assert _INTIMATE_SPAN not in json.dumps(result), "unclassified content egressed"
    assert result["status"] == "refused"
    assert result["reason"] == _REFUSAL_REASON
    assert factory.asked_tier is None


def test_unrecognised_entry_ref_tier_is_refused_fail_closed(tmp_path: Path) -> None:
    """A ``privacy_tier`` value outside the vocabulary is refused (fail closed).

    The third state of the tier field, distinct from both neighbours above: the
    key is *present* but carries a value this build cannot parse — a hand edit,
    a typo, or a vocabulary from a newer schema. ``_fragment_tier`` reaches its
    ``except ValueError`` arm, which must yield INTIMATE: an unknown
    classification says nothing about sensitivity, so admitting it would hand
    out the one fragment whose tier nobody can vouch for.

    The ceiling is ``PERSONAL`` rather than ``OPEN`` on purpose — it admits
    ``personal`` and refuses only ``intimate``, so it pins the ``except`` arm at
    the most-restrictive tier instead of merely "something above open".
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-bogus-tier", _INTIMATE_BODY, tier="not-a-tier")
    factory = _RecordingFactory(
        _notes_payload({"quote": _INTIMATE_BODY, "kind": "fear", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-bogus-tier",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert _INTIMATE_SPAN not in json.dumps(result), "unparseable-tier content egressed"
    assert result["status"] == "refused"
    assert result["reason"] == _REFUSAL_REASON
    assert factory.asked_tier is None


def test_raw_content_is_never_gated_by_the_ceiling(tmp_path: Path) -> None:
    """Raw inline ``content`` is the caller's own text and is never gated.

    The read-side gate exists to stop a caller *reading corpus fragments* above
    their ceiling. Text they supplied in the call is already in their hands, so
    gating it would refuse every ordinary journal reflection.
    """
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
    assert "reason" not in result  # never refused
    assert factory.asked_tier is PrivacyTier.OPEN


def test_content_wins_over_entry_ref_and_is_not_a_ceiling_bypass(
    tmp_path: Path,
) -> None:
    """Supplying both ``content`` and ``entry_ref`` reads the fragment not at all.

    ``_resolve_entry`` returns ``(content, None)`` the moment ``content`` is
    non-blank, so a call carrying *both* an inline entry and an ``entry_ref``
    pointed at an above-ceiling fragment never opens that fragment: there is no
    ``entry_tier``, the gate correctly does not fire, and nothing of the
    fragment can reach the response or the model.

    This pins the precedence as a **non-bypass**, not merely as a convenience.
    Its shape is exactly what an attempted bypass would look like — pair a
    harmless ``content`` with an ``entry_ref`` above your ceiling and see what
    comes back. The safety therefore rests on ``content`` short-circuiting the
    read *before* the fragment is loaded; an "improvement" that resolved
    ``entry_ref`` first (or merged the two) would turn this call into the leak
    #846 closed, and routing would follow an unread fragment's tier.

    The prompt is asserted alongside the response because the response alone
    only shows what came *back*. A merge that appended the fragment's text to
    the prompt while leaving ``entry_tier`` at ``None`` would egress intimate
    content to the provider — the leak in full — and still return clean,
    verbatim-checked notes drawn from the inline entry.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-intimate", _INTIMATE_BODY, tier="intimate")
    factory = _RecordingFactory(
        _notes_payload(
            {"quote": "the garden grew while I slept", "kind": "reframe", "note": "y"}
        )
    )
    result = reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        entry_ref="frag-intimate",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    # The fragment was never read, so not a span of it can appear.
    assert _INTIMATE_SPAN not in json.dumps(result), "intimate content egressed"
    assert result["notes"][0]["quote"] == "the garden grew while I slept"
    # Nor could it reach the model: the prompt carries only the inline entry.
    assert factory.prompt is not None
    assert _INTIMATE_SPAN not in factory.prompt, "intimate content reached the model"
    assert _ENTRY in factory.prompt  # the caller's own text is what was sent
    # Routed by the ceiling, not by the unread fragment's INTIMATE tier.
    assert factory.asked_tier is PrivacyTier.OPEN


def test_care_guard_is_not_consulted_for_an_above_ceiling_entry(
    tmp_path: Path,
) -> None:
    """The ceiling is checked *before* the care guard, which never runs (#846).

    Ordering is load-bearing. If care ran first, an unadmitted caller would get
    ``status: "escalate"`` instead of ``"refused"`` — a one-bit oracle telling
    them that a fragment they are not allowed to read carries acute-distress
    markers. The gate must therefore sit above the care seam, and the guard
    must not even be handed the entry text.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-intimate", _INTIMATE_BODY, tier="intimate")
    guard = _RecordingGuard()
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-intimate",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
        care_guard=guard,
    )
    assert result["status"] == "refused"
    assert result["reason"] == _REFUSAL_REASON
    assert guard.calls == 0


def test_care_guard_still_runs_for_an_admitted_entry_ref(tmp_path: Path) -> None:
    """An admitted ``entry_ref`` still passes through the care boundary (#753).

    The #846 gate must not disable the care seam: with a ceiling that admits
    the fragment, a firing guard escalates with the structured care signal
    rather than reflecting.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-intimate", _INTIMATE_BODY, tier="intimate")
    guard = _RecordingGuard()
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-intimate",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.INTIMATE,
        care_guard=guard,
    )
    assert result["status"] == "escalate"
    assert result["care_signal"] == CARE_SIGNAL
    assert guard.calls == 1
