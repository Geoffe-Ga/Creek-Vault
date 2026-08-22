"""``PUT /v1/journal-entries/{external_id}`` over the shared journal tool (#1075).

The first tracer stub replaced by real behaviour, and the one that has to prove
the epic's central claim: HTTP is an *adapter* over
:func:`creek_mcp.tools.journal.journal_ingest_tool`, not a second journal
implementation. Every idempotency, tier and audit guarantee this module asserts
is a guarantee the tool already had; what is new is that the route reaches them
by delegating rather than by re-deriving them.

It is also the write path, which is where a bypass would do durable damage. The
tool refuses an above-ceiling entry *before* staging and audits both the refusal
and the success; a route that called ``run_ingest`` directly, or that passed a
ceiling the adapter policy never admitted, would lose both silently and still
look correct from the outside. So the filesystem is asserted on, not only the
response — `test_a_tier_above_the_ceiling_stages_nothing` fails if a staged
entry appears even when the status line is a correct ``403``.

**Two of the issue's own claims did not survive re-derivation, and the tests
here follow the code rather than the issue.** See the module-level notes on
:func:`test_an_intimate_tier_is_not_even_expressible` and
:func:`test_an_above_ceiling_fragment_body_survives_a_lower_ceiling_overwrite`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek_mcp.api.models import ErrorCode
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.httpapi.journal import (
    MAX_EXTERNAL_ID_CHARS,
    REPLACEMENT_CHARACTER,
    journal_refusal_code,
)
from creek_mcp.tier_ceiling import TIER_REQUIRED_REASON, TierCeiling
from creek_mcp.tools.journal import journal_ingest_tool
from tests.adapter_parity import (
    Outcome,
    alias_fragment_ids,
    assert_parity,
    audit_trail,
    http_outcome,
    mcp_outcome,
)
from tests.v1_api_support import (
    CONSUMER,
    client,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
    snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx

_OK: Final[int] = 200
_INVALID: Final[int] = 422
_REFUSED: Final[int] = 403

_ENTRY_ID: Final[str] = "adepthood:entry:2026-07-31T06:12:00Z"
"""A synthetic consumer-side id in the shape Adepthood mints."""

_CONTENT: Final[str] = "synthetic-journal-sentence-1075 that must never be echoed"
"""The submitted body. Distinctive, so "did anything echo it?" is checkable."""

_EDITED: Final[str] = "synthetic-journal-sentence-1075 revised on a later sync"
"""A second body for the same id, so the update path is exercised for real."""

_TIMESTAMP: Final[str] = "2026-07-31T06:12:00+00:00"

_SUCCESS_KEYS: Final[tuple[str, ...]] = (
    "external_id",
    "fragment_id",
    "action",
    "tier",
    "tier_ceiling",
)
"""The success facts the parity harness compares for this vertical.

The tool returns all five under these exact names, so parity here is a direct
comparison rather than a translation — which is the point of picking journal as
the first vertical.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the HTTP adapter."""
    yield seed_vault(tmp_path / "http")


@pytest.fixture
def mcp_vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a second seeded vault, so the two adapters never share a trail."""
    yield seed_vault(tmp_path / "mcp")


# --------------------------------------------------------------------------- #
# Request helpers
# --------------------------------------------------------------------------- #


def _body(content: str = _CONTENT, tier: str = "open") -> dict[str, Any]:
    """Return a ``JournalUpsertRequest``-shaped body.

    Args:
        content: The entry body.
        tier: The declared entry tier.

    Returns:
        The request body.
    """
    return {"content": content, "timestamp": _TIMESTAMP, "tier": tier}


def _put(
    vault_path: Path,
    external_id: str = _ENTRY_ID,
    *,
    body: object | None = None,
    ceiling: str | None = None,
) -> httpx.Response:
    """Send one journal upsert and return the raw response.

    Args:
        vault_path: The vault the app is built over.
        external_id: The path segment to address.
        body: The JSON body, or ``None`` for the canonical one.
        ceiling: The declared tier ceiling, or ``None`` to send no header.

    Returns:
        The response.
    """
    with client(vault_path=vault_path) as test_client:
        return test_client.put(
            f"/v1/journal-entries/{external_id}",
            json=_body() if body is None else body,
            headers=headers(ceiling=ceiling),
        )


def _fragments(vault_path: Path) -> list[Path]:
    """Return every fragment file in *vault_path*.

    Args:
        vault_path: The vault root.

    Returns:
        The sorted fragment paths.
    """
    return sorted((vault_path / "01-Fragments").rglob("*.md"))


def _staged(vault_path: Path) -> list[Path]:
    """Return every staged journal entry in *vault_path*.

    Args:
        vault_path: The vault root.

    Returns:
        The sorted staged-entry paths.
    """
    return sorted((vault_path / "00-Creek-Meta/adepthood/journal").rglob("*.md"))


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
# Idempotency — the tracer
# --------------------------------------------------------------------------- #


def test_the_same_entry_twice_is_created_then_unchanged(vault: Path) -> None:
    """The proposed tracer: one entry, sent twice, one fragment, two audits.

    Args:
        vault: A seeded vault.
    """
    first = _put(vault)
    second = _put(vault)

    assert first.status_code == _OK
    assert second.status_code == _OK
    assert envelope(first)["action"] == "created"
    assert envelope(second)["action"] == "unchanged"
    assert envelope(first)["fragment_id"] == envelope(second)["fragment_id"]
    assert len(_fragments(vault)) == 1
    assert len(_audit_records(vault)) == 2


def test_an_edited_entry_updates_the_same_fragment(vault: Path) -> None:
    """A new body under the same id rewrites in place, id preserved.

    Args:
        vault: A seeded vault.
    """
    created = envelope(_put(vault))
    updated = envelope(_put(vault, body=_body(content=_EDITED)))

    assert updated["action"] == "updated"
    assert updated["fragment_id"] == created["fragment_id"]
    assert len(_fragments(vault)) == 1


def test_ids_that_slug_identically_get_distinct_fragments(vault: Path) -> None:
    """``_safe_stem``'s hash guarantee survives the route.

    ``"a b"`` and ``"a-b"`` collapse to the same readable slug, so a route that
    re-derived the staged name from the slug alone would merge two consumers'
    entries into one fragment. The trailing digest of the *raw* id is what stops
    that, and it only helps if the route hands the raw id to the tool.

    Args:
        vault: A seeded vault.
    """
    spaced = envelope(_put(vault, "a b"))
    hyphenated = envelope(_put(vault, "a-b"))

    assert spaced["fragment_id"] != hyphenated["fragment_id"]
    assert len(_fragments(vault)) == 2


# --------------------------------------------------------------------------- #
# Tier refusal — asserted on the filesystem, not only the status line
# --------------------------------------------------------------------------- #


def test_a_tier_above_the_ceiling_stages_nothing(vault: Path) -> None:
    """``personal`` under an ``open`` ceiling is refused before persistence.

    The status line alone would not prove it: the tool's gate 1 sits *above*
    ``_stage_entry`` precisely so a refusal leaves no staged copy, and a route
    that staged first and refused second would still answer ``403``.

    Args:
        vault: A seeded vault.
    """
    response = _put(vault, body=_body(tier="personal"), ceiling="open")

    assert response.status_code == _REFUSED
    assert envelope(response)["code"] == ErrorCode.PRIVACY_REFUSED.value
    assert _staged(vault) == []
    assert _fragments(vault) == []


def test_a_personal_entry_is_admitted_under_a_personal_ceiling(vault: Path) -> None:
    """The same write succeeds once the caller declares the ceiling for it.

    Pins that the refusal above is about the *ceiling*, not about ``personal``
    being unwritable over HTTP at all.

    Args:
        vault: A seeded vault.
    """
    response = _put(vault, body=_body(tier="personal"), ceiling="personal")

    assert response.status_code == _OK
    assert envelope(response)["tier"] == "personal"
    assert envelope(response)["tier_ceiling"] == "personal"


def test_an_intimate_tier_is_not_even_expressible(vault: Path) -> None:
    """#1075's fourth acceptance criterion is wrong, and this is why.

    The issue asks for ``tier: "intimate"`` under an ``open`` ceiling to answer
    ``privacy_refused``. It cannot, and it should not: #1072 typed the request's
    ``tier`` as :class:`~creek_mcp.api.models.WireTierCeiling`, which has two
    members, so ``intimate`` fails *schema* validation and earns
    ``invalid_request`` — the body does not satisfy the published schema, which
    is exactly true. Answering ``privacy_refused`` instead would mean the server
    had accepted the field, resolved something, and ranked it, none of which
    happened.

    What the criterion is really protecting — no staged file — is asserted here
    too, and the reachable tier-exceeds-ceiling path is covered by
    :func:`test_a_tier_above_the_ceiling_stages_nothing`.

    Args:
        vault: A seeded vault.
    """
    before = snapshot(vault)
    response = _put(vault, body=_body(tier="intimate"), ceiling="open")

    assert response.status_code == _INVALID
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert snapshot(vault) == before


def test_a_remote_caller_declaring_an_intimate_ceiling_never_reaches_the_route(
    vault: Path,
) -> None:
    """The adapter edge refuses the ceiling before any vault read (#1074).

    Nothing is written — not a fragment, not a staged entry, and not the audit
    file, whose creation would itself prove the vault had been touched.

    Args:
        vault: A seeded vault.
    """
    before = snapshot(vault)
    response = _put(vault, ceiling="intimate")

    assert response.status_code == _INVALID
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert snapshot(vault) == before


def test_an_above_ceiling_fragment_body_survives_a_lower_ceiling_overwrite(
    vault: Path,
) -> None:
    """#970's overwrite gate holds through the route — and #970 is *closed*.

    The issue calls this a "known inherited gap" and asks for a test pinning the
    broken behaviour. It is not a gap: #970 shipped
    :func:`creek_mcp.tools.journal._refuse_unadmitted_overwrite`, which asks
    "could this caller have *read* what it is about to destroy?" before staging
    anything. Pinning the pre-fix behaviour would have pinned a hole that no
    longer exists. So this pins the fix instead, through the adapter, which is
    the part #1075 could actually have broken — by passing its own ceiling, or
    by reaching ``run_ingest`` around the tool.

    Args:
        vault: A seeded vault.
    """
    created = _put(vault, body=_body(tier="personal"), ceiling="personal")
    assert created.status_code == _OK

    refused = _put(vault, body=_body(content=_EDITED), ceiling="open")

    assert refused.status_code == _REFUSED
    assert envelope(refused)["code"] == ErrorCode.PRIVACY_REFUSED.value
    body = frontmatter.load(_fragments(vault)[0]).content
    assert _CONTENT in body
    assert _EDITED not in body


# --------------------------------------------------------------------------- #
# The path segment
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "external_id"),
    [
        ("blank", "%20%20"),
        ("oversized", "x" * (MAX_EXTERNAL_ID_CHARS + 1)),
        ("undecodable", f"broken{REPLACEMENT_CHARACTER}id"),
        ("control-character", "line%00break"),
    ],
)
def test_an_inadmissible_external_id_is_refused_before_the_tool(
    vault: Path, label: str, external_id: str
) -> None:
    """A path segment that cannot be an idempotency key never reaches staging.

    ``_safe_stem`` accepts literally any string, so nothing below this point
    would refuse these — an oversized id would become an 80-character slug plus
    a digest, and an id carrying U+FFFD is one whose bytes did not survive URL
    decoding, so the client's key and the server's key are already different
    strings. Both would silently mint a fragment under an id the client cannot
    address again.

    Args:
        vault: A seeded vault.
        label: The refusal being exercised, for the parametrize id.
        external_id: The path segment to send.
    """
    assert label
    before = snapshot(vault)
    response = _put(vault, external_id)

    assert response.status_code == _INVALID
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert snapshot(vault) == before


def test_the_longest_admissible_external_id_is_accepted(vault: Path) -> None:
    """The bound is inclusive, so the cap is a cap and not an off-by-one.

    Args:
        vault: A seeded vault.
    """
    response = _put(vault, "x" * MAX_EXTERNAL_ID_CHARS)

    assert response.status_code == _OK


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


def test_an_omitted_timestamp_defaults_to_now_rather_than_failing(
    vault: Path,
) -> None:
    """``timestamp`` is optional on the wire and optional in the tool.

    Args:
        vault: A seeded vault.
    """
    response = _put(vault, body={"content": _CONTENT, "tier": "open"})

    assert response.status_code == _OK
    assert envelope(response)["action"] == "created"


def test_an_unparseable_timestamp_behaves_as_the_tool_does(
    vault: Path, mcp_vault: Path
) -> None:
    """Whatever the tool makes of a nonsense timestamp, the route makes too.

    Asserting a *specific* outcome here would encode this module's guess about
    the markdown ingestor's date handling. Asserting parity encodes the only
    thing #1075 is entitled to promise: the route adds no timestamp behaviour of
    its own, and in particular does not turn a tool-level oddity into a ``500``.

    Args:
        vault: The HTTP adapter's vault.
        mcp_vault: The MCP adapter's vault.
    """
    over_http = _put(vault, body=_body() | {"timestamp": "not-a-timestamp"})
    over_mcp = journal_ingest_tool(
        vault_path=mcp_vault,
        content=_CONTENT,
        external_id=_ENTRY_ID,
        timestamp="not-a-timestamp",
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer=CONSUMER,
    )

    assert over_http.status_code != 500
    assert_parity(
        "unparseable-timestamp",
        _normalised_http(over_http, vault),
        _normalised_mcp(over_mcp, mcp_vault),
    )


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def test_the_audit_record_carries_the_bearer_consumer(vault: Path) -> None:
    """The verified token's ``client_id`` reaches the trail, not a default.

    ``journal_ingest_tool``'s ``consumer`` defaults to ``"unknown"``, and the
    MCP server passes ``CREEK_MCP_CONSUMER``. A route that forgot to pass the
    authenticated identity would audit every Adepthood write as an anonymous
    one, which is precisely the fact an investigator would need.

    Args:
        vault: A seeded vault.
    """
    _put(vault)

    assert [record["consumer"] for record in _audit_records(vault)] == [CONSUMER]


def test_a_refusal_is_audited_too(vault: Path) -> None:
    """The tier gate audits before it refuses, and the route does not suppress it.

    Args:
        vault: A seeded vault.
    """
    _put(vault, body=_body(tier="personal"), ceiling="open")

    records = _audit_records(vault)
    assert [record["tier_ceiling"] for record in records] == ["open"]
    assert [record["consumer"] for record in records] == [CONSUMER]


def test_no_response_or_audit_record_carries_the_submitted_content(
    vault: Path,
) -> None:
    """The entry body appears in the fragment and nowhere else.

    Args:
        vault: A seeded vault.
    """
    response = _put(vault)
    refusal = _put(vault, body=_body(tier="personal"), ceiling="open")

    assert _CONTENT not in response.text
    assert _CONTENT not in refusal.text
    log = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert _CONTENT not in log


def test_a_refusal_echoes_neither_the_path_identifier_nor_a_path(
    vault: Path,
) -> None:
    """An error body is a function of the *code*, never of what the caller sent.

    Inherited from ``tests/test_v1_api_not_implemented.py``, which asserted it
    of the ``501`` stub and could not go on doing so once #1075 made this route
    real. A refusal that echoed the ``external_id`` is one step from a refusal
    that echoes what the server resolved from it; a refusal that carried a
    filesystem path has already taken that step.

    Args:
        vault: A seeded vault.
    """
    sentinel = "zz-sentinel-external-id-zz"
    refusal = _put(vault, sentinel, body=_body(tier="personal"), ceiling="open")

    assert refusal.status_code == _REFUSED
    assert sentinel not in refusal.text
    assert "personal" not in refusal.text
    assert not contains_a_path(refusal.text)


# --------------------------------------------------------------------------- #
# Refusal mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("content and external_id are required", ErrorCode.INVALID_REQUEST),
        ("unknown tier 'nonsense'", ErrorCode.INVALID_REQUEST),
        ("entry tier personal exceeds the ceiling", ErrorCode.PRIVACY_REFUSED),
        (
            "resolved content exceeds the declared tier ceiling",
            ErrorCode.PRIVACY_REFUSED,
        ),
        ("vault unavailable", ErrorCode.TEMPORARILY_UNAVAILABLE),
        ("ingest failed: /vault/00-Creek-Meta/x.md exploded", ErrorCode.INTERNAL_ERROR),
        ("something nobody has written yet", ErrorCode.INTERNAL_ERROR),
    ],
)
def test_every_tool_refusal_maps_onto_the_taxonomy(
    reason: str, code: ErrorCode
) -> None:
    """Each refusal the tool can produce has one published code, and an unknown
    one fails closed to ``internal_error`` rather than to a plausible refusal.

    Args:
        reason: The tool's structured refusal reason.
        code: The wire code it must map to.
    """
    assert journal_refusal_code(reason) == code


def test_journal_refusal_code_maps_the_missing_tier_refusal() -> None:
    """The missing-tier refusal is ``invalid_request``, not ``internal_error``.

    A direct unit test on a pure function rather than a route drive, because
    the route cannot produce this reason today:
    :class:`~creek_mcp.api.models.JournalUpsertRequest`'s ``tier``
    (``creek_mcp/api/models.py:614``) has no default and is typed
    :class:`~creek_mcp.api.models.WireTierCeiling`, so a body that omits it
    fails schema validation and never reaches the tool. Driving a request
    would therefore assert Pydantic's behaviour, not this mapping.

    The mapping is defence in depth. :func:`journal_refusal_code` documents
    itself as *total by construction* and fails closed to ``internal_error``
    for anything it does not recognise, so a fifth refusal reason arriving
    from the tool — as one now does — would be published to ``/v1`` consumers
    as a server fault rather than as the caller error it is. The wiring is
    cheap; noticing the gap after the fact is not.
    """
    assert journal_refusal_code(TIER_REQUIRED_REASON) is ErrorCode.INVALID_REQUEST


def test_an_ingest_failure_does_not_echo_the_underlying_message(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ingest failed: …`` can carry a staged path; the wire carries none of it.

    Reached by substituting the tool, because a genuine ``result.errors`` needs
    a corrupted ingest run — and the property under test is the *projection*,
    not how the tool got there.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    leak = "/private/vault/00-Creek-Meta/adepthood/journal/secret-name.md"

    def _failing(**_kwargs: object) -> dict[str, Any]:
        return {
            "status": "refused",
            "tool": "creek.journal",
            "tier_ceiling": "open",
            "reason": f"ingest failed: could not write {leak}",
        }

    monkeypatch.setattr(
        "creek_mcp.httpapi.journal.journal_ingest_tool", _failing, raising=True
    )
    response = _put(vault)

    assert response.status_code == 500
    assert envelope(response)["code"] == ErrorCode.INTERNAL_ERROR.value
    assert leak not in response.text
    assert "secret-name" not in response.text


def _no_source_ledger(*_args: object, **_kwargs: object) -> None:
    """Stand in for a source ledger that has no record of the staged entry.

    Args:
        *_args: ``ledger_for_source``'s source type and vault path, unused.
        **_kwargs: Ditto, by keyword.
    """


def test_a_written_entry_whose_fragment_id_will_not_resolve_is_a_fault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolved ``fragment_id`` is a ``500``, never the literal ``"None"``.

    :func:`~creek_mcp.tools.journal.journal_ingest_tool` answers ``status: ok``
    with ``fragment_id=None`` when the source ledger cannot map the staged entry
    back to a fragment immediately after a successful write — the writer and the
    ledger disagree about what was just written. ``str(None)`` raises nothing, so
    a projection that guarded only with ``try``/``except`` would answer ``200``
    carrying the id ``"None"``: something the caller can store, quote back and
    never resolve. Minting an id no vault object answers to is the one thing
    this module's docstring says it must not do, so the check is explicit and
    precedes construction rather than being inferred from a failure to validate.

    Driven through the real tool rather than a substitute, because the property
    is that the race is *reachable*: ``ledger_for_source`` answering ``None`` is
    exactly how ``_resolve_fragment_id`` comes back empty on a successful write.

    Args:
        vault: A seeded vault.
        monkeypatch: Removes the source ledger the resolver reads.
    """
    monkeypatch.setattr(
        "creek_mcp.tools.journal.ledger_for_source", _no_source_ledger, raising=True
    )
    response = _put(vault)

    assert response.status_code == 500
    assert envelope(response)["code"] == ErrorCode.INTERNAL_ERROR.value
    assert "None" not in response.text
    assert "fragment_id" not in response.text


# --------------------------------------------------------------------------- #
# HTTP <-> MCP parity
# --------------------------------------------------------------------------- #


def _normalised_http(response: httpx.Response, vault_path: Path) -> Outcome:
    """Return *response*'s parity outcome.

    Args:
        response: The response under test.
        vault_path: The vault the app ran against.

    Returns:
        The normalised outcome.
    """
    return http_outcome(response, vault_path, keys=_SUCCESS_KEYS)


def _normalised_mcp(result: dict[str, Any], vault_path: Path) -> Outcome:
    """Return *result*'s parity outcome.

    Args:
        result: The tool's return dict.
        vault_path: The vault the tool ran against.

    Returns:
        The normalised outcome.
    """
    return mcp_outcome(
        result, vault_path, keys=_SUCCESS_KEYS, classify=journal_refusal_code
    )


_SCENARIOS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("create-open", _CONTENT, "open", "open"),
    ("create-personal", _CONTENT, "personal", "personal"),
    ("refused-above-ceiling", _CONTENT, "personal", "open"),
)
"""``(name, content, entry tier, declared ceiling)`` rows run through both adapters.

Three rows, chosen so the table covers a success, a success at the *other*
admissible ceiling, and a refusal — the wheel and reflection verticals extend
this same table shape rather than building a second harness.
"""


@pytest.mark.parametrize(
    ("name", "content", "tier", "ceiling"),
    _SCENARIOS,
    ids=[row[0] for row in _SCENARIOS],
)
def test_http_and_mcp_agree_on_every_scenario(
    vault: Path,
    mcp_vault: Path,
    name: str,
    content: str,
    tier: str,
    ceiling: str,
) -> None:
    """The same scenario, both adapters, identical behavioural outcome.

    Args:
        vault: The HTTP adapter's vault.
        mcp_vault: The MCP adapter's vault.
        name: The scenario name, surfaced in a divergence message.
        content: The entry body.
        tier: The declared entry tier.
        ceiling: The declared tier ceiling.
    """
    over_http = _put(vault, body=_body(content=content, tier=tier), ceiling=ceiling)
    over_mcp = journal_ingest_tool(
        vault_path=mcp_vault,
        content=content,
        external_id=_ENTRY_ID,
        timestamp=_TIMESTAMP,
        tier=tier,
        privacy_tier_ceiling=TierCeiling(ceiling),
        consumer=CONSUMER,
    )

    assert_parity(
        name,
        _normalised_http(over_http, vault),
        _normalised_mcp(over_mcp, mcp_vault),
    )


def test_the_two_adapters_audit_the_same_facts(vault: Path, mcp_vault: Path) -> None:
    """Tool, ceiling, created tier and affected fragment ids all agree.

    Args:
        vault: The HTTP adapter's vault.
        mcp_vault: The MCP adapter's vault.
    """
    _put(vault)
    journal_ingest_tool(
        vault_path=mcp_vault,
        content=_CONTENT,
        external_id=_ENTRY_ID,
        timestamp=_TIMESTAMP,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer=CONSUMER,
    )

    assert alias_fragment_ids(
        Outcome(kind="ok", audit=audit_trail(vault))
    ) == alias_fragment_ids(Outcome(kind="ok", audit=audit_trail(mcp_vault)))


def test_the_parity_harness_can_actually_fail() -> None:
    """The harness is not vacuous: two different outcomes are not parity.

    Every parity assertion above is an equality between two values this module
    computes. If :func:`assert_parity` compared nothing, the whole table would
    stay green forever.
    """
    with pytest.raises(AssertionError, match="divergence"):
        assert_parity(
            "synthetic",
            Outcome(kind="ok", payload={"action": "created"}),
            Outcome(kind="ok", payload={"action": "unchanged"}),
        )


# --------------------------------------------------------------------------- #
# Advisories on the wire (#1372)
# --------------------------------------------------------------------------- #


def _unpin(vault_path: Path) -> None:
    """Leave *vault_path* looking un-migrated for the #1329 id-derivation fix.

    :func:`creek.ingest.pipeline.unpinned_vault_warning` fires when the
    markdown ingest ledger is empty and ``01-Fragments`` already holds
    markdown — a real pre-#1329 vault, and the one state in which an
    Adepthood consumer's writes silently mint duplicates. Seeding that state
    is how this module gets a genuine advisory out of the production code
    path rather than injecting one at the seam.

    Args:
        vault_path: The seeded vault to age.
    """
    stray = vault_path / "01-Fragments" / "Notes" / "pre-existing.md"
    stray.write_text("---\nid: frag-0ldc0ffee123\n---\nA note.\n", encoding="utf-8")


def test_a_quiet_write_is_byte_identical_to_what_it_was_before(vault: Path) -> None:
    """No advisory means no ``warnings`` key at all — not ``null``, not ``[]``.

    This is the whole compatibility argument for adding a field to a published
    ``/v1`` response. ``JournalUpsertResponse.warnings`` defaults to ``None``
    and the route dumps with ``exclude_none``, so the ordinary write serves a
    consumer negotiating contract minor ``0.4`` exactly the bytes it served
    before the field existed. Drop ``exclude_none`` and every ``200`` starts
    carrying ``"warnings": null`` to clients that never negotiated it —
    against a model whose whole family is ``extra="forbid"`` precisely because
    this repo expects consumers to validate closed.

    Args:
        vault: A seeded vault, with a fresh ledger and no stray fragments.
    """
    body = envelope(_put(vault))

    assert body["status"] == "ok"
    assert "warnings" not in body


def test_an_advisory_the_run_produced_reaches_the_wire(vault: Path) -> None:
    """A real un-pinned vault puts the #1329 advisory on the response.

    The counterpart to the test above, and the reason the field was added:
    ``journal_ingest_tool`` has always computed this advisory and the adapter
    has always dropped it, so an Adepthood consumer syncing into an
    un-migrated vault was answered ``ok`` while every entry it sent minted a
    duplicate. ``_render`` builds the model field by field, so a key the tool
    sets but the model does not declare is silently re-dropped here — which is
    why this assertion is on the HTTP body and not on the tool's dict.

    Args:
        vault: A seeded vault, aged into the pre-#1329 state.
    """
    _unpin(vault)

    body = envelope(_put(vault))

    assert body["status"] == "ok"
    assert any("--pin-source-ids" in advisory for advisory in body["warnings"])


def test_the_wire_advisory_names_no_vault_fragment(vault: Path) -> None:
    """Whatever crosses ``/v1`` carries no fragment id but its own.

    The advisory channel the route reads is
    :attr:`creek.ingest.pipeline.IngestRunResult.ceiling_safe_warnings`, whose
    entries are content-free by construction at the producer. The operator
    channel — which interpolates real superseded ids — must never reach a
    remote caller, so this asserts on the serialised advisories rather than on
    the whole body, which legitimately carries the id this very call created.

    Args:
        vault: A seeded vault, aged into the pre-#1329 state.
    """
    _unpin(vault)

    advisories = json.dumps(envelope(_put(vault))["warnings"])

    assert advisories != "[]"
    assert "frag-" not in advisories
