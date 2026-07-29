"""Material-change reclassify threshold + ingest summary (#675).

On an in-place update, a *trivial* edit (body similarity at/above the
configured threshold) preserves classifications; a *material* edit (below the
threshold) clears ``classification_method`` so the next classify pass re-does
only that fragment (OPS-001, no global ``--force``). ``creek ingest`` prints a
created/updated/tombed summary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter

from creek import models
from creek.cli import _run_ingest
from creek.ingest import INGESTOR_REGISTRY
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import VaultWriter
from creek_mcp.tier_ceiling import TierCeiling, tier_allowed
from creek_mcp.tools.reflect import _fragment_tier

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# difflib ratios vs _TRIVIAL: _TYPO ~0.99 (one dropped char, well above 0.9);
# _MATERIAL ~0.27 (a full rewrite, well below 0.9). The wide margins keep the
# tests robust to small threshold changes.
_TRIVIAL = "A reflection on quiet mornings and slow pours of coffee at dawn."
_TYPO = "A reflection on quiet morning and slow pours of coffee at dawn."
_MATERIAL = "Thunderous chaos erupted across the crowded city streets at noon."


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a minimal vault."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Journal",
        "10-Liminal/Orphaned",
        "personal/journal",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _journal_files(vault: Path) -> list[Path]:
    """Live fragment files under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _set_frontmatter(path: Path, **fields: object) -> None:
    """Overlay extra frontmatter keys onto an existing fragment file."""
    post = frontmatter.load(str(path))
    for key, value in fields.items():
        post[key] = value
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _ingest(
    vault: Path,
    target: Path,
    *,
    threshold: float = 0.0,
    print_summary: bool = False,
) -> tuple[int, list[str], int]:
    """Run the markdown ingestor with an explicit reclassify threshold."""
    return _run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=target,
        vault_path=vault,
        reclassify_threshold=threshold,
        print_summary=print_summary,
    )


# ---- Writer materiality (unit) -----------------------------------------


class TestUpdateMateriality:
    """update_fragment clears classification only on a material change."""

    def _seed(self, vault: Path, body: str) -> Fragment:
        frag = Fragment(
            id="frag-mat01",
            title="Day",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        VaultWriter(vault_path=vault).write_fragment(frag, body=body)
        path = _journal_files(vault)[0]
        _set_frontmatter(
            path,
            classification_method="llm",
            classified_at="2026-01-01T00:00:00+00:00",
            classification_reasoning="prior reasoning",
        )
        return frag

    def test_material_change_clears_classification(self, tmp_path: Path) -> None:
        """A below-threshold rewrite drops the classification keys."""
        vault = _make_vault(tmp_path)
        frag = self._seed(vault, _TRIVIAL)
        VaultWriter(vault_path=vault).update_fragment(
            frag, _MATERIAL, reclassify_threshold=0.9
        )
        post = frontmatter.load(str(_journal_files(vault)[0]))
        assert "classification_method" not in post.metadata
        assert "classified_at" not in post.metadata
        assert "classification_reasoning" not in post.metadata

    def test_trivial_change_preserves_classification(self, tmp_path: Path) -> None:
        """An at/above-threshold edit keeps the classification keys."""
        vault = _make_vault(tmp_path)
        frag = self._seed(vault, _TRIVIAL)
        VaultWriter(vault_path=vault).update_fragment(
            frag, _TYPO, reclassify_threshold=0.9
        )
        post = frontmatter.load(str(_journal_files(vault)[0]))
        assert post["classification_method"] == "llm"

    def test_zero_threshold_always_preserves(self, tmp_path: Path) -> None:
        """The default 0.0 threshold never clears, even on a big change."""
        vault = _make_vault(tmp_path)
        frag = self._seed(vault, _TRIVIAL)
        VaultWriter(vault_path=vault).update_fragment(frag, _MATERIAL)
        post = frontmatter.load(str(_journal_files(vault)[0]))
        assert post["classification_method"] == "llm"


# ---- End-to-end threshold gate -----------------------------------------


class TestReclassifyThresholdEndToEnd:
    """Through the ingest seam, trivial preserves and material reclassifies."""

    def test_trivial_preserves_material_reclassifies(self, tmp_path: Path) -> None:
        """A typo keeps tags; a rewrite clears classification_method."""
        vault = _make_vault(tmp_path)
        entry = vault / "personal" / "journal" / "2026-06-26.md"
        entry.write_text(f"---\ndate: 2026-06-26\n---\n{_TRIVIAL}\n", encoding="utf-8")
        _ingest(vault, entry, threshold=0.9)
        path = _journal_files(vault)[0]
        frag_id = frontmatter.load(str(path))["id"]
        _set_frontmatter(path, classification_method="llm")

        # Trivial edit -> preserved.
        entry.write_text(f"---\ndate: 2026-06-26\n---\n{_TYPO}\n", encoding="utf-8")
        _ingest(vault, entry, threshold=0.9)
        assert (
            frontmatter.load(str(_journal_files(vault)[0]))["classification_method"]
            == "llm"
        )

        # Material edit -> cleared (flagged for re-classification).
        entry.write_text(f"---\ndate: 2026-06-26\n---\n{_MATERIAL}\n", encoding="utf-8")
        _ingest(vault, entry, threshold=0.9)
        post = frontmatter.load(str(_journal_files(vault)[0]))
        assert post["id"] == frag_id  # still the same fragment
        assert "classification_method" not in post.metadata


# ---- Ingest summary -----------------------------------------------------


class TestIngestSummary:
    """The created/updated/tombed summary reports correct counts."""

    def test_summary_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """First ingest creates; an edit updates; a delete tombs."""
        vault = _make_vault(tmp_path)
        journal = vault / "personal" / "journal"
        entry = journal / "2026-06-26.md"

        entry.write_text("---\ndate: 2026-06-26\n---\nFirst entry.\n", encoding="utf-8")
        _ingest(vault, journal, print_summary=True)
        assert "1 created, 0 updated, 0 tombed" in capsys.readouterr().out

        # Re-run with NO changes: an unchanged no-op must not inflate any counter.
        _ingest(vault, journal, print_summary=True)
        assert "0 created, 0 updated, 0 tombed" in capsys.readouterr().out

        entry.write_text(
            "---\ndate: 2026-06-26\n---\nFirst entry, now meaningfully revised.\n",
            encoding="utf-8",
        )
        _ingest(vault, journal, print_summary=True)
        assert "0 created, 1 updated, 0 tombed" in capsys.readouterr().out

        entry.unlink()
        _ingest(vault, journal, print_summary=True)
        assert "0 created, 0 updated, 1 tombed" in capsys.readouterr().out


# ---- Material rewrite re-derives the privacy tier (#922) ----------------

# Tiers as they are serialised into frontmatter. Derived from the enum rather
# than hand-written literals so renaming a tier value breaks these tests
# loudly instead of silently comparing against a stale string.
_OPEN = models.PrivacyTier.OPEN.value
_INTIMATE = models.PrivacyTier.INTIMATE.value
_UNCLASSIFIED = models.PrivacyTier.UNCLASSIFIED.value

# Bodies for the ESSAY fixture. Their lengths are load-bearing twice over:
#
# * ``difflib``'s ratio is bounded above by
#   ``2 * min(len_a, len_b) / (len_a + len_b)``. The seed is 174 chars and
#   each rewrite is ~60, so that bound is ~0.5 — below the 0.9 threshold no
#   matter what the two texts happen to share. Materiality is guaranteed by
#   construction, not estimated from a sample diff.
# * Every body stays under 200 chars so ``SequenceMatcher``'s ``autojunk``
#   heuristic — which discards "popular" elements once the second sequence
#   reaches 200, and on character sequences that means nearly every letter —
#   never fires and depresses the trivial-edit ratio below the threshold.
_ESSAY_SEED = (
    "An essay on morning pages: three longhand sheets written before the "
    "day can argue back, no editing until the third page is full, because "
    "boredom is what quiets the performer."
)
_ESSAY_REWRITE_BENIGN = "A short field guide to bread: flour, water, salt, patience."
_ESSAY_REWRITE_INTIMATE = (
    "Tonight I wrote about my relapse and what sobriety costs me now."
)
# A typo-scale append (ratio 348/367 ~= 0.948, above the 0.9 threshold) that
# nonetheless drags in the recovery keyword "sponsor": a trivial edit must not
# re-tier at all, so the keyword must stay inert here.
_ESSAY_TRIVIAL_KEYWORD = _ESSAY_SEED + " My sponsor agreed."


class TestMaterialRewriteRetiers:
    """A material in-place rewrite re-derives ``privacy_tier`` from the new body.

    ``update_fragment`` cleared the classification keys on a material edit but
    left ``privacy_tier`` describing the OLD body (#922), so a fragment
    rewritten into intimate content kept an ``open`` tier and stayed
    admissible to an OPEN-ceiling MCP caller. The tier must be re-derived from
    the new body with the deterministic, LLM-free ``PrivacyClassifier`` and
    merged escalate-only against the tier already on disk.
    """

    def _seed(
        self,
        vault: Path,
        body: str,
        *,
        tier: str,
        voice_proxy_eligible: bool | None = None,
    ) -> tuple[Fragment, Path]:
        """Write a self-authored ESSAY fragment and force its on-disk tier.

        A self-authored ESSAY is the cleanest two-way fixture: a benign body
        falls through to the ESSAY rule (``open``) while a body carrying a
        recovery keyword trips the first rule (``intimate``). The title is
        deliberately keyword-free — the classifier scans title + body.
        """
        frag = Fragment(
            id="frag-tier01",
            title="Morning Pages",
            source=FragmentSource(
                platform=SourcePlatform.ESSAY,
                author=models.Authorship.SELF,
            ),
        )
        VaultWriter(vault_path=vault).write_fragment(frag, body=body)
        path = _journal_files(vault)[0]
        overlay: dict[str, object] = {"privacy_tier": tier}
        if voice_proxy_eligible is not None:
            overlay["voice_proxy_eligible"] = voice_proxy_eligible
        _set_frontmatter(path, **overlay)
        return frag, path

    def test_rewrite_into_recovery_content_is_refused_at_open_ceiling(
        self, tmp_path: Path
    ) -> None:
        """A rewrite into recovery content re-tiers to intimate AND is refused.

        The frontmatter value is only half the invariant; the read-side
        ceiling is what actually protects the user, so this asserts the
        rewritten fragment is inadmissible to an OPEN-ceiling MCP caller.
        """
        vault = _make_vault(tmp_path)
        frag, path = self._seed(vault, _ESSAY_SEED, tier=_OPEN)

        VaultWriter(vault_path=vault).update_fragment(
            frag, _ESSAY_REWRITE_INTIMATE, reclassify_threshold=0.9
        )

        post = frontmatter.load(str(path))
        assert post["privacy_tier"] == _INTIMATE
        assert tier_allowed(_fragment_tier(post.metadata), TierCeiling.OPEN) is False

    def test_rewrite_into_benign_content_stays_open(self, tmp_path: Path) -> None:
        """A benign rewrite keeps ``open`` — no blanket blackout on every edit.

        Re-tiering must derive the tier from the new body, not pessimistically
        bury every materially-edited fragment at the most restrictive tier.
        """
        vault = _make_vault(tmp_path)
        frag, path = self._seed(vault, _ESSAY_SEED, tier=_OPEN)

        VaultWriter(vault_path=vault).update_fragment(
            frag, _ESSAY_REWRITE_BENIGN, reclassify_threshold=0.9
        )

        post = frontmatter.load(str(path))
        assert post["privacy_tier"] == _OPEN
        assert tier_allowed(_fragment_tier(post.metadata), TierCeiling.OPEN) is True

    def test_rewrite_never_downgrades_an_intimate_tier(self, tmp_path: Path) -> None:
        """Escalate-only: a benign rewrite cannot lower ``intimate`` on disk.

        The merge base is the tier *on disk*, not the in-memory fragment's
        (which still carries the ``unclassified`` pipeline default), so an
        operator's restrictive decision survives a full-body rewrite.
        """
        vault = _make_vault(tmp_path)
        frag, path = self._seed(vault, _ESSAY_SEED, tier=_INTIMATE)

        VaultWriter(vault_path=vault).update_fragment(
            frag, _ESSAY_REWRITE_BENIGN, reclassify_threshold=0.9
        )

        post = frontmatter.load(str(path))
        assert post["privacy_tier"] == _INTIMATE
        assert tier_allowed(_fragment_tier(post.metadata), TierCeiling.OPEN) is False

    def test_trivial_edit_leaves_the_tier_untouched(self, tmp_path: Path) -> None:
        """An at/above-threshold edit does not re-tier, keyword or not.

        Re-tiering is bound to the same materiality gate as clearing the
        classification keys: a typo-scale edit changes nothing, even when the
        appended text would have classified INTIMATE on its own.
        """
        vault = _make_vault(tmp_path)
        frag, path = self._seed(vault, _ESSAY_SEED, tier=_OPEN)

        VaultWriter(vault_path=vault).update_fragment(
            frag, _ESSAY_TRIVIAL_KEYWORD, reclassify_threshold=0.9
        )

        post = frontmatter.load(str(path))
        assert post["privacy_tier"] == _OPEN
        assert post["voice_proxy_eligible"] is True

    def test_rewrite_refreshes_derived_voice_proxy_eligible(
        self, tmp_path: Path
    ) -> None:
        """The derived eligibility flag follows the re-derived tier down.

        ``voice_proxy_eligible`` is a computed field of ``privacy_tier`` and
        ``source.author``; leaving a stale ``true`` on an intimate fragment
        would feed it straight into voice-proxy generation.
        """
        vault = _make_vault(tmp_path)
        frag, path = self._seed(
            vault, _ESSAY_SEED, tier=_OPEN, voice_proxy_eligible=True
        )

        VaultWriter(vault_path=vault).update_fragment(
            frag, _ESSAY_REWRITE_INTIMATE, reclassify_threshold=0.9
        )

        post = frontmatter.load(str(path))
        assert post["voice_proxy_eligible"] is False

    def test_untiered_fragment_gets_a_tier_assigned_outright(
        self, tmp_path: Path
    ) -> None:
        """``unclassified`` on disk means "no decision yet" — take the candidate.

        This is the ``needs_tier`` branch: there is nothing to escalate
        against, so the fresh verdict is written as-is rather than merged.
        """
        vault = _make_vault(tmp_path)
        frag, path = self._seed(vault, _ESSAY_SEED, tier=_UNCLASSIFIED)

        VaultWriter(vault_path=vault).update_fragment(
            frag, _ESSAY_REWRITE_INTIMATE, reclassify_threshold=0.9
        )

        post = frontmatter.load(str(path))
        assert post["privacy_tier"] == _INTIMATE
        assert tier_allowed(_fragment_tier(post.metadata), TierCeiling.OPEN) is False

    def test_unrecognised_disk_tier_fails_closed_to_intimate(
        self, tmp_path: Path
    ) -> None:
        """An unparseable on-disk tier is read as ``intimate``, not ``open``.

        Frontmatter is hand-editable and forward-dated vaults may carry tier
        spellings this build has never heard of. ``_tier_on_disk`` refuses to
        guess: an unrecognised value is one nobody can vouch for, so the merge
        base becomes ``intimate`` and the escalate-only merge can only hold
        there. The rewrite body is deliberately *benign* — the classifier's
        candidate for it is ``open``, so the only route to an ``intimate``
        result is the fail-closed parse of ``"bogus-tier"``. A recovery-keyword
        body would have passed on the candidate alone and proved nothing.
        """
        vault = _make_vault(tmp_path)
        frag, path = self._seed(vault, _ESSAY_SEED, tier="bogus-tier")

        VaultWriter(vault_path=vault).update_fragment(
            frag, _ESSAY_REWRITE_BENIGN, reclassify_threshold=0.9
        )

        post = frontmatter.load(str(path))
        assert post["privacy_tier"] == _INTIMATE
        assert tier_allowed(_fragment_tier(post.metadata), TierCeiling.OPEN) is False
