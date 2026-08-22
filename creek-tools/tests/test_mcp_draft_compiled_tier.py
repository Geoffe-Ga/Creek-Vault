"""Compiled thread/eddy sections must raise ``creek.draft``'s routing tier (#1013).

``_source_routing_tier`` derived the LLM routing tier from the seed's
``source_fragments`` alone. But the draft prompt also carries ``## Threads``
and ``## Eddies`` sections rendered from compiled-layer pages, and nothing
tier-filtered those: ``Thread`` and ``Eddy`` carry no ``privacy_tier`` field
at all, and their loaders take no ``PrivacyTierOverride``.

So a compiled thread page that summarises ``intimate`` fragments could reach a
cloud provider on a draft whose routing tier was computed as ``open``. Same
family as #931 — a derived artifact laundering above-ceiling content past a
source-tier computation that cannot see it.

The tier survey now folds in the provenance the compiled pages already record
(``CompiledPage.provenance[].fragment_ids``), and fails closed to ``intimate``
whenever a named thread or eddy contributes text whose sources cannot be
enumerated. These tests pin both halves: the laundering is closed, and the
existing ``UNEXPLORED_ONTOLOGY`` exemption still routes by ceiling alone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.compile.provenance import ProvenanceEntry
from creek.models import (
    CompiledPage,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
)
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.draft import _source_routing_tier

if TYPE_CHECKING:
    from pathlib import Path

_THREAD_DIR = "02-Threads"
_EDDY_DIR = "03-Eddies"


def _write_fragment(vault: Path, frag_id: str, tier: PrivacyTier) -> None:
    """Write a fragment note carrying *tier*."""
    fragment = Fragment(
        id=frag_id,
        title=f"Fragment {frag_id}",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 3, 1, 9, 0, 0),
        ingested=datetime(2026, 3, 1, 9, 0, 0),
        frequency=FrequencyClassification(primary=Frequency.F5),
        voice=VoiceClassification(),
        privacy_tier=tier,
    )
    post = frontmatter.Post(content="Fragment body.")
    post.metadata.update(fragment.model_dump(mode="json"))
    path = vault / "01-Fragments" / f"{frag_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _write_compiled_page(
    vault: Path,
    *,
    reldir: str,
    target_kind: str,
    target_id: str,
    fragment_ids: list[str] | None,
) -> None:
    """Write a compiled page whose provenance names *fragment_ids*.

    ``fragment_ids=None`` writes a page with **no** provenance entries — the
    shape that makes a page's sources unenumerable.
    """
    provenance = (
        []
        if fragment_ids is None
        else [
            ProvenanceEntry(
                claim_id="c1",
                claim_excerpt="A synthesised claim.",
                fragment_ids=fragment_ids,
                compiled_at=datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC),
                compile_method="llm",
            ),
        ]
    )
    page = CompiledPage(
        target_kind=target_kind,  # type: ignore[arg-type]  # Literal narrowed by caller
        target_id=target_id,
        title=f"Page {target_id}",
        body="Synthesised body text.",
        provenance=provenance,
    )
    post = frontmatter.Post(content="Synthesised body text.")
    post.metadata.update(page.model_dump(mode="json"))
    path = vault / reldir / f"{target_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _seed(
    *,
    source_fragments: tuple[str, ...] = (),
    threads: tuple[str, ...] = (),
    eddies: tuple[str, ...] = (),
) -> object:
    """Build an ``IdeaSeed`` with the given vault references."""
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="Routing seed",
        source_fragments=source_fragments,
        threads=threads,
        eddies=eddies,
        frequency_affinity=(),
        brief_description="brief",
        score=0.5,
    )


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A vault with the folders the tier survey walks."""
    for rel in ("01-Fragments", _THREAD_DIR, _EDDY_DIR):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---- The laundering channel this issue is about ----


def test_thread_page_summarising_intimate_raises_the_tier(vault: Path) -> None:
    """An open seed whose thread page came from intimate fragments routes local.

    The exact bug: ``source_fragments`` are all ``open`` and the declared
    ceiling is ``OPEN``, yet the prompt carries a compiled thread body
    synthesised from ``intimate`` content.

    The ceiling has to be ``OPEN`` for this test to mean anything. Under
    ``TierCeiling.ALL`` the ceiling itself derives ``intimate``, so
    ``routing_tier`` returns ``intimate`` whatever the content says — the
    assertion would pass against the unfixed code and prove nothing.
    """
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)
    _write_fragment(vault, "frag-intimate", PrivacyTier.INTIMATE)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id="thread-1",
        fragment_ids=["frag-intimate"],
    )

    tier = _source_routing_tier(
        vault,
        _seed(source_fragments=("frag-open",), threads=("thread-1",)),
        TierCeiling.OPEN,
    )

    assert tier == PrivacyTier.INTIMATE


def test_eddy_page_summarising_intimate_raises_the_tier(vault: Path) -> None:
    """The eddy section is the same channel as the thread section."""
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)
    _write_fragment(vault, "frag-intimate", PrivacyTier.INTIMATE)
    _write_compiled_page(
        vault,
        reldir=_EDDY_DIR,
        target_kind="eddy",
        target_id="eddy-1",
        fragment_ids=["frag-intimate"],
    )

    tier = _source_routing_tier(
        vault,
        _seed(source_fragments=("frag-open",), eddies=("eddy-1",)),
        TierCeiling.OPEN,
    )

    assert tier == PrivacyTier.INTIMATE


def test_open_thread_page_does_not_raise_the_tier(vault: Path) -> None:
    """The fix must not force every threaded draft local.

    A compiled page whose provenance names only ``open`` fragments carries no
    above-ceiling content, so it leaves the routing tier where it was. Without
    this, the fix would be indistinguishable from disabling cloud drafting.
    """
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)
    _write_fragment(vault, "frag-open-2", PrivacyTier.OPEN)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id="thread-open",
        fragment_ids=["frag-open-2"],
    )

    tier = _source_routing_tier(
        vault,
        _seed(source_fragments=("frag-open",), threads=("thread-open",)),
        TierCeiling.OPEN,
    )

    assert tier == PrivacyTier.OPEN


# ---- Fail closed when the sources cannot be enumerated ----


def test_thread_with_no_compiled_page_fails_closed(vault: Path) -> None:
    """A named thread with no compiled page still puts text in the prompt.

    ``_compose_thread_section`` falls back to the thread's frontmatter
    ``description`` when no compiled page exists — derived text whose sources
    the survey cannot enumerate. With no evidence about what it carries, the
    safe assumption is the worst one.
    """
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)

    tier = _source_routing_tier(
        vault,
        _seed(source_fragments=("frag-open",), threads=("thread-missing",)),
        TierCeiling.OPEN,
    )

    assert tier == PrivacyTier.INTIMATE


def test_compiled_page_without_provenance_fails_closed(vault: Path) -> None:
    """A page recording no provenance is opaque, not empty.

    Reading "no provenance entries" as "no sensitive sources" would reopen the
    exact hole this issue reports, for every page compiled before provenance
    was recorded.
    """
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id="thread-bare",
        fragment_ids=None,
    )

    tier = _source_routing_tier(
        vault,
        _seed(source_fragments=("frag-open",), threads=("thread-bare",)),
        TierCeiling.OPEN,
    )

    assert tier == PrivacyTier.INTIMATE


def test_provenance_naming_an_unresolvable_fragment_fails_closed(
    vault: Path,
) -> None:
    """A page citing a fragment that no longer exists cannot be cleared.

    ``max_source_tier`` already fails closed on an empty survey; this pins that
    the compiled path inherits that behaviour rather than skipping the id.
    """
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id="thread-stale",
        fragment_ids=["frag-deleted"],
    )

    tier = _source_routing_tier(
        vault,
        _seed(threads=("thread-stale",)),
        TierCeiling.OPEN,
    )

    assert tier == PrivacyTier.INTIMATE


# ---- Behaviour that must not change ----


def test_unexplored_ontology_seed_still_routes_by_ceiling(vault: Path) -> None:
    """The documented exemption survives: no vault content, no forced local.

    ``UNEXPLORED_ONTOLOGY`` builds its title and description from ontology enum
    labels and leaves sources, threads and eddies all empty. Failing closed
    there would push every such draft onto the local model for no privacy gain
    — the existing docstring says so explicitly, and this fix must not quietly
    reverse it.
    """
    tier = _source_routing_tier(vault, _seed(), TierCeiling.OPEN)

    assert tier == PrivacyTier.OPEN


def test_sources_only_seed_is_unaffected(vault: Path) -> None:
    """A seed with no threads or eddies routes exactly as it did before."""
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)

    tier = _source_routing_tier(
        vault,
        _seed(source_fragments=("frag-open",)),
        TierCeiling.OPEN,
    )

    assert tier == PrivacyTier.OPEN


def test_ceiling_still_wins_when_more_sensitive(vault: Path) -> None:
    """Reconciliation with the declared ceiling is unchanged.

    A caller cannot pick low-tier sources *and* a clean thread page to win
    cloud routing under a broad ceiling — ``routing_tier`` still takes the more
    sensitive of the two signals.
    """
    _write_fragment(vault, "frag-open", PrivacyTier.OPEN)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id="thread-open",
        fragment_ids=["frag-open"],
    )

    tier = _source_routing_tier(
        vault,
        _seed(source_fragments=("frag-open",), threads=("thread-open",)),
        TierCeiling.INTIMATE,
    )

    assert tier == PrivacyTier.INTIMATE
