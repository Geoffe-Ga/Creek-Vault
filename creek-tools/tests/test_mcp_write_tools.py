"""Per-tool tests for the FEAT-011 MCP write tool wrappers.

Covers the seven new tools (``save``, ``ingest``, ``classify``,
``link``, ``report``, ``skills.refresh``, ``compile``), the write-side
tier-ceiling enforcement helper, and the audit-log write-side fields
(``created_path``, ``created_tier``, ``affected_fragment_ids``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.models import PrivacyTier
from creek.save import TARGET_SUBDIRS
from creek_mcp.audit import MCP_AUDIT_RELPATH, MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, write_tier_allowed
from creek_mcp.tools.classify import classify_tool
from creek_mcp.tools.compile import compile_tool
from creek_mcp.tools.ingest import ingest_tool
from creek_mcp.tools.link import link_tool
from creek_mcp.tools.report import report_tool
from creek_mcp.tools.save import save_tool
from creek_mcp.tools.skills import skills_refresh_tool

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Seed the directory layout the write tools expect."""
    for relparts in {
        ("00-Creek-Meta", "audit"),
        ("00-Creek-Meta", "Processing-Log"),
        ("00-Creek-Meta", "State"),
        ("01-Fragments", "Notes"),
        *TARGET_SUBDIRS.values(),
        ("10-Liminal", "Compost", "intimate-stubs"),
        ("06-Frequencies",),
        ("creek-skills",),
    }:
        (tmp_path.joinpath(*relparts)).mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _read_audit(vault: Path) -> list[dict[str, object]]:
    """Decode every entry in ``mcp.jsonl`` for an assertion-friendly list."""
    path = vault / MCP_AUDIT_RELPATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    body: str = "Body text.",
    privacy_tier: str = "open",
) -> None:
    """Write a minimal fragment under ``01-Fragments/Notes`` for compile tests."""
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": f"Title {frag_id}",
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": privacy_tier,
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tier-ceiling helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("write_tier", "ceiling", "expected"),
    [
        (PrivacyTier.OPEN, TierCeiling.OPEN, True),
        (PrivacyTier.PERSONAL, TierCeiling.OPEN, False),
        (PrivacyTier.INTIMATE, TierCeiling.OPEN, False),
        (PrivacyTier.PERSONAL, TierCeiling.PERSONAL, True),
        (PrivacyTier.INTIMATE, TierCeiling.PERSONAL, False),
        (PrivacyTier.INTIMATE, TierCeiling.INTIMATE, True),
        (PrivacyTier.INTIMATE, TierCeiling.ALL, True),
    ],
)
def test_write_tier_allowed_matrix(
    write_tier: PrivacyTier,
    ceiling: TierCeiling,
    expected: bool,
) -> None:
    """Write-side rule: ``ceiling`` must admit the to-be-created tier."""
    assert write_tier_allowed(write_tier, ceiling) is expected


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_save_writes_note_under_target_dir(vault: Path) -> None:
    """A successful save returns the relative path under the vault."""
    result = save_tool(
        vault_path=vault,
        target="thread",
        body="An answer worth keeping.",
        title="A useful insight",
        tier="open",
        provenance=["frag-a", "frag-b"],
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["saved_path"].startswith("02-Threads/Active/")
    assert result["created_tier"] == "open"
    assert result["affected_fragment_ids"] == ["frag-a", "frag-b"]


def test_save_writes_audit_with_write_side_fields(vault: Path) -> None:
    """The audit entry carries ``created_path``, ``created_tier``, and IDs."""
    save_tool(
        vault_path=vault,
        target="praxis",
        body="Take the small step now.",
        title="Praxis stub",
        tier="open",
        provenance=["frag-x"],
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    entries = _read_audit(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "creek.save"
    assert entry["created_tier"] == "open"
    assert entry["affected_fragment_ids"] == ["frag-x"]
    assert entry["created_path"].startswith("04-Praxis/")


def test_save_refuses_when_tier_exceeds_ceiling(vault: Path) -> None:
    """Intimate save with ``ceiling=open`` is refused, not downgraded."""
    body = "intimate journal contents"
    result = save_tool(
        vault_path=vault,
        target="unnamed",
        body=body,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "exceeds ceiling" in result["reason"]
    # And the body must not have been written under the vault.
    written = list((vault / "10-Liminal" / "Unnamed").glob("*.md"))
    assert written == []
    # An audit entry is still written for the refusal — and the body
    # must not appear verbatim in the audit log.
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert body not in raw


def test_save_success_path_does_not_embed_body(vault: Path) -> None:
    """Success path must not stash the body verbatim in ``mcp.jsonl``.

    Regression for PR #228 review: a body shorter than the
    ``summarise_args`` 64-char threshold previously rode along
    verbatim because the success-path audit dict carried ``body=body``.
    The fix records ``body_len`` instead, mirroring the refusal path.
    """
    body = "secret answer"  # short — would NOT be truncated by summarise_args.
    save_tool(
        vault_path=vault,
        target="thread",
        body=body,
        title="Saved insight",
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert body not in raw
    entry = _read_audit(vault)[0]
    assert entry["args_summary"]["body_len"] == len(body)


def test_save_refuses_unknown_target(vault: Path) -> None:
    """An unknown ``target`` returns a structured refusal."""
    result = save_tool(
        vault_path=vault,
        target="nonsense",
        body="x",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unknown target" in result["reason"]


def test_save_refuses_unknown_tier(vault: Path) -> None:
    """An unknown ``tier`` returns a structured refusal."""
    result = save_tool(
        vault_path=vault,
        target="thread",
        body="x",
        tier="not-a-tier",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unknown tier" in result["reason"]


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def test_ingest_refuses_open_ceiling_against_personal_default(vault: Path) -> None:
    """``ceiling=open`` cannot create personal-tier fragments via ingest."""
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(vault / "missing.md"),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "exceeds ceiling" in result["reason"]
    # Should not have written anything to 01-Fragments either.
    assert not any((vault / "01-Fragments").rglob("*.md"))


def test_ingest_refuses_unknown_source_type(vault: Path) -> None:
    """An unknown source_type returns a structured refusal."""
    result = ingest_tool(
        vault_path=vault,
        source_type="not-a-thing",
        input_path=str(vault),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "unknown source_type" in result["reason"]


def test_ingest_refuses_missing_path(vault: Path) -> None:
    """A non-existent input path returns a structured refusal."""
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(vault / "nope.md"),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "not found" in result["reason"]


def test_ingest_writes_audit_entry_on_refusal(vault: Path) -> None:
    """Refusal at the tier-ceiling gate still appends an audit entry."""
    ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(vault / "missing.md"),
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    entries = _read_audit(vault)
    assert any(e["tool"] == "creek.ingest" for e in entries)


def test_ingest_writes_fragments_and_audit(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success path: written count + ``affected_fragment_ids`` recorded."""
    from creek.ingest.base import IngestedFragment

    class _StubResult:
        def __init__(self) -> None:
            self.fragments: list[object] = ["stub-parsed"]
            self.errors: list[str] = []

    class _StubIngestor:
        def ingest(self, _input: object) -> object:
            return _StubResult()

    stub_fragment = IngestedFragment.__new__(IngestedFragment)
    fragment_holder: dict[str, object] = {}

    def _stub_assemble(_parsed: object) -> object:
        return type(
            "_A",
            (),
            {
                "fragment": type("_F", (), {"id": "frag-stub-1"})(),
                "body": "stub body",
            },
        )()

    class _StubWriter:
        def __init__(self, *, vault_path: object) -> None:
            fragment_holder["vault"] = vault_path

        def write_fragment(self, fragment: object, *, body: str) -> None:
            fragment_holder["fragment"] = fragment
            fragment_holder["body"] = body

    monkeypatch.setattr(
        "creek_mcp.tools.ingest.INGESTOR_REGISTRY",
        {"markdown": _StubIngestor},
    )
    monkeypatch.setattr(
        "creek_mcp.tools.ingest.assemble_ingested_fragment",
        _stub_assemble,
    )
    monkeypatch.setattr("creek_mcp.tools.ingest.VaultWriter", _StubWriter)

    fake_input = vault / "input.md"
    fake_input.write_text("# stub\n", encoding="utf-8")
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(fake_input),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert result["written"] == 1
    assert result["affected_fragment_ids"] == ["frag-stub-1"]
    entries = _read_audit(vault)
    last = entries[-1]
    assert last["tool"] == "creek.ingest"
    assert last["created_tier"] == "personal"
    assert last["affected_fragment_ids"] == ["frag-stub-1"]
    # Ensure the unused IngestedFragment alias does not cause import warnings.
    assert stub_fragment is not None


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


def test_classify_refuses_unknown_method(vault: Path) -> None:
    """An unknown method returns a structured refusal."""
    result = classify_tool(
        vault_path=vault,
        method="kazoo",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unknown method" in result["reason"]


def test_classify_runs_rules_on_empty_vault(vault: Path) -> None:
    """An empty vault returns zero counts and writes one audit entry."""
    result = classify_tool(
        vault_path=vault,
        method="rules",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert result["total"] == 0
    entries = _read_audit(vault)
    assert entries[-1]["tool"] == "creek.classify"
    assert entries[-1]["affected_fragment_ids"] == []


def test_classify_refuses_when_llm_provider_unavailable(vault: Path) -> None:
    """MCP classify returns a structured refusal, not a traceback.

    The fail-fast gate in :func:`run_classify` raises
    ``LLMProviderUnavailableError`` when the LLM cannot be reached. The
    MCP wrapper must translate that to the standard ``status: refused``
    payload so callers see a stable shape, never an unhandled
    ``RuntimeError`` propagating through the MCP transport.
    """
    from unittest.mock import PropertyMock, patch

    from creek.classify.classify_engine import LLMClassifier
    from creek.models import Fragment, FragmentSource, SourcePlatform

    # Seed at least one fragment so the engine reaches the gate (an
    # empty 01-Fragments early-returns before the availability check).
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    fragment = Fragment(
        id="frag-mcpunavail01",
        title="placeholder",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = fragments_dir / "frag.md"
    file.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="content",
                **fragment.model_dump(mode="json"),
            ),
        ),
        encoding="utf-8",
    )

    with patch.object(
        LLMClassifier,
        "available",
        new_callable=PropertyMock,
        return_value=False,
    ):
        result = classify_tool(
            vault_path=vault,
            method="llm",
            privacy_tier_ceiling=TierCeiling.OPEN,
            consumer="crawdad",
        )

    assert result["status"] == "refused"
    assert result["tool"] == "creek.classify"
    assert "unavailable" in result["reason"]


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


def test_link_refuses_unknown_method(vault: Path) -> None:
    """An unknown method returns a structured refusal."""
    result = link_tool(
        vault_path=vault,
        method="kazoo",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unknown method" in result["reason"]


def test_link_runs_temporal_on_empty_vault(vault: Path) -> None:
    """An empty vault returns zero counts and writes one audit entry."""
    result = link_tool(
        vault_path=vault,
        method="temporal",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert result["fragment_count"] == 0
    entries = _read_audit(vault)
    assert entries[-1]["tool"] == "creek.link"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_refuses_unsupported_type(vault: Path) -> None:
    """An unsupported report_type returns a structured refusal."""
    result = report_tool(
        vault_path=vault,
        report_type="wavelength",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unsupported report_type" in result["reason"]


def test_report_tags_writes_audit_entry(vault: Path) -> None:
    """``tags`` report runs to completion and writes an audit entry."""
    result = report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert result["report_type"] == "tags"
    entries = _read_audit(vault)
    assert entries[-1]["tool"] == "creek.report"


def test_report_decisions_stub_returns_would_generate(vault: Path) -> None:
    """The ``decisions`` skeleton routes to a stub: ok, no paths, writes nothing."""
    result = report_tool(
        vault_path=vault,
        report_type="decisions",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["report_type"] == "decisions"
    assert result["report_paths"] == []
    assert "would generate" in result["note"].lower()
    assert "08-Decisions/" in result["note"]
    # The invocation is still audited, but nothing was written — the audit
    # entry omits ``created_path`` entirely when no file is produced.
    last = _read_audit(vault)[-1]
    assert last["tool"] == "creek.report"
    assert "created_path" not in last


def test_report_lexicon_stub_returns_would_generate(vault: Path) -> None:
    """The ``lexicon`` skeleton routes to a stub: ok, no paths, writes nothing."""
    result = report_tool(
        vault_path=vault,
        report_type="lexicon",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["report_type"] == "lexicon"
    assert result["report_paths"] == []
    assert "would generate" in result["note"].lower()
    assert "07-Voice/Lexicon/" in result["note"]


# ---------------------------------------------------------------------------
# skills.refresh
# ---------------------------------------------------------------------------


def test_skills_refresh_writes_audit_entry(vault: Path) -> None:
    """``skills.refresh`` runs to completion and writes an audit entry."""
    result = skills_refresh_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    entries = _read_audit(vault)
    last = entries[-1]
    assert last["tool"] == "creek.skills.refresh"
    assert last["created_path"] == "creek-skills"


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------


def _noop_llm_factory() -> object:
    """Return a deterministic stub LLM for compile-tool tests."""
    return lambda _prompt: "{}"


def test_compile_refuses_unknown_target_kind(vault: Path) -> None:
    """An unknown target_kind returns a structured refusal."""
    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-1"],
        target_kind="not-a-kind",
        target_id="x",
        target_title="x",
        llm_factory=_noop_llm_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unknown target_kind" in result["reason"]


def test_compile_refuses_empty_fragment_ids(vault: Path) -> None:
    """An empty fragment_ids list is refused before any work."""
    result = compile_tool(
        vault_path=vault,
        fragment_ids=[],
        target_kind="thread",
        target_id="x",
        target_title="x",
        llm_factory=_noop_llm_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "non-empty" in result["reason"]


def test_compile_writes_audit_then_noop_on_re_run(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First compile writes an audit entry; a no-op re-run does not.

    Regression for FEAT-011 acceptance: re-invoking ``creek.compile``
    (idempotent per FEAT-003) doesn't double-write audit entries on
    no-op runs.
    """
    _write_fragment(vault, frag_id="frag-1")
    target_path = vault / "02-Threads" / "Active" / "thread-x.md"

    def _stub_compile(**_kw: object) -> object:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.write_text("# Thread X\n\nclaim\n", encoding="utf-8")
        return target_path

    monkeypatch.setattr(
        "creek_mcp.tools.compile.compile_to_vault",
        _stub_compile,
    )
    stub_factory = lambda: lambda _prompt: "{}"  # noqa: E731

    first = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-1"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=stub_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert first["status"] == "ok"
    entries_after_first = _read_audit(vault)
    assert sum(1 for e in entries_after_first if e["tool"] == "creek.compile") == 1

    second = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-1"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=stub_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert second["status"] == "noop"
    entries_after_second = _read_audit(vault)
    # The noop must not append a new ``creek.compile`` entry.
    compile_entries = [e for e in entries_after_second if e["tool"] == "creek.compile"]
    assert len(compile_entries) == 1


def test_compile_propagates_engine_errors_as_refusal(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``ValueError`` / ``RuntimeError`` becomes a structured refusal."""

    def _broken(**_kw: object) -> object:
        msg = "engine boom"
        raise ValueError(msg)

    monkeypatch.setattr("creek_mcp.tools.compile.compile_to_vault", _broken)
    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-1"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=lambda: lambda _prompt: "{}",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "engine boom" in result["reason"]


# ---------------------------------------------------------------------------
# Audit-log write-side schema
# ---------------------------------------------------------------------------


def test_audit_log_write_side_fields_round_trip(vault: Path) -> None:
    """Direct append: write-side fields persist verbatim through the log."""
    MCPAuditLog(vault).append(
        tool="creek.save",
        args={"target": "thread", "body": "x"},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
        created_path="02-Threads/Active/2026-05-12-x.md",
        created_tier="open",
        affected_fragment_ids=["frag-a", "frag-b"],
    )
    entry = _read_audit(vault)[0]
    assert entry["created_path"].endswith("-x.md")
    assert entry["created_tier"] == "open"
    assert entry["affected_fragment_ids"] == ["frag-a", "frag-b"]


def test_audit_log_read_side_does_not_emit_write_fields(vault: Path) -> None:
    """Read tools must not stamp ``created_*`` keys onto their audit rows."""
    MCPAuditLog(vault).append(
        tool="creek.mine",
        args={"phase": "rising", "limit": 5},
        tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    entry = _read_audit(vault)[0]
    assert "created_path" not in entry
    assert "created_tier" not in entry
    assert "affected_fragment_ids" not in entry
