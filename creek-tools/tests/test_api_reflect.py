"""``POST /v1/reflections`` over the shared reflect tool (#1077).

The last and most safety-sensitive vertical. It touches acute-distress
escalation, model routing and content the caller may not be admitted to, and
:func:`creek_mcp.tools.reflect.reflect_tool` already enforces four guarantees
over all of it: the read-side ceiling gate sits *above* the care seam, INTIMATE
routes local through the tier-keyed factory, quotes are verbatim or dropped, and
care escalation happens *before* any model call.

This module asserts that the route inherits every one of them by delegating and
adds none of its own — and it asserts the negative half on spies rather than by
inference. "The model was not called" is a fact about the factory, not something
to read off a response that merely looks like a refusal.

**One of the issue's mapping instructions is refused here, deliberately.** See
:func:`test_an_unresolvable_entry_ref_is_privacy_refused_not_not_found`.

Every test stubs ``llm_factory``: no live model call, so the suite stays
deterministic and offline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
from starlette.testclient import TestClient

from creek.care.guardrail import CARE_SIGNAL, acute_distress_guard
from creek.models import PrivacyTier
from creek_mcp.api.models import ErrorCode, ReflectionStatus
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.httpapi.reflect import reflect_refusal_code
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.reflect import reflect_tool
from tests.adapter_parity import assert_parity, http_outcome, mcp_outcome
from tests.v1_api_support import (
    CONSUMER,
    REFLECTIONS_PATH,
    build_app,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
    snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import httpx

_OK: Final[int] = 200
_INVALID: Final[int] = 422
_REFUSED: Final[int] = 403
_UNAVAILABLE: Final[int] = 503

_ENTRY: Final[str] = (
    "zz-reflect-1077-zz I keep saying yes to things I do not want, and then "
    "resenting the person who asked."
)
"""The submitted entry. Distinctive, so "did anything echo it?" is checkable."""

_VERBATIM: Final[str] = "I keep saying yes to things I do not want"
"""A span that really is in :data:`_ENTRY`, so a well-behaved note survives."""

_HALLUCINATED: Final[str] = "I have always known exactly what I wanted"
"""A span that is *not* in :data:`_ENTRY`, so a fabricated note is dropped."""

_FRAGMENT_TITLE: Final[str] = "zz-grounding-title-1077-zz"
"""A grounding fragment's title, which must never reach the wire."""

_SUCCESS_KEYS: Final[tuple[str, ...]] = (
    "status",
    "tier_ceiling",
    "routed_tier",
    "notes",
    "essay_grounded",
)
"""The success facts the parity harness compares for this vertical."""


class _FactorySpy:
    """A stub tier-keyed LLM factory that records whether it was ever entered.

    "The model was not called" is the load-bearing half of both the care gate
    and the ceiling gate, and it cannot be read off a response — a route that
    called the model and *then* refused would answer identically. So it is
    observed on the seam.

    Attributes:
        tiers: One entry per ``factory(tier)`` call, in order.
        prompts: One entry per completion, in order.
    """

    def __init__(self, response: str = "") -> None:
        """Return *response* from every completion.

        Args:
            response: The raw model turn to hand back.
        """
        self._response = response
        self.tiers: list[PrivacyTier] = []
        self.prompts: list[str] = []

    def __call__(self, tier: PrivacyTier) -> Callable[[str], str]:
        """Return the completion callable for *tier*, recording the tier.

        Args:
            tier: The routing tier the tool derived.

        Returns:
            The stub completion callable.
        """
        self.tiers.append(tier)

        def _complete(prompt: str) -> str:
            self.prompts.append(prompt)
            return self._response

        return _complete

    @property
    def called(self) -> bool:
        """Whether the factory was entered at all.

        Returns:
            ``True`` once :meth:`__call__` has run.
        """
        return bool(self.tiers)


def _turn(*notes: dict[str, str], essay: str | None = None) -> str:
    """Return a model turn carrying *notes* in the shape the tool parses.

    Args:
        *notes: The raw notes the model claims to have found.
        essay: Optional free prose.

    Returns:
        The serialised turn.
    """
    payload: dict[str, Any] = {"notes": list(notes)}
    if essay is not None:
        payload["essay"] = essay
    return json.dumps(payload)


_GOOD_TURN: Final[str] = _turn(
    {"quote": _VERBATIM, "kind": "pattern", "note": "You notice the cost later."}
)
"""One well-formed note whose quote really is a span of the entry."""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the HTTP adapter."""
    yield seed_vault(tmp_path / "http")


@pytest.fixture
def mcp_vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a second seeded vault, so the two adapters never share a trail."""
    yield seed_vault(tmp_path / "mcp")


# --------------------------------------------------------------------------- #
# Corpus + request helpers
# --------------------------------------------------------------------------- #


def _write_fragment(
    vault_path: Path,
    *,
    frag_id: str,
    body: str,
    privacy_tier: str = "open",
    title: str = _FRAGMENT_TITLE,
) -> None:
    """Write one fragment an ``entry_ref`` can resolve to.

    Args:
        vault_path: The vault root.
        frag_id: The fragment id, which is also the ``entry_ref``.
        body: The fragment body, which becomes the entry.
        privacy_tier: The tier to stamp.
        title: The fragment title.
    """
    folder = vault_path / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content=body,
                type="fragment",
                id=frag_id,
                title=title,
                source={"platform": "journal", "author": "self"},
                privacy_tier=privacy_tier,
            )
        ),
        encoding="utf-8",
    )


def _post(
    vault_path: Path,
    *,
    body: dict[str, Any] | None = None,
    ceiling: str | None = None,
    factory: _FactorySpy | None = None,
) -> httpx.Response:
    """Send one reflection request and return the raw response.

    Args:
        vault_path: The vault the app is built over.
        body: The JSON body, or ``None`` for a canonical inline-content one.
        ceiling: The declared tier ceiling, or ``None`` to send no header.
        factory: The stub LLM factory, or ``None`` for a silent one.

    Returns:
        The response.
    """
    spy = factory if factory is not None else _FactorySpy(_GOOD_TURN)
    app = build_app(vault_path=vault_path, reflect_llm_factory=lambda: spy)
    with TestClient(app) as test_client:
        return test_client.post(
            REFLECTIONS_PATH,
            json={"content": _ENTRY} if body is None else body,
            headers=headers(ceiling=ceiling),
        )


def _audit_records(vault_path: Path) -> list[dict[str, Any]]:
    """Return every raw audit record in *vault_path*.

    Args:
        vault_path: The vault root.

    Returns:
        The decoded records, in write order.
    """
    log = vault_path / MCP_AUDIT_RELPATH
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------- #
# The tracer
# --------------------------------------------------------------------------- #


def test_an_open_inline_reflection_returns_verbatim_anchored_notes(
    vault: Path,
) -> None:
    """The proposed tracer, with a stubbed factory and no live model call.

    Args:
        vault: A seeded vault.
    """
    response = _post(vault, ceiling="open")
    body = envelope(response)

    assert response.status_code == _OK
    assert body["status"] == ReflectionStatus.OK.value
    assert [note["quote"] for note in body["notes"]] == [_VERBATIM]
    assert body["notes"][0]["kind"] == "pattern"
    assert body["essay_grounded"] is False


def test_a_hallucinated_span_is_dropped(vault: Path) -> None:
    """A quote that is not a span of the entry never reaches the client.

    The client re-anchors quotes to character offsets, so a fabricated span is
    not merely wrong — it is unanchorable, and would surface as the client
    highlighting the wrong words of the writer's own entry.

    Args:
        vault: A seeded vault.
    """
    factory = _FactorySpy(
        _turn(
            {"quote": _HALLUCINATED, "kind": "fear", "note": "Invented."},
            {"quote": _VERBATIM, "kind": "pattern", "note": "Real."},
        )
    )
    body = envelope(_post(vault, ceiling="open", factory=factory))

    assert [note["quote"] for note in body["notes"]] == [_VERBATIM]
    assert _HALLUCINATED not in json.dumps(body)


def test_a_turn_with_nothing_usable_is_an_explicit_empty_and_a_200(
    vault: Path,
) -> None:
    """``empty`` is a success carrying an empty list, not an error.

    Args:
        vault: A seeded vault.
    """
    factory = _FactorySpy(_turn({"quote": _HALLUCINATED, "kind": "fear", "note": "x"}))
    response = _post(vault, ceiling="open", factory=factory)
    body = envelope(response)

    assert response.status_code == _OK
    assert body["status"] == ReflectionStatus.EMPTY.value
    assert body["notes"] == []


def test_essay_grounded_is_always_present_and_false(vault: Path) -> None:
    """The serializer cannot omit the flag, and cannot claim ``True``.

    ``essay`` is free model prose and is never verbatim-checked the way
    ``notes[].quote`` is. A client that read a missing field as "unknown", or a
    ``True`` as "safe to cite", would present ungrounded text as the writer's
    own.

    Args:
        vault: A seeded vault.
    """
    factory = _FactorySpy(
        _turn(
            {"quote": _VERBATIM, "kind": "pattern", "note": "Real."},
            essay="Some free prose the model wrote.",
        )
    )
    body = envelope(_post(vault, ceiling="open", factory=factory))

    assert "essay_grounded" in body
    assert body["essay_grounded"] is False
    assert body["essay"] == "Some free prose the model wrote."


# --------------------------------------------------------------------------- #
# Care escalation — asserted on the spy
# --------------------------------------------------------------------------- #


_CARE_ENTRY: Final[str] = "I want to kill myself and I have a plan for tonight."
"""Synthetic acute-distress text, shaped to trip the production guard."""


def test_a_care_flagged_entry_escalates_without_ever_calling_the_model(
    vault: Path,
) -> None:
    """``escalate`` carries the real ``CARE_SIGNAL`` and the factory stays cold.

    The response alone cannot prove the model was skipped — a route that called
    it and discarded the answer would look the same. The spy can.

    Args:
        vault: A seeded vault.
    """
    factory = _FactorySpy(_GOOD_TURN)
    response = _post(
        vault, body={"content": _CARE_ENTRY}, ceiling="open", factory=factory
    )
    body = envelope(response)

    assert response.status_code == _OK
    assert body["status"] == ReflectionStatus.ESCALATE.value
    # The guard's own reason, read off the guard rather than off
    # ``CARE_SIGNAL["kind"]``. The two differ — the guard answers
    # ``acute_distress_markers`` and the signal is kinded ``acute_distress`` —
    # so the committed fixture at
    # ``docs/contracts/adepthood-v1/examples/reflections/care-escalation.json``,
    # which builds ``reason`` from the signal's kind, publishes a value the
    # server never sends. Filed as a fixture defect rather than papered over
    # here: the route must carry what the guard decided, not what a fixture
    # guessed.
    assert body["reason"] == acute_distress_guard(_CARE_ENTRY)
    assert body["care_signal"] == CARE_SIGNAL
    assert not factory.called


def test_the_escalation_is_not_an_error_code(vault: Path) -> None:
    """A person in acute distress must not land in a client's error path.

    Args:
        vault: A seeded vault.
    """
    body = envelope(_post(vault, body={"content": _CARE_ENTRY}, ceiling="open"))

    assert "code" not in body
    assert ReflectionStatus.ESCALATE.value not in {code.value for code in ErrorCode}


# --------------------------------------------------------------------------- #
# The ceiling gate
# --------------------------------------------------------------------------- #


def test_an_above_ceiling_entry_ref_is_refused_without_a_model_call(
    vault: Path,
) -> None:
    """The read gate refuses, the model stays cold, and the tier is not echoed.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="frag-secret", body=_ENTRY, privacy_tier="personal")
    factory = _FactorySpy(_GOOD_TURN)

    response = _post(
        vault, body={"entry_ref": "frag-secret"}, ceiling="open", factory=factory
    )

    assert response.status_code == _REFUSED
    assert envelope(response)["code"] == ErrorCode.PRIVACY_REFUSED.value
    assert not factory.called
    assert "personal" not in response.text
    assert "intimate" not in response.text


def test_an_unresolvable_entry_ref_is_privacy_refused_not_not_found(
    vault: Path,
) -> None:
    """#1077 asks for ``not_found`` here. That instruction is refused.

    :class:`~creek_mcp.api.models.ErrorCode.NOT_FOUND` is documented in
    ``creek_mcp/api/models.py`` as a **routing** code — "no such endpoint on
    this server" — and is explicitly forbidden for a vault object, because a
    caller who can tell "no such fragment" from "not for you" can enumerate the
    corpus one id at a time without reading a byte of it. That is precisely the
    oracle #846 / #970 / #972 / #1090 spent five issues collapsing, and
    ``tests/test_v1_api_structure.py`` AST-pins ``NOT_FOUND`` to the routing
    layer so a handler cannot emit it at all.

    So every vault-object non-answer collapses to ``privacy_refused``, exactly
    as that module prescribes. The adapter is therefore *stricter* than the MCP
    tool, which keeps ``"entry_ref not found"`` and ``"entry_ref tier exceeds
    ceiling"`` distinct as a documented accepted residual oracle; over HTTP the
    two are indistinguishable.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="frag-real", body=_ENTRY, privacy_tier="personal")

    missing = _post(vault, body={"entry_ref": "frag-nothing"}, ceiling="open")
    above = _post(vault, body={"entry_ref": "frag-real"}, ceiling="open")

    assert missing.status_code == above.status_code == _REFUSED
    assert (
        envelope(missing)["code"]
        == envelope(above)["code"]
        == ErrorCode.PRIVACY_REFUSED.value
    )
    assert envelope(missing)["message"] == envelope(above)["message"]


def test_an_admitted_entry_ref_is_reflected(vault: Path) -> None:
    """The converse, so the refusals above are about the ceiling and not the ref.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="frag-open", body=_ENTRY, privacy_tier="open")

    body = envelope(_post(vault, body={"entry_ref": "frag-open"}, ceiling="open"))

    assert body["status"] == ReflectionStatus.OK.value
    assert [note["quote"] for note in body["notes"]] == [_VERBATIM]


def test_a_remote_caller_declaring_an_intimate_ceiling_reads_nothing(
    vault: Path,
) -> None:
    """The adapter edge refuses first: no vault read, no model call, no trace.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="frag-open", body=_ENTRY)
    before = snapshot(vault)
    factory = _FactorySpy(_GOOD_TURN)

    response = _post(vault, ceiling="intimate", factory=factory)

    assert response.status_code == _INVALID
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert not factory.called
    assert snapshot(vault) == before


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [("open", PrivacyTier.OPEN), ("personal", PrivacyTier.PERSONAL)],
)
def test_the_routing_tier_dominates_the_admitted_entrys_tier(
    vault: Path, ceiling: str, expected: PrivacyTier
) -> None:
    """The tier handed to the factory is never below what the ceiling admits.

    The wire's ``routed_tier`` is safe to publish precisely because it is a
    constant function of the caller's own declared ceiling. This pins both
    halves for an HTTP caller: the value on the wire, and the value the factory
    was actually keyed with.

    Args:
        vault: A seeded vault.
        ceiling: The declared tier ceiling.
        expected: The routing tier the factory must be keyed with.
    """
    _write_fragment(vault, frag_id="frag-open", body=_ENTRY, privacy_tier="open")
    factory = _FactorySpy(_GOOD_TURN)

    body = envelope(
        _post(
            vault,
            body={"entry_ref": "frag-open"},
            ceiling=ceiling,
            factory=factory,
        )
    )

    assert factory.tiers == [expected]
    assert body["routed_tier"] == ceiling


# --------------------------------------------------------------------------- #
# Refusal mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("entry_ref not found", ErrorCode.PRIVACY_REFUSED),
        ("entry_ref tier exceeds ceiling", ErrorCode.PRIVACY_REFUSED),
        ("no entry content supplied", ErrorCode.INVALID_REQUEST),
        ("reflection unavailable: RuntimeError", ErrorCode.TEMPORARILY_UNAVAILABLE),
        ("something nobody has written yet", ErrorCode.INTERNAL_ERROR),
    ],
)
def test_every_tool_refusal_maps_onto_the_taxonomy(
    reason: str, code: ErrorCode
) -> None:
    """Each refusal the tool can produce has one published code.

    Args:
        reason: The tool's structured refusal reason.
        code: The wire code it must map to.
    """
    assert reflect_refusal_code(reason) == code


def test_an_unavailable_provider_is_temporarily_unavailable_and_names_no_message(
    vault: Path,
) -> None:
    """A raising factory becomes ``503``, carrying nothing of the exception.

    ``reflect_tool`` already reduces the exception to its *type name*; the wire
    keeps not even that, because :func:`error_response` renders one constant per
    code.

    Args:
        vault: A seeded vault.
    """
    secret = "zz-provider-internals-1077-zz"

    class _Raising(_FactorySpy):
        """A factory that refuses to build a model callable."""

        def __call__(self, tier: PrivacyTier) -> Callable[[str], str]:
            """Refuse, naming an internal detail that must not travel.

            Args:
                tier: The routing tier, recorded before refusing.

            Returns:
                Never returns.

            Raises:
                RuntimeError: Always.
            """
            self.tiers.append(tier)
            raise RuntimeError(secret)

    response = _post(vault, ceiling="open", factory=_Raising())

    assert response.status_code == _UNAVAILABLE
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert secret not in response.text
    assert "RuntimeError" not in response.text


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("both-sources", {"content": "x", "entry_ref": "y"}),
        ("neither-source", {}),
        ("blank-content", {"content": "   "}),
        ("max-notes-too-low", {"content": _ENTRY, "max_notes": 0}),
        ("max-notes-too-high", {"content": _ENTRY, "max_notes": 11}),
    ],
)
def test_a_malformed_request_is_refused_before_the_vault(
    vault: Path, label: str, body: dict[str, Any]
) -> None:
    """Exactly one source, bounded ``max_notes``, and nothing written.

    Args:
        vault: A seeded vault.
        label: The refusal being exercised, for the parametrize id.
        body: The request body.
    """
    assert label
    before = snapshot(vault)
    factory = _FactorySpy(_GOOD_TURN)
    response = _post(vault, body=body, ceiling="open", factory=factory)

    assert response.status_code == _INVALID
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert not factory.called
    assert snapshot(vault) == before


# --------------------------------------------------------------------------- #
# Disclosure
# --------------------------------------------------------------------------- #


def test_nothing_echoes_the_entry_a_grounding_title_or_a_fragment_id(
    vault: Path,
) -> None:
    """Not the response, not a refusal, not the audit trail.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="frag-open", body=_ENTRY, privacy_tier="open")
    ok = _post(vault, body={"entry_ref": "frag-open"}, ceiling="open")
    refused = _post(vault, body={"entry_ref": "frag-missing"}, ceiling="open")
    log = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")

    assert _FRAGMENT_TITLE not in ok.text
    assert _FRAGMENT_TITLE not in log
    assert "frag-open" not in ok.text
    assert "frag-missing" not in refused.text
    assert _ENTRY not in log
    assert not contains_a_path(refused.text)


def test_the_audit_record_names_the_consumer_and_stays_a_non_oracle(
    vault: Path,
) -> None:
    """``has_entry_ref`` and nothing else — never the probed id, never the outcome.

    ``reflect_tool`` logs once, above the ceiling gate, so a refused probe is
    still recorded; what it must never record is *which* id was probed or
    whether the probe succeeded. A record carrying either would answer "did
    consumer X read fragment F?" — the question the whole design withholds.

    Args:
        vault: A seeded vault.
    """
    _post(vault, body={"entry_ref": "frag-missing"}, ceiling="open")

    records = _audit_records(vault)
    assert [record["consumer"] for record in records] == [CONSUMER]
    assert [record["args_summary"] for record in records] == [{"has_entry_ref": True}]
    assert all("frag-missing" not in json.dumps(record) for record in records)
    assert all("status" not in record for record in records)


# --------------------------------------------------------------------------- #
# HTTP <-> MCP parity
# --------------------------------------------------------------------------- #


_SCENARIOS: Final[tuple[tuple[str, dict[str, Any], str], ...]] = (
    ("inline-open", {"content": _ENTRY}, "open"),
    ("inline-personal", {"content": _ENTRY}, "personal"),
    ("entry-ref-admitted", {"entry_ref": "frag-open"}, "open"),
    ("entry-ref-above-ceiling", {"entry_ref": "frag-secret"}, "open"),
    ("entry-ref-missing", {"entry_ref": "frag-nothing"}, "open"),
    ("care-escalation", {"content": _CARE_ENTRY}, "open"),
)
"""``(name, request body, declared ceiling)`` rows run through both adapters.

Every refusal branch is present, which is what #1077 asks of the harness: the
ceiling gate, the unresolvable ref, and the escalation that must not be an
error at all.
"""


@pytest.mark.parametrize(
    ("name", "body", "ceiling"), _SCENARIOS, ids=[row[0] for row in _SCENARIOS]
)
def test_http_and_mcp_agree_on_every_scenario(
    vault: Path,
    mcp_vault: Path,
    name: str,
    body: dict[str, Any],
    ceiling: str,
) -> None:
    """The same scenario, both adapters, identical behavioural outcome.

    Args:
        vault: The HTTP adapter's vault.
        mcp_vault: The MCP adapter's vault.
        name: The scenario name, surfaced in a divergence message.
        body: The request body.
        ceiling: The declared tier ceiling.
    """
    for root in (vault, mcp_vault):
        _write_fragment(root, frag_id="frag-open", body=_ENTRY, privacy_tier="open")
        _write_fragment(
            root, frag_id="frag-secret", body=_ENTRY, privacy_tier="personal"
        )

    over_http = _post(
        vault, body=body, ceiling=ceiling, factory=_FactorySpy(_GOOD_TURN)
    )
    over_mcp = reflect_tool(
        vault_path=mcp_vault,
        llm_factory=_FactorySpy(_GOOD_TURN),
        content=body.get("content"),
        entry_ref=body.get("entry_ref"),
        care_guard=acute_distress_guard,
        privacy_tier_ceiling=TierCeiling(ceiling),
        consumer=CONSUMER,
        max_notes=3,
    )

    assert_parity(
        name,
        http_outcome(over_http, vault, keys=_SUCCESS_KEYS),
        mcp_outcome(
            over_mcp,
            mcp_vault,
            keys=_SUCCESS_KEYS,
            classify=reflect_refusal_code,
            # ``escalate`` is a 200 on the wire, so it must normalise as a
            # success here too; classifying it as an error would make the
            # harness demand the very collapse the design forbids.
            success_statuses=(
                ReflectionStatus.OK.value,
                ReflectionStatus.EMPTY.value,
                ReflectionStatus.ESCALATE.value,
            ),
        ),
    )
