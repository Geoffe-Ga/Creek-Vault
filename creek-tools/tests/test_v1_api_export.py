"""``GET /v1/export`` — the owner's own vault-held content, paged (#2033).

The route is the Creek half of Geoffe-Ga/adepthood#3579: on a managed vault the
vault is the only copy of an imported document, and the writer cannot reach the
vault except through adepthood. So the tests below are organised around the
promises adepthood builds its "Export my data" on:

* **Nothing stored only in the vault is stranded.** A synthetic canary uploaded
  through ``POST /v1/uploads`` comes back byte-for-byte, with the fragment it
  became, and an ``intimate`` fragment comes back too — this is the owner
  exporting to the owner.
* **Only the owner.** The route answers only the vault's sole configured
  consumer; any other configuration is the published ``403``.
* **Bounded and restart-stable.** Pages are capped by count and by content
  bytes, ordered deterministically by a digest of each on-disk key, and the
  cursor is that digest, so a fresh process continues where the last one
  stopped — and a cursor in a URL spells no filename.
* **Content never reaches a log.** Every log record and stream written while a
  page is built — the client's own URL logging included — is swept for the
  canary and the filenames.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.ingest.journal_staging import UPLOAD_STAGING_RELDIR
from creek_mcp import vault_export
from creek_mcp.api.models import (
    CAPABILITY_SINCE_MINOR,
    CONTRACT_MINOR,
    EXPORT_MAX_LIMIT,
    Capability,
    ErrorCode,
    ExportResponse,
)
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.httpapi import export as export_mod
from creek_mcp.vault_export import (
    ExportCursor,
    build_page,
    decode_cursor,
    encode_cursor,
    order_key,
)
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    CONSUMER,
    EXPORT_PATH,
    STRONG_TOKEN,
    UPLOAD_PATH,
    client,
    envelope,
    headers,
    seed_vault,
    snapshot,
    verifier,
)

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

_OK: Final[int] = 200
_REFUSED: Final[int] = 403
_INCOMPATIBLE_VERSION: Final[int] = 409
_INVALID_REQUEST: Final[int] = 422
_UNAVAILABLE: Final[int] = 503

_EXPORT_MINOR: Final[str] = "0.27"
_BEFORE_EXPORT_MINOR: Final[str] = "0.26"

_CANARY: Final[str] = "synthetic-canary-7f3a: a sentence only the vault holds"
"""A synthetic marker. Never real writing; unique enough to find in any sweep."""

_CANARY_BYTES: Final[bytes] = f"{_CANARY}\n".encode()
_CANARY_EXTERNAL_ID: Final[str] = "adepthood:doc:canary-only-in-vault"
_INTIMATE_MARKER: Final[str] = "synthetic-intimate-91c2: the owner's own words"
_BODY: Final[str] = "A synthetic fragment body for the export suite."
_STAMP: Final[str] = "2026-10-31T09:00:00+00:00"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a seeded vault with the scaffold the upload writer needs.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    return seed_vault(tmp_path / "vault")


def _owner_client(vault: Path, **kwargs: Any) -> Any:
    """Return a client over an app with exactly one consumer: the owner.

    A managed vault mounts one consumer secret
    (:class:`creek_mcp.container_runtime.SingleConsumerSecret`), which is the
    configuration the export is served under.

    Args:
        vault: The vault the app serves.
        **kwargs: Passed through to :func:`tests.v1_api_support.client`.

    Returns:
        The test client.
    """
    return client(
        vault_path=vault,
        verifier=verifier({CONSUMER: (STRONG_TOKEN,)}),
        **kwargs,
    )


def _get(test_client: Any, **params: str) -> httpx.Response:
    """GET the export with *params* as the query string.

    Args:
        test_client: The client to call through.
        **params: Query parameters.

    Returns:
        The response.
    """
    response: httpx.Response = test_client.get(
        EXPORT_PATH, params=params, headers=headers()
    )
    return response


def _write_fragment(
    vault: Path,
    name: str,
    *,
    frag_id: str,
    tier: str = "personal",
    body: str = _BODY,
    subdir: str = "01-Fragments/Notes",
    origin_key: str | None = None,
) -> Path:
    """Write one classified fragment file and return its path.

    Args:
        vault: The vault root.
        name: The file name.
        frag_id: The fragment id.
        tier: The privacy tier stamped on it.
        body: The fragment body.
        subdir: The vault-relative folder to write into.
        origin_key: The source key, when the fragment came from a staged file.

    Returns:
        The written path.
    """
    source: dict[str, Any] = {"platform": "document", "author": "self"}
    if origin_key is not None:
        source["origin_key"] = origin_key
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": f"Synthetic {frag_id}",
        "created": _STAMP,
        "ingested": _STAMP,
        "source": source,
        "frequency": {"primary": "F3", "secondary": []},
        "wavelength": {"phase": "rising"},
        "privacy_tier": tier,
    }
    folder = vault / subdir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return path


def _stage_document(vault: Path, name: str, raw: bytes) -> Path:
    """Place *raw* in the upload staging directory as the upload tool would.

    Args:
        vault: The vault root.
        name: The staged file name.
        raw: The document bytes.

    Returns:
        The staged path.
    """
    staged = vault / UPLOAD_STAGING_RELDIR / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(raw)
    return staged


def _walk(test_client: Any, *, limit: int) -> list[dict[str, Any]]:
    """Follow ``next_cursor`` to the end and return every page body.

    Args:
        test_client: The client to call through.
        limit: The page size to ask for.

    Returns:
        Every page, in order.
    """
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(100):
        params = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        response = _get(test_client, **params)
        assert response.status_code == _OK, response.text
        page = envelope(response)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
    pytest.fail("the export never reached its last page")


def _seed_mixed(vault: Path) -> None:
    """Stage three documents and write four fragments across two subtrees.

    Args:
        vault: The vault root.
    """
    for stem in ("c-doc", "a-doc", "b-doc"):
        _stage_document(vault, f"{stem}.txt", f"synthetic {stem}\n".encode())
    _write_fragment(vault, "z.md", frag_id="frag-z")
    _write_fragment(vault, "m.md", frag_id="frag-m")
    _write_fragment(vault, "b.md", frag_id="frag-b", subdir="09-Reference")
    _write_fragment(vault, "a.md", frag_id="frag-a", subdir="01-Fragments/Essays")


# --------------------------------------------------------------------------- #
# The canary: what lives only in the vault comes back
# --------------------------------------------------------------------------- #


def test_a_document_stored_only_in_the_vault_appears_in_the_export(
    vault: Path,
) -> None:
    """The adepthood#3579 acceptance test, against the real upload path.

    The canary is uploaded through ``POST /v1/uploads`` — the only way a
    managed vault receives a document — and then exported. Its original bytes
    come back exactly, with a matching digest, and so does the fragment the
    ingest pipeline made of it, linked back to it by ``document_id``.
    """
    upload_body = {
        "filename": "canary.txt",
        "content_base64": base64.b64encode(_CANARY_BYTES).decode("ascii"),
        "external_id": _CANARY_EXTERNAL_ID,
        "tier": "personal",
    }
    with _owner_client(vault) as test_client:
        uploaded = test_client.post(
            UPLOAD_PATH, json=upload_body, headers=headers(ceiling="personal")
        )
        assert uploaded.status_code == _OK, uploaded.text
        response = _get(test_client)

    assert response.status_code == _OK, response.text
    body = ExportResponse.model_validate(envelope(response))
    (document,) = body.documents
    assert document.content_base64 is not None
    assert base64.b64decode(document.content_base64) == _CANARY_BYTES
    assert document.sha256 == hashlib.sha256(_CANARY_BYTES).hexdigest()
    assert document.size_bytes == len(_CANARY_BYTES)
    assert document.content_included is True
    assert document.document_id.endswith(".txt")

    derived = [frag for frag in body.fragments if frag.document_id is not None]
    assert [frag.document_id for frag in derived] == [document.document_id]
    (fragment,) = derived
    assert fragment.markdown is not None
    assert _CANARY in fragment.markdown
    assert fragment.tier.value == "personal"
    assert body.next_cursor is None


def test_an_intimate_fragment_is_exported_to_its_owner(vault: Path) -> None:
    """Every tier crosses: the owner's own intimate writing is theirs to take.

    The ceiling header plays no part — an omitted header still reads as
    ``open`` everywhere else on ``/v1``, and the export is not ceiling-filtered
    because it is served to the owner only.
    """
    _write_fragment(
        vault, "x.md", frag_id="frag-x", tier="intimate", body=_INTIMATE_MARKER
    )
    with _owner_client(vault) as test_client:
        response = _get(test_client)

    assert response.status_code == _OK
    (fragment,) = ExportResponse.model_validate(envelope(response)).fragments
    assert fragment.tier.value == "intimate"
    assert fragment.markdown is not None
    assert _INTIMATE_MARKER in fragment.markdown
    assert fragment.frequency == "F3"
    assert fragment.phase == "rising"
    assert fragment.source_platform == "document"


def test_an_empty_vault_exports_an_empty_last_page(vault: Path) -> None:
    """A new vault is a ``200`` with nothing in it, never an error."""
    with _owner_client(vault) as test_client:
        response = _get(test_client)

    assert response.status_code == _OK
    assert envelope(response) == {
        "status": "ok",
        "documents": [],
        "fragments": [],
        "skipped": 0,
        "next_cursor": None,
    }


# --------------------------------------------------------------------------- #
# Ordering, pagination and the cursor
# --------------------------------------------------------------------------- #


def test_pages_walk_documents_then_fragments_in_a_fixed_order(vault: Path) -> None:
    """Documents, then fragments, each by the digest of its on-disk key.

    Walked at a page size that splits both kinds and straddles the boundary,
    and every item is seen exactly once.
    """
    _seed_mixed(vault)
    with _owner_client(vault) as test_client:
        pages = _walk(test_client, limit=2)

    documents = [doc["document_id"] for page in pages for doc in page["documents"]]
    fragments = [frag["fragment_id"] for page in pages for frag in page["fragments"]]
    assert documents == sorted(["a-doc.txt", "b-doc.txt", "c-doc.txt"], key=order_key)
    paths = {
        "01-Fragments/Essays/a.md": "frag-a",
        "01-Fragments/Notes/m.md": "frag-m",
        "01-Fragments/Notes/z.md": "frag-z",
        "09-Reference/b.md": "frag-b",
    }
    assert fragments == [paths[path] for path in sorted(paths, key=order_key)]
    assert all(len(p["documents"]) + len(p["fragments"]) <= 2 for p in pages)
    assert pages[-1]["next_cursor"] is None


def test_the_cursor_survives_a_restart(vault: Path) -> None:
    """A page asked of a fresh process continues exactly where the last left off.

    The cursor names on-disk keys and nothing held in memory, so two separate
    apps — the autostopped vault before and after it wakes — agree.
    """
    _seed_mixed(vault)
    with _owner_client(vault) as first:
        page_one = envelope(_get(first, limit="3"))
        continued = envelope(_get(first, limit="3", cursor=page_one["next_cursor"]))
    with _owner_client(vault) as second:
        restarted = envelope(_get(second, limit="3", cursor=page_one["next_cursor"]))

    assert page_one["next_cursor"] is not None
    assert restarted == continued


def test_a_page_size_above_the_maximum_is_clamped(vault: Path) -> None:
    """``limit`` is clamped to the named maximum rather than refused."""
    for index in range(EXPORT_MAX_LIMIT + 1):
        _stage_document(vault, f"doc-{index:04d}.txt", b"x")
    with _owner_client(vault) as test_client:
        response = _get(test_client, limit=str(EXPORT_MAX_LIMIT * 10))

    page = envelope(response)
    assert response.status_code == _OK
    assert len(page["documents"]) == EXPORT_MAX_LIMIT
    assert page["next_cursor"] is not None


def test_the_content_budget_splits_a_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two documents that together exceed the byte budget land on two pages."""
    monkeypatch.setattr(export_mod, "EXPORT_PAGE_CONTENT_BYTES", 10)
    _stage_document(vault, "a.txt", b"123456")
    _stage_document(vault, "b.txt", b"789012")
    with _owner_client(vault) as test_client:
        pages = _walk(test_client, limit=50)

    first, second = sorted(["a.txt", "b.txt"], key=order_key)
    assert [[d["document_id"] for d in p["documents"]] for p in pages] == [
        [first],
        [second],
    ]


def test_an_item_larger_than_the_budget_is_listed_without_its_content(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Too large to carry is said out loud, never silently dropped."""
    monkeypatch.setattr(export_mod, "EXPORT_PAGE_CONTENT_BYTES", 4)
    _stage_document(vault, "big.txt", b"far more than four bytes")
    _write_fragment(vault, "big.md", frag_id="frag-big")
    with _owner_client(vault) as test_client:
        pages = _walk(test_client, limit=50)

    documents = [d for p in pages for d in p["documents"]]
    fragments = [f for p in pages for f in p["fragments"]]
    assert documents == [
        {
            "document_id": "big.txt",
            "size_bytes": len(b"far more than four bytes"),
            "sha256": None,
            "content_base64": None,
            "content_included": False,
        }
    ]
    assert [
        (f["fragment_id"], f["markdown"], f["content_included"]) for f in fragments
    ] == [("frag-big", None, False)]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "many"},
        {"cursor": ""},
        {"cursor": "not a cursor!"},
        {"cursor": "x-" + "0" * 64},
        {"cursor": "d-" + "0" * 63},
        {"cursor": "d-" + "A" * 64},
        {"cursor": "d:a-doc.txt"},
        {"cursor": "f-" + "0" * 64 + "0"},
        {"order": "desc"},
    ],
    ids=[
        "zero-limit",
        "non-numeric-limit",
        "blank-cursor",
        "cursor-charset",
        "cursor-kind",
        "cursor-short-digest",
        "cursor-upper-case-digest",
        "cursor-names-a-file",
        "cursor-long-digest",
        "unknown-parameter",
    ],
)
def test_a_query_outside_the_schema_is_invalid_request(
    vault: Path, params: dict[str, str]
) -> None:
    """Every malformed query is the one published ``422``, reading nothing."""
    before = snapshot(vault)
    with _owner_client(vault) as test_client:
        response = _get(test_client, **params)

    assert response.status_code == _INVALID_REQUEST
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert snapshot(vault) == before


def test_a_repeated_parameter_is_invalid_request(vault: Path) -> None:
    """``limit=1&limit=2`` is refused, never resolved one way or the other."""
    with _owner_client(vault) as test_client:
        response = test_client.get(f"{EXPORT_PATH}?limit=1&limit=2", headers=headers())

    assert response.status_code == _INVALID_REQUEST


def test_the_cursor_round_trips() -> None:
    """Encoding then decoding is the identity, for both kinds."""
    for cursor in (
        ExportCursor(kind="d", digest=order_key("note-1a2b3c4d5e6f.pdf")),
        ExportCursor(kind="f", digest=order_key("01-Fragments/Notes/a b.md")),
    ):
        assert decode_cursor(encode_cursor(cursor)) == cursor


# --------------------------------------------------------------------------- #
# Ownership, versioning and availability
# --------------------------------------------------------------------------- #


def test_a_vault_with_a_second_consumer_refuses_and_reads_nothing(
    vault: Path,
) -> None:
    """With two consumers configured, no caller can prove sole ownership.

    Uploads are not consumer-scoped on disk, so the export is served only
    under the single-owner configuration a managed vault mounts. Every other
    caller gets the published ``403`` and the vault is untouched — not even
    an audit line is written.
    """
    _write_fragment(vault, "x.md", frag_id="frag-x", tier="intimate")
    before = snapshot(vault)
    with client(vault_path=vault) as test_client:
        response = _get(test_client)

    assert response.status_code == _REFUSED
    assert envelope(response)["code"] == ErrorCode.PRIVACY_REFUSED.value
    assert snapshot(vault) == before


def test_the_route_is_gated_on_its_minor(vault: Path) -> None:
    """A ``0.26`` caller is neither told about the route nor served it."""
    assert CAPABILITY_SINCE_MINOR[Capability.EXPORT] == _EXPORT_MINOR
    assert CONTRACT_MINOR == _EXPORT_MINOR
    with _owner_client(vault) as test_client:
        old = test_client.get(EXPORT_PATH, headers=headers(minor=_BEFORE_EXPORT_MINOR))
        old_caps = test_client.get(
            CAPABILITIES_PATH, headers=headers(minor=_BEFORE_EXPORT_MINOR)
        )
        new_caps = test_client.get(CAPABILITIES_PATH, headers=headers())

    assert old.status_code == _INCOMPATIBLE_VERSION
    assert Capability.EXPORT.value not in envelope(old_caps)["capabilities"]
    assert Capability.EXPORT.value in envelope(new_caps)["capabilities"]


def test_an_absent_vault_is_the_published_unavailable(tmp_path: Path) -> None:
    """No vault behind the server: the same ``503`` every ``/v1`` route gives.

    adepthood renders this as "your vault is unavailable" rather than an empty
    export, which is the whole reason it must not be a ``200``.
    """
    with _owner_client(tmp_path / "missing") as test_client:
        response = _get(test_client)

    assert response.status_code == _UNAVAILABLE
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value


def test_an_unreadable_vault_is_the_published_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that fails mid-page is ``503``, never a short ``200``."""

    def _broken(*_args: object, **_kwargs: object) -> object:
        """Fail as an unreadable directory would."""
        raise PermissionError

    _stage_document(vault, "a.txt", b"a")
    monkeypatch.setattr(export_mod, "build_page", _broken)
    with _owner_client(vault) as test_client:
        response = _get(test_client)

    assert response.status_code == _UNAVAILABLE
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value


# --------------------------------------------------------------------------- #
# Containment, skips and the audit line
# --------------------------------------------------------------------------- #


def test_symlinks_leaving_the_vault_are_never_followed(
    vault: Path, tmp_path: Path
) -> None:
    """A planted link in either tree exports nothing from outside the vault."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("---\ntype: fragment\nid: planted\n---\nplanted body\n")
    (outside / "secret.txt").write_text("planted bytes")
    staging = vault / UPLOAD_STAGING_RELDIR
    staging.mkdir(parents=True)
    (staging / "planted.txt").symlink_to(outside / "secret.txt")
    notes = vault / "01-Fragments" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "planted.md").symlink_to(secret)
    (vault / "01-Fragments" / "linked-dir").symlink_to(outside)

    with _owner_client(vault) as test_client:
        response = _get(test_client)

    assert response.status_code == _OK
    assert "planted" not in response.text


def test_an_unreadable_fragment_is_counted_and_a_plain_note_is_not(
    vault: Path,
) -> None:
    """A broken fragment is reported in ``skipped``; a non-fragment note is not.

    Notes coexist with fragments in a live vault and are not fragments at all.
    A file that cannot be decoded or parsed might have been one, so it is
    counted rather than silently dropped.
    """
    notes = vault / "01-Fragments" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "binary.md").write_bytes(b"---\nid: \xff\xfe\n---\n")
    (notes / "bad-key.md").write_text("---\n2024-05-01: x\n---\nbody\n")
    (notes / "invalid.md").write_text("---\ntype: fragment\n---\nno id\n")
    (notes / "note.md").write_text("---\ntitle: just a note\n---\nnot a fragment\n")
    _write_fragment(vault, "real.md", frag_id="frag-real")

    with _owner_client(vault) as test_client:
        page = envelope(_get(test_client))

    assert [f["fragment_id"] for f in page["fragments"]] == ["frag-real"]
    assert page["skipped"] == 3


def test_each_page_is_audited_without_content(vault: Path) -> None:
    """The owner's activity card shows the export; the trail holds no content."""
    _seed_mixed(vault)
    with _owner_client(vault) as test_client:
        first = envelope(_get(test_client, limit="2"))
        _get(test_client, limit="2", cursor=first["next_cursor"])

    trail = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert trail.count('"tool": "creek.export"') == 2
    assert first["next_cursor"] not in trail
    assert "a-doc" not in trail
    assert CONSUMER in trail


def test_nothing_content_bearing_reaches_a_log(
    vault: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Every record at every level, and both streams, stay free of content.

    Seeds the canary as a staged document and as an intimate fragment, plus
    the broken files whose skip paths are the classic place a filename gets
    logged, then exports every page.
    """
    _stage_document(vault, "canary-doc-name.txt", _CANARY_BYTES)
    _write_fragment(vault, "canary-frag-name.md", frag_id="frag-c", body=_CANARY)
    _write_fragment(
        vault,
        "intimate-frag-name.md",
        frag_id="frag-i",
        tier="intimate",
        body=_INTIMATE_MARKER,
    )
    notes = vault / "01-Fragments" / "Notes"
    (notes / "broken-frag-name.md").write_bytes(b"---\nid: \xff\n---\n")
    (notes / "invalid-frag-name.md").write_text("---\ntype: fragment\n---\nx\n")

    caplog.set_level(logging.DEBUG)
    with _owner_client(vault) as test_client:
        pages = _walk(test_client, limit=1)

    assert len(pages) > 1
    forbidden = [
        _CANARY,
        _INTIMATE_MARKER,
        "canary-doc-name",
        "canary-frag-name",
        "intimate-frag-name",
        "broken-frag-name",
        "invalid-frag-name",
        base64.b64encode(_CANARY_BYTES).decode("ascii"),
    ]
    captured = capfd.readouterr()
    logged = "\n".join(
        [caplog.text, *(record.getMessage() for record in caplog.records)]
    )
    for text in (logged, captured.out, captured.err):
        assert [marker for marker in forbidden if marker in text] == []


# --------------------------------------------------------------------------- #
# The page builder, without an app
# --------------------------------------------------------------------------- #


def _all_fragment_ids(vault: Path, *, content_bytes: int) -> list[list[str]]:
    """Page through *vault* with :func:`build_page` alone.

    Args:
        vault: The vault root.
        content_bytes: The page content budget.

    Returns:
        The fragment ids of each page, in order.
    """
    pages: list[list[str]] = []
    cursor: ExportCursor | None = None
    for _ in range(20):
        page = build_page(vault, cursor, limit=50, content_bytes=content_bytes)
        pages.append([fragment.fragment_id for fragment in page.fragments])
        if page.next_cursor is None:
            return pages
        cursor = decode_cursor(page.next_cursor)
    pytest.fail("the export never reached its last page")


def test_a_cursor_this_server_did_not_mint_decodes_to_nothing() -> None:
    """Anything off the published pattern is not a position."""
    assert decode_cursor("d:a-doc.txt") is None
    assert decode_cursor("") is None


def test_the_content_budget_splits_fragments_too(vault: Path) -> None:
    """Two fragments that together exceed the byte budget land on two pages."""
    first = _write_fragment(vault, "one.md", frag_id="frag-one")
    _write_fragment(vault, "two.md", frag_id="frag-two")
    budget = first.stat().st_size + 1

    pages = _all_fragment_ids(vault, content_bytes=budget)

    assert sorted(fragment_id for page in pages for fragment_id in page) == [
        "frag-one",
        "frag-two",
    ]
    assert all(len(page) == 1 for page in pages)


def test_a_fragment_from_elsewhere_names_no_document(vault: Path) -> None:
    """Only an ``origin_key`` inside the upload staging directory links back."""
    _write_fragment(
        vault,
        "a.md",
        frag_id="frag-a",
        origin_key="00-Creek-Meta/adepthood/journal/x.md",
    )
    _write_fragment(
        vault,
        "b.md",
        frag_id="frag-b",
        origin_key=f"{UPLOAD_STAGING_RELDIR.as_posix()}/report.xlsx#Sheet1",
    )

    page = build_page(vault, None, limit=50, content_bytes=1024 * 1024)

    assert {f.fragment_id: f.document_id for f in page.fragments} == {
        "frag-a": None,
        "frag-b": "report.xlsx",
    }


def test_a_fragment_the_wire_cannot_carry_is_counted(vault: Path) -> None:
    """A valid fragment whose id exceeds the published bound is skipped, counted."""
    _write_fragment(vault, "long.md", frag_id="x" * 600)

    page = build_page(vault, None, limit=50, content_bytes=1024 * 1024)

    assert page.fragments == []
    assert page.skipped == 1


def test_an_unlistable_subtree_fails_the_page_rather_than_shortening_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.walk`` swallows listing errors by default; the export must not."""

    def _walk_failing(
        _top: object, *, onerror: Any, followlinks: bool
    ) -> list[tuple[str, list[str], list[str]]]:
        """Report one unreadable directory the way ``os.walk`` does.

        Args:
            _top: Ignored.
            onerror: The callback ``os.walk`` hands each listing failure.
            followlinks: Ignored; asserted off.

        Returns:
            Nothing walked.
        """
        assert followlinks is False
        onerror(PermissionError("unlistable"))
        return []

    _write_fragment(vault, "a.md", frag_id="frag-a")
    monkeypatch.setattr(vault_export.os, "walk", _walk_failing)

    with pytest.raises(PermissionError):
        build_page(vault, None, limit=50, content_bytes=1024)


def test_a_file_gone_before_it_is_read_is_passed_over(vault: Path) -> None:
    """A file withdrawn between listing and reading is neither listed nor counted."""
    missing = vault / "01-Fragments" / "gone.md"

    assert vault_export._regular_size(missing) is None
    assert vault_export._read_bounded(missing, 10) is None


def test_items_that_place_nothing_never_end_the_walk_early(vault: Path) -> None:
    """Plain notes ahead of the last fragment cannot make a page look final.

    A note occupies a listing slot without being placed, so a page that ran out
    of *listed* keys has not necessarily run out of keys. The fragment is named
    so that it sorts after every note, which is the order that used to lose it.
    """
    notes = vault / "01-Fragments" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    note_keys = [f"01-Fragments/Notes/note-{index}.md" for index in range(5)]
    for key in note_keys:
        (vault / key).write_text("---\ntitle: a plain note\n---\nnot a fragment\n")
    last_note = max(order_key(key) for key in note_keys)
    name = next(
        f"frag-{index}.md"
        for index in range(1000)
        if order_key(f"01-Fragments/Notes/frag-{index}.md") > last_note
    )
    _write_fragment(vault, name, frag_id="frag-last")

    with _owner_client(vault) as test_client:
        pages = _walk(test_client, limit=1)

    found = [f["fragment_id"] for page in pages for f in page["fragments"]]
    assert found == ["frag-last"]
