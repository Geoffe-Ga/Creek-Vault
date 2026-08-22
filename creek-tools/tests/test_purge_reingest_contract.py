"""What ingest does to a vault a purge has emptied (#1453).

Destroying the ingest ledger closes the leak, and it also changes the
unchanged / changed / gone contract the next ingest runs under. Three of
those consequences are load-bearing enough to pin:

* the vault stays **usable** — a keep-list short by one entry breaks
  ``creek ingest`` or ``creek doctor`` against the purged vault, and that
  is the only cheap way to detect over-deletion;
* nothing is **tombed**, because ``live_keys()`` is empty and the gone
  branch has no ledgered key to miss;
* the ``#1329`` un-pinned-vault advisory stays **silent**, because it is
  guarded on the vault holding fragments and after a vault purge it holds
  none. That guard is what stops a correct erasure from producing a
  scary, inapplicable warning on the very next run.

The fourth is the one the issue asked to be prevented and cannot be: the
re-ingested fragment carries **the same id it had before the purge**.
That is asserted here rather than left implicit, so the behaviour is
pinned as intended rather than rediscovered as a bug. See
:func:`test_reingest_after_a_vault_purge_reuses_the_same_id`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.ledger import SourceLedger, ledger_dir
from creek.ingest.pipeline import _UNPINNED_VAULT_WARNING, run_ingest
from creek.purge import PurgeEngine
from creek.purge.engine import VAULT_PURGE_CONFIRMATION

if TYPE_CHECKING:
    from pathlib import Path

from creek.ingest.pipeline import IngestRunResult

pytestmark = pytest.mark.integration


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a vault the markdown ingestor and the purge engine accept.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for folder in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Notes",
        "02-Threads",
        "03-Eddies",
        "04-Praxis",
    ):
        (vault / folder).mkdir(parents=True, exist_ok=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# vault marker (GAP-003)\n",
        encoding="utf-8",
    )
    return vault


def _make_source(tmp_path: Path) -> Path:
    """Write one markdown source file for the ingestor to consume.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The source *directory*, so full-source passes see a whole source.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "2024-05-01-therapy.md").write_text(
        "# Therapy notes\n\nSomething private.\n",
        encoding="utf-8",
    )
    return source


def _ingest(vault: Path, source: Path) -> IngestRunResult:
    """Run the markdown ingestor over a whole source directory.

    A *directory* input on purpose: the gone branch only computes a tomb
    set on a full-source pass, so a single-file input would make
    :func:`test_reingest_after_a_vault_purge_tombs_nothing` pass for the
    wrong reason.

    Args:
        vault: Vault root.
        source: Source directory.

    Returns:
        The structured run result, including ``tombed`` and ``warnings``.
    """
    return run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=source,
        vault_path=vault,
    )


def _fragment_ids(vault: Path) -> list[str]:
    """Return every fragment id currently under ``01-Fragments/``.

    Args:
        vault: Vault root.

    Returns:
        Ids, in path order.
    """
    return [
        str(frontmatter.load(str(path)).get("id"))
        for path in sorted((vault / "01-Fragments").rglob("*.md"))
    ]


def test_a_vault_purge_leaves_the_ingest_ledger_directory_empty(
    tmp_path: Path,
) -> None:
    """End to end: ingest, purge the vault, and the ledger files are gone.

    The issue's own AC #3, driven through the real ingest path rather
    than a hand-written row, so a change to where the ledger is written
    cannot leave this passing against a file nobody produces any more.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _ingest(vault, source)
    assert list(ledger_dir(vault).glob("*.jsonl"))

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert not list(ledger_dir(vault).glob("*.jsonl"))
    assert len(SourceLedger.load(vault, source="markdown")) == 0


def test_reingest_after_a_vault_purge_tombs_nothing(tmp_path: Path) -> None:
    """``live_keys()`` is empty, so the gone branch has nothing to tomb (AC #19).

    Without this the erasure would have a nasty second-order effect: a
    full-source pass would see every purged unit as vanished and start
    soft-tombing fragments that no longer exist.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _ingest(vault, source)
    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    result = _ingest(vault, source)

    assert result.tombed == 0


def test_reingest_after_a_vault_purge_is_not_warned_as_unpinned(
    tmp_path: Path,
) -> None:
    """The #1329 advisory stays silent on a purged vault (AC #18).

    It fires on "markdown fragments present, ledger empty" — which is
    exactly what a half-migrated vault looks like, and exactly what a
    purged vault would look like if the fragment wipe had not also run.
    The guard that saves it is ``any(fragments_root.rglob('*.md'))``.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _ingest(vault, source)
    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    result = _ingest(vault, source)

    assert _UNPINNED_VAULT_WARNING not in result.warnings


def test_reingest_after_a_vault_purge_reuses_the_same_id(tmp_path: Path) -> None:
    """The purged id comes back, and that is the documented behaviour (#1453).

    The issue's AC #6 asked for the opposite, and no ledger change can
    deliver it: ``generate_fragment_id`` hashes source path, timestamp
    and content, and the markdown ingestor derives the timestamp from
    epoch mtime *precisely so ids reproduce*. Removing the ledger from
    the vault entirely still reproduces the id.

    Pinned as intended behaviour rather than left unasserted, so that
    the day somebody salts fragment ids they meet a test that says out
    loud what is changing. Anyone who can re-derive the id already holds
    the plaintext it was derived from; what the erasure removes is the
    vault's stored *mapping* from source path and content hash to it.
    """
    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _ingest(vault, source)
    before = _fragment_ids(vault)
    assert len(before) == 1

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)
    assert _fragment_ids(vault) == []

    _ingest(vault, source)

    assert _fragment_ids(vault) == before


def test_the_purged_vault_is_still_a_working_vault(tmp_path: Path) -> None:
    """Over-deletion proof: ingest and doctor both still succeed (AC #24).

    The cheapest detector for a keep-list that is one entry short. A
    sweep that took ``creek_config.yaml`` passes every erasure assertion
    in the suite and leaves a directory ``creek`` no longer recognises as
    a vault — including for the *next* purge, whose marker check would
    then refuse.
    """
    from typer.testing import CliRunner

    from creek.cli import app

    vault = _make_vault(tmp_path)
    source = _make_source(tmp_path)
    _ingest(vault, source)
    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    # The marker check a second purge depends on still passes.
    PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)

    reingest = _ingest(vault, source)
    assert reingest.written == 1
    assert reingest.errors == []

    lint = CliRunner().invoke(app, ["lint", "--vault", str(vault)])
    assert lint.exit_code == 0, lint.output


def test_the_swept_skill_tree_is_redeployable(tmp_path: Path) -> None:
    """``creek skills sync`` restores what the purge swept (#1453).

    The keep-list drops ``00-Creek-Meta/Skills/`` because operators drop
    their own skill files there and one of those may quote a purged
    fragment. That decision is only defensible if the *canonical* tree
    comes back on demand, which is what both the docs and the keep-list
    reason claim — so the claim is tested rather than asserted.

    The operator-authored file stays gone. That is the point of sweeping
    the directory, not a shortfall of the restore.
    """
    from creek.scaffold import deploy_skills

    vault = _make_vault(tmp_path)
    skills = vault / "00-Creek-Meta" / "Skills"
    skills.mkdir(parents=True, exist_ok=True)
    deploy_skills(vault)
    canonical = sorted(p.name for p in skills.rglob("*") if p.is_file())
    assert canonical, "the packaged skill tree seeded nothing to test with"
    custom = skills / "my-own.SKILL.md"
    custom.write_text("quotes [[Therapy notes]]\n", encoding="utf-8")

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)
    assert not skills.exists() or not any(skills.rglob("*.md"))

    deploy_skills(vault)

    assert sorted(p.name for p in skills.rglob("*") if p.is_file()) == canonical
    assert not custom.exists()
