"""Tests for the shared encoding decision (:mod:`creek.ingest.encoding`).

The module's whole claim is an asymmetry: a below-threshold detection
is trustworthy when the codec is strict multi-byte, and worthless when
it is single-byte. Both halves are *measured* here rather than
asserted, because both were wrong in the issues that prompted the
module — #1589 proposed lowering the threshold and #1591 proposed
swapping the fallback to latin-1, and each would have corrupted files
the other cared about.
"""

from __future__ import annotations

import codecs

import chardet
import pytest

from creek.ingest.encoding import (
    BINARY_CHECK_SIZE,
    BINARY_CONTROL_THRESHOLD,
    DEFAULT_CONFIDENCE_THRESHOLD,
    MULTIBYTE_CODECS,
    DecodedText,
    UndecodableBytesError,
    decode_bytes,
    is_multibyte_codec,
    looks_binary,
)

#: Single-byte corpora spanning the families chardet is likely to
#: confuse. The multi-byte branch must never fire on any of them, or
#: the fix reintroduces #1589's corruption from the Western side.
_SINGLE_BYTE_CORPORA: tuple[tuple[str, str, str], ...] = (
    (
        "cp1252-rich",
        "cp1252",
        "name,note\nAlice,café naïve — “quoted”\nBob,résumé \u2013 £85 €9 ©\n",
    ),
    (
        "latin1-fr",
        "latin-1",
        "Le matin, après avoir préparé un café très serré, il s'installe "
        "près de la fenêtre et relit ses notes. ",
    ),
    (
        "latin1-de",
        "latin-1",
        "Über den Fluß ging er täglich, während die Vögel sangen und "
        "größere Schatten fielen. ",
    ),
    (
        "iso8859-5-ru",
        "iso8859-5",
        "Он каждое утро выходит к реке и долго смотрит на воду. ",
    ),
    (
        "cp1251-ru",
        "cp1251",
        "Он каждое утро выходит к реке и долго смотрит на воду. ",
    ),
    (
        "iso8859-7-gr",
        "iso8859-7",
        "Κάθε πρωί περπατάει δίπλα στο ποτάμι και σκέφτεται ήσυχα. ",
    ),
    (
        "cp1254-tr",
        "cp1254",
        "Her sabah nehrin kenar\u0131nda yürüyor ve günü sessizce düşünüyor. ",
    ),
    (
        "iso8859-2-cz",
        "iso8859-2",
        "Každé ráno chodí k řece a dlouho se dívá na vodu. ",
    ),
)


class TestMultibyteCodecSet:
    """:data:`MULTIBYTE_CODECS` earns its exemption, it does not assert it."""

    def test_the_set_is_not_empty_and_covers_every_cjk_family(self) -> None:
        """The set has not been emptied, trimmed, or narrowed to one language.

        Both parametrised tests in this class draw their cases from
        :data:`MULTIBYTE_CODECS`, so emptying it would run *zero* cases
        and leave the suite green while every CJK file silently fell
        back to cp1252 again. This is the assertion that cannot be
        satisfied by deletion.

        The families are listed individually because #1589 asserted the
        defect was "specific to the Chinese detector". It is not:
        Japanese and Korean reach the same gate, so dropping either
        family would restore half the bug.
        """
        assert len(MULTIBYTE_CODECS) >= 20
        for representative in (
            "gb18030",
            "gbk",
            "big5",
            "cp932",
            "shift_jis",
            "euc_jp",
            "euc_kr",
            "cp949",
            "iso2022_jp",
        ):
            assert representative in MULTIBYTE_CODECS, (
                f"{representative} left MULTIBYTE_CODECS; files in that "
                "encoding fall back to cp1252 and reach the vault as "
                "mojibake (#1589) or crash on an undefined byte (#1591)"
            )

    @pytest.mark.parametrize("name", sorted(MULTIBYTE_CODECS))
    def test_entry_is_its_own_canonical_name(self, name: str) -> None:
        """Every entry is spelled the way :func:`codecs.lookup` spells it.

        Membership is tested against a canonicalised name, so an entry
        spelled any other way would simply never match — a typo here
        silently disables one codec's exemption rather than failing.

        Args:
            name: A codec name from the set.
        """
        assert codecs.lookup(name).name.replace("-", "_") == name

    @pytest.mark.parametrize("name", sorted(MULTIBYTE_CODECS))
    def test_entry_rejects_random_noise(self, name: str) -> None:
        """Every entry actually refuses non-conforming bytes.

        This is the entire justification for trusting a below-threshold
        detection: a strict codec's successful decode is evidence,
        because it could have failed. A codec that accepted anything
        would make the exemption a tautology and let mojibake through,
        so it does not belong in the set.

        Args:
            name: A codec name from the set.
        """
        noise = bytes(range(1, 256)) * 16
        with pytest.raises(UnicodeDecodeError):
            noise.decode(name)

    def test_utf16_and_utf32_are_deliberately_excluded(self) -> None:
        """The UTF family is absent, and does not need the exemption.

        They are multi-byte, so their absence looks like an oversight.
        It is not: they are detected with near-certainty from their BOM
        and byte pattern, so they clear the threshold on the ordinary
        path. Admitting them would widen the exemption for no gain.
        """
        for name in ("utf_16", "utf_16_le", "utf_16_be", "utf_32"):
            assert name not in MULTIBYTE_CODECS

        text = "name,city\n田中,東京\nAlice,Münster\n"
        detection = chardet.detect(text.encode("utf-16"))
        assert (detection.get("confidence") or 0.0) >= DEFAULT_CONFIDENCE_THRESHOLD
        assert decode_bytes(text.encode("utf-16")).text == text

    def test_is_multibyte_codec_spans_spellings_and_rejects_unknowns(self) -> None:
        """Detector spellings resolve; unknown and empty names do not."""
        assert is_multibyte_codec("GB18030")
        assert is_multibyte_codec("shift-jis")
        assert is_multibyte_codec("CP932")
        assert not is_multibyte_codec("cp1252")
        assert not is_multibyte_codec("not-a-real-codec")
        assert not is_multibyte_codec(None)
        assert not is_multibyte_codec("")


class TestSingleByteDetectionsAreNotTrusted:
    """The exemption must never fire on single-byte text."""

    @pytest.mark.parametrize(
        ("codec", "corpus"),
        [(codec, corpus) for _, codec, corpus in _SINGLE_BYTE_CORPORA],
        ids=[name for name, _, _ in _SINGLE_BYTE_CORPORA],
    )
    def test_chardet_never_answers_a_multibyte_codec(
        self,
        codec: str,
        corpus: str,
    ) -> None:
        """chardet's guess for single-byte text is never in the set.

        If it ever were, the exemption would fire on Western text and
        a wrong-but-decodable codec would be trusted below the
        threshold — exactly the corruption #1589 reported, arriving
        from the other direction.

        Args:
            codec: Codec the corpus is written in.
            corpus: Source text.
        """
        raw = (corpus * 8).encode(codec)
        detected = chardet.detect(raw).get("encoding")
        assert not is_multibyte_codec(detected), (
            f"chardet answered {detected!r} for {codec} text, which is in "
            "MULTIBYTE_CODECS; the below-threshold exemption would fire on "
            "single-byte content and trust a wrong codec"
        )

    def test_cp1252_punctuation_survives_exactly(self) -> None:
        """Smart quotes, dashes and € round-trip, so latin-1 is not the fallback.

        #1591 suggested latin-1 "or better" as the fallback. latin-1
        maps all 256 byte values, so it never raises — but it renders
        0x93/0x94/0x97/0x80 as control characters, silently destroying
        the punctuation every legacy Excel export is full of. This is
        the assertion that forbids that swap.
        """
        text = "He said “ok” — café \u2013 €5 ½ ©\n"
        result = decode_bytes(text.encode("cp1252"))
        assert result.text == text
        assert result.codec == "cp1252"
        assert result.degraded is True
        assert "\x93" not in result.text
        assert "\x80" not in result.text

    def test_a_decodable_below_threshold_single_byte_guess_is_rejected(self) -> None:
        """Decodability alone does not buy trust for a single-byte codec.

        #1589 suggested trusting any detection whose decode succeeds.
        Every single-byte codec's decode succeeds on every input, so
        that rule carries no information; here chardet's low-confidence
        guess decodes cleanly and is still refused in favour of cp1252.
        """
        text = "name,note\nAlice,café naïve — “quoted”\n"
        raw = text.encode("cp1252")
        detected = chardet.detect(raw).get("encoding")
        assert detected is not None
        assert not is_multibyte_codec(detected)
        raw.decode(detected)  # succeeds, and means nothing
        assert decode_bytes(raw).text == text


class TestMultibyteDetectionsAreTrustedBelowTheThreshold:
    """The exemption fires where it must."""

    @pytest.mark.parametrize(
        ("codec", "corpus"),
        [
            ("gbk", "姓名,城市\n王伟,北京\n李娜,上海\n"),
            ("shift_jis", "名前、年齢。\n田中、三十。\n"),
            ("euc-kr", "이름,도시\n김철수,서울\n이영희,부산\n"),
        ],
        ids=["gbk", "shift_jis", "euc-kr"],
    )
    def test_cjk_round_trips_though_confidence_is_under_the_gate(
        self,
        codec: str,
        corpus: str,
    ) -> None:
        """CJK text decodes exactly, and does so *below* the threshold.

        The second assertion is the point. If chardet's confidence rose
        above the gate these would pass through the ordinary path and
        stop testing the exemption at all, leaving it uncovered while
        the suite stayed green.

        Args:
            codec: Codec the corpus is written in.
            corpus: Source text.
        """
        raw = corpus.encode(codec)
        confidence = chardet.detect(raw).get("confidence") or 0.0
        assert confidence < DEFAULT_CONFIDENCE_THRESHOLD, (
            f"{codec} now scores {confidence:.3f}, at or above the gate, so "
            "this case no longer exercises the multi-byte exemption"
        )
        result = decode_bytes(raw)
        assert result.text == corpus
        assert result.degraded is False
        assert is_multibyte_codec(result.codec)


class TestFallbackChainAndBinaryRefusal:
    """The last-resort chain, and the file that must stay loud."""

    def test_empty_input_is_empty_text(self) -> None:
        """No bytes means no detection and no warning."""
        assert decode_bytes(b"") == DecodedText(text="", codec="utf-8", degraded=False)

    def test_utf8_and_bom_never_reach_the_detector(self) -> None:
        """The UTF-8 probe runs first and strips the BOM Excel writes."""
        text = "name,city\nMünster,São Paulo\n"
        assert decode_bytes(text.encode()).codec == "utf-8-sig"
        with_bom = decode_bytes(text.encode("utf-8-sig"))
        assert with_bom.text == text
        assert "﻿" not in with_bom.text
        assert with_bom.degraded is False

    def test_latin1_catches_what_cp1252_leaves_undefined(self) -> None:
        """Text-but-not-cp1252 bytes still decode, and are flagged degraded.

        cp1252 leaves 0x81 0x8d 0x8f 0x90 0x9d undefined — the #1591
        crash. These bytes are not binary, so refusing them would drop
        a readable file; latin-1 is the right last resort *here*, and
        only here, after cp1252 has had its turn.
        """
        raw = b"name,note\r\nAlice,\x81\x8d\x8f\x90\x9d caf\xe9\r\n"
        assert not looks_binary(raw)
        result = decode_bytes(raw)
        assert result.codec == "latin-1"
        assert result.degraded is True
        assert "café" in result.text

    def test_binary_input_raises_rather_than_decoding_to_garbage(self) -> None:
        """A binary file is refused, not silently turned into a fragment.

        latin-1 would happily decode this into 1024 characters of
        garbage. Raising is what lets ``Ingestor._parse_safe`` record
        the file as an error instead of writing it to the vault.
        """
        with pytest.raises(UndecodableBytesError):
            decode_bytes(bytes(range(256)) * 4)

    def test_a_detected_codec_beats_the_binary_guard(self) -> None:
        """UTF-16 is full of null bytes and must still decode.

        The binary guard sits *after* detection precisely so that
        genuinely-detected text is never refused for looking binary.
        Moving it earlier would reject every UTF-16 CSV.
        """
        text = "name,city\n田中,東京\n"
        raw = text.encode("utf-16")
        assert looks_binary(raw)
        assert decode_bytes(raw).text == text

    def test_the_single_byte_threshold_is_honoured(self) -> None:
        """A caller-supplied threshold changes which single-byte guesses pass.

        Raising it past a confident single-byte detection must push
        that file onto the fallback, proving the threshold is read
        rather than ignored now that codec class also grants entry.
        """
        raw = ("Он каждое утро выходит к реке и долго смотрит на воду. " * 8).encode(
            "iso8859-5",
        )
        detection = chardet.detect(raw)
        assert not is_multibyte_codec(detection.get("encoding"))
        permissive = decode_bytes(raw, confidence_threshold=0.1)
        assert permissive.degraded is False
        assert permissive.text.startswith("Он каждое утро")
        strict = decode_bytes(raw, confidence_threshold=0.99)
        assert strict.degraded is True
        assert strict.codec == "cp1252"


class TestLooksBinary:
    """The one binary heuristic, shared by every decode site."""

    def test_empty_and_plain_text_are_not_binary(self) -> None:
        """Neither no bytes nor ordinary text trips the heuristic."""
        assert looks_binary(b"") is False
        assert looks_binary(b"Hello, world!\r\n") is False
        assert looks_binary("café naïve".encode()) is False

    def test_a_null_byte_anywhere_is_binary(self) -> None:
        """A single null byte is decisive, wherever it appears."""
        assert looks_binary(b"hello\x00world") is True
        assert looks_binary(b"a" * 20000 + b"\x00") is True

    def test_dense_control_characters_are_binary(self) -> None:
        """Control-char density above the threshold is binary."""
        assert looks_binary(b"\x89PNG\r\n\x1a\n\x01\x02\x03\x04") is True
        assert looks_binary(b"text\x01\x02\x03\x04\x05\x06") is True

    def test_the_threshold_is_a_ratio_not_a_count(self) -> None:
        """Sparse control characters inside the scored window stay text.

        The control bytes have to sit *within* the first
        :data:`BINARY_CHECK_SIZE` bytes or this case proves nothing:
        anything past the window is never scored at all, so a corpus
        whose controls all fall beyond it stays text under a
        count-based rule just as readily as under a ratio. Here 100
        control bytes land inside a full-length prefix — 1.2%, well
        under :data:`BINARY_CONTROL_THRESHOLD` — while a rule keyed on
        the raw count would call the file binary.
        """
        sparse = b"a" * (BINARY_CHECK_SIZE - 200) + b"\x01" * 100 + b"a" * 100
        assert len(sparse) == BINARY_CHECK_SIZE
        assert 100 / BINARY_CHECK_SIZE < BINARY_CONTROL_THRESHOLD
        assert looks_binary(sparse) is False


class TestTheUnfixedPathsAreADeliberateBoundary:
    """Pin the two decode paths this module does **not** yet own.

    ``generic._try_decode`` and ``base.normalize_encoding`` are wrong in
    exactly the way #1589 describes, and were left that way on purpose:
    neither has a confidence gate, so imposing this module's 0.70
    single-byte threshold would push correctly-decoded Cyrillic and Greek
    into mojibake. The unification is #1600.

    These tests exist so the scope boundary is *executed* rather than
    claimed in a docstring. They pin the broken behaviour, so when #1600
    lands they go red and force this module's docstring to be corrected
    in the same change — which is the failure mode that produced the
    review comment on PR #1607.
    """

    CP1252_TEXT = "naïve café — £85"

    def test_normalize_encoding_still_mangles_a_genuine_cp1252_file(self) -> None:
        """base.normalize_encoding round-trips cp1252 wrongly. Owned by #1600."""
        from creek.ingest.base import normalize_encoding

        raw = self.CP1252_TEXT.encode("cp1252")
        text, detected = normalize_encoding(raw)

        assert detected.lower() != "cp1252", (
            "chardet started answering cp1252 for this corpus; the premise of "
            "#1600 has changed and this boundary test must be re-derived"
        )
        assert text != self.CP1252_TEXT, (
            "base.normalize_encoding now decodes cp1252 correctly. If #1600 "
            "landed, delete this test AND correct the encoding module docstring, "
            "which still says this path diverges"
        )

    def test_try_decode_still_mangles_a_genuine_cp1252_file(self) -> None:
        """generic._try_decode round-trips cp1252 wrongly. Owned by #1600."""
        from creek.ingest.generic import _try_decode

        raw = self.CP1252_TEXT.encode("cp1252")
        text = _try_decode(raw)

        assert text is not None
        assert text != self.CP1252_TEXT, (
            "generic._try_decode now decodes cp1252 correctly. If #1600 landed, "
            "delete this test AND correct the encoding module docstring"
        )

    def test_neither_unfixed_path_routes_through_decode_bytes(self) -> None:
        """The boundary is structural, not incidental — assert the call graph."""
        import inspect

        from creek.ingest import base, generic

        for module in (base, generic):
            source = inspect.getsource(module)
            assert "decode_bytes" not in source, (
                f"{module.__name__} now calls decode_bytes. If #1600 landed, "
                "delete this class and correct the encoding module docstring"
            )
