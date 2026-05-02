"""Tests for :mod:`creek.classify.privacy_filter`.

The module owns tier filtering for every generation flow, so the tests
pin down each branch of the override matrix and confirm the privacy
audit log records exactly the elevated-inclusion calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.audit import AuditLog
from creek.classify.privacy_filter import (
    PRIVACY_AUDIT_RELPATH,
    PrivacyTierOverride,
    filter_fragments_by_tier,
    override_elevates,
    parse_include_tier,
    record_privacy_override,
)
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PrivacyTier,
    SourcePlatform,
)

if TYPE_CHECKING:
    from pathlib import Path


def _frag(
    *, id_: str, tier: PrivacyTier, title: str = "T", body: str = "B"
) -> tuple[Fragment, str]:
    """Return a (fragment, body) pair pinned to a specific privacy tier."""
    fragment = Fragment(
        id=id_,
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
            original_file="x.md",
        ),
        created=datetime(2025, 1, 1, tzinfo=UTC),
        privacy_tier=tier,
        frequency=FrequencyClassification(primary=Frequency.UNCLASSIFIED, secondary=[]),
    )
    return fragment, body


def test_default_excludes_intimate_summarises_personal() -> None:
    """Default policy drops intimate and replaces personal bodies."""
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="secret stuff"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="personal stuff"),
        _frag(id_="frag-o", tier=PrivacyTier.PUBLIC, body="open stuff"),
    ]

    out = list(filter_fragments_by_tier(inputs))

    ids = [f.id for f, _ in out]
    assert "frag-i" not in ids
    bodies = {f.id: body for f, body in out}
    assert bodies["frag-o"] == "open stuff"
    assert "personal stuff" not in bodies["frag-p"]
    assert "summary" in bodies["frag-p"].lower()


def test_personal_override_passes_full_body_excludes_intimate() -> None:
    """``--include-tier personal`` keeps personal bodies, drops intimate."""
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="x"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="full body"),
    ]

    out = list(
        filter_fragments_by_tier(inputs, override=PrivacyTierOverride.PERSONAL),
    )

    ids = [f.id for f, _ in out]
    assert ids == ["frag-p"]
    assert out[0][1] == "full body"


@pytest.mark.parametrize(
    "override",
    [PrivacyTierOverride.INTIMATE, PrivacyTierOverride.ALL],
)
def test_intimate_or_all_lets_everything_through(
    override: PrivacyTierOverride,
) -> None:
    """``intimate``/``all`` includes every tier with full bodies."""
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="secret"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="personal"),
        _frag(id_="frag-o", tier=PrivacyTier.PUBLIC, body="open"),
    ]

    out = list(filter_fragments_by_tier(inputs, override=override))

    bodies = {f.id: body for f, body in out}
    assert bodies == {"frag-i": "secret", "frag-p": "personal", "frag-o": "open"}


def test_override_elevates_matrix() -> None:
    """The elevation predicate is true for everything except None/open."""
    assert not override_elevates(None)
    assert not override_elevates(PrivacyTierOverride.OPEN)
    assert override_elevates(PrivacyTierOverride.PERSONAL)
    assert override_elevates(PrivacyTierOverride.INTIMATE)
    assert override_elevates(PrivacyTierOverride.ALL)


def test_record_privacy_override_writes_audit_entry(tmp_path: Path) -> None:
    """Recording an override appends a chained entry to privacy.jsonl."""
    vault = tmp_path / "vault"
    vault.mkdir()

    record_privacy_override(
        vault_path=vault,
        command="mine",
        fragment_ids=["frag-A", "frag-B"],
        operator="alice",
        override=PrivacyTierOverride.INTIMATE,
    )

    log_path = vault / PRIVACY_AUDIT_RELPATH
    assert log_path.exists()
    entries = list(AuditLog(log_path).read())
    assert len(entries) == 1
    entry = entries[0]
    assert entry["command"] == "mine"
    assert entry["operator"] == "alice"
    assert entry["include_tier"] == "intimate"
    assert entry["fragment_ids"] == ["frag-A", "frag-B"]


def test_parse_include_tier_handles_known_and_unknown() -> None:
    """The parser accepts canonical values and rejects others."""
    assert parse_include_tier(None) is None
    assert parse_include_tier("intimate") is PrivacyTierOverride.INTIMATE
    assert parse_include_tier("ALL") is PrivacyTierOverride.ALL
    with pytest.raises(ValueError, match="--include-tier"):
        parse_include_tier("nope")
