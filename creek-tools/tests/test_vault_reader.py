"""Tests for the shared vault fragment reader."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
