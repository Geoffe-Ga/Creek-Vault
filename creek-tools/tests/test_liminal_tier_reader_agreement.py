"""One ``10-Liminal`` note must get one tier from both readers (#1079).

The state report admits a liminal note through
:func:`creek.generate.state._admitted_liminal_notes`, which reads the note's
*raw* frontmatter through
:func:`~creek.classify.privacy_filter.within_ceiling` /
:func:`~creek.classify.privacy_filter.raw_privacy_tier`. The mining corpus
behind ``## Suggested questions`` admits the same physical file through
:func:`creek.generate.mining._load_liminal_fragments`, which read the tier off
the *validated* :class:`~creek.models.Fragment`.

A note with **no** ``privacy_tier`` key therefore came out ``intimate`` on one
side and ``unclassified`` on the other: refused below ``ceiling=intimate`` by
the report, admitted from ``ceiling=open`` upward by the corpus — inside one
rendered document. That is the "two tools that disagree about the same file"
failure ``creek/classify/privacy_filter.py``'s own module docstring names, and
that ``tests/test_mcp_report_tier_ceiling.py``'s
``test_raw_and_model_tier_readers_agree_on_every_fragment`` already pins for
``01-Fragments``. This module pins it for ``10-Liminal``.

:func:`~creek.classify.privacy_filter.raw_privacy_tier` is the single
definition; the miner now calls it. Every assertion below therefore compares
the two call sites *against each other* over the same files rather than
against a hard-coded expectation of either one — a pin on the agreement, not
on an implementation. The one exception is the untiered note, checked
explicitly against ``INTIMATE``, because agreeing on the fail-closed answer
(rather than merely agreeing) is the whole point: an agreement on
``unclassified`` would be the two readers drifting together in the permissive
direction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.classify.privacy_filter import PrivacyTierOverride, tier_of
from creek.generate.mining import _load_liminal_fragments
from creek.generate.state import (
    _LIMINAL_ROOT,
    _LIMINAL_SUBDIRS,
    EMPTY_PLACEHOLDER,
    StateReportGenerator,
    _admitted_liminal_notes,
)
from creek.generate.state_tiers import TIER_STAMP_KEY
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from pathlib import Path

# Every ceiling the two readers can be asked about. ``ALL`` is included
# because "no ceiling declared" is a real production value — it is the
# ``StateReportGenerator`` default — and an agreement that only holds under a
# restriction is not an agreement.
_CEILINGS: tuple[PrivacyTierOverride, ...] = (
    PrivacyTierOverride.OPEN,
    PrivacyTierOverride.PERSONAL,
    PrivacyTierOverride.INTIMATE,
    PrivacyTierOverride.ALL,
)

# Stems restricted to ``[A-Za-z0-9-]`` so the join key below survives being
# used as both a filename and a frontmatter ``title``.
_NO_KEY_STEM = "LiminalNoKeyCanary"
_NOTES: tuple[tuple[str, str | None], ...] = (
    (_NO_KEY_STEM, None),
    ("LiminalOpenCanary", "open"),
    ("LiminalPersonalCanary", "personal"),
    ("LiminalIntimateCanary", "intimate"),
    ("LiminalUnclassifiedCanary", "unclassified"),
)


def _write_liminal_note(folder: Path, stem: str, tier: str | None) -> None:
    """Write one ``type: fragment`` liminal note, with or without a tier key.

    The file *stem* and the frontmatter ``title`` are deliberately the same
    string: the state reader keys its result by stem and the mining reader by
    the validated fragment, so one shared value is what lets the two results
    be compared note by note.

    Args:
        folder: Directory to write into; created if absent.
        stem: Filename stem, also used as the fragment title.
        tier: The ``privacy_tier`` value to declare, or ``None`` to omit the
            key entirely — the case the two readers disagreed about.
    """
    folder.mkdir(parents=True, exist_ok=True)
    tier_line = "" if tier is None else f"privacy_tier: {tier}\n"
    folder.joinpath(f"{stem}.md").write_text(
        "---\n"
        "type: fragment\n"
        f"id: {stem.lower()[:12]}\n"
        f"title: {stem}\n"
        "source:\n"
        "  platform: journal\n"
        "created: 2026-01-01T00:00:00Z\n"
        f"{tier_line}"
        "---\n\n"
        "Body text about eddies and rivers.\n",
        encoding="utf-8",
    )


@pytest.fixture
def liminal_vault(tmp_path: Path) -> Path:
    """Return a vault whose ``10-Liminal/Unnamed`` holds one note per tier.

    Both readers walk this one folder: ``_admitted_liminal_notes`` is called
    on it directly, and ``_load_liminal_fragments`` reaches it by walking
    ``10-Liminal``. Keeping every note in the single subfolder both readers
    cover is what makes their two results comparable as sets.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        The vault root.
    """
    for stem, tier in _NOTES:
        _write_liminal_note(tmp_path / _LIMINAL_ROOT / "Unnamed", stem, tier)
    return tmp_path


def _state_reader(vault: Path, ceiling: PrivacyTierOverride) -> dict[str, PrivacyTier]:
    """Return ``{stem: tier}`` as the ``## Liminal Watch`` side reads it."""
    return dict(_admitted_liminal_notes(vault / _LIMINAL_ROOT / "Unnamed", ceiling))


def _mining_reader(vault: Path, ceiling: PrivacyTierOverride) -> dict[str, PrivacyTier]:
    """Return ``{title: tier}`` as the ``## Suggested questions`` side reads it."""
    return {
        fragment.title: tier_of(fragment)
        for fragment, _body, _kind in _load_liminal_fragments(
            vault / _LIMINAL_ROOT,
            privacy_override=ceiling,
        )
    }


@pytest.mark.parametrize("ceiling", _CEILINGS)
def test_both_liminal_readers_admit_the_same_notes(
    liminal_vault: Path,
    ceiling: PrivacyTierOverride,
) -> None:
    """The two readers admit the same set of files under the same ceiling.

    Before #1079 the untiered note was admitted by the miner at every ceiling
    (``filter_fragments_by_tier`` *summarises* rather than excludes, so even
    ``ceiling=open`` kept it) while the report refused it below
    ``ceiling=intimate``.

    Args:
        liminal_vault: Vault holding one liminal note per tier.
        ceiling: The admission ceiling both readers are asked about.
    """
    assert set(_mining_reader(liminal_vault, ceiling)) == set(
        _state_reader(liminal_vault, ceiling),
    ), (
        f"the two liminal readers disagree about which notes {ceiling.value!r} "
        f"admits: the miner says {sorted(_mining_reader(liminal_vault, ceiling))}, "
        f"the report says {sorted(_state_reader(liminal_vault, ceiling))}"
    )


@pytest.mark.parametrize("ceiling", _CEILINGS)
def test_both_liminal_readers_report_the_same_tier(
    liminal_vault: Path,
    ceiling: PrivacyTierOverride,
) -> None:
    """For every commonly-admitted note the two readers name the same tier.

    Asserted per note rather than as a whole-mapping comparison so a failure
    names the file that diverged.

    Args:
        liminal_vault: Vault holding one liminal note per tier.
        ceiling: The admission ceiling both readers are asked about.
    """
    mined = _mining_reader(liminal_vault, ceiling)
    reported = _state_reader(liminal_vault, ceiling)
    shared = sorted(set(mined) & set(reported))
    for stem in shared:
        assert mined[stem] is reported[stem], (
            f"the two liminal readers disagree about the tier of {stem!r} at "
            f"ceiling={ceiling.value!r}: the miner says {mined[stem].value!r}, "
            f"the report says {reported[stem].value!r}"
        )


def test_untiered_liminal_note_is_intimate_to_both_readers(
    liminal_vault: Path,
) -> None:
    """A note with no ``privacy_tier`` key fails closed on *both* sides.

    The positive control for the two tests above: agreeing on ``unclassified``
    would satisfy them while being the two readers drifting together in the
    permissive direction, which is exactly what the one-way ratchet forbids.

    Args:
        liminal_vault: Vault holding one liminal note per tier.
    """
    ceiling = PrivacyTierOverride.ALL
    assert _mining_reader(liminal_vault, ceiling)[_NO_KEY_STEM] is PrivacyTier.INTIMATE
    assert _state_reader(liminal_vault, ceiling)[_NO_KEY_STEM] is PrivacyTier.INTIMATE


@pytest.mark.parametrize(
    "ceiling",
    [PrivacyTierOverride.OPEN, PrivacyTierOverride.PERSONAL],
)
def test_untiered_liminal_note_is_refused_by_the_miner_below_intimate(
    liminal_vault: Path,
    ceiling: PrivacyTierOverride,
) -> None:
    """The untiered note leaves the mining corpus below ``ceiling=intimate``.

    Stated as its own assertion because it is the *direction* of the fix: the
    divergence is closed by tightening the miner up to the report, never by
    loosening the report down to the miner. The miner's opaque 12-hex seed id
    is the only liminal field a prompt renders, so this is the surface that
    was reachable.

    Args:
        liminal_vault: Vault holding one liminal note per tier.
        ceiling: A ceiling below ``intimate``.
    """
    assert _NO_KEY_STEM not in _mining_reader(liminal_vault, ceiling)
    assert _NO_KEY_STEM not in _state_reader(liminal_vault, ceiling)


def test_untiered_compost_note_stamps_the_state_report_intimate(
    tmp_path: Path,
) -> None:
    """``10-Liminal/Compost`` is the axis the report cannot intersect (#1078).

    ``_LIMINAL_SUBDIRS`` covers only ``Unnamed`` and ``Paradoxes``, so a
    ``Compost`` note reaches ``## Suggested questions`` with no admitted list
    to narrow it against; ``_content_tier`` accounts for it on the artifact
    stamp instead. Reading that tier off the validated model stamped an
    untiered Compost note ``unclassified`` — one rank below the ``intimate``
    the raw frontmatter says — which under-reports the stamp the read gate
    trusts.

    The stamp is read back off the written artifact rather than from
    ``_content_tier``, so the assertion describes the file the read gate
    actually opens. The vault holds nothing but the one Compost note, so the
    only other contributor that could reach ``intimate`` — the lint summary's
    unconditional escalation — has no Processing-Log artifact to render and
    is asserted absent below.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    assert "Compost" not in _LIMINAL_SUBDIRS
    _write_liminal_note(tmp_path / _LIMINAL_ROOT / "Compost", "CompostNoKey", None)
    written = StateReportGenerator(
        vault_path=tmp_path,
        override=PrivacyTierOverride.INTIMATE,
    ).write()
    stamp = frontmatter.load(str(written)).metadata
    assert (
        EMPTY_PLACEHOLDER
        in written.read_text(encoding="utf-8").split(
            "## Lint summary",
        )[1]
    ), "the lint summary rendered content, so the stamp is not the note's"
    assert stamp[TIER_STAMP_KEY] == PrivacyTier.INTIMATE.value
