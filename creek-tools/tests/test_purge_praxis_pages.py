"""Right-to-be-forgotten must reach the ``04-Praxis`` pages a fragment produced (#2122).

Since #2122 the classify post-pass writes a ``04-Praxis/`` page per explicit
fragment, titled with that fragment's **first sentence** — so the page is a
copy of the fragment's words, not merely a reference to its id. The existing
provenance scrub replaces the id and leaves the sentence on disk; these tests
pin that a page derived *solely* from the purged fragment is deleted instead,
that a page with other surviving sources keeps the existing scrub, and that
the deletion is counted, previewed by a dry run, and written to the audit log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
from typer.testing import CliRunner

from creek.classify.praxis_writer import write_praxis_pages
from creek.cli import app
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PraxisPotential,
    PrivacyTier,
    SourcePlatform,
)
from creek.purge.audit import PurgeAuditLog
from creek.purge.engine import PurgeEngine
from creek.scaffold import scaffold_vault
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

SENTINEL = "QZVKX-PRAXIS-2122"
"""Byte sentinel unique to the purged fragment's first sentence."""

_BODY = f"From now on, I sit {SENTINEL} every morning.\nI will keep it short."


def _vault(tmp_path: Path) -> Path:
    """Scaffold a real vault and seed one explicit fragment with its page."""
    vault = tmp_path / "vault"
    scaffold_vault(vault)
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-purge-2122",
            title="Cushion",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN, author=Authorship.SELF
            ),
            praxis_potential=PraxisPotential.EXPLICIT,
            privacy_tier=PrivacyTier.PERSONAL,
        ),
        body=_BODY,
    )
    assert write_praxis_pages(vault).written == 1
    return vault


def _praxis_pages(vault: Path) -> list[Path]:
    """Return every markdown page under ``04-Praxis``, sorted."""
    return sorted((vault / "04-Praxis").rglob("*.md"))


def _notes_containing(vault: Path, needle: str) -> list[Path]:
    """Return every vault markdown note whose bytes contain *needle*.

    Markdown only, deliberately. The writer's ``.id-index.jsonl`` and the
    ``Processing-Log/provenance.jsonl`` both record the title-derived
    *filename* of every page :class:`~creek.vault.writer.VaultWriter`
    writes, and no purge path rewrites either of them — for fragments,
    threads and eddies as much as for praxis pages. That residual predates
    #2122 and is reported separately rather than half-fixed for one folder.
    """
    probe = needle.encode("utf-8")
    return [p for p in vault.rglob("*.md") if probe in p.read_bytes()]


def test_purging_the_sole_source_deletes_its_praxis_page(tmp_path: Path) -> None:
    """No copy of the purged fragment's sentence survives in ``04-Praxis``."""
    vault = _vault(tmp_path)
    assert _praxis_pages(vault)

    result = PurgeEngine(vault).purge_fragment("frag-purge-2122")

    assert result.praxis_pages_removed == 1
    assert _praxis_pages(vault) == []
    assert _notes_containing(vault, SENTINEL) == []


def test_a_dry_run_counts_the_page_and_deletes_nothing(tmp_path: Path) -> None:
    """The preview reports the same count an apply run would produce."""
    vault = _vault(tmp_path)
    before = _praxis_pages(vault)

    result = PurgeEngine(vault, dry_run=True).purge_fragment("frag-purge-2122")

    assert result.praxis_pages_removed == 1
    assert _praxis_pages(vault) == before


def test_a_page_with_another_source_is_scrubbed_not_deleted(tmp_path: Path) -> None:
    """An owner-curated, multi-source page keeps the existing id scrub."""
    vault = _vault(tmp_path)
    shared = vault / "04-Praxis" / "Seasonal" / "shared.md"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(
        "---\ntype: praxis\ntitle: Shared practice\npraxis_type: framework\n"
        "status: active\nderived_from:\n  - frag-purge-2122\n  - frag-other\n"
        "---\n\nOwner prose.\n",
        encoding="utf-8",
    )

    result = PurgeEngine(vault).purge_fragment("frag-purge-2122")

    assert result.praxis_pages_removed == 1
    assert _praxis_pages(vault) == [shared]
    text = shared.read_text(encoding="utf-8")
    assert "frag-purge-2122" not in text
    assert "frag-other" in text


def test_the_scoped_purge_path_deletes_the_page_too(tmp_path: Path) -> None:
    """``purge_source`` (via ``_purge_single``) reaches the page as well."""
    vault = _vault(tmp_path)

    result = PurgeEngine(vault).purge_source("markdown")

    assert result.praxis_pages_removed == 1
    assert _praxis_pages(vault) == []


def test_the_audit_outcome_records_the_deletion(tmp_path: Path) -> None:
    """The append-only log says a derived page went, not just the fragment."""
    vault = _vault(tmp_path)

    PurgeEngine(vault).purge_fragment("frag-purge-2122")

    outcome = [e for e in PurgeAuditLog(vault).read() if e.phase == "outcome"]
    assert outcome[-1].praxis_pages_removed == 1


def test_an_unrelated_or_malformed_page_is_left_alone(tmp_path: Path) -> None:
    """Only a ``type: praxis`` page naming exactly the purged id is deleted."""
    vault = _vault(tmp_path)
    root = vault / "04-Praxis" / "Situational"
    root.mkdir(parents=True, exist_ok=True)
    keep = [
        root / "other.md",
        root / "not-praxis.md",
    ]
    keep[0].write_text(
        "---\ntype: praxis\ntitle: O\nderived_from: [frag-other]\n---\n",
        encoding="utf-8",
    )
    keep[1].write_text(
        "---\ntype: note\ntitle: N\nderived_from: [frag-purge-2122]\n---\n",
        encoding="utf-8",
    )

    result = PurgeEngine(vault).purge_fragment("frag-purge-2122")

    assert result.praxis_pages_removed == 1
    assert all(path.exists() for path in keep)
    assert frontmatter.load(str(keep[0])).metadata["derived_from"] == ["frag-other"]


def test_the_cli_reports_the_removed_page(tmp_path: Path) -> None:
    """``creek purge fragment`` tells the operator a derived page went too."""
    vault = _vault(tmp_path)

    result = CliRunner().invoke(
        app, ["purge", "fragment", "frag-purge-2122", "--vault", str(vault), "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "Praxis pages removed: 1" in result.output
