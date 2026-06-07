"""Tests for the ``AuthorManifest`` model and ``_author.md`` loader (FEAT-041 #458).

The manifest is the attribution record for an entry under ``11-Other-Authors/``.
The loader parses ``<slug>/_author.md`` frontmatter and *fails closed*: malformed
attribution fields resolve to conservative defaults rather than raising, so a
hand-edited manifest can never crash the pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

import pytest
import yaml
from pydantic import ValidationError

from creek.models import AuthorKind, AuthorManifest, PrivacyTier, Representativeness
from creek.vault import authors
from creek.vault.authors import (
    load_author_manifest,
    load_author_manifest_or_default,
)

if TYPE_CHECKING:
    from pathlib import Path

_MALFORMED_YAML = "---\nauthor_kind: human_source\n  bad: indent\n: : :\n---\n# body\n"


def _a_validation_error() -> ValidationError:
    """Capture a genuine ``ValidationError`` to exercise the loader branch."""
    with pytest.raises(ValidationError) as exc_info:
        AuthorManifest.model_validate(123)
    return exc_info.value


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


def test_attribution_required_fails_closed_on_non_bool(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-boolean ``attribution_required`` fails closed to True, loudly."""
    body = _FULL_MANIFEST.replace(
        "attribution_required: true", "attribution_required: maybe"
    )
    path = _write_manifest(tmp_path, "bad-attr", body)

    with caplog.at_level("WARNING"):
        manifest = load_author_manifest(path)

    assert manifest.attribution_required is True
    # The fail-closed path is loud, never silent.
    assert any(
        "attribution_required" in record.message and "failing closed" in record.message
        for record in caplog.records
    )


def test_attribution_required_false_is_preserved(tmp_path: Path) -> None:
    """A genuine ``false`` for ``attribution_required`` is kept, not forced True."""
    body = _FULL_MANIFEST.replace(
        "attribution_required: true", "attribution_required: false"
    )
    path = _write_manifest(tmp_path, "attr-false", body)

    assert load_author_manifest(path).attribution_required is False


def test_attribution_required_numeric_one_is_true(tmp_path: Path) -> None:
    """YAML integer ``1`` is honoured as ``True`` (require attribution), not coerced."""
    body = _FULL_MANIFEST.replace(
        "attribution_required: true", "attribution_required: 1"
    )
    path = _write_manifest(tmp_path, "attr-one", body)

    assert load_author_manifest(path).attribution_required is True


def test_attribution_required_numeric_zero_is_false(tmp_path: Path) -> None:
    """YAML integer ``0`` carries unambiguous intent and is honoured as ``False``."""
    body = _FULL_MANIFEST.replace(
        "attribution_required: true", "attribution_required: 0"
    )
    path = _write_manifest(tmp_path, "attr-zero", body)

    assert load_author_manifest(path).attribution_required is False


def test_attribution_required_other_int_fails_closed(tmp_path: Path) -> None:
    """An out-of-domain integer (e.g. ``2``) is ambiguous and fails closed to True."""
    body = _FULL_MANIFEST.replace(
        "attribution_required: true", "attribution_required: 2"
    )
    path = _write_manifest(tmp_path, "attr-two", body)

    assert load_author_manifest(path).attribution_required is True


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


def test_load_or_default_returns_parsed_manifest(tmp_path: Path) -> None:
    """``load_author_manifest_or_default`` returns the parsed manifest when present."""
    _write_manifest(tmp_path, "naval-ravikant", _FULL_MANIFEST)

    manifest = load_author_manifest_or_default(tmp_path, "naval-ravikant")

    assert manifest.author_slug == "naval-ravikant"
    assert manifest.display_name == "Naval Ravikant"


def test_load_or_default_fails_closed_when_missing(tmp_path: Path) -> None:
    """A missing ``_author.md`` yields a safe default manifest, not a raise."""
    manifest = load_author_manifest_or_default(tmp_path, "ghost-author")

    assert manifest.author_slug == "ghost-author"
    assert manifest.author_kind == "human_source"
    assert manifest.voice_weight == 0.0
    assert manifest.representativeness == "reference"


def test_load_or_default_fails_closed_on_malformed_yaml(tmp_path: Path) -> None:
    """Malformed manifest YAML fails closed to a safe default, not a raise."""
    _write_manifest(tmp_path, "garbled", _MALFORMED_YAML)

    manifest = load_author_manifest_or_default(tmp_path, "garbled")

    assert manifest == AuthorManifest(author_slug="garbled")


@pytest.mark.parametrize(
    "error",
    [OSError("disk"), yaml.YAMLError("bad"), _a_validation_error()],
    ids=["oserror", "yamlerror", "validationerror"],
)
def test_load_or_default_fails_closed_on_loader_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Every fail-closed branch resolves to a safe default manifest (#500).

    Drives each ``except`` arm of ``load_author_manifest_or_default`` directly
    at the loader level (rather than only end-to-end through the writer) by
    making the inner parse raise the corresponding error.
    """

    def _raise(_path: Path) -> AuthorManifest:
        raise error

    monkeypatch.setattr(authors, "load_author_manifest", _raise)

    manifest = load_author_manifest_or_default(tmp_path, "slug")

    assert manifest == AuthorManifest(author_slug="slug")


def test_load_or_default_fails_closed_when_present_but_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present-but-unreadable ``_author.md`` fails closed like a missing one.

    ``is_file()`` returns True for a permission-locked file, so the *read* (not
    the existence check) is what fails. The resulting ``PermissionError`` (an
    ``OSError``) is caught and normalised to the same safe default a missing
    manifest produces — confirming the two paths agree (#500 item 3).
    """
    _write_manifest(tmp_path, "locked", _FULL_MANIFEST)

    def _deny(_path: str) -> object:
        raise PermissionError("locked")

    monkeypatch.setattr(authors.frontmatter, "load", _deny)

    manifest = load_author_manifest_or_default(tmp_path, "locked")

    assert manifest == AuthorManifest(author_slug="locked")
