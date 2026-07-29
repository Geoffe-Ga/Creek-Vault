"""``creek process`` and ``creek classify`` must agree on every axis.

Creek has two ways to get a classified fragment into a vault:
``creek process`` (ingest + classify in one shot, via
:class:`creek.pipeline.Pipeline`) and ``creek classify`` (classify what
is already on disk, via :func:`creek.classify.classify_engine.run_classify`).
They share the individual classifiers but **not** the code that calls
them, so a new axis wired into only one of the two orchestrators leaves
the other silently stamping the model default. That has now happened
three times: #876 (``privacy_tier``), #877 (``praxis_potential``) and
#937 (``audience``). This module is the *class* detector for that
defect — it fails automatically on a fourth instance, without anyone
remembering to extend a list of axes.

**The invariant.** "Byte-identical frontmatter" is what one would like
to say, and it is false as literally worded: ``run_classify`` adds
``classification_method`` and ``classified_at``, which are not
:class:`~creek.models.Fragment` model fields at all (see
``_write_fragment``, ``creek/classify/classify_engine.py:1564-1583``),
and YAML key ordering plus timestamp round-tripping make a bytes
comparison meaningless anyway. The true, stronger, testable contract is:

    For a vault written by ``creek process`` with ``no_llm=True`` from
    freshly-ingested, untiered fragments, a subsequent ``creek classify
    --method rules --force`` changes **no ``Fragment`` model field**. The
    only frontmatter delta is the *addition* of ``classification_method``
    and ``classified_at``.

**Preconditions**, each of which the test establishes deliberately:

1. ``no_llm=True`` on the process side and ``--method rules`` on the
   classify side. With ``--force``, a rules re-run would legitimately
   overwrite LLM-derived ``frequency`` / ``wavelength`` / ``voice``, so
   the invariant is scoped to the deterministic path where both
   orchestrators are supposed to compute the same answer from the same
   inputs. An LLM-vs-rules disagreement is not this defect class.
2. Fragments enter **untiered**. An ingester-supplied ``privacy_tier``
   lower than the heuristic's would be escalated by ``--force``, and the
   resulting delta would be correct behaviour rather than a wiring hole.
3. Compare *parsed metadata*, not bytes — see above.

The comparison is over the whole parsed frontmatter dict minus a small
documented exclusion set, never a hand-enumerated list of axes. That is
what makes the next unwired axis fail here on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.classify.classify_engine import run_classify
from creek.config import CreekConfig
from creek.ingest.base import IngestedFragment
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PraxisPotential,
    PrivacyTier,
    SourcePlatform,
)
from creek.pipeline import Pipeline, PipelineResult

if TYPE_CHECKING:
    from pathlib import Path

# Vault folders the pipeline and the classify engine both expect to exist.
VAULT_DIRS: list[str] = [
    "00-Creek-Meta/Processing-Log",
    "01-Fragments/Journal",
    "01-Fragments/Notes",
    "01-Fragments/Unsorted",
]

_CLASSIFY_ONLY_KEYS = frozenset({"classification_method", "classified_at"})
"""The only non-``Fragment`` keys a rules classify run adds to frontmatter.

``_write_fragment`` (``creek/classify/classify_engine.py:1564-1583``)
starts from the raw on-disk mapping, overlays ``fragment.model_dump()``,
then stamps exactly these two provenance keys. The two sibling
provenance keys are *not* additions on this path: ``classification_provider``
and ``classification_reasoning`` are explicitly popped for anything other
than an ``llm`` write with a provider and a non-empty trace
(``classify_engine.py:1572-1579``). Everything else in the mapping comes
from the ``Fragment`` model, which is precisely what must not move.
"""

_RICH_PARAGRAPH = (
    "I keep circling the same question about how the work fits the "
    "life and the life fits the work, and every pass over it turns "
    "up one more thing I had not noticed the time before. "
)

_RICH_BODY = (
    "# The long way round\n\n"
    "> Not all those who wander are lost.\n\n"
    + _RICH_PARAGRAPH * 20
    + "\n\n- [ ] write this up properly\n\nI will come back to it.\n"
)
"""A body that drives every deterministic axis off its model default.

Self-authored journal prose: the privacy heuristic reads ``intimate``,
the task checkbox makes the praxis pass read ``explicit``, and the
audience heuristic sums journal ``-3`` + intimate ``-3`` + structure
``+3`` (heading, blockquote, >= 300 words) to ``-3`` → ``private``.
"""

_NEUTRAL_BODY = "a plain note about the walk to the shops and the weather " * 6
"""A body that leaves the axes near their defaults (tier ``personal``,
audience ``mixed``), so the parity check covers the boring case too."""


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal Obsidian vault structure under tmp_path.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        The vault root, with the folders both orchestrators require.
    """
    vault = tmp_path / "vault"
    for directory in VAULT_DIRS:
        (vault / directory).mkdir(parents=True, exist_ok=True)
    return vault


def _ingested(frag_id: str, title: str, body: str, platform: str) -> IngestedFragment:
    """Build a freshly-ingested, untiered fragment bundle.

    Carries no ``privacy_tier``, ``praxis_potential`` or ``audience`` of
    its own — every axis is left at the model default so the process run
    is the thing that assigns them (precondition 2 in the module
    docstring).

    Args:
        frag_id: Fragment id; the key both snapshots are compared on.
        title: Fragment title.
        body: Markdown body the deterministic classifiers score.
        platform: ``SourcePlatform`` value for the fragment's source.

    Returns:
        The assembled :class:`~creek.ingest.base.IngestedFragment`.
    """
    fragment = Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(
            platform=SourcePlatform(platform),
            author=Authorship.SELF,
        ),
    )
    return IngestedFragment(fragment=fragment, body=body)


def _snapshot(vault: Path) -> dict[str, dict[str, object]]:
    """Return every fragment's parsed frontmatter, keyed by fragment id.

    Keyed by ``id`` rather than by path or iteration order: ``rglob`` is
    unsorted, and ``creek classify`` is free to rewrite a file in place
    under a name this test must not depend on. The walk itself is sorted
    for a deterministic failure ordering.

    Args:
        vault: Vault root to snapshot.

    Returns:
        Mapping of fragment id to a copy of its frontmatter mapping.
    """
    return {
        str(post.metadata["id"]): dict(post.metadata)
        for post in (
            frontmatter.load(str(path))
            for path in sorted((vault / "01-Fragments").rglob("*.md"))
        )
    }


def test_rules_classify_after_process_changes_no_fragment_field(
    vault_path: Path,
) -> None:
    """A rules re-classify of a processed vault moves no model field.

    This is the #876 / #877 / #937 defect class expressed as one
    assertion pair. Half one pins that every shared key is unchanged;
    half two pins that the only *new* keys are the two provenance stamps
    in :data:`_CLASSIFY_ONLY_KEYS`. Neither half names an axis, so an
    axis wired into ``run_classify`` but not into
    :meth:`creek.pipeline.Pipeline._run_classification` fails here
    without any test being updated.

    ``result.errors == []`` and the classify summary's own counts are
    asserted first: a silent vault-write failure, or a classify run that
    matched zero files, would otherwise make the parity comparison pass
    over an empty or half-written vault.

    The off-default guard on the rich fixture is the other anti-vacuity
    check. If a future edit blunted that body into something the
    heuristics leave at ``unclassified`` / ``none`` / ``mixed``, both
    snapshots would agree at the defaults and the parity assertion would
    be testing nothing. On today's RED run this guard is the first thing
    to fail, because ``creek process`` never stamps ``audience`` — which
    is the bug.
    """
    items = [
        _ingested("frag-parityrich01", "The long way round", _RICH_BODY, "journal"),
        _ingested("frag-parityneut01", "Walking notes", _NEUTRAL_BODY, "markdown"),
    ]
    pipeline = Pipeline(config=CreekConfig(), no_llm=True)
    result = PipelineResult()

    classified = pipeline._run_classification(items, vault_path, result)
    pipeline._write_to_vault(classified, vault_path, result)

    assert result.errors == []

    before = _snapshot(vault_path)
    assert set(before) == {"frag-parityrich01", "frag-parityneut01"}

    rich = before["frag-parityrich01"]
    assert (
        rich["privacy_tier"],
        rich["praxis_potential"],
        rich["audience"],
    ) == (
        PrivacyTier.INTIMATE.value,
        PraxisPotential.EXPLICIT.value,
        "private",
    )

    summary = run_classify(
        vault_path=vault_path,
        config=CreekConfig(),
        method="rules",
        force=True,
    )

    assert summary.errors == ()
    assert summary.total == len(items)

    after = _snapshot(vault_path)
    assert set(after) == set(before)

    for frag_id, before_meta in before.items():
        after_meta = after[frag_id]
        assert {
            key: value
            for key, value in after_meta.items()
            if key not in _CLASSIFY_ONLY_KEYS
        } == before_meta
        assert set(after_meta) - set(before_meta) == _CLASSIFY_ONLY_KEYS
