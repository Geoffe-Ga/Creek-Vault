"""``creek author`` must say when it found nothing to stand on.

Issue #1261. At the default ``open`` ceiling a fragment carrying no concrete
``privacy_tier`` is excluded from evidence gathering -- #1079 settled that a
missing tier resolves restrictively, and that filter is **not** changed here.
What was wrong is that a freshly-ingested, not-yet-classified vault produced a
confident-looking draft grounded in nothing, and said so only in a lowercase
``(no grounded evidence)`` fallback inside the body itself
(``creek/author/voice.py:222``).

``_emit_author_result`` did print ``provenance=0``, so the run was not
literally silent -- but a count is not a cause, and nothing named the remedy.

**Why these tests drive the two emitters directly.** Running the whole author
command would need a provider, and the surrounding suite's author tests are
exactly the ones that fail on a developer machine without credentials. The
behaviour under test is "given a draft with no provenance, what does each
surface emit", which is a pure function of the draft -- so the draft is
constructed and handed straight to the renderer. That also makes the negative
case (a grounded draft must NOT warn) trivial to state, which is half the
point: a warning that always fires is not a signal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from creek.author.models import (
    ZERO_EVIDENCE_WARNING,
    AuthoredDraft,
    has_zero_evidence,
)
from creek.cli import _emit_author_result
from creek.compile.provenance import ProvenanceEntry
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.author import _draft_response

if TYPE_CHECKING:
    import pytest


def _entry() -> ProvenanceEntry:
    """Return one grounded provenance entry.

    Returns:
        A :class:`~creek.compile.provenance.ProvenanceEntry` standing for a
        single cited claim.
    """
    return ProvenanceEntry(
        claim_id="claim-001",
        claim_excerpt="The vault says something about walking",
        fragment_ids=["frag-001"],
        compiled_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        compile_method="llm",
    )


def _draft(*, grounded: bool) -> AuthoredDraft:
    """Build a draft with or without provenance.

    Args:
        grounded: When ``True`` the draft cites one claim; when ``False`` it
            cites none, which is the #1261 condition.

    Returns:
        The assembled :class:`~creek.author.models.AuthoredDraft`.
    """
    return AuthoredDraft(
        medium="research",
        query="what have I been circling?",
        body="A paragraph of drafted prose.",
        provenance=[_entry()] if grounded else [],
        verdict="ESCALATE",
        rounds=1,
    )


def test_the_condition_is_keyed_on_provenance_not_on_the_verdict() -> None:
    """Zero provenance is the trigger; ``ESCALATE`` is not.

    Escalation is routine on this surface -- an empty vault escalates, and any
    unresolved soft finding escalates once the round budget runs out -- so a
    verdict-keyed warning would fire on ordinary grounded drafts. Both drafts
    here carry ``ESCALATE`` precisely so that confusion cannot pass.
    """
    assert has_zero_evidence(_draft(grounded=False)) is True
    assert has_zero_evidence(_draft(grounded=True)) is False


def test_the_cli_warns_when_a_draft_cites_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A zero-evidence run names the cause and the remedy, not just a count."""
    _emit_author_result(_draft(grounded=False))
    out = capsys.readouterr().out

    assert "provenance=0" in out, f"the summary line regressed: {out!r}"
    assert "creek classify" in out, (
        "the warning does not name the remedy, so an operator is told the "
        f"draft is ungrounded but not what to do about it: {out!r}"
    )
    assert "unclassified" in out, (
        f"the warning does not name the probable cause: {out!r}"
    )


def test_the_cli_stays_quiet_on_a_grounded_draft(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The negative half. A warning that always fires carries no information.

    Without this, ``test_the_cli_warns_when_a_draft_cites_nothing`` is
    satisfied by printing the warning unconditionally.
    """
    _emit_author_result(_draft(grounded=True))
    out = capsys.readouterr().out

    assert "provenance=1" in out
    assert "creek classify" not in out, (
        f"a grounded draft was warned about as if it had no evidence: {out!r}"
    )


def test_the_mcp_twin_carries_the_same_signal() -> None:
    """The MCP surface must not be the one left guessing (#1261 AC 3).

    The issue's table reads as a CLI-vs-MCP divergence (0 claims vs 7), but
    the two default to the same ceiling -- ``author_tool``'s
    ``privacy_tier_ceiling`` is ``TierCeiling.OPEN``, exactly the CLI's
    default. That gap was the probe's chosen ceiling, not a surface
    difference, so the criterion means "carry the same signal", which this
    pins on both sides of the condition.
    """
    refused = _draft_response(_draft(grounded=False), tier_ceiling=TierCeiling.OPEN)
    assert refused["warnings"] == [ZERO_EVIDENCE_WARNING]

    grounded = _draft_response(_draft(grounded=True), tier_ceiling=TierCeiling.OPEN)
    assert grounded["warnings"] == []


def test_both_surfaces_quote_one_shared_string() -> None:
    """The CLI and MCP wordings cannot drift apart.

    Two hand-written messages describing one condition is how the four
    unlinked copies in #1362 happened. The constant is defined once in
    ``creek.author.checks`` and both surfaces render it.
    """
    envelope = _draft_response(_draft(grounded=False), tier_ceiling=TierCeiling.OPEN)
    assert envelope["warnings"][0] is ZERO_EVIDENCE_WARNING

    assert "creek classify" in ZERO_EVIDENCE_WARNING
    assert "unclassified" in ZERO_EVIDENCE_WARNING
    assert "--include-tier" in ZERO_EVIDENCE_WARNING, (
        "the warning omits the deliberate override, so an operator who really "
        "does want to draft from unclassified material is not told how"
    )
