"""Tests for the FEAT-031 HTML authored-at extraction chain (#1176).

:mod:`creek.ingest.html` is the shared date-extraction chain for every
HTML-emitting ingestor (documents, substack, refresh). Its failure arms
— empty JSON-LD blocks, malformed JSON, list payloads holding non-dict
entries, blank ``datePublished`` values — are the whole point of the
module: FEAT-031 forbids guessing a date, so every one of them must
fall through to the next candidate and ultimately return ``None``.

The scalar-payload cases pin a real defect: ``list(parsed)`` raised
``TypeError`` for a JSON-LD body of ``null`` / ``5`` / ``true``, and the
broad ``except Exception`` in :meth:`creek.ingest.base.Ingestor._parse_safe`
turned that into a *silently dropped document* — the entire page's
content lost because one script tag held a scalar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.ingest.documents import DocumentIngestor
from creek.ingest.html import (
    _collect_json_ld_date,
    _collect_meta_dates,
    extract_html_authored_at,
)

if TYPE_CHECKING:
    from pathlib import Path


def _json_ld(body: str) -> str:
    """Wrap *body* in an ``application/ld+json`` script tag."""
    return f'<script type="application/ld+json">{body}</script>'


class TestCollectMetaDates:
    """The ``<meta>`` sweep keeps only tags with a real name *and* value."""

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            pytest.param(
                '<meta name="date" content="2024-03-15">',
                {"date": "2024-03-15"},
                id="name-and-content",
            ),
            pytest.param(
                '<meta name="  " content="2024-03-15">',
                {},
                id="whitespace-only-name-is-dropped",
            ),
            pytest.param(
                '<meta name="date" content="   ">',
                {},
                id="whitespace-only-content-is-dropped",
            ),
            pytest.param(
                '<meta content="2024-03-15" property="article:published_time">',
                {"article:published_time": "2024-03-15"},
                id="reversed-attribute-order",
            ),
            pytest.param(
                '<meta name="DATE" content="2024-03-15">',
                {"date": "2024-03-15"},
                id="name-is-lowercased",
            ),
        ],
    )
    def test_meta_sweep(self, html: str, expected: dict[str, str]) -> None:
        """Only tags carrying both a non-blank name and value survive."""
        assert _collect_meta_dates(html) == expected

    def test_blank_tag_does_not_shadow_a_real_one(self) -> None:
        """A blank-content tag must not mask a later real value.

        This is the ``99->96`` arm: the loop body runs, the guard
        rejects the blank pair, and iteration continues to the next
        match rather than recording an empty string.
        """
        html = (
            '<meta name="date" content="   "><meta name="pubdate" content="2024-03-15">'
        )
        assert _collect_meta_dates(html) == {"pubdate": "2024-03-15"}

    def test_later_duplicate_wins(self) -> None:
        """A repeated key takes its last value, as a browser would."""
        html = (
            '<meta name="date" content="2024-01-01">'
            '<meta name="date" content="2024-02-02">'
        )
        assert _collect_meta_dates(html) == {"date": "2024-02-02"}


class TestCollectJsonLdDate:
    """Every malformed-payload arm falls through instead of raising."""

    def test_empty_script_body_is_skipped(self) -> None:
        """A blank ``<script>`` body is skipped before ``json.loads``."""
        assert _collect_json_ld_date(_json_ld("   ")) is None

    def test_empty_body_does_not_shadow_a_later_script(self) -> None:
        """The blank-body ``continue`` still reaches the next script."""
        html = _json_ld("") + _json_ld('{"datePublished": "2024-03-15"}')
        assert _collect_json_ld_date(html) == "2024-03-15"

    def test_malformed_json_is_skipped(self) -> None:
        """An unparseable body is swallowed, not propagated."""
        assert _collect_json_ld_date(_json_ld("{not json,")) is None

    def test_malformed_json_does_not_shadow_a_later_script(self) -> None:
        """The ``except`` arm continues to the next script tag."""
        html = _json_ld("{oops") + _json_ld('{"datePublished": "2024-03-15"}')
        assert _collect_json_ld_date(html) == "2024-03-15"

    def test_non_dict_entry_in_list_is_skipped(self) -> None:
        """A list holding junk before a real object still resolves."""
        html = _json_ld('["a string", 42, null, {"datePublished": "2024-03-15"}]')
        assert _collect_json_ld_date(html) == "2024-03-15"

    @pytest.mark.parametrize(
        "bad_value",
        [
            pytest.param('""', id="empty-string"),
            pytest.param('"   "', id="whitespace-only"),
            pytest.param("null", id="null"),
            pytest.param("12345", id="non-string"),
            pytest.param('{"@value": "2024-01-01"}', id="nested-object"),
        ],
    )
    def test_unusable_date_value_falls_through_to_next_entry(
        self, bad_value: str
    ) -> None:
        """A dict whose date is blank or non-string is not accepted."""
        html = _json_ld(
            f'[{{"datePublished": {bad_value}}}, {{"datePublished": "2024-03-15"}}]'
        )
        assert _collect_json_ld_date(html) == "2024-03-15"

    def test_exhausted_list_falls_through_to_next_script(self) -> None:
        """A fully barren first script hands off to the second one."""
        html = _json_ld('[{"name": "no date here"}, "junk"]') + _json_ld(
            '{"datePublished": "2024-03-15"}'
        )
        assert _collect_json_ld_date(html) == "2024-03-15"

    def test_date_published_preferred_over_date_created(self) -> None:
        """``datePublished`` wins when both keys are present."""
        html = _json_ld('{"datePublished": "2024-03-15", "dateCreated": "2020-01-01"}')
        assert _collect_json_ld_date(html) == "2024-03-15"

    def test_date_created_used_when_date_published_absent(self) -> None:
        """``dateCreated`` is the documented second choice."""
        html = _json_ld('{"dateCreated": "2020-01-01"}')
        assert _collect_json_ld_date(html) == "2020-01-01"

    def test_value_is_stripped(self) -> None:
        """Surrounding whitespace is trimmed off the returned string."""
        html = _json_ld('{"datePublished": "  2024-03-15  "}')
        assert _collect_json_ld_date(html) == "2024-03-15"

    @pytest.mark.parametrize(
        "scalar",
        [
            pytest.param("null", id="null"),
            pytest.param("5", id="int"),
            pytest.param("1.5", id="float"),
            pytest.param("true", id="bool"),
            pytest.param('"just a string"', id="string"),
        ],
    )
    def test_scalar_payload_yields_none_instead_of_raising(self, scalar: str) -> None:
        """A scalar JSON-LD body is unusable, not a crash.

        ``list(parsed)`` used to raise ``TypeError`` here, which the
        ingestor's broad ``except Exception`` converted into a dropped
        document.
        """
        assert _collect_json_ld_date(_json_ld(scalar)) is None

    def test_scalar_payload_does_not_shadow_a_later_script(self) -> None:
        """A scalar first script must not cost us the real date."""
        html = _json_ld("null") + _json_ld('{"datePublished": "2024-03-15"}')
        assert _collect_json_ld_date(html) == "2024-03-15"


class TestExtractHtmlAuthoredAt:
    """The full chain prefers meta keys in order, then JSON-LD."""

    def test_article_published_time_wins_over_generic_date(self) -> None:
        """``article:published_time`` outranks a bare ``date`` tag."""
        html = (
            '<meta name="date" content="2020-01-01">'
            '<meta property="article:published_time" content="2024-03-15">'
        )
        assert extract_html_authored_at(html) == "2024-03-15"

    def test_dublin_core_wins_over_pubdate(self) -> None:
        """``DC.date.issued`` outranks the lower-fidelity ``pubdate``."""
        html = (
            '<meta name="pubdate" content="2020-01-01">'
            '<meta name="DC.date.issued" content="2024-03-15">'
        )
        assert extract_html_authored_at(html) == "2024-03-15"

    def test_falls_through_to_json_ld_when_no_meta_matches(self) -> None:
        """JSON-LD is the last resort in the documented chain."""
        html = '<meta name="author" content="Someone">' + _json_ld(
            '{"datePublished": "2024-03-15"}'
        )
        assert extract_html_authored_at(html) == "2024-03-15"

    def test_returns_none_when_nothing_is_extractable(self) -> None:
        """No date anywhere → ``None``; FEAT-031 forbids guessing."""
        assert extract_html_authored_at("<html><body>hi</body></html>") is None

    def test_scalar_json_ld_returns_none(self) -> None:
        """The scalar defect must not escape through the public entry point."""
        assert extract_html_authored_at(_json_ld("null")) is None


class TestScalarJsonLdEndToEnd:
    """A scalar JSON-LD block must not cost the reader the document."""

    @pytest.mark.parametrize(
        "scalar",
        [
            pytest.param("null", id="null"),
            pytest.param("5", id="int"),
            pytest.param("true", id="bool"),
        ],
    )
    def test_page_still_ingests_with_content(self, tmp_path: Path, scalar: str) -> None:
        """The page yields one fragment with its body and no guessed date."""
        page = tmp_path / "post.html"
        page.write_text(
            "<html><body><h1>Real Title</h1><p>Body worth keeping.</p>"
            f"{_json_ld(scalar)}</body></html>",
            encoding="utf-8",
        )
        ingestor = DocumentIngestor()
        raws = ingestor.discover(page)
        assert len(raws) == 1

        fragments = ingestor.parse(raws[0])

        assert len(fragments) == 1
        assert "Body worth keeping." in fragments[0].content
        assert fragments[0].metadata["authored_at"] is None

    def test_scalar_block_does_not_hide_a_real_date(self, tmp_path: Path) -> None:
        """A good second block still supplies ``authored_at``."""
        page = tmp_path / "post.html"
        page.write_text(
            "<html><body><p>Body.</p>"
            + _json_ld("null")
            + _json_ld('{"datePublished": "2024-03-15T08:30:00+00:00"}')
            + "</body></html>",
            encoding="utf-8",
        )
        ingestor = DocumentIngestor()
        fragments = ingestor.parse(ingestor.discover(page)[0])

        authored = fragments[0].metadata["authored_at"]
        assert authored is not None
        assert authored.year == 2024
        assert authored.month == 3
        assert authored.day == 15
