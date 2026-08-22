"""The one encoding decision every ingest path makes.

Three call sites used to decide independently how to turn bytes into
text, with three different policies, each wrong in a different
direction: ``spreadsheets._read_csv`` gated ``chardet`` behind a single
confidence threshold and fell back to ``cp1252``; ``generic._try_decode``
trusted it at any confidence and fell back to ``latin-1``;
``base.normalize_encoding`` trusted it at any confidence with
``errors="replace"``. All three now route through :func:`decode_bytes`
(#1600), so a file decodes the same way whether it arrives as a CSV, a
markdown note, a source file or a Substack export.

**The decision rule, and why it is not a threshold.**

Issues #1589 and #1591 both proposed threshold-shaped fixes — lower the
gate, or trust any detection that decodes without raising. Measured
against real corpora under chardet 7.6.0, both are unsafe:

* Confidence does not separate the cases. A GBK CSV scores 0.36 and a
  short Shift-JIS one 0.32, while a genuine cp1252 file's top guess
  scores 0.05 and a Cyrillic one 0.45. No threshold admits the first
  two and excludes the fourth.
* "It decoded, so trust it" is not evidence for a **single-byte**
  codec, because single-byte codecs decode *everything*. chardet
  answers Windows-1250 for a genuine cp1252 file; that decode succeeds
  and silently rewrites ``naïve`` and ``£85``.

So a below-threshold guess has to earn its acceptance against the
fallback it would displace, on two counts.

**1. Placement.** :func:`_placement_score` asks, of every non-ASCII
letter, whether it sits where a letter belongs: beside another letter
of its own script. A wrong codec turns a *symbol* byte into a letter,
and that letter lands beside a digit or a space — ``£85`` becomes
``Ł85`` under cp1250, and ``-5°C`` becomes ``-5蚓`` under Big5, whose
lead/trail pair swallows the ``C``. A right codec never does that. The
candidate must score no worse than the ``cp1252`` fallback.

**2. Provenance.** Placement alone is not enough for a *single-byte*
guess, because within one script family a wrong codec can still land
letters beside letters: ``5µg`` decodes to ``5Ág`` under cp850 and
scores a perfect 1.00. There is no evidence to separate those, so a
single-byte guess is additionally required to introduce a **writing
system the fallback does not contain** — Cyrillic, Greek, Hebrew — and
is rejected when it merely proposes a different Latin codec. A
*multi-byte* guess is exempt from this second test, and only from
this one: the 21 codecs in :data:`MULTIBYTE_CODECS` reject byte
sequences that do not conform to their lead/trail structure, so a
clean decode under one of them is already independent evidence, which
is the asymmetry #1607 established. The exemption extends to
placement in one narrow way — an isolated CJK character in an
otherwise-ASCII cell (``1,Alice,河``) has no same-script neighbour but
is not embedded in a Latin word either, so it is not counted against
the guess.

**What this deliberately does not fix.** A file written in a
*Latin-script* single-byte codec that chardet gets wrong stays wrong:
ISO-8859-2 Czech and cp1254 Turkish both round-trip incorrectly
through the ``cp1252`` fallback, and admitting them would mean
admitting cp850-for-``5µg`` too, which corrupts a Western file that is
correct today. That trade is taken deliberately, and #1610 carries
the measured table behind it. The recoverable route is a better
detector or an operator-pinned per-source encoding, not a change to
this rule.

UTF-16 and UTF-32 are deliberately **absent** from
:data:`MULTIBYTE_CODECS`. They are multi-byte, but they are detected at
0.95-1.0 by the BOM and byte pattern, so they clear the threshold on
the ordinary path and never need the exemption.
"""

from __future__ import annotations

import codecs
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

import chardet

__all__ = [
    "BINARY_CHECK_SIZE",
    "BINARY_CONTROL_THRESHOLD",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "MULTIBYTE_CODECS",
    "PLACEMENT_SAMPLE_SIZE",
    "DecodedText",
    "UndecodableBytesError",
    "decode_bytes",
    "is_multibyte_codec",
    "looks_binary",
]

DEFAULT_CONFIDENCE_THRESHOLD: float = 0.7
"""Minimum ``chardet`` confidence required to trust a guess *unjudged*.

Below it a guess is not refused, it is judged: it must place its
letters at least as well as the cp1252 fallback would, and — unless it
is a strict multi-byte codec — propose a writing system that fallback
cannot produce. See the module docstring. Lowering this number is not
the fix for anything; it only widens the band that skips the judging.
"""

BINARY_CHECK_SIZE: int = 8192
"""Prefix length scored by :func:`looks_binary`."""

BINARY_CONTROL_THRESHOLD: float = 0.10
"""Control-character ratio above which a prefix is judged binary."""

PLACEMENT_SAMPLE_SIZE: int = 4096
"""Characters of a candidate decode scored by :func:`_placement_score`.

The score is a ratio, so a prefix answers the same question as the
whole file while keeping a per-character Python loop off multi-megabyte
inputs. Both the candidate and the fallback are scored over the same
prefix length, which is the beginning of the same file in both cases.
"""

MULTIBYTE_CODECS: frozenset[str] = frozenset(
    {
        "big5",
        "big5hkscs",
        "cp932",
        "cp949",
        "euc_jis_2004",
        "euc_jp",
        "euc_kr",
        "gb18030",
        "gb2312",
        "gbk",
        "hz",
        "iso2022_jp",
        "iso2022_jp_1",
        "iso2022_jp_2",
        "iso2022_jp_2004",
        "iso2022_jp_3",
        "iso2022_jp_ext",
        "iso2022_kr",
        "johab",
        "shift_jis",
        "shift_jisx0213",
    },
)
"""Legacy CJK codecs that reject non-conforming byte sequences.

Entries are :func:`codecs.lookup` canonical names. Membership is what
exempts a below-threshold detection from the *provenance* test: a
strict codec's clean decode is independent evidence, where a
single-byte codec's decode is a tautology. So a codec belongs here
only if it actually rejects malformed input — see
``tests/test_ingest_encoding.py``, which proves that for every entry
rather than taking the list on faith.

Membership is not on its own enough to be trusted, and #1607 read it
that way. ``0xB0 0x43`` — ``°C`` in cp1252 — is a valid Big5
lead/trail pair, so a multi-byte guess still has to place its letters
where letters belong.
"""

_REJECTED_DETECTIONS: frozenset[str] = frozenset({"ascii", "utf_8_sig", "cp1252"})
"""Detections that never beat the fallback.

``ascii`` is a subset of the UTF-8 probe that already failed, and the
other two *are* the fallback, so accepting them changes nothing while
costing a second decode.
"""


class UndecodableBytesError(ValueError):
    """Raised when bytes are binary rather than text in an unknown codec.

    Byte-wise almost nothing is undecodable — ``latin-1`` maps all 256
    values — so "undecodable" has to be a policy rather than a codec
    error. The policy is :func:`looks_binary`. Raising rather than
    returning garbage is deliberate: ``Ingestor._parse_safe`` records
    the failure and skips the file, which is the loud outcome. Falling
    through to an all-accepting codec would write a fragment of
    mojibake that nothing downstream could distinguish from real text.
    """


@dataclass(frozen=True, slots=True)
class DecodedText:
    """The outcome of decoding a byte stream.

    Attributes:
        text: The decoded text.
        codec: The codec that produced it, as reported by the detector
            or as the literal fallback name.
        degraded: ``True`` when no codec was positively identified and
            the fallback was used, so the caller can warn.
        detected: What ``chardet`` guessed, kept even when the guess
            was rejected so a warning can name it. ``None`` when the
            detector was never consulted.
        confidence: The score ``chardet`` gave *detected*.
    """

    text: str
    codec: str
    degraded: bool
    detected: str | None = None
    confidence: float = 0.0


def _canonical_codec(name: str | None) -> str | None:
    """Return the :mod:`codecs` canonical name for *name*, or ``None``.

    chardet spells the same codec several ways across versions
    (``cp932`` vs ``CP932``, ``shift-jis`` vs ``shift_jis``), so every
    comparison in this module goes through here rather than through
    :meth:`str.lower`.

    Args:
        name: A codec name, or ``None``.

    Returns:
        The canonical name, or ``None`` if unknown to Python.
    """
    if not name:
        return None
    try:
        return codecs.lookup(name).name.replace("-", "_")
    except LookupError:
        return None


def is_multibyte_codec(name: str | None) -> bool:
    """Report whether *name* is a strict multi-byte codec.

    Args:
        name: A codec name as reported by ``chardet``, or ``None``.

    Returns:
        ``True`` when *name* resolves to a member of
        :data:`MULTIBYTE_CODECS`.
    """
    return _canonical_codec(name) in MULTIBYTE_CODECS


def looks_binary(raw: bytes) -> bool:
    """Report whether *raw* looks like binary rather than text.

    Two heuristics, unchanged from the implementation this replaced:
    a null byte anywhere, or more than
    :data:`BINARY_CONTROL_THRESHOLD` of the first
    :data:`BINARY_CHECK_SIZE` bytes being non-text control characters
    (0-8, 14-31).

    Args:
        raw: The bytes to judge.

    Returns:
        ``True`` when the bytes appear to be binary.
    """
    if not raw:
        return False
    if b"\x00" in raw:
        return True
    sample = raw[:BINARY_CHECK_SIZE]
    control_count = sum(1 for byte in sample if byte < 9 or 14 <= byte <= 31)
    return control_count / len(sample) > BINARY_CONTROL_THRESHOLD


_SCRIPT_FAMILIES: frozenset[str] = frozenset(
    {
        "ARABIC",
        "ARMENIAN",
        "BENGALI",
        "CJK",
        "CYRILLIC",
        "DEVANAGARI",
        "ETHIOPIC",
        "GEORGIAN",
        "GREEK",
        "GUJARATI",
        "HANGUL",
        "HEBREW",
        "HIRAGANA",
        "KANNADA",
        "KATAKANA",
        "LATIN",
        "MALAYALAM",
        "MYANMAR",
        "ORIYA",
        "SYRIAC",
        "TAMIL",
        "TELUGU",
        "THAANA",
        "THAI",
    },
)
"""First words of :mod:`unicodedata` names that name a writing system.

A character whose name starts with anything else — ``MICRO SIGN``,
``FEMININE ORDINAL INDICATOR``, ``DEGREE CELSIUS`` — is a letterlike
symbol rather than a letter of some script. Those are never counted
against a decode, because their placement says nothing: ``µ`` sits in
``5µg`` exactly as happily as it would in mojibake.
"""

_SCRIPT_ALIASES: dict[str, str] = {"HIRAGANA": "CJK", "KATAKANA": "CJK"}
"""Families that are one writing system split across three name prefixes.

Japanese text mixes kanji, hiragana and katakana within a single word,
so treating them as three scripts would score correct Japanese as
misplaced.
"""


@lru_cache(maxsize=1024)
def _script_family(char: str) -> str:
    """Return the writing system *char* belongs to, or ``""``.

    Args:
        char: A single character.

    Returns:
        A member of :data:`_SCRIPT_FAMILIES` after alias folding, or
        ``""`` for anything that is not a letter of a known script.
    """
    name = unicodedata.name(char, "")
    family = name.split(" ", 1)[0]
    if family not in _SCRIPT_FAMILIES:
        return ""
    return _SCRIPT_ALIASES.get(family, family)


def _neighbours(text: str, index: int) -> tuple[str, ...]:
    """Return the characters either side of *index*.

    Args:
        text: The text being scored.
        index: Position of the character whose neighbours are wanted.

    Returns:
        Zero to two single-character strings.
    """
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return tuple(char for char in (before, after) if char)


def _is_well_placed(text: str, index: int, *, isolated_is_ok: bool) -> bool:
    """Report whether the letter at *index* sits where a letter belongs.

    Args:
        text: The candidate decode.
        index: Position of a non-ASCII letter within *text*.
        isolated_is_ok: When ``True``, a letter with no neighbouring
            ASCII alphanumeric counts as well placed even without a
            same-script neighbour — an isolated CJK character in an
            otherwise-ASCII cell. Only a multi-byte guess earns this;
            see the module docstring.

    Returns:
        ``True`` when the letter's position is consistent with real
        text rather than with a misread symbol byte.
    """
    family = _script_family(text[index])
    if not family:
        return True
    neighbours = _neighbours(text, index)
    if any(char.isalpha() and _script_family(char) == family for char in neighbours):
        return True
    return isolated_is_ok and not any(
        char.isascii() and char.isalnum() for char in neighbours
    )


def _placement_score(text: str, *, isolated_is_ok: bool) -> float:
    """Score what fraction of *text*'s non-ASCII letters are well placed.

    Args:
        text: A candidate decode.
        isolated_is_ok: Passed through to :func:`_is_well_placed`.

    Returns:
        A ratio in ``[0.0, 1.0]``; ``1.0`` when there is nothing to
        score, so an all-ASCII decode is never penalised.
    """
    sample = text[:PLACEMENT_SAMPLE_SIZE]
    positions = [
        index
        for index, char in enumerate(sample)
        if not char.isascii() and char.isalpha()
    ]
    if not positions:
        return 1.0
    placed = sum(
        1
        for index in positions
        if _is_well_placed(sample, index, isolated_is_ok=isolated_is_ok)
    )
    return placed / len(positions)


def _script_families(text: str) -> frozenset[str]:
    """Return the writing systems the letters of *text* belong to.

    Args:
        text: A candidate decode.

    Returns:
        The set of non-empty families found in the scored prefix.
    """
    return frozenset(
        family
        for family in (
            _script_family(char)
            for char in text[:PLACEMENT_SAMPLE_SIZE]
            if char.isalpha()
        )
        if family
    )


def _fallback_text(raw: bytes) -> tuple[str, str]:
    """Decode *raw* with the last-resort chain, cp1252 before latin-1.

    Order matters and is the whole content of this function. cp1252 is
    what legacy Excel exports actually are, and it renders their smart
    quotes, en/em dashes and ``€`` correctly; latin-1 maps those same
    bytes to control characters. latin-1 runs second only to catch the
    five values cp1252 leaves undefined (0x81 0x8d 0x8f 0x90 0x9d) —
    the #1591 crash.

    Args:
        raw: The bytes to decode.

    Returns:
        ``(text, codec)``.
    """
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def _guess_beats_fallback(raw: bytes, detected: str, candidate: str) -> bool:
    """Report whether *candidate* is better evidence than the fallback.

    The two tests are the module docstring's placement and provenance,
    in that order.

    Args:
        raw: The bytes being decoded.
        detected: ``chardet``'s guess, already known to decode *raw*.
        candidate: The text that guess produced.

    Returns:
        ``True`` when the guess should be trusted below the confidence
        threshold.
    """
    fallback, _codec = _fallback_text(raw)
    multibyte = is_multibyte_codec(detected)
    candidate_score = _placement_score(candidate, isolated_is_ok=multibyte)
    if candidate_score < _placement_score(fallback, isolated_is_ok=False):
        return False
    if multibyte:
        return True
    return bool(_script_families(candidate) - _script_families(fallback))


def _accept_detection(
    raw: bytes,
    confidence_threshold: float,
) -> tuple[DecodedText | None, str | None, float]:
    """Run ``chardet`` and accept its guess if the accept rule allows.

    Args:
        raw: The bytes to detect.
        confidence_threshold: Score at or above which a guess is taken
            unjudged. Below it every guess — single-byte or
            multi-byte — goes through :func:`_guess_beats_fallback`.

    Returns:
        ``(result, detected, confidence)``. *result* is ``None`` when
        the guess was rejected or failed to decode; *detected* and
        *confidence* are returned either way so the caller can name the
        rejected guess in a warning.
    """
    detection = chardet.detect(raw)
    detected = detection.get("encoding")
    confidence = detection.get("confidence") or 0.0
    if not detected or _canonical_codec(detected) in _REJECTED_DETECTIONS:
        return None, detected, confidence
    try:
        text = raw.decode(detected)
    except (UnicodeDecodeError, LookupError):
        return None, detected, confidence
    if confidence < confidence_threshold and not _guess_beats_fallback(
        raw,
        detected,
        text,
    ):
        return None, detected, confidence
    return (
        DecodedText(
            text=text,
            codec=detected,
            degraded=False,
            detected=detected,
            confidence=confidence,
        ),
        detected,
        confidence,
    )


def _decode_by_fallback(
    raw: bytes,
    detected: str | None,
    confidence: float,
) -> DecodedText:
    """Wrap :func:`_fallback_text` as a degraded :class:`DecodedText`.

    The chain itself lives in :func:`_fallback_text` because the
    accept rule scores the same text before choosing (see
    :func:`_guess_beats_fallback`); the fallback a guess is compared
    against has to be the fallback that would actually be used.

    Args:
        raw: The bytes to decode.
        detected: ``chardet``'s rejected guess, recorded for the
            caller's warning.
        confidence: The score that guess was given.

    Returns:
        A degraded :class:`DecodedText`; no codec was identified.
    """
    text, codec = _fallback_text(raw)
    return DecodedText(
        text=text,
        codec=codec,
        degraded=True,
        detected=detected,
        confidence=confidence,
    )


def decode_bytes(
    raw: bytes,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> DecodedText:
    """Decode *raw* to text, judging a low-scoring guess rather than refusing it.

    Probe order:

    1. ``utf-8-sig`` — covers plain UTF-8 and the BOM Excel writes.
    2. ``chardet``, accepted outright at or above
       *confidence_threshold* and otherwise only if it beats the
       cp1252 fallback on placement and provenance — the rule in the
       module docstring.
    3. :func:`looks_binary` — refuse rather than reach an
       all-accepting codec.
    4. ``cp1252``, then ``latin-1``. cp1252 goes first because it is
       what legacy Excel exports actually are; latin-1 would turn
       their smart quotes, dashes and ``€`` into control characters.
       latin-1 catches the remainder, which cp1252 leaves undefined at
       0x81 0x8d 0x8f 0x90 0x9d.

    Args:
        raw: The bytes to decode.
        confidence_threshold: Score at or above which ``chardet``'s
            guess is taken unjudged.

    Returns:
        A :class:`DecodedText`.

    Raises:
        UndecodableBytesError: When *raw* is binary and no codec was
            positively identified.
    """
    if not raw:
        return DecodedText(text="", codec="utf-8", degraded=False)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    else:
        return DecodedText(text=text, codec="utf-8-sig", degraded=False)

    accepted, detected, confidence = _accept_detection(raw, confidence_threshold)
    if accepted is not None:
        return accepted

    if looks_binary(raw):
        msg = "content is binary, not text in an undetected encoding"
        raise UndecodableBytesError(msg)

    return _decode_by_fallback(raw, detected, confidence)
