"""Versioned ``/v1`` Voice Draft storage and recall (#1727).

These tests drive the HTTP boundary and inspect the resulting vault bytes.  A
contract-only stub would therefore fail beside an implementation that wrote an
ordinary user fragment: the same journey proves routing, idempotency,
retraction, privacy admission, and the AI-attribution guarantee.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import frontmatter
from pydantic import ValidationError

from creek.generate.voice import _eligible_register
from creek.vault.reader import try_load_fragment
from creek_mcp.api.bundle import build_bundle
from creek_mcp.api.models import (
    CAPABILITY_SINCE_MINOR,
    CONTRACT_MINOR,
    Capability,
    ErrorCode,
    VoiceDraftAttribution,
    VoiceDraftDeleteResponse,
    VoiceDraftReadResponse,
    VoiceDraftUpsertResponse,
)
from creek_mcp.audit import MCP_AUDIT_RELPATH
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    client,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

_OK: Final[int] = 200
_PRIVACY_REFUSED: Final[int] = 403
_INVALID_REQUEST: Final[int] = 422
_INTERNAL_ERROR: Final[int] = 500
_OLD_MINOR: Final[str] = "0.14"
_EXTERNAL_ID: Final[str] = "adepthood-voicedraft-7f4132"
_DRAFT_PATH: Final[str] = f"/v1/voice-drafts/{_EXTERNAL_ID}"
_BODY: Final[str] = "A synthetic expanded reflection about patient attention."


def _request(
    *,
    content: str = _BODY,
    title: str | None = "Patient attention",
    tier: str = "open",
) -> dict[str, Any]:
    """Return one valid Voice Draft upsert body."""
    body: dict[str, Any] = {"content": content, "tier": tier}
    if title is not None:
        body["title"] = title
    return body


def _put(
    vault: Path,
    body: dict[str, Any] | None = None,
    *,
    ceiling: str = "open",
    minor: str = CONTRACT_MINOR,
    external_id: str = _EXTERNAL_ID,
) -> httpx.Response:
    """Upsert a draft through the real Starlette adapter."""
    with client(vault_path=vault) as test_client:
        return test_client.put(
            f"/v1/voice-drafts/{external_id}",
            json=_request() if body is None else body,
            headers=headers(ceiling=ceiling, minor=minor),
        )


def _get(
    vault: Path,
    *,
    ceiling: str = "open",
    minor: str = CONTRACT_MINOR,
    external_id: str = _EXTERNAL_ID,
) -> httpx.Response:
    """Recall a draft through the real Starlette adapter."""
    with client(vault_path=vault) as test_client:
        return test_client.get(
            f"/v1/voice-drafts/{external_id}",
            headers=headers(ceiling=ceiling, minor=minor),
        )


def _delete(
    vault: Path,
    *,
    ceiling: str = "open",
    minor: str = CONTRACT_MINOR,
    external_id: str = _EXTERNAL_ID,
) -> httpx.Response:
    """Retract a draft through the real Starlette adapter."""
    with client(vault_path=vault) as test_client:
        return test_client.delete(
            f"/v1/voice-drafts/{external_id}",
            headers=headers(ceiling=ceiling, minor=minor),
        )


def _draft_files(vault: Path) -> list[Path]:
    """Return the deterministic Voice Draft note files, excluding lock files."""
    return sorted((vault / "11-Other-Authors" / "ai-as-user").glob("*.md"))


def _error_shape(response: httpx.Response) -> tuple[int, str, str]:
    """Return the non-correlating portion of an error envelope."""
    body = envelope(response)
    return response.status_code, str(body["code"]), str(body["message"])


def test_current_clients_negotiate_the_voice_drafts_capability(tmp_path: Path) -> None:
    """The capability is advertised only from the minor that publishes it."""
    vault = seed_vault(tmp_path / "vault")
    with client(vault_path=vault) as test_client:
        current = envelope(
            test_client.get(CAPABILITIES_PATH, headers=headers(ceiling="open"))
        )
        old = envelope(
            test_client.get(
                CAPABILITIES_PATH,
                headers=headers(ceiling="open", minor=_OLD_MINOR),
            )
        )

    assert Capability.VOICE_DRAFTS.value in current["capabilities"]
    assert Capability.VOICE_DRAFTS.value not in old["capabilities"]
    assert CAPABILITY_SINCE_MINOR[Capability.VOICE_DRAFTS] == CONTRACT_MINOR


def test_upsert_files_an_ai_attributed_voice_neutral_fragment(tmp_path: Path) -> None:
    """A Voice Draft is durable AI material and never owner-voice evidence."""
    vault = seed_vault(tmp_path / "vault")

    response = _put(vault)

    assert response.status_code == _OK
    parsed = VoiceDraftUpsertResponse.model_validate(envelope(response))
    assert parsed.external_id == _EXTERNAL_ID
    assert parsed.action.value == "created"
    assert parsed.attribution == VoiceDraftAttribution()
    files = _draft_files(vault)
    assert len(files) == 1
    assert _EXTERNAL_ID not in files[0].name
    assert "Patient-attention" not in files[0].name
    post = frontmatter.load(str(files[0]))
    assert post["voice_draft"]["external_id"] == _EXTERNAL_ID
    assert post["source"]["platform"] == "other"
    assert post["source"]["author"] == "ai"
    assert post["source"]["author_slug"] == "ai-as-user"
    assert post["voice_weight"] == 0.0
    assert post.content == _BODY

    loaded = try_load_fragment(files[0])
    assert loaded is not None
    fragment, _body, _raw = loaded
    assert fragment.id == parsed.fragment_id
    assert _eligible_register(fragment, allow_intimate=True) is None


def test_same_external_id_is_unchanged_then_updates_one_document(
    tmp_path: Path,
) -> None:
    """Caller identity, not title or content, owns the one durable document."""
    vault = seed_vault(tmp_path / "vault")
    first = VoiceDraftUpsertResponse.model_validate(envelope(_put(vault)))

    unchanged = VoiceDraftUpsertResponse.model_validate(envelope(_put(vault)))
    updated = VoiceDraftUpsertResponse.model_validate(
        envelope(
            _put(
                vault,
                _request(content="Revised synthetic draft.", title="Revised"),
            )
        )
    )
    recalled = VoiceDraftReadResponse.model_validate(envelope(_get(vault)))

    assert unchanged.action.value == "unchanged"
    assert updated.action.value == "updated"
    assert first.fragment_id == unchanged.fragment_id == updated.fragment_id
    assert recalled.fragment_id == first.fragment_id
    assert recalled.content == "Revised synthetic draft."
    assert recalled.title == "Revised"
    assert recalled.attribution == VoiceDraftAttribution()
    assert len(_draft_files(vault)) == 1


def test_two_external_ids_remain_distinct(tmp_path: Path) -> None:
    """The idempotency test is not green because all writes share one file."""
    vault = seed_vault(tmp_path / "vault")

    first = VoiceDraftUpsertResponse.model_validate(envelope(_put(vault)))
    second = VoiceDraftUpsertResponse.model_validate(
        envelope(_put(vault, external_id="adepthood-voicedraft-other"))
    )

    assert first.fragment_id != second.fragment_id
    assert len(_draft_files(vault)) == 2


def test_omitted_title_uses_a_content_free_placeholder(tmp_path: Path) -> None:
    """No title is synthesized from model prose or the caller-owned id."""
    vault = seed_vault(tmp_path / "vault")

    assert _put(vault, _request(title=None)).status_code == _OK

    recalled = VoiceDraftReadResponse.model_validate(envelope(_get(vault)))
    assert recalled.title == "Voice draft"


def test_personal_draft_requires_personal_ceiling_for_write_and_read(
    tmp_path: Path,
) -> None:
    """Declared and existing tiers are both admitted before bytes cross."""
    vault = seed_vault(tmp_path / "vault")

    refused_write = _put(vault, _request(tier="personal"))
    assert _error_shape(refused_write)[0:2] == (
        _PRIVACY_REFUSED,
        ErrorCode.PRIVACY_REFUSED.value,
    )
    assert _draft_files(vault) == []

    assert _put(vault, _request(tier="personal"), ceiling="personal").status_code == _OK
    hidden = _get(vault, ceiling="open")
    missing = _get(vault, ceiling="open", external_id="never-written")
    assert _error_shape(hidden) == _error_shape(missing)
    recalled = VoiceDraftReadResponse.model_validate(
        envelope(_get(vault, ceiling="personal"))
    )
    assert recalled.content == _BODY
    assert recalled.tier.value == "personal"


def test_open_caller_cannot_overwrite_an_existing_personal_draft(
    tmp_path: Path,
) -> None:
    """An incoming open tier cannot bypass the read gate on the old bytes."""
    vault = seed_vault(tmp_path / "vault")
    assert _put(vault, _request(tier="personal"), ceiling="personal").status_code == _OK

    refused = _put(vault, _request(content="replacement", tier="open"))

    assert _error_shape(refused)[0:2] == (
        _PRIVACY_REFUSED,
        ErrorCode.PRIVACY_REFUSED.value,
    )
    recalled = VoiceDraftReadResponse.model_validate(
        envelope(_get(vault, ceiling="personal"))
    )
    assert recalled.content == _BODY
    assert recalled.tier.value == "personal"


def test_delete_retracts_only_the_addressed_draft_after_read_admission(
    tmp_path: Path,
) -> None:
    """A later INTIMATE reclassification can retract a previously mirrored draft."""
    vault = seed_vault(tmp_path / "vault")
    assert _put(vault, _request(tier="personal"), ceiling="personal").status_code == _OK
    assert _put(vault, external_id="adepthood-voicedraft-other").status_code == _OK

    refused = _delete(vault, ceiling="open")
    assert refused.status_code == _PRIVACY_REFUSED
    assert len(_draft_files(vault)) == 2

    deleted = VoiceDraftDeleteResponse.model_validate(
        envelope(_delete(vault, ceiling="personal"))
    )
    assert deleted.external_id == _EXTERNAL_ID
    assert deleted.action == "deleted"
    assert _get(vault, ceiling="personal").status_code == _PRIVACY_REFUSED
    assert _error_shape(_delete(vault, ceiling="personal")) == _error_shape(
        _get(vault, ceiling="personal")
    )
    assert _get(vault, external_id="adepthood-voicedraft-other").status_code == _OK
    assert len(_draft_files(vault)) == 1


def test_malformed_requests_write_nothing(tmp_path: Path) -> None:
    """Blank content and unusable path ids fail before vault mutation."""
    vault = seed_vault(tmp_path / "vault")

    blank = _put(vault, _request(content="   "))
    bad_id_responses = (
        _put(vault, external_id="%20"),
        _get(vault, external_id="%20"),
        _delete(vault, external_id="%20"),
    )

    assert blank.status_code == _INVALID_REQUEST
    assert {response.status_code for response in bad_id_responses} == {_INVALID_REQUEST}
    assert _draft_files(vault) == []


def test_voice_draft_audit_never_records_prose_title_or_external_id(
    tmp_path: Path,
) -> None:
    """Audit attempts retain useful shape metadata without protected strings."""
    vault = seed_vault(tmp_path / "vault")
    protected_title = "private synthetic title"
    protected_body = "private synthetic body that must not enter audit bytes"

    assert (
        _put(
            vault,
            _request(content=protected_body, title=protected_title),
        ).status_code
        == _OK
    )
    assert _get(vault).status_code == _OK
    assert _delete(vault).status_code == _OK

    audit_text = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entries = [json.loads(line) for line in audit_text.splitlines()]
    assert protected_body not in audit_text
    assert protected_title not in audit_text
    assert _EXTERNAL_ID not in audit_text
    assert [entry["tool"] for entry in entries] == [
        "creek.voice-draft.upsert",
        "creek.voice-draft.read",
        "creek.voice-draft.delete",
    ]
    assert entries[0]["args_summary"] == {
        "body_len": len(protected_body),
        "has_external_id": True,
        "has_title": True,
        "tier": "open",
    }


def test_every_verb_refuses_a_redirected_ai_author_namespace(
    tmp_path: Path,
) -> None:
    """A parent symlink cannot move draft bytes outside their fixed subtree."""
    vault = seed_vault(tmp_path / "vault")
    assert _put(vault).status_code == _OK
    namespace = vault / "11-Other-Authors"
    redirected = tmp_path / "redirected-authors"
    namespace.rename(redirected)
    namespace.symlink_to(redirected, target_is_directory=True)
    stored = next((redirected / "ai-as-user").glob("*.md"))
    before = stored.read_bytes()

    responses = (
        _get(vault),
        _put(vault, _request(content="must not escape")),
        _delete(vault),
    )

    assert {
        (response.status_code, envelope(response)["code"]) for response in responses
    } == {(_INTERNAL_ERROR, ErrorCode.INTERNAL_ERROR.value)}
    assert stored.read_bytes() == before


def test_old_minor_is_refused_every_voice_draft_route(tmp_path: Path) -> None:
    """A client that cannot negotiate the models cannot guess the endpoints."""
    vault = seed_vault(tmp_path / "vault")

    responses = (
        _put(vault, minor=_OLD_MINOR),
        _get(vault, minor=_OLD_MINOR),
        _delete(vault, minor=_OLD_MINOR),
    )

    assert {response.status_code for response in responses} == {409}
    assert {envelope(response)["code"] for response in responses} == {
        ErrorCode.INCOMPATIBLE_VERSION.value
    }
    assert _draft_files(vault) == []


def test_bundle_publishes_all_voice_draft_models_without_example_prose() -> None:
    """The vendored artifact carries schemas and content-free worked responses."""
    bundle = build_bundle()
    required = {
        "VoiceDraftAttribution",
        "VoiceDraftDeleteResponse",
        "VoiceDraftReadResponse",
        "VoiceDraftUpsertRequest",
        "VoiceDraftUpsertResponse",
    }

    assert {f"schemas/{name}.schema.json" for name in required} <= set(bundle)
    assert "examples/voice-drafts/success.json" in bundle
    assert "examples/voice-drafts/empty.json" in bundle
    assert _BODY not in "".join(bundle.values())


def test_attribution_schema_cannot_describe_owner_authorship() -> None:
    """The AI attribution promise is fixed vocabulary, not advisory prose."""
    try:
        VoiceDraftAttribution.model_validate(
            {"author": "self", "author_slug": "ai-as-user", "voice_weight": 0.0}
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("owner authorship must be inexpressible for a Voice Draft")


def test_attribution_schema_cannot_describe_nonzero_voice_weight() -> None:
    """The zero voice weight is a closed value, not a producer convention."""
    try:
        VoiceDraftAttribution.model_validate(
            {"author": "ai", "author_slug": "ai-as-user", "voice_weight": 0.1}
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("nonzero voice weight must be inexpressible")
