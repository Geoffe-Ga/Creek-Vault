"""Citation-integrity detectors (FEAT-040.11, category ``citation``).

These tells are **voice-neutral**: they target factual-integrity failures the
field guide's Citations section (WP:AIFICTREF) names — fabricated or broken
references — rather than idiolect. They catch the most policy-dangerous LLM
failure mode (hallucinated sources): invalid ISBN/DOI checksums, references to
undefined footnotes, ``utm_source=chatgpt.com`` provenance tracking params, and
book citations missing a page locator.

Everything here is **detection only** — a fired tell surfaces a finding; it
never rewrites or deletes a citation. All detectors are offline and
deterministic. Live link-checking is opt-in and lives in
:func:`check_external_links`, gated by config so the default path makes no
network calls.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from creek.generate.ai_style.model import Span
from creek.generate.ai_style.tells import Tell, rate_per_kwords, register

if TYPE_CHECKING:
    from collections.abc import Iterable

# --- ISBN -------------------------------------------------------------------

_ISBN_CANDIDATE_RE = re.compile(
    r"\bISBN(?:-1[03])?:?\s*([0-9][0-9Xx\- ]{8,16}[0-9Xx])",
)


def _normalise_isbn(raw: str) -> str:
    """Strip hyphens and spaces from a candidate ISBN string."""
    return re.sub(r"[\- ]", "", raw)


def _is_valid_isbn10(digits: str) -> bool:
    """Return whether *digits* (length 10) is a checksum-valid ISBN-10."""
    if len(digits) != 10:
        return False
    total = 0
    for index, char in enumerate(digits):
        if char in "Xx" and index == 9:
            value = 10
        elif char.isdigit():
            value = int(char)
        else:
            return False
        total += value * (10 - index)
    return total % 11 == 0


def _is_valid_isbn13(digits: str) -> bool:
    """Return whether *digits* (length 13) is a checksum-valid ISBN-13."""
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum(
        int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(digits)
    )
    return total % 10 == 0


def _is_valid_isbn(raw: str) -> bool:
    """Return whether a normalised ISBN candidate passes its checksum."""
    digits = _normalise_isbn(raw)
    if len(digits) == 10:
        return _is_valid_isbn10(digits)
    if len(digits) == 13:
        return _is_valid_isbn13(digits)
    return False


def _locate_invalid_isbn(text: str) -> list[Span]:
    """Return spans of ISBNs whose checksum does not validate."""
    return [
        Span(match.start(), match.end())
        for match in _ISBN_CANDIDATE_RE.finditer(text)
        if not _is_valid_isbn(match.group(1))
    ]


def _measure_invalid_isbn(text: str) -> float:
    """Return the per-1000-word rate of checksum-invalid ISBNs."""
    return rate_per_kwords(len(_locate_invalid_isbn(text)), text)


# --- DOI --------------------------------------------------------------------

# A reference that announces itself as a DOI (``doi:`` prefix or a doi.org URL)
# but whose body is not a syntactically valid DOI (``10.<registrant>/<suffix>``).
_DOI_REFERENCE_RE = re.compile(
    r"(?:doi:\s*|https?://(?:dx\.)?doi\.org/)(\S+)",
    re.IGNORECASE,
)
_VALID_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def _locate_invalid_doi(text: str) -> list[Span]:
    """Return spans of DOI references whose syntax is malformed."""
    spans: list[Span] = []
    for match in _DOI_REFERENCE_RE.finditer(text):
        body = match.group(1).rstrip(".,;)")
        if not _VALID_DOI_RE.match(body):
            spans.append(Span(match.start(), match.end()))
    return spans


def _measure_invalid_doi(text: str) -> float:
    """Return the per-1000-word rate of malformed DOI references."""
    return rate_per_kwords(len(_locate_invalid_doi(text)), text)


# --- Undefined / unused named references ------------------------------------

_REF_DEFINITION_RE = re.compile(r"""<ref\s+name\s*=\s*["']([^"']+)["']\s*>""")
_REF_USE_RE = re.compile(r"""<ref\s+name\s*=\s*["']([^"']+)["']\s*/>""")
_CITE_ERROR_RE = re.compile(r"Cite error:[^\n]*", re.IGNORECASE)


def _locate_undefined_ref(text: str) -> list[Span]:
    """Return spans of self-closing ref uses naming an undefined ref.

    A ``<ref name="x"/>`` use whose name is never defined by a
    ``<ref name="x">…</ref>`` is the ``Cite error: … is not used`` family —
    a hallucinated footnote. Literal ``Cite error:`` text is flagged too.
    """
    defined = {m.group(1) for m in _REF_DEFINITION_RE.finditer(text)}
    spans = [
        Span(m.start(), m.end())
        for m in _REF_USE_RE.finditer(text)
        if m.group(1) not in defined
    ]
    spans.extend(Span(m.start(), m.end()) for m in _CITE_ERROR_RE.finditer(text))
    return spans


def _measure_undefined_ref(text: str) -> float:
    """Return the per-1000-word rate of undefined-reference uses."""
    return rate_per_kwords(len(_locate_undefined_ref(text)), text)


# --- Tracking-parameter provenance ------------------------------------------

# ``utm_source=chatgpt.com`` (and the openai variant) left on a pasted URL is a
# strong AI-involvement signal. Detection only — stripping lives in ingestion.
_TRACKING_PARAM_RE = re.compile(
    r"utm_source=(?:chatgpt\.com|openai\.com|chat\.openai\.com)",
    re.IGNORECASE,
)


def _locate_tracking_param(text: str) -> list[Span]:
    """Return spans of AI-provenance tracking parameters."""
    return [Span(m.start(), m.end()) for m in _TRACKING_PARAM_RE.finditer(text)]


def _measure_tracking_param(text: str) -> float:
    """Return the per-1000-word rate of AI-provenance tracking params."""
    return rate_per_kwords(len(_locate_tracking_param(text)), text)


# --- Page-less book citations -----------------------------------------------

_PAGE_LOCATOR_RE = re.compile(r"\bp{1,2}\.?\s*\d|\bpages?\b", re.IGNORECASE)


def _locate_pageless_book(text: str) -> list[Span]:
    """Return spans of book citations (ISBN-bearing lines) lacking a page.

    A line carrying a valid ISBN but no page locator (``p.``/``pp.``/``page``)
    is a general-topic book citation with no verifiable locator. Surfaced, not
    failed: some citations legitimately omit pages.
    """
    spans: list[Span] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if _ISBN_CANDIDATE_RE.search(line) and not _PAGE_LOCATOR_RE.search(line):
            stripped = line.rstrip("\n")
            spans.append(Span(offset, offset + len(stripped)))
        offset += len(line)
    return spans


def _measure_pageless_book(text: str) -> float:
    """Return the per-1000-word rate of page-less book citations."""
    return rate_per_kwords(len(_locate_pageless_book(text)), text)


# --- Tell registration ------------------------------------------------------

INVALID_ISBN = register(
    Tell(
        id="invalid_isbn",
        category="citation",
        feature_key="invalid_isbn_rate",
        handling="surface",
        polarity="avoid",
        description="ISBN whose checksum does not validate (likely fabricated).",
        caveat="OCR'd or hand-typed ISBNs can carry a transposition typo; verify "
        "against the book before assuming fabrication.",
        measure=_measure_invalid_isbn,
        locate=_locate_invalid_isbn,
        generic_prior=0.0,
        margin=0.0,
    ),
)

INVALID_DOI = register(
    Tell(
        id="invalid_doi",
        category="citation",
        feature_key="invalid_doi_rate",
        handling="surface",
        polarity="avoid",
        description="A doi: / doi.org reference whose syntax is not a valid DOI.",
        caveat="Live resolution is opt-in; a syntactically valid DOI can still be "
        "dead or point at an unrelated article — format validity is necessary, "
        "not sufficient.",
        measure=_measure_invalid_doi,
        locate=_locate_invalid_doi,
        generic_prior=0.0,
        margin=0.0,
    ),
)

UNDEFINED_REF = register(
    Tell(
        id="undefined_ref",
        category="citation",
        feature_key="undefined_ref_rate",
        handling="surface",
        polarity="avoid",
        description="A named <ref/> use whose definition is missing (Cite error).",
        caveat="A ref defined later in a longer document the scanner only saw a "
        "slice of can false-positive; check the whole source.",
        measure=_measure_undefined_ref,
        locate=_locate_undefined_ref,
        generic_prior=0.0,
        margin=0.0,
    ),
)

TRACKING_PARAM = register(
    Tell(
        id="tracking_param_provenance",
        category="citation",
        feature_key="tracking_param_rate",
        handling="surface",
        polarity="avoid",
        description="A URL carrying utm_source=chatgpt.com / openai provenance.",
        caveat="A signal of AI involvement in sourcing, not proof the content is "
        "wrong; strip the param (handled at ingestion) rather than the citation.",
        measure=_measure_tracking_param,
        locate=_locate_tracking_param,
        generic_prior=0.0,
        margin=0.0,
    ),
)

PAGELESS_BOOK = register(
    Tell(
        id="pageless_book_cite",
        category="citation",
        feature_key="pageless_book_rate",
        handling="surface",
        polarity="avoid",
        description="A book citation (ISBN present) with no page locator.",
        caveat="Many legitimate citations of a whole work omit a page; surface "
        "for review, never auto-fail.",
        measure=_measure_pageless_book,
        locate=_locate_pageless_book,
        generic_prior=0.0,
        margin=0.0,
    ),
)


# --- Opt-in live link checking ----------------------------------------------

_URL_RE = re.compile(r"https?://[^\s)>\]]+")
# The 2018-2023 VisualEditor low-PMID artefact and paywalled/library hosts are
# field-guide false positives: a dead-looking link there is not AI fabrication.
_LINK_CHECK_SKIP_HOSTS: frozenset[str] = frozenset(
    {"jstor.org", "sci-hub", "libgen", "doi.org", "ncbi.nlm.nih.gov"},
)


def _should_skip_link(url: str) -> bool:
    """Return whether *url* is a documented false-positive host (skip it)."""
    return any(host in url for host in _LINK_CHECK_SKIP_HOSTS)


def extract_urls(text: str) -> list[str]:
    """Return the outbound HTTP(S) URLs in *text*, in order."""
    return [m.group(0).rstrip(".,;)") for m in _URL_RE.finditer(text)]


def check_external_links(
    text: str,
    *,
    enabled: bool,
    checker: Iterable[tuple[str, bool]] | None = None,
) -> list[str]:
    """Return URLs that a live check found broken — opt-in and offline-safe.

    Args:
        text: The body whose links to check.
        enabled: When ``False`` (the default config path) no network call is
            made and an empty list is returned — offline mode skips cleanly.
        checker: Pre-resolved ``(url, ok)`` pairs (used by tests / a cached
            resolver) instead of a live HTTP probe. ``None`` with
            ``enabled=True`` means "no resolver wired"; nothing is reported.

    Returns:
        The broken (and not skip-listed) URLs. Documented false-positive hosts
        (paywalls, libraries, the low-PMID artefact) are never reported.
    """
    if not enabled:
        return []
    results = dict(checker) if checker is not None else {}
    broken: list[str] = []
    for url in extract_urls(text):
        if _should_skip_link(url):
            continue
        if results.get(url, True) is False:
            broken.append(url)
    return broken
