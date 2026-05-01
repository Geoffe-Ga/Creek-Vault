"""Property-based tests for vault frontmatter round-tripping.

The vault writer serialises a Pydantic model as YAML frontmatter and the
reader (``frontmatter.load``) parses it back. The contract: a model
written and re-read must yield equivalent ``model_dump(mode="json")``
output. Hypothesis generates Fragment instances with arbitrary titles,
tags, and timestamps to surface YAML escaping bugs that hand-rolled
fixtures miss.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    SourcePlatform,
)
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import TempPathFactory

_PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _make_vault(root: Path) -> Path:
    """Build the minimal directory structure VaultWriter requires."""
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
        "01-Fragments/Unsorted",
    ):
        (root / d).mkdir(parents=True, exist_ok=True)
    return root


_TITLES = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),
        blacklist_characters="\x00",
    ),
    min_size=1,
    max_size=80,
).filter(lambda s: s.strip() != "")

_TAGS = st.lists(
    st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N"),
            whitelist_characters="-_",
        ),
        min_size=1,
        max_size=20,
    ),
    max_size=8,
)

_TIMESTAMPS = st.datetimes(
    timezones=st.just(UTC),
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
)

# All SourcePlatform members — using list(SourcePlatform) means a new
# platform added in models.py is automatically covered by these
# property tests without a separate update here.
_PLATFORMS = st.sampled_from(list(SourcePlatform))


@st.composite
def fragments(draw: st.DrawFn) -> Fragment:
    """Build a Fragment instance from primitive strategies."""
    return Fragment(
        id=draw(
            st.from_regex(r"frag-[0-9a-f]{12}", fullmatch=True),
        ),
        title=draw(_TITLES),
        source=FragmentSource(
            platform=draw(_PLATFORMS),
            author=Authorship.SELF,
        ),
        created=draw(_TIMESTAMPS),
        ingested=draw(_TIMESTAMPS),
        tags=draw(_TAGS),
    )


@_PROFILE
@given(fragment=fragments())
def test_fragment_frontmatter_roundtrip(
    fragment: Fragment, tmp_path_factory: TempPathFactory
) -> None:
    """Every field round-trips through frontmatter unchanged.

    YAML loads scalars as native Python types (datetimes, ints, bools)
    while ``model_dump(mode="json")`` produces JSON-friendly strings.
    Comparison is done via re-validation through Fragment so the
    contract is "the fragment we wrote equals the fragment we re-load,"
    not "the literal YAML matches the JSON dump."
    """
    vault = _make_vault(tmp_path_factory.mktemp("vault"))
    writer = VaultWriter(vault_path=vault)
    written_path = writer.write_fragment(fragment)

    post = frontmatter.load(str(written_path))
    parsed = post.metadata
    expected = fragment.model_dump(mode="json")

    # Every expected key must be present after the round-trip.
    for key in expected:
        assert key in parsed, f"Missing key {key!r} in frontmatter"

    # Re-validate the loaded metadata back into a Fragment and compare
    # field-by-field on the JSON-mode dump. This catches scalar
    # round-trip bugs (None title, truncated id, dropped tags) that a
    # dict/list-only assertion would miss.
    reloaded = Fragment.model_validate(parsed)
    actual = reloaded.model_dump(mode="json")
    for key, value in expected.items():
        assert actual[key] == value, (
            f"Round-trip mismatch on {key!r}: {actual[key]!r} != {value!r}"
        )


@_PROFILE
@given(fragment=fragments())
def test_fragment_id_survives_roundtrip(
    fragment: Fragment, tmp_path_factory: TempPathFactory
) -> None:
    """The ``id`` field must survive verbatim — it's the dedup key."""
    vault = _make_vault(tmp_path_factory.mktemp("vault"))
    writer = VaultWriter(vault_path=vault)
    written_path = writer.write_fragment(fragment)
    post = frontmatter.load(str(written_path))
    assert post.metadata["id"] == fragment.id


def test_frontmatter_roundtrip_smoke(tmp_path: Path) -> None:
    """Concrete regression case: a fragment with smart quotes and emoji."""
    vault = _make_vault(tmp_path)
    writer = VaultWriter(vault_path=vault)
    fragment = Fragment(
        id="frag-abc123def456",
        title="“Smart quotes” and 🌊 emoji",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        tags=["tag-with-dash", "tag_with_underscore"],
    )
    written = writer.write_fragment(fragment)
    post = frontmatter.load(str(written))
    assert post.metadata["id"] == "frag-abc123def456"
    assert post.metadata["title"] == "“Smart quotes” and 🌊 emoji"
    assert post.metadata["tags"] == ["tag-with-dash", "tag_with_underscore"]
