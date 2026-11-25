"""A Substack export uploaded as LOOSE files lands as a zipped one does (#2123).

adepthood's folder import sends an unzipped export one ``POST /v1/uploads`` per
file. Before #2123 only a ``.zip`` got content-based source detection, so every
``<post_id>.<slug>.html`` post was read by the generic document ingestor and
filed in ``01-Fragments/Unsorted`` as ``platform: other``, and ``posts.csv``
became a spreadsheet fragment in ``01-Fragments/Data``.

**The RED this was written against**: uploading the fixture export below file
by file put all three posts in ``01-Fragments/Unsorted`` and wrote
``01-Fragments/Data/posts.md``.

The suite drives the real tool against a temp vault and asserts on disk —
where each file landed and what its frontmatter says — because the issue is
about what the owner finds in the vault, not about what the response says.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.ingest.substack import SubstackPost, single_post_ingestor
from creek.ingest.substack_loose import (
    SUBSTACK_MANIFEST_RELPATH,
    ManifestEntry,
    is_substack_manifest,
    load_manifest,
    parse_manifest_rows,
    record_manifest,
    substack_post_identity,
)
from creek.ingest.upload_receipts import read_upload_receipts
from creek.models import PrivacyTier
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.upload import (
    MANIFEST_STATUS,
    SUBSCRIBER_DATA_REASON,
    upload_tool,
)

if TYPE_CHECKING:
    from pathlib import Path

_TS: Final[str] = "2026-11-20T10:00:00+00:00"

_HEADER: Final[str] = (
    "post_id,post_date,is_published,email_sent_at,inbox_sent_at,type,"
    "audience,title,subtitle,podcast_url\n"
)
"""Substack's ``posts.csv`` header, in the column order the export writes."""

_POSTS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("171234567", "on-attention", "On Attention", "2024-03-15T08:30:00.000Z"),
    ("171234568", "second-essay", "Second Essay", "2025-01-02T12:00:00.000Z"),
    ("171234569", "plain-words", "Plain Words", "2026-04-01T09:00:00.000Z"),
)
"""``(post_id, slug, title, post_date)`` for each post in the fixture export."""

_BODY_MARKER: Final[str] = "synthetic-substack-body-2123"
"""A nonsense token in every post body, so "the body landed" is unambiguous."""


def _manifest_csv(*, id_with_slug: bool = False) -> bytes:
    """Return the fixture export's ``posts.csv``.

    Args:
        id_with_slug: Spell each ``post_id`` cell ``<id>.<slug>``, as some
            Substack exports do, instead of the bare id.

    Returns:
        The CSV bytes.
    """
    rows = "".join(
        f"{f'{post_id}.{slug}' if id_with_slug else post_id},{date},true,,,"
        f"newsletter,everyone,{title},A subtitle,\n"
        for post_id, slug, title, date in _POSTS
    )
    return (_HEADER + rows).encode()


def _post_html(title: str, *, markup: bool = True) -> bytes:
    """Return one post's HTML as Substack's export writes it.

    Args:
        title: Used only in the body text.
        markup: Include Substack's editor markup (an image container served
            from ``substackcdn.com`` and a subscription widget). A text-only
            post has none.

    Returns:
        The HTML bytes.
    """
    figure = (
        '<div class="captioned-image-container"><figure>'
        '<img src="https://substackcdn.com/image/fetch/w_1456/x.png"/>'
        "</figure></div>"
        if markup
        else ""
    )
    widget = '<div class="subscription-widget-wrap-editor"></div>' if markup else ""
    return (f"{figure}<p>{title}: {_BODY_MARKER}.</p>{widget}").encode()


def _b64(payload: bytes) -> str:
    """Return the base64 of *payload*.

    Args:
        payload: The bytes to encode.

    Returns:
        The base64 string.
    """
    return base64.b64encode(payload).decode("ascii")


def _upload(
    vault: Path,
    filename: str,
    payload: bytes,
    *,
    tier: str = "personal",
    ceiling: TierCeiling = TierCeiling.PERSONAL,
) -> dict[str, Any]:
    """Upload one loose file, keyed as adepthood keys a folder import.

    Args:
        vault: Vault root.
        filename: The file's bare name, as the folder import sends it.
        payload: The file's bytes.
        tier: The declared tier.
        ceiling: The caller's admission ceiling.

    Returns:
        The tool's return dict.
    """
    return upload_tool(
        vault_path=vault,
        filename=filename,
        content_base64=_b64(payload),
        external_id=f"adepthood:upload:{filename}",
        timestamp=_TS,
        tier=tier,
        privacy_tier_ceiling=ceiling,
    )


def _upload_posts(vault: Path, *, tier: str = "personal") -> list[dict[str, Any]]:
    """Upload every fixture post, one call per file.

    Args:
        vault: Vault root.
        tier: The declared tier.

    Returns:
        One response per post, in fixture order.
    """
    return [
        _upload(vault, f"{post_id}.{slug}.html", _post_html(title), tier=tier)
        for post_id, slug, title, _date in _POSTS
    ]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return the minimum vault layout the writer, ledger and audit need.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    root = tmp_path / "vault"
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _fragments(vault: Path) -> list[Path]:
    """Return every fragment file under ``01-Fragments``.

    Args:
        vault: Vault root.

    Returns:
        Sorted fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _relative_dirs(vault: Path) -> set[str]:
    """Return each fragment's folder, relative to ``01-Fragments``.

    Args:
        vault: Vault root.

    Returns:
        The set of folders holding a fragment.
    """
    root = vault / "01-Fragments"
    return {path.parent.relative_to(root).as_posix() for path in _fragments(vault)}


def _by_body(vault: Path, title: str) -> frontmatter.Post:
    """Return the one fragment whose body names *title*.

    Args:
        vault: Vault root.
        title: The post title written into the fixture body.

    Returns:
        The parsed fragment.
    """
    matches = [
        post
        for post in (frontmatter.load(path) for path in _fragments(vault))
        if f"{title}: {_BODY_MARKER}" in post.content
    ]
    assert len(matches) == 1, [m.metadata.get("title") for m in matches]
    return matches[0]


# --------------------------------------------------------------------------- #
# The issue's acceptance test: an export, one upload per file
# --------------------------------------------------------------------------- #


def test_an_export_uploaded_file_by_file_lands_as_substack_writing(
    vault: Path,
) -> None:
    """``posts.csv`` is a manifest; every post is Substack writing, dated by it."""
    manifest = _upload(vault, "posts.csv", _manifest_csv())
    posts = _upload_posts(vault)

    assert manifest["status"] == MANIFEST_STATUS
    assert manifest["source_type"] == "substack"
    assert "fragment_id" not in manifest
    assert [response["status"] for response in posts] == ["ok"] * len(_POSTS)
    assert {response["source_type"] for response in posts} == {"substack"}

    assert _relative_dirs(vault) == {"Writing/Substack"}
    assert len(_fragments(vault)) == len(_POSTS)
    assert not (vault / "01-Fragments" / "Data").exists()
    for _post_id, _slug, title, date in _POSTS:
        post = _by_body(vault, title)
        assert post.metadata["source"]["platform"] == "substack"
        assert post.metadata["source"]["kind"] == "writing"
        assert post.metadata["title"] == title
        assert str(post.metadata["authored_at"]).startswith(date[:10])


def test_posts_uploaded_before_the_manifest_still_land_as_substack(
    vault: Path,
) -> None:
    """Order is the folder walk's, not ours: posts first still file correctly.

    With no manifest yet, the title comes from the slug and the date is left
    unset rather than guessed.
    """
    _upload_posts(vault)

    assert _relative_dirs(vault) == {"Writing/Substack"}
    post = _by_body(vault, "On Attention")
    assert post.metadata["source"]["platform"] == "substack"
    assert post.metadata["title"] == "On attention"
    assert post.metadata.get("authored_at") is None


def test_without_a_manifest_the_htmls_own_date_is_used(vault: Path) -> None:
    """A post carrying ``datePublished`` is dated from its own HTML."""
    html = (
        b'<script type="application/ld+json">{"datePublished":"2023-07-09"}'
        b"</script>" + _post_html("Dated")
    )
    _upload(vault, "171234570.dated.html", html)

    assert str(_by_body(vault, "Dated").metadata["authored_at"]).startswith(
        "2023-07-09"
    )


def test_a_manifest_keyed_by_id_and_slug_still_dates_its_posts(
    vault: Path,
) -> None:
    """``post_id`` cells spelled ``<id>.<slug>`` join on the leading id."""
    _upload(vault, "posts.csv", _manifest_csv(id_with_slug=True))
    _upload_posts(vault)

    assert str(_by_body(vault, "Second Essay").metadata["authored_at"]).startswith(
        "2025-01-02"
    )


def test_resending_a_post_is_idempotent(vault: Path) -> None:
    """The second send of a post answers ``unchanged`` and writes nothing."""
    first = _upload(vault, "171234567.on-attention.html", _post_html("On Attention"))
    second = _upload(vault, "171234567.on-attention.html", _post_html("On Attention"))

    assert first["action"] == "created"
    assert second["action"] == "unchanged"
    assert second["fragment_id"] == first["fragment_id"]
    assert len(_fragments(vault)) == 1


def test_the_post_is_staged_where_every_upload_is(vault: Path) -> None:
    """The post keeps the single-file staging, ledger and RTBF origin key.

    Its ``origin_key`` names the staged upload, so the RTBF sweep reaches the
    staged bytes exactly as it does for any other uploaded document.
    """
    response = _upload(vault, "171234567.on-attention.html", _post_html("On Attention"))

    post = _by_body(vault, "On Attention")
    staged = vault / post.metadata["source"]["origin_key"]
    assert staged.is_file()
    assert staged.parent == vault / "00-Creek-Meta/adepthood/uploads"
    assert response["affected_fragment_ids"] == [post.metadata["id"]]


def test_the_manifest_is_audited_and_writes_no_fragment(vault: Path) -> None:
    """The manifest call is audited, and nothing is staged or written."""
    _upload(vault, "posts.csv", _manifest_csv())

    assert _fragments(vault) == []
    staging = vault / "00-Creek-Meta/adepthood/uploads"
    assert not staging.exists() or not any(staging.iterdir())
    entries = [
        json.loads(line)
        for line in (vault / MCP_AUDIT_RELPATH).read_text().splitlines()
        if line.strip()
    ]
    assert entries[-1]["tool"] == "creek.upload"
    assert entries[-1]["affected_fragment_ids"] == []


# --------------------------------------------------------------------------- #
# Detection is by content as well as by name
# --------------------------------------------------------------------------- #


def test_a_post_shaped_name_without_substack_markup_stays_a_document(
    vault: Path,
) -> None:
    """The issue's own example: the owner's ``2024.notes.html`` is a document."""
    response = _upload(
        vault, "2024.notes.html", b"<html><body><p>Notes to self.</p></body></html>"
    )

    assert response["source_type"] == "document"
    (fragment,) = _fragments(vault)
    assert frontmatter.load(fragment).metadata["source"]["platform"] == "other"


def test_substack_markup_under_a_plain_name_stays_a_document(vault: Path) -> None:
    """Content alone is not enough either: the name must be post-shaped."""
    response = _upload(vault, "saved-page.html", _post_html("Saved"))

    assert response["source_type"] == "document"


def test_a_text_only_post_listed_in_the_manifest_is_substack(vault: Path) -> None:
    """A post with no editor markup is recognised by the manifest listing it."""
    _upload(vault, "posts.csv", _manifest_csv())
    response = _upload(
        vault, "171234569.plain-words.html", _post_html("Plain Words", markup=False)
    )

    assert response["source_type"] == "substack"
    assert _relative_dirs(vault) == {"Writing/Substack"}


def test_a_text_only_post_with_no_manifest_stays_a_document(vault: Path) -> None:
    """Without markup or a manifest entry there is no Substack evidence."""
    response = _upload(
        vault, "171234569.plain-words.html", _post_html("Plain Words", markup=False)
    )

    assert response["source_type"] == "document"


def test_a_posts_csv_without_substacks_columns_stays_a_spreadsheet(
    vault: Path,
) -> None:
    """Only Substack's header makes ``posts.csv`` a manifest."""
    response = _upload(vault, "posts.csv", b"name,count\nbread,2\n")

    assert response["status"] == "ok"
    assert response["source_type"] == "spreadsheet"


# --------------------------------------------------------------------------- #
# The manifest never carries content above the tier it arrived at
# --------------------------------------------------------------------------- #


def test_a_manifest_above_the_posts_tier_is_not_joined(vault: Path) -> None:
    """A ``personal`` manifest's titles and dates never enter an ``open`` post."""
    _upload(vault, "posts.csv", _manifest_csv(), tier="personal")
    _upload(
        vault,
        "171234567.on-attention.html",
        _post_html("On Attention"),
        tier="open",
        ceiling=TierCeiling.OPEN,
    )

    post = _by_body(vault, "On Attention")
    assert post.metadata["title"] == "On attention"
    assert post.metadata.get("authored_at") is None


def test_a_manifest_above_the_posts_tier_is_no_detection_evidence(
    vault: Path,
) -> None:
    """A text-only ``open`` post is not recognised through a ``personal`` list."""
    _upload(vault, "posts.csv", _manifest_csv(), tier="personal")
    response = _upload(
        vault,
        "171234569.plain-words.html",
        _post_html("Plain Words", markup=False),
        tier="open",
        ceiling=TierCeiling.OPEN,
    )

    assert response["source_type"] == "document"


# --------------------------------------------------------------------------- #
# The detection and manifest helpers, directly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("filename", "payload", "expected"),
    [
        ("posts.csv", _HEADER.encode(), True),
        ("POSTS.CSV", _HEADER.encode(), True),
        ("posts.csv", "﻿".encode() + _HEADER.encode(), True),
        ("posts.csv", b"post_id,title\n1,x\n", False),
        ("posts.csv", b"", False),
        ("posts.csv", b"\xff\xfe\x00broken", False),
        ("other.csv", _HEADER.encode(), False),
    ],
)
def test_is_substack_manifest(filename: str, payload: bytes, expected: bool) -> None:
    """The manifest is ``posts.csv`` by name AND Substack's columns by header."""
    assert is_substack_manifest(filename, payload) is expected


def test_parse_manifest_rows_keys_by_leading_id_and_drops_unjoinable_rows() -> None:
    """Rows join on the numeric id; rows without one are useless and dropped."""
    payload = _HEADER.encode() + b",2024-01-01,true,,,newsletter,everyone,No id,,\n"
    payload += b"999.slug,2024-02-02,true,,,newsletter,everyone,Kept,,\n"

    rows = parse_manifest_rows(payload)

    assert list(rows) == ["999"]
    assert rows["999"]["title"] == "Kept"
    assert rows["999"]["post_date"] == "2024-02-02"


def test_record_manifest_merges_and_round_trips(vault: Path) -> None:
    """A second manifest adds to, and overrides, what the first recorded."""
    record_manifest(vault, {"1": {"title": "One"}}, PrivacyTier.OPEN)
    record_manifest(vault, {"2": {"title": "Two"}}, PrivacyTier.PERSONAL)
    record_manifest(vault, {"1": {"title": "Uno"}}, PrivacyTier.PERSONAL)

    assert load_manifest(vault) == {
        "1": ManifestEntry(tier=PrivacyTier.PERSONAL, row={"title": "Uno"}),
        "2": ManifestEntry(tier=PrivacyTier.PERSONAL, row={"title": "Two"}),
    }
    assert (vault / SUBSTACK_MANIFEST_RELPATH).is_file()


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        "[]",
        '{"posts": []}',
        '{"posts": {"1": "x"}}',
        '{"posts": {"1": {"tier": "nope", "row": {}}}}',
        '{"posts": {"1": {"tier": 1, "row": {}}}}',
        '{"posts": {"1": {"tier": "open", "row": []}}}',
    ],
)
def test_an_unreadable_manifest_store_reads_as_empty(vault: Path, stored: str) -> None:
    """A corrupt store is no evidence at all, never an exception."""
    path = vault / SUBSTACK_MANIFEST_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stored, encoding="utf-8")

    assert load_manifest(vault) == {}


def test_load_manifest_of_a_vault_with_none_is_empty(vault: Path) -> None:
    """No manifest has arrived yet."""
    assert load_manifest(vault) == {}


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("171234567.on-attention.html", ("171234567", "on-attention")),
        ("171234567.on-attention.htm", ("171234567", "on-attention")),
        ("171234567.On-Attention.HTML", ("171234567", "On-Attention")),
        ("on-attention.html", None),
        ("171234567.html", None),
        ("171234567.on_attention.html", None),
        ("171234567.on-attention.html.txt", None),
    ],
)
def test_substack_post_identity_needs_a_post_shaped_name(
    filename: str, expected: tuple[str, str] | None
) -> None:
    """``<post_id>.<slug>.html`` only; the issue's filename rule, verbatim."""
    found = substack_post_identity(filename, _post_html("x"), {}, PrivacyTier.OPEN)

    assert (None if found is None else (found.post_id, found.slug)) == expected


def test_single_post_ingestor_discovers_nothing_for_a_missing_file(
    tmp_path: Path,
) -> None:
    """The single-post ingestor reads exactly one existing file."""
    ingestor = single_post_ingestor(SubstackPost(post_id="1", slug="x"))()

    assert ingestor.discover(tmp_path / "absent.html") == []
    assert ingestor.discover(tmp_path) == []


# --------------------------------------------------------------------------- #
# Upload receipts for GET /v1/pipeline/status (#2124)
# --------------------------------------------------------------------------- #


def test_each_loose_post_is_receipted_with_its_fragment(vault: Path) -> None:
    """A loose post lands as a fragment, so the status route can name it."""
    _upload(vault, "posts.csv", _manifest_csv())
    posts = _upload_posts(vault)

    receipts = read_upload_receipts(vault)

    assert [r.fragment_id for r in receipts] == [
        fragment_id for post in posts for fragment_id in post["affected_fragment_ids"]
    ]
    assert [r.filename for r in receipts] == [
        f"{post_id}.{slug}.html" for post_id, slug, _title, _date in _POSTS
    ]


def test_a_manifest_is_never_receipted(vault: Path) -> None:
    """``posts.csv`` writes no fragment, so it is no document on the status route."""
    manifest = _upload(vault, "posts.csv", _manifest_csv())

    assert manifest["status"] == MANIFEST_STATUS
    assert read_upload_receipts(vault) == []


# --------------------------------------------------------------------------- #
# Subscriber lists are refused, never ingested (#2133)
# --------------------------------------------------------------------------- #

_SUBSCRIBER_EMAILS: Final[tuple[str, ...]] = (
    "reader.one@subscribers.example",
    "reader.two@subscribers.example",
)
"""Third parties' addresses in every subscriber fixture; none may reach disk."""

_SUBSCRIBER_FILES: Final[tuple[str, ...]] = (
    "email_list.csv",
    "email_list.my-publication.csv",
    "171234567.on-attention.delivers.csv",
    "171234567.on-attention.opens.csv",
    "posts/171234567.on-attention.opens.csv",
    "EMAIL_LIST.CSV",
)
"""Each subscriber-file shape a Substack export holds, as a folder import names it."""


def _subscriber_csv() -> bytes:
    """Return a subscriber CSV naming every address in :data:`_SUBSCRIBER_EMAILS`.

    Returns:
        The CSV bytes.
    """
    rows = "".join(
        f"{email},2024-03-15T08:30:00.000Z,true\n" for email in _SUBSCRIBER_EMAILS
    )
    return ("email,timestamp,active_subscription\n" + rows).encode()


def _files_naming_a_subscriber(vault: Path) -> list[str]:
    """Return every file under *vault* whose bytes hold a subscriber address.

    Args:
        vault: Vault root.

    Returns:
        Vault-relative paths, empty when no address was written anywhere.
    """
    needles = [email.encode() for email in _SUBSCRIBER_EMAILS]
    return sorted(
        path.relative_to(vault).as_posix()
        for path in vault.rglob("*")
        if path.is_file() and any(needle in path.read_bytes() for needle in needles)
    )


@pytest.mark.parametrize("filename", _SUBSCRIBER_FILES)
def test_a_loose_subscriber_csv_is_refused_and_nothing_is_written(
    vault: Path, filename: str
) -> None:
    """Every subscriber-file shape is refused: no fragment, no address on disk.

    **The RED this was written against**: each of these was read by the
    spreadsheet ingestor and written to ``01-Fragments/Data`` with the
    subscribers' addresses in its body and its staged copy.
    """
    response = _upload(vault, filename, _subscriber_csv())

    assert response["status"] == "refused"
    assert response["reason"] == SUBSCRIBER_DATA_REASON
    assert _fragments(vault) == []
    assert _files_naming_a_subscriber(vault) == []
    staging = vault / "00-Creek-Meta/adepthood/uploads"
    assert not staging.exists() or not any(staging.iterdir())
    assert read_upload_receipts(vault) == []


def test_a_subscriber_refusal_is_audited_without_content(vault: Path) -> None:
    """The refusal is audited, and names neither the file nor its rows back."""
    response = _upload(vault, "171234567.on-attention.opens.csv", _subscriber_csv())

    assert set(response) == {"status", "tool", "tier_ceiling", "reason"}
    assert "on-attention" not in response["reason"]
    entries = [
        json.loads(line)
        for line in (vault / MCP_AUDIT_RELPATH).read_text().splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["tool"] == "creek.upload"
    assert "affected_fragment_ids" not in entries[0] or not entries[0].get(
        "affected_fragment_ids"
    )
    assert "created_path" not in entries[0] or entries[0]["created_path"] is None


def test_a_whole_loose_export_keeps_its_posts_and_drops_its_subscribers(
    vault: Path,
) -> None:
    """Sent file by file, the export lands as a zipped one does: posts only."""
    _upload(vault, "posts.csv", _manifest_csv())
    _upload_posts(vault)
    for filename in _SUBSCRIBER_FILES:
        _upload(vault, filename, _subscriber_csv())

    assert len(_fragments(vault)) == len(_POSTS)
    assert _relative_dirs(vault) == {"Writing/Substack"}
    assert _files_naming_a_subscriber(vault) == []


def test_an_ordinary_csv_is_still_a_spreadsheet(vault: Path) -> None:
    """A CSV matching no subscriber filter is ingested exactly as before."""
    response = _upload(vault, "budget.csv", b"item,cost\nink,12\n")

    assert response["status"] == "ok"
    assert response["source_type"] == "spreadsheet"
