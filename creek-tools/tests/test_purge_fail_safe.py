"""A purge is never vetoed by a corrupt artifact, and never over-claims (#1265).

Four defects, one rule: **a right-to-be-forgotten erasure must not be
stoppable by an unparseable derived file, and must not report destroying
something it left on disk.**

* #1481 — ``journal_staged_removed`` was incremented *before* the
  ``unlink``, so an ``OSError`` from a read-only mount produced a
  ``status="partial"`` audit line whose count included a staged file
  still holding the entry's full plaintext body.
* #1455 — ``RecursionError`` and ``MemoryError`` are outside
  ``FRONTMATTER_LOAD_ERRORS``, so a nesting or alias bomb in one
  fragment's frontmatter aborted the whole purge: a denial-of-erasure
  primitive in a vault that ingests other people's exports.
* #1480 — a truncated ``embeddings.parquet`` raised ``ArrowInvalid`` out
  of the last statement of ``purge_vault``'s body and aborted it.
* #1547 — the meta sweep removes files and symlinks and never a
  directory, so an identifying directory *name* survived a whole-vault
  purge as an empty folder.

The #1455 parser exhaustion is provoked by **patching the loader**, not
by writing a real YAML bomb. A genuine ``[[[[…`` document does not raise
portably: whether PyYAML answers ``RecursionError`` or the interpreter
overruns its C stack and dies depends on the recursion limit and the
thread stack size of the machine running the test. Patching asserts the
contract — "when the parser raises this, the purge continues" — which is
the part that has to hold on every runner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.purge import PurgeEngine
from creek.purge.engine import VAULT_PURGE_CONFIRMATION

if TYPE_CHECKING:
    from pathlib import Path

FRAGMENT_ID = "frag-hostile"
"""The one fragment every vault in this module holds."""


def _make_vault(tmp_path: Path) -> Path:
    """Build a minimal but real Creek vault with one fragment.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# vault marker (GAP-003)\n",
        encoding="utf-8",
    )
    frag_dir = vault / "01-Fragments" / "Journal"
    frag_dir.mkdir(parents=True)
    (frag_dir / "entry.md").write_text(
        f"---\nid: {FRAGMENT_ID}\ntitle: Entry\n"
        "created: 2026-03-11\nsource:\n  platform: discord\n---\n\nbody\n",
        encoding="utf-8",
    )
    return vault


# ---------------------------------------------------------------------------
# #1481 — the counter must not over-claim an erasure
# ---------------------------------------------------------------------------


def _stage_journal_entry(vault: Path) -> Path:
    """Write a staged Adepthood body and point the fragment at it.

    Args:
        vault: Vault root built by :func:`_make_vault`.

    Returns:
        The staged file's path.
    """
    staged = vault / "00-Creek-Meta" / "adepthood" / "journal" / "entry.md"
    staged.parent.mkdir(parents=True)
    staged.write_text("the entry's full plaintext body\n", encoding="utf-8")

    frag_file = vault / "01-Fragments" / "Journal" / "entry.md"
    post = frontmatter.load(str(frag_file))
    post["source"] = {
        "platform": "adepthood",
        "origin_key": "00-Creek-Meta/adepthood/journal/entry.md",
    }
    frag_file.write_text(frontmatter.dumps(post), encoding="utf-8")
    return staged


def test_a_failed_unlink_does_not_count_a_staged_file_as_erased(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped purge counts a staged source file only once it is really gone.

    The #1481 defect in one assertion: with the increment ahead of the
    ``unlink``, the ``OSError`` propagated out of a result that already
    read ``journal_staged_removed == 1`` — an audit line certifying the
    destruction of a file whose full plaintext body was still on disk.
    """
    vault = _make_vault(tmp_path)
    staged = _stage_journal_entry(vault)
    engine = PurgeEngine(vault)

    real_unlink = type(staged).unlink

    def refuse_staged(self: Path, *args: object, **kwargs: object) -> None:
        """Fail the staged file's unlink the way a read-only mount does."""
        if self == staged:
            msg = "Read-only file system"
            raise OSError(msg)
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]  # Issue #1481: pathlib.Path.unlink is untyped for *args

    monkeypatch.setattr("pathlib.Path.unlink", refuse_staged)

    with pytest.raises(OSError, match="Read-only file system"):
        engine.purge_fragment(FRAGMENT_ID)

    assert staged.is_file(), "the guard must leave the staged file on disk"


def test_a_failed_unlink_leaves_the_audit_count_at_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partial audit line names zero staged removals, not one.

    Asserting the *record* rather than the in-memory result: the audit
    log is what outlives the purge, and it is where the over-claim would
    have been read back by a compliance auditor.
    """
    from creek.purge import PurgeAuditLog

    vault = _make_vault(tmp_path)
    staged = _stage_journal_entry(vault)
    engine = PurgeEngine(vault)

    real_unlink = type(staged).unlink

    def refuse_staged(self: Path, *args: object, **kwargs: object) -> None:
        """Fail only the staged file's unlink."""
        if self == staged:
            msg = "Read-only file system"
            raise OSError(msg)
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]  # Issue #1481: pathlib.Path.unlink is untyped for *args

    monkeypatch.setattr("pathlib.Path.unlink", refuse_staged)

    with pytest.raises(OSError, match="Read-only file system"):
        engine.purge_fragment(FRAGMENT_ID)

    outcomes = [
        entry for entry in PurgeAuditLog(vault).read() if entry.phase == "outcome"
    ]
    assert outcomes, "the aborted purge still wrote an outcome line"
    assert outcomes[-1].status == "partial"
    assert outcomes[-1].journal_staged_removed == 0


def test_the_vault_staging_sweep_counts_only_what_it_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second #1481 site: ``_wipe_adepthood_staging``'s own counter.

    Two staged files, the *second* of which refuses to be unlinked. The
    count must read ``1`` — the one that really went — and never ``2``.
    """
    vault = _make_vault(tmp_path)
    staging = vault / "00-Creek-Meta" / "adepthood" / "journal"
    staging.mkdir(parents=True)
    first = staging / "a-entry.md"
    second = staging / "b-entry.md"
    first.write_text("first body\n", encoding="utf-8")
    second.write_text("second body\n", encoding="utf-8")

    engine = PurgeEngine(vault)
    real_unlink = type(second).unlink

    def refuse_second(self: Path, *args: object, **kwargs: object) -> None:
        """Fail only the second staged file's unlink."""
        if self == second:
            msg = "Read-only file system"
            raise OSError(msg)
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]  # Issue #1481: pathlib.Path.unlink is untyped for *args

    monkeypatch.setattr("pathlib.Path.unlink", refuse_second)

    with pytest.raises(OSError, match="Read-only file system"):
        engine.purge_vault(VAULT_PURGE_CONFIRMATION)

    from creek.purge import PurgeAuditLog

    outcomes = [
        entry for entry in PurgeAuditLog(vault).read() if entry.phase == "outcome"
    ]
    assert outcomes[-1].journal_staged_removed == 1
    assert second.is_file()


# ---------------------------------------------------------------------------
# #1455 — hostile frontmatter must not veto a purge
# ---------------------------------------------------------------------------


def _defeat_the_parser_with(
    monkeypatch: pytest.MonkeyPatch,
    exc_type: type[BaseException],
) -> None:
    """Make every ``frontmatter.load`` in the purge engine raise *exc_type*.

    Stands in for a YAML nesting or alias bomb, which cannot be written
    portably: a real one either raises ``RecursionError`` or overruns the
    C stack and kills the interpreter, depending on the runner's
    recursion limit and thread stack size.

    Args:
        monkeypatch: Pytest patcher.
        exc_type: The exhaustion PyYAML would have raised.
    """

    def explode(*_args: object, **_kwargs: object) -> frontmatter.Post:
        """Raise the parser exhaustion under test."""
        raise exc_type

    monkeypatch.setattr("creek.purge.engine.frontmatter.load", explode)


@pytest.mark.parametrize("exc_type", [RecursionError, MemoryError])
def test_a_frontmatter_bomb_does_not_veto_a_vault_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_type: type[BaseException],
) -> None:
    """``purge vault`` completes, and still destroys and counts the fragment.

    The three acceptance criteria of #1455 in one test: the purge does
    not abort, the unreadable fragment is still counted in
    ``fragments_affected`` (the wipe destroys it regardless of whether
    anything could read it), and it contributes no id to
    ``affected_fragment_ids`` — naming an id nobody could read would be
    a fabrication in a compliance record.
    """
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)
    _defeat_the_parser_with(monkeypatch, exc_type)

    result = engine.purge_vault(VAULT_PURGE_CONFIRMATION)

    assert result.fragments_affected == 1
    assert result.affected_fragment_ids == []
    assert not (vault / "01-Fragments" / "Journal" / "entry.md").exists()


@pytest.mark.parametrize("exc_type", [RecursionError, MemoryError])
def test_a_frontmatter_bomb_does_not_veto_a_source_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_type: type[BaseException],
) -> None:
    """``purge source`` returns a result instead of aborting.

    A file that cannot be parsed matches nothing, so it is left on disk
    — the same answer this engine already gives for every other
    unparseable file, and the restrictive one. What must not happen is
    the exception escaping and taking the operation with it.
    """
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)
    _defeat_the_parser_with(monkeypatch, exc_type)

    result = engine.purge_source("discord")

    assert result.fragments_affected == 0
    assert (vault / "01-Fragments" / "Journal" / "entry.md").is_file()


@pytest.mark.parametrize("exc_type", [RecursionError, MemoryError])
def test_a_frontmatter_bomb_does_not_veto_a_daterange_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_type: type[BaseException],
) -> None:
    """``purge daterange`` returns a result instead of aborting."""
    from datetime import date

    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)
    _defeat_the_parser_with(monkeypatch, exc_type)

    result = engine.purge_daterange(date(2026, 1, 1), date(2026, 12, 31))

    assert result.fragments_affected == 0


def test_the_write_safe_loader_still_lets_a_parser_bomb_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrow tuple on the rewrite path is deliberately unchanged (#1455).

    ``purge_classifications`` rewrites the post it reads. There a parse
    failure protects the operator's bytes from a lossy re-encode, and
    there is no erasure for the exception to veto — so the widening is
    scoped to the delete-decision loaders and this path keeps raising.
    Pinning it stops a future "tidy-up" from making the two loaders
    symmetric.
    """
    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)
    _defeat_the_parser_with(monkeypatch, RecursionError)

    with pytest.raises(RecursionError):
        engine.purge_classifications()


def test_a_frontmatter_bomb_is_reported_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Continuing past the bomb is only safe if the operator is told.

    The warning names the file (an operator has to go and look at it)
    and the exception **type**, never the parser's message — a
    ``MarkedYAMLError`` stringifies with the offending source snippet,
    which here is vault content.
    """
    import logging

    vault = _make_vault(tmp_path)
    engine = PurgeEngine(vault)
    _defeat_the_parser_with(monkeypatch, RecursionError)

    with caplog.at_level(logging.WARNING, logger="creek.purge.engine"):
        engine.purge_source("discord")

    warnings = [rec.getMessage() for rec in caplog.records]
    assert any("exhausted the YAML parser" in msg for msg in warnings)
    assert any("RecursionError" in msg for msg in warnings)


# ---------------------------------------------------------------------------
# #1480 — a corrupt embeddings cache must not veto a vault purge
# ---------------------------------------------------------------------------


def test_a_corrupt_embeddings_cache_does_not_veto_a_vault_purge(
    tmp_path: Path,
) -> None:
    """A truncated parquet is deleted anyway and the purge reports complete.

    Reproduces the issue's own recipe: four bytes where a parquet
    footer should be. ``pq.read_metadata`` raises ``ArrowInvalid`` — a
    ``ValueError`` — from the *last* statement of ``purge_vault``'s
    body, so before this fix the operation aborted with an unhandled
    exception. A derived cache holding no authoritative data must not be
    able to veto an erasure, and it must not survive one either.
    """
    vault = _make_vault(tmp_path)
    cache = vault / "00-Creek-Meta" / "embeddings.parquet"
    cache.write_bytes(b"PAR1")

    result = PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert not cache.exists(), "the unreadable cache is still destroyed"
    assert result.embeddings_removed == 0
    assert result.outcome_status == "complete"


def test_a_corrupt_embeddings_cache_preview_removes_nothing(
    tmp_path: Path,
) -> None:
    """The dry run reports the same zero and leaves the file alone.

    A preview of an irreversible erasure must not diverge from its apply
    twin, and must not delete anything on the way to saying so.
    """
    vault = _make_vault(tmp_path)
    cache = vault / "00-Creek-Meta" / "embeddings.parquet"
    cache.write_bytes(b"PAR1")

    preview = PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert preview.embeddings_removed == 0
    assert cache.is_file()


# ---------------------------------------------------------------------------
# #1547 — identifying directory NAMES must not survive
# ---------------------------------------------------------------------------


def _seed_identifying_dirs(vault: Path) -> Path:
    """Seed the issue's reproduction: a channel-named staging directory.

    Args:
        vault: Vault root.

    Returns:
        The channel directory whose *name* must not survive.
    """
    channel = (
        vault
        / "00-Creek-Meta"
        / "State"
        / "discord"
        / "capture-staging"
        / "messages"
        / "secret-therapy-channel"
    )
    channel.mkdir(parents=True)
    (channel / "messages.json").write_text('[{"content": "raw"}]', encoding="utf-8")
    return channel


def test_an_identifying_directory_name_does_not_survive_a_vault_purge(
    tmp_path: Path,
) -> None:
    """The channel *name* is gone, not merely the file inside it (#1547).

    Asserted against a walk of the whole vault rather than against the
    one path, because the defect was that the sweep removed files and
    links but never a directory: any surviving component of that nested
    layout is the same leak.
    """
    vault = _make_vault(tmp_path)
    channel = _seed_identifying_dirs(vault)

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    survivors = {path.as_posix() for path in vault.rglob("*")}
    assert not any("secret-therapy-channel" in name for name in survivors)
    assert not channel.exists()
    assert not (vault / "00-Creek-Meta" / "State").exists()


def test_the_directory_holding_a_kept_file_survives(tmp_path: Path) -> None:
    """``audit/`` outlives the purge — by holding a keep, not by being named.

    The prune is ``rmdir``-based, so a directory that still holds the
    erasure record cannot be removed. That is the property that keeps
    the compliance log reachable, and it must not be traded away for a
    tidier walk.
    """
    vault = _make_vault(tmp_path)
    _seed_identifying_dirs(vault)

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    audit_log = vault / "00-Creek-Meta" / "audit" / "purge.jsonl"
    assert audit_log.is_file()
    assert (vault / "00-Creek-Meta" / "creek_config.yaml").is_file()
    assert (vault / "00-Creek-Meta").is_dir()


def test_the_prune_never_follows_a_symlink_out_of_the_meta_directory(
    tmp_path: Path,
) -> None:
    """A link to an out-of-vault directory is not descended, and not emptied.

    ``Path.is_dir()`` follows a symlink, so a prune that asked that
    question first would walk *through* the link and ``rmdir`` a tree
    outside the vault entirely. The link is an occupant of its parent
    and nothing more.
    """
    outside = tmp_path / "outside"
    (outside / "keepme").mkdir(parents=True)

    vault = _make_vault(tmp_path)
    linkdir = vault / "00-Creek-Meta" / "linked"
    linkdir.mkdir()
    (linkdir / "elsewhere").symlink_to(outside, target_is_directory=True)

    PurgeEngine(vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert (outside / "keepme").is_dir(), "the prune must not reach outside the vault"


def test_a_dry_run_prunes_no_directory_and_moves_no_count(tmp_path: Path) -> None:
    """The preview removes nothing and reports exactly what the apply run does.

    The prune is uncounted by design — an empty directory is a name, not
    a destroyed artifact — so the two runs agree on every number, and
    the preview leaves the filesystem untouched.
    """
    preview_vault = _make_vault(tmp_path / "preview")
    apply_vault = _make_vault(tmp_path / "apply")
    channel = _seed_identifying_dirs(preview_vault)
    _seed_identifying_dirs(apply_vault)

    preview = PurgeEngine(preview_vault, dry_run=True).purge_vault(
        VAULT_PURGE_CONFIRMATION,
    )
    applied = PurgeEngine(apply_vault).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert channel.is_dir(), "a dry run removes no directory"
    assert preview.meta_artifacts_removed == applied.meta_artifacts_removed
    assert preview.fragments_affected == applied.fragments_affected
