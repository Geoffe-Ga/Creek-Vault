"""Tests for the typography + markup-artifact sanitizer (FEAT-040.3)."""

from __future__ import annotations

from creek.config import AIStyleConfig
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.sanitize_typography import (
    normalize_typography,
    sanitize,
    strip_markup_artifacts,
)

_CONFIG = AIStyleConfig()
# A non-thin fingerprint where the user uses neither curly quotes nor em-dashes.
_PLAIN_USER = VoiceFingerprint(
    features={
        "curly_quote_density": FeatureStat(rate=0.0, support=50),
        "em_dash_density": FeatureStat(rate=0.0, support=50),
    },
    fragment_count=50,
)
# A non-thin fingerprint where the user genuinely uses both.
_ORNATE_USER = VoiceFingerprint(
    features={
        "curly_quote_density": FeatureStat(rate=8.0, support=50),
        "em_dash_density": FeatureStat(rate=6.0, support=50),
    },
    fragment_count=50,
)


class TestStripMarkupArtifacts:
    """Artifacts are stripped unconditionally."""

    def test_oaicite_contentreference(self) -> None:
        """The :contentReference[oaicite:N]{index=N} artifact is removed."""
        text = "The school is a center.:contentReference[oaicite:1]{index=1} Next."
        assert "oaicite" not in strip_markup_artifacts(text)
        assert "contentReference" not in strip_markup_artifacts(text)

    def test_turn_search_artifact(self) -> None:
        """citeturn0search1 / turn0image0 style artifacts are removed."""
        assert "turn0search1" not in strip_markup_artifacts("see citeturn0search1")
        assert "turn0image0" not in strip_markup_artifacts("imgs turn0image0 here")

    def test_lenticular_and_file_web_tokens(self) -> None:
        """Lenticular 【..】 and [attached_file:n]/[web:n] tokens are removed."""
        assert "【" not in strip_markup_artifacts("a claim【85†L1-2】")
        assert "attached_file" not in strip_markup_artifacts("x [attached_file:1]")
        assert "web:1" not in strip_markup_artifacts("y [web:1]")

    def test_grok_and_attribution(self) -> None:
        """grok-card / grok_render and attribution JSON are removed."""
        assert "grok-card" not in strip_markup_artifacts('a<grok-card data-id="x">')
        assert "attribution" not in strip_markup_artifacts(
            'born here.({"attribution":{"attributableIndex":"1009-1"}})',
        )

    def test_utm_stripped_url_preserved(self) -> None:
        """AI utm_source/referrer params go; the rest of the URL stays."""
        out = strip_markup_artifacts(
            "see https://example.com/p?id=7&utm_source=chatgpt.com here",
        )
        assert "utm_source" not in out
        assert "https://example.com/p?id=7" in out

    def test_negative_real_search_word_untouched(self) -> None:
        """A normal sentence with the word 'search' is not mangled."""
        text = "I will search the river bank tomorrow morning."
        assert strip_markup_artifacts(text) == text


class TestVaultAwareTypography:
    """Typography is normalised only against the user's grain."""

    def test_straightens_quotes_when_user_plain(self) -> None:
        """A plain user's curly quotes are straightened."""
        out = normalize_typography(
            "\u201cit\u2019s\u201d fine",
            fingerprint=_PLAIN_USER,
            config=_CONFIG,
        )
        assert out == '"it\'s" fine'

    def test_preserves_quotes_when_user_ornate(self) -> None:
        """A curly-quote user keeps their curly quotes."""
        text = "\u201cit\u2019s\u201d fine"
        out = normalize_typography(text, fingerprint=_ORNATE_USER, config=_CONFIG)
        assert out == text

    def test_demotes_spaced_em_dash_when_user_plain(self) -> None:
        """A plain user's spaced em-dash becomes a comma."""
        out = normalize_typography(
            "it was good — then better",
            fingerprint=_PLAIN_USER,
            config=_CONFIG,
        )
        assert "—" not in out
        assert "good, then better" in out

    def test_preserves_em_dash_when_user_uses_it(self) -> None:
        """An em-dash user keeps their em-dashes."""
        text = "it was good — then better"
        assert normalize_typography(text, fingerprint=_ORNATE_USER, config=_CONFIG) == (
            text
        )

    def test_thin_fingerprint_normalises_generically(self) -> None:
        """A thin fingerprint cannot vouch for the user → normalise."""
        thin = VoiceFingerprint(features={}, fragment_count=1)
        out = normalize_typography("\u201chi\u201d", fingerprint=thin, config=_CONFIG)
        assert out == '"hi"'


class TestSanitize:
    """The frontmatter-safe, idempotent aggregator."""

    def test_frontmatter_preserved(self) -> None:
        """The YAML frontmatter block is left byte-for-byte intact."""
        text = (
            "---\n"
            "id: frag-1\n"
            'title: "stays \u201ccurly\u201d here"\n'
            "---\n"
            "body \u201cquote\u201d turn0search0 end"
        )
        out = sanitize(text, fingerprint=_PLAIN_USER, config=_CONFIG)
        assert '---\nid: frag-1\ntitle: "stays \u201ccurly\u201d here"\n---\n' in out
        assert "turn0search0" not in out
        assert '"quote"' in out

    def test_idempotent(self) -> None:
        """Running the sanitizer twice equals running it once."""
        text = "a \u201cquote\u201d — and turn0search0 cruft"
        once = sanitize(text, fingerprint=_PLAIN_USER, config=_CONFIG)
        twice = sanitize(once, fingerprint=_PLAIN_USER, config=_CONFIG)
        assert once == twice
