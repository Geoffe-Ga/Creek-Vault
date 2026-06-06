"""Tests for the citation-integrity tells (FEAT-040.11).

Voice-neutral factual-integrity detectors: invalid ISBN/DOI, undefined named
references, AI-provenance tracking params, page-less book citations, and an
opt-in, offline-safe live-link checker. Detection only — never rewrites or
deletes a citation.
"""

from __future__ import annotations

from creek.generate.ai_style.citations import (
    check_external_links,
    extract_urls,
)
from creek.generate.ai_style.tells import TELL_REGISTRY


def _fire(tell_id: str, text: str) -> int:
    """Return the number of findings *tell_id* locates in *text*."""
    return len(TELL_REGISTRY[tell_id].locate(text))


# --- ISBN -------------------------------------------------------------------


def test_valid_isbn13_does_not_fire() -> None:
    """A checksum-valid ISBN-13 is not flagged."""
    # 9780306406157 is a canonical valid ISBN-13.
    assert _fire("invalid_isbn", "See ISBN 978-0-306-40615-7 for details.") == 0


def test_valid_isbn10_does_not_fire() -> None:
    """A checksum-valid ISBN-10 (incl. an X check digit) is not flagged."""
    # 0-306-40615-2 is the matching valid ISBN-10.
    assert _fire("invalid_isbn", "ISBN 0-306-40615-2 in the bibliography.") == 0


def test_invalid_isbn_checksum_fires() -> None:
    """An ISBN whose checksum fails is flagged as likely fabricated."""
    assert _fire("invalid_isbn", "ISBN 978-0-306-40615-0 is bogus.") == 1


def test_invalid_isbn_rate_is_positive() -> None:
    """The measure returns a positive rate when an invalid ISBN is present."""
    assert TELL_REGISTRY["invalid_isbn"].measure("ISBN 1234567890 here.") > 0.0


# --- DOI --------------------------------------------------------------------


def test_valid_doi_does_not_fire() -> None:
    """A syntactically valid DOI reference is not flagged."""
    assert _fire("invalid_doi", "doi:10.1000/xyz123 and more text") == 0
    assert _fire("invalid_doi", "https://doi.org/10.1038/nature12373 ok") == 0


def test_malformed_doi_fires() -> None:
    """A doi: reference that is not a valid DOI is flagged."""
    assert _fire("invalid_doi", "doi: not-a-real-doi here") == 1


# --- Undefined references ---------------------------------------------------


def test_defined_ref_does_not_fire() -> None:
    """A self-closing ref whose name is defined is not flagged."""
    text = '<ref name="a">Source A</ref> ... later <ref name="a"/>'
    assert _fire("undefined_ref", text) == 0


def test_undefined_ref_fires() -> None:
    """A self-closing ref naming an undefined reference is flagged."""
    assert _fire("undefined_ref", 'text <ref name="ghost"/> more') == 1


def test_literal_cite_error_fires() -> None:
    """Literal ``Cite error:`` text is flagged."""
    assert _fire("undefined_ref", "Cite error: ref 'x' is not used") == 1


# --- Tracking-param provenance ----------------------------------------------


def test_chatgpt_tracking_param_fires() -> None:
    """A utm_source=chatgpt.com provenance param is flagged."""
    assert (
        _fire("tracking_param_provenance", "https://x.com/a?utm_source=chatgpt.com")
        == 1
    )


def test_clean_url_does_not_fire() -> None:
    """A URL with no AI-provenance tracking param is not flagged."""
    assert (
        _fire("tracking_param_provenance", "https://x.com/a?utm_source=newsletter") == 0
    )


# --- Page-less book citations -----------------------------------------------


def test_pageless_book_fires() -> None:
    """A book citation with an ISBN but no page locator is flagged."""
    assert (
        _fire("pageless_book_cite", "Smith, A Big Book, ISBN 978-0-306-40615-7.") == 1
    )


def test_book_with_page_does_not_fire() -> None:
    """A book citation carrying a page locator is not flagged."""
    text = "Smith, A Big Book, ISBN 978-0-306-40615-7, p. 42."
    assert _fire("pageless_book_cite", text) == 0


# --- Registration -----------------------------------------------------------


def test_all_citation_tells_registered_in_category() -> None:
    """Every citation detector registers under the ``citation`` category."""
    expected = {
        "invalid_isbn",
        "invalid_doi",
        "undefined_ref",
        "tracking_param_provenance",
        "pageless_book_cite",
    }
    for tell_id in expected:
        assert TELL_REGISTRY[tell_id].category == "citation"
        assert TELL_REGISTRY[tell_id].handling == "surface"
        # Integrity tells never rewrite/delete — margin 0 fires on any hit.
        assert TELL_REGISTRY[tell_id].margin == 0.0


# --- Opt-in live-link checking ----------------------------------------------


def test_link_check_disabled_makes_no_calls() -> None:
    """Offline mode (enabled=False) returns no broken links."""
    text = "See https://dead.example.com/x for more."
    assert (
        check_external_links(
            text, enabled=False, checker=[("https://dead.example.com/x", False)]
        )
        == []
    )


def test_link_check_reports_broken_when_enabled() -> None:
    """With a resolver wired, broken links are reported."""
    text = "Good https://ok.example.com and bad https://dead.example.com/x ."
    broken = check_external_links(
        text,
        enabled=True,
        checker=[
            ("https://ok.example.com", True),
            ("https://dead.example.com/x", False),
        ],
    )
    assert broken == ["https://dead.example.com/x"]


def test_link_check_skips_false_positive_hosts() -> None:
    """Paywall / low-PMID hosts are never reported even if the probe fails."""
    text = "Paywalled https://www.jstor.org/stable/123 reference."
    broken = check_external_links(
        text,
        enabled=True,
        checker=[("https://www.jstor.org/stable/123", False)],
    )
    assert broken == []


def test_extract_urls_strips_trailing_punctuation() -> None:
    """URL extraction trims a trailing period from a sentence-final link."""
    assert extract_urls("Visit https://example.com/page.") == [
        "https://example.com/page"
    ]


def test_citation_findings_surface_only_when_category_enabled() -> None:
    """Citation tells are off the default voice scan but opt-in via the category."""
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
    from creek.generate.ai_style.scanner import scan

    fingerprint = VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=0.0, support=20)},
        fragment_count=20,
    )
    text = "A claim with ISBN 978-0-306-40615-0 (bad checksum)."

    default = scan(text, fingerprint=fingerprint, config=AIStyleConfig())
    assert not any(f.category == "citation" for f in default.findings)

    opt_in = scan(
        text,
        fingerprint=fingerprint,
        config=AIStyleConfig(
            enabled_categories=["lexical", "rhetorical", "discourse", "citation"],
        ),
    )
    assert any(f.tell_id == "invalid_isbn" for f in opt_in.findings)
