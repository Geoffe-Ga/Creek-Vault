"""Tests for the per-upload receipts kept beside the ingest ledger (#2124)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.ingest.ledger import forget_fragment_ids, ledger_dir
from creek.ingest.upload_receipts import (
    UploadReceipt,
    read_upload_receipts,
    receipts_path,
    record_upload_receipts,
)

if TYPE_CHECKING:
    from pathlib import Path

_AT = datetime(2026, 11, 20, 17, 2, 11, tzinfo=UTC)


def _vault(tmp_path: Path) -> Path:
    """Return a vault root carrying the ``00-Creek-Meta`` marker directory."""
    (tmp_path / "00-Creek-Meta").mkdir()
    return tmp_path


def _record(vault: Path, *fragment_ids: str, external_id: str = "up-1") -> bool:
    """Record one upload's receipts with fixed identity fields."""
    return record_upload_receipts(
        vault,
        consumer="adepthood",
        external_id=external_id,
        filename="on-attention.html",
        tier="personal",
        fragment_ids=list(fragment_ids),
        at=_AT,
    )


def test_receipts_live_in_the_ingest_ledger_directory(tmp_path: Path) -> None:
    """Beside the ledger, so the ledger's erasure pass reaches them too."""
    assert receipts_path(tmp_path).parent == ledger_dir(tmp_path)


def test_one_receipt_per_fragment_reads_back(tmp_path: Path) -> None:
    """A multi-fragment upload records one line per fragment, same instant."""
    vault = _vault(tmp_path)

    assert _record(vault, "frag-a", "frag-b") is True
    assert read_upload_receipts(vault) == [
        UploadReceipt(
            consumer="adepthood",
            external_id="up-1",
            filename="on-attention.html",
            tier="personal",
            fragment_id=fragment_id,
            at=_AT,
        )
        for fragment_id in ("frag-a", "frag-b")
    ]


def test_an_upload_with_no_fragment_records_nothing(tmp_path: Path) -> None:
    """No fragment, no receipt: there is nothing a status could point at."""
    vault = _vault(tmp_path)

    assert _record(vault) is False
    assert not receipts_path(vault).exists()


def test_a_non_vault_is_never_scaffolded(tmp_path: Path) -> None:
    """Without the meta marker nothing is created."""
    assert _record(tmp_path, "frag-a") is False
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_receipt_file_is_reported_not_raised(tmp_path: Path) -> None:
    """An upload never fails because its receipt could not be written."""
    vault = _vault(tmp_path)
    receipts_path(vault).mkdir(parents=True)

    assert _record(vault, "frag-a") is False
    assert read_upload_receipts(vault) == []


@pytest.mark.parametrize(
    "line",
    [
        "{",
        '"just a string"',
        '{"fragment_id": "x"}',
        '{"consumer": "c", "external_id": "e", "filename": "f", "tier": "open", '
        '"fragment_id": "x", "at": "not a time"}',
        '{"consumer": "c", "external_id": "e", "filename": "f", "tier": "open", '
        '"fragment_id": "x", "at": "2026-11-20T17:02:11"}',
        '{"consumer": "c", "external_id": "", "filename": "f", "tier": "open", '
        '"fragment_id": "x", "at": "2026-11-20T17:02:11+00:00"}',
    ],
)
def test_a_damaged_line_is_skipped(tmp_path: Path, line: str) -> None:
    """A torn line costs that line only."""
    vault = _vault(tmp_path)
    _record(vault, "frag-a")
    with receipts_path(vault).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n\n")

    assert [r.fragment_id for r in read_upload_receipts(vault)] == ["frag-a"]


def test_the_ledger_erasure_removes_a_forgotten_fragments_receipt(
    tmp_path: Path,
) -> None:
    """Forgetting a fragment erases the receipt naming it, filename and all."""
    vault = _vault(tmp_path)
    _record(vault, "frag-a", "frag-b")

    erased = forget_fragment_ids(vault, ["frag-a"])

    assert erased == 1
    assert [r.fragment_id for r in read_upload_receipts(vault)] == ["frag-b"]
    assert "frag-a" not in receipts_path(vault).read_text(encoding="utf-8")


def test_forgetting_every_receipt_unlinks_the_file(tmp_path: Path) -> None:
    """A file left with no rows does not survive as a husk."""
    vault = _vault(tmp_path)
    _record(vault, "frag-a")

    forget_fragment_ids(vault, ["frag-a"])

    assert not receipts_path(vault).exists()
    assert read_upload_receipts(vault) == []
