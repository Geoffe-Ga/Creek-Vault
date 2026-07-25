"""SEC-006: mining and drafting must filter out intimate fragments by default.

Pinning the behaviour at the engine level (independent of the CLI)
guards against regressions where a future contributor adds a new
generation flow that bypasses the shared filter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.generate.drafts import _load_fragments_by_id
from creek.generate.mining import _load_fragments

if TYPE_CHECKING:
    from pathlib import Path


def _write_fragment(
    fragments_dir: Path,
    fragment_id: str,
    *,
    title: str,
    privacy_tier: str,
    body: str,
) -> None:
    """Persist a fragment markdown file with the given privacy tier."""
    metadata: dict[str, object] = {
        "id": fragment_id,
        "title": title,
        "type": "fragment",
        "source": {
            "platform": "journal",
            "author": "self",
            "original_file": "x.md",
        },
        "created": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        "frequency": {"primary": "F3", "secondary": []},
        "wavelength": {
            "phase": "rising",
            "mode": "inhabit",
            "orientation": "do",
            "dosage": "medicine",
            "color": "orange",
            "descriptor": "bright",
        },
        "voice": {"voice_register": "analytical", "confidence": "settled"},
        "privacy_tier": privacy_tier,
    }
    target = fragments_dir / f"{fragment_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


def test_mining_load_excludes_intimate_by_default(tmp_path: Path) -> None:
    """``_load_fragments`` returns no intimate fragments under default policy."""
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir, "frag-i", title="Diary", privacy_tier="intimate", body="x"
    )
    _write_fragment(
        fragments_dir, "frag-o", title="Essay", privacy_tier="open", body="y"
    )

    pairs = _load_fragments(fragments_dir)
    ids = [f.id for f, _ in pairs]
    assert ids == ["frag-o"]


def test_mining_load_summarises_personal_by_default(tmp_path: Path) -> None:
    """Personal fragments survive but their bodies are replaced."""
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir,
        "frag-p",
        title="Personal note",
        privacy_tier="personal",
        body="confidential body",
    )

    pairs = _load_fragments(fragments_dir)
    assert len(pairs) == 1
    _, body = pairs[0]
    assert "confidential body" not in body
    assert "Personal note" in body


def test_mining_load_with_intimate_override_includes_full_body(tmp_path: Path) -> None:
    """``--include-tier intimate`` lets intimate bodies pass through."""
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir,
        "frag-i",
        title="Diary",
        privacy_tier="intimate",
        body="diary body",
    )

    pairs = _load_fragments(
        fragments_dir,
        privacy_override=PrivacyTierOverride.INTIMATE,
    )
    assert len(pairs) == 1
    _, body = pairs[0]
    assert body == "diary body"


def test_mining_load_summarises_unclassified_by_default(tmp_path: Path) -> None:
    """An untiered fragment contributes a summary, not its full body (#876).

    Deliberate behaviour change: ``unclassified`` used to rank alongside
    ``open`` and hand its full body to ``creek mine``. Since ``creek
    classify`` had no privacy caller, every fragment in a vault was
    untiered — so the whole private corpus was mineable at the open tier.
    Three fragments at three distinct tiers pin the whole ladder at once.
    """
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir,
        "frag-u",
        title="Untiered note",
        privacy_tier="unclassified",
        body="untiered body",
    )
    _write_fragment(
        fragments_dir, "frag-o", title="Essay", privacy_tier="open", body="open body"
    )
    _write_fragment(
        fragments_dir, "frag-i", title="Diary", privacy_tier="intimate", body="secret"
    )

    bodies = {f.id: body for f, body in _load_fragments(fragments_dir)}

    assert set(bodies) == {"frag-u", "frag-o"}  # intimate still dropped entirely
    assert bodies["frag-o"] == "open body"
    assert "untiered body" not in bodies["frag-u"]
    assert "Untiered note" in bodies["frag-u"]


def test_mining_load_with_personal_override_returns_untiered_full_body(
    tmp_path: Path,
) -> None:
    """``--include-tier personal`` lets an untiered body through intact (#876)."""
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir,
        "frag-u",
        title="Untiered note",
        privacy_tier="unclassified",
        body="untiered body",
    )

    pairs = _load_fragments(
        fragments_dir,
        privacy_override=PrivacyTierOverride.PERSONAL,
    )

    assert [(f.id, body) for f, body in pairs] == [("frag-u", "untiered body")]


def test_drafts_load_summarises_unclassified_by_default(tmp_path: Path) -> None:
    """``_load_fragments_by_id`` mirrors the #876 untiered contract for drafts."""
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir,
        "frag-u",
        title="Untiered note",
        privacy_tier="unclassified",
        body="untiered body",
    )
    _write_fragment(
        fragments_dir, "frag-o", title="Essay", privacy_tier="open", body="open body"
    )
    _write_fragment(
        fragments_dir, "frag-i", title="Diary", privacy_tier="intimate", body="secret"
    )

    loaded = _load_fragments_by_id(fragments_dir)

    assert set(loaded) == {"frag-u", "frag-o"}
    assert loaded["frag-o"][1] == "open body"
    assert "untiered body" not in loaded["frag-u"][1]
    assert "Untiered note" in loaded["frag-u"][1]


def test_drafts_load_excludes_intimate_by_default(tmp_path: Path) -> None:
    """``_load_fragments_by_id`` mirrors the mining default behaviour."""
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir, "frag-i", title="Diary", privacy_tier="intimate", body="x"
    )
    _write_fragment(
        fragments_dir, "frag-o", title="Essay", privacy_tier="open", body="y"
    )

    loaded = _load_fragments_by_id(fragments_dir)
    assert "frag-i" not in loaded
    assert "frag-o" in loaded


def test_drafts_load_with_intimate_override_returns_full_body(tmp_path: Path) -> None:
    """``--include-tier intimate`` overrides the default for drafts too."""
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir()
    _write_fragment(
        fragments_dir,
        "frag-i",
        title="Diary",
        privacy_tier="intimate",
        body="diary body",
    )

    loaded = _load_fragments_by_id(
        fragments_dir,
        privacy_override=PrivacyTierOverride.INTIMATE,
    )
    assert "frag-i" in loaded
    _, body = loaded["frag-i"]
    assert body == "diary body"
