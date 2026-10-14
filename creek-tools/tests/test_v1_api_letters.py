"""``POST /v1/letters/{external_id}`` — the vault writes a Voice Draft letter (#1862).

A vault-bound Adepthood writer's AI operations must stay inside their vault, so
the letter that expands a margin note is written *here* and stored as the same
AI-attributed Voice Draft ``PUT /v1/voice-drafts/{external_id}`` writes. These
tests drive the real Starlette adapter and observe the model seam on a spy:
"the model was never reached" is a fact about the factory, not something to
read off a response that merely looks like a refusal.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
import yaml
from starlette.testclient import TestClient

from creek._fslock import VaultLockTimeoutError
from creek.care.guardrail import CARE_SIGNAL
from creek.classify.llm.consent import CLOUD_CONSENT_ENV
from creek.classify.llm.router import IntimateRoutingError
from creek.config import CreekConfig
from creek.models import PrivacyTier
from creek.voice_drafts import (
    VoiceDraftAccessDeniedError,
    VoiceDraftAction,
    VoiceDraftDocument,
    VoiceDraftStorageError,
    create_voice_draft_if_absent,
)
from creek_mcp.api.models import (
    CAPABILITY_SINCE_MINOR,
    CONTRACT_MINOR,
    Capability,
    CareEscalationResponse,
    ErrorCode,
    LetterResponse,
    VoiceDraftAttribution,
    VoiceDraftReadResponse,
)
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tools.letter import (
    LETTER_MAX_OUTPUT_TOKENS,
    MAX_LETTER_CHARS,
    LetterSource,
    build_letter_prompt,
)
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    build_app,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

_OK: Final[int] = 200
_PRIVACY_REFUSED: Final[int] = 403
_INCOMPATIBLE: Final[int] = 409
_INVALID_REQUEST: Final[int] = 422
_INTERNAL_ERROR: Final[int] = 500
_UNAVAILABLE: Final[int] = 503
_OLD_MINOR: Final[str] = "0.16"
_EXTERNAL_ID: Final[str] = "adepthood-voicedraft-1862aa"
_LETTERS_PATH: Final[str] = f"/v1/letters/{_EXTERNAL_ID}"
_VOICE_DRAFT_PATH: Final[str] = f"/v1/voice-drafts/{_EXTERNAL_ID}"

_ENTRY: Final[str] = (
    "zz-entry-1862-zz I walked the long way home so I could hear the river "
    "before deciding anything."
)
"""The posted entry. Distinctive, so "did anything log it?" is checkable."""

_ANCHOR: Final[str] = "hear the river before deciding anything"
_NOTE: Final[str] = "zz-note-1862-zz You already know how to wait for clarity."
_LETTER: Final[str] = (
    "zz-letter-1862-zz Dear one, the river you walked toward is a practice "
    "you already keep."
)
"""What the stub model writes. Distinctive, so leak sweeps can find it."""

_ACUTE_ENTRY: Final[str] = "I am going to kill myself tonight."
"""Trips :func:`creek.care.guardrail.acute_distress_guard`."""


class _FactorySpy:
    """A tier-keyed letter LLM factory that records every entry.

    Attributes:
        tiers: One entry per ``factory(tier)`` call, in order.
        max_tokens: The output budget each call was handed.
        prompts: One entry per completion, in order.
    """

    def __init__(self, response: str = _LETTER, *, error: Exception | None = None):
        """Return *response* from every completion, or raise *error* on build.

        Args:
            response: The raw model turn to hand back.
            error: Raised from ``factory(tier)`` instead of building a client.
        """
        self._response = response
        self._error = error
        self.tiers: list[PrivacyTier] = []
        self.max_tokens: list[int] = []
        self.prompts: list[str] = []

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
        """Return the completion callable, recording routing and budget.

        Args:
            tier: The routing tier the tool derived.
            max_tokens: The hard output ceiling the tool derived.

        Returns:
            The stub completion callable.

        Raises:
            Exception: The configured *error*, when one was given.
        """
        self.tiers.append(tier)
        self.max_tokens.append(max_tokens)
        if self._error is not None:
            raise self._error

        def _complete(prompt: str) -> str:
            self.prompts.append(prompt)
            return self._response

        return _complete


def _request(
    *,
    content: str = _ENTRY,
    note: str = _NOTE,
    anchor_text: str = _ANCHOR,
    kind: str = "pattern",
    tier: str = "personal",
    title: str | None = None,
) -> dict[str, Any]:
    """Return one valid letter request body."""
    body: dict[str, Any] = {
        "content": content,
        "note": {"kind": kind, "anchor_text": anchor_text, "note": note},
        "tier": tier,
    }
    if title is not None:
        body["title"] = title
    return body


def _client(vault: Path, spy: _FactorySpy | None = None) -> TestClient:
    """Return a client whose letters route dials *spy* (never a real model)."""
    factory = spy if spy is not None else _FactorySpy()
    return TestClient(build_app(vault_path=vault, letter_llm_factory=lambda: factory))


def _post(
    test_client: TestClient,
    body: dict[str, Any] | None = None,
    *,
    ceiling: str = "personal",
    minor: str = CONTRACT_MINOR,
    external_id: str = _EXTERNAL_ID,
) -> httpx.Response:
    """POST one letter request through the real adapter."""
    return test_client.post(
        f"/v1/letters/{external_id}",
        json=_request() if body is None else body,
        headers=headers(ceiling=ceiling, minor=minor),
    )


def _draft_files(vault: Path) -> list[Path]:
    """Return the stored Voice Draft note files."""
    return sorted((vault / "11-Other-Authors" / "ai-as-user").glob("*.md"))


def _code(response: httpx.Response) -> tuple[int, str]:
    """Return the status and wire code of an error envelope."""
    return response.status_code, str(envelope(response)["code"])


# --------------------------------------------------------------------------- #
# Capability negotiation
# --------------------------------------------------------------------------- #


def test_letters_is_advertised_only_from_the_minor_that_publishes_it(
    tmp_path: Path,
) -> None:
    """A 0.16 client is not told about letters; a current client is."""
    vault = seed_vault(tmp_path / "vault")
    with _client(vault) as test_client:
        current = envelope(
            test_client.get(CAPABILITIES_PATH, headers=headers(ceiling="open"))
        )
        old = envelope(
            test_client.get(
                CAPABILITIES_PATH, headers=headers(ceiling="open", minor=_OLD_MINOR)
            )
        )

    assert Capability.LETTERS.value in current["capabilities"]
    assert Capability.LETTERS.value not in old["capabilities"]
    assert CAPABILITY_SINCE_MINOR[Capability.LETTERS] == "0.17"


def test_an_older_minor_is_refused_at_the_route_before_any_model_call(
    tmp_path: Path,
) -> None:
    """The route-level twin of the advertisement filter."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        response = _post(test_client, minor=_OLD_MINOR)

    assert _code(response) == (_INCOMPATIBLE, ErrorCode.INCOMPATIBLE_VERSION.value)
    assert spy.tiers == []
    assert _draft_files(vault) == []


# --------------------------------------------------------------------------- #
# The letter itself
# --------------------------------------------------------------------------- #


def test_a_letter_is_written_grounded_in_the_note_and_entry(tmp_path: Path) -> None:
    """The prompt carries the posted note, its anchor and the entry."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        response = _post(test_client)

    assert response.status_code == _OK
    parsed = LetterResponse.model_validate(envelope(response))
    assert parsed.letter == _LETTER
    assert parsed.external_id == _EXTERNAL_ID
    assert parsed.action.value == "created"
    assert parsed.tier.value == "personal"
    assert parsed.tier_ceiling.value == "personal"
    assert parsed.attribution == VoiceDraftAttribution()
    assert spy.max_tokens == [LETTER_MAX_OUTPUT_TOKENS]
    assert len(spy.prompts) == 1
    prompt = spy.prompts[0]
    for grounding in (_ENTRY, _ANCHOR, _NOTE, "pattern"):
        assert grounding in prompt


def test_the_letter_is_stored_as_an_ai_attributed_voice_draft(tmp_path: Path) -> None:
    """Same storage as ``PUT /v1/voice-drafts``: AI authorship, zero voice."""
    vault = seed_vault(tmp_path / "vault")
    with _client(vault) as test_client:
        written = LetterResponse.model_validate(envelope(_post(test_client)))
        recalled = test_client.get(
            _VOICE_DRAFT_PATH, headers=headers(ceiling="personal")
        )

    files = _draft_files(vault)
    assert len(files) == 1
    post = frontmatter.load(str(files[0]))
    assert post["voice_draft"]["external_id"] == _EXTERNAL_ID
    assert post["source"]["author"] == "ai"
    assert post["source"]["author_slug"] == "ai-as-user"
    assert post["voice_weight"] == 0.0
    assert post["privacy_tier"] == "personal"
    assert post.content == _LETTER
    read = VoiceDraftReadResponse.model_validate(envelope(recalled))
    assert read.fragment_id == written.fragment_id
    assert read.content == _LETTER


def test_the_stored_letter_is_retractable_by_external_id(tmp_path: Path) -> None:
    """The existing Voice Draft DELETE retracts a vault-written letter."""
    vault = seed_vault(tmp_path / "vault")
    with _client(vault) as test_client:
        assert _post(test_client).status_code == _OK
        deleted = test_client.delete(
            _VOICE_DRAFT_PATH, headers=headers(ceiling="personal")
        )

    assert deleted.status_code == _OK
    assert _draft_files(vault) == []


def test_a_repeat_returns_the_first_letter_without_a_second_dial(
    tmp_path: Path,
) -> None:
    """A note's first letter is kept: a retry after a lost ack costs nothing."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        first = LetterResponse.model_validate(envelope(_post(test_client)))
        again = LetterResponse.model_validate(
            envelope(_post(test_client, _request(note="A different note.")))
        )

    assert again.action.value == "unchanged"
    assert again.letter == first.letter == _LETTER
    assert again.fragment_id == first.fragment_id
    assert len(spy.prompts) == 1
    assert len(_draft_files(vault)) == 1


def test_an_explicit_title_is_stored(tmp_path: Path) -> None:
    """The caller may name the letter; otherwise the content-free default."""
    vault = seed_vault(tmp_path / "vault")
    with _client(vault) as test_client:
        titled = LetterResponse.model_validate(
            envelope(_post(test_client, _request(title="River letter")))
        )
        untitled = LetterResponse.model_validate(
            envelope(_post(test_client, external_id="adepthood-voicedraft-other"))
        )

    assert titled.title == "River letter"
    assert untitled.title == "Voice draft"


def test_an_over_long_completion_is_capped(tmp_path: Path) -> None:
    """The stored letter is bounded no matter what the model returns."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy("x" * (MAX_LETTER_CHARS + 50))
    with _client(vault, spy) as test_client:
        parsed = LetterResponse.model_validate(envelope(_post(test_client)))

    assert len(parsed.letter) == MAX_LETTER_CHARS


def test_a_blank_completion_stores_nothing_and_is_retryable(tmp_path: Path) -> None:
    """A whitespace turn is not a letter, so nothing is cached under the id."""
    vault = seed_vault(tmp_path / "vault")
    with _client(vault, _FactorySpy("   \n")) as test_client:
        response = _post(test_client)

    assert _code(response) == (_UNAVAILABLE, ErrorCode.TEMPORARILY_UNAVAILABLE.value)
    assert _draft_files(vault) == []


# --------------------------------------------------------------------------- #
# Privacy: nothing above the ceiling, nothing intimate, to a cloud provider
# --------------------------------------------------------------------------- #


def test_an_above_ceiling_letter_is_refused_before_the_model(tmp_path: Path) -> None:
    """A personal entry under an open ceiling never reaches any provider."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        response = _post(test_client, ceiling="open")

    assert _code(response) == (_PRIVACY_REFUSED, ErrorCode.PRIVACY_REFUSED.value)
    assert spy.tiers == []
    assert _draft_files(vault) == []


def test_an_intimate_tier_is_not_expressible_on_the_wire(tmp_path: Path) -> None:
    """``intimate`` is refused at the boundary, before routing exists."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        response = _post(test_client, _request(tier="intimate"))

    assert _code(response) == (_INVALID_REQUEST, ErrorCode.INVALID_REQUEST.value)
    assert spy.tiers == []


def test_the_routing_tier_is_never_below_the_entry_tier(tmp_path: Path) -> None:
    """An open entry under a personal ceiling still routes at personal."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        assert _post(test_client, _request(tier="open")).status_code == _OK
        assert (
            _post(
                test_client,
                _request(tier="open"),
                ceiling="open",
                external_id="adepthood-voicedraft-open",
            ).status_code
            == _OK
        )

    assert spy.tiers == [PrivacyTier.PERSONAL, PrivacyTier.OPEN]


def test_an_existing_personal_letter_is_hidden_from_an_open_caller(
    tmp_path: Path,
) -> None:
    """The stored tier is admitted too, so an open retry cannot read it back."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        assert _post(test_client).status_code == _OK
        hidden = _post(test_client, _request(tier="open"), ceiling="open")

    assert _code(hidden) == (_PRIVACY_REFUSED, ErrorCode.PRIVACY_REFUSED.value)
    assert len(spy.prompts) == 1


def test_a_router_refusal_is_retryable_and_stores_nothing(tmp_path: Path) -> None:
    """``IntimateRoutingError`` (no local backend) never falls back to egress."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy(error=IntimateRoutingError("no local backend"))
    with _client(vault, spy) as test_client:
        response = _post(test_client)

    assert _code(response) == (_UNAVAILABLE, ErrorCode.TEMPORARILY_UNAVAILABLE.value)
    assert spy.prompts == []
    assert _draft_files(vault) == []


def test_a_factory_that_cannot_be_built_is_retryable(tmp_path: Path) -> None:
    """A broken provider configuration is an unavailable model, not a crash."""
    vault = seed_vault(tmp_path / "vault")

    def _broken() -> _FactorySpy:
        msg = "unreadable config"
        raise OSError(msg)

    app = build_app(vault_path=vault, letter_llm_factory=_broken)
    with TestClient(app) as test_client:
        response = _post(test_client)

    assert _code(response) == (_UNAVAILABLE, ErrorCode.TEMPORARILY_UNAVAILABLE.value)
    assert _draft_files(vault) == []


def _cloud_generation_vault(tmp_path: Path) -> Path:
    """Scaffold a vault whose ``generation`` stage is a cloud provider."""
    vault = seed_vault(tmp_path / "vault")
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["generation"] = {
        **data["llm"]["default"],
        "provider": "anthropic",
        "model": "claude-test",
    }
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        yaml.dump(data, sort_keys=False), encoding="utf-8"
    )
    return vault


def test_the_production_factory_forces_an_intimate_letter_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording provider builder never sees a cloud config for intimate."""
    from creek_mcp.server import _build_letter_llm_factory

    vault = _cloud_generation_vault(tmp_path)
    built: list[str] = []

    class _Local:
        available = True

    def _record(cfg: Any) -> _Local:
        built.append(str(cfg.provider))
        return _Local()

    monkeypatch.setattr("creek.classify.llm.providers.build_provider", _record)
    _build_letter_llm_factory(vault)(PrivacyTier.INTIMATE, max_tokens=16)

    assert built == ["ollama"]


def test_without_cloud_consent_a_cloud_routed_letter_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consent latch is inherited: no consent, no egress, nothing stored."""
    monkeypatch.delenv(CLOUD_CONSENT_ENV, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    vault = _cloud_generation_vault(tmp_path)

    with TestClient(build_app(vault_path=vault)) as test_client:
        response = _post(test_client)

    assert _code(response) == (_UNAVAILABLE, ErrorCode.TEMPORARILY_UNAVAILABLE.value)
    assert _draft_files(vault) == []


# --------------------------------------------------------------------------- #
# Care
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [_request(content=_ACUTE_ENTRY), _request(note=_ACUTE_ENTRY)],
    ids=["entry", "note"],
)
def test_acute_distress_escalates_before_any_model_call(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    """An escalation is a 200 carrying the care signal, and no letter."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        response = _post(test_client, body)

    assert response.status_code == _OK
    parsed = CareEscalationResponse.model_validate(envelope(response))
    assert parsed.care_signal.model_dump(mode="json") == CARE_SIGNAL
    assert spy.tiers == []
    assert _draft_files(vault) == []


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        _request(content="   "),
        _request(note=" "),
        _request(anchor_text=""),
        _request(kind="Not A Kind!"),
        _request(title=" "),
        {"content": _ENTRY, "tier": "personal"},
    ],
    ids=[
        "blank-entry",
        "blank-note",
        "blank-anchor",
        "bad-kind",
        "blank-title",
        "no-note",
    ],
)
def test_a_malformed_body_is_invalid(tmp_path: Path, body: dict[str, Any]) -> None:
    """Schema failures are refused at the boundary, never dialled."""
    vault = seed_vault(tmp_path / "vault")
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        response = _post(test_client, body)

    assert _code(response) == (_INVALID_REQUEST, ErrorCode.INVALID_REQUEST.value)
    assert spy.tiers == []


def test_an_inadmissible_external_id_is_invalid(tmp_path: Path) -> None:
    """The id rules are the Voice Draft resource's, unchanged."""
    vault = seed_vault(tmp_path / "vault")
    with _client(vault) as test_client:
        response = _post(test_client, external_id="x" * 600)

    assert _code(response) == (_INVALID_REQUEST, ErrorCode.INVALID_REQUEST.value)


def test_no_vault_is_unavailable(tmp_path: Path) -> None:
    """An absent vault is the operator-action refusal."""
    with _client(tmp_path / "missing") as test_client:
        response = _post(test_client)

    assert _code(response) == (_UNAVAILABLE, ErrorCode.UNAVAILABLE.value)


def test_a_corrupt_existing_slot_is_an_internal_error(tmp_path: Path) -> None:
    """A slot that is not a valid draft is never overwritten or served."""
    vault = seed_vault(tmp_path / "vault")
    with _client(vault) as test_client:
        assert _post(test_client).status_code == _OK
        _draft_files(vault)[0].write_text("not a fragment", encoding="utf-8")
        response = _post(test_client)

    assert _code(response) == (_INTERNAL_ERROR, ErrorCode.INTERNAL_ERROR.value)


# --------------------------------------------------------------------------- #
# Disclosure: ids and closed vocabulary only
# --------------------------------------------------------------------------- #


def test_logs_and_audit_carry_no_prose(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the entry, the note, the prompt nor the letter is recorded."""
    vault = seed_vault(tmp_path / "vault")
    caplog.set_level(logging.DEBUG)
    with _client(vault) as test_client:
        assert _post(test_client).status_code == _OK
        assert _post(test_client, ceiling="open").status_code == _PRIVACY_REFUSED

    audit = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in audit.splitlines() if line.strip()]
    letter_rows = [row for row in rows if row.get("tool") == "creek.letter.write"]
    assert len(letter_rows) == 2
    recorded = audit + "\n".join(record.getMessage() for record in caplog.records)
    for prose in ("zz-entry-1862-zz", "zz-note-1862-zz", "zz-letter-1862-zz", _ANCHOR):
        assert prose not in recorded
    assert any(
        getattr(record, "letter_outcome", None) == "above_ceiling"
        for record in caplog.records
    )


def test_the_prompt_builder_delimits_every_caller_field() -> None:
    """Caller text sits inside named delimiters, after the instructions."""
    prompt = build_letter_prompt(
        LetterSource(
            content=_ENTRY,
            note=_NOTE,
            anchor_text=_ANCHOR,
            kind="pattern",
            tier=PrivacyTier.PERSONAL,
            title=None,
        )
    )

    assert (
        prompt.index("<passage>") < prompt.index(_ANCHOR) < prompt.index("</passage>")
    )
    assert prompt.index("<entry>") < prompt.index(_ENTRY) < prompt.index("</entry>")
    assert prompt.index("CARE BOUNDARY") < prompt.index(_NOTE)


# --------------------------------------------------------------------------- #
# Storage outcomes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (VaultLockTimeoutError(), (_UNAVAILABLE, ErrorCode.TEMPORARILY_UNAVAILABLE)),
        (VoiceDraftAccessDeniedError(), (_PRIVACY_REFUSED, ErrorCode.PRIVACY_REFUSED)),
        (VoiceDraftStorageError("x"), (_INTERNAL_ERROR, ErrorCode.INTERNAL_ERROR)),
    ],
    ids=["lock-timeout", "raced-above-ceiling", "corrupt-at-write"],
)
def test_a_store_failure_maps_to_one_closed_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: tuple[int, ErrorCode],
) -> None:
    """Each failure of the first-writer-wins store has one wire answer."""
    vault = seed_vault(tmp_path / "vault")

    def _raise(**_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("creek_mcp.tools.letter.create_voice_draft_if_absent", _raise)
    with _client(vault) as test_client:
        response = _post(test_client)

    assert _code(response) == (expected[0], expected[1].value)
    assert _draft_files(vault) == []


def test_create_if_absent_keeps_the_first_letter(tmp_path: Path) -> None:
    """A second writer for the same id is answered with the first document."""
    vault = seed_vault(tmp_path / "vault")
    first = create_voice_draft_if_absent(
        vault_path=vault,
        external_id=_EXTERNAL_ID,
        document=VoiceDraftDocument(content="first", title=None, tier=PrivacyTier.OPEN),
        existing_is_admitted=lambda _tier: True,
    )
    second = create_voice_draft_if_absent(
        vault_path=vault,
        external_id=_EXTERNAL_ID,
        document=VoiceDraftDocument(content="second", title="T", tier=PrivacyTier.OPEN),
        existing_is_admitted=lambda _tier: True,
    )

    assert first.action is VoiceDraftAction.CREATED
    assert second.action is VoiceDraftAction.UNCHANGED
    assert second.record.content == "first"
    assert second.record.title == "Voice draft"


def test_create_if_absent_refuses_an_unadmitted_existing_draft(tmp_path: Path) -> None:
    """The existing tier is admitted under the lock, as on upsert."""
    vault = seed_vault(tmp_path / "vault")
    document = VoiceDraftDocument(content="kept", title=None, tier=PrivacyTier.PERSONAL)
    create_voice_draft_if_absent(
        vault_path=vault,
        external_id=_EXTERNAL_ID,
        document=document,
        existing_is_admitted=lambda _tier: True,
    )

    with pytest.raises(VoiceDraftAccessDeniedError):
        create_voice_draft_if_absent(
            vault_path=vault,
            external_id=_EXTERNAL_ID,
            document=document,
            existing_is_admitted=lambda _tier: False,
        )


def test_a_stored_tier_the_wire_cannot_name_is_an_internal_error(
    tmp_path: Path,
) -> None:
    """A hand-edited ``unclassified`` draft is never published as a label."""
    vault = seed_vault(tmp_path / "vault")
    create_voice_draft_if_absent(
        vault_path=vault,
        external_id=_EXTERNAL_ID,
        document=VoiceDraftDocument(
            content="kept", title=None, tier=PrivacyTier.UNCLASSIFIED
        ),
        existing_is_admitted=lambda _tier: True,
    )
    spy = _FactorySpy()
    with _client(vault, spy) as test_client:
        response = _post(test_client)

    assert _code(response) == (_INTERNAL_ERROR, ErrorCode.INTERNAL_ERROR.value)
    assert spy.tiers == []


def test_an_unchanged_letter_is_returned_exactly_as_stored(tmp_path: Path) -> None:
    """A long draft put by the Voice Draft route is not truncated on the way out."""
    vault = seed_vault(tmp_path / "vault")
    long_draft = "y" * (MAX_LETTER_CHARS + 10)
    create_voice_draft_if_absent(
        vault_path=vault,
        external_id=_EXTERNAL_ID,
        document=VoiceDraftDocument(
            content=long_draft, title=None, tier=PrivacyTier.PERSONAL
        ),
        existing_is_admitted=lambda _tier: True,
    )
    with _client(vault) as test_client:
        parsed = LetterResponse.model_validate(envelope(_post(test_client)))

    assert parsed.action.value == "unchanged"
    assert parsed.letter == long_draft
