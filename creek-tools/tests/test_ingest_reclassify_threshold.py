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

from creek.cli import _run_ingest
from creek.ingest import INGESTOR_REGISTRY
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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

        entry.write_text(
            "---\ndate: 2026-06-26\n---\nFirst entry, now meaningfully revised.\n",
            encoding="utf-8",
        )
        _ingest(vault, journal, print_summary=True)
        assert "0 created, 1 updated, 0 tombed" in capsys.readouterr().out

        entry.unlink()
        _ingest(vault, journal, print_summary=True)
        assert "0 created, 0 updated, 1 tombed" in capsys.readouterr().out
