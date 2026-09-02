"""Per-tool integration tests for the MCP wrappers (FEAT-010)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.models import PrivacyTier
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.author import author_tool
from creek_mcp.tools.draft import draft_tool
from creek_mcp.tools.lint import lint_tool
from creek_mcp.tools.mine import mine_tool
from creek_mcp.tools.state import state_render_tool
from creek_mcp.tools.state_read import state_read_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
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
    """``state.read`` reads ``00-Creek-Meta/State/latest.md`` verbatim.

    The fixture carries the #969 ``privacy_tier: open`` stamp that
    ``StateReportGenerator.write`` now writes. Before #969 an unstamped file
    was served at every ceiling; now an *unstamped* report reads as
    ``intimate`` (``raw_privacy_tier`` fails closed on a missing key), which is
    an accurate statement about bytes rendered completely unfiltered. Stamping
    the fixture keeps this test pinning what it always pinned — that admitted
    content comes back verbatim — rather than accidentally becoming a second,
    weaker copy of the refusal test below.
    """
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "---\ntype: state-report\nprivacy_tier: open\ntier_ceiling: open\n---\n\n"
        "# Audit report\n\nVault summary lives here.\n",
        encoding="utf-8",
    )
    result = state_read_tool(vault_path=vault)
    assert result["status"] == "ok"
    assert result["tool"] == "creek.state.read"
    assert result["tier_ceiling"] == "open"
    assert "Audit report" in result["content"]


def test_state_read_refuses_a_legacy_unstamped_report(vault: Path) -> None:
    """An unstamped ``latest.md`` fails closed at the default ceiling (#969).

    Added alongside — not instead of — the verbatim-read test above, because
    the two halves have to be pinned together: "stamped ``open`` is served"
    means nothing if "unstamped is also served".

    Every ``latest.md`` written before #969 was rendered with no ceiling at
    all, i.e. the equivalent of ``--include-tier all``. Refusing it states a
    fact about those bytes rather than a precaution about them, and recovery is
    one command: ``creek.state.render`` (or ``creek state --include-tier
    open``) re-renders and re-stamps at the caller's ceiling.
    """
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit report\n\nVault summary lives here.\n",
        encoding="utf-8",
    )
    result = state_read_tool(vault_path=vault)
    assert result["status"] == "refused"
    assert "Vault summary lives here." not in json.dumps(result, default=str)


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


def _write_liminal_unnamed(vault: Path, *, stem: str, privacy_tier: str) -> None:
    """Write a ``10-Liminal/Unnamed`` note whose file *stem* is the sentinel.

    The Liminal Watch section renders ``path.stem`` and nothing else, so the
    stem is the one place a sentinel can prove the rendered report reached
    above-ceiling content. Used by the test below to get a canary into
    ``latest.md`` through the production render path.

    Args:
        vault: Vault root.
        stem: File stem, carrying the sentinel.
        privacy_tier: The note's declared tier.
    """
    target = vault / "10-Liminal" / "Unnamed" / f"{stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "type": "fragment",
        "id": stem,
        "title": stem,
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "unclassified", "secondary": []},
        "privacy_tier": privacy_tier,
    }
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="unnamed body", **metadata)),
        encoding="utf-8",
    )


def test_state_read_does_not_serve_above_ceiling_content_from_latest_md(
    vault: Path,
) -> None:
    """``state.read`` refuses a report whose stamp is above the caller's ceiling.

    **What this replaced, and why the old test could not fail.** The previous
    ``test_state_read_does_not_embed_fragment_bodies`` wrote an intimate
    fragment, then **hand-wrote** ``latest.md`` as the fixed string
    ``"# Audit report\\n\\n- Fragments: 1\\n"``, then asserted
    ``"secret journal body" not in result["content"]``. That was true by
    construction: nothing in ``state_read_tool`` ever read the fragment, so the
    ``_write_fragment`` call was decorative and the assertion held whether or
    not a gate existed. It passed for the entire life of the #969 gap.

    **Why this one can fail.** The artifact is produced through the production
    path — ``state_render_tool(..., ceiling=ALL)`` — so the canary genuinely
    reaches ``latest.md``, which is asserted first so the setup cannot rot into
    the same vacuity. Delete the read gate and this test fails, because the
    canary comes back. Delete the render gate and its companion in
    ``tests/test_state_tier_ceiling.py`` fails, because the ``open`` render
    would carry it.

    The refusal, not merely the canary's absence, is what is asserted: a change
    that stopped resolving ``latest.md`` would return ``status="empty"`` with
    no canary in it and prove nothing about the ceiling.
    """
    canary = "CANARY-STATE-READ-INTIMATE-4d16"
    _write_fragment(
        vault,
        frag_id="frag-intimate-1",
        title="Private moment",
        privacy_tier="intimate",
        body="this is a secret journal body",
    )
    _write_liminal_unnamed(vault, stem=f"unnamed-{canary}", privacy_tier="intimate")

    state_render_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.ALL)
    latest = (vault / "00-Creek-Meta" / "State" / "latest.md").read_text(
        encoding="utf-8",
    )
    assert canary in latest, (
        "the ceiling=all render did not put the canary into latest.md, so the "
        "refusal assertion below would pass on an artifact with nothing in it "
        "to refuse — exactly the vacuity this test replaced"
    )

    result = state_read_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.OPEN)
    assert result["status"] == "refused"
    assert canary not in json.dumps(result, default=str), (
        "creek.state.read served above-ceiling content from latest.md at "
        f"privacy_tier_ceiling=open:\n\n{result}"
    )


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
        llm_factory=lambda tier: lambda prompt: "ignored",
        phase="rising",
    )
    assert result["status"] == "empty"
    assert result["reason"] == "no idea seeds surfaced"


def test_draft_writes_audit_entry_for_empty_seed_path(vault: Path) -> None:
    """Audit happens even when the draft cannot proceed (empty seeds)."""
    draft_tool(
        vault_path=vault,
        llm_factory=lambda tier: lambda prompt: "ignored",
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
        llm_factory=lambda tier: lambda prompt: "drafted",
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
        llm_factory=lambda tier: lambda prompt: "",
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
        llm_factory=lambda tier: lambda prompt: "x",
        phase="rising",
        index=99,
    )
    assert result["status"] == "refused"
    assert result["tool"] == "creek.draft"
    assert "out of range" in result["reason"]


# ---------------------------------------------------------------------------
# draft — LLM routing tier (#958)
# ---------------------------------------------------------------------------


class _TierRecordingFactory:
    """A ``draft_tool`` ``llm_factory`` recording the routing tier it was handed.

    Doubles as the LLM callable it returns, so one object captures both the
    tier the tool derived (local-vs-cloud routing) and the prompt text that
    actually reached the model.
    """

    def __init__(self, body: str = "drafted") -> None:
        """Start with empty recordings and the canned completion *body*."""
        self.tiers: list[PrivacyTier] = []
        self.prompts: list[str] = []
        self._body = body

    def __call__(self, tier: PrivacyTier) -> Callable[[str], str]:
        """Record *tier* and return the recording LLM callable."""
        self.tiers.append(tier)
        return self._complete

    def _complete(self, prompt: str) -> str:
        """Record *prompt* and return the canned body."""
        self.prompts.append(prompt)
        return self._body


def _seed_with_sources(source_fragments: tuple[str, ...]) -> object:
    """Return an ``IdeaSeed`` naming *source_fragments* as its only sources."""
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="Routing seed",
        source_fragments=source_fragments,
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="brief",
        score=0.5,
    )


def _unexplored_ontology_seed() -> object:
    """Return the real ``UNEXPLORED_ONTOLOGY`` seed shape: no vault content.

    ``creek/generate/mining.py::_seed_from_ontology_tuple`` (line 1511) builds
    the title and description purely from ontology enum labels and leaves
    ``source_fragments`` / ``threads`` / ``eddies`` empty (line 1529), so no
    fragment, thread, or eddy text reaches the prompt at all.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.UNEXPLORED_ONTOLOGY,
        title="Unexplored: F1 / rising / structural / plain / micro",
        source_fragments=(),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="a corner you have never inhabited",
        score=1.0,
    )


def _patch_miner(monkeypatch: pytest.MonkeyPatch, seed: object) -> None:
    """Force ``draft_tool``'s ``IdeaMiner`` to surface exactly *seed*."""
    monkeypatch.setattr(
        "creek_mcp.tools.draft.IdeaMiner",
        lambda **kwargs: type(
            "_M",
            (),
            {"mine_all": lambda self, vault_path, *, current_phase: [seed]},
        )(),
    )


def _patch_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``DraftGenerator`` so routing tests never touch the skill tree.

    The stub still *calls* the llm it was handed once, so a factory that was
    invoked but whose result was dropped on the floor is still distinguishable
    from one that was genuinely wired into generation.
    """
    from creek.generate.drafts import Draft

    class _Generator:
        """Minimal generator returning a fixed draft and saving a stub file."""

        def __init__(self, *, llm: Callable[[str], str], **kwargs: object) -> None:
            """Record the llm callable; other generator options are ignored."""
            del kwargs
            self._llm = llm

        def generate_draft(self, idea: object, *, vault_path: Path) -> Draft:
            """Call the llm once and return a fixed draft."""
            del idea, vault_path
            self._llm("stub prompt")
            return Draft(
                title="Routing seed",
                body="drafted",
                idea_strategy="thread_terminus",
                source_fragments=(),
                threads=(),
                eddies=(),
                skill_stack=(),
                prompt="stub prompt",
                generated_date=datetime(2026, 5, 11, tzinfo=UTC),
            )

        def save_draft(self, draft: Draft, vault_path: Path) -> Path:
            """Write a placeholder draft file and return its path."""
            del draft
            target = vault_path / "07-Voice" / "Drafts" / "2026-05-11-routing.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("body", encoding="utf-8")
            return target

    monkeypatch.setattr("creek_mcp.tools.draft.DraftGenerator", _Generator)


def test_draft_routes_personal_sources_above_the_open_ceiling(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``personal`` source outranks an ``open`` ceiling for LLM routing.

    ``creek/generate/drafts.py::_render_fragment_section`` (line 2604) emits
    ``### {fid}: {fragment.title}`` unconditionally, and at an ``open``
    ceiling ``filter_fragments_by_tier`` replaces a personal body with
    ``[Personal-tier summary: {title}]`` rather than dropping the fragment —
    so the personal fragment's id *and* title reach the prompt even though its
    body was redacted. The ceiling-derived tier alone therefore understates
    what reaches the model: an implementation that routed on
    ``routing_tier(ceiling, None)`` would hand personal titles to the cloud
    ``generation`` stage on the caller's say-so. The tier must be the more
    sensitive of the ceiling and the sources' own classifications.
    """
    _write_fragment(
        vault,
        frag_id="frag-personal",
        title="Personal note",
        privacy_tier="personal",
    )
    _patch_miner(monkeypatch, _seed_with_sources(("frag-personal",)))
    _patch_generator(monkeypatch)
    factory = _TierRecordingFactory()

    result = draft_tool(
        vault_path=vault,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
        phase="rising",
    )

    assert result["status"] == "ok"
    assert factory.tiers == [PrivacyTier.PERSONAL]
    assert factory.prompts == ["stub prompt"]


def test_draft_prompt_carries_the_personal_title_at_an_open_ceiling(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload check behind the routing rule: the real prompt carries it.

    The sibling routing test asserts what ``draft_tool`` *intends*; this one
    drives the **real** :class:`~creek.generate.drafts.DraftGenerator` and
    asserts on the bytes that actually reach the model. Under an ``open``
    ceiling the personal fragment is not dropped — its body is swapped for
    ``[Personal-tier summary: {title}]`` and rendered under a
    ``### {id}: {title}`` heading — so the personal fragment's title is in the
    prompt string verbatim. Without this, the routing rule rests on a reading
    of the code rather than on its output.
    """
    _write_fragment(
        vault,
        frag_id="frag-personal",
        title="Personal note about my marriage",
        privacy_tier="personal",
        body="the private body that must not appear",
    )
    _patch_miner(monkeypatch, _seed_with_sources(("frag-personal",)))
    skills_root = vault / "empty-skills"
    skills_root.mkdir()
    factory = _TierRecordingFactory(body="drafted body")

    result = draft_tool(
        vault_path=vault,
        llm_factory=factory,
        skills_root=skills_root,
        privacy_tier_ceiling=TierCeiling.OPEN,
        phase="rising",
    )

    assert result["status"] == "ok"
    assert len(factory.prompts) == 1
    prompt = factory.prompts[0]
    assert "Personal note about my marriage" in prompt
    assert "[Personal-tier summary: " in prompt
    assert "the private body that must not appear" not in prompt
    assert factory.tiers == [PrivacyTier.PERSONAL]


def test_draft_routes_intimate_sources_local_under_an_all_ceiling(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``intimate`` source routes INTIMATE, which the router forces local.

    ``ALL`` already maps to ``INTIMATE`` via ``CEILING_ROUTING_TIER``, so this
    pins the whole chain end to end: the tool must key the factory with a
    :class:`~creek.models.PrivacyTier` at all (the pre-fix factory takes no
    tier), and it must be the intimate one so
    :class:`~creek.classify.llm.router.ModelRouter` redirects the cloud
    ``generation`` stage onto the local ``default`` — or refuses.
    """
    _write_fragment(
        vault,
        frag_id="frag-intimate",
        title="Intimate note",
        privacy_tier="intimate",
    )
    _patch_miner(monkeypatch, _seed_with_sources(("frag-intimate",)))
    _patch_generator(monkeypatch)
    factory = _TierRecordingFactory()

    result = draft_tool(
        vault_path=vault,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.ALL,
        phase="rising",
    )

    assert result["status"] == "ok"
    assert factory.tiers == [PrivacyTier.INTIMATE]


def test_draft_fails_closed_when_no_named_source_resolves(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed whose named sources do not resolve routes INTIMATE, not OPEN.

    Mirrors ``compile``'s ``max(..., default=PrivacyTier.INTIMATE)``: with no
    evidence about what the call would carry, the safe assumption is the worst
    one. The vault deliberately holds an unrelated ``open`` fragment, so an
    implementation that took the maximum over *every* fragment in the vault
    (rather than over the seed's named sources) would answer ``OPEN`` here and
    fail — as would one that treated "no tiers found" as tier zero.
    """
    _write_fragment(
        vault,
        frag_id="frag-other",
        title="Unrelated",
        privacy_tier="open",
    )
    _patch_miner(monkeypatch, _seed_with_sources(("frag-missing",)))
    _patch_generator(monkeypatch)
    factory = _TierRecordingFactory()

    result = draft_tool(
        vault_path=vault,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
        phase="rising",
    )

    assert result["status"] == "ok"
    assert factory.tiers == [PrivacyTier.INTIMATE]


def test_draft_routes_a_sourceless_seed_by_the_ceiling_alone(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``UNEXPLORED_ONTOLOGY`` seed routes by its ceiling, not fail-closed.

    ``source_fragments == ()`` is not "no tiers resolved" — it is "there is no
    vault content in this prompt at all". ``mining.py::_seed_from_ontology_tuple``
    builds that seed's title and description purely from ontology enum labels,
    so forcing INTIMATE here would push every unexplored-ontology draft onto
    the local model for no privacy gain whatsoever. The distinction is exactly
    the ``content_tier is None`` branch of
    :func:`creek_mcp.tier_ceiling.routing_tier`, and it is the one case where
    the ``no ids resolved`` fail-closed rule must *not* fire.

    The vault holds an intimate fragment the seed does not name, so an
    implementation that scanned the vault instead of the seed's sources would
    answer INTIMATE and fail.
    """
    _write_fragment(
        vault,
        frag_id="frag-intimate",
        title="Intimate note",
        privacy_tier="intimate",
    )
    _patch_miner(monkeypatch, _unexplored_ontology_seed())
    _patch_generator(monkeypatch)
    factory = _TierRecordingFactory()

    result = draft_tool(
        vault_path=vault,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
        phase="rising",
    )

    assert result["status"] == "ok"
    assert factory.tiers == [PrivacyTier.OPEN]


def test_draft_fails_closed_when_a_source_fragment_has_no_privacy_tier(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source whose frontmatter omits ``privacy_tier`` routes INTIMATE.

    :class:`~creek.models.Fragment` defaults a *missing* ``privacy_tier`` to
    ``unclassified``, which since #961 ranks alongside ``personal`` — so
    reading the tier off the validated model alone would route the file nobody
    can vouch for (a hand-edited or legacy fragment) as ``personal``, i.e.
    cloud-eligible, rather than local-only. The raw frontmatter is the only
    place the two cases are still distinguishable, which is why the shared
    ``fragment_tier`` consults it; draft must agree with compile rather than
    route the same file two different ways.
    """
    metadata = {
        "type": "fragment",
        "id": "frag-untiered",
        "title": "Untiered note",
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "eddies": [],
    }
    (vault / "01-Fragments" / "Notes" / "frag-untiered.md").write_text(
        frontmatter.dumps(frontmatter.Post(content="body text", **metadata)),
        encoding="utf-8",
    )
    _patch_miner(monkeypatch, _seed_with_sources(("frag-untiered",)))
    _patch_generator(monkeypatch)
    factory = _TierRecordingFactory()

    result = draft_tool(
        vault_path=vault,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.OPEN,
        phase="rising",
    )

    assert result["status"] == "ok"
    assert factory.tiers == [PrivacyTier.INTIMATE]


def test_draft_refuses_when_the_router_has_no_local_backend(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``IntimateRoutingError`` becomes a refusal, not an MCP transport error.

    The router raises it — a ``RuntimeError`` subclass — when intimate content
    hits a cloud stage and ``llm.default`` is also cloud, so there is no local
    backend to redirect to. That must cross the MCP boundary as the same
    structured ``refused`` envelope every other draft failure uses; letting it
    propagate turns an operator misconfiguration into an unhandled exception
    for the client. The reason must also name no fragment id: the caller
    learns the provider is misconfigured, not which of their fragments is
    intimate.
    """
    from creek.classify.llm.router import IntimateRoutingError

    _write_fragment(
        vault,
        frag_id="frag-intimate",
        title="Intimate note",
        privacy_tier="intimate",
    )
    _patch_miner(monkeypatch, _seed_with_sources(("frag-intimate",)))
    _patch_generator(monkeypatch)

    def _explode(tier: PrivacyTier) -> Callable[[str], str]:
        """Refuse to build a provider, as the router does with no local stage."""
        del tier
        raise IntimateRoutingError(stage_provider="anthropic")

    result = draft_tool(
        vault_path=vault,
        llm_factory=_explode,
        privacy_tier_ceiling=TierCeiling.ALL,
        phase="rising",
    )

    assert result["status"] == "refused"
    assert result["tool"] == "creek.draft"
    assert result["tier_ceiling"] == "all"
    assert "no local backend" in result["reason"]
    assert "frag-intimate" not in result["reason"]


def test_draft_does_not_build_a_provider_when_it_cannot_draft(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither non-drafting exit path may build an LLM provider.

    Both the "no idea seeds" empty response and the out-of-range-index refusal
    return before any prompt exists, so invoking the factory would spend a
    provider handshake (and, on a cloud stage, an API credential) on a call
    that never drafts. ``draft_tool`` takes an ``llm_factory:
    DraftLLMFactory``, not an already-built ``llm``, and the ``creek.draft``
    wrapper registered by ``creek_mcp.server._register_authoring_tools`` passes
    ``llm_factory=factory`` **uncalled**, so the laziness has shipped and this
    test is what pins it rather than aspiring to it.
    """
    invocations: list[PrivacyTier] = []

    def _must_not_run(tier: PrivacyTier) -> Callable[[str], str]:
        """Record the call and fail: nothing here should reach a provider."""
        invocations.append(tier)
        msg = "llm_factory must not be invoked when no draft is produced"
        raise AssertionError(msg)

    empty = draft_tool(
        vault_path=vault,
        llm_factory=_must_not_run,
        phase="rising",
    )
    assert empty["status"] == "empty"

    _patch_miner(monkeypatch, _seed_with_sources(("frag-1",)))
    refused = draft_tool(
        vault_path=vault,
        llm_factory=_must_not_run,
        phase="rising",
        index=99,
    )
    assert refused["status"] == "refused"
    assert "out of range" in refused["reason"]
    assert invocations == []


def test_author_tool_returns_draft_envelope(vault: Path) -> None:
    """``creek.author`` returns the full draft envelope from the real desk."""
    _write_fragment(vault, frag_id="frag-a", title="F6 Pluralism and community")
    _write_fragment(vault, frag_id="frag-b", title="Agency and momentum")
    result = author_tool(
        vault_path=vault,
        query="What is F6 Pluralism?",
        medium="research",
        consumer="test",
    )
    assert result["status"] == "ok"
    assert result["tool"] == "creek.author"
    assert result["medium"] == "research"
    assert result["verdict"] in {"PASS", "REVISE", "ESCALATE"}
    assert isinstance(result["provenance"], list)
    assert result["provenance"]  # non-empty
    assert result["claims"]  # cited claims envelope key
    assert all(claim["source_fragments"] for claim in result["claims"])
    assert result["body"].strip()
    assert result["dry_run"] is False
    assert result["rounds"] >= 1
    # #660: the ceiling is now enforced — the specialists tier-filter retrieval.
    assert result["tier_ceiling_enforced"] is True


def test_author_tool_rejects_unknown_medium(vault: Path) -> None:
    """An unsupported medium returns a structured error, not a draft."""
    result = author_tool(
        vault_path=vault,
        query="q",
        medium="not-a-medium",
        consumer="test",
    )
    assert result["status"] == "error"
    assert result["tool"] == "creek.author"
    assert result["tier_ceiling"] == "open"  # ceiling echoed on the error path
    assert result["tier_ceiling_enforced"] is False  # key present on every envelope
    assert "research" in result["reason"]


def test_author_tool_error_envelope_includes_dry_run(vault: Path) -> None:
    """The error envelope carries ``dry_run`` so the shape matches success."""
    result = author_tool(
        vault_path=vault,
        query="q",
        medium="not-a-medium",
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
    _write_fragment(vault, frag_id="frag-med", title="Power as medicine")
    _write_fragment(vault, frag_id="frag-tox", title="Power as toxic")
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
    # With the real specialists (#463/#467) every cited fragment is one the
    # desk actually scanned in the seeded corpus.
    cited = {fid for claim in result["claims"] for fid in claim["source_fragments"]}
    assert cited <= {"frag-med", "frag-tox"}


def test_author_tool_dry_run_returns_plan(vault: Path) -> None:
    """``dry_run`` returns the pipeline plan + evidence summary, not a draft."""
    _write_fragment(vault, frag_id="frag-a", title="Alpha note about q")
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
    # The real specialists (#463/#467) ground evidence in the seeded corpus.
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


def test_author_tool_wraps_desk_errors(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A desk failure surfaces as a structured envelope, not an exception."""

    def boom(**_kwargs: object) -> object:
        msg = "provider unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr("creek_mcp.tools.author.run_author", boom)

    result = author_tool(
        vault_path=vault, query="q", medium="research", consumer="test"
    )

    assert result["status"] == "error"
    assert result["tool"] == "creek.author"
    assert result["tier_ceiling_enforced"] is False
    assert "provider unavailable" in result["reason"]
