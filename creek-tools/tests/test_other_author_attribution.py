"""Tests for other-author attribution on the vault-writer path (FEAT-041 #470).

When a fragment carries ``source.author_slug`` it is borrowed content governed
by ``11-Other-Authors/<slug>/_author.md``. The writer routes it under
``11-Other-Authors/<slug>/`` and stamps its ATTRIBUTION (author / author_slug /
representativeness / voice_weight) from that manifest, *failing closed* to
``voice_weight=0.0`` when the manifest is absent or unreadable. The fragment's
IDEAS classification (frequency / wavelength / voice, set upstream) is left
untouched. Native (non-borrowed) writes are unchanged — the tracer invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter as fm_mod
import pytest
from pydantic import ValidationError

from creek.models import (
    AuthorManifest,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)
from creek.vault.writer import VaultWriter, _apply_other_author_attribution

if TYPE_CHECKING:
    from pathlib import Path

_HUMAN_SOURCE_MANIFEST = """---
type: author_manifest
display_name: "Naval Ravikant"
author_kind: human_source
voice_weight: 0.0
representativeness: reference
default_privacy_tier: open
attribution_required: true
---

# Naval Ravikant
"""

_AI_AS_USER_MANIFEST = """---
type: author_manifest
display_name: "House AI"
author_kind: ai_as_user
voice_weight: 0.0
representativeness: reference
---

# House AI
"""

_ASPIRATIONAL_MANIFEST = """---
type: author_manifest
display_name: "Mentor"
author_kind: human_source
voice_weight: 0.3
representativeness: aspirational
---

# Mentor
"""


# ---- Fixtures ----


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal vault structure with a native fragment subfolder."""
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
    ):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def writer(vault_path: Path) -> VaultWriter:
    """Return a VaultWriter for the test vault."""
    return VaultWriter(vault_path=vault_path)


def _write_manifest(vault: Path, slug: str, frontmatter_body: str) -> None:
    """Seed ``11-Other-Authors/<slug>/_author.md`` with *frontmatter_body*."""
    folder = vault / "11-Other-Authors" / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "_author.md").write_text(frontmatter_body, encoding="utf-8")


def _classified_borrowed_fragment(slug: str, frag_id: str) -> Fragment:
    """Build a borrowed fragment with pre-set IDEAS classification."""
    return Fragment(
        id=frag_id,
        title="Borrowed Idea",
        source=FragmentSource(
            platform=SourcePlatform.SUBSTACK,
            author_slug=slug,
        ),
        frequency=FrequencyClassification(primary=Frequency.F3),
        wavelength=WavelengthClassification(phase=Phase.RISING),
    )


# ---- Borrowed-author routing + stamping ----


class TestOtherAuthorAttribution:
    """Borrowed fragments route under 11-Other-Authors and inherit attribution."""

    def test_human_source_routes_and_stamps_other(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """A human_source borrowed fragment lands under the slug folder as OTHER."""
        _write_manifest(vault_path, "naval-ravikant", _HUMAN_SOURCE_MANIFEST)
        frag = _classified_borrowed_fragment("naval-ravikant", "frag-naval0001")

        result = writer.write_fragment(frag)

        assert "11-Other-Authors/naval-ravikant" in str(result)
        assert "01-Fragments" not in str(result)
        post = fm_mod.load(str(result))
        assert post["source"]["author"] == "other"
        assert post["source"]["author_slug"] == "naval-ravikant"
        assert post["voice_weight"] == 0.0

    def test_human_source_preserves_ideas_classification(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """Stamping attribution does NOT clear the frequency/phase (ideas) tags."""
        _write_manifest(vault_path, "naval-ravikant", _HUMAN_SOURCE_MANIFEST)
        frag = _classified_borrowed_fragment("naval-ravikant", "frag-naval0002")

        result = writer.write_fragment(frag)

        post = fm_mod.load(str(result))
        assert post["frequency"]["primary"] == "F3"
        assert post["wavelength"]["phase"] == "rising"

    def test_ai_as_user_stamps_ai_and_forces_endorsed(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """An ai_as_user manifest yields author=ai and forced representativeness."""
        _write_manifest(vault_path, "house-ai", _AI_AS_USER_MANIFEST)
        frag = _classified_borrowed_fragment("house-ai", "frag-houseai01")

        result = writer.write_fragment(frag)

        post = fm_mod.load(str(result))
        assert post["source"]["author"] == "ai"
        assert post["representativeness"] == "endorsed"
        assert post["voice_weight"] == 0.0

    def test_representativeness_and_weight_read_from_manifest(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """A human_source manifest's representativeness/voice_weight are inherited."""
        _write_manifest(vault_path, "mentor", _ASPIRATIONAL_MANIFEST)
        frag = _classified_borrowed_fragment("mentor", "frag-mentor001")

        result = writer.write_fragment(frag)

        post = fm_mod.load(str(result))
        assert post["representativeness"] == "aspirational"
        assert post["voice_weight"] == 0.3
        assert post["source"]["author"] == "other"

    def test_missing_manifest_fails_closed_to_zero_weight(
        self,
        writer: VaultWriter,
    ) -> None:
        """A slug with no _author.md still writes, fail-closed to voice_weight=0.0."""
        frag = _classified_borrowed_fragment("no-manifest", "frag-nomani001")

        result = writer.write_fragment(frag)

        assert "11-Other-Authors/no-manifest" in str(result)
        post = fm_mod.load(str(result))
        assert post["voice_weight"] == 0.0
        assert post["source"]["author"] == "other"
        assert post["source"]["author_slug"] == "no-manifest"
        assert post["representativeness"] == "reference"

    def test_garbage_manifest_fails_closed_to_zero_weight(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """A garbage voice_weight in _author.md fails closed to 0.0, not a crash."""
        body = _HUMAN_SOURCE_MANIFEST.replace(
            "voice_weight: 0.0",
            'voice_weight: "banana"',
        )
        _write_manifest(vault_path, "garbage", body)
        frag = _classified_borrowed_fragment("garbage", "frag-garbage01")

        result = writer.write_fragment(frag)

        post = fm_mod.load(str(result))
        assert post["voice_weight"] == 0.0
        assert post["source"]["author"] == "other"

    def test_malformed_yaml_manifest_fails_closed_to_zero_weight(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """An _author.md with unparseable YAML fails closed, not crashes (#470)."""
        # Unbalanced flow sequence -> yaml.YAMLError during frontmatter parse,
        # raised before the model's per-field fail-closed validators run.
        malformed = "---\nvoice_weight: [0.0\ndisplay_name: broken\n---\n# x\n"
        _write_manifest(vault_path, "malformed", malformed)
        frag = _classified_borrowed_fragment("malformed", "frag-malformed01")

        result = writer.write_fragment(frag)

        post = fm_mod.load(str(result))
        assert post["voice_weight"] == 0.0
        assert post["source"]["author"] == "other"
        assert post["source"]["author_slug"] == "malformed"
        assert post["representativeness"] == "reference"

    def test_write_does_not_mutate_caller_fragment(
        self,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """Stamping attribution must not mutate the caller's Fragment (#470)."""
        _write_manifest(vault_path, "mentor", _ASPIRATIONAL_MANIFEST)
        frag = _classified_borrowed_fragment("mentor", "frag-nomutate01")

        writer.write_fragment(frag)

        # The caller's object keeps its pre-call (native-default) attribution —
        # only the written file carries the stamped manifest values.
        assert frag.voice_weight == 1.0
        assert frag.representativeness == "self"
        assert frag.source.author == "self"
        assert frag.source.author_slug == "mentor"

    def test_native_write_is_unchanged(
        self,
        writer: VaultWriter,
    ) -> None:
        """A native fragment (author_slug=None) routes to 01-Fragments, untouched."""
        frag = Fragment(
            id="frag-native001",
            title="Native Fragment",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )

        result = writer.write_fragment(frag)

        assert "01-Fragments/Conversations" in str(result)
        assert "11-Other-Authors" not in str(result)
        post = fm_mod.load(str(result))
        # Native model defaults survive: SELF author, full voice weight.
        assert post["source"]["author"] == "self"
        assert post["source"]["author_slug"] is None
        assert post["voice_weight"] == 1.0
        assert post["representativeness"] == "self"


def test_apply_other_author_attribution_returns_new_fragment_without_mutating() -> None:
    """The helper is pure: it returns a stamped copy and never mutates its input.

    The writer's deep-copy guard (#470) already protected callers end-to-end,
    but the helper itself was an in-place mutator — a footgun for any future
    caller. It now returns a fresh Fragment so caller-mutation can't be
    reintroduced (#500).
    """
    frag = _classified_borrowed_fragment("mentor", "frag-pure0001")
    manifest = AuthorManifest(
        author_slug="mentor",
        author_kind="human_source",
        voice_weight=0.3,
        representativeness="aspirational",
    )

    stamped = _apply_other_author_attribution(frag, manifest)

    # A distinct object (and distinct nested source) is returned.
    assert stamped is not frag
    assert stamped.source is not frag.source
    # The returned copy carries the manifest attribution.
    assert stamped.voice_weight == 0.3
    assert stamped.representativeness == "aspirational"
    assert stamped.source.author == "other"
    assert stamped.source.author_slug == "mentor"
    # The caller's fragment keeps its native defaults — never mutated.
    assert frag.voice_weight == 1.0
    assert frag.representativeness == "self"
    assert frag.source.author == "self"


class TestAuthorSlugPathSafety:
    """``FragmentSource.author_slug`` is interpolated into vault paths (#470).

    A slug carrying path separators or ``..`` could escape
    ``11-Other-Authors/`` (path traversal), so the model rejects anything that
    is not a single safe path segment at the data boundary.
    """

    @pytest.mark.parametrize(
        "bad_slug",
        [
            "../evil",
            "../../etc/passwd",
            "naval/../../escape",
            "nested/slug",
            "back\\slash",
            "/absolute",
            "..",
            ".",
            "",
            "   ",
            "embedded space",
        ],
    )
    def test_unsafe_author_slug_is_rejected(self, bad_slug: str) -> None:
        """A traversal / multi-segment author_slug raises ValidationError."""
        with pytest.raises(ValidationError):
            FragmentSource(platform=SourcePlatform.SUBSTACK, author_slug=bad_slug)

    @pytest.mark.parametrize("good_slug", ["naval-ravikant", "ai-as-user", "a_b.c1"])
    def test_safe_author_slug_is_accepted(self, good_slug: str) -> None:
        """A normal single-segment slug is accepted unchanged."""
        source = FragmentSource(platform=SourcePlatform.SUBSTACK, author_slug=good_slug)
        assert source.author_slug == good_slug

    def test_none_author_slug_is_accepted(self) -> None:
        """``None`` (a native fragment) is always valid."""
        source = FragmentSource(platform=SourcePlatform.CLAUDE)
        assert source.author_slug is None
