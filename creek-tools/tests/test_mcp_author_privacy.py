"""``creek.author`` must refuse, not publish, a draft the privacy gate condemned.

``creek_mcp.tools.author._draft_response`` used to build its success envelope
with ``"body": draft.body`` unconditionally. So when the Writing Desk's HARD
privacy check fired, the verb still answered ``status: "ok"`` — carrying a
``privacy_compliance``/``HIGH`` finding *and* the protected text itself, on the
wire three times over: in ``body``, in ``claims[*].claim``, and in
``provenance[*].claim_excerpt``. The gate detected the breach and then handed
the caller the breach.

Every pre-existing ``creek.author`` test probes at ``ceiling=open``, where the
#660 retrieval filter drops the intimate fragment before it can reach a draft
at all — which is exactly why the defect survived them. The breach here is
against a *different* gate: the medium contract's ``default_privacy_tier``
(``open`` in all six shipped templates). At ``ceiling=all`` the caller is
admitted to intimate content, the retrieval filter correctly lets it through,
and nothing downstream stops the drafted body from being returned.

Two of the tests below are controls rather than reproductions. They pin what
must NOT change: an ordinary ``ESCALATE`` still returns its body, and an
intimate fragment that never reaches the evidence never triggers a refusal.
Without them "fix the leak" is satisfiable by refusing everything.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.author import author_tool
from tests.helpers import write_raw_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "The night I wept alone in the parked car for one hour."
"""The protected text of the intimate fragment — deliberately short.

Its length is load-bearing in *both* directions, so do not lengthen it and do
not shorten it. 54 characters, 12 words:

* **Under 80 characters.** ``creek/author/conductor.py:177`` truncates every
  provenance entry to ``claim_excerpt=claim.claim[:_EXCERPT_LEN]``, and
  ``_EXCERPT_LEN`` is 80. ``body`` is never truncated, so a long secret is
  caught there — but it is *not* caught in ``claims`` or ``provenance``, which
  is exactly the half-fixed tree ("scrub the key I remembered") that the
  whole-envelope sweep in
  :func:`test_privacy_leak_is_absent_from_the_whole_serialized_response` exists
  to catch. MEASURED against a body-scrubbed envelope: at 54 characters the
  sweep fails on ``claims``/``provenance``; at 141 characters it passes while
  the first **80 characters** of the intimate sentence still ship. A short
  secret is what makes the sweep honest about the two keys nobody remembers.
* **At least four words.** ``_MIN_PROTECTED_LEAK_WORDS``
  (``creek/author/checks.py:228``) is 4: a shorter snippet is treated as
  coincidental overlap and never trips the HARD gate, so the fixture would
  quietly stop reproducing the defect at all.
"""

_LEAK_FRAGMENT_ID = "frag-int"
"""Id of the intimate fragment whose protected text the draft leaks."""

_HOSTILE_FRAGMENT_ID = "frag[bold]x"
"""A fragment id carrying console-markup brackets (see the #1352 analogue)."""

_LEAK_QUERY = "parked car"
"""Query that retrieves the intimate fragment without quoting it."""

_MIXED_SECRET = "Origins and the solo pitch I wept about in the parked car."
"""Protected text that trips ``privacy_compliance`` **and** three other findings.

:data:`SECRET` is deliberately plain English, so a draft leaking it carries
exactly one finding — which would make
:func:`test_refusal_carries_only_privacy_dimension_findings` vacuous: with a
single privacy finding in the set, widening the dimension filter to admit
everything changes nothing and the test stays green. This wording smuggles in
three legacy taxonomy aliases (``origins`` → ``rising``, ``solo`` →
``inhabit``, ``pitch`` → ``F2``; INC-019) so the same leaking draft also
carries three ``ontological_accuracy``/``MID`` findings, and the filter has
something to actually exclude.

MEASURED at ``ceiling=all``: findings are
``[privacy_compliance/HIGH, and ontological_accuracy/MID three times]``, and
the body still
reproduces this sentence verbatim. 58 characters, 12 words — same two length
constraints as :data:`SECRET`, for the same two reasons.
"""


def _seed_leaking_fragment(vault: Path, frag_id: str = _LEAK_FRAGMENT_ID) -> None:
    """Seed one intimate fragment whose title and body are both :data:`SECRET`.

    ``title == body`` is what makes the leak reachable with **no LLM at all**,
    which is the hermetic CI-default path this lane runs on:
    ``creek/author/agents.py:145`` builds each ``EvidenceClaim`` from the
    fragment *title*, ``creek/author/voice.py:134`` renders the deterministic
    body as ``- {claim.claim}`` per claim, and ``check_privacy_compliance``
    matches fragment *bodies*. Only when the two are identical does the
    deterministic renderer reproduce protected text verbatim and trip the gate.

    Args:
        vault: Vault root to seed (the helper creates the subdirectory).
        frag_id: Fragment id and file stem.
    """
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        frag_id,
        SECRET,
        body=SECRET,
        privacy_tier="intimate",
    )


def _author(
    vault: Path,
    *,
    ceiling: TierCeiling,
    query: str = _LEAK_QUERY,
) -> dict[str, Any]:
    """Invoke ``creek.author`` the way an MCP client would.

    ``medium="research"`` is the medium every existing author test uses, and
    its contract carries ``default_privacy_tier: open`` — the ceiling the HARD
    privacy check measures each cited fragment against.

    Args:
        vault: Vault to author from.
        ceiling: The caller's ``privacy_tier_ceiling``.
        query: The user query to author about.

    Returns:
        The raw MCP envelope, asserted on directly rather than through any
        model, because the envelope *is* the wire.
    """
    return author_tool(
        vault_path=vault,
        query=query,
        medium="research",
        privacy_tier_ceiling=ceiling,
        consumer="test",
    )


def _privacy_messages(response: dict[str, Any]) -> list[str]:
    """Return every ``privacy_findings`` message carried by *response*.

    Args:
        response: A refusal envelope.

    Returns:
        The finding messages, in envelope order.
    """
    return [str(finding["message"]) for finding in response["privacy_findings"]]


@pytest.fixture
def leaking_vault(tmp_path: Path) -> Path:
    """Return a vault holding exactly one intimate fragment that gets leaked.

    One fragment, not several, so no assertion here can pass by accident on a
    neighbouring open fragment's text.

    Args:
        tmp_path: Pytest's per-test temporary directory, used as the vault root.

    Returns:
        The vault root, seeded and ready to author from.
    """
    _seed_leaking_fragment(tmp_path)
    return tmp_path


def test_privacy_leak_is_absent_from_the_whole_serialized_response(
    leaking_vault: Path,
) -> None:
    """No protected text anywhere in the envelope — the acceptance criterion.

    Written against ``json.dumps`` of the *whole* response rather than against
    a list of named keys, precisely so it cannot be satisfied by scrubbing only
    the key someone remembered. Today the same sentence rides out three times
    (``body``, ``claims[*].claim``, ``provenance[*].claim_excerpt``); a
    key-by-key assertion would go green the moment ``body`` alone was cleared.
    The serialized sweep also covers whatever key a later refactor invents.
    """
    resp = _author(leaking_vault, ceiling=TierCeiling.ALL)

    assert SECRET not in json.dumps(resp, default=str)


def test_privacy_leak_refuses_rather_than_returning_ok(leaking_vault: Path) -> None:
    """The verb refuses; ``status: "ok"`` on a condemned draft is the bug.

    ``"refused"`` is the MCP surface's existing vocabulary for "I will not
    return this content" (``creek_mcp/tier_ceiling.py:264``; already emitted by
    ``creek.link``, ``creek.report``, ``creek.state.read``, ``creek.journal``
    and ``creek.upload``), so every consumer's ``status != "ok"`` branch
    handles it for free. Not ``"error"``: nothing malfunctioned, and
    ``_error_response`` owns the malformed/blew-up path.

    ``tier_ceiling_enforced`` stays ``True`` — it is a claim about the #660
    retrieval filter, which ran and did its job. The breach is against the
    medium contract's ``default_privacy_tier``, a different gate.
    """
    resp = _author(leaking_vault, ceiling=TierCeiling.ALL)

    assert resp["status"] == "refused"
    assert resp["tool"] == "creek.author"
    assert "privacy" in resp["reason"].lower()
    assert resp["tier_ceiling"] == "all"
    assert resp["tier_ceiling_enforced"] is True
    assert resp["dry_run"] is False
    assert resp["medium"] == "research"
    assert resp["query"] == _LEAK_QUERY


def test_refused_envelope_omits_body_claims_and_provenance(
    leaking_vault: Path,
) -> None:
    """The content keys are *absent*, not emptied.

    These are membership tests, never truthiness tests, and that distinction is
    the point: ``""`` and ``None`` must both fail them. A falsy-but-present
    ``body`` is exactly the shape that lets a caller print nothing and believe
    it published a draft. With the key gone, ``resp["body"]`` raises
    ``KeyError`` and a naive consumer fails loudly at the moment of the
    refusal instead of shipping an empty page.
    """
    resp = _author(leaking_vault, ceiling=TierCeiling.ALL)

    assert "body" not in resp
    assert "claims" not in resp
    assert "provenance" not in resp


def test_refusal_names_the_offending_fragment_and_tier(leaking_vault: Path) -> None:
    """The refusal is actionable: it names the fragment id and its tier.

    Without this, ``"refused"`` is a dead end — an operator cannot tell which
    of N cited fragments tripped the gate, and the only recovery is guesswork.

    Naming the id is not a tier oracle. At ``TierCeiling.PERSONAL`` the same
    intimate fragment is excluded from the evidence outright (``provenance ==
    []``, pinned by
    :func:`test_a_ceiling_that_excludes_the_fragment_returns_an_ordinary_draft`),
    so a finding can only ever name a fragment the caller's own ceiling already
    admitted them to read. The id discloses no membership fact they lacked.
    """
    resp = _author(leaking_vault, ceiling=TierCeiling.ALL)

    assert "privacy_findings" in resp
    messages = _privacy_messages(resp)
    assert any(_LEAK_FRAGMENT_ID in message for message in messages)
    assert any("intimate" in message for message in messages)


def test_refusal_carries_only_privacy_dimension_findings(tmp_path: Path) -> None:
    """Only ``privacy_compliance`` findings are echoed — the others re-leak.

    This is the regression test for the obvious over-fix. Dumping all of
    ``draft.findings`` into the refusal would undo the scrub, because two
    sibling checks interpolate corpus text into their own messages:
    ``biographical_grounding`` embeds ``finding.sentence!r``, a sentence lifted
    straight out of the drafted body (``creek/author/checks.py:176``), and
    ``attribution_correctness`` embeds ``claim.claim!r`` — the cited fragment's
    title, on the deterministic path (``checks.py:536``). The
    ``privacy_compliance`` message is corpus-free by construction
    (``checks.py:342-346``): fragment id and tier, nothing else.
    Hence the key is ``privacy_findings``, filtered to the one dimension, and
    not ``findings``.

    It runs on :data:`_MIXED_SECRET` rather than the shared ``leaking_vault``
    for one specific reason, found by mutating the filter and watching nothing
    go red: a draft leaking the plain :data:`SECRET` carries a set of exactly
    one finding, so ``all(dimension == "privacy_compliance")`` is satisfied by
    a filter that excludes nothing at all. The assertion only bites when the
    condemned draft carries findings the filter is *obliged* to drop — here,
    three ``ontological_accuracy`` ones — which is what ``== 1`` and the
    ``"Legacy taxonomy term"`` sweep pin. Widening the filter makes this test
    fail three ways.
    """
    write_raw_fragment_file(
        tmp_path,
        "01-Fragments/Notes",
        _LEAK_FRAGMENT_ID,
        _MIXED_SECRET,
        body=_MIXED_SECRET,
        privacy_tier="intimate",
    )

    resp = _author(tmp_path, ceiling=TierCeiling.ALL)

    assert resp["status"] == "refused"
    assert len(resp["privacy_findings"]) == 1
    assert all(
        finding["dimension"] == "privacy_compliance"
        for finding in resp["privacy_findings"]
    )
    assert all(finding["severity"] == "HIGH" for finding in resp["privacy_findings"])
    # The excluded findings' own messages must not ride along either.
    assert "Legacy taxonomy term" not in json.dumps(resp, default=str)
    assert _MIXED_SECRET not in json.dumps(resp, default=str)


def test_an_ordinary_escalate_still_returns_the_body(tmp_path: Path) -> None:
    """An ``ESCALATE`` with no privacy finding still ships its draft.

    **This test passes today and must still pass afterwards.** It is not
    redundant with the suite's green baseline — it is the control that stops
    the fix from degenerating into "always refuse", and it is why the trigger
    is the finding *dimension* and never the verdict. Several existing MCP
    author runs return ``verdict=ESCALATE`` with zero findings, and one returns
    ESCALATE carrying only ``ontological_accuracy``/``unglossed_jargon``
    (the legacy-taxonomy aliases this fixture's wording trips on purpose).
    Gating on the verdict would withhold every one of those ordinary drafts.
    """
    write_raw_fragment_file(
        tmp_path,
        "01-Fragments/Notes",
        "frag-a",
        "Origins and the solo pitch of agency",
        body="Origins and the solo pitch of agency.",
        privacy_tier="open",
    )

    resp = _author(tmp_path, ceiling=TierCeiling.OPEN, query="origins")

    assert resp["status"] == "ok"
    assert resp["verdict"] == "ESCALATE"
    assert resp["body"].strip()
    assert resp["provenance"]


def test_a_ceiling_that_excludes_the_fragment_returns_an_ordinary_draft(
    leaking_vault: Path,
) -> None:
    """An intimate fragment merely *on disk* is not grounds for refusal.

    **This test passes today and must still pass afterwards.** Same vault as
    the leak case, one notch down the ceiling: at ``PERSONAL`` the #660
    retrieval filter excludes the intimate fragment before the desk ever sees
    it, so there is no evidence, no protected text in the body, and no finding.
    It pins that the refusal is driven by an *actual leak* in the drafted
    prose, not by the presence of restricted content somewhere in the vault —
    a fix keyed on "the vault contains intimate fragments" would break here.

    It is also the evidence for the tier-oracle argument in
    :func:`test_refusal_names_the_offending_fragment_and_tier`: an
    under-privileged caller never receives a finding naming this fragment,
    because no finding is raised at all.
    """
    resp = _author(leaking_vault, ceiling=TierCeiling.PERSONAL)

    assert resp["status"] == "ok"
    assert resp["provenance"] == []
    assert SECRET not in json.dumps(resp, default=str)


def test_a_hostile_fragment_id_survives_the_json_envelope(tmp_path: Path) -> None:
    """A ``[...]``-bearing fragment id reaches the refusal message verbatim.

    The JSON-surface analogue of the Rich-markup class PR #1352 fixed on the
    CLI — asked the other way round, and answered the other way round.
    ``creek_mcp`` imports no ``rich`` anywhere on this path; CrawDad renders
    this envelope into an LLM prompt or Discord markdown, never into a Rich
    console. So ``rich.markup.escape`` is deliberately **not** applied here:
    escaping would mangle a real fragment id into ``frag\\[bold]x`` inside the
    one message an operator uses to go find the offending file, trading a real
    defect for a cosmetic one. Copying #1352's *fix* would be cargo-culting;
    its *reasoning* — never let content be interpreted as markup by whatever
    renders it — is already satisfied on a JSON wire by ``json.dumps``, which
    quotes the brackets as data. The round-trip assertion is what proves that,
    rather than leaving it as an assumption.
    """
    _seed_leaking_fragment(tmp_path, frag_id=_HOSTILE_FRAGMENT_ID)

    resp = _author(tmp_path, ceiling=TierCeiling.ALL)

    assert resp["status"] == "refused"
    assert "privacy_findings" in resp
    assert any(_HOSTILE_FRAGMENT_ID in msg for msg in _privacy_messages(resp))
    assert json.loads(json.dumps(resp)) == resp
