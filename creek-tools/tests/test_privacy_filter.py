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


def test_unclassified_tier_passes_through_with_full_body() -> None:
    """``unclassified`` is treated as ``open`` — full body, no exclusion.

    Documents and pins the existing fall-through behaviour. Fragments
    that have not yet been classified are presumed non-sensitive; the
    classifier should backfill an explicit tier before they enter
    sensitive flows.
    """
    inputs = [
        _frag(id_="frag-u", tier=PrivacyTier.UNCLASSIFIED, body="raw body"),
    ]

    out = list(filter_fragments_by_tier(inputs))

    assert len(out) == 1
    fragment, body = out[0]
    assert fragment.id == "frag-u"
    assert body == "raw body"


def test_tier_of_unknown_string_fails_closed_to_intimate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fragments carrying an unrecognised tier string fail closed.

    Regression for PR #193 review (comment 4367360694 LOW): the prior
    ``_tier_of`` did ``PrivacyTier(fragment.privacy_tier)`` with no
    safety net, so a hand-edited or schema-migrated vault with an
    unknown tier string would crash generation flows. The new helper
    catches the :class:`ValueError`, logs a warning that names the
    fragment ID, and returns :data:`PrivacyTier.INTIMATE` so the
    fragment is excluded from the default-policy output.
    """
    from creek.classify.privacy_filter import _tier_of

    # ``model_construct`` skips Pydantic validation, allowing us to
    # plant a bogus tier value the same way a hand-edited markdown file
    # or a forward-incompatible schema migration would.
    bogus = Fragment.model_construct(
        id="frag-bogus",
        title="t",
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
            original_file="x.md",
        ),
        created=datetime(2025, 1, 1, tzinfo=UTC),
        privacy_tier="brand-new-tier-v2",  # type: ignore[arg-type]
        frequency=FrequencyClassification(primary=Frequency.UNCLASSIFIED, secondary=[]),
    )

    with caplog.at_level("WARNING", logger="creek.classify.privacy_filter"):
        tier = _tier_of(bogus)

    assert tier is PrivacyTier.INTIMATE
    assert any(
        "frag-bogus" in r.message and "INTIMATE" in r.message for r in caplog.records
    )


def test_open_override_matches_default_behaviour() -> None:
    """``--include-tier open`` explicitly == default (no flag).

    The flag value exists for symmetry with ``personal``/``intimate``/
    ``all``; users who pass it should observe identical filtering to
    callers who pass nothing.
    """
    inputs = [
        _frag(id_="frag-i", tier=PrivacyTier.INTIMATE, body="x"),
        _frag(id_="frag-p", tier=PrivacyTier.PERSONAL, body="full"),
        _frag(id_="frag-o", tier=PrivacyTier.PUBLIC, body="open"),
    ]

    default_out = list(filter_fragments_by_tier(inputs))
    open_out = list(
        filter_fragments_by_tier(inputs, override=PrivacyTierOverride.OPEN),
    )

    assert [f.id for f, _ in default_out] == [f.id for f, _ in open_out]
    assert [body for _, body in default_out] == [body for _, body in open_out]


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
