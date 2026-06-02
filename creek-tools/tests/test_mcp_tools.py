"""Per-tool integration tests for the MCP wrappers (FEAT-010)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.author import author_tool
from creek_mcp.tools.draft import draft_tool
from creek_mcp.tools.lint import lint_tool
from creek_mcp.tools.mine import mine_tool
from creek_mcp.tools.state import state_render_tool
from creek_mcp.tools.state_read import state_read_tool

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _seed_vault(vault: Path) -> None:
    """Create the minimum folder layout the read tools expect."""
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/audit",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis/Daily",
        "07-Voice/Drafts",
        "10-Liminal/Synchronicities",
        "creek-skills",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str = "Note",
    privacy_tier: str = "open",
    body: str = "body text",
) -> None:
    """Write a minimal fragment file (frontmatter + body)."""
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
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


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault rooted under ``tmp_path``."""
    _seed_vault(tmp_path)
    yield tmp_path


# ---------------------------------------------------------------------------
# state.read
# ---------------------------------------------------------------------------


def test_state_read_returns_latest_md_content(vault: Path) -> None:
    """``state.read`` reads ``00-Creek-Meta/State/latest.md`` verbatim."""
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit report\n\nVault summary lives here.\n",
        encoding="utf-8",
    )
    result = state_read_tool(vault_path=vault)
    assert result["status"] == "ok"
    assert result["tool"] == "creek.state.read"
    assert result["tier_ceiling"] == "open"
    assert "Audit report" in result["content"]


def test_state_read_returns_empty_on_missing_report(vault: Path) -> None:
    """A fresh vault with no rendered report yields a structured "empty"."""
    result = state_read_tool(vault_path=vault)
    assert result["status"] == "empty"
    assert result["content"] == ""


def test_state_read_writes_audit_entry(vault: Path) -> None:
    """Every invocation appends one chained line to ``mcp.jsonl``."""
    state_read_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="claude-code",
    )
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[0])
    assert entry["tool"] == "creek.state.read"
    assert entry["consumer"] == "claude-code"
    assert entry["tier_ceiling"] == "personal"


def test_state_read_does_not_embed_fragment_bodies(vault: Path) -> None:
    """Intimate fragment bodies must not appear in the audit report.

    The audit report aggregates titles + counts. A vault with an
    ``intimate``-tier fragment must not see its body surface through
    ``state.read`` even when the caller specifies ``ceiling=open``.
    """
    _write_fragment(
        vault,
        frag_id="frag-intimate-1",
        title="Private moment",
        privacy_tier="intimate",
        body="this is a secret journal body",
    )
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit report\n\n- Fragments: 1\n",
        encoding="utf-8",
    )
    result = state_read_tool(vault_path=vault)
    assert "secret journal body" not in result["content"]


# ---------------------------------------------------------------------------
# state.render
# ---------------------------------------------------------------------------


def test_state_render_writes_report_file(vault: Path) -> None:
    """``state.render`` regenerates ``State/<iso-week>.md`` and returns it."""
    result = state_render_tool(vault_path=vault)
    assert result["status"] == "ok"
    assert result["report_path"].startswith("00-Creek-Meta/State/")
    assert "# Creek state" in result["content"]


def test_state_render_writes_audit_entry(vault: Path) -> None:
    """Render path also writes the audit entry."""
    state_render_tool(vault_path=vault, consumer="crawdad")
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[0])
    assert entry["tool"] == "creek.state.render"
    assert entry["consumer"] == "crawdad"


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def test_lint_runs_default_checks(vault: Path) -> None:
    """The default invocation returns one ``checks`` entry per ran check."""
    result = lint_tool(vault_path=vault)
    assert result["status"] == "ok"
    assert result["tool"] == "creek.lint"
    assert len(result["checks"]) >= 1
    assert all("name" in check for check in result["checks"])


def test_lint_writes_audit_entry_with_checks_summary(vault: Path) -> None:
    """The audit entry captures the check list as a count summary."""
    lint_tool(vault_path=vault, checks=["broken-links"], consumer="crawdad")
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[0])
    assert entry["tool"] == "creek.lint"
    # ``checks=["broken-links"]`` collapses to ``{"count": 1}`` via
    # summarise_args, so the audit log never grows with check names.
    assert entry["args_summary"]["checks"] == {"count": 1}


def test_lint_since_window(vault: Path) -> None:
    """``since`` triggers semantic checks via ``parse_since``."""
    result = lint_tool(vault_path=vault, since="7d")
    assert result["status"] == "ok"


def test_lint_empty_checks_list_runs_nothing(vault: Path) -> None:
    """``checks=[]`` must be distinct from ``checks=None``.

    Regression for PR #224 review: a falsy-check (``if checks``) on the
    parameter previously collapsed ``[]`` into ``None`` and ran the
    full default check set, the opposite of what an explicit empty
    list signals.
    """
    result_empty = lint_tool(vault_path=vault, checks=[])
    result_default = lint_tool(vault_path=vault)
    assert len(result_empty["checks"]) == 0
    assert len(result_default["checks"]) > 0


# ---------------------------------------------------------------------------
# mine
# ---------------------------------------------------------------------------


def test_mine_returns_empty_list_for_fresh_vault(vault: Path) -> None:
    """An empty vault yields zero seeds without raising."""
    result = mine_tool(vault_path=vault, phase="rising")
    assert result["status"] == "ok"
    assert result["total"] == 0
    assert result["seeds"] == []


def test_mine_honours_tier_ceiling(vault: Path) -> None:
    """``ceiling=open`` maps to ``PrivacyTierOverride.OPEN`` for the miner."""
    result = mine_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.OPEN,
        phase="unclassified",
    )
    assert result["tier_ceiling"] == "open"


def test_mine_writes_audit_entry(vault: Path) -> None:
    """The audit entry pins phase + limit but not vault contents."""
    mine_tool(vault_path=vault, phase="rising", limit=5, consumer="crawdad")
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[0])
    assert entry["tool"] == "creek.mine"
    assert entry["args_summary"]["phase"] == "rising"
    assert entry["args_summary"]["limit"] == 5


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------


def _stub_seed() -> object:
    """Build a minimal :class:`IdeaSeed`-shaped object for draft tests."""
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="Stub idea",
        source_fragments=("frag-1",),
        threads=("thread-1",),
        eddies=("eddy-1",),
        frequency_affinity=(),
        brief_description="brief",
        score=0.75,
    )


def test_draft_returns_empty_when_no_seeds(vault: Path) -> None:
    """No seeds → structured empty (not a crash, not a refusal)."""
    result = draft_tool(
        vault_path=vault,
        llm=lambda prompt: "ignored",
        phase="rising",
    )
    assert result["status"] == "empty"
    assert result["reason"] == "no idea seeds surfaced"


def test_draft_writes_audit_entry_for_empty_seed_path(vault: Path) -> None:
    """Audit happens even when the draft cannot proceed (empty seeds)."""
    draft_tool(
        vault_path=vault,
        llm=lambda prompt: "ignored",
        phase="rising",
        consumer="crawdad",
    )
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[0])
    assert entry["tool"] == "creek.draft"
    assert entry["consumer"] == "crawdad"
    # Body never embedded in args_summary — LLM responses live outside
    # the audit log.
    assert "body" not in entry["args_summary"]


def test_draft_success_path_saves_file(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When seeds exist, ``draft_tool`` saves a draft and returns its path."""
    from creek.generate.drafts import Draft

    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **kw: type(
            "_M",
            (),
            {"mine_all": lambda self, vault_path, *, current_phase: [_stub_seed()]},
        )(),
    )

    class _StubGenerator:
        def __init__(self, **kw: object) -> None:
            pass

        def generate_draft(self, idea: object, *, vault_path: Path) -> Draft:
            return Draft(
                title="Stub idea",
                body="drafted",
                idea_strategy="thread_terminus",
                source_fragments=("frag-1",),
                threads=("thread-1",),
                eddies=("eddy-1",),
                skill_stack=(),
                prompt="prompt",
                generated_date=datetime(2026, 5, 11, tzinfo=UTC),
            )

        def save_draft(self, draft: Draft, vault_path: Path) -> Path:
            target = vault_path / "07-Voice" / "Drafts" / "2026-05-11-stub.md"
            target.write_text("body", encoding="utf-8")
            return target

    monkeypatch.setattr("creek_mcp.tools.draft.DraftGenerator", _StubGenerator)
    result = draft_tool(
        vault_path=vault,
        llm=lambda prompt: "drafted",
        phase="rising",
    )
    assert result["status"] == "ok"
    assert result["draft_path"] == "07-Voice/Drafts/2026-05-11-stub.md"
    assert result["title"] == "Stub idea"
    assert result["source_fragments"] == ["frag-1"]


def test_draft_refuses_when_llm_returns_empty(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``RuntimeError`` from the generator is converted into a refusal."""
    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **kw: type(
            "_M",
            (),
            {"mine_all": lambda self, vault_path, *, current_phase: [_stub_seed()]},
        )(),
    )

    class _BrokenGenerator:
        def __init__(self, **kw: object) -> None:
            pass

        def generate_draft(self, idea: object, *, vault_path: Path) -> object:
            msg = "LLM returned an empty draft body"
            raise RuntimeError(msg)

    monkeypatch.setattr("creek_mcp.tools.draft.DraftGenerator", _BrokenGenerator)
    result = draft_tool(
        vault_path=vault,
        llm=lambda prompt: "",
        phase="rising",
    )
    assert result["status"] == "refused"
    assert "LLM" in result["reason"]


def test_draft_refuses_for_out_of_range_index(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-range index returns a structured refusal, not a raise.

    Regression for PR #224 review: the previous implementation raised
    ``TierCeilingViolationError`` for caller input errors, conflating
    privacy refusals with programmer mistakes and bypassing the
    refusal-response JSON path. The graceful pattern mirrors the
    ``"no idea seeds"`` empty path already in ``draft_tool``.
    """
    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **kw: type(
            "_M",
            (),
            {"mine_all": lambda self, vault_path, *, current_phase: [_stub_seed()]},
        )(),
    )
    result = draft_tool(
        vault_path=vault,
        llm=lambda prompt: "x",
        phase="rising",
        index=99,
    )
    assert result["status"] == "refused"
    assert result["tool"] == "creek.draft"
    assert "out of range" in result["reason"]


def test_author_tool_returns_stub_draft_with_verdict(vault: Path) -> None:
    """``creek.author`` returns a typed stub draft (verdict + provenance)."""
    result = author_tool(
        vault_path=vault,
        query="What is F6 Pluralism?",
        medium="research",
        consumer="test",
    )
    assert result["status"] == "ok"
    assert result["tool"] == "creek.author"
    assert result["medium"] == "research"
    # Deterministic stub: the verdict is exactly PASS, not merely a member of
    # the verdict set (a membership check would be vacuous here).
    assert result["verdict"] == "PASS"
    assert isinstance(result["provenance"], list)
    assert result["provenance"]  # non-empty mock provenance
    assert result["body"].strip()
    assert result["dry_run"] is False
    assert result["rounds"] == 1  # stub always reports a single round


def test_author_tool_rejects_unknown_medium(vault: Path) -> None:
    """An unsupported medium returns a structured error, not a draft."""
    result = author_tool(
        vault_path=vault,
        query="q",
        medium="book-report",
        consumer="test",
    )
    assert result["status"] == "error"
    assert result["tool"] == "creek.author"
    assert result["tier_ceiling"] == "open"  # ceiling echoed on the error path
    assert "research" in result["reason"]


def test_author_tool_error_envelope_includes_dry_run(vault: Path) -> None:
    """The error envelope carries ``dry_run`` so the shape matches success."""
    result = author_tool(
        vault_path=vault,
        query="q",
        medium="book-report",
        dry_run=True,
        consumer="test",
    )
    assert result["status"] == "error"
    assert result["dry_run"] is True


def test_author_tool_echoes_non_default_ceiling(vault: Path) -> None:
    """A non-default privacy ceiling is echoed on the success path."""
    result = author_tool(
        vault_path=vault,
        query="q",
        medium="research",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="test",
    )
    assert result["status"] == "ok"
    assert result["tier_ceiling"] == "personal"


def test_author_tool_records_max_rounds_in_audit(vault: Path) -> None:
    """A non-``None`` ``max_rounds`` is captured in the audit entry."""
    author_tool(
        vault_path=vault,
        query="q",
        medium="research",
        max_rounds=5,
        consumer="test",
    )
    entry = json.loads(
        (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()[0]
    )
    assert entry["tool"] == "creek.author"
    assert entry["args_summary"]["max_rounds"] == 5


def test_author_tool_echoes_dry_run_flag(vault: Path) -> None:
    """The ``dry_run`` arg is accepted for CLI parity and echoed back."""
    result = author_tool(
        vault_path=vault,
        query="q",
        medium="research",
        dry_run=True,
        consumer="test",
    )
    assert result["status"] == "ok"
    assert result["dry_run"] is True


def test_author_tool_writes_audit_entry(vault: Path) -> None:
    """The tool appends an audit entry recording the call."""
    author_tool(vault_path=vault, query="q", medium="research", consumer="test")
    audit = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert "creek.author" in audit


def test_author_tool_returns_real_cited_draft(vault: Path) -> None:
    """``creek.author`` delegates to the real desk: cited claims + verdict (#460)."""
    result = author_tool(
        vault_path=vault,
        query="F6 medicine vs toxic",
        medium="research",
        consumer="test",
    )

    assert result["status"] == "ok"
    assert result["verdict"] in {"PASS", "REVISE", "ESCALATE"}
    assert result["claims"]  # non-empty
    assert all(claim["source_fragments"] for claim in result["claims"])


def test_author_tool_dry_run_returns_plan(vault: Path) -> None:
    """``dry_run`` returns the pipeline plan + evidence summary, not a draft."""
    result = author_tool(
        vault_path=vault,
        query="q",
        medium="research",
        dry_run=True,
        consumer="test",
    )

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["plan"]
    assert result["evidence"]["claims"] >= 1


def test_author_tool_forwards_max_rounds(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_rounds`` is now forwarded to the real desk (was a no-op in the stub)."""
    from creek.author import run_author as real_run_author

    captured: dict[str, object] = {}

    def spy(**kwargs: object) -> object:
        captured.update(kwargs)
        return real_run_author(**kwargs)

    monkeypatch.setattr("creek_mcp.tools.author.run_author", spy)

    author_tool(
        vault_path=vault, query="q", medium="research", max_rounds=7, consumer="test"
    )

    assert captured["max_rounds"] == 7
