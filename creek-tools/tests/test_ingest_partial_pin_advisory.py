"""The un-pinned advisory must survive a partially-pinned vault (#1367).

RED-FIRST. :func:`creek.ingest.pipeline.unpinned_vault_warning` derives its
whole signal from the markdown ledger being *empty*::

    if len(SourceLedger.load(vault_path, source=LEDGERED_SOURCE_TYPE)) > 0:
        return None

and :func:`creek.ingest.pin_ids.pin_source_ids` refuses to pin any
``source_key`` claimed by more than one live fragment, returning those
``conflicts`` **in memory only** — nothing is persisted. So the realistic
migration outcome is a *partially* pinned vault: the clean sources are pinned,
the duplicated ones are not, the ledger is now non-empty, and the advisory
goes permanently silent about exactly the fragments still at risk of being
re-minted on the next ingest.

The advisory has to become a statement about *what remains unpinned*, backed
by state that outlives the process that computed it, and it has to clear
itself when the operator resolves the duplicate and re-pins — an advisory that
cannot go quiet is one operators learn to ignore.

Disclosure: the conflicted ``source_key`` is a path inside the operator's own
vault, so the operator-facing rendering may name it and the ceiling-safe twin
may not (#1372, producer-side). Both halves are asserted, because an advisory
that travels with a path in it is a leak and one that never travels is the
#1372 defect it was written to fix.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.ledger import SourceLedger, ledger_dir
from creek.ingest.pin_ids import pin_source_ids
from creek.ingest.pipeline import IngestRunResult, run_ingest
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path

_CONFLICTED_KEY = "00-Inbox/dup.md"
_CLEAN_KEY = "00-Inbox/clean.md"
_PINNED_MTIME = datetime(2024, 1, 5, 8, 30, tzinfo=UTC)

# Any fragment id, and any vault path, in a rendering that may cross a ceiling.
_FRAGMENT_ID_TOKEN = re.compile(r"frag-[0-9a-f]{6,}")

_ENTRY = "---\ndate: 2024-01-05\n---\n\n# Entry\n\nBody text.\n"


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree ``VaultWriter`` and the ledger need."""
    vault = tmp_path / "vault"
    for relative in (
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/State/ingest",
        "00-Inbox",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "10-Liminal/Orphaned",
    ):
        (vault / relative).mkdir(parents=True, exist_ok=True)
    return vault


def _pin_mtime(path: Path) -> None:
    """Pin *path*'s mtime so the ingestor's derived timestamp is deterministic."""
    epoch = _PINNED_MTIME.timestamp()
    os.utime(path, (epoch, epoch))


def _seed_fragment(vault: Path, *, fragment_id: str, title: str, source: str) -> Path:
    """Write one live fragment claiming *source* as its ``original_file``.

    Two fragments naming one source is the shape ``pin_source_ids`` refuses to
    pin — the already-duplicated vault the advisory has to keep talking about.
    """
    fragment = Fragment(
        id=fragment_id,
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            original_file=source,
        ),
    )
    return VaultWriter(vault_path=vault).write_fragment(fragment, body="Body text.\n")


def _seed_partially_pinnable_vault(tmp_path: Path) -> Path:
    """Build a vault with one cleanly pinnable source and one contested one."""
    vault = _make_vault(tmp_path)
    for name in ("clean.md", "dup.md"):
        source = vault / "00-Inbox" / name
        source.write_text(_ENTRY, encoding="utf-8")
        _pin_mtime(source)
    _seed_fragment(
        vault, fragment_id="frag-clean00001", title="Clean", source=_CLEAN_KEY
    )
    _seed_fragment(
        vault, fragment_id="frag-dupfirst01", title="Dup First", source=_CONFLICTED_KEY
    )
    _seed_fragment(
        vault, fragment_id="frag-dupsecond1", title="Dup Second", source=_CONFLICTED_KEY
    )
    return vault


def _ingest_inbox(vault: Path) -> IngestRunResult:
    """Run the markdown ingestor over the vault's ``00-Inbox`` directory."""
    return run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=vault / "00-Inbox",
        vault_path=vault,
    )


def _state_text(vault: Path) -> str:
    """Concatenate every ingest state file except the ledgers themselves.

    The advisory has to be backed by something durable; *which* file is the
    implementation's choice, so this reads the whole state directory rather
    than pinning a filename the fix is free to pick.
    """
    directory = ledger_dir(vault)
    if not directory.is_dir():
        return ""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.suffix != ".jsonl"
    )


def test_pin_source_ids_persists_the_keys_it_refused_to_pin(tmp_path: Path) -> None:
    """A refused key must outlive the process that refused it.

    RED at HEAD: ``PinResult.conflicts`` is returned in memory and dropped.
    Once the ledger is non-empty nothing in the vault records that a source
    was skipped, which is why the advisory can never recover the fact.
    """
    vault = _seed_partially_pinnable_vault(tmp_path)

    result = pin_source_ids(vault)

    assert result.pinned == 1, f"the clean source was not pinned: {result}"
    assert len(result.conflicts) == 1, f"expected one contested key: {result.conflicts}"
    assert _CONFLICTED_KEY in result.conflicts[0]
    assert len(SourceLedger.load(vault, source="markdown")) == 1
    assert _CONFLICTED_KEY in _state_text(vault), (
        "the contested key was not persisted anywhere under "
        f"{ledger_dir(vault)}, so the next process cannot know it exists."
    )


def test_the_advisory_fires_on_a_partially_pinned_vault(tmp_path: Path) -> None:
    """A non-empty ledger must not silence a vault that is still half-migrated.

    RED at HEAD: ``unpinned_vault_warning`` returns ``None`` the moment the
    ledger holds one record, so this run says nothing at all about the two
    fragments still sharing an unpinned key.
    """
    vault = _seed_partially_pinnable_vault(tmp_path)
    pin_source_ids(vault)

    result = _ingest_inbox(vault)
    operator = "\n".join(result.warnings)

    assert _CONFLICTED_KEY in operator, (
        "the partially-pinned vault produced no advisory naming the source "
        f"that is still unpinned. Warnings were: {result.warnings}"
    )


def test_the_travelling_rendering_names_no_vault_path_or_fragment_id(
    tmp_path: Path,
) -> None:
    """The ceiling-safe twin carries the finding without the vault content.

    RED at HEAD for the same reason as above — there is no advisory to have a
    twin of. After the fix the twin must still *say something*, so the count
    is required: an advisory that withholds the number as well as the paths
    reports no problem at all.
    """
    vault = _seed_partially_pinnable_vault(tmp_path)
    pin_source_ids(vault)

    result = _ingest_inbox(vault)
    travelling = "\n".join(result.ceiling_safe_warnings)

    assert "unpinned" in travelling.lower() or "pin" in travelling.lower(), (
        f"nothing about the unpinned sources travels to a remote caller: "
        f"{result.ceiling_safe_warnings}"
    )
    assert "1" in travelling, "the ceiling-safe rendering withheld the count too."
    assert _CONFLICTED_KEY not in travelling, (
        f"a vault path crossed the tier ceiling: {travelling!r}"
    )
    assert "dup.md" not in travelling
    assert _FRAGMENT_ID_TOKEN.search(travelling) is None, (
        f"a fragment id crossed the tier ceiling: {travelling!r}"
    )


def test_resolving_the_duplicate_and_re_pinning_restores_silence(
    tmp_path: Path,
) -> None:
    """The advisory clears itself, and clears the state that produced it.

    The positive control comes first: the advisory must be firing *before* the
    duplicate is resolved, or the silence afterwards proves nothing. That
    control is what makes this test red at HEAD.
    """
    vault = _seed_partially_pinnable_vault(tmp_path)
    pin_source_ids(vault)
    before = "\n".join(_ingest_inbox(vault).warnings)
    assert _CONFLICTED_KEY in before, (
        "positive control failed: the advisory was already silent, so the "
        "silence asserted below would be meaningless."
    )

    duplicate = next(
        path
        for path in (vault / "01-Fragments").rglob("*.md")
        if "frag-dupsecond1" in path.read_text(encoding="utf-8")
    )
    duplicate.unlink()
    repin = pin_source_ids(vault)
    assert repin.conflicts == []

    after = _ingest_inbox(vault)

    assert _CONFLICTED_KEY not in "\n".join(after.warnings), (
        "the advisory kept naming a source the operator has already resolved; "
        "one that cannot go quiet is one operators learn to ignore."
    )
    assert _CONFLICTED_KEY not in _state_text(vault), (
        "the persisted conflict record outlived its conflict."
    )
