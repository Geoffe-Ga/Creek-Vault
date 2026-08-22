"""``creek.journal`` MCP tool — Adepthood journal entry → fragment (#754).

Drives the REAL ledger-backed ingest (`run_ingest`) against a temp vault, so the
idempotency and edit-in-place guarantees are exercised end-to-end, not mocked:

- re-sending the same external id is a no-op (one fragment, same id);
- editing an entry (same external id, new content) rewrites in place (same id,
  no orphaned duplicate);
- tier is honored (an INTIMATE entry lands intimate; it is refused under a lower
  ceiling, never downgraded);
- the update-in-place path honours the tier of the fragment it would *overwrite*,
  not just the tier of the incoming entry — you may only overwrite what you could
  have read (#970).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.models import PrivacyTier
from creek.purge import PurgeEngine
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.tier_ceiling import TierCeiling, tier_allowed
from creek_mcp.tools.journal import TOOL_NAME, journal_ingest_tool

if TYPE_CHECKING:
    from pathlib import Path

_TS = "2026-06-20T10:00:00+00:00"


def _vault(tmp_path: Path) -> Path:
    """Create the minimum vault layout the writer + ledger + audit need."""
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _fragments(vault: Path) -> list[Path]:
    """Return all fragment files under 01-Fragments."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _load(path: Path) -> frontmatter.Post:
    """Load a fragment file's frontmatter + body."""
    return frontmatter.load(path)


def _audit(vault: Path) -> list[dict[str, object]]:
    """Return parsed MCP audit-log entries."""
    log = vault / MCP_AUDIT_RELPATH
    assert log.exists()
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# The #970 overwrite-gate fixtures. Two sentinels, deliberately disjoint from
# every tier word, so an assertion about "intimate" and an assertion about the
# protected plaintext can never accidentally satisfy each other.
_SECRET = "synthetic-tender-secret-970"
_BENIGN = "benign-open-replacement-970"


def _staged(vault: Path) -> list[Path]:
    """Return all staged entries under ``00-Creek-Meta/adepthood/journal``."""
    return sorted((vault / "00-Creek-Meta" / "adepthood" / "journal").rglob("*.md"))


def _leaking_files(vault: Path, needle: str) -> list[Path]:
    """Return every markdown file anywhere under *vault* containing *needle*."""
    return [
        path
        for path in sorted(vault.rglob("*.md"))
        if needle in path.read_text(encoding="utf-8")
    ]


def _seed_intimate(vault: Path, external_id: str) -> dict[str, object]:
    """Create one INTIMATE entry carrying ``_SECRET`` under the broadest ceiling.

    Args:
        vault: Vault root.
        external_id: The idempotency key the later overwrite attempt reuses.

    Returns:
        The tool's success response, asserted to be a fresh creation so a
        later ``refused`` cannot be mistaken for "there was nothing there".
    """
    result = journal_ingest_tool(
        vault_path=vault,
        content=f"a tender confession {_SECRET}",
        external_id=external_id,
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "ok"
    assert result["action"] == "created"
    return result


def _overwrite_at_open(vault: Path, external_id: str) -> dict[str, object]:
    """Re-send *external_id* as benign OPEN-tier text under an OPEN ceiling.

    The incoming tier is ``open``, so the pre-existing ``write_tier_allowed``
    gate admits it — this call is only ever refused by a gate that consults
    the tier of the fragment being *overwritten*.

    Args:
        vault: Vault root.
        external_id: The idempotency key of the entry to overwrite.

    Returns:
        The tool's raw response dict.
    """
    return journal_ingest_tool(
        vault_path=vault,
        content=f"{_BENIGN} nothing to see here",
        external_id=external_id,
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="adepthood",
    )


def test_entry_becomes_a_journal_fragment(tmp_path: Path) -> None:
    """An entry is ingested as one JOURNAL-platform fragment carrying its tier."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="Today I rested and the work survived.",
        external_id="adep-001",
        timestamp=_TS,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "ok"
    assert result["tool"] == TOOL_NAME
    assert result["action"] == "created"
    assert result["fragment_id"]
    frags = _fragments(vault)
    assert len(frags) == 1
    post = _load(frags[0])
    assert post.metadata["privacy_tier"] == "personal"
    assert str(post.metadata["source"]["platform"]) == "journal"


def test_resending_the_same_entry_is_idempotent(tmp_path: Path) -> None:
    """Same external id + same content → one fragment, same id, no duplicate."""
    vault = _vault(tmp_path)

    def _send() -> dict[str, object]:
        return journal_ingest_tool(
            vault_path=vault,
            content="A steady entry.",
            external_id="adep-002",
            tier="open",
            timestamp=_TS,
            privacy_tier_ceiling=TierCeiling.PERSONAL,
        )

    first = _send()
    second = _send()
    assert len(_fragments(vault)) == 1  # no duplicate
    assert second["fragment_id"] == first["fragment_id"]
    assert second["action"] == "unchanged"


def test_editing_an_entry_updates_in_place(tmp_path: Path) -> None:
    """Same external id + new content → same fragment id, body updated, no orphan."""
    vault = _vault(tmp_path)
    first = journal_ingest_tool(
        vault_path=vault,
        content="The original wording.",
        external_id="adep-003",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    edited = journal_ingest_tool(
        vault_path=vault,
        content="The revised, longer wording of the same entry.",
        external_id="adep-003",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert len(_fragments(vault)) == 1  # updated in place, not orphaned
    assert edited["fragment_id"] == first["fragment_id"]
    assert edited["action"] == "updated"
    assert "revised" in _load(_fragments(vault)[0]).content


def test_intimate_entry_is_refused_under_a_lower_ceiling(tmp_path: Path) -> None:
    """An INTIMATE entry is refused (not downgraded) under an OPEN ceiling."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="something tender",
        external_id="adep-004",
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert _fragments(vault) == []  # nothing written


def test_intimate_entry_is_stored_intimate_under_an_intimate_ceiling(
    tmp_path: Path,
) -> None:
    """An INTIMATE entry admitted under an intimate ceiling lands intimate."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="a tender private entry",
        external_id="adep-005",
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert result["status"] == "ok"
    assert result["tier"] == "intimate"
    assert _load(_fragments(vault)[0]).metadata["privacy_tier"] == "intimate"


def test_empty_content_is_refused(tmp_path: Path) -> None:
    """A blank entry is a structured refusal, not a crash or an empty fragment.

    The reason is asserted, not just the status, and that is the half this
    test was missing (#1494). It used to omit ``tier`` as well, so once an
    omitted tier became its own refusal this test would have gone on passing
    while proving nothing about blank content at all — a green assertion
    satisfied by the wrong gate. Naming the reason pins which gate fired, and
    passing ``tier`` explicitly keeps the blank-content gate the only one this
    call can trip.
    """
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="   ",
        external_id="adep-006",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "content and external_id are required" in str(result["reason"])


def test_unknown_tier_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """An unrecognised ``tier`` refuses with its own reason, staging nothing.

    The malformed-call gate sits above both admission gates, so the refusal
    must name the malformation — a caller whose argument is simply wrong needs
    to be told which way, and nothing here is derived from vault content — and
    it must leave no fragment, no staged entry and no audit entry behind: at
    this point there is no meaningful tier to record.
    """
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="an entry",
        external_id="adep-bad-tier",
        timestamp=_TS,
        tier="not-a-tier",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "unknown tier" in str(result["reason"])
    assert _fragments(vault) == []
    assert _staged(vault) == []
    assert not (vault / MCP_AUDIT_RELPATH).exists()


def test_omitted_tier_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """An OMITTED ``tier`` is refused too — never filled in as ``open`` (#1494).

    ``tier=`` is left out of the call entirely rather than passed as ``None``,
    and that is the whole point: omission is the caller's actual mistake, and
    it is what makes this one test kill two separate mutations — restoring the
    ``tier: str = "open"`` default, and deleting the ``tier is None`` guard.
    An explicit ``tier=None`` would kill only the second.

    The reason substring is required, not stylistic. ``PrivacyTier(None)``
    raises ``ValueError``, so with the guard deleted this call still returns
    ``status: refused`` — carrying ``unknown tier None`` — and a status-only
    assertion could not tell the guard's presence from its absence.

    Everything else mirrors the unknown-tier sibling above: a malformed call is
    caught above both admission gates, so it leaves no fragment, no staged
    entry and no audit entry — at this point there is no meaningful tier to
    record.
    """
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="an entry whose tier the caller never named",
        external_id="adep-no-tier",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert result["tool"] == TOOL_NAME
    assert result["tier_ceiling"] == "personal"
    assert "tier is required" in str(result["reason"])
    assert _fragments(vault) == []
    assert _staged(vault) == []
    assert not (vault / MCP_AUDIT_RELPATH).exists()


def test_journal_success_audit_records_tier_and_fragment(tmp_path: Path) -> None:
    """The success audit records the tier and the fragment it wrote (#754 review)."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="an entry",
        external_id="adep-007",
        timestamp=_TS,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="adepthood",
    )
    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["consumer"] == "adepthood"
    assert last["args_summary"]["tier"] == "personal"
    assert last["created_tier"] == "personal"
    assert last["affected_fragment_ids"] == [result["fragment_id"]]


def test_refused_intimate_attempt_is_audited_with_tier(tmp_path: Path) -> None:
    """A refused INTIMATE attempt is audited (with its tier) so it is investigable."""
    vault = _vault(tmp_path)
    journal_ingest_tool(
        vault_path=vault,
        content="tender",
        external_id="adep-008",
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="adepthood",
    )
    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["args_summary"]["tier"] == "intimate"  # the attempted tier is recorded


def test_slug_colliding_external_ids_stay_distinct(tmp_path: Path) -> None:
    """Distinct external ids that slugify identically stay distinct (#754 review).

    ``"a/b"`` and ``"a-b"`` both slug to ``a-b``; the stable stem's id hash keeps
    them apart, so the idempotency key never collides.
    """
    vault = _vault(tmp_path)
    first = journal_ingest_tool(
        vault_path=vault,
        content="entry A",
        external_id="a/b",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    second = journal_ingest_tool(
        vault_path=vault,
        content="entry B",
        external_id="a-b",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert first["fragment_id"] != second["fragment_id"]
    assert len(_fragments(vault)) == 2


def test_ingest_failure_is_refused_and_audited(tmp_path: Path) -> None:
    """A failure inside run_ingest refuses AND leaves an audit trace (#754 review)."""
    from creek.ingest.pipeline import IngestRunResult

    def _failing_runner(**_kwargs: object) -> IngestRunResult:
        return IngestRunResult(
            written=0,
            errors=["boom"],
            discovered=1,
            created=0,
            updated=0,
            unchanged=0,
            tombed=0,
            skipped=0,
        )

    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="an entry",
        external_id="adep-009",
        timestamp=_TS,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="adepthood",
        run=_failing_runner,
    )
    assert result["status"] == "refused"
    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["args_summary"]["tier"] == "personal"
    assert last["created_tier"] == "personal"


def test_resend_after_purge_restages_cleanly(tmp_path: Path) -> None:
    """RTBF then re-send: purge removes the staged body; re-ingest restages (#845).

    The staged plaintext under ``00-Creek-Meta/adepthood/journal/`` must
    be gone right after the purge (the RTBF core), and re-sending the
    SAME external id + content afterwards must land the fragment AND the
    staged file again at their stable paths — a purge must not poison
    the idempotency key.
    """
    vault = _vault(tmp_path)
    secret = "synthetic-intimate-secret-845"

    def _send() -> dict[str, object]:
        """Ingest the same intimate entry (stable external id + content)."""
        return journal_ingest_tool(
            vault_path=vault,
            content=f"a tender entry {secret}",
            external_id="adep-845",
            timestamp=_TS,
            tier="intimate",
            privacy_tier_ceiling=TierCeiling.INTIMATE,
        )

    first = _send()
    assert first["status"] == "ok"
    staged_dir = vault / "00-Creek-Meta" / "adepthood" / "journal"
    staged = sorted(staged_dir.glob("*.md"))
    assert len(staged) == 1
    assert secret in staged[0].read_text(encoding="utf-8")

    PurgeEngine(vault).purge_fragment(str(first["fragment_id"]))

    # The RTBF core (#845): the staged plaintext body is gone right
    # after the purge, before any re-send.
    assert not staged[0].exists()
    assert _fragments(vault) == []

    resent = _send()
    assert resent["status"] == "ok"
    assert len(_fragments(vault)) == 1  # the fragment exists again
    assert staged[0].exists()  # restaged at the same stable path


def test_staging_path_unchanged(tmp_path: Path) -> None:
    """The staging dir is pinned at ``00-Creek-Meta/adepthood/journal/`` (#845).

    Regression pin: the purge engine's staging-dir containment guard
    and the source ledger both key on this path — relocating the
    constant without a ledger migration would orphan every existing
    staged entry. May already pass; that is the point.
    """
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="a steady entry",
        external_id="adep-846",
        tier="open",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "ok"
    staged = sorted((vault / "00-Creek-Meta" / "adepthood" / "journal").glob("*.md"))
    assert len(staged) == 1
    post = _load(_fragments(vault)[0])
    source = post.metadata["source"]
    assert isinstance(source, dict)
    # The ledger key recorded on the fragment names the same staging path.
    assert str(source["origin_key"]) == (
        f"00-Creek-Meta/adepthood/journal/{staged[0].name}"
    )


def test_update_of_an_above_ceiling_fragment_is_refused(tmp_path: Path) -> None:
    """An OPEN-ceiling caller may not overwrite an INTIMATE fragment (#970).

    The issue's verbatim reproduction. ``external_id`` is the idempotency key,
    so the second call is an update-in-place: it destroys the intimate body it
    maps to while satisfying ``write_tier_allowed`` on its own ``open`` tier.
    The rule being pinned is *you may only overwrite what you could have
    read*, so the refusal is the canonical read-gate refusal.

    The refusal must also name no tier. It carries
    :data:`~creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON` verbatim and
    nothing derived from the protected fragment — no ``fragment_id``, no tier
    echoed anywhere in the payload — because a refusal that ranks the content
    it withheld is a tier-classification oracle over the corpus (#969, rule 4).
    """
    vault = _vault(tmp_path)
    _seed_intimate(vault, "e1")

    result = _overwrite_at_open(vault, "e1")

    assert result["status"] == "refused"
    assert result["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert "fragment_id" not in result
    assert "intimate" not in json.dumps(result)


def test_refused_update_leaves_the_fragment_bytes_untouched(tmp_path: Path) -> None:
    """The refused overwrite changes not one byte of the protected fragment.

    Byte-level rather than response-level on purpose. A JSON-response
    assertion is structurally incapable of catching a write-side leak: on
    #968 a response-level guardrail passed 5/5 while six generators went on
    leaking to disk. The evidence for #970 is the bytes, so this asserts
    ``read_bytes()`` equality across the refused call, that the intimate
    plaintext and its tier stamp both survive, and that the refused caller's
    own text landed in **no** markdown file anywhere under the vault.
    """
    vault = _vault(tmp_path)
    _seed_intimate(vault, "e2")
    fragment = _fragments(vault)[0]
    before = fragment.read_bytes()

    result = _overwrite_at_open(vault, "e2")

    # The disk evidence is asserted first, deliberately: it is what the
    # response-shaped assertion below cannot see.
    assert fragment.read_bytes() == before
    post = _load(fragment)
    assert _SECRET in post.content
    assert post.metadata["privacy_tier"] == "intimate"
    assert _leaking_files(vault, _BENIGN) == []
    assert result["status"] == "refused"


def test_refused_update_leaves_the_staged_entry_untouched(tmp_path: Path) -> None:
    """The refused overwrite changes not one byte of the STAGED entry either.

    **This test must fail if the gate is added without moving ``_stage_entry``
    below it.** ``_stage_entry`` currently runs before anything else, so the
    staged copy under ``00-Creek-Meta/adepthood/journal/`` is rewritten before
    a refusal could be returned — and it is the worse loss of the two: the
    fragment keeps its tier stamp through the escalate-only privacy merge,
    while the staged file has no ratchet at all, so its ``privacy_tier`` is
    rewritten *downward* and its intimate body replaced outright.
    """
    vault = _vault(tmp_path)
    _seed_intimate(vault, "e3")
    staged = _staged(vault)[0]
    before = staged.read_bytes()

    result = _overwrite_at_open(vault, "e3")

    # Staging order is the thing under test, so the staged bytes are asserted
    # ahead of the response: a gate bolted on above ``_stage_entry`` returns a
    # correct refusal over an already-destroyed staged entry.
    assert staged.read_bytes() == before
    staged_post = _load(staged)
    assert _SECRET in staged_post.content
    assert staged_post.metadata["privacy_tier"] == "intimate"
    assert result["status"] == "refused"


def test_refused_update_is_audited_without_naming_the_fragment(tmp_path: Path) -> None:
    """The refused overwrite IS audited, and the entry names no protected thing.

    An attempted ceiling violation is an operator-relevant signal, so it gets
    a trail — the tool, the ceiling and the caller's *own* arguments. What it
    must not get is anything resolved from the fragment it was refused:
    ``created_tier`` and ``affected_fragment_ids`` stay absent-or-empty, and
    neither the protected fragment's id nor its tier appears anywhere in the
    serialised entry. The audit artifact is served onward through other
    surfaces, so read_gate's rule 1 — never the probed target id, never the
    outcome — applies to these bytes exactly as it does to the response.
    """
    vault = _vault(tmp_path)
    seeded = _seed_intimate(vault, "e4")

    assert _overwrite_at_open(vault, "e4")["status"] == "refused"

    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["tier_ceiling"] == "open"
    assert last["consumer"] == "adepthood"
    assert last["args_summary"] == {"external_id": "e4", "tier": "open"}
    assert not last.get("created_tier")
    assert not last.get("affected_fragment_ids")
    serialised = json.dumps(last)
    assert str(seeded["fragment_id"]) not in serialised
    assert "intimate" not in serialised


def test_creating_a_new_entry_at_a_low_ceiling_still_works(tmp_path: Path) -> None:
    """A FRESH external id at ceiling=open still creates (#970 non-regression).

    The gate is about *overwriting* a fragment the caller could not read. An
    id that maps to nothing has nothing to protect, so it must not fail
    closed — that would make the ordinary Adepthood write path unusable at
    every ceiling below the tier of whatever the vault happens to hold.
    """
    vault = _vault(tmp_path)

    result = journal_ingest_tool(
        vault_path=vault,
        content="a plainly public note",
        external_id="e5-fresh",
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "ok"
    assert result["action"] == "created"
    assert len(_fragments(vault)) == 1


def test_within_ceiling_update_still_works(tmp_path: Path) -> None:
    """Editing an OPEN fragment at ceiling=open still updates in place (#970).

    The overwrite gate admits what the caller could have read, so the ordinary
    edit-in-place loop at a matching ceiling is untouched: same id, new body,
    no orphaned duplicate.
    """
    vault = _vault(tmp_path)
    created = journal_ingest_tool(
        vault_path=vault,
        content="the original public wording",
        external_id="e6",
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert created["action"] == "created"

    edited = journal_ingest_tool(
        vault_path=vault,
        content="the revised, longer public wording of the same entry",
        external_id="e6",
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert edited["status"] == "ok"
    assert edited["action"] == "updated"
    assert edited["fragment_id"] == created["fragment_id"]
    assert len(_fragments(vault)) == 1
    assert "revised" in _load(_fragments(vault)[0]).content


@pytest.mark.parametrize("ceiling", [TierCeiling.INTIMATE, TierCeiling.ALL])
def test_intimate_entry_stays_updatable_at_an_admitted_ceiling(
    tmp_path: Path,
    ceiling: TierCeiling,
) -> None:
    """An INTIMATE entry is still editable by a caller admitted to read it.

    Recoverability, proved rather than reasoned about: a prior lane nearly
    shipped a "fail closed to the most restrictive tier" fix that made
    entries permanently un-updatable, which is a one-way ratchet into
    permanent burial. So the path back is exercised at both admitting
    ceilings — the update succeeds, the new body is on disk, and the
    fragment's ``privacy_tier`` is **still intimate** (the escalate-only
    merge must not be traded away for the new gate). The final leg re-pins
    that widening the gate for an admitted caller did not reopen it for an
    unadmitted one.

    Args:
        tmp_path: pytest temp dir.
        ceiling: An admitting ceiling for intimate content.
    """
    vault = _vault(tmp_path)
    _seed_intimate(vault, "e7")

    edited = journal_ingest_tool(
        vault_path=vault,
        content=f"a revised tender confession {_SECRET}",
        external_id="e7",
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=ceiling,
    )

    assert edited["status"] == "ok"
    assert edited["action"] == "updated"
    assert len(_fragments(vault)) == 1
    post = _load(_fragments(vault)[0])
    assert "revised" in post.content
    assert post.metadata["privacy_tier"] == "intimate"

    # Admitting the admitted caller must not admit the unadmitted one.
    assert _overwrite_at_open(vault, "e7")["status"] == "refused"


def test_gate_reads_the_vault_tier_not_the_staged_stamp(tmp_path: Path) -> None:
    """The gate ranks the fragment's CURRENT vault tier, not a stale stamp.

    The discriminator. An entry created at ``open`` is escalated in the vault
    the way ``creek classify`` would escalate it — the fragment's frontmatter
    only, leaving the staged copy's stamp reading ``open`` and the caller
    still self-declaring ``open``. Any implementation that consults the
    caller-supplied tier, or the staged file's stale stamp, admits this call;
    only one that reads the fragment's present tier out of the vault refuses
    it. A caller-supplied tier is not a gate.
    """
    vault = _vault(tmp_path)
    created = journal_ingest_tool(
        vault_path=vault,
        content="a note that later turns out to be tender",
        external_id="e8",
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert created["action"] == "created"

    fragment = _fragments(vault)[0]
    escalated = frontmatter.load(fragment)
    escalated.metadata["privacy_tier"] = "intimate"
    fragment.write_text(frontmatter.dumps(escalated), encoding="utf-8")
    # The staged stamp is deliberately left stale at ``open`` — that plus the
    # caller's own ``tier="open"`` is what makes this the discriminator.
    assert _load(_staged(vault)[0]).metadata["privacy_tier"] == "open"
    before = fragment.read_bytes()

    result = _overwrite_at_open(vault, "e8")

    assert fragment.read_bytes() == before
    assert _leaking_files(vault, _BENIGN) == []
    assert result["status"] == "refused"
    assert result["reason"] == GENERIC_ABOVE_CEILING_REASON


@pytest.mark.parametrize(
    ("existing_tier", "ceiling", "admitted"),
    [
        (PrivacyTier.PERSONAL, TierCeiling.OPEN, False),
        (PrivacyTier.PERSONAL, TierCeiling.PERSONAL, True),
        (PrivacyTier.UNCLASSIFIED, TierCeiling.OPEN, False),
        (PrivacyTier.UNCLASSIFIED, TierCeiling.PERSONAL, True),
        (PrivacyTier.INTIMATE, TierCeiling.PERSONAL, False),
        (PrivacyTier.OPEN, TierCeiling.OPEN, True),
    ],
)
def test_overwrite_admission_tracks_the_ranking_at_every_tier(
    tmp_path: Path,
    existing_tier: PrivacyTier,
    ceiling: TierCeiling,
    admitted: bool,
) -> None:
    """The gate admits exactly what a *read* at *ceiling* would have admitted.

    The other #970 tests drive the two extremes — an ``intimate`` fragment
    against an ``open`` ceiling, and the recovery ceilings that re-admit it.
    This one walks the middle of the ranking, because "you may only overwrite
    what you could have read" is a claim about the whole table and not just
    its corners. ``personal`` is the tier a remote consumer actually operates
    at, and ``unclassified`` is the one every freshly-ingested fragment
    carries before ``creek classify`` runs — it ranks *with* ``personal``
    rather than with ``open`` (#961), so an ``open`` ceiling must refuse it.

    The existing fragment's tier is set on disk rather than through the
    creating call's own ``tier`` argument, so the assertion is about the tier
    the gate reads out of the vault and not about what the seeding caller
    declared.

    Args:
        tmp_path: pytest temp dir.
        existing_tier: The tier the fragment carries in the vault.
        ceiling: The overwriting caller's declared ceiling.
        admitted: Whether ``tier_allowed`` admits that pairing.
    """
    vault = _vault(tmp_path)
    journal_ingest_tool(
        vault_path=vault,
        content="a note whose tier is decided on disk below",
        external_id="e10",
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    fragment = _fragments(vault)[0]
    post = frontmatter.load(fragment)
    post.metadata["privacy_tier"] = existing_tier.value
    fragment.write_text(frontmatter.dumps(post), encoding="utf-8")
    before = fragment.read_bytes()

    result = journal_ingest_tool(
        vault_path=vault,
        content=f"{_BENIGN} replacement body",
        external_id="e10",
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=ceiling,
    )

    assert tier_allowed(existing_tier, ceiling) is admitted, (
        "the expectation table has drifted from the shared ranking; fix the "
        "table, not the gate"
    )
    if admitted:
        assert result["status"] == "ok"
        assert _BENIGN in _load(fragment).content
    else:
        assert result["status"] == "refused"
        assert result["reason"] == GENERIC_ABOVE_CEILING_REASON
        assert fragment.read_bytes() == before
        assert _leaking_files(vault, _BENIGN) == []


def test_a_ledger_record_whose_fragment_is_gone_fails_closed(tmp_path: Path) -> None:
    """An external id the ledger maps to a missing fragment fails closed (#970).

    With the fragment gone there is no tier to rank, and no evidence about
    what the id used to hold — so the reduction over an empty tier set is
    ``INTIMATE`` and an OPEN-ceiling overwrite is refused. That must not be a
    permanent burial of the id: the same call under ``ceiling=all`` is
    admitted, so the recovery path is one broader call rather than a
    hand-edit of the ledger.
    """
    vault = _vault(tmp_path)
    _seed_intimate(vault, "e9")
    _fragments(vault)[0].unlink()

    refused = _overwrite_at_open(vault, "e9")
    assert refused["status"] == "refused"
    assert refused["reason"] == GENERIC_ABOVE_CEILING_REASON

    admitted = journal_ingest_tool(
        vault_path=vault,
        content=f"{_BENIGN} re-sent by an admitted caller",
        external_id="e9",
        timestamp=_TS,
        tier="open",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert admitted["status"] == "ok"
