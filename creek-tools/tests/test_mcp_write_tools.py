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

from creek.classify.llm.router import ModelRouter
from creek.classify.privacy_filter import PrivacyTierOverride
from creek.cli import _REPORT_DISPATCH
from creek.compile.engine import PARADOX_LOG_RELPATH
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier
from creek.save import TARGET_SUBDIRS
from creek.surface_modes import REPORT_TYPES
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
from creek_mcp.tools.report import (
    _MCP_REPORTS,
    _TIER_BLIND_GENERATORS,
    _generate_wavelength,
    _ReportRequest,
    report_tool,
)
from creek_mcp.tools.save import save_tool
from creek_mcp.tools.skills import skills_refresh_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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
    title: str | None = None,
    parent_id: str | None = None,
    structural_path: list[str] | None = None,
    child_ids: list[str] | None = None,
) -> None:
    """Write a minimal fragment under ``01-Fragments/Notes`` for compile tests.

    The three hierarchy kwargs are emitted **only when supplied**, so every
    pre-existing caller's frontmatter is byte-for-byte unchanged. They exist
    for the #931 ancestry tests, which need a child whose persisted
    ``structural_path`` carries an ancestor's heading while the ancestor
    itself is never named in the request.

    Args:
        vault: Vault root; the file lands under ``01-Fragments/Notes``.
        frag_id: Fragment id, also the filename stem.
        body: Markdown body beneath the frontmatter.
        privacy_tier: The ``privacy_tier`` frontmatter value.
        title: Fragment title; defaults to ``"Title <frag_id>"``.
        parent_id: Optional ``parent_id`` link up the hierarchy.
        structural_path: Optional persisted breadcrumb, as
            :func:`creek.atomize.split._build_children` writes it.
        child_ids: Optional ``child_ids`` links down the hierarchy.
    """
    metadata: dict[str, object] = {
        "type": "fragment",
        "id": frag_id,
        "title": title or f"Title {frag_id}",
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": privacy_tier,
        "eddies": [],
    }
    if parent_id is not None:
        metadata["parent_id"] = parent_id
    if structural_path is not None:
        metadata["structural_path"] = structural_path
    if child_ids is not None:
        metadata["child_ids"] = child_ids
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


def test_link_reaches_the_threads_linker(vault: Path) -> None:
    """``method="threads"`` runs the linker instead of refusing (#1252).

    The thread half of #880 — the fix for one thread swallowing 94% of a
    corpus — was unreachable over MCP because the tool carried a retyped copy
    of the CLI's method tuple that had lost ``"threads"``. An empty vault is
    enough to prove reachability: ``run_link`` short-circuits before touching
    an embedding model, so what is under test is the routing, not the linker.

    Args:
        vault: Vault fixture.
    """
    result = link_tool(
        vault_path=vault,
        method="threads",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok", result
    assert result["method"] == "threads"


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
        report_type="definitely-not-a-report",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unsupported report_type" in result["reason"]


def test_every_cli_report_type_has_an_mcp_branch() -> None:
    """``_MCP_REPORTS`` covers the declared surface exactly (#1253).

    The refusal path keys on :data:`creek.surface_modes.REPORT_TYPES`, so a
    name declared there without a branch here would raise ``KeyError`` across
    the MCP boundary, and a branch here for a name nobody declares is
    unreachable. Both are the drift class #1253 filed, one step further along.
    """
    assert set(_MCP_REPORTS) == set(REPORT_TYPES), (
        f"declared-but-unserved: {sorted(set(REPORT_TYPES) - set(_MCP_REPORTS))}; "
        f"served-but-undeclared: {sorted(set(_MCP_REPORTS) - set(REPORT_TYPES))}"
    )


def test_tier_blind_types_are_a_subset_of_the_declared_surface() -> None:
    """A tier-blind entry for a name nobody routes refuses nothing (#1253)."""
    assert set(_TIER_BLIND_GENERATORS) <= set(REPORT_TYPES), (
        f"not routed: {sorted(set(_TIER_BLIND_GENERATORS) - set(REPORT_TYPES))}"
    )


def test_wavelength_branch_writes_nothing_for_an_unresolvable_period(
    vault: Path,
) -> None:
    """The generator's own period guard holds when called directly.

    :func:`report_tool` refuses an unparseable period before dispatching, so
    this branch is defensive — and an untested defence is a defence nobody can
    rely on. Asserted here rather than through the tool for that reason.

    Args:
        vault: Vault fixture.
    """
    written = _generate_wavelength(
        _ReportRequest(vault_path=vault, override=PrivacyTierOverride.ALL, period=None),
    )
    assert written == []


@pytest.mark.parametrize("report_type", sorted({*_REPORT_DISPATCH, "wavelength"}))
def test_report_serves_every_cli_type_at_the_widest_ceiling(
    vault: Path,
    report_type: str,
) -> None:
    """Every type ``creek report`` routes runs to completion over MCP (#1253).

    ``ceiling=all`` is the ceiling ``creek report`` itself runs under, so this
    is the parity claim at its strongest: an MCP caller with the operator's own
    privileges can reach the whole report surface. Four of these types have no
    tier-filtered generator and are refused *below* this ceiling — that is the
    next test — but none of them may be missing from the surface entirely,
    which is the defect #1253 filed.

    Args:
        vault: Vault fixture.
        report_type: One CLI-routed report type.
    """
    (vault / "10-Liminal" / "Unnamed").mkdir(parents=True, exist_ok=True)
    result = report_tool(
        vault_path=vault,
        report_type=report_type,
        period="weekly",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "ok", result


@pytest.mark.parametrize(
    "report_type",
    ["unnamed", "fingerprint", "paradox", "synchronicity"],
)
def test_report_names_tier_blind_types_rather_than_hiding_them(
    vault: Path,
    report_type: str,
) -> None:
    """A type MCP cannot filter is refused *by name*, never omitted (#1253).

    These four generators take no ``PrivacyTierOverride``, so serving them
    below the widest ceiling would distil above-ceiling content into vault
    artifacts — the exact defect #968 closed for the other six. The refusal
    must say that, and must not read as "no such report type": pretending the
    type does not exist is the omission that produced #1253.

    Args:
        vault: Vault fixture.
        report_type: One tier-blind report type.
    """
    result = report_tool(
        vault_path=vault,
        report_type=report_type,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "unsupported report_type" not in str(result["reason"])
    assert "no tier-filtered generator" in str(result["reason"])


def test_report_wavelength_refuses_an_unparseable_period(vault: Path) -> None:
    """``wavelength`` needs a period, and says so rather than guessing."""
    result = report_tool(
        vault_path=vault,
        report_type="wavelength",
        period="last-tuesday",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "refused"
    assert "period" in str(result["reason"])


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
    """The ``decisions`` report now generates a real Decision note (#581).

    The fixture states ``privacy_tier: open`` explicitly since #968. It used to
    omit the key, and an omitted key now fails closed to ``intimate``
    (``creek.classify.privacy_filter.raw_privacy_tier``) rather than falling
    back to the model's ``unclassified`` default, so at ``ceiling=open`` the
    fragment would never reach the detector. The tier is fixture bookkeeping
    here — this test is about *whether a Decision note is written*, and the
    ceiling behaviour it now depends on is pinned by
    ``tests/test_mcp_report_tier_ceiling.py``.
    """
    frags = vault / "01-Fragments" / "Notes"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "frag-decide50.md").write_text(
        '---\ntype: fragment\nid: frag-decide50\ntitle: "Should I switch frameworks"\n'
        "privacy_tier: open\n"
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
    """The ``rhetorical-patterns`` report writes a per-register note (#582).

    ``privacy_tier: open`` is stated explicitly since #968 — see
    ``test_report_decisions_generates_note`` for why an omitted key no longer
    reaches an ``open``-ceiling report.
    """
    frags = vault / "01-Fragments" / "Notes"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "ex-1.md").write_text(
        '---\ntype: fragment\nid: ex-1\ntitle: "T"\n'
        "privacy_tier: open\n"
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
    """The ``mode-profiles`` report writes a per-mode note (#583).

    ``privacy_tier: open`` is stated explicitly since #968 — see
    ``test_report_decisions_generates_note`` for why an omitted key no longer
    reaches an ``open``-ceiling report.
    """
    frags = vault / "01-Fragments" / "Notes"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "m1.md").write_text(
        '---\ntype: fragment\nid: m1\ntitle: "Building momentum"\n'
        "privacy_tier: open\n"
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
    """The ``lexicon`` report now generates a real glossary (#580), not a stub.

    Called at ``ceiling=personal`` since #968, and the fixture's
    ``privacy_tier: personal`` is deliberately **not** downgraded to make it
    pass. The glossary quotes exemplar sentences verbatim, so a personal-tier
    exemplar reaching it at ``ceiling=open`` is precisely the leak #968 closed;
    a caller entitled to that exemplar is one that declares ``personal``. This
    test's subject is whether a glossary is produced at all, and the tier
    behaviour it now depends on is pinned by
    ``tests/test_mcp_report_tier_ceiling.py::test_report_at_open_ceiling_excludes_above_ceiling_content``
    (the ``lexicon`` row), which asserts the opposite outcome at ``open``.
    """
    _seed_voice_exemplar(
        vault,
        "ex-1",
        "The dharma teaches that the river flows; the river flows toward the sea.",
    )
    result = report_tool(
        vault_path=vault,
        report_type="lexicon",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
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


def _noop_llm_factory(tier: PrivacyTier) -> object:
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
    stub_factory = lambda tier: lambda _prompt: "{}"  # noqa: E731

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
        llm_factory=lambda tier: lambda _prompt: "{}",
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

    **Who invokes the factory moved in #962, and the stubs had to follow.**
    ``compile_tool`` no longer calls it; ``compile_to_vault`` does, from
    inside the engine, once it has loaded the fragments and can say how
    sensitive the call is. So a test that monkeypatches the engine away
    must have its stub honour that half of the contract, or ``calls``
    silently becomes a dead signal — zero for admitted *and* refused rows
    alike, which would make every ``assert factory.calls == 1`` below
    vacuous rather than failing loudly. The stubs therefore invoke
    ``llm_factory`` themselves. The *tier* they pass is immaterial to
    these tests, which count invocations rather than inspect tiers; see
    :class:`_TierRecordingLLMFactory` for the tests that pin the value.
    """

    def __init__(self) -> None:
        """Start with a zero invocation count."""
        self.calls = 0

    def __call__(self, tier: PrivacyTier) -> object:
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

    def _leaky_factory(tier: PrivacyTier) -> object:
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

    def _stub_compile(
        *,
        llm_factory: Callable[[PrivacyTier], object],
        **_kw: object,
    ) -> object:
        """Stand in for the engine: build the client, then write a page.

        The ``llm_factory`` call is load-bearing, not decorative. Since
        #962 the engine builds the compile client itself instead of
        receiving a pre-built one, so a stub that swallowed the factory
        would drive ``factory.calls`` to 0 on every row and the allowed-row
        assertion below would silently stop distinguishing "the gate
        passed" from "the wrapper short-circuited". The tier passed is the
        one the real engine derives from this row's single source fragment.
        """
        llm_factory(PrivacyTier(privacy_tier))
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


@pytest.mark.parametrize(
    ("ceiling", "allowed"),
    [
        (TierCeiling.OPEN, False),
        (TierCeiling.PERSONAL, True),
    ],
)
def test_compile_refuses_unclassified_source_at_open_ceiling(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: TierCeiling,
    allowed: bool,
) -> None:
    """An ``unclassified`` source needs a ``personal`` ceiling (#961).

    Inverted by #961. This test previously asserted that ``ceiling=open``
    *admitted* an explicitly-unclassified source, because
    ``creek_mcp.tier_ceiling._TIER_RANK`` ranked ``unclassified``
    alongside ``open`` (rank 0). It now ranks 1, with ``personal``,
    matching ``creek.classify.privacy_filter._TIER_RANK``: an untiered
    fragment is content nobody has vouched for, and every
    pipeline-written pre-classification fragment carries an *explicit*
    ``privacy_tier: unclassified`` (the ``Fragment`` model default), so
    the old ranking let an ``open``-ceiling caller compile a whole
    freshly-ingested vault. (The docstring here used to cite #923 for the
    ranking; that was a mis-citation — #923 is the separate bare
    ``_TIER_RANK`` subscript bug. #961 owns the policy.)

    Both halves are asserted, following
    :func:`test_compile_fails_closed_when_privacy_tier_key_is_absent`:
    the refused row pins the reason constant and a never-invoked factory,
    and the admitted row pins that ``personal`` still gets the work done,
    so a rank that overshot to ``intimate`` cannot pass.

    Args:
        vault: The seeded vault fixture.
        monkeypatch: Used to stub the compile engine on the admitted row.
        ceiling: The caller's declared ceiling.
        allowed: Whether *ceiling* must admit the unclassified source.
    """
    _write_fragment(vault, frag_id="frag-src", privacy_tier="unclassified")
    target_path = vault / "02-Threads" / "Active" / "thread-x.md"

    def _stub_compile(
        *,
        llm_factory: Callable[[PrivacyTier], object],
        **_kw: object,
    ) -> object:
        """Stand in for the engine: build the client, then write a page.

        Calls ``llm_factory`` because the engine does (#962); a stub that
        swallowed it would zero out ``factory.calls`` and quietly retire the
        admitted row's "the work really got done" assertion. The tier is the
        explicit ``unclassified`` this row's source carries.
        """
        llm_factory(PrivacyTier.UNCLASSIFIED)
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
    ``privacy_tier`` to ``unclassified``, which since #961 ranks 1 (with
    ``personal``) and so would be admitted at ``personal`` and above — so
    reading the tier off the model alone still fails **open** on exactly
    the hand-edited or legacy file whose tier nobody can vouch for. That
    is why ``fragment_tier`` consults the raw frontmatter rather than the
    validated model: only the raw dict can tell "the key is absent" from
    "the key says unclassified". The #961 ranking narrowed the blast
    radius of getting this wrong (``open`` no longer leaks) without
    removing the need for it — the ``PERSONAL`` row below is the one that
    would flip if the raw-frontmatter read were dropped.

    ``creek.reflect`` already refuses that file closed to ``intimate``
    (``creek_mcp.tools.reflect._fragment_tier``, #847), mirroring
    :func:`creek.classify.privacy_filter.tier_of`. Compile must agree:
    two MCP tools that disagree about the same file is the divergence
    the shared-loader design exists to prevent. Contrast
    :func:`test_compile_refuses_unclassified_source_at_open_ceiling`,
    where the key is present and explicitly ``unclassified`` — admitted
    at ``personal``, where this file is refused.
    """
    _write_fragment_without_tier(vault, frag_id="frag-legacy")
    target_path = vault / "02-Threads" / "Active" / "thread-x.md"

    def _stub_compile(
        *,
        llm_factory: Callable[[PrivacyTier], object],
        **_kw: object,
    ) -> object:
        """Stand in for the engine: build the client, then write a page.

        Calls ``llm_factory`` because the engine does (#962); a stub that
        swallowed it would zero out ``factory.calls`` and quietly retire the
        admitted rows' "the work really got done" assertion. The tier is the
        fail-closed ``INTIMATE`` the engine derives for a fragment whose
        ``privacy_tier`` key is absent — the very reading this test is about.
        """
        llm_factory(PrivacyTier.INTIMATE)
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

    The factory is never invoked (#962). It used to be built once, because
    ``llm_factory(tier)`` was evaluated as a call *argument* to
    ``compile_to_vault`` and Python therefore ran it before the engine
    could look anything up. The engine now calls the factory itself, after
    ``_load_fragments_for_compile`` has already raised for ``frag-missing``
    — so a request the engine cannot resolve builds no provider at all.
    That is strictly better than building one: a provider handshake is the
    last step before a real backend client exists, and a request that will
    be refused has no business reaching it.
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
    assert factory.calls == 0


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


# ---------------------------------------------------------------------------
# compile — unnamed-ancestor ceiling leak (issue #931)
# ---------------------------------------------------------------------------

_ANCESTOR_HEADING = "Ritual with M."
"""Heading of the above-ceiling ancestor, as the child's breadcrumb carries it.

Deliberately the *heading* rather than a synthetic marker:
:func:`creek.atomize.split._build_children` accumulates ancestor headings into
each child's persisted ``structural_path``, and for an intermediate ancestor the
heading *is* its title. That string is what
:func:`creek.hierarchy.structural_path_context` returns and
:func:`creek.compile.engine._build_prompt` renders after ``structural_path:``.
"""


def _seed_ancestry(vault: Path, *, ancestor_tier: str, child_tier: str) -> None:
    """Write an ancestor/child pair linked both by id and by persisted breadcrumb.

    The child alone is what the #931 tests name. Its ``structural_path``
    carries :data:`_ANCESTOR_HEADING`, so the leak is reachable through the
    *persisted* branch of :func:`creek.hierarchy.structural_path_context` —
    the branch that needs no ancestor fragment in memory and is therefore the
    one an MCP caller can reach.

    Args:
        vault: Vault root.
        ancestor_tier: ``privacy_tier`` for ``frag-ancestor``.
        child_tier: ``privacy_tier`` for ``frag-child``.
    """
    _write_fragment(
        vault,
        frag_id="frag-ancestor",
        privacy_tier=ancestor_tier,
        title=_ANCESTOR_HEADING,
        child_ids=["frag-child"],
    )
    _write_fragment(
        vault,
        frag_id="frag-child",
        privacy_tier=child_tier,
        title="On grief",
        parent_id="frag-ancestor",
        structural_path=[_ANCESTOR_HEADING],
    )


def test_compile_refuses_an_unnamed_above_ceiling_ancestor_at_the_open_ceiling(
    vault: Path,
) -> None:
    """An ``intimate`` ancestor refuses the call even though only its child is named.

    Issue #931, the narrowest ceiling. #848 ranks the ids the caller
    **names**; here the caller names one ``open`` child, so that gate admits
    it — and the engine then renders the child's persisted
    ``structural_path``, egressing the intimate ancestor's heading to a
    cloud-routed provider. Ranking ancestry closes the channel.

    ``factory.calls == 0`` is the load-bearing egress assertion; a
    ``status == "refused"`` on its own would be a false green (see
    :class:`_RecordingLLMFactory`). The refusal must also be
    *indistinguishable* from a named-id refusal — a separate reason string
    would be a fresh oracle telling the caller the offender is above them in
    the tree — hence the verbatim :data:`_ABOVE_CEILING_REASON` comparison.
    """
    _seed_ancestry(vault, ancestor_tier="intimate", child_tier="open")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-child"],
        target_kind="thread",
        target_id="thread-anc",
        target_title="Thread Anc",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert factory.calls == 0


def test_compile_ancestor_refusal_leaves_zero_state_and_one_clean_audit_row(
    vault: Path,
) -> None:
    """The ancestor refusal is audited exactly like a named-id refusal (#931).

    Same path, same payload shape, same absence of on-disk state: no
    compiled page, no hash marker, no paradox log, no target directory. The
    stub LLM returns a payload that *would* write all three, so each missing
    artefact is evidence the engine never ran.

    The audit row must not distinguish an ancestor violation from a named-id
    one either — no extra field, no ancestor id, no tier — so the assertions
    mirror ``test_compile_refusal_is_audited_without_ids_or_tiers`` exactly.
    """
    _seed_ancestry(vault, ancestor_tier="intimate", child_tier="open")
    payload = json.dumps(
        {
            "claims": [
                {"id": "c1", "text": "A claim.", "fragment_ids": ["frag-child"]}
            ],
            "paradoxes": [
                {"description": "A tension.", "fragment_ids": ["frag-child"]}
            ],
        },
    )

    def _leaky_factory(tier: PrivacyTier) -> object:
        """Return a stub LLM that would emit a page body and a paradox."""
        return lambda _prompt: payload

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-child"],
        target_kind="eddy",
        target_id="eddy-anc",
        target_title="Eddy Anc",
        llm_factory=_leaky_factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert not (vault / "03-Eddies" / "eddy-anc.md").exists()
    assert not (vault / PARADOX_LOG_RELPATH).exists()
    assert not (vault / "00-Creek-Meta" / "audit" / "compile-eddy-anc.hash").exists()

    entries = [e for e in _read_audit(vault) if e["tool"] == "creek.compile"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["args_summary"]["fragment_ids"] == {"count": 1}
    assert "affected_fragment_ids" not in entry
    assert "created_path" not in entry
    assert "created_tier" not in entry
    assert "target_title" not in entry["args_summary"]

    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert _ANCESTOR_HEADING not in raw
    assert "frag-ancestor" not in raw
    assert "intimate" not in raw
    verify_mcp_audit_chain(vault)


def test_compile_admits_a_within_ceiling_ancestor(vault: Path) -> None:
    """Ancestry ranking narrows; it must not refuse everything (anti-vacuity).

    Identical shape to the refusal above with the ancestor at ``open``: the
    call is admitted, the engine runs, and the breadcrumb reaches the prompt
    as designed. Without this row the #931 fix could be "refuse every
    fragment that has a parent" and the refusal tests would still pass.
    """
    _seed_ancestry(vault, ancestor_tier="open", child_tier="open")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-child"],
        target_kind="thread",
        target_id="thread-ok",
        target_title="Thread OK",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "ok"
    assert factory.calls == 1


def test_compile_refuses_a_dangling_ancestry_link(vault: Path) -> None:
    """A ``parent_id`` that resolves to nothing fails closed (#931).

    A missing, unreadable, non-``fragment``-typed or schema-invalid parent is
    invisible to :func:`creek.vault.reader.try_load_fragment`, so its tier is
    unknowable — and the child's breadcrumb still carries whatever that
    ancestor was called. Matching :func:`creek.classify.privacy_filter.fragment_tier`'s
    missing-key posture, an unsurveyable chain ranks ``INTIMATE``.
    """
    _write_fragment(
        vault,
        frag_id="frag-child",
        privacy_tier="open",
        title="On grief",
        parent_id="frag-vanished",
        structural_path=[_ANCESTOR_HEADING],
    )
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-child"],
        target_kind="thread",
        target_id="thread-dangle",
        target_title="Thread Dangle",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert factory.calls == 0


def test_compile_refuses_an_orphan_breadcrumb(vault: Path) -> None:
    """A breadcrumb with no ``parent_id`` to walk fails closed (#931).

    The persisted ``structural_path`` is a ``list[str]`` with no id binding,
    so a fragment carrying one while its ``parent_id`` is ``None`` —
    re-parented, parent deleted, hand-edited — has ancestry that can be
    *rendered* but not *ranked*. ``creek.atomize.split._build_children`` is
    the only writer of the field and always sets ``parent_id`` in the same
    ``model_copy``, so this state is anomalous by construction and ranks
    ``INTIMATE`` rather than sailing through.
    """
    _write_fragment(
        vault,
        frag_id="frag-orphan",
        privacy_tier="open",
        title="On grief",
        structural_path=[_ANCESTOR_HEADING],
    )
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-orphan"],
        target_kind="thread",
        target_id="thread-orphan",
        target_title="Thread Orphan",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON
    assert factory.calls == 0


def test_compile_ancestry_gate_still_loses_to_a_missing_id(vault: Path) -> None:
    """Ancestry ranking must not swallow the engine's not-found refusal (#931).

    The ordering #848 established is unchanged: an id that resolves to
    nothing contributes no tier to the survey, so a request naming only
    within-ceiling fragments plus a typo still reaches the engine and the
    caller still learns *which* id does not resolve.
    """
    _seed_ancestry(vault, ancestor_tier="open", child_tier="open")
    factory = _RecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-child", "frag-missing"],
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
# compile — tier-routed LLM factory (issues #928 / #929)
# ---------------------------------------------------------------------------


class _CompletionStub:
    """A provider completion exposing only the ``.text`` field callers read."""

    def __init__(self, text: str) -> None:
        """Store the completion *text*."""
        self.text = text


class _ProviderSpy:
    """A ``build_provider`` stand-in that records configs *and* prompts.

    It doubles as the provider it hands back, so one object captures both
    halves of the egress path: *which*
    :class:`~creek.config.LLMConfig` the router resolved (i.e. the
    ``Intimate``-never-cloud decision) and *what text* actually reached the
    backend. Asserting only the first would leave "the prompt is harmless
    anyway" unproven; asserting only the second would leave the routing
    unproven.
    """

    def __init__(self, *, available: bool = True, text: str = "{}") -> None:
        """Start with empty recordings, the given availability and response."""
        self.configs: list[LLMConfig] = []
        self.prompts: list[str] = []
        self.available = available
        self.text = text

    def build(self, config: LLMConfig) -> _ProviderSpy:
        """Record *config* and return this spy as the constructed provider."""
        self.configs.append(config)
        return self

    def complete(self, prompt: str) -> _CompletionStub:
        """Record *prompt* and return the canned completion."""
        self.prompts.append(prompt)
        return _CompletionStub(self.text)

    @property
    def provider_names(self) -> list[str]:
        """Return the ``provider`` of every recorded config, in call order."""
        return [config.provider for config in self.configs]


class _RoutingConfigStub:
    """A minimal ``CreekConfig`` stand-in carrying a *real* routing config.

    Exposes both ``llm`` (the raw :class:`~creek.config.LLMRoutingConfig`)
    and ``model_router`` (a real
    :class:`~creek.classify.llm.router.ModelRouter` over it), so the
    ``Intimate``-never-cloud decision exercised by these tests is the
    production one no matter which of the two the server's factory reads.
    Nothing about the tier gate is stubbed.
    """

    def __init__(self, *, default: str, generation: str) -> None:
        """Build the two-stage routing config and its router."""
        self.llm = LLMRoutingConfig(
            default=LLMConfig(provider=default),
            generation=LLMConfig(provider=generation),
        )
        self.model_router = ModelRouter(self.llm)


def _patch_build_provider(monkeypatch: pytest.MonkeyPatch, spy: _ProviderSpy) -> None:
    """Route every ``build_provider`` import path through *spy*.

    The provider factory is reachable under two names —
    ``creek.classify.llm.providers.build_provider`` (what the router and
    ``creek_mcp.server._build_reflect_llm_factory`` import) and the
    ``creek.classify.llm`` re-export (what
    :func:`creek.compile.engine.default_llm` imports). Both are patched so
    the recorded config is the one the production path actually used,
    whichever import it takes.
    """
    monkeypatch.setattr("creek.classify.llm.providers.build_provider", spy.build)
    monkeypatch.setattr("creek.classify.llm.build_provider", spy.build)


class _TierRecordingLLMFactory:
    """Compile LLM factory recording the tier it was keyed with, per call.

    Distinct from :class:`_RecordingLLMFactory`, which counts invocations to
    prove the #848 gate refused *before* the client was built. This one pins
    *what* the wrapper computed as the routing tier, which is the whole of
    issue #928: a factory invoked with the wrong tier egresses exactly as
    badly as one invoked when it should not have been.

    ``tiers`` is typed to admit ``None`` so a regression that passes no tier
    (or an explicit ``None``, which
    :meth:`~creek.classify.llm.router.ModelRouter.resolve` treats as "no tier
    gate") is caught by an assertion rather than silently ranking as
    non-intimate.
    """

    def __init__(self) -> None:
        """Start with no recorded tiers."""
        self.tiers: list[PrivacyTier | None] = []

    def __call__(self, tier: PrivacyTier) -> object:
        """Record *tier* and return a deterministic stub LLM."""
        self.tiers.append(tier)
        return lambda _prompt: "{}"


_INTIMATE_TITLE = "Sealed Confession Marker"
_INTIMATE_BODY = "The intimate body text that must never leave this machine."


def test_compile_routes_intimate_sources_to_the_local_provider(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed-tier compile: the real router forces the real prompt onto ollama.

    The flagship regression for #928 (compile bypasses ``ModelRouter``) and
    #929 (the production factory does not import). Nothing is stubbed at the
    seam under test: ``creek_mcp.server._build_compile_llm`` *is* the
    factory, a real :class:`~creek.classify.llm.router.ModelRouter` resolves
    the ``generation`` stage, and only ``build_provider`` is replaced — by a
    spy that records what it was handed.

    The ceiling is ``intimate``, so the #848 gate *admits* the intimate
    source: this is the live-egress case, not the refusal case.

    The prompt assertion is the point. ``creek.compile.engine._build_prompt``
    emits ``title:`` for every source unconditionally, and
    ``_fragment_excerpt_for_prompt`` redacts only the *body* — putting the
    title straight back into the body line as
    ``[Intimate-tier summary: <title>]``. An intimate fragment's title is
    therefore live egress payload. It may reach a local model; it must never
    reach the cloud one.
    """
    from creek_mcp import server as server_mod

    _write_fragment(vault, frag_id="frag-open", privacy_tier="open")
    _write_fragment(
        vault,
        frag_id="frag-int",
        body=_INTIMATE_BODY,
        privacy_tier="intimate",
        title=_INTIMATE_TITLE,
    )
    spy = _ProviderSpy()
    _patch_build_provider(monkeypatch, spy)
    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda: _RoutingConfigStub(default="ollama", generation="anthropic"),
    )

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-open", "frag-int"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=server_mod._build_compile_llm,
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )

    # (a) the real ``_enforce_local_for_intimate`` ran: the cloud
    # ``generation`` config was replaced by the local ``default``.
    assert spy.provider_names == ["ollama"]
    # (b) the payload that would have egressed is real, not hypothetical.
    assert len(spy.prompts) == 1
    assert _INTIMATE_TITLE in spy.prompts[0]
    assert _INTIMATE_BODY not in spy.prompts[0]
    # (c) routing correctly does not break the ordinary success path.
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    ("sources", "ceiling", "expected_tier"),
    [
        (
            (("frag-open", "open"),),
            TierCeiling.OPEN,
            PrivacyTier.OPEN,
        ),
        (
            (("frag-open", "open"), ("frag-pers", "personal")),
            TierCeiling.PERSONAL,
            PrivacyTier.PERSONAL,
        ),
        (
            (("frag-open", "open"),),
            TierCeiling.INTIMATE,
            PrivacyTier.INTIMATE,
        ),
        (
            (("frag-open", "open"), ("frag-int", "intimate")),
            TierCeiling.INTIMATE,
            PrivacyTier.INTIMATE,
        ),
        (
            (("frag-open", "open"),),
            TierCeiling.ALL,
            PrivacyTier.INTIMATE,
        ),
        (
            (("frag-open", "open"), ("frag-pers", "personal")),
            TierCeiling.ALL,
            PrivacyTier.INTIMATE,
        ),
    ],
    ids=[
        "open-ceiling-open-source",
        "personal-ceiling-personal-source",
        "intimate-ceiling-open-sources-only",
        "intimate-ceiling-intimate-source",
        "all-ceiling-open-sources-only",
        "all-ceiling-personal-source",
    ],
)
def test_compile_keys_the_llm_factory_with_the_routing_tier(
    vault: Path,
    sources: tuple[tuple[str, str], ...],
    ceiling: TierCeiling,
    expected_tier: PrivacyTier,
) -> None:
    """The factory is keyed with the more sensitive of ceiling and sources.

    Pins the routing table for #928. Two rows carry the defense-in-depth
    argument that mirrors ``creek_mcp.tools.reflect._routing_tier``: an
    ``intimate`` ceiling over nothing but ``open`` sources, and an ``all``
    ceiling over the same, both route ``INTIMATE`` — because the ceiling the
    caller declared is itself a statement about what the call is permitted
    to reach, and ``all`` admits intimate content by definition.

    The factory must be invoked exactly once (a second build is a second
    provider handshake, and on a re-resolve could pick a different backend)
    and never with ``None``, which
    :meth:`~creek.classify.llm.router.ModelRouter.resolve` treats as "apply
    no tier gate at all" — the precise shape of the #928 bug.
    """
    for frag_id, privacy_tier in sources:
        _write_fragment(vault, frag_id=frag_id, privacy_tier=privacy_tier)
    factory = _TierRecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=[frag_id for frag_id, _ in sources],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=ceiling,
    )

    assert result["status"] == "ok"
    assert factory.tiers == [expected_tier]
    assert None not in factory.tiers


def test_compile_routing_fails_closed_when_no_source_id_resolves(
    vault: Path,
) -> None:
    """An unresolvable request reaches no model at all and still says "not found".

    Two halves, and both matter. The routing half's original premise is
    **superseded by #962**, in the safe direction. It used to be the one row
    in the #928 table where the *source* term strictly dominated the ceiling
    term: with nothing found in the vault the max-source tier failed closed
    to ``INTIMATE`` even under ``ceiling=open``, so the call was routed
    local. Now the factory is invoked by the engine, *after* the load — and
    the load raises for an id that does not resolve — so a zero-resolving
    request never reaches a model at all. Routing something local is a
    guarantee about where it goes; building nothing is the strictly stronger
    guarantee that there was no "it".

    The fail-closed ``INTIMATE`` default itself is untouched and still
    exercised: an *empty* ``fragment_ids`` loads nothing without raising, and
    ``tests/test_compile.py``'s
    ``test_compile_to_vault_fails_closed_when_no_fragments_are_requested``
    pins that path keying the factory ``INTIMATE`` rather than ``OPEN``. What
    changed here is only which of the two refusals gets there first.

    The UX half guards the fix's blast radius: routing local must not turn
    the engine's ordinary not-found refusal into a generic one. A caller
    with a typo'd id still learns which id does not resolve — and learns it
    from the engine, not from the ceiling gate (hence the explicit
    inequality against :data:`_ABOVE_CEILING_REASON`).
    """
    factory = _TierRecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-missing"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert factory.tiers == []
    assert result["status"] == "refused"
    assert "not found" in result["reason"]
    assert "frag-missing" in result["reason"]
    assert result["reason"] != _ABOVE_CEILING_REASON


def test_compile_routing_fails_closed_when_privacy_tier_key_is_absent(
    vault: Path,
) -> None:
    """A fragment with no ``privacy_tier`` key routes as ``INTIMATE``.

    The routing counterpart to
    :func:`test_compile_fails_closed_when_privacy_tier_key_is_absent`, which
    covers the *admission* side of the same file. ``ceiling=all`` admits the
    hand-edited / legacy fragment, so the routing tier is the only thing
    standing between its title and a cloud provider;
    ``creek.classify.privacy_filter.fragment_tier`` reports ``INTIMATE`` for it and the
    factory must be keyed with exactly that.
    """
    _write_fragment_without_tier(vault, frag_id="frag-legacy")
    factory = _TierRecordingLLMFactory()

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-legacy"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.ALL,
    )

    assert result["status"] == "ok"
    assert factory.tiers == [PrivacyTier.INTIMATE]


def test_compile_refuses_when_intimate_has_no_local_backend(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``IntimateRoutingError`` surfaces as a refusal, not a transport crash.

    When *both* ``default`` and ``generation`` are cloud there is nowhere
    safe to send intimate content, and the router raises rather than
    egressing. ``IntimateRoutingError`` subclasses ``RuntimeError``, which
    ``compile_tool`` already catches, so the caller must get the ordinary
    structured refusal.

    Everything except the routing decision is wired to *succeed* — the
    provider spy reports ``available`` and returns parseable JSON — so a
    refusal here can only have come from the router. The reason string is
    pinned to the router's own wording because a provider-unavailability
    ``RuntimeError`` also becomes ``status="refused"``, and a bare status
    assertion could not tell the two apart.
    """
    from creek_mcp import server as server_mod

    _write_fragment(vault, frag_id="frag-int", privacy_tier="intimate")
    spy = _ProviderSpy()
    _patch_build_provider(monkeypatch, spy)
    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda: _RoutingConfigStub(default="anthropic", generation="anthropic"),
    )

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-int"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=server_mod._build_compile_llm,
        privacy_tier_ceiling=TierCeiling.ALL,
    )

    assert result["status"] == "refused"
    assert "cannot route to cloud provider" in result["reason"]
    assert "anthropic" in result["reason"]
    # No provider was ever constructed, so nothing could have egressed.
    assert spy.configs == []
    assert spy.prompts == []
    # A refused call leaves no page and no idempotency state behind.
    assert not (vault / "02-Threads" / "Active" / "thread-x.md").exists()
    assert not (vault / "00-Creek-Meta" / "audit" / "compile-thread-x.hash").exists()


def test_compile_not_found_refusal_no_longer_depends_on_model_config(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-resolving compile says "not found" under any model config (#962).

    **The seam this test documented is now closed.** It previously pinned a
    real deployment-dependent quirk of ``_survey_sources``'s contract:
    ``llm_factory(tier)`` was evaluated as a call *argument* to
    ``compile_to_vault``, so Python ran it before the engine could report
    "Fragment(s) not found". With no requested id resolving, the fail-closed
    ``INTIMATE`` tier made an all-cloud router refuse first, and its message
    — not the not-found one — reached the caller. That was documented rather
    than fixed, on the grounds that it failed closed and named no id.

    #962 removes the argument-evaluation seam entirely: ``compile_to_vault``
    now takes a ``llm_factory`` and invokes it *itself*, after
    ``_load_fragments_for_compile`` has already raised for the unresolvable
    id. So the ordering no longer turns on when Python evaluates an
    argument, and every assertion below is the inverse of the one it
    replaces. The two calls at the end are what "config-independent" means,
    asserted rather than merely claimed.

    The residual guarantees are unchanged and still pinned: nothing is
    built, nothing egresses, and a legitimate caller with a typo'd id still
    learns *which* id does not resolve — from the engine, not from the
    ceiling gate, whose deliberately content-free
    :data:`_ABOVE_CEILING_REASON` names nothing. This now agrees with
    :func:`test_compile_routing_fails_closed_when_no_source_id_resolves` on
    every config rather than only on local-default ones.
    """
    from creek_mcp import server as server_mod

    spy = _ProviderSpy()
    _patch_build_provider(monkeypatch, spy)
    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda: _RoutingConfigStub(default="anthropic", generation="anthropic"),
    )

    result = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-does-not-exist"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=server_mod._build_compile_llm,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result["status"] == "refused"
    # Inverted: the routing refusal no longer preempts the engine's.
    assert "cannot route to cloud provider" not in result["reason"]
    assert "not found" in result["reason"]
    # Inverted: the caller does learn which id failed to resolve.
    assert "frag-does-not-exist" in result["reason"]
    assert result["reason"] != _ABOVE_CEILING_REASON
    # Unchanged: nothing was ever built, so nothing could have egressed.
    assert spy.configs == []
    assert spy.prompts == []

    # Config-independence, asserted rather than described: the same call on
    # a config with a *local* default — where the router would never have
    # refused at all — produces the byte-identical refusal. The message now
    # turns on the vault's contents, not on the operator's model choice.
    monkeypatch.setattr(
        server_mod,
        "load_config",
        lambda: _RoutingConfigStub(default="ollama", generation="anthropic"),
    )
    with_local_default = compile_tool(
        vault_path=vault,
        fragment_ids=["frag-does-not-exist"],
        target_kind="thread",
        target_id="thread-x",
        target_title="Thread X",
        llm_factory=server_mod._build_compile_llm,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert with_local_default["reason"] == result["reason"]
    assert with_local_default["status"] == "refused"
    assert spy.configs == []
    assert spy.prompts == []


def test_compile_above_ceiling_refusal_gains_no_tier_field(vault: Path) -> None:
    """Routing must not add a tier-derived field to the refusal or its audit row.

    Anti-oracle guard for #928. The above-ceiling refusal deliberately names
    nothing — see :data:`_ABOVE_CEILING_REASON` — and the computed routing
    tier is exactly the kind of field a well-meaning implementation would
    add "for debuggability". Echoing it would turn one refused call into the
    bulk tier-classification oracle that constant exists to prevent, so the
    key sets are pinned exactly rather than merely spot-checked.
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

    assert set(result) == {"status", "tool", "tier_ceiling", "reason"}
    assert result["status"] == "refused"
    assert result["reason"] == _ABOVE_CEILING_REASON

    entries = [e for e in _read_audit(vault) if e["tool"] == "creek.compile"]
    assert len(entries) == 1
    entry = entries[0]
    assert {key for key in entry if "tier" in key} == {"tier_ceiling"}
    args_summary = entry["args_summary"]
    assert isinstance(args_summary, dict)
    assert {key for key in args_summary if "tier" in key} == set()


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


# ---------------------------------------------------------------------------
# link — the cluster-health counts the CLI renders and MCP dropped (#1372)
# ---------------------------------------------------------------------------


class TestLinkAdvisoryFields:
    """``creek.link`` must report cluster health, not just totals.

    The tool returned ``method``/``fragment_count``/``link_count`` and
    nothing else, so the three counts ``creek link`` prints on the console
    stopped at the MCP boundary. The empty-vault tests above cannot see
    this: every one of these fields is ``0`` on an empty vault, which is
    also what a payload that never carried them looks like.
    """

    @staticmethod
    def _link_with_summary(
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, object]:
        """Run ``link_tool`` over a stubbed summary with degenerate clustering.

        The linker itself is not under test — reproducing a 521-fragment
        discard from real embeddings would need a corpus and a model — so
        ``run_link`` is replaced at the tool's own import site and the
        summary is handed over verbatim. What is under test is the
        translation from :class:`~creek.link.link_engine.LinkSummary` to
        the response dict.

        Args:
            vault: Vault fixture.
            monkeypatch: Pytest monkeypatch fixture.

        Returns:
            The tool's response payload.
        """
        from creek.link.link_engine import LinkSummary

        summary = LinkSummary(
            method="eddies",
            fragment_count=1200,
            link_count=18,
            eddies_detected=18,
            eddies_written=18,
            member_fragments_updated=679,
            largest_cluster_fragments=12,
            clusters_split=6,
            oversized_discarded=521,
        )

        def _fake_run_link(**_kwargs: object) -> LinkSummary:
            """Return the fixed summary, ignoring what the tool asked for."""
            return summary

        monkeypatch.setattr("creek_mcp.tools.link.run_link", _fake_run_link)
        return link_tool(
            vault_path=vault,
            method="eddies",
            privacy_tier_ceiling=TierCeiling.OPEN,
        )

    def test_link_reports_the_fragments_it_discarded(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All three cluster-health counts reach the caller, with their values.

        ``creek/cli.py:2833-2841`` renders exactly these on the CLI
        console, and a discard is data loss — those fragments carry no
        wiki-link at all — so an MCP caller that cannot see it believes
        the link pass succeeded when 521 fragments were dropped to noise.

        Args:
            vault: Vault fixture.
            monkeypatch: Pytest monkeypatch fixture.
        """
        result = self._link_with_summary(vault, monkeypatch)

        assert result["status"] == "ok", result
        assert result["largest_cluster_fragments"] == 12
        assert result["clusters_split"] == 6
        assert result["oversized_discarded"] == 521

    def test_link_counts_are_plain_integers_not_content(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """These three need no ceiling gate, and their type is the reason.

        They come off a frozen dataclass of counts and can never name a
        fragment, quote one, or vary with a caller's admission — unlike
        the ingest advisories, whose text interpolates real vault ids and
        therefore crosses the boundary only in a content-free form.

        Args:
            vault: Vault fixture.
            monkeypatch: Pytest monkeypatch fixture.
        """
        result = self._link_with_summary(vault, monkeypatch)

        for key in (
            "largest_cluster_fragments",
            "clusters_split",
            "oversized_discarded",
        ):
            assert isinstance(result[key], int), key
