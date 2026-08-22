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
import unicodedata

import chardet
import pytest

from creek.ingest.encoding import (
    BINARY_CHECK_SIZE,
    BINARY_CONTROL_THRESHOLD,
    DEFAULT_CONFIDENCE_THRESHOLD,
    MULTIBYTE_CODECS,
    DecodedText,
    UndecodableBytesError,
    _placement_score,
    _script_families,
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
        """A caller-supplied threshold still decides who skips the tie-break.

        #1600 gave a below-threshold guess a second route in, so the
        old form of this test — raise the gate past a Cyrillic
        detection and watch it degrade — no longer holds: that guess
        now enters on placement and provenance instead, which is the
        fix. What the threshold still governs is who gets in *without*
        being judged, so this drops the gate to zero and watches a
        genuine cp1252 file get corrupted by the guess the default
        threshold refuses. If the threshold were ignored, both calls
        would return the same text.
        """
        text = "name,note\nAlice,café naïve — “quoted”\nBob,résumé \u2013 £85 €9 ©\n"
        raw = text.encode("cp1252")
        detection = chardet.detect(raw)
        assert not is_multibyte_codec(detection.get("encoding"))
        ungated = decode_bytes(raw, confidence_threshold=0.0)
        assert ungated.degraded is False
        assert ungated.text != text, (
            "with the gate at zero chardet's guess is taken unjudged, and "
            "this corpus is the one it gets wrong; if it now round-trips, "
            "the threshold is no longer being read"
        )
        default = decode_bytes(raw)
        assert default.text == text
        assert default.codec == "cp1252"
        assert default.degraded is True


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


# ---- the unified decision (#1600, #1601, and #1607's regression) -------------

#: Corpora that pin the accept/reject rule, one row per *reason*.
#:
#: Each row is ``(id, codec, corpus, expected_codec_is_the_guess)``.
#: The last field says which way the rule must fall: ``True`` when
#: chardet's below-threshold guess has to be taken, ``False`` when the
#: cp1252 fallback has to win. Confidences are deliberately absent —
#: they move with corpus length and chardet version, and four of the
#: six quoted in #1600/#1601 did not reproduce.
_DECISION_CORPORA: tuple[tuple[str, str, str, bool], ...] = (
    (
        # #1607's live regression. 0xB0 0x43 ("°C") is a valid Big5
        # lead/trail pair, so the multi-byte exemption fired at 0.10
        # confidence, the decode succeeded, and degraded=False meant not
        # even the warning fired. The C is swallowed into the ideograph.
        "cp1252-degree-sign",
        "cp1252",
        "city,temp\nOslo,-5°C\nRio,32°C\n",
        False,
    ),
    (
        # Rejected on *placement*: chardet answers a Greek codec, so the
        # provenance test passes and only placement can refuse it.
        "cp1252-pound-sign",
        "cp1252",
        "naïve café — £85",
        False,
    ),
    (
        # Rejected on *provenance*: cp850 turns "5µg" into "5Ág",
        # a Latin letter beside a Latin letter, which places perfectly.
        # Nothing in the text can separate the two readings, so the
        # Latin-family guess is refused.
        "cp1252-micro-sign",
        "cp1252",
        "sample,dose\nAB,5µg\nCD,7µg\n",
        False,
    ),
    (
        "cp1251-cyrillic",
        "cp1251",
        "имя,город,заметка\n"
        "Иванов,Москва,Он каждое утро выходит к реке и долго смотрит на воду\n"
        "Петрова,Казань,Она любит лес и тишину\n",
        True,
    ),
    (
        "iso8859-5-cyrillic",
        "iso8859-5",
        "имя,город,заметка\n"
        "Иванов,Москва,Он каждое утро выходит к реке и долго смотрит на воду\n"
        "Петрова,Казань,Она любит лес и тишину\n",
        True,
    ),
    (
        "iso8859-7-greek",
        "iso8859-7",
        "όνομα,πόλη,σημείωση\n"
        "Γιώργος,Αθήνα,Κάθε πρωί περπατάει δίπλα στο ποτάμι\n"
        "Μαρία,Πάτρα,Της αρέσει το δάσος και η ησυχία\n",
        True,
    ),
    (
        "gbk-chinese",
        "gbk",
        "姓名,城市,备注\n王伟,北京,他每天早上都会沿着小河散步\n",
        True,
    ),
    (
        "shift-jis-japanese",
        "shift_jis",
        "名前、年齢。\n田中、三十。\n",
        True,
    ),
    (
        # The isolated-cell case the multi-byte exemption exists for: a
        # lone ideograph has no same-script neighbour, and would be
        # scored as misplaced if strict codecs were judged like loose
        # ones.
        "gbk-one-ideograph-per-cell",
        "gbk",
        "id,name,note\n1,Alice,河\n2,Bob,河\n",
        True,
    ),
)


def _letter_scripts(text: str) -> set[str]:
    """Return the writing systems the letters of *text* belong to.

    Recomputed here from :mod:`unicodedata` rather than imported from
    the module under test, so a test asserting "this guess proposes a
    new script" cannot be satisfied by the same bug it is guarding.
    It is deliberately coarser than the production version — it folds
    nothing and drops nothing — which is enough to tell "same writing
    system" from "different writing system" on the two rows that use
    it.

    Args:
        text: Any text.

    Returns:
        The first word of each letter's Unicode name, which is its
        script for every alphabet this rule cares about.
    """
    return {
        unicodedata.name(char, " ").split(" ", 1)[0] for char in text if char.isalpha()
    }


class TestTheAcceptRuleOnRealCorpora:
    """Every below-threshold guess is judged, and judged the same way."""

    @pytest.mark.parametrize(
        ("codec", "corpus", "guess_wins"),
        [(codec, corpus, wins) for _, codec, corpus, wins in _DECISION_CORPORA],
        ids=[name for name, _, _, _ in _DECISION_CORPORA],
    )
    def test_the_corpus_round_trips_whichever_way_the_rule_falls(
        self,
        codec: str,
        corpus: str,
        guess_wins: bool,
    ) -> None:
        """The text survives, and it survives for the stated reason.

        Args:
            codec: Codec the corpus is written in.
            corpus: Source text.
            guess_wins: Whether chardet's guess must be accepted
                (``degraded=False``) or refused in favour of cp1252.
        """
        raw = corpus.encode(codec)
        result = decode_bytes(raw)
        assert result.text == corpus, (
            f"{codec} bytes did not round-trip; decoded as {result.codec!r}"
        )
        assert result.degraded is not guess_wins, (
            f"expected the guess to {'win' if guess_wins else 'lose'} against "
            f"the cp1252 fallback, but got codec={result.codec!r} "
            f"degraded={result.degraded}"
        )

    @pytest.mark.parametrize(
        ("codec", "corpus"),
        [(codec, corpus) for _, codec, corpus, _ in _DECISION_CORPORA],
        ids=[name for name, _, _, _ in _DECISION_CORPORA],
    )
    def test_every_row_is_actually_below_the_threshold(
        self,
        codec: str,
        corpus: str,
    ) -> None:
        """None of these rows is decided by confidence alone.

        A row that drifted above the gate would pass on the ordinary
        path and stop exercising the rule, leaving it uncovered while
        the suite stayed green.

        Args:
            codec: Codec the corpus is written in.
            corpus: Source text.
        """
        confidence = chardet.detect(corpus.encode(codec)).get("confidence") or 0.0
        assert confidence < DEFAULT_CONFIDENCE_THRESHOLD, (
            f"{codec} now scores {confidence:.3f}, at or above the gate, so "
            "this row no longer exercises the accept rule"
        )

    def test_the_table_still_covers_every_reason(self) -> None:
        """The table has not been emptied, trimmed, or re-scoped.

        Deleting a row removes a guard without turning anything red.
        Each id below is a distinct *reason* a decode is accepted or
        refused; losing one loses that half of the rule.
        """
        assert {name for name, _, _, _ in _DECISION_CORPORA} == {
            "cp1252-degree-sign",
            "cp1252-pound-sign",
            "cp1252-micro-sign",
            "cp1251-cyrillic",
            "iso8859-5-cyrillic",
            "iso8859-7-greek",
            "gbk-chinese",
            "shift-jis-japanese",
            "gbk-one-ideograph-per-cell",
        }

    def test_the_degree_sign_row_really_exercises_the_multibyte_exemption(
        self,
    ) -> None:
        """The Big5 row is refused *despite* being a strict-codec guess.

        Without this assertion the row would still pass if chardet
        started answering a single-byte codec, and the guard that
        actually matters — a multi-byte decode has to be plausible too,
        not merely successful — would go untested.
        """
        raw = "city,temp\nOslo,-5°C\nRio,32°C\n".encode("cp1252")
        detected = chardet.detect(raw).get("encoding")
        assert is_multibyte_codec(detected), (
            f"chardet now answers {detected!r} here, which is not in "
            "MULTIBYTE_CODECS; re-derive this row or the multi-byte "
            "plausibility guard is no longer covered"
        )
        raw.decode(str(detected))  # it decodes cleanly, and means nothing
        assert decode_bytes(raw).codec == "cp1252"

    def test_the_pound_sign_row_is_refused_on_placement_not_provenance(
        self,
    ) -> None:
        """The Greek guess proposes a new script and is still refused.

        Provenance — "a single-byte guess must introduce a writing
        system the fallback lacks" — is satisfied here, so the only
        thing left to refuse this corpus is placement. Pinning that
        keeps the two halves of the single-byte rule separately
        covered.
        """
        raw = "naïve café — £85".encode("cp1252")
        detected = chardet.detect(raw).get("encoding")
        assert detected is not None
        assert not is_multibyte_codec(detected)
        candidate = raw.decode(detected)
        assert _letter_scripts(candidate) - _letter_scripts(raw.decode("cp1252")), (
            f"chardet now answers {detected!r}, which proposes no new script, "
            "so this row is refused by provenance and no longer covers "
            "placement"
        )
        assert decode_bytes(raw).codec == "cp1252"

    def test_the_micro_sign_row_is_refused_on_provenance_not_placement(
        self,
    ) -> None:
        """The cp850 guess places perfectly and is still refused.

        ``5µg`` becomes ``5Ág``: a Latin letter between a digit
        and a Latin letter, which is exactly where a letter belongs.
        Placement cannot separate this from a genuine Latin-script
        file, so provenance has to.
        """
        raw = "sample,dose\nAB,5µg\nCD,7µg\n".encode("cp1252")
        detected = chardet.detect(raw).get("encoding")
        assert detected is not None
        assert not is_multibyte_codec(detected)
        candidate = raw.decode(detected)
        assert candidate != raw.decode("cp1252")
        assert not _letter_scripts(candidate) - _letter_scripts(raw.decode("cp1252")), (
            f"chardet now answers {detected!r}, which proposes a new script, "
            "so this row no longer covers the provenance half of the rule"
        )
        assert decode_bytes(raw).codec == "cp1252"


class TestAllThreeDecodePathsAgree:
    """#1600: one decision, whichever door the bytes came through.

    ``base.normalize_encoding`` and ``generic._try_decode`` used to
    trust chardet at any confidence, each with its own fallback. They
    now route through :func:`decode_bytes`, so markdown, documents,
    code, chatgpt, claude, substack and the generic path all decode a
    file the way the CSV path does.
    """

    @pytest.mark.parametrize(
        ("codec", "corpus"),
        [(codec, corpus) for _, codec, corpus, _ in _DECISION_CORPORA],
        ids=[name for name, _, _, _ in _DECISION_CORPORA],
    )
    def test_the_three_entry_points_return_the_same_text(
        self,
        codec: str,
        corpus: str,
    ) -> None:
        """Same bytes, same text, on all three paths.

        Args:
            codec: Codec the corpus is written in.
            corpus: Source text.
        """
        from creek.ingest.base import normalize_encoding
        from creek.ingest.generic import _try_decode

        raw = corpus.encode(codec)
        assert decode_bytes(raw).text == corpus
        assert normalize_encoding(raw)[0] == corpus
        assert _try_decode(raw) == corpus

    @pytest.mark.parametrize(
        ("codec", "corpus"),
        [(codec, corpus) for _, codec, corpus, _ in _DECISION_CORPORA],
        ids=[name for name, _, _, _ in _DECISION_CORPORA],
    )
    def test_the_codec_normalize_encoding_reports_decodes_the_bytes(
        self,
        codec: str,
        corpus: str,
    ) -> None:
        """The stamp is usable, not decorative.

        ``chatgpt``, ``discord`` and ``claude`` decode the raw bytes a
        second time by this name — ``claude`` with a bare ``.decode()``
        that raises rather than replacing — so a name that does not
        decode the bytes is a crash, not a cosmetic wrong answer.

        Args:
            codec: Codec the corpus is written in.
            corpus: Source text.
        """
        from creek.ingest.base import normalize_encoding

        raw = corpus.encode(codec)
        text, reported = normalize_encoding(raw)
        assert raw.decode(reported) == text

    def test_normalize_encoding_never_raises_on_binary(self) -> None:
        """Binary input comes back as text, because 19 call sites assume it.

        Several of them sit in discovery loops that guard only
        ``OSError``; ``decode_bytes`` raising
        :class:`UndecodableBytesError` there would abort a whole scan
        over one unreadable file.
        """
        from creek.ingest.base import normalize_encoding

        text, reported = normalize_encoding(bytes(range(256)) * 4)
        assert isinstance(text, str)
        assert reported == "latin-1"

    def test_try_decode_still_drops_binary(self) -> None:
        """``None`` still means "skip this file", including for UTF-8-shaped noise.

        ``decode_bytes`` probes UTF-8 before it judges binary, so a
        null-riddled blob that happens to be valid UTF-8 decodes there.
        The generic path screens for binary first and must keep doing
        so, or a ``.bin`` file becomes a fragment.
        """
        from creek.ingest.generic import _try_decode

        assert _try_decode(bytes(range(256)) * 4) is None
        assert _try_decode(b"hello\x00world") is None


class TestWhatTheRuleStillGetsWrong:
    """The blind spot, executed rather than claimed in a docstring.

    A Latin-script single-byte codec that chardet gets wrong stays
    wrong: provenance refuses it, because admitting it would mean
    admitting cp850-for-``5µg`` and corrupting Western files that
    are correct today. Czech and Turkish are the two measured cases.
    Tracked in #1610; the route out is a better detector or an
    operator-pinned per-source encoding, not a change to this rule.

    These tests pin the *loudness* rather than the mojibake: the
    operator is warned (``degraded=True``), which is the difference
    between a known limit and a silent corruption.
    """

    @pytest.mark.parametrize(
        ("codec", "corpus"),
        [
            (
                "iso8859-2",
                "jméno,město,poznámka\n"
                "Novák,Praha,Každé ráno chodí k řece a dlouho se dívá na vodu\n",
            ),
            (
                "cp1254",
                "ad,şehir,not\nAyşe,İstanbul,Her sabah nehrin kenar\u0131nda yürüyor\n",
            ),
        ],
        ids=["czech-iso8859-2", "turkish-cp1254"],
    )
    def test_a_latin_script_single_byte_codec_falls_back_and_says_so(
        self,
        codec: str,
        corpus: str,
    ) -> None:
        """The file is mis-decoded, but the warning fires.

        If one of these starts round-tripping, the rule got stronger —
        delete the row and say so in #1610 rather than leaving a stale
        limitation in the module docstring.

        Args:
            codec: Codec the corpus is written in.
            corpus: Source text.
        """
        result = decode_bytes(corpus.encode(codec))
        assert result.degraded is True, (
            f"{codec} now decodes without falling back. If the rule was "
            "extended, delete this row AND the 'what this deliberately does "
            "not fix' paragraph in the encoding module docstring"
        )
        assert result.codec == "cp1252"


class TestThePlacementScorerItself:
    """Unit-level guards for the scorer the accept rule is built on.

    :class:`TestTheAcceptRuleOnRealCorpora` exercises the rule
    end-to-end, which covers the two verdicts but not the reasoning
    that produces them: mutating four separate decisions inside
    :func:`~creek.ingest.encoding._placement_score` and its helpers
    leaves every corpus row green, because no row happens to contain
    the character shape each decision is about. These tests supply
    those shapes directly, so the helpers cannot be simplified into
    something that still passes the corpus table while scoring real
    files wrongly.
    """

    def test_a_letterlike_symbol_is_never_counted_against_a_decode(self) -> None:
        """``µ`` in ``5µg`` is a symbol, so its placement says nothing.

        It is alphabetic to Python but belongs to no writing system,
        and it sits beside a digit in perfectly correct text. Scoring
        it as misplaced would drive the *fallback's* score to zero and
        make every candidate trivially "no worse", which is the half
        of the rule that refuses Big5-for-``°C``.
        """
        assert _placement_score("AB,5µg", isolated_is_ok=False) == 1.0
        assert _placement_score("ª º ¹", isolated_is_ok=False) == 1.0

    def test_japanese_kana_and_kanji_count_as_one_writing_system(self) -> None:
        """``Tokyoの街`` places both characters, because Japanese mixes them.

        ``の`` is HIRAGANA and ``街`` is CJK by Unicode name. Without
        the alias fold they are two scripts, neither has a same-script
        neighbour, and correct Japanese scores 0.0 — which would make
        chardet's guess unable to beat any fallback on a real Japanese
        file.
        """
        assert _placement_score("Tokyoの街", isolated_is_ok=False) == 1.0

    def test_the_neighbour_on_the_right_counts_too(self) -> None:
        """``Á`` in ``5Ág`` is placed by the letter *after* it.

        Its left neighbour is a digit. If only the left side were
        consulted this would score 0.0, cp850-for-``5µg`` would be
        refused on placement, and the provenance test that actually
        refuses it would go untested — a passing suite for the wrong
        reason.
        """
        assert _placement_score("5Ág", isolated_is_ok=False) == 1.0
        assert _placement_score("Ág5", isolated_is_ok=False) == 1.0

    def test_a_neighbouring_punctuation_mark_is_not_a_same_script_letter(
        self,
    ) -> None:
        """``;`` is GREEK QUESTION MARK by name but is not a letter.

        :func:`~creek.ingest.encoding._script_family` reads the first
        word of the Unicode name, and several *punctuation* characters
        are named after a script. Accepting one as a same-script
        neighbour would let a mojibake letter next to mojibake
        punctuation vouch for itself.
        """
        greek_question_mark = "\u037e"
        assert unicodedata.name(greek_question_mark).startswith("GREEK")
        assert not greek_question_mark.isalpha()
        greek_alpha = "\u0391"
        assert (
            _placement_score(
                f"x{greek_alpha}{greek_question_mark}",
                isolated_is_ok=False,
            )
            == 0.0
        )

    def test_provenance_counts_letters_only(self) -> None:
        """A script-named punctuation mark does not introduce a script.

        Provenance asks whether the candidate proposes a writing system
        the cp1252 fallback cannot produce. A stray ``;`` named
        GREEK QUESTION MARK is not evidence of Greek, and counting it
        would admit exactly the Latin-codec guesses the rule refuses.
        """
        assert _script_families("a\u037e") == frozenset({"LATIN"})
        assert _script_families("\u0391\u03b2") == frozenset({"GREEK"})

    def test_an_all_ascii_decode_scores_perfectly(self) -> None:
        """Nothing to score means nothing to hold against the candidate."""
        assert _placement_score("id,name\n1,Alice\n", isolated_is_ok=False) == 1.0
