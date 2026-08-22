"""``creek.ingest`` over MCP runs the real pipeline (#1467).

RED-FIRST. ``creek_mcp/tools/ingest.py`` re-implements the write loop inline
with a bare ``writer.write_fragment``: no ledger, no idempotency, no
``source.origin_key``, no advisories. Three consequences, all live today:

1. A repeat ingest of an *edited* in-vault ``.md`` mints a new id and orphans
   its predecessor — #953's defect, on the MCP surface, recorded nowhere.
2. Fragments written here never receive ``source.origin_key``, so the RTBF
   purge sweep — which resolves its targets from exactly that field — cannot
   see them. That is #1363's gap, on a second surface.
3. Operator advisories are not dropped here, they are never computed; the
   tool is structurally *mute*. A blanket "no MCP surface drops an advisory"
   claim is not satisfied by leaving it alone.

The module docstring's stated reason for not converting is **stale**: it says
``run_ingest`` with a ``ledger_source`` plus a directory ``input_path`` arms
``tomb_missing_units`` for that ledger. #1329 split those — the tomb gate is on
the ingestor's registry key, not on holding a ledger — and the tool needs no
``ledger_source`` at all. The residual risk is narrower and real:
``source_type="markdown"`` plus a directory input *does* arm tombing, which is
vault-mutating authority this tool has never held. That is closed by a
caller-declared ``may_tomb=False``, a restrictive conjunct that can only ever
subtract authority, and asserted below.

**ACCEPTANCE-CRITERION SUBSTITUTION.** #1467's AC3 asks for a test at
``ceiling=open`` proving no fragment id, title or body excerpt reaches the
payload. That is unsatisfiable as written: ``DEFAULT_INGEST_TIER`` is
``personal`` and ``write_tier_allowed`` refuses at ``open`` before any source
is read, so there is no payload to inspect. The intent is satisfied at
``ceiling=personal``, the lowest ceiling that admits the call — and the
refusal-before-any-side-effect property at ``open`` is kept as its own
assertion so nothing is lost by the substitution.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.ingest.ledger import SourceLedger
from creek.ingest.pin_ids import pin_source_ids
from creek.ingest.pipeline import partially_pinned_warning
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.writer import VaultWriter
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.ingest import ingest_tool

if TYPE_CHECKING:
    from pathlib import Path

_SECRET_TITLE = "SECRET-TITLE-DONOTLEAK"
_SECRET_BODY = "SECRET-BODY-EXCERPT-DONOTLEAK"
_SECRET_SOURCE_NAME = "SECRET-SOURCE-DONOTLEAK.md"
_SECRET_SOURCE_KEY = f"00-Inbox/{_SECRET_SOURCE_NAME}"
_VANISHED_KEY = "00-Inbox/vanished.md"
_VANISHED_ID = "frag-vanished001"

_FRAGMENT_ID_TOKEN = re.compile(r"frag-[0-9a-f]{6,}")

_PINNED_MTIME = datetime(2024, 1, 5, 8, 30, tzinfo=UTC)
_LATER_MTIME = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)

_ORIGINAL = "---\ndate: 2024-01-05\n---\n\n# Morning\n\nOriginal body.\n"
_EDITED = "---\ndate: 2024-01-05\n---\n\n# Morning\n\nEdited, MARKER-EDIT.\n"


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree the writer, ledger and audit log need."""
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


def _pin_mtime(path: Path, moment: datetime = _PINNED_MTIME) -> None:
    """Pin *path*'s mtime so the ingestor's derived timestamp is deterministic."""
    epoch = moment.timestamp()
    os.utime(path, (epoch, epoch))


def _live_fragments(vault: Path) -> list[Path]:
    """Return every live fragment file under ``01-Fragments``."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _orphans(vault: Path) -> list[Path]:
    """Return every soft-tombed fragment file under ``10-Liminal/Orphaned``."""
    return sorted((vault / "10-Liminal" / "Orphaned").rglob("*.md"))


def _origin_keys(vault: Path) -> list[str | None]:
    """Read ``source.origin_key`` off every live fragment's frontmatter."""
    keys: list[str | None] = []
    for path in _live_fragments(vault):
        source = frontmatter.load(path).get("source")
        keys.append(source.get("origin_key") if isinstance(source, dict) else None)
    return keys


def _write_entry(vault: Path, name: str, text: str) -> Path:
    """Write an in-vault markdown source under ``00-Inbox`` with a pinned mtime."""
    path = vault / "00-Inbox" / name
    path.write_text(text, encoding="utf-8")
    _pin_mtime(path)
    return path


def _ingest(vault: Path, target: str = "00-Inbox") -> dict[str, object]:
    """Call the MCP ingest tool at the lowest ceiling that admits it."""
    return ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=target,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )


def test_a_repeat_ingest_of_an_edited_source_updates_in_place(
    tmp_path: Path,
) -> None:
    """The MCP surface is idempotent, like every other ingest path.

    RED at HEAD: the inline loop calls ``writer.write_fragment`` with a freshly
    derived id, so the edited entry lands as a second fragment and the first is
    orphaned with no ledger record pointing at either.
    """
    vault = _make_vault(tmp_path)
    entry = _write_entry(vault, "2024-01-05.md", _ORIGINAL)

    first = _ingest(vault)
    assert first["status"] == "ok", first
    assert len(_live_fragments(vault)) == 1

    entry.write_text(_EDITED, encoding="utf-8")
    _pin_mtime(entry, _LATER_MTIME)
    second = _ingest(vault)

    assert second["status"] == "ok", second
    assert second["affected_fragment_ids"] == first["affected_fragment_ids"], (
        f"the edit minted a new id: {second['affected_fragment_ids']} != "
        f"{first['affected_fragment_ids']}"
    )
    live = _live_fragments(vault)
    assert len(live) == 1, f"the edit duplicated the fragment: {live}"
    assert "MARKER-EDIT" in live[0].read_text(encoding="utf-8")


def test_an_mcp_ingested_fragment_is_visible_to_the_purge_sweep(
    tmp_path: Path,
) -> None:
    """Ledger backing is what makes MCP-written content erasable.

    RED at HEAD: ``source.origin_key`` is ``None`` on every fragment this tool
    writes, and the RTBF purge sweep skips fragments lacking that field — so
    content ingested over MCP is not reachable by an erasure request.
    """
    vault = _make_vault(tmp_path)
    _write_entry(vault, "2024-01-05.md", _ORIGINAL)

    response = _ingest(vault)

    assert response["status"] == "ok", response
    keys = _origin_keys(vault)
    assert None not in keys, (
        f"an MCP-ingested fragment carries no source.origin_key: {keys}"
    )
    assert len(SourceLedger.load(vault, source="markdown")) == 1


def test_a_directory_ingest_cannot_soft_tomb_a_previously_ledgered_unit(
    tmp_path: Path,
) -> None:
    """Ledger backing must not hand this tool deletion authority.

    A markdown directory ingest arms ``tomb_missing_units``. This surface has
    never held vault-mutating deletion authority and must not acquire it as a
    side effect of gaining a ledger, so the conversion passes
    ``may_tomb=False``.

    RED at HEAD on the ledger half (nothing is recorded). The tombing half is
    the assertion the conversion must not break; flipping ``may_tomb`` to
    ``True`` in the implementation turns it red.
    """
    vault = _make_vault(tmp_path)
    VaultWriter(vault_path=vault).write_fragment(
        Fragment(
            id=_VANISHED_ID,
            title="Vanished",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL, original_file=_VANISHED_KEY
            ),
        ),
        body="Body of a unit whose source is not in this pass.\n",
    )
    SourceLedger.load(vault, source="markdown").record(
        _VANISHED_KEY, _VANISHED_ID, "0" * 64
    )
    _write_entry(vault, "2024-01-05.md", _ORIGINAL)

    response = _ingest(vault)

    assert response["status"] == "ok", response
    assert _orphans(vault) == [], (
        f"the MCP ingest soft-tombed a fragment: {_orphans(vault)}. This "
        "surface has never held deletion authority."
    )
    assert any(
        _VANISHED_ID in path.read_text(encoding="utf-8")
        for path in _live_fragments(vault)
    ), "the previously-ledgered fragment is no longer live under 01-Fragments."
    assert len(SourceLedger.load(vault, source="markdown")) == 2, (
        "the run recorded nothing, so the tombing assertion above passes "
        "vacuously: an unledgered pass could never have tombed anything."
    )


def test_the_response_carries_advisories_with_no_vault_content(
    tmp_path: Path,
) -> None:
    """Advisories reach the caller through the ceiling-safe channel only.

    **The vault must produce an advisory whose two renderings differ**, or this
    test cannot see which channel the response used. The un-pinned-vault
    advisory alone is not enough: its text is a fixed constant with no
    interpolation, so its operator and ceiling-safe forms are byte-identical
    and swapping ``result.ceiling_safe_warnings`` for ``result.warnings``
    survives. That survivor is what this seeding exists to kill.

    So the vault is seeded *both* un-pinned and **partially pinned** (#1367):
    two live fragments claim one source, ``pin_source_ids`` refuses to pin it
    and records the refusal, and the resulting advisory interpolates that
    source path — a real piece of the operator's vault layout, and one this
    test names distinctively so a leak is detectable rather than argued about.
    The positive control below asserts the operator rendering really does
    carry it, so the absence asserted in the payload means something.

    RED at HEAD: the response has no ``warnings`` key at all, because the
    inline loop never calls ``run_ingest`` and therefore never computes an
    advisory. Asserted at ``ceiling=personal``; see the module docstring for
    why ``open`` cannot carry this test.
    """
    vault = _make_vault(tmp_path)
    writer = VaultWriter(vault_path=vault)
    _write_entry(vault, _SECRET_SOURCE_NAME, _ORIGINAL)
    for index, title in enumerate((_SECRET_TITLE, f"{_SECRET_TITLE}-2")):
        writer.write_fragment(
            Fragment(
                id=f"frag-preexist00{index}",
                title=title,
                source=FragmentSource(
                    platform=SourcePlatform.JOURNAL,
                    original_file=_SECRET_SOURCE_KEY,
                ),
            ),
            body=f"{_SECRET_BODY} and more prose.\n",
        )
    refused = pin_source_ids(vault)
    assert len(refused.conflicts) == 1, (
        f"setup failed: the contested source was not refused: {refused}"
    )
    _write_entry(vault, "2024-01-05.md", _ORIGINAL)

    response = _ingest(vault)

    assert response["status"] == "ok", response
    warnings = response["warnings"]
    assert isinstance(warnings, list)
    assert warnings, (
        "an un-pinned, partially-pinned vault produced no advisory on the MCP "
        "surface; the tool is mute rather than merely lossy."
    )
    control = partially_pinned_warning("markdown", vault)
    assert control is not None, "positive control failed: no advisory to compare"
    assert _SECRET_SOURCE_KEY in control.message, (
        "positive control failed: the operator rendering does not name the "
        "contested source, so its absence from the payload proves nothing."
    )
    payload = json.dumps(response)
    assert _SECRET_SOURCE_KEY not in payload, (
        "a vault source path reached the payload, so the response carried the "
        "operator rendering rather than the ceiling-safe one."
    )
    assert _SECRET_TITLE not in payload, "a vault fragment title reached the payload."
    assert _SECRET_BODY not in payload, "a vault body excerpt reached the payload."
    for warning in warnings:
        assert _FRAGMENT_ID_TOKEN.search(str(warning)) is None, (
            f"a fragment id crossed the tier ceiling in an advisory: {warning!r}"
        )


def test_an_open_ceiling_is_still_refused_before_anything_is_read(
    tmp_path: Path,
) -> None:
    """The substituted acceptance criterion's other half, kept explicit.

    ``ceiling=open`` must refuse before any source data is read and before any
    fragment is written — which is precisely why AC3 cannot be asserted there.
    Green at HEAD and it must stay green: the conversion adds a ``run_ingest``
    call, and the refusal has to keep happening in front of it.
    """
    vault = _make_vault(tmp_path)
    _write_entry(vault, "2024-01-05.md", _ORIGINAL)

    response = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path="00-Inbox",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert response["status"] == "refused", response
    assert _live_fragments(vault) == []
