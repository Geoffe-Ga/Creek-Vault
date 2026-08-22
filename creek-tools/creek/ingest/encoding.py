"""The encoding decision for the CSV path, and the rule the others should adopt.

Three call sites decide independently how to turn bytes into text, with
three different policies, each wrong in a different direction.
``spreadsheets._read_csv`` gated ``chardet`` behind a single confidence
threshold and fell back to ``cp1252``; ``generic._try_decode`` trusts it
at any confidence and falls back to ``latin-1``; ``base.normalize_encoding``
trusts it at any confidence with ``errors="replace"``.

**Only the first of those three is routed through this module today.**
``generic._try_decode`` and ``base.normalize_encoding`` are unchanged and
still diverge — and they are not merely stale, they are wrong in the way
#1589 describes: both silently rewrite a genuine cp1252 file (``naïve`` ->
``naďve``, ``£85`` -> ``Ł85``), and ``base.normalize_encoding`` is the
encoding path for ``markdown``, ``documents``, ``code``, ``chatgpt``,
``claude`` and ``substack``, a far larger surface than the CSV path.

They were deliberately left alone rather than rerouted here, because
neither has a confidence gate today: imposing this module's 0.70
single-byte threshold on them would push Cyrillic (0.45), Greek (0.37)
and other correctly-decoded single-byte corpora from correct to mojibake.
Unifying them needs a tie-break this module does not yet have. That work
is #1600; until it lands, "one encoding decision" describes the intent of
this module, not the state of the pipeline.

**The decision rule, and why it is not a threshold.**

Issues #1589 and #1591 both proposed threshold-shaped fixes — lower the
gate, or trust any detection that decodes without raising. Measured
against real corpora under chardet 7.6.0, both are unsafe:

* Confidence does not separate the cases. A GBK CSV scores 0.38 and a
  short Shift-JIS one 0.32, while a genuine cp1252 file's top guess
  scores 0.05 and a Cyrillic one 0.45. There is no threshold that
  admits the first two and excludes the fourth.
* "It decoded, so trust it" is not evidence for a **single-byte**
  codec, because single-byte codecs decode *everything*. chardet
  answers Windows-1250 for a genuine cp1252 file; that decode succeeds
  and silently rewrites ``naïve`` and ``£85``.

What *is* evidence is the codec's class. Legacy CJK multi-byte codecs
are strict: they reject byte sequences that do not conform to their
lead/trail structure. All 21 codecs in :data:`MULTIBYTE_CODECS` reject
random 8-bit noise, so a clean decode under one of them is a real
signal rather than a tautology. So the rule is an asymmetry:

* a **single-byte** detection is accepted only above
  :data:`DEFAULT_CONFIDENCE_THRESHOLD` — unchanged behaviour;
* a **multi-byte** detection is additionally accepted below the
  threshold when the bytes actually decode under it.

Probed across nine single-byte corpora (cp1252, latin-1 French and
German, ISO-8859-5, cp1251, ISO-8859-7, cp1254, ISO-8859-2), chardet
never once answered a codec in :data:`MULTIBYTE_CODECS`, so the new
branch cannot fire on Western text and the previously-correct
behaviour is preserved by construction.

UTF-16 and UTF-32 are deliberately **absent** from
:data:`MULTIBYTE_CODECS`. They are multi-byte, but they are detected at
0.95-1.0 by the BOM and byte pattern, so they clear the threshold on
the ordinary path and never need the exemption.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

import chardet

__all__ = [
    "BINARY_CHECK_SIZE",
    "BINARY_CONTROL_THRESHOLD",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "MULTIBYTE_CODECS",
    "DecodedText",
    "UndecodableBytesError",
    "decode_bytes",
    "is_multibyte_codec",
    "looks_binary",
]

DEFAULT_CONFIDENCE_THRESHOLD: float = 0.7
"""Minimum ``chardet`` confidence required to trust a *single-byte* guess.

Multi-byte guesses bypass this by codec class (see the module
docstring), never by lowering it.
"""

BINARY_CHECK_SIZE: int = 8192
"""Prefix length scored by :func:`looks_binary`."""

BINARY_CONTROL_THRESHOLD: float = 0.10
"""Control-character ratio above which a prefix is judged binary."""

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
earns a below-threshold detection the right to be trusted, so a codec
belongs here only if it actually rejects malformed input — see
``tests/test_ingest_encoding.py``, which proves that for every entry
rather than taking the list on faith.
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


def _accept_detection(
    raw: bytes,
    confidence_threshold: float,
) -> tuple[DecodedText | None, str | None, float]:
    """Run ``chardet`` and accept its guess if the asymmetry rule allows.

    Args:
        raw: The bytes to detect.
        confidence_threshold: Floor applied to single-byte guesses.

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
    trusted = confidence >= confidence_threshold or is_multibyte_codec(detected)
    if not trusted:
        return None, detected, confidence
    try:
        text = raw.decode(detected)
    except (UnicodeDecodeError, LookupError):
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
    """Decode *raw* with the last-resort chain, cp1252 before latin-1.

    Order matters and is the whole content of this function. cp1252 is
    what legacy Excel exports actually are, and it renders their smart
    quotes, en/em dashes and ``€`` correctly; latin-1 maps those same
    bytes to control characters. latin-1 runs second only to catch the
    five values cp1252 leaves undefined (0x81 0x8d 0x8f 0x90 0x9d) —
    the #1591 crash — which by this point are known not to be binary.

    Args:
        raw: The bytes to decode.
        detected: ``chardet``'s rejected guess, recorded for the
            caller's warning.
        confidence: The score that guess was given.

    Returns:
        A degraded :class:`DecodedText`; no codec was identified.
    """
    try:
        text = raw.decode("cp1252")
    except UnicodeDecodeError:
        return DecodedText(
            text=raw.decode("latin-1"),
            codec="latin-1",
            degraded=True,
            detected=detected,
            confidence=confidence,
        )
    return DecodedText(
        text=text,
        codec="cp1252",
        degraded=True,
        detected=detected,
        confidence=confidence,
    )


def decode_bytes(
    raw: bytes,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> DecodedText:
    """Decode *raw* to text, choosing a codec by class rather than score.

    Probe order:

    1. ``utf-8-sig`` — covers plain UTF-8 and the BOM Excel writes.
    2. ``chardet``, accepted per the asymmetry rule in the module
       docstring.
    3. :func:`looks_binary` — refuse rather than reach an
       all-accepting codec.
    4. ``cp1252``, then ``latin-1``. cp1252 goes first because it is
       what legacy Excel exports actually are; latin-1 would turn
       their smart quotes, dashes and ``€`` into control characters.
       latin-1 catches the remainder, which cp1252 leaves undefined at
       0x81 0x8d 0x8f 0x90 0x9d.

    Args:
        raw: The bytes to decode.
        confidence_threshold: Floor for single-byte detections.

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
