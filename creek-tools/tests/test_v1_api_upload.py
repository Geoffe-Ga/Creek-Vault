"""``POST /v1/uploads`` — the fifth capability, and its gates (#1524).

``creek.upload`` has existed since contract 0.3 and was reachable only over
MCP, so no HTTP client could seed a vault with a document. This module is the
contract for the route that changes that, and it is deliberately written
against the *route* rather than the tool: the tool's own gates are covered by
``tests/test_mcp_upload.py``, and a route that reimplemented any of them would
pass those tests while diverging from them.

Four properties carry the weight, and each one is a place a plausible
implementation would have gone wrong.

* **Idempotency is asserted end to end, on the filesystem.** The second request
  must return the *same* ``fragment_id`` and leave the fragment count
  unchanged. Reading the response alone would be satisfied by a handler that
  re-created the fragment and reported the new id, which is the bug.

* **The tier ceiling is asserted on the written bytes, never on the response.**
  ``upload_tool`` reports ``"tier": "open"`` for a document whose own
  frontmatter declares ``intimate``, and it is *right* to: the declared tier is
  a create-time floor and classification is escalate-only. A response field
  claiming the resulting tier would therefore be a lie — that exact defect was
  #1491 — which is why :class:`~creek_mcp.api.models.UploadResponse` publishes
  none, and why the assertion here reads the fragment back off disk and
  compares it against what ``creek ingest`` produces from the same bytes.

* **Version negotiation is two-sided.** A ``0.2``-pinned client is not told
  about ``upload`` *and* cannot reach it. One without the other is the failure:
  advertised-but-refused is a server calling itself a liar, and
  withheld-but-served is an endpoint a consumer integrates against without ever
  having negotiated its response model.

* **The #1526 refusal survives the boundary.** A ``.json`` upload must land as
  ``415`` carrying the published remedy — never a ``500``, never a traceback,
  and never a fragment. Swept over *every* refused extension rather than one,
  with a non-zero collected count as the positive control.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.gdrive import _EXTENSION_ROUTES, _Refuse
from creek.ingest.pipeline import run_ingest
from creek.models import PrivacyTier
from creek_mcp.api.models import (
    CAPABILITY_SINCE_MINOR,
    ERROR_MESSAGES,
    ERROR_STATUS,
    Capability,
    ErrorCode,
    UploadResponse,
    minor_at_least,
)
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.httpapi.upload import upload_refusal_code
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.tier_ceiling import TIER_REQUIRED_REASON
from creek_mcp.tools.upload import MAX_UPLOAD_BYTES
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    UPLOAD_PATH,
    build_app,
    client,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
    snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

_OK_STATUS: Final[int] = 200
_INVALID_REQUEST_STATUS: Final[int] = ERROR_STATUS[ErrorCode.INVALID_REQUEST]
_PRIVACY_REFUSED_STATUS: Final[int] = ERROR_STATUS[ErrorCode.PRIVACY_REFUSED]
_INCOMPATIBLE_STATUS: Final[int] = ERROR_STATUS[ErrorCode.INCOMPATIBLE_VERSION]
_UNSUPPORTED_SOURCE_STATUS: Final[int] = ERROR_STATUS[ErrorCode.UNSUPPORTED_SOURCE]

_OLD_MINOR: Final[str] = "0.2"
"""A minor this server still serves, and that predates ``upload``."""

_DOCUMENT_BODY: Final[bytes] = b"# Ridge notes\n\nThe fog lifted at seven.\n"
"""Synthetic markdown. Never a real journal entry; nothing here is a person."""

_TENDER_BODY: Final[bytes] = (
    b"---\nprivacy_tier: intimate\n---\n\nsynthetic-tender-marker-1524\n"
)
"""A document that classifies itself ``intimate`` in its own frontmatter.

The marker is a nonsense token rather than prose, so "the plaintext did not
leak" and "the tier is intimate" are two assertions that cannot accidentally
satisfy one another.
"""


def _b64(payload: bytes) -> str:
    """Return the base64 of *payload*, derived at runtime and never hardcoded.

    Args:
        payload: The bytes to encode.

    Returns:
        The ASCII base64 string.
    """
    return base64.b64encode(payload).decode("ascii")


def _body(
    *,
    filename: str = "ridge-notes.md",
    content: bytes = _DOCUMENT_BODY,
    external_id: str = "adepthood:doc:1524",
    tier: str = "open",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a valid ``UploadRequest`` body with one field varied at a time.

    Args:
        filename: The caller's filename, whose extension picks the ingestor.
        content: The raw document bytes, encoded here.
        external_id: The idempotency key.
        tier: The declared tier.
        timestamp: Optional ISO-8601 upload time.

    Returns:
        The request body as a dict.
    """
    body: dict[str, Any] = {
        "filename": filename,
        "content_base64": _b64(content),
        "external_id": external_id,
        "tier": tier,
    }
    if timestamp is not None:
        body["timestamp"] = timestamp
    return body


def _post(
    vault: Path,
    body: dict[str, Any],
    *,
    ceiling: str = "open",
    minor: str | None = None,
) -> httpx.Response:
    """POST *body* to the upload route against *vault*.

    Args:
        vault: The vault the app serves.
        body: The request body.
        ceiling: The declared tier ceiling.
        minor: The declared contract minor, or ``None`` for the current one.

    Returns:
        The response.
    """
    kwargs = (
        {"ceiling": ceiling} if minor is None else {"ceiling": ceiling, "minor": minor}
    )
    with client(vault_path=vault) as test_client:
        return test_client.post(UPLOAD_PATH, json=body, headers=headers(**kwargs))


def _fragments(vault: Path) -> list[Path]:
    """Return every fragment file written under ``01-Fragments``.

    Args:
        vault: The vault root.

    Returns:
        Sorted fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _refused_extensions() -> tuple[str, ...]:
    """Return every extension :func:`route_to_ingestor` refuses, sorted.

    Read off :data:`creek.ingest.gdrive._EXTENSION_ROUTES` rather than listed,
    so an extension added to the refusal table is swept by this module without
    anyone remembering to add it. Reaching for the private name is deliberate:
    a public restatement would be a second list, and a second list is what this
    derivation exists to avoid.

    Returns:
        The refused suffixes, in a stable order.
    """
    return tuple(
        sorted(
            suffix
            for suffix, route in _EXTENSION_ROUTES.items()
            if isinstance(route, _Refuse)
        )
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a seeded vault with the ingest scaffold the writer needs.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    root = seed_vault(tmp_path / "vault")
    (root / "01-Fragments").mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# AC1 / AC2 — the route exists, and it is idempotent through the ledger
# --------------------------------------------------------------------------- #


def test_an_upload_returns_the_created_fragments_identity(vault: Path) -> None:
    """The route answers ``200`` with the published response and a real id.

    Before #1524 this exact request answered ``404 not_found`` — there was no
    such endpoint — which is the RED this module was written against.

    Args:
        vault: A seeded vault.
    """
    response = _post(vault, _body(timestamp="2026-08-18T09:30:00Z"))

    assert response.status_code == _OK_STATUS
    parsed = UploadResponse.model_validate(envelope(response))
    assert parsed.external_id == "adepthood:doc:1524"
    assert parsed.action.value == "created"
    assert parsed.source_type == "markdown"
    assert parsed.affected_fragment_ids == [parsed.fragment_id]
    written = _fragments(vault)
    assert len(written) == 1
    assert parsed.fragment_id in written[0].read_text(encoding="utf-8")


def test_the_response_carries_no_field_beyond_the_published_model(
    vault: Path,
) -> None:
    """The key set is a subset of ``UploadResponse``'s, and misses nothing required.

    ``extra="forbid"`` catches an added key on parse; comparing key sets catches
    a *missing* one too. ``warnings`` is legitimately absent on a quiet upload,
    which is the one optional field.

    Args:
        vault: A seeded vault.
    """
    body = envelope(_post(vault, _body()))
    published = set(UploadResponse.model_fields)
    assert set(body) <= published
    assert published - set(body) == {"warnings"}


def test_the_response_publishes_no_tier_field(vault: Path) -> None:
    """No ``tier`` on the wire — the #1491 defect made structurally impossible.

    The tool *does* return one, and it names the caller's declared tier rather
    than the tier the fragment landed at. Forwarding it would be a response
    claiming a tier the bytes do not carry; resolving it would be a tier
    oracle. The model has no such field, so neither is expressible.

    Args:
        vault: A seeded vault.
    """
    assert "tier" not in UploadResponse.model_fields
    assert "tier" not in envelope(_post(vault, _body()))


def test_resending_the_same_external_id_creates_nothing(vault: Path) -> None:
    """The idempotency claim, asserted on the filesystem and not on the response.

    A handler that re-ingested and reported the *new* fragment's id would
    satisfy any response-only assertion. So this compares the ids across the
    two calls **and** counts the fragments on disk: the second call must be
    ``unchanged``, name the same fragment, and add nothing.

    Args:
        vault: A seeded vault.
    """
    first = envelope(_post(vault, _body()))
    after_first = _fragments(vault)

    second = envelope(_post(vault, _body()))

    assert second["fragment_id"] == first["fragment_id"]
    assert second["action"] == "unchanged"
    assert _fragments(vault) == after_first
    assert len(after_first) == 1


def test_a_second_external_id_creates_a_second_fragment(vault: Path) -> None:
    """The idempotency test above is not passing because writes never happen.

    Without this control, a route that silently refused every write after the
    first would satisfy it.

    Args:
        vault: A seeded vault.
    """
    first = envelope(_post(vault, _body(external_id="adepthood:doc:a")))
    second = envelope(
        _post(
            vault, _body(external_id="adepthood:doc:b", content=b"# Other\n\nText.\n")
        )
    )

    assert first["fragment_id"] != second["fragment_id"]
    assert len(_fragments(vault)) == 2


# --------------------------------------------------------------------------- #
# AC3 — version negotiation, both halves
# --------------------------------------------------------------------------- #


def _advertised(vault: Path, minor: str | None) -> list[str]:
    """Return the capability list a caller at *minor* is handed.

    Args:
        vault: A seeded vault.
        minor: The declared contract minor, or ``None`` to declare none.

    Returns:
        The advertised capability names.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(
            CAPABILITIES_PATH, headers=headers(minor=minor, ceiling="open")
        )
    capabilities: list[str] = envelope(response)["capabilities"]
    return capabilities


def test_capabilities_advertises_upload_to_a_current_client(vault: Path) -> None:
    """A client on the current minor is told the capability exists.

    Args:
        vault: A seeded vault.
    """
    assert Capability.UPLOAD.value in _advertised(vault, None)


def test_a_zero_two_client_is_not_told_about_upload(vault: Path) -> None:
    """A ``0.2``-pinned client sees the four founding capabilities and no fifth.

    This is the point of the whole ``CAPABILITY_SINCE_MINOR`` table. ``0.2`` is
    still served — the compatibility window only ever widens — so the client
    gets a ``200`` and a complete, correct answer *for the contract it
    vendored*, which does not describe ``upload``.

    Args:
        vault: A seeded vault.
    """
    assert _advertised(vault, _OLD_MINOR) == [
        "capabilities",
        "journal-upsert",
        "reflections",
        "wheel",
    ]


def test_a_zero_two_client_is_refused_the_route_it_was_not_told_about(
    vault: Path,
) -> None:
    """Withheld on the handshake, refused on the route — off the same table.

    The half that matters. Advertising is only honest if the omission is
    enforced: a client that guessed the path must not be quietly served a
    response model, an error code and a status its contract has no vocabulary
    for. And nothing may be written on the way to that refusal.

    Args:
        vault: A seeded vault.
    """
    before = snapshot(vault)

    response = _post(vault, _body(), minor=_OLD_MINOR)

    assert response.status_code == _INCOMPATIBLE_STATUS
    assert envelope(response)["code"] == ErrorCode.INCOMPATIBLE_VERSION.value
    assert _fragments(vault) == []
    assert snapshot(vault) == before


def test_the_capabilities_endpoint_still_answers_200_for_an_unservable_minor(
    vault: Path,
) -> None:
    """The since-minor gate must not have made the handshake refusable.

    ``GET /v1/capabilities`` reports incompatibility in its *body* and never in
    its status line, because a client that cannot read a version off a server
    has no way to learn what is wrong with it. The gate added by #1524 sits
    behind ``requires_contract_version``, which this route does not set —
    asserted here rather than left to inspection.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(
            CAPABILITIES_PATH, headers=headers(minor="0.1", ceiling="open")
        )
    assert response.status_code == _OK_STATUS
    assert envelope(response)["status"] == "incompatible"


def test_capability_since_minor_is_total_and_names_served_minors() -> None:
    """Every capability has an introduction minor, and it is one still served.

    Totality is what keeps the lookup from ``KeyError``-ing mid-request; the
    membership check is what stops a capability being introduced at a minor no
    client can declare, which would make it unreachable to everyone.

    Uses ``minor_at_least`` on itself as the positive control: a capability is
    always available to a client on the current minor.
    """
    from creek_mcp.api.models import CONTRACT_MINOR, SUPPORTED_CONTRACT_MINORS

    assert set(CAPABILITY_SINCE_MINOR) == set(Capability)
    for capability, since in CAPABILITY_SINCE_MINOR.items():
        assert since in SUPPORTED_CONTRACT_MINORS, capability
        assert minor_at_least(CONTRACT_MINOR, since), capability


@pytest.mark.parametrize(
    ("declared", "required", "expected"),
    [
        ("0.8", "0.8", True),
        ("0.9", "0.8", True),
        ("0.7", "0.8", False),
        ("0.2", "0.2", True),
        # The one a string compare gets wrong, and the reason this helper
        # parses integers: "0.10" sorts *below* "0.8" lexicographically.
        ("0.10", "0.8", True),
        ("not-a-version", "0.8", False),
    ],
)
def test_minor_at_least_compares_componentwise(
    declared: str, required: str, expected: bool
) -> None:
    """Minors are compared as integers, and anything unparseable fails closed.

    Args:
        declared: The caller's minor.
        required: The minor being demanded.
        expected: Whether *declared* satisfies *required*.
    """
    assert minor_at_least(declared, required) is expected


# --------------------------------------------------------------------------- #
# AC4 — the tier ceiling, asserted on the written bytes
# --------------------------------------------------------------------------- #


def test_a_tier_above_the_ceiling_is_refused_before_anything_is_written(
    vault: Path,
) -> None:
    """``tier=personal`` at ``ceiling=open`` refuses, and stages nothing.

    The refusal is ``privacy_refused``, and the filesystem snapshot is what
    proves the bytes never reached the staging directory — the tool's tier gate
    sits above its decode for exactly this reason.

    Args:
        vault: A seeded vault.
    """
    response = _post(vault, _body(tier="personal"), ceiling="open")

    assert response.status_code == _PRIVACY_REFUSED_STATUS
    assert envelope(response)["code"] == ErrorCode.PRIVACY_REFUSED.value
    assert _fragments(vault) == []
    assert not any(
        path.is_file() and _DOCUMENT_BODY in path.read_bytes()
        for path in vault.rglob("*")
    )


def test_the_declared_tier_is_a_floor_and_never_lowers_the_written_fragment(
    vault: Path, tmp_path: Path
) -> None:
    """The seeded fragment carries the tier ``creek ingest`` would give it.

    **This is asserted on the bytes on disk, not on the response.** A document
    whose own frontmatter declares ``intimate`` is uploaded at ``tier=open``,
    the weakest tier a caller can name, and the fragment must still land at
    ``intimate``: classification is escalate-only, so the caller's declared
    tier is a floor rather than a ceiling.

    The comparison is against a real ``run_ingest`` over the same bytes — the
    path ``creek ingest`` takes — rather than against a literal, so "never
    weaker than ``creek ingest``" is measured rather than asserted from memory.

    Args:
        vault: A seeded vault.
        tmp_path: pytest's per-test directory, for the comparison vault.
    """
    response = _post(vault, _body(filename="tender.md", content=_TENDER_BODY))
    assert response.status_code == _OK_STATUS

    uploaded = _fragments(vault)
    assert len(uploaded) == 1
    uploaded_tier = frontmatter.load(uploaded[0]).metadata["privacy_tier"]

    reference_vault = seed_vault(tmp_path / "reference")
    (reference_vault / "01-Fragments").mkdir(parents=True, exist_ok=True)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "tender.md").write_bytes(_TENDER_BODY)
    run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=source_dir,
        vault_path=reference_vault,
        privacy_tier=PrivacyTier.OPEN,
    )
    reference = _fragments(reference_vault)
    assert len(reference) == 1
    reference_tier = frontmatter.load(reference[0]).metadata["privacy_tier"]

    assert uploaded_tier == reference_tier == PrivacyTier.INTIMATE.value


def test_the_success_response_never_names_the_resolved_tier(vault: Path) -> None:
    """The ``intimate`` outcome above is not disclosed to an ``open`` caller.

    The companion to the assertion above, and the reason the response has no
    tier field: the caller declared ``open``, was served at ``ceiling=open``,
    and must learn nothing about the fact that the fragment resolved higher.

    Args:
        vault: A seeded vault.
    """
    body = envelope(_post(vault, _body(filename="tender.md", content=_TENDER_BODY)))

    assert PrivacyTier.INTIMATE.value not in json.dumps(body)
    assert body["tier_ceiling"] == "open"


# --------------------------------------------------------------------------- #
# The #1526 unsupported-source refusal, across the boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", _refused_extensions())
def test_every_refused_extension_lands_as_415_with_the_remedy(
    vault: Path, suffix: str
) -> None:
    """A format ``route_to_ingestor`` refuses is a ``415``, never a ``500``.

    The whole family, not one representative: ``.json`` is the one everybody
    thinks of, and the archive and legacy-Office families take a different
    guidance string each, so a mapping that recognised only the first would
    still pass a single-case test while turning the other two into server
    faults.

    The body carries the published remedy — a refusal that does not say what to
    do instead is one the caller retries verbatim — and no fragment is written.

    Args:
        vault: A seeded vault.
        suffix: One refused extension, swept off the routing table.
    """
    response = _post(
        vault,
        _body(filename=f"export{suffix}", content=b'{"conversations": []}'),
    )

    assert response.status_code == _UNSUPPORTED_SOURCE_STATUS
    body = envelope(response)
    assert body["code"] == ErrorCode.UNSUPPORTED_SOURCE.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.UNSUPPORTED_SOURCE]
    assert "creek ingest" in body["message"]
    assert _fragments(vault) == []


def test_the_refused_extension_sweep_is_not_vacuous() -> None:
    """The sweep above really does cover all three refused families.

    An emptied parametrize list is a test that vanishes behind a green gate, so
    the count is asserted and the three family representatives are named.
    """
    refused = _refused_extensions()
    assert len(refused) >= 3
    assert {".json", ".zip", ".doc"} <= set(refused)


def test_an_unsupported_upload_leaves_no_staged_bytes(vault: Path) -> None:
    """The format gate sits above the staging write, and stays there.

    A refusal that had already written the caller's bytes would be a correct
    answer over a document nobody can purge, because no fragment references it.

    Args:
        vault: A seeded vault.
    """
    marker = b"synthetic-export-marker-1524"

    _post(vault, _body(filename="conversations.json", content=marker))

    assert not any(
        path.is_file() and marker in path.read_bytes() for path in vault.rglob("*")
    )


def test_a_routed_extension_still_ingests(vault: Path) -> None:
    """The control for the sweep: a ``.txt`` is not refused.

    Without it, a route that answered ``415`` to everything would satisfy every
    assertion above.

    Args:
        vault: A seeded vault.
    """
    response = _post(vault, _body(filename="notes.txt", content=b"Plain prose.\n"))

    assert response.status_code == _OK_STATUS
    assert envelope(response)["source_type"] == "document"


# --------------------------------------------------------------------------- #
# Malformed requests, and what a refusal may say
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("filename", ""),
        ("filename", "   "),
        ("content_base64", ""),
        ("external_id", ""),
        ("external_id", "  \t "),
        ("tier", "intimate"),
        ("tier", "nonsense"),
    ],
    ids=[
        "blank-filename",
        "space-filename",
        "blank-payload",
        "blank-id",
        "space-id",
        "intimate-tier",
        "unknown-tier",
    ],
)
def test_a_malformed_body_is_refused_without_touching_the_vault(
    vault: Path, field: str, value: str
) -> None:
    """Schema violations refuse at the model, before anything is resolved.

    ``tier: "intimate"`` is in the list because it is the one a caller would
    actually try: :class:`~creek_mcp.api.models.WireTierCeiling` has two members
    and ``intimate`` is not one of them, so a remote caller cannot even *ask*
    for an intimate write — the refusal is the type system's, not a runtime
    check somebody could forget.

    Args:
        vault: A seeded vault.
        field: The field to corrupt.
        value: The value to corrupt it with.
    """
    before = snapshot(vault)
    body = _body()
    body[field] = value

    response = _post(vault, body)

    assert response.status_code == _INVALID_REQUEST_STATUS
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert snapshot(vault) == before


def test_a_missing_required_field_is_refused(vault: Path) -> None:
    """``tier`` has no default, so a body omitting it never reaches the tool.

    #1494 removed that default from ``creek.upload`` because only the caller
    knows what a document holds; the wire model never had one. Pinned so a
    later "convenience" default is a red test rather than content filed in the
    clear.

    Args:
        vault: A seeded vault.
    """
    body = _body()
    del body["tier"]

    response = _post(vault, body)

    assert response.status_code == _INVALID_REQUEST_STATUS


def test_an_unaddressable_external_id_is_refused(vault: Path) -> None:
    """An over-long id would mint a fragment the client can never address again.

    The rule is imported from the journal route rather than restated, so the
    two write surfaces cannot disagree about what an idempotency key is.

    Args:
        vault: A seeded vault.
    """
    before = snapshot(vault)

    response = _post(vault, _body(external_id="x" * 513))

    assert response.status_code == _INVALID_REQUEST_STATUS
    assert snapshot(vault) == before


def test_a_control_character_in_the_id_is_refused(vault: Path) -> None:
    """A control byte would reach both the staged frontmatter and the audit trail.

    Within the length bound, so this exercises the printability rule rather
    than the length one.

    Args:
        vault: A seeded vault.
    """
    response = _post(vault, _body(external_id="adepthood:doc\x00:1524"))

    assert response.status_code == _INVALID_REQUEST_STATUS


def test_an_undecodable_payload_is_refused(vault: Path) -> None:
    """Not-base64 is the caller's error, and it is not a server fault.

    Args:
        vault: A seeded vault.
    """
    body = _body()
    body["content_base64"] = "!!!! not base64 !!!!"

    response = _post(vault, body)

    assert response.status_code == _INVALID_REQUEST_STATUS


def test_resending_an_id_under_a_new_extension_is_refused(vault: Path) -> None:
    """One ``external_id`` maps to one fragment, and forking it is a caller error.

    A caller error rather than a server fault, so it must not fall through to
    ``internal_error``: the remedy — purge the fragment, or use a new id — is
    entirely in the caller's hands.

    Args:
        vault: A seeded vault.
    """
    assert _post(vault, _body()).status_code == _OK_STATUS

    response = _post(vault, _body(filename="ridge-notes.txt"))

    assert response.status_code == _INVALID_REQUEST_STATUS
    assert len(_fragments(vault)) == 1


def test_no_refusal_body_carries_a_path_or_the_document(vault: Path) -> None:
    """Every refusal is a constant envelope: no staged path, no document bytes.

    ``ingest failed: …`` can name a staged file path in the tool's own
    vocabulary, and ``error_response`` renders one constant per code precisely
    so that nothing of it reaches the wire. Swept across the refusal shapes this
    route can actually produce.

    Args:
        vault: A seeded vault.
    """
    marker = b"synthetic-body-marker-1524"
    probes = [
        _body(filename="conversations.json", content=marker),
        _body(tier="personal", content=marker),
        _body(external_id="x" * 513, content=marker),
    ]
    for probe in probes:
        response = _post(vault, probe)
        text = response.text
        assert not contains_a_path(text), text
        assert _b64(marker) not in text
        assert marker.decode("ascii") not in text
        assert set(envelope(response)) == {"code", "message", "request_id"}


# --------------------------------------------------------------------------- #
# The refusal-code mapping, including the reasons the route cannot reach
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # Defence in depth: the wire model makes both unreachable today —
        # `tier` is required and typed — so they are pinned directly. The day a
        # new caller path or a relaxed field lets one through, it is published
        # as the caller error it is rather than as a server fault.
        (TIER_REQUIRED_REASON, ErrorCode.INVALID_REQUEST),
        ("unknown tier 'sideways'", ErrorCode.INVALID_REQUEST),
        # The #970 overwrite gate's refusal, which names neither the protected
        # fragment nor its tier.
        (GENERIC_ABOVE_CEILING_REASON, ErrorCode.PRIVACY_REFUSED),
        ("vault unavailable", ErrorCode.TEMPORARILY_UNAVAILABLE),
        (
            f"encoded upload exceeds the {MAX_UPLOAD_BYTES}-byte cap",
            ErrorCode.INVALID_REQUEST,
        ),
        (
            f"decoded upload exceeds the {MAX_UPLOAD_BYTES}-byte cap",
            ErrorCode.INVALID_REQUEST,
        ),
        # The two that must fail closed. `ingest failed:` can carry a staged
        # path; an unrecognised reason is one this adapter must not narrate.
        ("ingest failed: /some/staged/path.md exploded", ErrorCode.INTERNAL_ERROR),
        (
            "markdown ingest produced no fragment from this file",
            ErrorCode.INTERNAL_ERROR,
        ),
        ("a reason nobody has written yet", ErrorCode.INTERNAL_ERROR),
    ],
)
def test_upload_refusal_code_is_total(reason: str, expected: ErrorCode) -> None:
    """Each tool refusal maps to its published code, and the default is closed.

    Args:
        reason: The tool's structured refusal reason.
        expected: The wire code it must become.
    """
    assert upload_refusal_code(reason) is expected


def test_an_absent_vault_is_a_refusal_and_not_a_crash(tmp_path: Path) -> None:
    """A vault that is not there is reported, never rebuilt on the way past.

    Args:
        tmp_path: pytest's per-test directory.
    """
    missing = tmp_path / "not-a-vault"

    response = _post(missing, _body())

    assert response.status_code == ERROR_STATUS[ErrorCode.TEMPORARILY_UNAVAILABLE]
    assert not missing.exists()


# --------------------------------------------------------------------------- #
# The per-route body cap
# --------------------------------------------------------------------------- #


def test_the_upload_route_accepts_a_body_the_global_cap_would_refuse(
    vault: Path,
) -> None:
    """A document larger than the process-wide mebibyte still gets through.

    This is the reason the per-route cap exists at all: with the global default
    governing, an ordinary PDF would come back ``422 invalid_request`` — a
    refusal about the *request* for a request that is entirely well formed.

    Args:
        vault: A seeded vault.
    """
    from creek_mcp.httpapi.middleware.limits import DEFAULT_MAX_BODY_BYTES

    oversized = b"# Big\n\n" + b"prose prose prose\n" * 80_000
    assert len(_b64(oversized)) > DEFAULT_MAX_BODY_BYTES

    response = _post(vault, _body(filename="big.md", content=oversized))

    assert response.status_code == _OK_STATUS
    assert len(_fragments(vault)) == 1


def test_a_body_above_the_upload_cap_is_still_refused(vault: Path) -> None:
    """The per-route cap raises the limit; it does not remove it.

    Refused by the middleware, above the router, so nothing is buffered past
    the cap and no handler runs.

    Args:
        vault: A seeded vault.
    """
    from creek_mcp.api.routes import UPLOAD_MAX_BODY_BYTES

    with client(vault_path=vault) as test_client:
        response = test_client.post(
            UPLOAD_PATH,
            content=b"x" * (UPLOAD_MAX_BODY_BYTES + 1),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )

    assert response.status_code == _INVALID_REQUEST_STATUS
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert _fragments(vault) == []


def test_the_other_routes_keep_the_global_cap(vault: Path) -> None:
    """Raising the upload cap raised nothing else.

    The whole argument for a per-route cap rather than a bigger global one is
    that no other route should be allowed to make the server buffer thirteen
    megabytes. Asserted on the reflection route, which takes a body and is not
    in the override map.

    Args:
        vault: A seeded vault.
    """
    from tests.v1_api_support import REFLECTIONS_PATH

    small = 256
    with client(vault_path=vault, max_body_bytes=small) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH,
            content=b"x" * (small + 1),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )

    assert response.status_code == _INVALID_REQUEST_STATUS


def test_lowering_the_global_cap_does_not_lower_the_upload_route(
    vault: Path,
) -> None:
    """The route's declared cap wins on its own path, and that is documented.

    Stated as a test rather than left for a reader to infer from the
    middleware: an operator who hardens ``max_body_bytes`` is not thereby
    hardening ``POST /v1/uploads``, whose limit is published contract.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, max_body_bytes=8) as test_client:
        response = test_client.post(
            UPLOAD_PATH, json=_body(), headers=headers(ceiling="open")
        )

    assert response.status_code == _OK_STATUS


# --------------------------------------------------------------------------- #
# The audit trail
# --------------------------------------------------------------------------- #


def test_a_successful_upload_is_audited_without_the_document(vault: Path) -> None:
    """The tool's audit append happens, and the bytes are not in it.

    The route adds no audit call of its own — it delegates — so this is a check
    that the delegation reaches the real tool rather than a reimplementation
    that skipped the trail.

    Args:
        vault: A seeded vault.
    """
    marker = b"synthetic-audit-marker-1524"

    assert _post(vault, _body(content=marker + b"\n")).status_code == _OK_STATUS

    log = vault / MCP_AUDIT_RELPATH
    text = log.read_text(encoding="utf-8")
    entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert [entry for entry in entries if entry["tool"] == "creek.upload"]
    assert _b64(marker + b"\n") not in text
    assert marker.decode("ascii") not in text


# --------------------------------------------------------------------------- #
# The two fail-closed guards on an otherwise successful ingest
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"fragment_id": None}, "ledger-and-writer-disagree"),
        ({"action": "invented"}, "action-outside-the-wire-enum"),
        ({"tier_ceiling": "intimate"}, "ceiling-the-wire-cannot-name"),
        ({"affected_fragment_ids": None}, "unwalkable-id-list"),
    ],
    ids=[
        "null-fragment-id",
        "unknown-action",
        "unnameable-ceiling",
        "unwalkable-ids",
    ],
)
def test_a_success_the_contract_cannot_express_is_a_server_fault(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    """An ``ok`` the wire model cannot render is ``500``, never an invented body.

    The sharpest case is the null ``fragment_id``: ``str(None)`` raises
    nothing, so a projection that skipped this guard would mint the literal id
    ``"None"`` and hand the caller something it can store, quote back and never
    resolve. The rest cover the shape the tool is not expected to return but
    could after a refactor — an ``action`` outside
    :class:`~creek_mcp.api.models.JournalAction`, a ``tier_ceiling`` the wire
    enum cannot name, an id list that is not a list.

    Driven by substituting the tool, because a real ingest cannot produce any
    of these — which is precisely why the guards would otherwise be untested
    and quietly rot.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
        overrides: The fields to corrupt on an otherwise-good tool return.
        reason: A readable label for the failure being simulated.
    """
    from creek_mcp.httpapi import upload as upload_module

    real = upload_module.upload_tool

    def _corrupting_tool(**kwargs: Any) -> dict[str, Any]:
        """Run the real tool, then break one field of its success payload."""
        result = real(**kwargs)
        return {**result, **overrides} if result.get("status") == "ok" else result

    monkeypatch.setattr(upload_module, "upload_tool", _corrupting_tool)

    response = _post(vault, _body())

    assert response.status_code == ERROR_STATUS[ErrorCode.INTERNAL_ERROR], reason
    assert envelope(response)["code"] == ErrorCode.INTERNAL_ERROR.value


# --------------------------------------------------------------------------- #
# The published document
# --------------------------------------------------------------------------- #


def test_only_the_upload_route_documents_the_415() -> None:
    """``415`` is declared on the one route that can return it, and no other.

    Both directions matter. Omitting it from the upload route leaves a
    generated client with no branch for the refusal it meets the first time
    somebody uploads a ``conversations.json``; declaring it on the other five
    advertises a status they cannot produce, which is a branch a consumer
    writes and can never exercise.
    """
    from creek_mcp.api.openapi import build_openapi

    status = str(_UNSUPPORTED_SOURCE_STATUS)
    documenting = {
        path
        for path, operations in build_openapi()["paths"].items()
        for operation in operations.values()
        if status in operation["responses"]
    }
    assert documenting == {UPLOAD_PATH}


def test_the_documented_415_carries_the_published_message() -> None:
    """The document quotes ``ERROR_MESSAGES``, never a second copy of the prose.

    A description written by hand here is one that drifts from what the server
    actually sends, and a consumer reading the document would then be shown a
    remedy the refusal does not carry.
    """
    from creek_mcp.api.openapi import build_openapi

    responses = build_openapi()["paths"][UPLOAD_PATH]["post"]["responses"]
    assert (
        responses[str(_UNSUPPORTED_SOURCE_STATUS)]["description"]
        == ERROR_MESSAGES[ErrorCode.UNSUPPORTED_SOURCE]
    )


def test_the_upload_request_model_is_documented() -> None:
    """``POST /v1/uploads`` publishes its request body, so a client can generate.

    The route table names ``UploadRequest``; this asserts the document actually
    reached it, rather than mounting a body-taking route with no schema.
    """
    from creek_mcp.api.openapi import build_openapi

    document = build_openapi()
    operation = document["paths"][UPLOAD_PATH]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("UploadRequest")
    assert "UploadRequest" in document["components"]["schemas"]
    assert "UploadResponse" in document["components"]["schemas"]


# --------------------------------------------------------------------------- #
# Mounting
# --------------------------------------------------------------------------- #


def test_the_upload_route_is_mounted_for_post_only(vault: Path) -> None:
    """Every other verb on the path is a routing miss, not a method error.

    ``405`` is outside the published status set on purpose, so a method miss
    must be indistinguishable from a path miss.

    Args:
        vault: A seeded vault.
    """
    build_app(vault_path=vault)
    with client(vault_path=vault) as test_client:
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            response = test_client.request(
                method, UPLOAD_PATH, headers=headers(ceiling="open")
            )
            assert response.status_code == ERROR_STATUS[ErrorCode.NOT_FOUND], method


# --------------------------------------------------------------------------- #
# One bound, both write surfaces
# --------------------------------------------------------------------------- #


def test_both_write_surfaces_bound_the_external_id_by_the_same_object() -> None:
    """The journal route and the upload model share one ``MAX_EXTERNAL_ID_CHARS``.

    ``external_id`` reaches the vault two ways — as a URL path segment on
    ``PUT /v1/journal-entries/{external_id}`` and as a JSON field on
    ``POST /v1/uploads`` — and both mint a staged name through the same
    ``safe_stem``. Two independently-declared bounds that happen to agree today
    would let one surface accept an id the other refuses, so the same id would
    be addressable through one write path and not the other.
    """
    from creek_mcp.api import models
    from creek_mcp.httpapi import journal

    assert journal.MAX_EXTERNAL_ID_CHARS is models.MAX_EXTERNAL_ID_CHARS


def test_the_journal_route_imports_the_bound_rather_than_restating_it() -> None:
    """``journal.py`` declares no literal of its own, as its docstring claims.

    Equality alone would still pass if somebody re-declared the literal in
    ``journal.py`` at the same value, which is precisely the drift this pins:
    the module's binding must come from an import of
    :mod:`creek_mcp.api.models`, so a later edit to the canonical bound cannot
    leave a second copy behind.
    """
    import ast
    import inspect

    from creek_mcp.api import models
    from creek_mcp.httpapi import journal

    tree = ast.parse(inspect.getsource(journal))
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in (
            [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        )
        if isinstance(target, ast.Name)
    }
    assert "MAX_EXTERNAL_ID_CHARS" not in assigned

    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "MAX_EXTERNAL_ID_CHARS"
    }
    assert imported_from == {models.__name__}
