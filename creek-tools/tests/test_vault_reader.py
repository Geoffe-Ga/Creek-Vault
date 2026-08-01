"""Tests for the shared vault fragment reader."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.reader import iter_vault_fragments, try_load_fragment
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path


def test_try_load_fragment_returns_triple(tmp_path: Path) -> None:
    """Valid fragments come back as ``(fragment, body, raw_metadata)``."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-readok000001",
        title="Readable",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = write_fragment_file(vault=vault, fragment=fragment, body="hello")

    record = try_load_fragment(file)
    assert record is not None
    loaded_fragment, body, raw = record
    assert loaded_fragment.id == fragment.id
    assert body.strip() == "hello"
    assert raw["type"] == "fragment"


def test_try_load_fragment_returns_none_for_non_fragment(tmp_path: Path) -> None:
    """A markdown file without ``type: fragment`` is skipped silently."""
    file = tmp_path / "note.md"
    file.write_text("---\nnot: a fragment\n---\nplain body\n", encoding="utf-8")

    assert try_load_fragment(file) is None


def test_try_load_fragment_returns_none_for_invalid_schema(tmp_path: Path) -> None:
    """Files claiming ``type: fragment`` but missing required keys skip silently."""
    file = tmp_path / "broken.md"
    file.write_text(
        "---\ntype: fragment\nid: frag-broken000001\n---\nbody\n",
        encoding="utf-8",
    )

    # The Fragment schema requires ``title`` and ``source``; this entry
    # has neither, so model validation fails and we get ``None`` rather
    # than a raised exception.
    assert try_load_fragment(file) is None


def test_try_load_fragment_propagates_oserror(tmp_path: Path) -> None:
    """Real I/O failures propagate so the caller can surface them."""
    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(OSError):
        try_load_fragment(missing)


def test_iter_vault_fragments_skips_non_fragments(tmp_path: Path) -> None:
    """``iter_vault_fragments`` returns only valid Creek fragments."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-mixed000001",
        title="Real one",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    write_fragment_file(vault=vault, fragment=fragment, body="body")

    fragments_dir = vault / "01-Fragments" / "Notes"
    (fragments_dir / "plain.md").write_text("# Just a note\n", encoding="utf-8")
    (fragments_dir / "broken-yaml.md").write_text(
        "---\nbroken: [\n",
        encoding="utf-8",
    )

    records = iter_vault_fragments(vault / "01-Fragments")
    titles = [record[1].title for record in records]
    assert titles == ["Real one"]


def test_iter_vault_fragments_returns_empty_for_missing_root(tmp_path: Path) -> None:
    """Missing ``01-Fragments`` returns an empty list, not an error."""
    assert iter_vault_fragments(tmp_path / "no-such-dir") == []


def test_try_load_fragment_without_hierarchy_keys_defaults_to_root(
    tmp_path: Path,
) -> None:
    """Pre-FEAT-020 fragments load as root documents without crash or warning.

    Acceptance criterion: "Pre-existing fragments (no hierarchy fields
    in frontmatter) load with the documented defaults — no crash, no
    warning spam." Loads a hand-crafted legacy frontmatter blob (no
    ``parent_id`` / ``child_ids`` / ``level`` / ``structural_path``
    keys) with ``warnings`` promoted to errors so any incidental
    deprecation noise from the schema would fail the test.
    """
    import warnings

    legacy = tmp_path / "legacy.md"
    legacy.write_text(
        "---\n"
        "type: fragment\n"
        "id: frag-legacy000007\n"
        "title: Pre-FEAT-020 fragment\n"
        "source:\n"
        "  platform: claude\n"
        "---\n"
        "Body of the legacy fragment.\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        record = try_load_fragment(legacy)

    assert record is not None
    fragment, body, raw = record
    assert fragment.parent_id is None
    assert fragment.child_ids == []
    assert fragment.level == "document"
    assert fragment.structural_path == []
    assert body.strip() == "Body of the legacy fragment."
    # Legacy frontmatter must remain untouched — raw should not have
    # been mutated to inject the new keys (callers that round-trip via
    # ``raw`` would otherwise stamp defaults into untouched vault
    # files and explode the audit diff on the next pass).
    assert "parent_id" not in raw
    assert "level" not in raw


def test_fragment_with_offsetless_frontmatter_timestamp_loads_tz_aware(
    tmp_path: Path,
) -> None:
    """An offsetless ``ingested:`` loads coerced to LA — and is not dropped.

    PyYAML parses the unquoted frontmatter line
    ``ingested: 2024-01-01 12:00:00`` into a *naive* datetime, which
    before issue #976 flowed straight through
    :meth:`Fragment.model_validate` and detonated later, at whichever
    consumer first compared it against a tz-aware timestamp from a
    neighbouring fragment. This is the on-disk round trip: the frontmatter
    is hand-written rather than dumped from a :class:`Fragment`, because a
    dumped model would already carry an offset and prove nothing.

    Assertion (a) — ``record is not None`` — is deliberate, not padding.
    :func:`try_load_fragment` swallows ``ValidationError`` and returns
    ``None`` at DEBUG level, so "hardening" the model to *reject* naive
    timestamps rather than coerce them would silently delete every
    offsetless fragment from every vault scan with no error surfaced
    anywhere. This test pins coerce-over-reject.
    """
    fragment_file = tmp_path / "offsetless.md"
    fragment_file.write_text(
        "---\n"
        "type: fragment\n"
        "id: frag-offsetless01\n"
        "title: Offsetless timestamp\n"
        "source:\n"
        "  platform: journal\n"
        "ingested: 2024-01-01 12:00:00\n"
        "---\n"
        "Body of the offsetless fragment.\n",
        encoding="utf-8",
    )

    record = try_load_fragment(fragment_file)

    # (a) The fragment survives the load — naive timestamps are repaired,
    # never grounds for silently discarding vault content.
    assert record is not None
    fragment, _body, _raw = record
    # (b) …and it is repaired to LA, not merely stamped with some offset.
    la = ZoneInfo("America/Los_Angeles")
    assert fragment.ingested.tzinfo is not None
    assert fragment.ingested.utcoffset() == la.utcoffset(fragment.ingested)
    # Attached, not converted: the wall clock the file recorded survives.
    assert fragment.ingested.hour == 12
    assert fragment.ingested.date().isoformat() == "2024-01-01"


def test_try_load_fragment_round_trips_hierarchy_fields(tmp_path: Path) -> None:
    """A fragment carrying every hierarchy field round-trips through reader."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-hier-readok",
        title="Hierarchical",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        parent_id="frag-hier-parent",
        child_ids=["frag-hier-kidaa", "frag-hier-kidbb"],
        level="section",
        structural_path=["Essay", "Part 2"],
    )
    file = write_fragment_file(vault=vault, fragment=fragment, body="body")

    record = try_load_fragment(file)
    assert record is not None
    loaded_fragment, _body, raw = record
    assert loaded_fragment.parent_id == "frag-hier-parent"
    assert loaded_fragment.child_ids == ["frag-hier-kidaa", "frag-hier-kidbb"]
    assert loaded_fragment.level == "section"
    assert loaded_fragment.structural_path == ["Essay", "Part 2"]
    # raw_metadata must surface every hierarchy key — classify/link
    # engines pass raw forward when rewriting frontmatter, so dropping
    # any one would silently nuke that field on the next write.
    assert raw["parent_id"] == "frag-hier-parent"
    assert raw["child_ids"] == ["frag-hier-kidaa", "frag-hier-kidbb"]
    assert raw["level"] == "section"
    assert raw["structural_path"] == ["Essay", "Part 2"]
