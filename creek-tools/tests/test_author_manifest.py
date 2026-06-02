"""Tests for the ``AuthorManifest`` model and ``_author.md`` loader (FEAT-041 #458).

The manifest is the attribution record for an entry under ``11-Other-Authors/``.
The loader parses ``<slug>/_author.md`` frontmatter and *fails closed*: malformed
attribution fields resolve to conservative defaults rather than raising, so a
hand-edited manifest can never crash the pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

import pytest

from creek.models import AuthorKind, AuthorManifest, PrivacyTier, Representativeness
from creek.vault.authors import load_author_manifest

if TYPE_CHECKING:
    from pathlib import Path

_FULL_MANIFEST = """---
type: author_manifest
author_slug: "ignored-in-favour-of-folder"
display_name: "Naval Ravikant"
author_kind: human_source
voice_weight: 0.0
representativeness: reference
default_privacy_tier: open
attribution_required: true
notes: "Captured for ideas, not voice."
---

# Naval Ravikant
"""


def _write_manifest(vault: Path, slug: str, frontmatter_body: str) -> Path:
    """Write *frontmatter_body* to ``11-Other-Authors/<slug>/_author.md``."""
    folder = vault / "11-Other-Authors" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "_author.md"
    path.write_text(frontmatter_body, encoding="utf-8")
    return path


def test_load_valid_manifest_round_trips(tmp_path: Path) -> None:
    """A well-formed manifest loads with all fields intact."""
    path = _write_manifest(tmp_path, "naval-ravikant", _FULL_MANIFEST)

    manifest = load_author_manifest(path)

    assert isinstance(manifest, AuthorManifest)
    assert manifest.display_name == "Naval Ravikant"
    assert manifest.author_kind == "human_source"
    assert manifest.voice_weight == 0.0
    assert manifest.representativeness == "reference"
    assert manifest.attribution_required is True


def test_slug_taken_from_folder_name(tmp_path: Path) -> None:
    """The author slug is the folder name (authority), not the frontmatter value."""
    path = _write_manifest(tmp_path, "naval-ravikant", _FULL_MANIFEST)

    manifest = load_author_manifest(path)

    assert manifest.author_slug == "naval-ravikant"


def test_voice_weight_fails_closed_on_garbage(tmp_path: Path) -> None:
    """A non-numeric ``voice_weight`` resolves to 0.0 without raising."""
    body = _FULL_MANIFEST.replace("voice_weight: 0.0", 'voice_weight: "banana"')
    path = _write_manifest(tmp_path, "garbage-weight", body)

    manifest = load_author_manifest(path)

    assert manifest.voice_weight == 0.0


def test_voice_weight_fails_closed_when_out_of_range(tmp_path: Path) -> None:
    """A ``voice_weight`` outside [0, 1] fails closed to 0.0."""
    body = _FULL_MANIFEST.replace("voice_weight: 0.0", "voice_weight: 5.0")
    path = _write_manifest(tmp_path, "over-weight", body)

    assert load_author_manifest(path).voice_weight == 0.0


def test_voice_weight_in_range_is_preserved(tmp_path: Path) -> None:
    """A valid in-range ``voice_weight`` survives loading."""
    body = _FULL_MANIFEST.replace("voice_weight: 0.0", "voice_weight: 0.5")
    path = _write_manifest(tmp_path, "half-weight", body)

    assert load_author_manifest(path).voice_weight == 0.5


def test_author_kind_fails_closed(tmp_path: Path) -> None:
    """An unrecognised ``author_kind`` fails closed to ``human_source``."""
    body = _FULL_MANIFEST.replace("author_kind: human_source", "author_kind: wizard")
    path = _write_manifest(tmp_path, "bad-kind", body)

    assert load_author_manifest(path).author_kind == "human_source"


def test_representativeness_defaults_to_reference_when_missing(tmp_path: Path) -> None:
    """A missing ``representativeness`` defaults to ``reference``."""
    body = _FULL_MANIFEST.replace("representativeness: reference\n", "")
    path = _write_manifest(tmp_path, "no-representativeness", body)

    assert load_author_manifest(path).representativeness == "reference"


def test_representativeness_fails_closed_on_garbage(tmp_path: Path) -> None:
    """An invalid ``representativeness`` fails closed to ``reference``."""
    body = _FULL_MANIFEST.replace(
        "representativeness: reference", "representativeness: vibes"
    )
    path = _write_manifest(tmp_path, "bad-representativeness", body)

    assert load_author_manifest(path).representativeness == "reference"


def test_missing_voice_weight_defaults_zero(tmp_path: Path) -> None:
    """A missing ``voice_weight`` defaults to 0.0."""
    body = _FULL_MANIFEST.replace("voice_weight: 0.0\n", "")
    path = _write_manifest(tmp_path, "no-weight", body)

    assert load_author_manifest(path).voice_weight == 0.0


def test_default_privacy_tier_parses(tmp_path: Path) -> None:
    """``default_privacy_tier`` resolves to the matching :class:`PrivacyTier`."""
    path = _write_manifest(tmp_path, "tiered", _FULL_MANIFEST)

    assert load_author_manifest(path).default_privacy_tier == PrivacyTier.OPEN


def test_default_privacy_tier_fails_closed_on_garbage(tmp_path: Path) -> None:
    """An unrecognised ``default_privacy_tier`` fails closed to OPEN, not raising."""
    body = _FULL_MANIFEST.replace(
        "default_privacy_tier: open", "default_privacy_tier: banana"
    )
    path = _write_manifest(tmp_path, "bad-tier", body)

    assert load_author_manifest(path).default_privacy_tier == PrivacyTier.OPEN


def test_attribution_required_fails_closed_on_non_bool(tmp_path: Path) -> None:
    """A non-boolean ``attribution_required`` fails closed to True (require it)."""
    body = _FULL_MANIFEST.replace(
        "attribution_required: true", "attribution_required: maybe"
    )
    path = _write_manifest(tmp_path, "bad-attr", body)

    assert load_author_manifest(path).attribution_required is True


def test_attribution_required_false_is_preserved(tmp_path: Path) -> None:
    """A genuine ``false`` for ``attribution_required`` is kept, not forced True."""
    body = _FULL_MANIFEST.replace(
        "attribution_required: true", "attribution_required: false"
    )
    path = _write_manifest(tmp_path, "attr-false", body)

    assert load_author_manifest(path).attribution_required is False


def test_null_privacy_tier_fails_closed(tmp_path: Path) -> None:
    """An explicit ``null`` ``default_privacy_tier`` fails closed to OPEN."""
    body = _FULL_MANIFEST.replace(
        "default_privacy_tier: open", "default_privacy_tier: null"
    )
    path = _write_manifest(tmp_path, "null-tier", body)

    assert load_author_manifest(path).default_privacy_tier == PrivacyTier.OPEN


def test_loader_is_exported_from_vault_package() -> None:
    """``load_author_manifest`` is reachable from the ``creek.vault`` surface."""
    from creek.vault import load_author_manifest as exported

    assert exported is load_author_manifest


def test_missing_file_raises(tmp_path: Path) -> None:
    """A missing manifest path raises a clear ``FileNotFoundError``."""
    missing = tmp_path / "11-Other-Authors" / "ghost" / "_author.md"

    with pytest.raises(FileNotFoundError, match="Author manifest not found"):
        load_author_manifest(missing)


def test_every_literal_author_kind_is_accepted() -> None:
    """All declared ``AuthorKind`` values round-trip (guards frozenset drift)."""
    for kind in get_args(AuthorKind):
        manifest = AuthorManifest.model_validate(
            {"author_slug": "x", "author_kind": kind}
        )
        assert manifest.author_kind == kind


def test_every_literal_representativeness_is_accepted() -> None:
    """All declared ``Representativeness`` values round-trip (guards drift)."""
    for value in get_args(Representativeness):
        manifest = AuthorManifest.model_validate(
            {"author_slug": "x", "representativeness": value}
        )
        assert manifest.representativeness == value


def test_null_fields_fail_closed() -> None:
    """Explicit ``null`` attribution values fail closed to safe defaults."""
    manifest = AuthorManifest.model_validate(
        {
            "author_slug": "x",
            "author_kind": None,
            "voice_weight": None,
            "representativeness": None,
        }
    )

    assert manifest.author_kind == "human_source"
    assert manifest.voice_weight == 0.0
    assert manifest.representativeness == "reference"


def test_model_validate_coerces_directly() -> None:
    """``AuthorManifest.model_validate`` applies the same fail-closed coercion."""
    manifest = AuthorManifest.model_validate(
        {"author_slug": "x", "voice_weight": "nope", "author_kind": "alien"}
    )

    assert manifest.voice_weight == 0.0
    assert manifest.author_kind == "human_source"
    assert manifest.representativeness == "reference"
