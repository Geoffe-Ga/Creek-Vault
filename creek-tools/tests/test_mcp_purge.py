"""Tests for the FEAT-012 MCP purge tools.

The purge tools wrap :class:`creek.purge.engine.PurgeEngine` behind the
elevated-authorization gate (:mod:`creek_mcp.auth`). A call without the
``CREEK_MCP_ELEVATED_TOKEN`` token returns a structured refusal — never
a partial purge — and the ``creek.purge.vault`` tool additionally
requires a ``confirm_vault_path`` matching the absolute path of the
target vault, mirroring the CLI's interactive guard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.purge.audit import PurgeAuditEntry, PurgeAuditLog
from creek.purge.engine import PurgeResult
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tools.purge import (
    purge_classifications_tool,
    purge_daterange_tool,
    purge_fragment_tool,
    purge_source_tool,
    purge_vault_tool,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# 41 chars — clears the 32-char floor (#907). Low-entropy test literal,
# not a real credential.
ELEVATED_TOKEN = "test-elevated-secret-" + "a" * 20


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Seed a vault tree with a single fragment for the purge tools."""
    for relparts in (
        ("00-Creek-Meta", "audit"),
        ("00-Creek-Meta", "Processing-Log"),
        ("01-Fragments", "Notes"),
        ("02-Threads", "Active"),
        ("03-Eddies",),
    ):
        (tmp_path.joinpath(*relparts)).mkdir(parents=True, exist_ok=True)
    # GAP-003 marker so `purge_vault` recognises this as a Creek vault.
    (tmp_path / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# minimal marker for GAP-003\n",
        encoding="utf-8",
    )
    metadata = {
        "type": "fragment",
        "id": "frag-001",
        "title": "Test fragment",
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": "open",
        "eddies": [],
    }
    fragment_path = tmp_path / "01-Fragments" / "Notes" / "frag-001.md"
    fragment_path.write_text(
        frontmatter.dumps(frontmatter.Post(content="Body.", **metadata)),
        encoding="utf-8",
    )
    yield tmp_path


@pytest.fixture(autouse=True)
def configured_elevated_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a known elevated token to every purge test by default."""
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", ELEVATED_TOKEN)


def _audit_entries(vault_path: Path) -> list[dict[str, object]]:
    """Return parsed MCP audit entries for the test vault."""
    log = vault_path / MCP_AUDIT_RELPATH
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Refusal path: no token, wrong token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invoke",
    [
        lambda vault: purge_fragment_tool(
            vault_path=vault,
            fragment_id="frag-001",
            auth_token=None,
        ),
        lambda vault: purge_source_tool(
            vault_path=vault,
            source_type="journal",
            auth_token=None,
        ),
        lambda vault: purge_classifications_tool(
            vault_path=vault,
            auth_token=None,
        ),
        lambda vault: purge_daterange_tool(
            vault_path=vault,
            start="2026-05-01",
            end="2026-05-02",
            auth_token=None,
        ),
        lambda vault: purge_vault_tool(
            vault_path=vault,
            confirm_vault_path=str(vault),
            auth_token=None,
        ),
    ],
)
def test_purge_tool_refuses_without_token(
    vault: Path,
    invoke: object,
) -> None:
    """Each purge tool refuses when no elevated token is supplied.

    The fragment must remain on disk after the refusal — a partial
    purge with no audit cover would be the worst-case outcome.
    """
    result = invoke(vault)  # type: ignore[operator]
    assert result["status"] == "refused"
    assert result["reason"].startswith("elevated authorization required")
    fragment = vault / "01-Fragments" / "Notes" / "frag-001.md"
    assert fragment.exists(), "no purge tool may delete files without auth"


def test_purge_tool_refuses_with_wrong_token(vault: Path) -> None:
    """A non-matching token is rejected by the gate."""
    result = purge_fragment_tool(
        vault_path=vault,
        fragment_id="frag-001",
        auth_token="not-the-secret",
    )
    assert result["status"] == "refused"
    assert (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_refused_purge_still_writes_audit_entry(vault: Path) -> None:
    """Refusals are themselves audited — silent denials are not allowed."""
    purge_fragment_tool(
        vault_path=vault,
        fragment_id="frag-001",
        auth_token=None,
    )
    entries = _audit_entries(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "creek.purge.fragment"
    assert entry["consumer"] in {"unknown", "claude-code", "crawdad"}
    # No leak of the (absent) auth token into args_summary
    assert "auth_token" not in entry["args_summary"]


# ---------------------------------------------------------------------------
# Happy path with elevated token
# ---------------------------------------------------------------------------


def test_purge_fragment_with_token_deletes_file(vault: Path) -> None:
    """With the elevated token, ``creek.purge.fragment`` deletes the file."""
    result = purge_fragment_tool(
        vault_path=vault,
        fragment_id="frag-001",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "ok"
    assert result["tool"] == "creek.purge.fragment"
    assert result["fragments_affected"] == 1
    assert not (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_purge_fragment_dry_run_preserves_file(vault: Path) -> None:
    """``dry_run=True`` previews without deleting."""
    result = purge_fragment_tool(
        vault_path=vault,
        fragment_id="frag-001",
        auth_token=ELEVATED_TOKEN,
        dry_run=True,
    )
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_purge_source_with_token_deletes_matching_fragments(vault: Path) -> None:
    """``creek.purge.source`` removes every fragment from the platform."""
    result = purge_source_tool(
        vault_path=vault,
        source_type="journal",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "ok"
    assert result["fragments_affected"] == 1
    assert not (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_purge_classifications_with_token_resets_fields(vault: Path) -> None:
    """``creek.purge.classifications`` wipes classification fields."""
    result = purge_classifications_tool(
        vault_path=vault,
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "ok"
    assert result["classifications_reset"] == 1
    post = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-001.md"),
    )
    assert post["frequency"]["primary"] == "unclassified"  # type: ignore[index]


def test_purge_daterange_with_token_deletes_in_window(vault: Path) -> None:
    """``creek.purge.daterange`` deletes fragments created in the window."""
    result = purge_daterange_tool(
        vault_path=vault,
        start="2026-05-01",
        end="2026-05-02",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "ok"
    assert result["fragments_affected"] == 1


def test_purge_daterange_rejects_invalid_date(vault: Path) -> None:
    """Malformed ISO dates surface a structured error, not a stack trace."""
    result = purge_daterange_tool(
        vault_path=vault,
        start="not-a-date",
        end="2026-05-02",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert "start" in result["reason"] or "date" in result["reason"]


def test_purge_daterange_rejects_inverted_window(vault: Path) -> None:
    """The engine raises when ``end`` is before ``start`` — surface it cleanly."""
    result = purge_daterange_tool(
        vault_path=vault,
        start="2026-05-10",
        end="2026-05-01",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"


# ---------------------------------------------------------------------------
# Vault purge: requires BOTH token and confirm_vault_path
# ---------------------------------------------------------------------------


def test_purge_vault_refuses_without_confirm_path(vault: Path) -> None:
    """Even with the elevated token, ``confirm_vault_path`` is required."""
    result = purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=None,
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_purge_vault_refuses_when_confirm_path_does_not_match(
    vault: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The confirmation path must equal the resolved vault root exactly."""
    other = tmp_path_factory.mktemp("other-vault")
    result = purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=str(other),
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_purge_vault_proceeds_with_token_and_matching_path(vault: Path) -> None:
    """Token + matching path = the only combination that destroys content."""
    result = purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=str(vault),
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "ok"
    assert not (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_purge_vault_resolves_relative_confirmation_paths(vault: Path) -> None:
    """Symlink-free, relative-form paths still resolve to the same vault."""
    # `.../vault/.` resolves to vault; resolution must accept it.
    result = purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=str(vault / "."),
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Audit hardening: the purge entries chain into the MCP audit log
# ---------------------------------------------------------------------------


def test_purge_audit_entry_records_operation_and_consumer(vault: Path) -> None:
    """A successful purge appends to ``mcp.jsonl`` with ``entry_hash``."""
    purge_fragment_tool(
        vault_path=vault,
        fragment_id="frag-001",
        auth_token=ELEVATED_TOKEN,
        consumer="claude-code",
    )
    entries = _audit_entries(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "creek.purge.fragment"
    assert entry["consumer"] == "claude-code"
    assert "entry_hash" in entry
    assert "prev_hash" in entry
    # Token must not leak into the args summary
    assert "auth_token" not in entry["args_summary"]


# ---------------------------------------------------------------------------
# Engine-exception path: the audit-completeness invariant
# ---------------------------------------------------------------------------


_EXPLOSIVE_FAILURE = "engine boom"


def _explode(*_args: object, **_kwargs: object) -> None:
    """Stand-in for a :class:`PurgeEngine` method that raises mid-call."""
    raise RuntimeError(_EXPLOSIVE_FAILURE)


def test_purge_fragment_audits_engine_exception(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise from ``PurgeEngine.purge_fragment`` still writes one audit entry.

    Regression for the audit-completeness invariant flagged on PR #233.
    The module docstring promises "every call appends an audit entry to
    ``mcp.jsonl`` regardless of whether the purge proceeded" — an
    unhandled exception in the engine would silently break that.
    """
    from creek.purge import PurgeEngine as _PurgeEngine

    monkeypatch.setattr(_PurgeEngine, "purge_fragment", _explode)
    result = purge_fragment_tool(
        vault_path=vault,
        fragment_id="frag-001",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert _EXPLOSIVE_FAILURE in result["reason"]
    entries = _audit_entries(vault)
    assert len(entries) == 1
    assert entries[0]["tool"] == "creek.purge.fragment"


def test_purge_source_audits_engine_exception(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise from ``PurgeEngine.purge_source`` still writes one audit entry."""
    from creek.purge import PurgeEngine as _PurgeEngine

    monkeypatch.setattr(_PurgeEngine, "purge_source", _explode)
    result = purge_source_tool(
        vault_path=vault,
        source_type="journal",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert _EXPLOSIVE_FAILURE in result["reason"]
    entries = _audit_entries(vault)
    assert len(entries) == 1
    assert entries[0]["tool"] == "creek.purge.source"


def test_purge_classifications_audits_engine_exception(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise from ``PurgeEngine.purge_classifications`` still writes audit."""
    from creek.purge import PurgeEngine as _PurgeEngine

    monkeypatch.setattr(_PurgeEngine, "purge_classifications", _explode)
    result = purge_classifications_tool(
        vault_path=vault,
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert _EXPLOSIVE_FAILURE in result["reason"]
    entries = _audit_entries(vault)
    assert len(entries) == 1
    assert entries[0]["tool"] == "creek.purge.classifications"


def test_purge_vault_audits_engine_exception(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise from ``PurgeEngine.purge_vault`` still writes one audit entry.

    The path-confirmation guards both passed at this point; the failure
    happens inside the engine. The audit must still capture it.
    """
    from creek.purge import PurgeEngine as _PurgeEngine

    monkeypatch.setattr(_PurgeEngine, "purge_vault", _explode)
    result = purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=str(vault),
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert _EXPLOSIVE_FAILURE in result["reason"]
    entries = _audit_entries(vault)
    assert len(entries) == 1
    assert entries[0]["tool"] == "creek.purge.vault"


def test_purge_daterange_audits_engine_exception(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``daterange`` already handled ValueError; this pins the general case."""
    from creek.purge import PurgeEngine as _PurgeEngine

    monkeypatch.setattr(_PurgeEngine, "purge_daterange", _explode)
    result = purge_daterange_tool(
        vault_path=vault,
        start="2026-05-01",
        end="2026-05-02",
        auth_token=ELEVATED_TOKEN,
    )
    assert result["status"] == "refused"
    assert _EXPLOSIVE_FAILURE in result["reason"]
    entries = _audit_entries(vault)
    assert len(entries) == 1
    assert entries[0]["tool"] == "creek.purge.daterange"


def test_crawdad_consumer_cannot_purge(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end regression: a CrawDad-style call (no token) is refused.

    CrawDad's MCP client is deliberately configured without the
    elevated token. The vault must remain intact after such a call —
    this test stands in for the FEAT's "CrawDad cannot purge anything"
    fixture-vault regression.

    The server-side token must clear the 32-char floor (#907): with a weak
    secret configured the refusal would be right for the *wrong* reason
    (weak server config, not CrawDad's missing token).
    """
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", ELEVATED_TOKEN)
    result = purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=str(vault),
        auth_token=None,  # CrawDad has no token
        consumer="crawdad",
    )
    assert result["status"] == "refused"
    assert (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()


def test_purge_refused_when_configured_token_is_sub_minimum(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A weak server secret disarms purge entirely, without leaking why (#907).

    The caller presents the exact configured token *and* the correct
    ``confirm_vault_path`` — the only thing wrong is that the operator's
    secret is 31 characters. The purge must be refused, the vault left
    intact, and the refusal audited. The caller-visible ``reason`` must
    stay the generic elevated-authorization refusal: telling a hostile
    caller "the server secret is 31 chars" would be a configuration
    oracle handed out for free.
    """
    # 31 chars — one under the floor. Test literal, not a real credential.
    weak_token = "weak-elevated-" + "a" * 17
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", weak_token)

    result = purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=str(vault),
        auth_token=weak_token,  # an exact match against the weak secret
        consumer="claude-code",
    )

    assert result["status"] == "refused"
    assert (vault / "01-Fragments" / "Notes" / "frag-001.md").exists()
    entries = _audit_entries(vault)
    assert len(entries) == 1
    assert entries[0]["tool"] == "creek.purge.vault"
    reason = str(result["reason"])
    assert "32" not in reason  # no config oracle: never disclose the floor
    assert "chars" not in reason  # nor the observed length
    assert weak_token not in reason  # nor, obviously, the token itself


# ---------------------------------------------------------------------------
# The payload tells the whole truth (#1246)
# ---------------------------------------------------------------------------


def _make_the_voice_sweep_fall_short(vault_path: Path) -> None:
    """Set the vault up so ``purge_fragment`` completes but erases partially.

    Two ingredients. A ``07-Voice/`` directory, because the derived-
    artifact sweep is skipped entirely when the voice root does not
    exist. And bytes past the closing ``---`` that are not valid UTF-8,
    so the frontmatter still parses (the fragment still matches the
    purge criteria) while the strict body re-read the content-keyed
    profile pass depends on raises — which is exactly the shortfall
    :attr:`~creek.purge.engine.PurgeResult.voice_body_undecodable`
    exists to name.
    """
    (vault_path / "07-Voice").mkdir(exist_ok=True)
    frag_file = vault_path / "01-Fragments" / "Notes" / "frag-001.md"
    frag_file.write_bytes(frag_file.read_bytes() + b"\xff\xfe")


def _purge_frag_001(vault_path: Path) -> dict[str, object]:
    """Purge the seeded fragment through the MCP tool with a valid token."""
    return purge_fragment_tool(
        vault_path=vault_path,
        fragment_id="frag-001",
        auth_token=ELEVATED_TOKEN,
        consumer="claude-code",
    )


def _last_purge_outcome(vault_path: Path) -> PurgeAuditEntry:
    """Return the engine's final ``outcome`` line from ``purge.jsonl``."""
    outcomes = [e for e in PurgeAuditLog(vault_path).read() if e.phase == "outcome"]
    assert outcomes, "the engine must have written an outcome line"
    return outcomes[-1]


def test_a_partial_erasure_is_not_reported_as_ok(vault: Path) -> None:
    """An erasure that fell short must not be certified as success.

    The engine finished without raising, so every count in the payload
    is real — and a ``07-Voice/<register>-profile.md`` may still quote
    the fragment. ``ok`` would tell the caller the opposite.
    """
    _make_the_voice_sweep_fall_short(vault)

    payload = _purge_frag_001(vault)

    assert payload["status"] == "partial"


def test_the_mcp_status_agrees_with_the_audit_outcome_line(vault: Path) -> None:
    """The two surfaces must not describe the same purge differently.

    ``purge.jsonl`` is the compliance record and the MCP payload is what
    the operator's client sees; the field is even spelled ``status`` on
    both. One saying ``partial`` while the other says ``ok`` is the
    defect, so the agreement is asserted rather than each half alone.
    """
    _make_the_voice_sweep_fall_short(vault)

    payload = _purge_frag_001(vault)

    assert _last_purge_outcome(vault).status == "partial"
    assert payload["status"] == "partial"


def test_the_payload_names_the_fragments_the_sweep_could_not_reach(
    vault: Path,
) -> None:
    """Ids, so the caller can act — a bare ``partial`` is not actionable."""
    _make_the_voice_sweep_fall_short(vault)

    payload = _purge_frag_001(vault)

    assert payload["voice_body_undecodable"] == ["frag-001"]


def test_a_complete_erasure_still_reports_ok(vault: Path) -> None:
    """The success spelling is unchanged, so existing callers keep working."""
    payload = _purge_frag_001(vault)

    assert payload["status"] == "ok"
    assert payload["voice_body_undecodable"] == []


def test_every_purge_result_field_reaches_the_caller(vault: Path) -> None:
    """The payload's field set is derived from the model, not hand-picked.

    A hand-maintained subset is the drift mechanism that lost six fields
    between #845 and #1211. This assertion is the tripwire: add a field
    to :class:`~creek.purge.engine.PurgeResult` and forget the payload,
    and this test — not a caller reading a silently-truncated erasure
    report — is what notices.
    """
    payload = _purge_frag_001(vault)

    missing = set(PurgeResult.model_fields) - set(payload)
    assert missing == set()


def test_the_previously_dropped_counters_reach_the_caller(vault: Path) -> None:
    """The six fields #1246 found missing, named one by one.

    The field-set tripwire above would pass if someone re-hardcoded a
    list that happened to be complete today; these are the specific
    fields whose absence made an incomplete erasure unreadable.
    """
    payload = _purge_frag_001(vault)

    for field in (
        "embeddings_removed",
        "provenance_scrubbed",
        "intimate_stubs_removed",
        "journal_staged_removed",
        "voice_artifacts_removed",
        "voice_body_undecodable",
    ):
        assert field in payload, f"{field} never reaches the MCP caller"
