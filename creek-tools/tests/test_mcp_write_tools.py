"""Per-tool tests for the FEAT-011 MCP write tool wrappers.

Covers the seven new tools (``save``, ``ingest``, ``classify``,
``link``, ``report``, ``skills.refresh``, ``compile``), the write-side
tier-ceiling enforcement helper, and the audit-log write-side fields
(``created_path``, ``created_tier``, ``affected_fragment_ids``).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.compile.engine import PARADOX_LOG_RELPATH
from creek.models import PrivacyTier
from creek.save import TARGET_SUBDIRS
from creek_mcp.audit import (
    MCP_AUDIT_RELPATH,
    MCPAuditLog,
    verify_mcp_audit_chain,
)
from creek_mcp.tier_ceiling import TierCeiling, write_tier_allowed
from creek_mcp.tools.classify import classify_tool
from creek_mcp.tools.compile import _ABOVE_CEILING_REASON, compile_tool
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
# ingest — vault path confinement (issue #819)
# ---------------------------------------------------------------------------


def test_ingest_refuses_absolute_path_outside_vault(vault: Path) -> None:
    """An absolute ``input_path`` resolving outside the vault root is refused."""
    outside = vault.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(outside),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "outside the vault root" in result["reason"]
    assert not any((vault / "01-Fragments").rglob("*.md"))


def test_ingest_refuses_dot_dot_traversal_outside_vault(vault: Path) -> None:
    """A ``..``-relative ``input_path`` that escapes the vault is refused.

    Proves the confinement check resolves the path (collapsing ``..``)
    before comparing it against the vault root.
    """
    outside = vault.parent / "escape.md"
    outside.write_text("# escape\n", encoding="utf-8")
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(vault / ".." / "escape.md"),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "outside the vault root" in result["reason"]
    assert not any((vault / "01-Fragments").rglob("*.md"))


def test_ingest_refuses_symlink_escaping_vault(vault: Path) -> None:
    """A symlink inside the vault pointing outside it is refused.

    Proves the confinement check follows symlinks (via ``resolve()``)
    rather than trusting the in-vault-looking literal path.
    """
    outside = vault.parent / "secret.md"
    outside.write_text("# secret\n", encoding="utf-8")
    link = vault / "link-to-secret.md"
    os.symlink(outside, link)
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(link),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "outside the vault root" in result["reason"]
    assert not any((vault / "01-Fragments").rglob("*.md"))


def test_ingest_confinement_refusal_skips_ingestor_invocation(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confinement is checked before any ingestor touches the source file."""

    class _RaisingIngestor:
        def ingest(self, _input: object) -> object:
            raise AssertionError("should not be reached")

    monkeypatch.setattr(
        "creek_mcp.tools.ingest.INGESTOR_REGISTRY",
        {"markdown": _RaisingIngestor},
    )
    outside = vault.parent / "untouched.md"
    outside.write_text("# untouched\n", encoding="utf-8")

    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(outside),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"
    assert "outside the vault root" in result["reason"]


def test_ingest_relative_input_path_resolves_against_vault(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``input_path`` is resolved against the vault root, not cwd."""

    class _StubResult:
        def __init__(self) -> None:
            self.fragments: list[object] = ["stub-parsed"]
            self.errors: list[str] = []

    class _StubIngestor:
        def ingest(self, _input: object) -> object:
            return _StubResult()

    def _stub_assemble(_parsed: object) -> object:
        return type(
            "_A",
            (),
            {
                "fragment": type("_F", (), {"id": "frag-stub-rel"})(),
                "body": "stub body",
            },
        )()

    class _StubWriter:
        def __init__(self, *, vault_path: object) -> None:
            self.vault_path = vault_path

        def write_fragment(self, fragment: object, *, body: str) -> None:
            del fragment, body

    monkeypatch.setattr(
        "creek_mcp.tools.ingest.INGESTOR_REGISTRY",
        {"markdown": _StubIngestor},
    )
    monkeypatch.setattr(
        "creek_mcp.tools.ingest.assemble_ingested_fragment",
        _stub_assemble,
    )
    monkeypatch.setattr("creek_mcp.tools.ingest.VaultWriter", _StubWriter)

    (vault / "input.md").write_text("# stub\n", encoding="utf-8")
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path="input.md",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "ok"
    assert result["written"] == 1


def test_ingest_tier_ceiling_checked_before_confinement(vault: Path) -> None:
    """The tier-ceiling gate refuses before the confinement check runs.

    ``ceiling=open`` against the personal-tier default must fail on
    ``"exceeds ceiling"`` even for an outside-the-vault path, proving
    the ceiling gate still fires first.
    """
    outside = vault.parent / "outside-ceiling.md"
    outside.write_text("# outside\n", encoding="utf-8")
    result = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path=str(outside),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "exceeds ceiling" in result["reason"]


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


def test_report_decisions_generates_note(vault: Path) -> None:
    """The ``decisions`` report now generates a real Decision note (#581)."""
    frags = vault / "01-Fragments" / "Notes"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "frag-decide50.md").write_text(
        '---\ntype: fragment\nid: frag-decide50\ntitle: "Should I switch frameworks"\n'
        "source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )
    result = report_tool(
        vault_path=vault,
        report_type="decisions",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert result["report_type"] == "decisions"
    assert any("08-Decisions/Active" in p for p in result["report_paths"])
    assert _read_audit(vault)[-1]["tool"] == "creek.report"


def test_report_decisions_no_candidates_returns_empty_paths(vault: Path) -> None:
    """A decisions report on a signal-free vault is ok with no paths, no file."""
    result = report_tool(
        vault_path=vault,
        report_type="decisions",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["report_paths"] == []


def test_report_rhetorical_patterns_generates(vault: Path) -> None:
    """The ``rhetorical-patterns`` report writes a per-register note (#582)."""
    frags = vault / "01-Fragments" / "Notes"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "ex-1.md").write_text(
        '---\ntype: fragment\nid: ex-1\ntitle: "T"\n'
        "source:\n  platform: journal\n  author: self\n"
        "voice:\n  voice_register: confessional\n  confidence: conviction\n"
        "---\nThe truth is we rise; as I said before, we rise.\n",
        encoding="utf-8",
    )
    result = report_tool(
        vault_path=vault,
        report_type="rhetorical-patterns",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert any("07-Voice/Rhetorical-Patterns" in p for p in result["report_paths"])
    assert _read_audit(vault)[-1]["tool"] == "creek.report"


def test_report_rhetorical_patterns_no_exemplars_returns_empty_paths(
    vault: Path,
) -> None:
    """A rhetorical-patterns report on an exemplar-free vault is ok with no paths."""
    result = report_tool(
        vault_path=vault,
        report_type="rhetorical-patterns",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["report_paths"] == []


def test_report_mode_profiles_generates(vault: Path) -> None:
    """The ``mode-profiles`` report writes a per-mode note (#583)."""
    frags = vault / "01-Fragments" / "Notes"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "m1.md").write_text(
        '---\ntype: fragment\nid: m1\ntitle: "Building momentum"\n'
        "source:\n  platform: journal\n  author: self\n"
        "wavelength:\n  mode: express\n  phase: rising\n"
        "frequency:\n  primary: F3\n---\nbody\n",
        encoding="utf-8",
    )
    result = report_tool(
        vault_path=vault,
        report_type="mode-profiles",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert any("05-Wavelength/Mode-Profiles" in p for p in result["report_paths"])
    assert _read_audit(vault)[-1]["tool"] == "creek.report"


def test_report_mode_profiles_no_data_returns_empty_paths(vault: Path) -> None:
    """A mode-profiles report on a vault with no classified modes is ok, no paths."""
    result = report_tool(
        vault_path=vault,
        report_type="mode-profiles",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert result["report_paths"] == []


def _seed_voice_exemplar(vault: Path, frag_id: str, body: str) -> None:
    """Write a qualifying voice-exemplar fragment (settled + register) on disk."""
    fm = (
        f'type: fragment\nid: {frag_id}\ntitle: "T {frag_id}"\n'
        "source:\n  platform: journal\n  author: self\n"
        "frequency:\n  primary: F5\n"
        "wavelength:\n  phase: rising\n  mode: express\n"
        "voice:\n  voice_register: confessional\n  confidence: settled\n"
        "privacy_tier: personal\n"
    )
    (vault / "01-Fragments" / "Notes" / f"{frag_id}.md").write_text(
        f"---\n{fm}---\n{body}\n",
        encoding="utf-8",
    )


def test_report_lexicon_generates_glossary(vault: Path) -> None:
    """The ``lexicon`` report now generates a real glossary (#580), not a stub."""
    _seed_voice_exemplar(
        vault,
        "ex-1",
        "The dharma teaches that the river flows; the river flows toward the sea.",
    )
    result = report_tool(
        vault_path=vault,
        report_type="lexicon",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )
    assert result["status"] == "ok"
    assert result["report_type"] == "lexicon"
    assert any("glossary.md" in p for p in result["report_paths"])
    assert (vault / "07-Voice" / "Lexicon" / "glossary.md").exists()
    assert _read_audit(vault)[-1]["tool"] == "creek.report"


def test_report_lexicon_no_exemplars_returns_empty_paths(vault: Path) -> None:
    """A lexicon report on an exemplar-free vault is ok with no paths, no file."""
    result = report_tool(
        vault_path=vault,
        report_type="lexicon",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"
    assert result["report_paths"] == []
    assert not (vault / "07-Voice" / "Lexicon").exists()


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
# compile — write-side source-tier ceiling (issue #848)
# ---------------------------------------------------------------------------


class _RecordingLLMFactory:
    """Compile LLM factory that records how many times it was invoked.

    Issue #848's gate must refuse *before* the LLM client is built,
    because the compile prompt is the egress surface: it carries source
    fragment ids and titles to a possibly-cloud provider. A ``status ==
    "refused"`` assertion alone cannot prove that. The pre-fix wrapper
    evaluates ``llm_factory()`` as an argument *inside* the ``try``
    around ``compile_to_vault``, so any factory that raises
    ``ValueError``/``RuntimeError`` produces a refusal even with no gate
    at all. Counting invocations separates "refused by the ceiling
    gate" from "blew up on the way to the engine".
    """

    def __init__(self) -> None:
        """Start with a zero invocation count."""
        self.calls = 0

    def __call__(self) -> object:
        """Record one invocation and return a deterministic stub LLM."""
        self.calls += 1
        return lambda _prompt: "{}"


def test_compile_refuses_intimate_source_under_open_ceiling(vault: Path) -> None:
    """An ``intimate`` source fragment is refused at ``ceiling=open``.

    Acceptance test for issue #848: the ``creek_mcp.tools.compile``
    module docstring promises a caller cannot create a compiled page
    whose source fragments include a tier they could not read.
    """
    _write_fragment(vault, frag_id="frag-sealed", privacy_tier="intimate")
    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-sealed"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=_noop_llm_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON


def test_compile_refusal_never_invokes_the_llm_factory(vault: Path) -> None:
    """The above-ceiling refusal never builds the compile LLM client.

    This is the load-bearing egress assertion for issue #848: source
    fragment ids and titles reach the (possibly cloud) provider through
    the compile prompt, so the gate is only real if the factory is
    never called. See :class:`_RecordingLLMFactory` for why a refusal
    status on its own would be a false green.
    """
    _write_fragment(vault, frag_id="frag-sealed", privacy_tier="intimate")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-sealed"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert factory.calls == 0


def test_compile_refusal_writes_no_page_no_paradox_log_no_hash_marker(
    vault: Path,
) -> None:
    """An above-ceiling refusal leaves no on-disk trace of the sources.

    The stub LLM returns a payload that *would* produce both a compiled
    page (with provenance naming the intimate fragment) and a paradox
    log entry, so each missing artefact is evidence the engine never
    ran rather than evidence it ran and found nothing to write.
    """
    frag_id = "frag-sealed"
    _write_fragment(vault, frag_id=frag_id, privacy_tier="intimate")
    payload = json.dumps(
        {
            "claims": [{"id": "c1", "text": "A claim.", "fragment_ids": [frag_id]}],
            "paradoxes": [
                {"description": "A tension.", "fragment_ids": [frag_id]},
            ],
        },
    )

    def _leaky_factory() -> object:
        """Return a stub LLM that would emit a page body and a paradox."""
        return lambda _prompt: payload

    result = compile_tool(
        vault_path=vault,
        fragment_ids=[frag_id],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=_leaky_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert not (vault / "02-Threads" / "Active" / "thread-x.md").exists()
    assert not (vault / PARADOX_LOG_RELPATH).exists()
    assert not (vault / "00-Creek-Meta" / "audit" / "compile-thread-x.hash").exists()


def test_compile_refusal_is_audited_without_ids_or_tiers(vault: Path) -> None:
    """The refusal is audited, but the audit row names no source content.

    One ``creek.compile`` entry records *that* a ceiling violation was
    attempted (operators need the signal) while the write-side fields
    stay absent and ``fragment_ids`` collapses to the count that
    :func:`creek_mcp.audit.summarise_args` produces for any list — so
    the log cannot become a side channel for the ids, titles, or tier
    of content the caller was refused.
    """
    frag_id = "frag-sealed"
    _write_fragment(vault, frag_id=frag_id, privacy_tier="intimate")

    result = compile_tool(
        vault_path=vault,
        fragment_ids=[frag_id],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=_noop_llm_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON

    entries = [e for e in _read_audit(vault) if e["tool"] == "creek.compile"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["args_summary"]["fragment_ids"] == {"count": 1}
    assert "affected_fragment_ids" not in entry
    assert "created_path" not in entry
    assert "created_tier" not in entry

    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert frag_id not in raw
    assert f"Title {frag_id}" not in raw
    assert "intimate" not in raw

    # The refusal append must leave the hash chain intact — a tamper-evident
    # log that the refusal path could silently break would be worse than none.
    verify_mcp_audit_chain(vault)


@pytest.mark.parametrize(
    "fragment_ids",
    [
        ["frag-open", "frag-sealed"],
        ["frag-sealed", "frag-open"],
    ],
    ids=["above-ceiling-last", "above-ceiling-first"],
)
def test_compile_refuses_when_above_ceiling_source_is_not_first(
    vault: Path,
    fragment_ids: list[str],
) -> None:
    """One above-ceiling source refuses the whole call, at any position.

    The gate must scan every requested fragment, not just the head of
    the list. Both assertions are position-independent (invocation
    count and the reason constant) so the test says nothing about
    filesystem traversal order or fragment file naming.
    """
    _write_fragment(vault, frag_id="frag-open", privacy_tier="open")
    _write_fragment(vault, frag_id="frag-sealed", privacy_tier="intimate")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=fragment_ids,
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert factory.calls == 0


@pytest.mark.parametrize(
    ("privacy_tier", "ceiling", "allowed"),
    [
        ("open", TierCeiling.OPEN, True),
        ("personal", TierCeiling.OPEN, False),
        ("personal", TierCeiling.PERSONAL, True),
        ("intimate", TierCeiling.PERSONAL, False),
        ("intimate", TierCeiling.INTIMATE, True),
        ("intimate", TierCeiling.ALL, True),
    ],
)
def test_compile_source_tier_ceiling_matrix(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    privacy_tier: str,
    ceiling: TierCeiling,
    allowed: bool,
) -> None:
    """Source-tier admission mirrors :func:`write_tier_allowed` exactly.

    The allowed rows assert the factory *was* invoked, so a future
    short-circuit elsewhere in the wrapper cannot masquerade as the
    gate passing; the refused rows assert the ceiling reason and a
    never-invoked factory.
    """
    _write_fragment(vault, frag_id="frag-src", privacy_tier=privacy_tier)
    target_path = vault / "02-Threads" / "Active" / "thread-x.md"

    def _stub_compile(**_kw: object) -> object:
        """Write a placeholder target page instead of running the engine."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.write_text("# Thread X\n\nclaim\n", encoding="utf-8")
        return target_path

    if allowed:
        monkeypatch.setattr("creek_mcp.tools.compile.compile_to_vault", _stub_compile)
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-src"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=ceiling,
    )

    if allowed:
        assert result["status"] == "ok"
        assert factory.calls == 1
    else:
        assert result["status"] == "refused"
        assert result["reason"] == _ABOVE_CEILING_REASON
        assert factory.calls == 0


def test_compile_admits_unclassified_source_at_open_ceiling(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``unclassified`` source is admitted at ``ceiling=open``.

    This pins existing, deliberate policy rather than proposing new
    policy: ``creek_mcp.tier_ceiling._TIER_RANK`` ranks ``unclassified``
    alongside ``open`` (rank 0), and every ceiling comparison in the MCP
    surface inherits that. Issue #923 owns any change to the ranking; if
    it lands, update ``_TIER_RANK`` and this test together — do not
    weaken the ceiling gate here.
    """
    _write_fragment(vault, frag_id="frag-src", privacy_tier="unclassified")
    target_path = vault / "02-Threads" / "Active" / "thread-x.md"

    def _stub_compile(**_kw: object) -> object:
        """Write a placeholder target page instead of running the engine."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.write_text("# Thread X\n\nclaim\n", encoding="utf-8")
        return target_path

    monkeypatch.setattr("creek_mcp.tools.compile.compile_to_vault", _stub_compile)
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-src"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "ok"
    assert factory.calls == 1


def _write_fragment_without_tier(vault: Path, *, frag_id: str) -> None:
    """Write a fragment whose frontmatter omits ``privacy_tier`` entirely.

    Distinct from ``_write_fragment(..., privacy_tier="unclassified")``:
    that writes the key *explicitly*, which is what every
    pipeline-written fragment carries. Omitting the key is the
    hand-edited / legacy-vault case, where the tier is unknown rather
    than known-to-be-unclassified.
    """
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": f"Title {frag_id}",
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="Body text.", **metadata)),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("ceiling", "allowed"),
    [
        (TierCeiling.OPEN, False),
        (TierCeiling.PERSONAL, False),
        (TierCeiling.INTIMATE, True),
        (TierCeiling.ALL, True),
    ],
)
def test_compile_fails_closed_when_privacy_tier_key_is_absent(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: TierCeiling,
    allowed: bool,
) -> None:
    """A fragment with no ``privacy_tier`` key is treated as ``intimate``.

    The :class:`~creek.models.Fragment` model defaults a *missing*
    ``privacy_tier`` to ``unclassified``, which ranks 0 and would be
    admitted at every ceiling — so reading the tier off the model alone
    fails **open** on exactly the hand-edited or legacy file whose tier
    nobody can vouch for.

    ``creek.reflect`` already refuses that file closed to ``intimate``
    (``creek_mcp.tools.reflect._fragment_tier``, #847), mirroring
    :func:`creek.classify.privacy_filter.tier_of`. Compile must agree:
    two MCP tools that disagree about the same file is the divergence
    the shared-loader design exists to prevent. Contrast
    :func:`test_compile_admits_unclassified_source_at_open_ceiling`,
    where the key is present and explicitly ``unclassified``.
    """
    _write_fragment_without_tier(vault, frag_id="frag-legacy")
    target_path = vault / "02-Threads" / "Active" / "thread-x.md"

    def _stub_compile(**_kw: object) -> object:
        """Write a placeholder target page instead of running the engine."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.write_text("# Thread X\n\nclaim\n", encoding="utf-8")
        return target_path

    if allowed:
        monkeypatch.setattr("creek_mcp.tools.compile.compile_to_vault", _stub_compile)
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-legacy"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=ceiling,
    )

    if allowed:
        assert result["status"] == "ok"
        assert factory.calls == 1
    else:
        assert result["status"] == "refused"
        assert result["reason"] == _ABOVE_CEILING_REASON
        assert factory.calls == 0


def test_compile_missing_fragment_id_still_reports_not_found(vault: Path) -> None:
    """A nonexistent id keeps the engine's "not found" refusal.

    Runs the real engine with nothing above the ceiling in the vault:
    the new gate must let the call through so the caller still learns
    the id does not exist, instead of the ceiling check swallowing
    every unresolvable id behind a generic refusal.
    """
    _write_fragment(vault, frag_id="frag-open", privacy_tier="open")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-open", "frag-missing"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert "not found" in result["reason"]
    assert "frag-missing" in result["reason"]
    assert result["reason"] != _ABOVE_CEILING_REASON
    assert factory.calls == 1


def test_compile_above_ceiling_wins_over_missing_id(vault: Path) -> None:
    """A ceiling violation is reported ahead of any missing-id error.

    Deliberate ordering: if the not-found error won, a caller holding
    ``ceiling=open`` could pair one above-ceiling id with a batch of
    guesses and read back which of the guesses exist. The ceiling
    refusal reveals nothing about the rest of the request.
    """
    _write_fragment(vault, frag_id="frag-sealed", privacy_tier="intimate")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-sealed", "frag-missing"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert "not found" not in result["reason"]
    assert factory.calls == 0


def test_compile_reports_unknown_target_kind_even_with_above_ceiling_sources(
    vault: Path,
) -> None:
    """Cheap argument validation runs before the vault-walking gate.

    An unknown ``target_kind`` is rejected on its own terms even when
    the request also names an above-ceiling fragment, so a typo never
    costs a full fragment scan and the caller gets the actionable
    message.
    """
    _write_fragment(vault, frag_id="frag-sealed", privacy_tier="intimate")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-sealed"],
        target_kind="not-a-kind",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert "unknown target_kind" in result["reason"]
    assert result["reason"] != _ABOVE_CEILING_REASON
    assert factory.calls == 0


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
