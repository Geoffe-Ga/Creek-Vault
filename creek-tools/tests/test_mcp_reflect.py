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
   degradation with no provider, the care seam (#753), and tolerance of
   malformed fragment frontmatter under ``01-Fragments`` (#847).
5. **The default (production) grounder actually grounds (#964)** — every other
   test in this file injects a ``retrieve=`` stub, so the retrieval
   ``build_server`` really wires had zero coverage and silently returned no
   grounding at all. The final section drives that path with no ``retrieve=``:
   it feeds the prompt corpus fragment **titles** (never bodies), admits
   exactly the within-ceiling corpus and leaves no trace of anything above it,
   routes at a tier dominating every fragment it retrieved, and on a retrieval
   failure degrades to ``(none)`` while logging the exception's *type name*
   only.

The LLM and retrieval are injected — no live calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest

from creek.author.agents import RetrievalSpecialist
from creek.care.guardrail import CARE_SIGNAL
from creek.classify.llm.router import IntimateRoutingError
from creek.link.embeddings import EmbeddingLinker
from creek.models import PrivacyTier
from creek_mcp import tier_ceiling
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.compiled_pages import RelatedCompiled
from creek_mcp.server import Transport, build_server
from creek_mcp.tier_ceiling import TierCeiling, to_privacy_override
from creek_mcp.tools.reflect import (
    TOOL_NAME,
    GroundingSession,
    _default_retrieve,
    _routing_tier,
    reflect_tool,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from creek.author.models import EvidenceBundle

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
        # ...and ``unclassified`` now *raises* the routing tier to ``personal``
        # rather than tying with ``open`` (#961): it ranks with ``personal``,
        # and is normalised into the routing vocabulary because "unclassified"
        # is not a routing key. This row is unreachable through ``reflect_tool``
        # — the #846 gate refuses an unclassified ``entry_ref`` at ``open``
        # before ``_routing_tier`` runs — which is why it is asserted against
        # the helper directly (see the docstring below).
        (TierCeiling.OPEN, PrivacyTier.UNCLASSIFIED, PrivacyTier.PERSONAL),
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


@pytest.mark.parametrize(
    ("ceiling", "admitted"),
    [
        (TierCeiling.OPEN, False),
        (TierCeiling.PERSONAL, True),
    ],
)
def test_explicitly_unclassified_entry_ref_is_refused_under_open_ceiling(
    tmp_path: Path,
    ceiling: TierCeiling,
    admitted: bool,
) -> None:
    """An explicit ``privacy_tier: unclassified`` needs a PERSONAL ceiling (#961).

    Inverted by #961. This test previously asserted the OPEN ceiling *admitted*
    an explicitly-unclassified fragment, because ``unclassified`` shared rank 0
    with ``open`` in ``creek_mcp.tier_ceiling._TIER_RANK``. It now ranks 1, with
    ``personal``: an untiered fragment is content nobody has vouched for, and
    every pipeline-written pre-classification fragment carries an *explicit*
    ``privacy_tier: unclassified`` (the ``Fragment`` model default), so the old
    policy made a whole freshly-ingested vault readable through ``creek.reflect``
    at ``ceiling=open``.

    Both halves of the new policy are asserted, and the PERSONAL row is the
    load-bearing one. A refusal at OPEN alone would be satisfied by any rank
    above 0 — including a fail-closed-to-INTIMATE reading — which would erase
    the distinction this file depends on: an *explicit* ``unclassified``
    (rank 1, admitted at ``personal``) versus a *missing* ``privacy_tier`` key
    (fails closed to INTIMATE, refused at ``personal`` — see
    :func:`test_untiered_entry_ref_is_refused_fail_closed` below). The PERSONAL
    row is what keeps those two cases distinguishable.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: The caller's declared ceiling.
        admitted: Whether *ceiling* must admit the unclassified fragment.
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
        privacy_tier_ceiling=ceiling,
    )
    if admitted:
        assert result["status"] == "ok"
        assert result["notes"][0]["quote"] == body
    else:
        assert body not in json.dumps(result), "unclassified content egressed"
        assert result["status"] == "refused"
        assert result["reason"] == _REFUSAL_REASON
        assert factory.asked_tier is None


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
    :func:`test_explicitly_unclassified_entry_ref_is_refused_under_open_ceiling`:
    an *explicit* ``unclassified`` is a classification decision and ranks 1
    (with ``personal``, #961), whereas a *missing* key means the fragment was
    never classified at all. ``_fragment_tier`` treats that as INTIMATE, so the
    gate must refuse it at ``personal`` too.

    That pairing is what makes the PERSONAL row discriminating, and #961 made it
    sharper rather than weaker. Both cases are now refused at OPEN, so only the
    PERSONAL ceiling still tells them apart — and there they diverge outright:
    an explicit ``unclassified`` is **admitted** while a missing key is
    **refused**. Before #961 the two differed at OPEN and agreed nowhere else;
    now the discrimination sits exactly on the rank boundary the fail-closed
    default is supposed to clear.

    Scope, stated precisely: this fail-closed covers fragments missing the key
    *entirely* — hand-edited, legacy, or otherwise not written by the pipeline.
    It is **not** the guard for a half-ingested vault: ``creek/vault/writer.py``
    serialises via ``model_dump(mode="json")`` and ``Fragment.privacy_tier``
    defaults to ``PrivacyTier.UNCLASSIFIED``, so every pipeline-written
    pre-classification fragment carries an *explicit* ``privacy_tier:
    unclassified``. Those fragments are governed by the separate, systemic
    "``unclassified`` ranks with ``personal``" policy shared by
    :mod:`creek_mcp.tier_ceiling` and :mod:`creek.classify.privacy_filter`
    (#876/#961) — deliberate, and out of scope here.

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


# --- Malformed fragment frontmatter (#847) --------------------------------------------

# Corrupt files written alongside a resolvable target. Three rather than one so the
# skip has to survive *repeated* failures and still reach the far side of the walk.
# The names carry no ordering meaning: traversal order is a filesystem detail, and
# the guarantee is carried by :func:`_assert_sweep_reaches_every_fragment` instead.
_CORRUPT_SIBLINGS = ("bad-one", "bad-two", "bad-three")
_UNDECODABLE_SIBLINGS = ("undecodable-one", "undecodable-two", "undecodable-three")

# A phrase that appears only inside corrupt files, so finding it in a response can
# only mean unparseable content was salvaged instead of skipped.
_CORRUPT_BODY = "a fragment nobody can parse"


def _frag_path(vault: Path, name: str) -> Path:
    """Return the ``01-Fragments/Notes`` path for *name*, creating the directory.

    Every fragment in this section lands in the same directory
    :func:`_write_fragment` uses, so it is swept by exactly the same ``rglob``
    that resolves an ``entry_ref`` — and the sweep assertions below have to name
    those exact paths back. Defining the location once keeps the two in step.

    Args:
        vault: Vault root as created by :func:`_vault`.
        name: The file stem; ``.md`` is appended.

    Returns:
        ``<vault>/01-Fragments/Notes/<name>.md``. The parent directory exists on
        return; the entry itself is not created.
    """
    frag_dir = vault / "01-Fragments" / "Notes"
    frag_dir.mkdir(parents=True, exist_ok=True)
    return frag_dir / f"{name}.md"


def _spy_on_frontmatter_load(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every path handed to ``frontmatter.load``, delegating to the real one.

    ``_resolve_entry`` does a lazy ``import frontmatter`` inside its own body and
    then calls ``frontmatter.load(path)``, so the attribute is looked up on the
    module object at call time and patching it here is picked up. The real loader
    still runs — including raising — so nothing about the behaviour under test
    changes; this only observes. Deliberately not ``caplog``: asserting on log
    text would couple the suite to a debug message's phrasing rather than to the
    fact that the load was attempted.

    Args:
        monkeypatch: The builtin pytest fixture; it restores the real
            ``frontmatter.load`` at teardown.

    Returns:
        The live record — ``str(path)`` per call, in call order. Read it after
        the call under test.
    """
    real_load = frontmatter.load
    loaded: list[str] = []

    def _spy(fd: Any, *args: Any, **kwargs: Any) -> Any:
        """Record *fd*, then delegate to (and raise exactly like) the real load."""
        loaded.append(str(fd))
        return real_load(fd, *args, **kwargs)

    monkeypatch.setattr(frontmatter, "load", _spy)
    return loaded


def _assert_sweep_reaches_every_fragment(
    vault: Path, monkeypatch: pytest.MonkeyPatch, *names: str
) -> None:
    """Assert one full sweep of *vault* opens every named fragment and survives.

    ORDERING HAZARD, and why this exists. ``rglob`` yields ``os.scandir`` order.
    Nothing in ``pathlib`` sorts it and no mainstream filesystem promises
    anything about it — it is hash-ordered on APFS and XFS, roughly insertion-
    ordered on ext4. So in a vault that also holds a resolvable target,
    ``_resolve_entry`` may reach the ``id`` match and return *before* it ever
    opens a corrupt file, and a test that only checks the target's outcome then
    passes without entering the skip path at all. That is a false green, and no
    naming scheme fixes it — do not try to "strengthen" these tests by adding
    more or better-sorting file names.

    The guarantee is carried by an assertion instead. This probes with an
    ``entry_ref`` that matches nothing, which cannot early-return and therefore
    must exhaust the directory on every platform, and then asserts each named
    file really was handed to ``frontmatter.load``. Reaching *all* of them also
    pins ``continue`` as the skip's control flow: an implementation that
    abandoned the walk at the first bad file would record only one.

    Note:
        The probe is a real ``reflect_tool`` call and appends its own audit
        record, so assert audit-log counts *before* calling this.

    Args:
        vault: Vault root as created by :func:`_vault`.
        monkeypatch: The builtin pytest fixture, forwarded to the load spy.
        *names: Fragment stems, as passed to the writers, that the sweep must
            reach.
    """
    loaded = _spy_on_frontmatter_load(monkeypatch)
    probe = reflect_tool(
        vault_path=vault,
        entry_ref="does-not-exist",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert probe["status"] == "refused"
    assert probe["reason"] == "entry_ref not found"
    for name in names:
        path = str(_frag_path(vault, name))
        assert path in loaded, f"never handed to frontmatter.load: {path}"


def _write_malformed_fragment(vault: Path, name: str, body: str) -> None:
    """Write a fragment whose YAML front matter cannot be parsed at all.

    The front matter opens a flow sequence and never closes it, so
    ``frontmatter.load`` fails at the *scanner* level and raises
    ``yaml.YAMLError`` rather than returning partial metadata.

    Args:
        vault: Vault root as created by :func:`_vault`.
        name: The file stem, and the ``id`` the unparseable front matter
            nominally carries — it must never become resolvable.
        body: The text below the front matter.
    """
    _frag_path(vault, name).write_text(
        f"---\nfoo: [unterminated\nid: {name}\n---\n{body}\n", encoding="utf-8"
    )


def _write_undecodable_fragment(vault: Path, name: str) -> None:
    """Write a fragment file whose raw bytes are not valid UTF-8 at all.

    ``frontmatter.load`` decodes the file *before* any YAML parser sees it, so
    this one raises ``UnicodeDecodeError`` rather than ``yaml.YAMLError`` — the
    sibling failure mode that proves the skip cannot be a narrow ``except
    yaml.YAMLError``.

    Args:
        vault: Vault root as created by :func:`_vault`.
        name: The file stem. Nothing in the file is decodable, so it must never
            become resolvable as an ``entry_ref``.
    """
    _frag_path(vault, name).write_bytes(b"---\n\xff\xfe: x\n---\nbody\n")


def _write_nonstring_key_fragment(vault: Path, name: str) -> None:
    """Write a fragment whose front matter parses but carries a non-string key.

    A bare ``2024-05-01:`` line is resolved by YAML's safe loader to a
    ``datetime.date`` key, and ``frontmatter.loads`` splats the parsed mapping as
    ``Post(content, handler, **metadata)`` — so the load dies one layer *above*
    YAML with a bare ``TypeError: keywords must be strings``. A ``TypeError`` is
    not a ``yaml.YAMLError`` (the YAML parsed fine), not a ``ValueError`` and not
    an ``OSError``, so the narrow tuple used at other call sites in this
    codebase, ``except (OSError, ValueError, yaml.YAMLError)``, would let it
    escape. This is the sharpest reason the skip must be ``except Exception``.
    A date key in hand-edited front matter is an ordinary vault accident.

    Args:
        vault: Vault root as created by :func:`_vault`.
        name: The file stem, and the ``id`` the front matter nominally carries —
            the load fails before that ``id`` is reachable, so it must never
            become resolvable.
    """
    _frag_path(vault, name).write_text(
        f"---\n2024-05-01: note\nid: {name}\n---\nbody\n", encoding="utf-8"
    )


def test_malformed_sibling_frontmatter_does_not_block_entry_ref_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One corrupt fragment must not break every other ``entry_ref`` (#847).

    ``_resolve_entry`` walks ``01-Fragments`` and calls ``frontmatter.load`` on
    every ``*.md`` it finds. A single hand-edited or half-written fragment with
    unparseable YAML therefore takes down *all* ``entry_ref`` reflection, as an
    unhandled ``yaml.YAMLError`` crossing the MCP boundary — not a structured
    refusal. The unreadable files must be skipped and the walk must continue.

    Traversal order decides whether the target's own resolution ever touches a
    corrupt sibling, so that half is not what proves the skip ran; the closing
    :func:`_assert_sweep_reaches_every_fragment` call is, on every filesystem.
    """
    vault = _vault(tmp_path)
    for bad in _CORRUPT_SIBLINGS:
        _write_malformed_fragment(vault, bad, _CORRUPT_BODY)
    body = "the kettle boiled before I remembered why"
    _write_fragment(vault, "frag-target", body, tier="open")
    factory = _RecordingFactory(
        _notes_payload({"quote": body, "kind": "pattern", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-target",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert isinstance(result, dict)  # returns rather than raises (the #847 AC)
    assert result["status"] == "ok"
    assert result["notes"][0]["quote"] == body
    # Defense in depth against a future "salvage the raw text" regression: the
    # skipped siblings' content must stay out of the response entirely.
    assert _CORRUPT_BODY not in json.dumps(result)
    _assert_sweep_reaches_every_fragment(
        vault, monkeypatch, *_CORRUPT_SIBLINGS, "frag-target"
    )


def test_malformed_fragment_can_never_be_resolved_as_an_entry(tmp_path: Path) -> None:
    """An unparseable fragment is unreadable by *anyone* — the fail-closed half.

    This is the security contract, and it is why the fix must be a skip rather
    than a salvage. The file's raw bytes contain both ``id: frag-corrupt`` and
    intimate body text, but its front matter never parsed, so its
    ``privacy_tier`` is unknown and unknowable. A "helpful" recovery — regexing
    the id out of the raw text, or falling back to the body with no metadata —
    would hand an ``open``-ceiling caller content whose classification nobody
    can vouch for: exactly the leak the #846 gate exists to prevent, reached by
    walking around it.

    So: unparseable implies unresolvable. Not routed to any model, not quoted
    back, and indistinguishable from absent (see the test below).
    """
    vault = _vault(tmp_path)
    _write_malformed_fragment(vault, "frag-corrupt", _INTIMATE_BODY)
    factory = _RecordingFactory(
        _notes_payload({"quote": _INTIMATE_BODY, "kind": "fear", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-corrupt",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert _INTIMATE_SPAN not in json.dumps(result), "unparseable content egressed"
    assert result["status"] == "refused"
    assert result["reason"] == "entry_ref not found"
    assert factory.asked_tier is None  # the model was never reached


def test_malformed_fragment_skip_is_indistinguishable_from_not_found(
    tmp_path: Path,
) -> None:
    """Skipping a corrupt fragment must not become a new existence oracle (#847).

    A caller who probes ``entry_ref`` values learns only "not found" today. If
    the skip path grew its own reason — ``"entry_ref unreadable"``, say — that
    refusal would confirm a fragment with that id *exists in the corpus*, to a
    caller who is not admitted to any of its content and cannot even be told its
    tier. That is a strictly new read oracle over the corpus, in the one place
    where the tier is unknown so nothing can be reasoned about it.

    The two responses must therefore be *identical*, not merely similarly
    refused: ``refusal_response`` builds a fixed four-key dict with nothing
    nondeterministic in it, so whole-dict equality is the honest assertion and it
    also catches a distinguishing field added anywhere else in the payload. This
    test exists so that a future, well-meaning "more debuggable" distinct reason
    fails the suite rather than shipping.
    """
    vault = _vault(tmp_path)
    _write_malformed_fragment(vault, "frag-corrupt", _INTIMATE_BODY)
    corrupt = reflect_tool(
        vault_path=vault,
        entry_ref="frag-corrupt",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    missing = reflect_tool(
        vault_path=vault,
        entry_ref="does-not-exist",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert corrupt == missing
    # Kept explicit so a failure names the contracted reason, not just a diff.
    assert corrupt["reason"] == "entry_ref not found"


def test_malformed_sibling_does_not_weaken_the_ceiling_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corrupt-file skip must not reorder or bypass the #846 read gate.

    The skip lands inside ``_resolve_entry``, directly upstream of the read-side
    ceiling gate. An implementation that swallowed the parse error by, say,
    returning early with a default tier, or by continuing past the ``id`` match
    with partially-populated metadata, would resolve an INTIMATE fragment as
    something the gate admits. The gate must still fire with the tier-neutral
    reason, nothing of the body may appear, the model must be untouched, and the
    single audit append above the gate must stay exactly one record.

    The audit assertion is made *before* the closing sweep, which is itself a
    ``reflect_tool`` call and appends a record of its own; see
    :func:`_assert_sweep_reaches_every_fragment` for why the sweep — rather than
    this call's own traversal order — is what proves the skip path ran.
    """
    vault = _vault(tmp_path)
    for bad in _CORRUPT_SIBLINGS:
        _write_malformed_fragment(vault, bad, _CORRUPT_BODY)
    _write_fragment(vault, "frag-intimate", _INTIMATE_BODY, tier="intimate")
    factory = _RecordingFactory(
        _notes_payload({"quote": _INTIMATE_BODY, "kind": "fear", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-intimate",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert _INTIMATE_SPAN not in json.dumps(result), "intimate content egressed"
    assert _CORRUPT_BODY not in json.dumps(result), "unparseable content egressed"
    assert result["reason"] == _REFUSAL_REASON
    assert factory.asked_tier is None
    assert len(_audit(vault)) == 1
    _assert_sweep_reaches_every_fragment(
        vault, monkeypatch, *_CORRUPT_SIBLINGS, "frag-intimate"
    )


def test_unreadable_fragment_file_is_skipped_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fragment that is not even valid UTF-8 is skipped, not fatal (#847).

    This is the evidence that the fix cannot be a narrow ``except
    yaml.YAMLError``. ``frontmatter.load`` decodes the file before any YAML
    parser sees it, so a byte sequence that is not valid UTF-8 raises
    ``UnicodeDecodeError``, which is not a ``YAMLError`` and is just as fatal to
    every other ``entry_ref`` in the vault. The skip must be broad enough to
    cover "this file could not be opened and understood", whatever the reason,
    because the corpus is user-managed and arbitrarily messy. The ``OSError`` and
    ``TypeError`` arms of that breadth get their own tests below.

    As with the malformed siblings above, the closing sweep — not this call's
    traversal order — is what guarantees the undecodable files were opened.
    """
    vault = _vault(tmp_path)
    for bad in _UNDECODABLE_SIBLINGS:
        _write_undecodable_fragment(vault, bad)
    body = "the kettle boiled before I remembered why"
    _write_fragment(vault, "frag-target", body, tier="open")
    factory = _RecordingFactory(
        _notes_payload({"quote": body, "kind": "pattern", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-target",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["notes"][0]["quote"] == body
    _assert_sweep_reaches_every_fragment(
        vault, monkeypatch, *_UNDECODABLE_SIBLINGS, "frag-target"
    )


def test_undecodable_fragment_never_crashes_a_full_scan(tmp_path: Path) -> None:
    """``UnicodeDecodeError`` is survived by construction, not by luck (#847).

    The sibling test above needs a spy to guarantee the undecodable file is ever
    reached; this one gets the guarantee from the vault's shape. The corrupt file
    is the *only* fragment and the ``entry_ref`` matches nothing, so
    ``_resolve_entry`` cannot early-return: it has to open the file, fail to
    decode it, skip it, and run off the end into the ``"entry_ref not found"``
    refusal. Every traversal order produces exactly that.

    Without this, a regression narrowing the catch to ``except (yaml.YAMLError,
    OSError)`` could survive the entire suite on a lucky directory ordering.
    """
    vault = _vault(tmp_path)
    _write_undecodable_fragment(vault, "undecodable")
    result = reflect_tool(
        vault_path=vault,
        entry_ref="does-not-exist",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert result["reason"] == "entry_ref not found"


def test_unopenable_fragment_path_is_skipped_not_crashed(tmp_path: Path) -> None:
    """The I/O arm: an ``OSError`` out of ``frontmatter.load`` is skipped (#847).

    ``rglob("*.md")`` matches by name, so a *directory* called ``not-a-file.md``
    is swept exactly like a file. ``frontmatter.load`` hands it to ``open()``,
    which raises ``IsADirectoryError`` — an ``OSError``, not a ``YAMLError`` and
    not a ``UnicodeDecodeError``. The same arm covers a file that vanished
    mid-walk or that the process cannot read; a directory is the portable way to
    provoke it, since ``chmod`` is a no-op for root and CI containers run as
    root.

    Order-independent by construction: it is the only entry under
    ``01-Fragments`` and the ``entry_ref`` matches nothing, so the walk must
    exhaust it. Without this, a mutant narrowing the catch to ``except
    (yaml.YAMLError, UnicodeDecodeError)`` survives deterministically forever.
    """
    vault = _vault(tmp_path)
    _frag_path(vault, "not-a-file").mkdir()
    result = reflect_tool(
        vault_path=vault,
        entry_ref="does-not-exist",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert result["reason"] == "entry_ref not found"


def test_nonstring_frontmatter_key_is_skipped_not_crashed(tmp_path: Path) -> None:
    """The ``TypeError`` arm — why the catch cannot be the narrow tuple (#847).

    This is the case that decides the *shape* of the catch. The YAML here parses
    cleanly; the failure is one layer above it, in ``frontmatter.loads``, which
    does ``Post(content, handler, **metadata)``. A bare ``2024-05-01:`` line
    loads as a ``datetime.date`` key, so that splat raises ``TypeError: keywords
    must be strings``. A ``TypeError`` is not a ``yaml.YAMLError``, not a
    ``ValueError`` and not an ``OSError``, so this codebase's usual narrow tuple
    ``except (OSError, ValueError, yaml.YAMLError)`` would let it cross the MCP
    boundary and take down every ``entry_ref`` in the vault. ``except Exception``
    is load-bearing, and this test is what stops a future "tightening" of it.

    Order-independent by construction, as above: sole fragment, unmatchable ref.
    """
    vault = _vault(tmp_path)
    _write_nonstring_key_fragment(vault, "date-keyed")
    result = reflect_tool(
        vault_path=vault,
        entry_ref="does-not-exist",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert result["reason"] == "entry_ref not found"


# --- Default (production) grounding retrieval (#964) ------------------------------

# Every test above injects ``retrieve=``, so ``_default_retrieve`` — the grounder
# ``build_server`` actually wires — has never been executed by this suite. Nothing
# below passes ``retrieve=`` (except the last test, whose subject *is* the seam),
# so these drive the production path end to end.

# A verbatim span of ``_ENTRY``, so the canned response survives ``_clean_notes``
# and the call lands on ``status: "ok"``. Reused wherever a test needs to tell
# "grounding degraded" apart from "the whole call degraded".
_GROUNDED_QUOTE = "the garden grew while I slept"

# The literal ``_build_prompt`` renders when the grounding list is empty — i.e.
# exactly what #964 shipped for every production reflection.
_NO_GROUNDING = "(none)"

# Grounding supplied by an injected stub, distinct from every corpus sentinel.
_INJECTED_SNIPPET = "INJECTED-GROUNDING-0d5e"

# A vault-content-shaped marker carried in a retrieval exception's *message*.
_RETRIEVAL_FAILURE_SENTINEL = "VAULT-SECRET-4c19"


@dataclass(frozen=True)
class _CorpusSentinels:
    """The three markers identifying one corpus-fixture fragment in a prompt.

    Title, body and id are deliberately distinct, mutually non-substring
    strings that appear nowhere in :data:`_ENTRY`. That is what lets an
    assertion say *which* of the three reached the model: a title/body mix-up
    (the whole subject of #964 — grounding feeds titles, not bodies) and a
    stray id both become impossible to miss rather than silently equivalent.
    """

    frag_id: str
    title: str
    body: str


#: One corpus fixture per ``privacy_tier``, keyed by the tier's front-matter
#: value. Four entries on purpose: ``author.retrieval_top_k`` defaults to 5, so
#: nothing here is ever truncated by the top-k slice and "admitted" is exactly
#: set-equal to "retrieved".
_CORPUS: dict[str, _CorpusSentinels] = {
    "open": _CorpusSentinels(
        frag_id="frag-corpus-open-9f2a",
        title="TITLE-OPEN-9f2a",
        body="BODY-OPEN-3c71",
    ),
    "personal": _CorpusSentinels(
        frag_id="frag-corpus-personal-5b8d",
        title="TITLE-PERSONAL-5b8d",
        body="BODY-PERSONAL-1a46",
    ),
    "unclassified": _CorpusSentinels(
        frag_id="frag-corpus-unclassified-2e93",
        title="TITLE-UNCLASSIFIED-2e93",
        body="BODY-UNCLASSIFIED-7d05",
    ),
    "intimate": _CorpusSentinels(
        frag_id="frag-corpus-intimate-8c4f",
        title="TITLE-INTIMATE-8c4f",
        body="BODY-INTIMATE-6b29",
    ),
}

_SOURCES_MARKER = "SOURCE FRAGMENTS:\n"
_ENTRY_MARKER = "\n\nENTRY:\n"


def _source_section(prompt: str) -> str:
    """Return only the ``SOURCE FRAGMENTS`` block of *prompt*.

    **Every grounding assertion in this section runs against this slice, never
    against the raw prompt**, and both directions of that matter:

    * a bare ``title in prompt`` can pass off the ``ENTRY:`` block (or the
      schema preamble) and report grounding that never happened;
    * a bare ``body not in prompt`` can fail off the ``ENTRY:`` block whenever
      the entry legitimately quotes the same words.

    Slicing first makes each assertion mean what it says. The one deliberate
    exception is :func:`_assert_tier_left_no_trace`, which asserts *absence*
    from the whole prompt — nothing above the ceiling may reach the provider by
    any route, grounding block or not.

    Args:
        prompt: The full prompt recorded by :class:`_RecordingFactory`.

    Returns:
        The text between the ``SOURCE FRAGMENTS:`` header and the ``ENTRY:``
        header — ``"(none)"`` when the grounding list was empty.
    """
    assert _SOURCES_MARKER in prompt, "prompt carries no SOURCE FRAGMENTS block"
    after = prompt.split(_SOURCES_MARKER, 1)[1]
    assert _ENTRY_MARKER in after, "SOURCE FRAGMENTS block is never terminated"
    return after.split(_ENTRY_MARKER, 1)[0]


def _write_corpus_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str,
    body: str,
    privacy_tier: str,
) -> None:
    """Write a fragment the *retrieval corpus* can actually see.

    **Do not "de-duplicate" this against :func:`_write_fragment`.** They look
    alike and are not interchangeable. ``_write_fragment`` writes only ``id``
    and ``privacy_tier``, which is all an ``entry_ref`` lookup needs — that path
    reads front matter directly. Corpus retrieval goes through
    ``creek.vault.reader.try_load_fragment``, which returns ``None`` for any
    file without ``type: fragment`` and for anything the ``Fragment`` model
    rejects. A ``_write_fragment`` file is therefore **invisible** to
    ``_load_corpus``, so a grounding test built on it would run against an empty
    corpus and pass vacuously — the exact false green that let #964 sit
    undetected. This writer mirrors the full, model-valid recipe instead.

    Args:
        vault: Vault root as created by :func:`_vault`.
        frag_id: The fragment ``id`` (also the file stem).
        title: The fragment ``title`` — what ``_fragment_claim`` turns into an
            ``EvidenceClaim.claim``, i.e. what grounding is made of.
        body: The markdown body, which grounding must *not* carry.
        privacy_tier: The ``privacy_tier`` front-matter value.
    """
    stamp = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": stamp,
        "ingested": stamp,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": privacy_tier,
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


def _write_tiered_corpus(vault: Path) -> None:
    """Write every :data:`_CORPUS` fixture — one fragment per privacy tier.

    Args:
        vault: Vault root as created by :func:`_vault`.
    """
    for tier, sentinels in _CORPUS.items():
        _write_corpus_fragment(
            vault,
            frag_id=sentinels.frag_id,
            title=sentinels.title,
            body=sentinels.body,
            privacy_tier=tier,
        )


def _tiers_grounded_in(prompt: str) -> set[str]:
    """Return the tiers whose corpus fixture reached *prompt*'s grounding block.

    ORDERING HAZARD, and why this returns a *set*. The conftest's autouse
    sentence-transformer mock seeds each vector from ``hash(text)``, which
    Python randomises per process, so the similarity ranking of these fixtures
    differs run to run. Nothing here may assert on order or index; membership is
    the only stable observable. It is also the one that matters: the contract is
    *which* fragments are admitted, not what order they came back in.

    Args:
        prompt: The full prompt recorded by :class:`_RecordingFactory`.

    Returns:
        The subset of :data:`_CORPUS` keys whose ``title`` appears in the
        grounding block.
    """
    sources = _source_section(prompt)
    return {tier for tier, marks in _CORPUS.items() if marks.title in sources}


def _assert_tier_left_no_trace(prompt: str, tier: str) -> None:
    """Assert nothing of the *tier* corpus fixture appears anywhere in *prompt*.

    Checked against the **whole** prompt rather than :func:`_source_section`,
    unlike every positive assertion here. An above-ceiling fragment crossing to
    the provider is a leak wherever in the prompt it lands, so restricting this
    to the grounding block would let a future "helpful context" section egress
    it while the suite stayed green.

    All three markers are checked because they fail differently: a *title* means
    the ceiling filter admitted the fragment outright, a *body* means grounding
    switched from titles to bodies (the design question #964 settles), and an
    *id* means attribution metadata leaked without any prose to explain it.

    Args:
        prompt: The full prompt recorded by :class:`_RecordingFactory`.
        tier: The :data:`_CORPUS` key that must be absent.
    """
    marks = _CORPUS[tier]
    assert marks.title not in prompt, f"{tier} fragment title reached the model"
    assert marks.body not in prompt, f"{tier} fragment body reached the model"
    assert marks.frag_id not in prompt, f"{tier} fragment id reached the model"


def _patch_gather_to_raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Make every :meth:`RetrievalSpecialist.gather` call raise *exc*.

    Patches the class attribute rather than the module binding because
    ``_default_retrieve`` imports ``RetrievalSpecialist`` lazily *inside* its own
    body: it resolves the same class object this patch mutates, so the
    substitution is picked up without reaching into the tool's import machinery.

    Args:
        monkeypatch: The builtin pytest fixture; restores the real ``gather``.
        exc: The exception instance every ``gather`` call raises.
    """

    def _raise(*args: Any, **kwargs: Any) -> EvidenceBundle:
        """Raise *exc* instead of gathering evidence."""
        raise exc

    monkeypatch.setattr(RetrievalSpecialist, "gather", _raise)


def test_default_grounding_feeds_fragment_titles_not_bodies(tmp_path: Path) -> None:
    """The production grounder retrieves, and what it retrieves is titles (#964).

    The anti-vacuity test for this whole section, and the direct regression for
    #964: ``_default_retrieve`` read ``claim.text`` off an ``EvidenceClaim``
    that carries ``claim``, so it raised ``AttributeError`` into a bare
    ``except`` and every production reflection was grounded on ``(none)``.
    Nothing in the response shows that — only the prompt does.

    Both halves are load-bearing. The grounding block must be the fragment's
    **title**, asserted by exact equality so the rendering (``"- " + claim``)
    is pinned rather than merely "the title is in there somewhere"; and the
    fragment **body** must appear nowhere in the prompt at all, because
    ``EvidenceClaim`` could just as easily have been made to carry body text,
    and grounding on bodies would push whole corpus fragments to the provider on
    every call.
    """
    vault = _vault(tmp_path)
    marks = _CORPUS["open"]
    _write_corpus_fragment(
        vault,
        frag_id=marks.frag_id,
        title=marks.title,
        body=marks.body,
        privacy_tier="open",
    )
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    prompt = factory.prompt
    assert prompt is not None
    sources = _source_section(prompt)
    assert sources != _NO_GROUNDING, "production grounding retrieved nothing (#964)"
    assert sources == f"- {marks.title}"
    assert marks.body not in prompt, "grounding carried the body, not the title"


@pytest.mark.parametrize(
    ("ceiling", "expected_tiers"),
    [
        (TierCeiling.OPEN, {"open"}),
        (TierCeiling.PERSONAL, {"open", "personal", "unclassified"}),
        (TierCeiling.INTIMATE, {"open", "personal", "unclassified", "intimate"}),
        (TierCeiling.ALL, {"open", "personal", "unclassified", "intimate"}),
    ],
)
def test_grounding_admits_exactly_the_within_ceiling_corpus(
    tmp_path: Path, ceiling: TierCeiling, expected_tiers: set[str]
) -> None:
    """Production grounding admits the within-ceiling corpus — no more, no less.

    Switching ``_default_retrieve`` on (#964) opens a *second* read path into
    the vault, alongside the ``entry_ref`` resolution the #846 gate guards. This
    one is not gated by tier at the tool: it relies entirely on the ceiling
    being converted to a ``PrivacyTierOverride`` and handed to
    ``RetrievalSpecialist.gather``, which drops above-override fragments inside
    ``_load_corpus``. That is the only thing standing between an ``open``-ceiling
    caller and the intimate half of their corpus, so it is pinned per ceiling.

    Asserted as **set equality**, not as a leak check, so an *omission* fails as
    loudly as a leak: a filter tightened to "open only" would silently strip
    real grounding from every personal-tier caller, and a leak-only assertion
    would call that a pass. The excluded fragments then get the stronger
    whole-prompt check via :func:`_assert_tier_left_no_trace`.

    ``ALL`` and ``INTIMATE`` deliberately share an expectation: ``ALL`` is a
    ceiling, not a tier, and must not admit anything ``INTIMATE`` does not.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: The caller's declared ceiling.
        expected_tiers: The :data:`_CORPUS` keys whose titles must ground the
            prompt at *ceiling* — exactly those, and no others.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=factory,
        privacy_tier_ceiling=ceiling,
    )
    prompt = factory.prompt
    assert prompt is not None
    assert _tiers_grounded_in(prompt) == expected_tiers
    for tier in _CORPUS:
        if tier not in expected_tiers:
            _assert_tier_left_no_trace(prompt, tier)


@pytest.mark.parametrize(
    ("ceiling", "expected_routed"),
    [
        (TierCeiling.OPEN, PrivacyTier.OPEN),
        (TierCeiling.PERSONAL, PrivacyTier.PERSONAL),
        (TierCeiling.INTIMATE, PrivacyTier.INTIMATE),
        (TierCeiling.ALL, PrivacyTier.INTIMATE),
    ],
)
def test_routing_tier_dominates_every_retrieved_fragment_tier(
    tmp_path: Path, ceiling: TierCeiling, expected_routed: PrivacyTier
) -> None:
    """Whatever grounding pulled in, the routing tier is at least as sensitive.

    This is the INTIMATE-never-egresses guarantee restated for the read path
    #964 switches on. Retrieval widens what crosses to the provider from "the
    caller's own entry" to "titles drawn from their corpus", so the routing tier
    must dominate the most sensitive fragment *actually retrieved* — otherwise a
    ceiling that admits intimate fragments into grounding while routing at a
    cloud-eligible tier would egress them.

    Two assertions, deliberately redundant, because each catches what the other
    cannot:

    (a) the hard-coded expected :class:`PrivacyTier` per ceiling, which fails if
        ``_CEILING_ROUTING_TIER`` is ever retuned — the table is a policy
        decision and a silent change to it must not pass;
    (b) the inequality itself, derived from the tiers whose titles actually
        reached the prompt rather than restated from the table above, so it
        still holds if a *new* tier is added to the vocabulary and the fixtures
        grow to match, and it fails if the admission filter and the routing
        table ever drift apart.

    (b) would be vacuous over an empty retrieval set — which is precisely the
    #964 state — so a non-empty grounding set is asserted first.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: The caller's declared ceiling.
        expected_routed: The tier the factory must be asked for.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=factory,
        privacy_tier_ceiling=ceiling,
    )
    asked = factory.asked_tier
    assert asked is not None, "the model was never reached"
    assert asked is expected_routed
    prompt = factory.prompt
    assert prompt is not None
    grounded = _tiers_grounded_in(prompt)
    assert grounded, "no grounding reached the prompt (#964): (b) would be vacuous"
    assert tier_ceiling.tier_sensitivity(asked) >= max(
        tier_ceiling.tier_sensitivity(PrivacyTier(tier)) for tier in grounded
    )


def test_above_ceiling_fragment_contributes_nothing_not_even_a_title(
    tmp_path: Path,
) -> None:
    """An intimate corpus fragment leaves no residue at an ``open`` ceiling.

    Distinct from the residue #931/#1032 found on the compile/structural paths,
    and the distinction is structural rather than lucky. Those paths run
    ``filter_fragments_by_tier``, which *keeps* an above-ceiling fragment as a
    summarised stub — so its title survives by design and the leak is the
    surviving stub. Grounding retrieval does not go near that: ``_load_corpus``
    applies ``tier_within_override``, a hard rank cutoff that drops the record
    entirely before ranking, so there is no summarise-the-body step and nothing
    can survive it. This test pins that *by construction* property, so a future
    "reuse the compile filter here for consistency" refactor — which would look
    like a tidy-up and would import the residue wholesale — fails loudly.

    The ``open`` fragment is not decoration: it is the control that proves
    grounding ran at all. Without it the intimate absences would hold trivially
    under the #964 bug, and the test would be a permanent false green.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    prompt = factory.prompt
    assert prompt is not None
    # The control: grounding really ran, so the absences below mean something.
    assert _CORPUS["open"].title in _source_section(prompt)
    _assert_tier_left_no_trace(prompt, "intimate")


def test_retrieval_failure_degrades_to_no_grounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retrieval failure costs the reflection its grounding, not its answer.

    Grounding is an enhancement; the entry is the subject. A corpus whose
    embedding model is missing, whose cache is corrupt, or whose fragments fail
    to load must not turn an ordinary journal reflection into a refusal — so
    ``_default_retrieve`` swallows the failure and returns no snippets.

    Pinned on both sides: the envelope is a normal ``ok`` (never ``refused``),
    *and* the grounding block is exactly ``(none)``. The second half is what
    stops a future "degrade to whatever we had" from shipping partial or stale
    grounding out of a half-failed retrieval.

    That broad ``except`` is also what hid #964 for the entire life of the
    feature, which is why the degradation must stay observable — see
    :func:`test_retrieval_failure_logs_the_exception_type_not_its_message`.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    _patch_gather_to_raise(monkeypatch, RuntimeError("embedding backend unavailable"))
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "ok"
    assert result["notes"][0]["quote"] == _GROUNDED_QUOTE
    prompt = factory.prompt
    assert prompt is not None
    assert _source_section(prompt) == _NO_GROUNDING


def test_retrieval_failure_logs_the_exception_type_not_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The silent degradation must leave a trace — a *type name*, never a message.

    #964 is a bug that ran in production for the whole life of the feature
    precisely because ``except Exception: return []`` says nothing. Every
    reflection was ungrounded and no operator could tell. A DEBUG line naming
    the exception type is what makes the next such failure findable.

    Naming only ``type(exc).__name__`` is not fastidiousness, it is the same
    rule ``_resolve_entry`` already follows at ``reflect.py:326-331``:
    ``yaml.MarkedYAMLError`` stringifies *with the offending source snippet*, so
    ``str(exc)``, ``exc_info=True`` or ``logger.exception`` would write
    tier-unknown vault content into a log file that carries no tier and is not
    covered by the ceiling. The failure paths here are the same corpus files, so
    the same rule applies — and a retrieval exception can just as easily carry a
    fragment's text in its message.

    Asserted both ways round: the type name must be present (a silent swallow
    fails) and the message sentinel absent (a chatty log fails).
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    _patch_gather_to_raise(monkeypatch, RuntimeError(_RETRIEVAL_FAILURE_SENTINEL))
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    with caplog.at_level(logging.DEBUG, logger="creek_mcp.tools.reflect"):
        result = reflect_tool(
            vault_path=vault,
            content=_ENTRY,
            llm_factory=factory,
            privacy_tier_ceiling=TierCeiling.PERSONAL,
        )
    assert result["status"] == "ok"
    assert "RuntimeError" in caplog.text, "the degradation was swallowed silently"
    assert _RETRIEVAL_FAILURE_SENTINEL not in caplog.text, "exception message logged"


def test_injected_retrieve_bypasses_the_default_grounder(tmp_path: Path) -> None:
    """An injected ``retrieve=`` still wins outright over the default grounder.

    The seam every other test in this file depends on. ``reflect_tool`` picks
    ``_default_retrieve`` only when ``retrieve is None``, and switching the
    default on (#964) is exactly the change that could turn that into a merge —
    grounding from both sources — without any existing test noticing, since they
    all run against empty vaults where the default contributes nothing anyway.

    Asserted with a populated corpus, so "the default did not also run" is a
    real observation rather than a tautology, and by exact equality on the
    grounding block: the injected snippet is not merely *present*, it is the
    *only* thing there.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    calls: list[str] = []

    def _stub_retrieve(query: str, vault_arg: Path, override: object) -> list[str]:
        """Record the call and return one sentinel grounding snippet."""
        del vault_arg, override
        calls.append(query)
        return [_INJECTED_SNIPPET]

    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=factory,
        retrieve=_stub_retrieve,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert calls == [_ENTRY]
    prompt = factory.prompt
    assert prompt is not None
    assert _source_section(prompt) == f"- {_INJECTED_SNIPPET}"
    assert _tiers_grounded_in(prompt) == set()


# ---------------------------------------------------------------------------
# The compiled layer: related_praxis / related_eddies (#873)
# ---------------------------------------------------------------------------
#
# Additive and optional, and the tests below are ordered by stakes: the ceiling
# first (a compiled page is synthesised from fragments the caller may not be
# entitled to, so it is the laundering surface), then absence-when-nothing-
# qualifies (a pre-#873 consumer's parse must not change), then the seam.

_EDDY_873 = "Rest and Ruin"
_PRAXIS_873 = "Rest before the collapse"


def _write_compiled_layer(vault: Path, *, member_tier: str) -> str:
    """Write an eddy + praxis compiled from one ``open`` and one *member_tier* fragment.

    Args:
        vault: Vault root.
        member_tier: The ``privacy_tier`` of the *second* contributor. The
            entry the caller reflects on is always the ``open`` one, so this is
            the only thing that varies between the admitted and withheld cases.

    Returns:
        The ``entry_ref`` of the ``open`` fragment.
    """
    entry_ref = "frag-873open0001"
    other = "frag-873other001"
    link = f"[[{_EDDY_873}]]"
    for frag_id, tier in ((entry_ref, "open"), (other, member_tier)):
        directory = vault / "01-Fragments" / "Notes"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{frag_id}.md").write_text(
            "---\ntype: fragment\n"
            f"id: {frag_id}\ntitle: {frag_id}\nsource:\n  platform: journal\n"
            f"privacy_tier: {tier}\n"
            f'eddies:\n  - "{link}"\n---\n\n{_ENTRY}\n'
        )
    eddies = vault / "03-Eddies"
    eddies.mkdir(parents=True, exist_ok=True)
    (eddies / "eddy.md").write_text(
        "---\ntype: eddy\nid: eddy-0000000001\n"
        f"title: {_EDDY_873}\nformed: 2026-03-04\nfragment_count: 2\n"
        "description: Where rest and ruin keep meeting.\n---\n\nSummary.\n"
    )
    praxis = vault / "04-Praxis" / "Daily"
    praxis.mkdir(parents=True, exist_ok=True)
    (praxis / "praxis.md").write_text(
        "---\ntype: praxis\nid: prax-0000000001\n"
        f"title: {_PRAXIS_873}\npraxis_type: practice\nstatus: active\n"
        f"derived_from:\n  - {entry_ref}\n  - {other}\n---\n\nRest is the practice.\n"
    )
    return entry_ref


def _reflect_with_compiled_layer(
    vault: Path, entry_ref: str, ceiling: TierCeiling
) -> dict[str, Any]:
    """Run ``reflect_tool`` against the real compiled-layer lookup."""
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    return reflect_tool(
        vault_path=vault,
        entry_ref=entry_ref,
        llm_factory=factory,
        # Injected so this test needs no embedding stack. The entry's own
        # ``entry_ref`` still seeds the compiled-layer lookup, so the
        # production ``related_compiled`` really runs -- this does not quietly
        # become a test of the injected path.
        retrieve=_no_retrieval,
        privacy_tier_ceiling=ceiling,
    )


def test_compiled_pages_reach_a_caller_admitted_to_every_contributor(
    tmp_path: Path,
) -> None:
    """The control: an all-``open`` eddy and praxis are published at ``open``.

    Without this, every withholding assertion below could be explained by the
    feature simply never firing.
    """
    vault = _vault(tmp_path)
    entry_ref = _write_compiled_layer(vault, member_tier="open")

    result = _reflect_with_compiled_layer(vault, entry_ref, TierCeiling.OPEN)

    assert [row["title"] for row in result["related_eddies"]] == [_EDDY_873]
    assert [row["title"] for row in result["related_praxis"]] == [_PRAXIS_873]


@pytest.mark.parametrize(
    ("ceiling", "member_tier"),
    [
        (TierCeiling.OPEN, "personal"),
        (TierCeiling.OPEN, "intimate"),
        (TierCeiling.PERSONAL, "intimate"),
    ],
    ids=["personal@open", "intimate@open", "intimate@personal"],
)
def test_a_page_compiled_above_the_ceiling_never_reaches_the_response(
    tmp_path: Path, ceiling: TierCeiling, member_tier: str
) -> None:
    """One above-ceiling contributor withholds the whole compiled page.

    Asserted at the **narrowest** ceiling that can fail — ``personal`` content
    refused an ``open``-ceiling caller — rather than at a permissive one where
    nothing would have been filtered and the assertion would pass against
    unfixed code. The remote cap
    (:data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS`) means these two
    ceilings are the only ones a network consumer can ever declare, so this is
    the whole reachable surface for Adepthood.
    """
    vault = _vault(tmp_path)
    entry_ref = _write_compiled_layer(vault, member_tier=member_tier)

    result = _reflect_with_compiled_layer(vault, entry_ref, ceiling)

    assert "related_eddies" not in result
    assert "related_praxis" not in result
    # The *whole* response, not just those two keys: a leak that renamed the
    # field, or folded the description into the notes, would still be a leak.
    assert "Where rest and ruin keep meeting" not in json.dumps(result)
    assert _PRAXIS_873 not in json.dumps(result)


def test_the_fields_are_absent_not_empty_when_nothing_qualifies(
    tmp_path: Path,
) -> None:
    """A pre-#873 consumer's parse is unchanged when the vault has no matches.

    ``absent`` rather than ``present-and-empty`` is the whole compatibility
    claim: a consumer validating the older closed shape sees no new key at all.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-873lonely01", _ENTRY, tier="open")
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )

    result = reflect_tool(
        vault_path=vault,
        entry_ref="frag-873lonely01",
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "ok"
    assert "related_praxis" not in result
    assert "related_eddies" not in result
    assert set(result) == {
        "status",
        "tool",
        "tier_ceiling",
        "routed_tier",
        "notes",
        "essay_grounded",
    }


def test_a_refused_entry_carries_no_compiled_layer(tmp_path: Path) -> None:
    """The #846 read gate still short-circuits everything below it.

    The compiled-layer lookup must not become a way around the gate: an
    unadmitted ``entry_ref`` gets the four-key refusal and nothing else.
    """
    vault = _vault(tmp_path)
    entry_ref = _write_compiled_layer(vault, member_tier="open")
    (vault / "01-Fragments" / "Notes" / f"{entry_ref}.md").write_text(
        "---\ntype: fragment\n"
        f"id: {entry_ref}\ntitle: t\nsource:\n  platform: journal\n"
        "privacy_tier: intimate\n---\n\nbody\n"
    )
    factory = _RecordingFactory(_notes_payload())

    result = reflect_tool(
        vault_path=vault,
        entry_ref=entry_ref,
        llm_factory=factory,
        retrieve=_no_retrieval,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert set(result) == {"status", "tool", "tier_ceiling", "reason"}


def test_an_escalation_carries_no_compiled_layer(tmp_path: Path) -> None:
    """A care escalation is care and nothing else -- no enrichment rides along."""
    vault = _vault(tmp_path)
    entry_ref = _write_compiled_layer(vault, member_tier="open")
    factory = _RecordingFactory(_notes_payload())

    result = reflect_tool(
        vault_path=vault,
        entry_ref=entry_ref,
        llm_factory=factory,
        retrieve=_no_retrieval,
        care_guard=lambda _entry: "acute_distress",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "escalate"
    assert "related_praxis" not in result
    assert "related_eddies" not in result


def test_a_failing_compiled_lookup_does_not_veto_the_reflection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Enrichment may subtract itself; it may never take down the answer.

    The failure mode this pins is the one that keeps recurring in this repo: a
    tidy-up or enrichment step that raises and vetoes the operation it was
    meant to decorate. The reflection was already produced by the time this
    runs, so a raising lookup must cost only the optional fields.
    """
    vault = _vault(tmp_path)
    entry_ref = _write_compiled_layer(vault, member_tier="open")
    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )

    def _exploding(seeds: object, vault_arg: object, ceiling: object) -> object:
        """Raise the way a corrupt vault page might."""
        del seeds, vault_arg, ceiling
        raise TypeError("keywords must be strings")

    with caplog.at_level(logging.DEBUG, logger="creek_mcp.tools.reflect"):
        result = reflect_tool(
            vault_path=vault,
            entry_ref=entry_ref,
            llm_factory=factory,
            retrieve=_no_retrieval,
            related_lookup=_exploding,  # type: ignore[arg-type]
            privacy_tier_ceiling=TierCeiling.OPEN,
        )

    assert result["status"] == "ok"
    assert result["notes"], "the reflection itself was lost to an enrichment failure"
    assert "related_praxis" not in result
    assert "TypeError" in caplog.text
    assert "keywords must be strings" not in caplog.text


def test_the_entry_ref_seeds_the_compiled_lookup(tmp_path: Path) -> None:
    """The reflected entry's own id is what selects its compiled neighbours.

    Also pins the negative half: raw inline ``content`` has no vault identity,
    so it contributes no seed and (with an injected grounder) selects nothing.
    """
    vault = _vault(tmp_path)
    _write_fragment(vault, "frag-873seed0001", _ENTRY, tier="open")
    seen: list[list[str]] = []

    def _record(seeds, vault_arg, ceiling):
        """Record the seeds handed to the lookup."""
        del vault_arg, ceiling
        seen.append(list(seeds))
        return RelatedCompiled([], [])

    factory = _RecordingFactory(_notes_payload())
    reflect_tool(
        vault_path=vault,
        entry_ref="frag-873seed0001",
        llm_factory=factory,
        retrieve=_no_retrieval,
        related_lookup=_record,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        related_lookup=_record,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    # Both sources supplied: ``content`` wins, so the reflected text is not the
    # fragment named by ``entry_ref`` and that id never passed the #846 gate.
    # It must not be seeded either -- a caller must not be able to point the
    # selection at an arbitrary id by naming it alongside their own text.
    reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        entry_ref="frag-873seed0001",
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        related_lookup=_record,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert seen == [["frag-873seed0001"], [], []]


def test_the_default_grounder_carries_its_fragment_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One retrieval pass yields both the titles and the ids behind them.

    #873 forbids a second embedding sweep on the hot path, so the compiled
    layer has to be selected from the ids the grounding pass already resolved.
    ``EvidenceClaim.source_fragments`` always carried them; before this they
    were dropped on the floor.
    """
    from creek.author.models import EvidenceBundle as _Bundle
    from creek.author.models import EvidenceClaim as _Claim
    from creek_mcp.tools.reflect import _default_retrieve

    def _fake_gather(
        _self: object, _query: str, _vault: Path, *, override: object
    ) -> EvidenceBundle:
        """Return one claim carrying its source fragment ids."""
        del override
        return _Bundle(
            claims=[_Claim(claim="A title", source_fragments=["frag-a", "frag-b"])]
        )

    monkeypatch.setattr(RetrievalSpecialist, "gather", _fake_gather)

    grounding = _default_retrieve(
        _ENTRY, _vault(tmp_path), to_privacy_override(TierCeiling.OPEN)
    )

    assert grounding.lines == ["A title"]
    assert grounding.source_ids == ["frag-a", "frag-b"]


def test_a_retrieval_seed_alone_selects_the_compiled_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page reachable only through retrieval is still selected.

    ``seeds = from_entry + retrieved_ids`` composes two independent sources,
    and until this test only the ``entry_ref`` half was observed: every other
    test that asserts ``related_*`` injects ``retrieve=``, and an injected
    grounder contributes no ids by design. Mutating the line to
    ``seeds = from_entry`` therefore left the whole suite green — the
    retrieval-seeded half of selection was never exercised.

    Here the request carries inline ``content`` and **no** ``entry_ref``, so
    ``from_entry`` is empty by construction and the only way the eddy and
    praxis can be found is through the ids the grounding pass resolved.
    """
    from creek.author.models import EvidenceBundle as _Bundle
    from creek.author.models import EvidenceClaim as _Claim

    vault = _vault(tmp_path)
    entry_ref = _write_compiled_layer(vault, member_tier="open")

    def _fake_gather(
        _self: object, _query: str, _vault: Path, *, override: object
    ) -> EvidenceBundle:
        """Ground on the fragment the compiled pages were built from."""
        del override
        return _Bundle(claims=[_Claim(claim="A title", source_fragments=[entry_ref])])

    monkeypatch.setattr(RetrievalSpecialist, "gather", _fake_gather)

    factory = _RecordingFactory(
        _notes_payload({"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"})
    )
    result = reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        privacy_tier_ceiling=TierCeiling.OPEN,
        llm_factory=factory,
    )

    assert result.get("status") != "refused", result
    assert [row["title"] for row in result["related_praxis"]] == [_PRAXIS_873], result


# --- Exit-path characterisation (#1386) ----------------------------------------------

# ``reflect_tool`` has **seven** terminal exit paths, not the six its issue body
# claimed. Before #1386's extract-method, only two of the seven had their key
# set pinned anywhere (:2125 and :2160) and none had its *values* pinned, so a
# restructuring could have rerouted the ``escalate`` path through the ok-path
# renderer -- silently adding ``routed_tier``/``notes``/``essay_grounded`` to a
# care escalation -- with the whole suite green. These tests pin every path's
# complete response dict by ``==``, so a stray added or dropped key fails.

_CHAR_NOTE = {"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"}
_CHAR_ESSAY = "a paragraph of free model prose"
_CHAR_INTIMATE_REF = "frag-1386intim01"
_CHAR_MISSING_REF = "frag-1386absent1"

_CHAR_PRAXIS = [
    {
        "title": "Rest before the collapse",
        "praxis_type": "practice",
        "status": "active",
        "excerpt": "Rest is the practice.",
    }
]
_CHAR_EDDIES = [
    {
        "title": "Rest and Ruin",
        "description": "Where rest and ruin keep meeting.",
        "fragment_count": 2,
        "formed": "2026-03-04",
    }
]


class _CountingLookup:
    """A compiled-layer lookup that records how many times it was consulted.

    "A non-answer costs no corpus walk" is asserted here by **non-invocation**,
    not merely by the optional keys being absent: a refactor that ran the
    lookup and then discarded its result would satisfy every absent-key
    assertion in this file while still paying the walk -- and, on the
    above-ceiling path, walking the corpus on behalf of a caller the #846 gate
    just refused.
    """

    def __init__(self) -> None:
        """Init the consultation counter."""
        self.calls = 0

    def __call__(
        self, seeds: Sequence[str], vault: Path, ceiling: TierCeiling
    ) -> RelatedCompiled:
        """Record the consultation and return nothing qualifying."""
        del seeds, vault, ceiling
        self.calls += 1
        return RelatedCompiled([], [])


def _char_unavailable_factory(tier: PrivacyTier):
    """Refuse the way a missing/unavailable provider does."""
    del tier
    raise RuntimeError("no provider configured")


def _exit_ok(vault: Path, lookup: _CountingLookup) -> dict[str, Any]:
    """Drive the ``ok`` path: one verbatim note, no essay, no neighbours."""
    return reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=_RecordingFactory(_notes_payload(_CHAR_NOTE)),
        retrieve=_no_retrieval,
        related_lookup=lookup,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _exit_empty(vault: Path, lookup: _CountingLookup) -> dict[str, Any]:
    """Drive the ``empty`` path: the model returned no usable note."""
    return reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=_RecordingFactory(_notes_payload()),
        retrieve=_no_retrieval,
        related_lookup=lookup,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _exit_escalate(vault: Path, lookup: _CountingLookup) -> dict[str, Any]:
    """Drive the ``escalate`` path through the #753 care seam."""
    return reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=_RecordingFactory(_notes_payload(_CHAR_NOTE)),
        retrieve=_no_retrieval,
        related_lookup=lookup,
        care_guard=lambda _entry: "acute_distress_markers",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _exit_no_content(vault: Path, lookup: _CountingLookup) -> dict[str, Any]:
    """Drive the blank-``content`` refusal."""
    return reflect_tool(
        vault_path=vault,
        content="   ",
        llm_factory=_RecordingFactory(_notes_payload(_CHAR_NOTE)),
        retrieve=_no_retrieval,
        related_lookup=lookup,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _exit_not_found(vault: Path, lookup: _CountingLookup) -> dict[str, Any]:
    """Drive the unresolvable-``entry_ref`` refusal."""
    return reflect_tool(
        vault_path=vault,
        entry_ref=_CHAR_MISSING_REF,
        llm_factory=_RecordingFactory(_notes_payload(_CHAR_NOTE)),
        retrieve=_no_retrieval,
        related_lookup=lookup,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _exit_above_ceiling(vault: Path, lookup: _CountingLookup) -> dict[str, Any]:
    """Drive the #846 read-gate refusal: an INTIMATE ref under an OPEN ceiling."""
    _write_fragment(vault, _CHAR_INTIMATE_REF, _INTIMATE_BODY, tier="intimate")
    return reflect_tool(
        vault_path=vault,
        entry_ref=_CHAR_INTIMATE_REF,
        llm_factory=_RecordingFactory(_notes_payload(_CHAR_NOTE)),
        retrieve=_no_retrieval,
        related_lookup=lookup,
        care_guard=lambda _entry: "acute_distress_markers",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


def _exit_unavailable(vault: Path, lookup: _CountingLookup) -> dict[str, Any]:
    """Drive the provider-unavailable refusal."""
    return reflect_tool(
        vault_path=vault,
        content=_ENTRY,
        llm_factory=_char_unavailable_factory,
        retrieve=_no_retrieval,
        related_lookup=lookup,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


_EXIT_PATHS = {
    "ok": (
        _exit_ok,
        {
            "status": "ok",
            "tool": TOOL_NAME,
            "tier_ceiling": "open",
            "routed_tier": "open",
            "notes": [{"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"}],
            "essay_grounded": False,
        },
    ),
    "empty": (
        _exit_empty,
        {
            "status": "empty",
            "tool": TOOL_NAME,
            "tier_ceiling": "open",
            "routed_tier": "open",
            "notes": [],
            "essay_grounded": False,
        },
    ),
    # Deliberately asymmetric: a care escalation carries **no** ``routed_tier``,
    # ``notes`` or ``essay_grounded``. It is care and nothing else -- it never
    # reached the model, so there is no routing or grounding to report, and
    # rendering it through the ok-path renderer would invent all three.
    "escalate": (
        _exit_escalate,
        {
            "status": "escalate",
            "tool": TOOL_NAME,
            "tier_ceiling": "open",
            "reason": "acute_distress_markers",
            "care_signal": CARE_SIGNAL,
        },
    ),
    # The four refusal reasons are byte-pinned here. They are deliberately
    # distinct from one another (see the ACCEPTED RESIDUAL RISK comment in
    # ``_admit_entry``), and "no entry content supplied" was, before #1386,
    # the one of the four that no assertion anywhere in the repository
    # sourced from the tool.
    "no_content": (
        _exit_no_content,
        {
            "status": "refused",
            "tool": TOOL_NAME,
            "tier_ceiling": "open",
            "reason": "no entry content supplied",
        },
    ),
    "not_found": (
        _exit_not_found,
        {
            "status": "refused",
            "tool": TOOL_NAME,
            "tier_ceiling": "open",
            "reason": "entry_ref not found",
        },
    ),
    "above_ceiling": (
        _exit_above_ceiling,
        {
            "status": "refused",
            "tool": TOOL_NAME,
            "tier_ceiling": "open",
            "reason": "entry_ref tier exceeds ceiling",
        },
    ),
    "unavailable": (
        _exit_unavailable,
        {
            "status": "refused",
            "tool": TOOL_NAME,
            "tier_ceiling": "open",
            "reason": "reflection unavailable: RuntimeError",
        },
    ),
}


@pytest.mark.parametrize("path", sorted(_EXIT_PATHS))
def test_the_seven_exit_paths_are_exactly_these_dicts(
    tmp_path: Path, path: str
) -> None:
    """Every terminal response of ``reflect_tool`` equals its pinned dict (#1386).

    Whole-dict ``==`` equality, never a subset check and never a series of
    ``in`` checks, so an extraction that adds a key to one path or drops one
    from another fails here rather than reaching a consumer. The
    ``above_ceiling`` row additionally passes a flagging ``care_guard``: its
    absence from the response is what pins the ceiling gate above the care
    seam from the response side.
    """
    driver, expected = _EXIT_PATHS[path]
    lookup = _CountingLookup()

    result = driver(_vault(tmp_path), lookup)

    assert result == expected


_ESSAY_KEY = frozenset({"essay"})
_PRAXIS_KEY = frozenset({"related_praxis"})
_EDDIES_KEY = frozenset({"related_eddies"})
_BASE_OK_KEYS = frozenset(
    {"status", "tool", "tier_ceiling", "routed_tier", "notes", "essay_grounded"}
)


@pytest.mark.parametrize("has_essay", [False, True], ids=["no-essay", "essay"])
@pytest.mark.parametrize("has_praxis", [False, True], ids=["no-praxis", "praxis"])
@pytest.mark.parametrize("has_eddies", [False, True], ids=["no-eddies", "eddies"])
def test_the_optional_keys_are_omitted_not_empty(
    tmp_path: Path, has_essay: bool, has_praxis: bool, has_eddies: bool
) -> None:
    """The full 2x2x2 lattice of the three conditional keys (#1386).

    ``essay``, ``related_praxis`` and ``related_eddies`` are each **absent**
    when they do not qualify, never present-and-empty -- the compatibility
    claim a pre-#873 consumer relies on. The 0-of-3 corner is already pinned by
    ``test_the_fields_are_absent_not_empty_when_nothing_qualifies`` and the
    3-of-3 corner by
    ``test_compiled_pages_reach_a_caller_admitted_to_every_contributor``;
    the mixed corners existed nowhere, and they are exactly what a
    ``if related.praxis or related.eddies:`` collapse in the extracted
    response builder would silently break.
    """
    praxis = _CHAR_PRAXIS if has_praxis else []
    eddies = _CHAR_EDDIES if has_eddies else []

    result = reflect_tool(
        vault_path=_vault(tmp_path),
        content=_ENTRY,
        llm_factory=_RecordingFactory(
            _notes_payload(_CHAR_NOTE, essay=_CHAR_ESSAY if has_essay else None)
        ),
        retrieve=_no_retrieval,
        related_lookup=lambda *_args: RelatedCompiled(praxis, eddies),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    expected_keys = set(_BASE_OK_KEYS)
    if has_essay:
        expected_keys |= _ESSAY_KEY
    else:
        assert "essay" not in result
    if has_praxis:
        expected_keys |= _PRAXIS_KEY
    else:
        assert "related_praxis" not in result
    if has_eddies:
        expected_keys |= _EDDIES_KEY
    else:
        assert "related_eddies" not in result
    assert set(result) == expected_keys


@pytest.mark.parametrize("path", ["above_ceiling", "escalate", "unavailable"])
def test_a_non_answer_costs_no_compiled_layer_walk(tmp_path: Path, path: str) -> None:
    """A refusal, an escalation and an unavailable provider walk no corpus (#1386).

    ``_related`` runs *after* the model call on purpose. Pinned by
    non-invocation rather than by absent keys, because a hoisted lookup whose
    result is discarded leaves the response identical while still paying the
    walk -- and on the ``above_ceiling`` row, paying it for a caller the #846
    gate has just refused.
    """
    driver, _expected = _EXIT_PATHS[path]
    lookup = _CountingLookup()

    driver(_vault(tmp_path), lookup)

    assert lookup.calls == 0


@pytest.mark.parametrize(
    "path", ["ok", "empty", "escalate", "not_found", "unavailable"]
)
def test_every_exit_path_appends_exactly_one_audit_entry(
    tmp_path: Path, path: str
) -> None:
    """One ``creek.reflect`` audit append per call, on every exit path (#1386).

    The append is deliberately the first statement of ``reflect_tool``, above
    the ceiling gate, so a refused above-ceiling probe is still recorded -- and
    it is the *only* append, so the log cannot answer "did consumer X read
    fragment F?" by cardinality either. The ``above_ceiling`` path is already
    pinned by ``test_entry_ref_above_ceiling_is_refused_not_reflected`` and the
    ``not_found`` path end-to-end by
    ``tests/test_api_reflect.py::test_the_audit_record_names_the_consumer_and_stays_a_non_oracle``;
    these are the five paths nothing pinned.
    """
    driver, _expected = _EXIT_PATHS[path]
    vault = _vault(tmp_path)

    driver(vault, _CountingLookup())

    assert [e for e in _audit(vault) if e["tool"] == TOOL_NAME] != []
    assert len([e for e in _audit(vault) if e["tool"] == TOOL_NAME]) == 1


# --- Complexity budget (#1386) -------------------------------------------------------

# ``creek_mcp/`` sits outside ``scripts/complexity.sh``'s xenon gate (which
# targets ``creek/`` only), so nothing else in the repo stands between this
# function and its next silent complexity regression -- which is exactly how
# ``reflect_tool`` reached radon C (14) in one commit with every gate green.

_COMPLEXITY_BUDGET = 6
_BUDGETED = frozenset(
    {"reflect_tool", "_admit_entry", "_gather_grounding", "_reflection_response"}
)


def test_reflect_tool_and_its_helpers_stay_within_the_complexity_budget() -> None:
    """No function in ``reflect_tool``'s own decomposition exceeds radon CC 6 (#1386).

    Scored with radon's own ``cc_visit`` -- the same engine
    ``scripts/complexity.sh`` drives -- so this test cannot drift from the
    numbers quoted in review. ``_parse_notes`` (C 11) and ``_clean_notes``
    (B 9) are deliberately outside the budget: they are #826's, and naming
    the budgeted functions rather than asserting a module maximum is what
    keeps this test from either failing on their behalf or silently
    tightening their scope.
    """
    from pathlib import Path as _Path

    from radon.complexity import cc_visit

    import creek_mcp.tools.reflect as _reflect_module

    source = _Path(_reflect_module.__file__).read_text(encoding="utf-8")
    scored = {block.name: block.complexity for block in cc_visit(source)}
    over = {
        name: cc
        for name, cc in scored.items()
        if name in _BUDGETED and cc > _COMPLEXITY_BUDGET
    }
    assert over == {}


# --- Owner-scoped grounding session (#1034) ------------------------------------------

# The #1034 defect is one of **lifetime**, not of memoisation. That the
# specialist's two memo slots work is already proven three times over in
# ``tests/test_real_agents.py`` -- ``test_retrieval_reuses_linker_across_gather_
# calls`` (one model load per instance), ``test_retrieval_loads_cache_once_
# across_gather_calls`` (one parquet read per instance + vault) and
# ``test_retrieval_cache_hit_avoids_re_embedding``. What was missing is an owner
# to hold the instance: ``_default_retrieve`` built one inline in the call
# expression and dropped it, so every reflection started cold. These tests
# observe the lifetime through the wiring, never by calling the memo directly.

_SESSION_NOTE = {"quote": _GROUNDED_QUOTE, "kind": "reframe", "note": "yours"}


def _session_factory() -> _RecordingFactory:
    """Return a recording LLM factory that answers with one grounded note."""
    return _RecordingFactory(_notes_payload(_SESSION_NOTE))


def _count_specialist_builds(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Count ``RetrievalSpecialist`` constructions while still constructing them.

    Wraps and delegates rather than stubbing: a stubbed ``__init__`` would leave
    the instance without its memo slots, so the parquet and model-load counts
    beside it would be measuring a different object than production uses.

    Args:
        monkeypatch: The builtin fixture; restores the real ``__init__``.

    Returns:
        A list that gains one entry per construction.
    """
    built: list[object] = []
    real_init = RetrievalSpecialist.__init__

    def _counting_init(self: Any, **kwargs: Any) -> None:
        """Record the construction, then run the real initialiser."""
        built.append(self)
        real_init(self, **kwargs)

    monkeypatch.setattr(RetrievalSpecialist, "__init__", _counting_init)
    return built


def _count_cache_reads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Count ``EmbeddingLinker.load_cache`` parquet reads, still performing them.

    Args:
        monkeypatch: The builtin fixture; restores the real ``load_cache``.

    Returns:
        A list that gains one entry per parquet read.
    """
    reads: list[object] = []
    real_load = EmbeddingLinker.load_cache

    def _counting_load(self: Any, path: Path) -> Any:
        """Record the read, then perform the real one."""
        reads.append(path)
        return real_load(self, path)

    monkeypatch.setattr(EmbeddingLinker, "load_cache", _counting_load)
    return reads


def test_one_server_builds_one_retrieval_specialist_for_many_reflections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sentence_transformer: Any,
) -> None:
    """Three ``creek.reflect`` calls through one server pay the setup once (#1034).

    The MCP half of the defect, driven through ``server.call_tool`` rather than
    ``reflect_tool`` directly -- otherwise the test stops observing the wiring,
    which is the entire subject. Against unfixed code all three counts read
    ``3``: ``_default_retrieve`` evaluated ``RetrievalSpecialist().gather(...)``
    inline, so ``_get_cache``'s ``if self._cache is None`` guard and
    ``load_model``'s ``if self._model is None`` guard were both cold every call.

    The model-load count is the headline. It is corpus-size independent and is
    paid even on this four-fragment vault, because ``_load_sentence_transformer``
    carries no ``lru_cache`` and ``EmbeddingLinker._model`` is a per-instance
    slot -- so a per-call construction meant a model load from disk per call.

    The ``status`` assertion passes *today*, which is the point: the defect was
    invisible to every existing assertion in this file. The grounding assertion
    is what stops the counts from going green because grounding silently died.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    builds = _count_specialist_builds(monkeypatch)
    reads = _count_cache_reads(monkeypatch)
    factory = _session_factory()

    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        reflect_llm_factory=lambda: factory,
    )
    results = [
        _structured_reflect(server, TierCeiling.PERSONAL.value) for _ in range(3)
    ]

    assert [row["status"] for row in results] == ["ok", "ok", "ok"], results
    assert len(builds) == 1, f"{len(builds)} specialists built, expected 1"
    assert len(reads) == 1, f"{len(reads)} parquet reads, expected 1"
    assert mock_sentence_transformer.call_count == 1, "model loaded more than once"
    assert _tiers_grounded_in(factory.prompt or "") == {
        "open",
        "personal",
        "unclassified",
    }, "the counts went green because grounding died"


def _structured_reflect(server: Any, ceiling: str) -> dict[str, Any]:
    """Call ``creek.reflect`` on *server* and return its structured content.

    Args:
        server: The :class:`FastMCP` instance from ``build_server``.
        ceiling: The declared ``privacy_tier_ceiling`` value.

    Returns:
        The tool's structured result dict.
    """
    result = asyncio.run(
        server.call_tool(
            "creek.reflect",
            {"content": _ENTRY, "privacy_tier_ceiling": ceiling},
        )
    )
    return result[1] if isinstance(result, tuple) else result


def test_a_warmed_specialist_mutates_nothing_across_calls_at_any_ceiling(
    tmp_path: Path,
) -> None:
    """A shared specialist gains no instance state, at any ceiling (#1034).

    **The structural privacy guard**, and the one that makes the dangerous
    variant of this change unrepresentable rather than merely unwritten. Sharing
    a specialist is safe *only* because everything it holds is override-blind:
    the model handle and the tier-blind parquet id→vector map. The corpus record
    list, the ranked order, the top-k slice, the returned bundle, the caller's
    override and the live-embed counter are all locals of ``gather`` and must
    stay locals -- any one of them memoised on the instance would carry one
    caller's *admitted content* into the next caller's call, which is a privacy
    leak wearing an optimisation's clothes.

    So this asserts the instance **key set** by equality, not just the values: a
    future ``self._ranked = ranked`` fails here by name the moment it is written,
    without anyone having to think of the leak it would cause. The identity
    checks catch the other half -- a re-assignment of an existing slot, which
    would mean a second model load or a second parquet read.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    session = GroundingSession()
    specialist = session.specialist(vault)
    before = dict(vars(specialist))

    for ceiling in TierCeiling:
        _default_retrieve(_ENTRY, vault, to_privacy_override(ceiling), session=session)

    after = dict(vars(specialist))
    assert set(after) == {
        "_linker",
        "_cache",
        "_cache_vault",
        "_max_live_embeds",
    }, "a gather added or removed an instance attribute"
    assert after["_linker"] is before["_linker"], "the linker was rebuilt"
    assert after["_cache"] is before["_cache"], "the parquet map was rebuilt"
    assert after["_cache_vault"] == before["_cache_vault"]
    assert after["_max_live_embeds"] == before["_max_live_embeds"]


def test_a_shared_session_never_carries_an_above_ceiling_title_into_a_lower_call(
    tmp_path: Path,
) -> None:
    """A permissive call through a shared session cannot widen the next one.

    The behavioural half of the privacy guard above (#1034). Admission is
    re-decided per call -- ``gather`` runs ``_load_config`` and ``_load_corpus``
    on every call, before either memo is consulted -- so a preceding ``ALL``
    call through the *same* session must leave the following ``OPEN`` call
    exactly as narrow as it would have been alone.

    Asserted as a **set of tiers**, never as an ordered title string: the
    conftest model mock seeds each vector from ``hash(text)``, which
    ``PYTHONHASHSEED`` randomises per process, so multi-title ordering is not
    stable across runs. The first assertion is the control -- without it, the
    second could pass because grounding never fired at all.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    session = GroundingSession()

    permissive = _session_factory()
    reflect_tool(
        vault_path=vault,
        llm_factory=permissive,
        content=_ENTRY,
        session=session,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    narrow = _session_factory()
    reflect_tool(
        vault_path=vault,
        llm_factory=narrow,
        content=_ENTRY,
        session=session,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert _tiers_grounded_in(permissive.prompt or "") == set(_CORPUS)
    assert _tiers_grounded_in(narrow.prompt or "") == {"open"}
    for tier in ("personal", "unclassified", "intimate"):
        _assert_tier_left_no_trace(narrow.prompt or "", tier)


def test_the_grounding_session_holds_one_specialist_and_evicts_on_vault_change(
    tmp_path: Path,
) -> None:
    """The session keeps exactly one specialist, never a per-vault dictionary.

    A ``dict[Path, RetrievalSpecialist]`` would be unbounded per-request growth
    introduced by a change whose stated purpose is bounding cost: on ``/v1``
    production passes no ``vault_path``, so ``configured_vault`` re-reads
    ``creek_config.yaml`` every request, and a multi-vault or reconfigured host
    would accumulate one loaded sentence-transformer **and** one full id→vector
    map per distinct path for the life of the process.

    The third call is the load-bearing one: returning to vault A must build
    *afresh* rather than find A still cached, which is what proves the slot was
    replaced rather than added to.
    """
    vault_a = _vault(tmp_path / "a")
    vault_b = _vault(tmp_path / "b")
    _write_tiered_corpus(vault_a)
    _write_tiered_corpus(vault_b)
    session = GroundingSession()

    first = session.specialist(vault_a)
    assert session.specialist(vault_a) is first, "same vault rebuilt the specialist"
    second = session.specialist(vault_b)
    assert second is not first, "a new vault reused the old specialist"
    third = session.specialist(vault_a)

    assert third is not first, "vault A's specialist survived a switch to vault B"
    assert third is not second


def test_no_module_global_holds_a_specialist_linker_or_session() -> None:
    """No module-level binding or cache decorator owns the shared state (#1034).

    ``RetrievalSpecialist``'s own docstring forbids a module global, and the
    conftest's autouse sentence-transformer patch is the concrete reason: a
    process-lifetime instance would carry one test's ``MagicMock`` model into
    the next test, and the failure would surface far from its cause. An
    ``lru_cache`` on the grounder would do the same thing by another name.

    Owner-scoping is what replaces it, so this is the guard that keeps the
    replacement honest. Scanned with ``ast`` over the four modules that could
    plausibly acquire one, following the AST-pinning precedent in
    ``tests/test_v1_api_structure.py``.
    """
    import ast
    from pathlib import Path as _Path

    import creek.author.agents as _agents
    import creek_mcp.httpapi.app as _app
    import creek_mcp.server as _server
    import creek_mcp.tools.reflect as _reflect

    forbidden = {"RetrievalSpecialist", "EmbeddingLinker", "GroundingSession"}
    cached = {"lru_cache", "cache"}
    offenders: list[str] = []

    for module in (_reflect, _server, _app, _agents):
        source = _Path(module.__file__ or "").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                name = func.attr if isinstance(func, ast.Attribute) else None
                name = name or getattr(func, "id", None)
                if name in forbidden:
                    offenders.append(f"{module.__name__}: module-level {name}()")
        for inner in ast.walk(tree):
            if not isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for deco in inner.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                label = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else getattr(target, "id", None)
                )
                if label in cached:
                    offenders.append(f"{module.__name__}: @{label} on {inner.name}")

    assert offenders == [], offenders


def test_the_mcp_server_hands_reflect_tool_a_reusable_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_server`` really passes a ``session=``, and it really is reusable.

    Criterion 5 of the issue asked for this through ``retrieve=``, and that is
    **unsatisfiable**: ``_gather_grounding`` composes
    ``_Grounding(retrieve(entry, vault_path, override), [])``, so an injected
    ``retrieve`` returns ``list[str]`` and contributes *no* fragment ids by
    design -- as ``reflect_tool``'s own docstring states and
    ``test_injected_retrieve_bypasses_the_default_grounder`` pins. Routing
    production through it would empty ``source_ids``, break the #873 selection
    path and redden ``test_a_retrieval_seed_alone_selects_the_compiled_layer``.
    So the lifetime arrives on a **sibling** keyword instead, and ``retrieve=``
    keeps its exact contract and precedence.

    Recording the kwarg alone would prove only that *something* was passed, so
    this also drives the recorded session twice and asserts it hands back the
    same object -- the property the whole change exists to provide.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    seen: list[Any] = []

    def _recording_reflect_tool(**kwargs: Any) -> dict[str, Any]:
        """Record the ``session`` kwarg and answer with a minimal success."""
        seen.append(kwargs.get("session"))
        return {"status": "ok", "notes": []}

    monkeypatch.setattr("creek_mcp.server.reflect_tool", _recording_reflect_tool)
    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        reflect_llm_factory=_session_factory,
    )
    _structured_reflect(server, TierCeiling.OPEN.value)

    assert seen and seen[0] is not None, "creek.reflect passed no session"
    session = seen[0]
    assert session.specialist(vault) is session.specialist(vault)


def test_a_shared_session_whose_gather_fails_still_degrades_to_no_grounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sharing the specialist does not remove the degradation path (#1034).

    ``test_retrieval_failure_degrades_to_no_grounding`` and
    ``test_retrieval_failure_logs_the_exception_type_not_its_message`` already
    pin this for the per-call grounder; the risk a shared session introduces is
    that the failure is now raised by an object built on a *previous* call, so
    this re-asserts both halves on the shared path.

    Reuses ``_patch_gather_to_raise`` rather than writing a second helper: it
    patches the class attribute, which is what makes the lazy in-body import in
    ``_default_retrieve`` pick up the substitution.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    session = GroundingSession()
    session.specialist(vault)
    _patch_gather_to_raise(monkeypatch, RuntimeError(_RETRIEVAL_FAILURE_SENTINEL))
    factory = _session_factory()

    with caplog.at_level(logging.DEBUG):
        result = reflect_tool(
            vault_path=vault,
            llm_factory=factory,
            content=_ENTRY,
            session=session,
            privacy_tier_ceiling=TierCeiling.OPEN,
        )

    assert result["status"] == "ok", result
    assert _source_section(factory.prompt or "").strip() == _NO_GROUNDING
    assert "RuntimeError" in caplog.text
    assert _RETRIEVAL_FAILURE_SENTINEL not in caplog.text


def test_a_session_that_cannot_warm_stores_nothing_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed session build degrades, caches nothing, and the next call retries.

    The half of criterion 19 that a "degrades to empty grounding" assertion
    alone would miss. If ``specialist()`` stored a half-built object -- or
    remembered the vault it failed on -- one transient model-load failure at
    boot would leave the process permanently ungrounded, and every later
    reflection would silently return no grounding with nothing in the logs to
    say why.
    """
    vault = _vault(tmp_path)
    _write_tiered_corpus(vault)
    session = GroundingSession()
    attempts: list[Path] = []

    def _failing_warm(_self: Any, warmed: Path) -> None:
        """Record the attempt, then fail as a cold model load would."""
        attempts.append(warmed)
        raise OSError("model weights unavailable")

    monkeypatch.setattr(RetrievalSpecialist, "warm", _failing_warm)
    grounding = _default_retrieve(
        _ENTRY, vault, to_privacy_override(TierCeiling.OPEN), session=session
    )

    assert grounding == ([], []), "a failed warm did not degrade to no grounding"
    assert vars(session)["_specialist"] is None, "a failed build was cached"
    assert vars(session)["_vault"] is None, "a failed build recorded its vault"

    monkeypatch.undo()
    assert session.specialist(vault) is not None, "the session never retried"
    assert len(attempts) == 1


def test_a_shared_session_still_selects_the_compiled_layer_from_one_pass(
    tmp_path: Path,
) -> None:
    """#873's one-pass selection survives the shared specialist (#1034).

    ``related_praxis`` is selected from the ids the *grounding* pass resolved,
    with no second embedding sweep. The request below carries inline ``content``
    and **no** ``entry_ref``, so ``from_entry`` is empty by construction and the
    compiled page can only be reached through a retrieval seed -- exactly the
    property that wiring production through ``retrieve=`` would have silently
    destroyed, since an injected grounder contributes no ids.

    Modelled on ``test_a_retrieval_seed_alone_selects_the_compiled_layer``,
    whose docstring records that this property was reachable but unobserved
    until it existed. Here it runs through the real grounder and a real session.
    """
    vault = _vault(tmp_path)
    entry_ref = _write_compiled_layer(vault, member_tier="open")
    session = GroundingSession()
    factory = _session_factory()

    result = reflect_tool(
        vault_path=vault,
        llm_factory=factory,
        content=_ENTRY,
        session=session,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result.get("status") == "ok", result
    # The seed really came from the grounding pass: the compiled page's own
    # fragment title is in the grounding block, and no ``entry_ref`` was sent.
    assert entry_ref in _source_section(factory.prompt or ""), factory.prompt
    assert [row["title"] for row in result["related_praxis"]] == [_PRAXIS_873], result


_BENCH_FRAGMENTS = 40


def _seed_bench_vault(vault: Path, count: int) -> list[str]:
    """Write *count* open-tier corpus fragments and return their ids.

    Args:
        vault: Vault root as created by :func:`_vault`.
        count: How many fragments to write.

    Returns:
        The fragment ids written.
    """
    ids = [f"frag-bench-{index:04d}" for index in range(count)]
    for frag_id in ids:
        _write_corpus_fragment(
            vault,
            frag_id=frag_id,
            title=f"TITLE-{frag_id}",
            body=f"BODY-{frag_id}",
            privacy_tier="open",
        )
    return ids


def _seed_bench_parquet(vault: Path) -> None:
    """Persist a warm embeddings parquet covering the whole admitted corpus.

    Vectors come from the autouse mock model, the same one the code under test
    uses, so a cache hit yields exactly the vector a live embed would -- which
    is what makes the warm cell's ``generate_embedding`` count meaningful rather
    than an artefact of a different embedder.

    Args:
        vault: Vault root whose corpus is embedded into the parquet.
    """
    from datetime import datetime as _dt

    from creek.author.agents import _load_config, _load_corpus
    from creek.link.embeddings import (
        CachedEmbedding,
        content_hash_for_text,
        embeddings_cache_path,
        fragment_embedding_text,
    )

    config = _load_config(vault)
    linker = EmbeddingLinker(config.embeddings)
    entries = {
        fragment.id: CachedEmbedding(
            fragment_id=fragment.id,
            content_hash=content_hash_for_text(fragment_embedding_text(fragment)),
            model_name=config.embeddings.model,
            vector=linker.generate_embedding(fragment_embedding_text(fragment)),
            computed_at=_dt.now(tz=UTC),
        )
        for fragment, _body in _load_corpus(vault)
    }
    path = embeddings_cache_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    linker.save_cache(entries, path)


@pytest.mark.slow
def test_benchmark_grounding_setup_is_paid_once_per_process_not_per_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_sentence_transformer: Any,
    record_property: Any,
) -> None:
    """Measure the #1034 saving as counts across four cells (#1034).

    **Counts are the measurement; the wall time recorded beside them is
    structure only and must not be quoted as latency.** ``conftest``
    auto-mocks the sentence-transformer for every test in this suite, so an
    in-suite second is the cost of a ``MagicMock``, not of a model. Converting
    these counts to time needs a separately measured single-model-load and
    single-embed latency from a real-model run; the PR does that conversion and
    says which number came from where.

    Four cells: {per-call, shared} x {cold parquet, warm parquet}, three
    reflections each. The shared cells are what the fix buys; the per-call cells
    are what production did before it.
    """
    vault = _vault(tmp_path)
    ids = _seed_bench_vault(vault, _BENCH_FRAGMENTS)
    calls = 3
    override = to_privacy_override(TierCeiling.OPEN)

    for cache_state in ("cold", "warm"):
        if cache_state == "warm":
            _seed_bench_parquet(vault)
        for mode in ("per_call", "shared"):
            builds = _count_specialist_builds(monkeypatch)
            reads = _count_cache_reads(monkeypatch)
            embeds: list[str] = []
            real_embed = EmbeddingLinker.generate_embedding

            def _counting_embed(
                self: Any, text: str, _sink: Any = embeds, _real: Any = real_embed
            ) -> Any:
                """Record the embed, then perform the real one."""
                _sink.append(text)
                return _real(self, text)

            monkeypatch.setattr(EmbeddingLinker, "generate_embedding", _counting_embed)
            before_loads = mock_sentence_transformer.call_count
            session = GroundingSession() if mode == "shared" else None

            started = time.perf_counter()
            for _ in range(calls):
                _default_retrieve(_ENTRY, vault, override, session=session)
            elapsed = time.perf_counter() - started

            label = f"{mode}/{cache_state}"
            loads = mock_sentence_transformer.call_count - before_loads
            record_property(
                f"grounding-{label}",
                f"calls={calls} builds={len(builds)} parquet_reads={len(reads)} "
                f"embeds={len(embeds)} model_loads={loads} "
                f"mocked_seconds={elapsed:.4f}",
            )
            if mode == "shared":
                assert len(builds) == 1, f"{label}: {len(builds)} builds"
                assert len(reads) == 1, f"{label}: {len(reads)} parquet reads"
                assert loads <= 1, f"{label}: {loads} model loads"
            else:
                assert len(builds) == calls, f"{label}: {len(builds)} builds"
                assert len(reads) == calls, f"{label}: {len(reads)} parquet reads"
            if cache_state == "cold" and mode == "per_call":
                # The unbounded pre-fix shape: every admitted fragment, every call.
                assert len(embeds) == calls * (len(ids) + 1), f"{label}: {len(embeds)}"
            monkeypatch.undo()
