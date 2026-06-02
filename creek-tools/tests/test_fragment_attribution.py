"""Tests for the two-axis attribution fields on ``Fragment`` (FEAT-041 #462).

The fields are purely additive: legacy fragments (no attribution block) load
with native-fragment defaults, and a fragment carrying the new fields
round-trips through the writer/reader unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.models import Authorship, Fragment, FragmentSource, SourcePlatform
from creek.scaffold import scaffold_vault
from creek.vault.reader import try_load_fragment
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

_LEGACY_FRONTMATTER: dict[str, object] = {
    "type": "fragment",
    "id": "frag-legacy-0001",
    "title": "A pre-FEAT-041 fragment",
    "source": {"platform": "journal", "author": "self"},
}


def test_legacy_fragment_gets_native_defaults() -> None:
    """A fragment with no attribution block loads with native defaults."""
    fragment = Fragment.model_validate(_LEGACY_FRONTMATTER)

    assert fragment.voice_weight == 1.0
    assert fragment.representativeness == "self"
    assert fragment.source.author_slug is None


def test_bad_voice_weight_fails_closed() -> None:
    """A non-numeric or out-of-range ``voice_weight`` fails closed to 1.0."""
    garbage = {**_LEGACY_FRONTMATTER, "voice_weight": "banana"}
    out_of_range = {**_LEGACY_FRONTMATTER, "voice_weight": 5.0}

    assert Fragment.model_validate(garbage).voice_weight == 1.0
    assert Fragment.model_validate(out_of_range).voice_weight == 1.0


def test_bad_representativeness_fails_closed() -> None:
    """An unrecognised ``representativeness`` fails closed to ``self``."""
    bad = {**_LEGACY_FRONTMATTER, "representativeness": "vibes"}

    assert Fragment.model_validate(bad).representativeness == "self"


def _other_author_fragment() -> Fragment:
    """Build an other-author fragment with the new attribution fields set."""
    return Fragment(
        id="frag-naval-0001",
        title="On leverage",
        source=FragmentSource(
            platform=SourcePlatform.MARKDOWN,
            author=Authorship.OTHER,
            author_slug="naval-ravikant",
        ),
        voice_weight=0.0,
        representativeness="reference",
    )


def test_new_attribution_fields_round_trip(tmp_path: Path) -> None:
    """A fragment with the new fields round-trips through write → read."""
    scaffold_vault(tmp_path)
    writer = VaultWriter(vault_path=tmp_path)
    path = writer.write_fragment(_other_author_fragment(), body="leverage body")

    record = try_load_fragment(path)
    assert record is not None
    loaded, _body, _meta = record

    assert loaded.source.author_slug == "naval-ravikant"
    assert loaded.voice_weight == 0.0
    assert loaded.representativeness == "reference"
    assert loaded.source.author == "other"


def test_native_fragment_round_trips_with_defaults(tmp_path: Path) -> None:
    """A native fragment with no explicit attribution keeps its defaults."""
    native = Fragment(
        id="frag-native-0001",
        title="My own note",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
    )
    scaffold_vault(tmp_path)
    writer = VaultWriter(vault_path=tmp_path)
    path = writer.write_fragment(native, body="native body")

    record = try_load_fragment(path)
    assert record is not None
    loaded, _body, _meta = record

    assert loaded.voice_weight == 1.0
    assert loaded.representativeness == "self"
    assert loaded.source.author_slug is None
