"""Tests for the idiolect / voice-fingerprint profiler (FEAT-040.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import (
    build_fingerprint,
    extract_user_turns,
    load_fingerprint,
    save_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG = AIStyleConfig()


def _write_fragment(
    vault: Path,
    name: str,
    *,
    author: str = "self",
    platform: str = "markdown",
    tier: str = "open",
    body: str = "A short honest sentence about the river.",
) -> None:
    """Write a minimal fragment .md with the given source frontmatter."""
    path = vault / "01-Fragments" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    front = (
        "---\n"
        "source:\n"
        f"  author: {author}\n"
        f"  platform: {platform}\n"
        f"privacy_tier: {tier}\n"
        "---\n"
        f"{body}\n"
    )
    path.write_text(front, encoding="utf-8")


_CHATGPT_BODY = (
    "# Conversation (turn 1)\n\n"
    "> **User**: I reckon it was fine and that was that\n"
    ">\n"
    "> **Assistant**: a vibrant tapestry that delve into the intricate "
    "and pivotal underscore of it all\n"
)


class TestExtractUserTurns:
    """The user-turn splitter underpinning the authorship filter."""

    def test_extracts_only_user(self) -> None:
        """Assistant text is dropped; user text is kept."""
        user = extract_user_turns(_CHATGPT_BODY)
        assert user is not None
        assert "I reckon it was fine" in user
        assert "tapestry" not in user
        assert "Assistant" not in user

    def test_no_user_marker_returns_none(self) -> None:
        """A body with no User marker cannot be split → excluded."""
        assert extract_user_turns("# Title\n\njust some prose, no markers") is None


class TestAuthorshipFilter:
    """The decisive correctness property: never fingerprint AI text."""

    def test_assistant_turn_excluded_from_fingerprint(self, tmp_path: Path) -> None:
        """A chatgpt fragment contributes only its user-turn.

        The assistant half is dense with AI vocabulary; the user half has
        none. If the filter works, ``ai_vocab_density`` is 0.0.
        """
        _write_fragment(tmp_path, "c.md", platform="chatgpt", body=_CHATGPT_BODY)
        fp = build_fingerprint(tmp_path, _CONFIG)
        assert fp.fragment_count == 1
        assert fp.rate_for("ai_vocab_density") == 0.0

    def test_non_self_author_excluded(self, tmp_path: Path) -> None:
        """AI- and other-authored fragments never enter the fingerprint."""
        _write_fragment(tmp_path, "self.md", author="self")
        _write_fragment(tmp_path, "ai.md", author="ai", body="a vibrant tapestry")
        _write_fragment(tmp_path, "other.md", author="other", body="delve delve")
        fp = build_fingerprint(tmp_path, _CONFIG)
        assert fp.fragment_count == 1

    def test_intimate_excluded_by_default(self, tmp_path: Path) -> None:
        """Intimate-tier fragments are excluded unless explicitly included."""
        _write_fragment(tmp_path, "i.md", tier="intimate")
        assert build_fingerprint(tmp_path, _CONFIG).fragment_count == 0
        included = build_fingerprint(tmp_path, _CONFIG, include_intimate=True)
        assert included.fragment_count == 1


class TestBuild:
    """Rate computation and corpus handling."""

    def test_rates_match_hand_computed(self, tmp_path: Path) -> None:
        """A known body yields the hand-computed em-dash rate."""
        body = "— — " + " ".join(["word"] * 998)
        _write_fragment(tmp_path, "m.md", body=body)
        fp = build_fingerprint(tmp_path, _CONFIG)
        rate = fp.rate_for("em_dash_density")
        assert rate is not None
        assert abs(rate - 2.0) < 0.05

    def test_empty_vault_yields_empty_fingerprint(self, tmp_path: Path) -> None:
        """No fragments → fragment_count 0, no features."""
        fp = build_fingerprint(tmp_path, _CONFIG)
        assert fp.fragment_count == 0
        assert fp.features == {}

    def test_platform_weighting(self, tmp_path: Path) -> None:
        """A higher-weighted platform pulls the mean toward its rate.

        A journal (weight 1.0) full of em-dashes plus a chatgpt user-turn
        (weight 0.5) with none should yield a weighted mean above the plain
        average — i.e. the journal dominates.
        """
        _write_fragment(
            tmp_path,
            "j.md",
            platform="journal",
            body="— — " + " ".join(["w"] * 98),
        )
        _write_fragment(
            tmp_path,
            "c.md",
            platform="chatgpt",
            body="# c\n\n> **User**: no dashes here at all\n",
        )
        fp = build_fingerprint(tmp_path, _CONFIG)
        # journal em-dash rate ≈ 20/1k; chatgpt ≈ 0. Weighted (1.0,0.5):
        # (1.0*20 + 0.5*0)/1.5 ≈ 13.3 > plain mean 10.
        rate = fp.rate_for("em_dash_density")
        assert rate is not None
        assert rate > 11.0


class TestPersistence:
    """Round-trip to the vault JSON artifact."""

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        """A saved fingerprint loads back with identical data."""
        _write_fragment(tmp_path, "m.md", body="— a quick note about the day")
        built = build_fingerprint(tmp_path, _CONFIG)
        path = save_fingerprint(built, tmp_path, _CONFIG)
        assert path.exists()
        loaded = load_fingerprint(tmp_path, _CONFIG)
        assert loaded.fragment_count == built.fragment_count
        assert loaded.rate_for("em_dash_density") == built.rate_for("em_dash_density")

    def test_load_absent_returns_empty(self, tmp_path: Path) -> None:
        """Loading when no file exists yields an empty fingerprint."""
        loaded = load_fingerprint(tmp_path, _CONFIG)
        assert loaded.fragment_count == 0
        assert loaded.features == {}

    def test_load_corrupted_json_returns_empty(self, tmp_path: Path) -> None:
        """A corrupted fingerprint file is ignored, not raised on."""
        path = tmp_path / _CONFIG.fingerprint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        loaded = load_fingerprint(tmp_path, _CONFIG)
        assert loaded.fragment_count == 0
        assert loaded.features == {}

    def test_load_version_mismatch_returns_empty(self, tmp_path: Path) -> None:
        """A stale-schema fingerprint is ignored so a rebuild is triggered."""
        import json

        path = tmp_path / _CONFIG.fingerprint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 999, "fragment_count": 3, "features": {}}),
            encoding="utf-8",
        )
        assert load_fingerprint(tmp_path, _CONFIG).fragment_count == 0


def test_build_skips_malformed_frontmatter(tmp_path: Path) -> None:
    """A fragment with broken YAML frontmatter is skipped, not fatal."""
    good = tmp_path / "01-Fragments" / "good.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text(
        "---\nsource:\n  author: self\n  platform: markdown\n---\nclean note\n",
        encoding="utf-8",
    )
    bad = tmp_path / "01-Fragments" / "bad.md"
    bad.write_text("---\nsource: {unbalanced: [\n---\nbody\n", encoding="utf-8")
    fp = build_fingerprint(tmp_path, _CONFIG)
    assert fp.fragment_count == 1


def test_fingerprint_defensively_copies_features() -> None:
    """Mutating the source dict after construction does not alter the fp."""
    from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint

    src = {"em_dash_density": FeatureStat(rate=1.0, support=3)}
    fp = VoiceFingerprint(features=src, fragment_count=3)
    src["em_dash_density"] = FeatureStat(rate=999.0, support=3)
    assert fp.rate_for("em_dash_density") == 1.0
