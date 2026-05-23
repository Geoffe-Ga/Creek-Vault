"""Tests for the compile primitive (FEAT-003).

The compile module rolls up fragments from ``01-Fragments/`` into
synthesis pages on the compiled layer (``02-Threads/``, ``03-Eddies/``,
``06-Frequencies/``) with per-claim provenance back to the source
fragment IDs. The acceptance criteria covered here:

* Engine returns a :class:`CompiledPage` carrying provenance for every claim.
* LLM-detected paradoxes route to a side-channel JSONL log instead of the
  synthesis body.
* CLI ``creek compile <fragment-id>`` produces an updated thread / eddy
  note with provenance frontmatter.
* ``intimate``-tier fragments contribute title-only excerpts.
* Re-runs are idempotent: provenance lists merge, no duplicate claims.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.compile.engine import (
    PARADOX_LOG_RELPATH,
    CompileLLM,
    CompileResult,
    compile_fragments,
    compile_to_vault,
)
from creek.compile.provenance import (
    ProvenanceEntry,
    merge_provenance,
)
from creek.models import (
    CompiledPage,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---- Helpers ---------------------------------------------------------------


def _make_fragment(
    *,
    frag_id: str,
    title: str = "A fragment about systems",
    privacy: PrivacyTier = PrivacyTier.OPEN,
) -> Fragment:
    """Build a deterministic Fragment for tests."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        privacy_tier=privacy,
    )


def _write_fragment_to_vault(vault: Path, fragment: Fragment, body: str) -> Path:
    """Persist *fragment* to ``<vault>/01-Fragments/<id>.md``."""
    root = vault / "01-Fragments" / "Notes"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{fragment.id}.md"
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Create a minimal vault layout under ``tmp_path``."""
    for sub in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "06-Frequencies",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _llm_response(
    claims: list[dict[str, object]], paradoxes: list[dict[str, object]] | None = None
) -> str:
    """Render a canned LLM response payload."""
    payload: dict[str, object] = {"claims": claims, "paradoxes": paradoxes or []}
    return json.dumps(payload)


def _make_llm(responses: list[str]) -> tuple[CompileLLM, list[str]]:
    """Return an LLM stub that yields *responses* in order; record prompts."""
    received: list[str] = []
    iterator: Iterator[str] = iter(responses)

    def _call(prompt: str) -> str:
        received.append(prompt)
        return next(iterator)

    return _call, received


# ---- ProvenanceEntry & merge ---------------------------------------------


def test_provenance_entry_required_fields() -> None:
    """ProvenanceEntry validates the FEAT-003 schema."""
    entry = ProvenanceEntry(
        claim_id="claim-001",
        claim_excerpt="Systems thinking helps integrate patterns",
        fragment_ids=["frag-001", "frag-002"],
        compiled_at=datetime(2026, 5, 6, 17, 35, tzinfo=UTC),
        compile_method="llm",
    )
    assert entry.claim_id == "claim-001"
    assert "frag-001" in entry.fragment_ids
    assert entry.compile_method == "llm"


def test_merge_provenance_dedupes_fragment_ids() -> None:
    """Re-running compile with overlapping fragments dedupes per claim."""
    older = ProvenanceEntry(
        claim_id="claim-001",
        claim_excerpt="A claim",
        fragment_ids=["frag-001"],
        compiled_at=datetime(2026, 1, 1, tzinfo=UTC),
        compile_method="llm",
    )
    newer = ProvenanceEntry(
        claim_id="claim-001",
        claim_excerpt="A claim",
        fragment_ids=["frag-001", "frag-002"],
        compiled_at=datetime(2026, 5, 1, tzinfo=UTC),
        compile_method="llm",
    )
    merged = merge_provenance([older], [newer])
    assert len(merged) == 1
    assert merged[0].fragment_ids == ["frag-001", "frag-002"]
    assert merged[0].compiled_at == datetime(2026, 5, 1, tzinfo=UTC)


def test_merge_provenance_appends_new_claims() -> None:
    """Disjoint claim IDs accumulate rather than replace each other."""
    a = ProvenanceEntry(
        claim_id="claim-001",
        claim_excerpt="A",
        fragment_ids=["frag-001"],
        compiled_at=datetime(2026, 1, 1, tzinfo=UTC),
        compile_method="llm",
    )
    b = ProvenanceEntry(
        claim_id="claim-002",
        claim_excerpt="B",
        fragment_ids=["frag-002"],
        compiled_at=datetime(2026, 5, 1, tzinfo=UTC),
        compile_method="llm",
    )
    merged = merge_provenance([a], [b])
    ids = sorted(entry.claim_id for entry in merged)
    assert ids == ["claim-001", "claim-002"]


# ---- engine.compile_fragments --------------------------------------------


def test_compile_fragments_returns_provenance_for_every_claim() -> None:
    """Engine emits a CompiledPage with one ProvenanceEntry per LLM claim."""
    frag_a = _make_fragment(frag_id="frag-aaa")
    frag_b = _make_fragment(frag_id="frag-bbb", title="A second fragment")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "Systems thinking integrates patterns across domains.",
                "fragment_ids": ["frag-aaa"],
            },
            {
                "id": "claim-002",
                "text": "Provenance must trace each claim to its source.",
                "fragment_ids": ["frag-aaa", "frag-bbb"],
            },
        ],
    )
    llm, _ = _make_llm([response])

    result = compile_fragments(
        [(frag_a, "body of A"), (frag_b, "body of B")],
        llm=llm,
        target_kind="thread",
        target_id="thread-systems",
        target_title="Systems Thinking",
    )

    assert isinstance(result, CompileResult)
    assert isinstance(result.page, CompiledPage)
    assert len(result.page.provenance) == 2
    claim_ids = {p.claim_id for p in result.page.provenance}
    assert claim_ids == {"claim-001", "claim-002"}
    assert result.paradoxes == []


def test_compile_fragments_routes_paradoxes_to_side_channel() -> None:
    """Paradox entries surface on the result, not in the synthesis body."""
    frag_a = _make_fragment(frag_id="frag-aaa")
    frag_b = _make_fragment(frag_id="frag-bbb")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A settled claim from frag-aaa.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
        paradoxes=[
            {
                "description": "frag-aaa says X but frag-bbb contradicts it.",
                "fragment_ids": ["frag-aaa", "frag-bbb"],
            },
        ],
    )
    llm, _ = _make_llm([response])

    result = compile_fragments(
        [(frag_a, "body A"), (frag_b, "body B")],
        llm=llm,
        target_kind="thread",
        target_id="thread-x",
        target_title="X",
    )

    assert len(result.paradoxes) == 1
    assert result.paradoxes[0].fragment_ids == ["frag-aaa", "frag-bbb"]
    # The paradox text must NOT appear in the synthesis page body.
    assert "contradicts" not in result.page.body


def test_compile_fragments_intimate_tier_contributes_title_only() -> None:
    """Intimate-tier fragments hand the LLM a title-only excerpt."""
    intimate = _make_fragment(
        frag_id="frag-int",
        title="An intimate observation",
        privacy=PrivacyTier.INTIMATE,
    )
    open_frag = _make_fragment(frag_id="frag-pub", title="An open observation")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A claim",
                "fragment_ids": ["frag-int", "frag-pub"],
            },
        ],
    )
    llm, prompts = _make_llm([response])

    compile_fragments(
        [
            (intimate, "the intimate body that must not leak"),
            (open_frag, "the open body that may flow"),
        ],
        llm=llm,
        target_kind="eddy",
        target_id="eddy-y",
        target_title="Y",
    )

    prompt = prompts[0]
    assert "the intimate body that must not leak" not in prompt
    assert "An intimate observation" in prompt
    assert "the open body that may flow" in prompt


def test_compile_fragments_idempotent_merges_provenance() -> None:
    """Re-running with the same fragments merges (does not duplicate) claims."""
    frag = _make_fragment(frag_id="frag-aaa")
    claim_payload = [
        {
            "id": "claim-001",
            "text": "A stable claim about systems.",
            "fragment_ids": ["frag-aaa"],
        },
    ]
    llm, _ = _make_llm([_llm_response(claim_payload)] * 2)

    first = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="thread-z",
        target_title="Z",
    )
    second = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="thread-z",
        target_title="Z",
        existing_provenance=first.page.provenance,
    )

    assert len(second.page.provenance) == 1
    assert second.page.provenance[0].fragment_ids == ["frag-aaa"]


# ---- compile_to_vault & paradox log --------------------------------------


def test_compile_to_vault_writes_provenance_frontmatter(vault: Path) -> None:
    """compile_to_vault writes the synthesis page with provenance frontmatter."""
    frag = _make_fragment(frag_id="frag-aaa", title="A fragment")
    _write_fragment_to_vault(vault, frag, "body of A")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A surfaced claim.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
    )
    llm, _ = _make_llm([response])

    written = compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-aaa",
        target_title="Systems",
        llm=llm,
    )

    assert written.exists()
    post = frontmatter.load(str(written))
    provenance = post.metadata.get("provenance")
    assert isinstance(provenance, list) and len(provenance) == 1
    assert provenance[0]["claim_id"] == "claim-001"
    assert provenance[0]["fragment_ids"] == ["frag-aaa"]


def test_compile_to_vault_logs_paradoxes_to_side_channel(vault: Path) -> None:
    """Paradoxes go to the side-channel JSONL, never into the page body."""
    frag_a = _make_fragment(frag_id="frag-aaa")
    frag_b = _make_fragment(frag_id="frag-bbb")
    _write_fragment_to_vault(vault, frag_a, "body A")
    _write_fragment_to_vault(vault, frag_b, "body B")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A settled claim.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
        paradoxes=[
            {
                "description": "Direct contradiction between A and B.",
                "fragment_ids": ["frag-aaa", "frag-bbb"],
            },
        ],
    )
    llm, _ = _make_llm([response])

    written = compile_to_vault(
        fragment_ids=["frag-aaa", "frag-bbb"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-aaa",
        target_title="X",
        llm=llm,
    )

    log_path = vault / PARADOX_LOG_RELPATH
    assert log_path.exists()
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["target_id"] == "thread-aaa"
    assert record["fragment_ids"] == ["frag-aaa", "frag-bbb"]
    body = frontmatter.load(str(written)).content
    assert "contradiction" not in body.lower()


def test_compile_to_vault_is_idempotent(vault: Path) -> None:
    """Running compile twice merges provenance without duplicating claims."""
    frag = _make_fragment(frag_id="frag-aaa")
    _write_fragment_to_vault(vault, frag, "body")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "Stable claim.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
    )
    llm, _ = _make_llm([response, response])

    compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-z",
        target_title="Z",
        llm=llm,
    )
    written = compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-z",
        target_title="Z",
        llm=llm,
    )
    post = frontmatter.load(str(written))
    provenance = post.metadata["provenance"]
    assert len(provenance) == 1
    assert provenance[0]["fragment_ids"] == ["frag-aaa"]


def test_compile_to_vault_unknown_fragment_raises(vault: Path) -> None:
    """A missing fragment ID surfaces as a clear error rather than a silent skip."""
    llm, _ = _make_llm([])
    with pytest.raises(ValueError, match="not found"):
        compile_to_vault(
            fragment_ids=["frag-missing"],
            vault_path=vault,
            target_kind="thread",
            target_id="thread-z",
            target_title="Z",
            llm=llm,
        )


# ---- CLI integration -----------------------------------------------------


def test_cli_compile_writes_thread_with_provenance(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`creek compile <fragment-id>` produces an updated thread note."""
    frag = _make_fragment(frag_id="frag-aaa")
    _write_fragment_to_vault(vault, frag, "body A")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A claim.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
    )

    def fake_llm(_prompt: str) -> str:
        return response

    monkeypatch.setattr("creek.compile.engine._default_llm", lambda _config: fake_llm)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "compile",
            "frag-aaa",
            "--vault",
            str(vault),
            "--target-kind",
            "thread",
            "--target-id",
            "thread-aaa",
            "--target-title",
            "Systems",
        ],
    )

    assert result.exit_code == 0, result.stdout
    written = vault / "02-Threads" / "Active" / "thread-aaa.md"
    assert written.exists()
    post = frontmatter.load(str(written))
    assert post.metadata["provenance"][0]["claim_id"] == "claim-001"


def test_compile_fragments_unknown_target_kind_raises() -> None:
    """An unsupported target_kind surfaces as ValueError before LLM dispatch."""
    llm, _ = _make_llm([])
    with pytest.raises(ValueError, match="Unknown target_kind"):
        compile_fragments(
            [],
            llm=llm,
            target_kind="not_a_real_kind",  # type: ignore[arg-type]
            target_id="t",
            target_title="T",
        )


def test_compile_fragments_personal_tier_contributes_title_only() -> None:
    """Personal-tier fragments collapse to a title-only summary."""
    personal = _make_fragment(
        frag_id="frag-personal",
        title="A personal note",
        privacy=PrivacyTier.PERSONAL,
    )
    response = _llm_response(
        claims=[
            {"id": "claim-001", "text": "x", "fragment_ids": ["frag-personal"]},
        ],
    )
    llm, prompts = _make_llm([response])
    compile_fragments(
        [(personal, "the personal body that must not leak")],
        llm=llm,
        target_kind="thread",
        target_id="t-x",
        target_title="X",
    )
    assert "the personal body that must not leak" not in prompts[0]
    assert "Personal-tier summary" in prompts[0]


def test_compile_fragments_rejects_non_json_response() -> None:
    """Non-JSON payloads from the LLM raise a clear ValueError."""
    frag = _make_fragment(frag_id="frag-aaa")
    llm, _ = _make_llm(["not json at all"])
    with pytest.raises(ValueError, match="non-JSON"):
        compile_fragments(
            [(frag, "body")],
            llm=llm,
            target_kind="thread",
            target_id="t",
            target_title="T",
        )


def test_compile_fragments_tolerates_json_fenced_block() -> None:
    """LLM responses wrapped in ``` ```json ... ``` `` ` parse successfully.

    Claude 4.x routinely wraps structured responses in fenced code
    blocks regardless of prompt wording (INC-007). The parser must
    survive both ``` ```json `` ` and bare ``` ``` `` ` fences.
    """
    frag = _make_fragment(frag_id="frag-aaa")
    payload = json.dumps(
        {
            "claims": [{"id": "c1", "text": "X.", "fragment_ids": ["frag-aaa"]}],
            "paradoxes": [],
        },
    )
    wrapped = f"```json\n{payload}\n```"
    llm, _ = _make_llm([wrapped])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert [p.claim_id for p in result.page.provenance] == ["c1"]


def test_compile_fragments_tolerates_plain_fenced_block() -> None:
    """LLM responses wrapped in a plain ``` ``` `` ` fence (no language tag) parse."""
    frag = _make_fragment(frag_id="frag-aaa")
    payload = json.dumps(
        {
            "claims": [{"id": "c1", "text": "X.", "fragment_ids": ["frag-aaa"]}],
            "paradoxes": [],
        },
    )
    wrapped = f"```\n{payload}\n```"
    llm, _ = _make_llm([wrapped])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert [p.claim_id for p in result.page.provenance] == ["c1"]


def test_compile_fragments_tolerates_leading_preamble() -> None:
    """A short preamble before the JSON object is stripped before parsing."""
    frag = _make_fragment(frag_id="frag-aaa")
    payload = json.dumps(
        {
            "claims": [{"id": "c1", "text": "X.", "fragment_ids": ["frag-aaa"]}],
            "paradoxes": [],
        },
    )
    wrapped = f"Here is the requested JSON:\n{payload}"
    llm, _ = _make_llm([wrapped])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert [p.claim_id for p in result.page.provenance] == ["c1"]


def test_compile_fragments_tolerates_trailing_text() -> None:
    """A trailing sign-off after the JSON object is ignored."""
    frag = _make_fragment(frag_id="frag-aaa")
    payload = json.dumps(
        {
            "claims": [{"id": "c1", "text": "X.", "fragment_ids": ["frag-aaa"]}],
            "paradoxes": [],
        },
    )
    wrapped = f"{payload}\n\nLet me know if you need anything else."
    llm, _ = _make_llm([wrapped])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert [p.claim_id for p in result.page.provenance] == ["c1"]


def test_compile_fragments_handles_nested_braces_in_payload() -> None:
    """A wrapped payload with braces inside string values still parses."""
    frag = _make_fragment(frag_id="frag-aaa")
    payload = json.dumps(
        {
            "claims": [
                {
                    "id": "c1",
                    "text": "A claim with {curly} braces in the text {value}.",
                    "fragment_ids": ["frag-aaa"],
                },
            ],
            "paradoxes": [],
        },
    )
    wrapped = f"```json\n{payload}\n```"
    llm, _ = _make_llm([wrapped])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert [p.claim_id for p in result.page.provenance] == ["c1"]


def test_compile_fragments_rejects_unterminated_object_in_fence() -> None:
    """An unterminated JSON object — even inside a fence — still raises."""
    frag = _make_fragment(frag_id="frag-aaa")
    llm, _ = _make_llm(['```json\n{"claims": [{"id": "c1"\n```'])
    with pytest.raises(ValueError, match="non-JSON"):
        compile_fragments(
            [(frag, "body")],
            llm=llm,
            target_kind="thread",
            target_id="t",
            target_title="T",
        )


def test_build_prompt_requests_raw_json() -> None:
    """The compile prompt explicitly instructs the LLM to emit raw JSON only.

    Tightening the prompt is one half of the INC-007 fix; the tolerant
    parser is the other. Together they keep the system forgiving on
    input and explicit on output.
    """
    frag = _make_fragment(frag_id="frag-aaa")
    response = _llm_response(
        claims=[{"id": "c1", "text": "x", "fragment_ids": ["frag-aaa"]}],
    )
    llm, prompts = _make_llm([response])
    compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    lowered = prompts[0].lower()
    assert "raw json" in lowered
    assert "code fence" in lowered or "fenced" in lowered or "fences" in lowered


def test_compile_fragments_rejects_non_object_payload() -> None:
    """A JSON array (rather than an object) at the top level is rejected."""
    frag = _make_fragment(frag_id="frag-aaa")
    llm, _ = _make_llm([json.dumps(["not", "an", "object"])])
    with pytest.raises(ValueError, match="JSON object"):
        compile_fragments(
            [(frag, "body")],
            llm=llm,
            target_kind="thread",
            target_id="t",
            target_title="T",
        )


def test_compile_fragments_rejects_non_array_claims() -> None:
    """If ``claims`` or ``paradoxes`` come back as the wrong shape, fail loudly."""
    frag = _make_fragment(frag_id="frag-aaa")
    bad = json.dumps({"claims": "not a list", "paradoxes": []})
    llm, _ = _make_llm([bad])
    with pytest.raises(ValueError, match="must be arrays"):
        compile_fragments(
            [(frag, "body")],
            llm=llm,
            target_kind="thread",
            target_id="t",
            target_title="T",
        )


def test_compile_fragments_skips_claim_with_missing_id() -> None:
    """Claims with a missing or empty ``id`` are dropped before body render.

    Without the guard, ``_render_body`` would emit a broken Markdown
    footnote (``[^]``) and ``ProvenanceEntry`` construction would
    KeyError. Treat both as a malformed-LLM signal and skip silently.
    """
    frag = _make_fragment(frag_id="frag-aaa")
    response = json.dumps(
        {
            "claims": [
                {"text": "no id here", "fragment_ids": ["frag-aaa"]},
                {"id": "", "text": "empty id", "fragment_ids": ["frag-aaa"]},
                {
                    "id": "claim-002",
                    "text": "well-formed claim.",
                    "fragment_ids": ["frag-aaa"],
                },
            ],
            "paradoxes": [],
        },
    )
    llm, _ = _make_llm([response])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert "[^]" not in result.page.body
    assert len(result.page.provenance) == 1
    assert result.page.provenance[0].claim_id == "claim-002"


def test_compile_fragments_skips_empty_claim_text() -> None:
    """Claims with empty text fall out of the body (still tracked in provenance)."""
    frag = _make_fragment(frag_id="frag-aaa")
    response = _llm_response(
        claims=[
            {"id": "claim-001", "text": "", "fragment_ids": ["frag-aaa"]},
            {"id": "claim-002", "text": "Real claim.", "fragment_ids": ["frag-aaa"]},
        ],
    )
    llm, _ = _make_llm([response])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert "claim-001" not in result.page.body
    assert "claim-002" in result.page.body


def test_compile_to_vault_skips_extra_fragments_in_directory(vault: Path) -> None:
    """Fragments in 01-Fragments that aren't requested are not picked up."""
    wanted = _make_fragment(frag_id="frag-wanted")
    extra = _make_fragment(frag_id="frag-extra")
    _write_fragment_to_vault(vault, wanted, "wanted body")
    _write_fragment_to_vault(vault, extra, "extra body")
    response = _llm_response(
        claims=[
            {"id": "claim-001", "text": "C", "fragment_ids": ["frag-wanted"]},
        ],
    )
    llm, prompts = _make_llm([response])
    compile_to_vault(
        fragment_ids=["frag-wanted"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-x",
        target_title="X",
        llm=llm,
    )
    assert "extra body" not in prompts[0]


def test_load_existing_provenance_handles_unreadable_page(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt existing page falls back to an empty provenance baseline."""
    from creek.compile import engine as engine_module

    target = vault / "02-Threads" / "Active" / "thread-x.md"
    target.write_text("not even valid frontmatter\x00\x00", encoding="utf-8")

    def boom(_path: str) -> object:
        msg = "synthetic frontmatter parse failure"
        raise OSError(msg)

    monkeypatch.setattr(engine_module.frontmatter, "load", boom)
    assert engine_module._load_existing_provenance(target) == []


def test_compile_to_vault_ignores_non_list_existing_provenance(vault: Path) -> None:
    """If the existing page has a non-list provenance, treat it as empty."""
    frag = _make_fragment(frag_id="frag-aaa")
    _write_fragment_to_vault(vault, frag, "body")
    target = vault / "02-Threads" / "Active" / "thread-x.md"
    post = frontmatter.Post(content="# T\n", provenance="not a list")
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    response = _llm_response(
        claims=[{"id": "claim-001", "text": "X", "fragment_ids": ["frag-aaa"]}],
    )
    llm, _ = _make_llm([response])
    compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-x",
        target_title="X",
        llm=llm,
    )
    written = frontmatter.load(str(target))
    assert len(written.metadata["provenance"]) == 1


def test_str_list_drops_non_list_inputs_from_llm() -> None:
    """An LLM that returns a non-list ``fragment_ids`` produces an empty list."""
    frag = _make_fragment(frag_id="frag-aaa")
    response = json.dumps(
        {
            "claims": [
                {"id": "claim-001", "text": "X", "fragment_ids": "not-a-list"},
            ],
            "paradoxes": [],
        },
    )
    llm, _ = _make_llm([response])
    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="t",
        target_title="T",
    )
    assert result.page.provenance[0].fragment_ids == []


def test_default_llm_returns_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI entry point's LLM factory wraps the Anthropic provider."""
    from creek.compile import engine as engine_module

    captured: dict[str, object] = {}

    class _StubProvider:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        def call(self, prompt: str) -> str:
            return f"echo:{prompt}"

    monkeypatch.setattr(
        "creek.classify.llm.AnthropicProvider",
        _StubProvider,
    )
    sentinel = object()
    llm = engine_module._default_llm(sentinel)
    assert callable(llm)
    assert llm("hi") == "echo:hi"
    assert captured["config"] is sentinel


def test_cli_compile_unknown_target_kind_exits_2(vault: Path) -> None:
    """Unknown --target-kind exits with code 2 and a helpful message."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "compile",
            "frag-aaa",
            "--vault",
            str(vault),
            "--target-kind",
            "bogus",
            "--target-id",
            "thread-x",
            "--target-title",
            "X",
        ],
    )
    assert result.exit_code == 2
    assert "target-kind" in result.stdout.lower() or "kind" in result.stdout.lower()


# ---- FEAT-025: hierarchy-aware compile -----------------------------------


def _make_child_fragment(
    *,
    frag_id: str,
    parent_id: str,
    level: str = "paragraph",
    title: str = "A child fragment",
) -> Fragment:
    """Build a child fragment that references a parent via parent_id."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level=level,  # type: ignore[arg-type]
        parent_id=parent_id,
    )


def test_compile_records_level_policy_and_source_levels_in_frontmatter() -> None:
    """The CompiledPage records the level_policy + source_levels used."""
    frag = _make_fragment(frag_id="frag-aaa")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A claim.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
    )
    llm, _ = _make_llm([response])

    result = compile_fragments(
        [(frag, "body")],
        llm=llm,
        target_kind="thread",
        target_id="thread-l",
        target_title="L",
    )

    assert result.page.level_policy == "leaves"
    assert result.page.source_levels == ["document"]


def test_compile_filters_to_leaves_by_default() -> None:
    """Parents whose children are also in the set drop out of the synthesis."""
    parent = Fragment(
        id="frag-parent",
        title="Whole document",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level="document",
        child_ids=["frag-child-1", "frag-child-2"],
    )
    child1 = _make_child_fragment(frag_id="frag-child-1", parent_id="frag-parent")
    child2 = _make_child_fragment(
        frag_id="frag-child-2",
        parent_id="frag-parent",
        title="A second child",
    )
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "Synthesised from leaves.",
                "fragment_ids": ["frag-child-1", "frag-child-2"],
            },
        ],
    )
    llm, prompts = _make_llm([response])

    result = compile_fragments(
        [
            (parent, "the entire parent body that must not be duplicated"),
            (child1, "child one body"),
            (child2, "child two body"),
        ],
        llm=llm,
        target_kind="thread",
        target_id="thread-h",
        target_title="H",
    )

    prompt = prompts[0]
    # Parent body must NOT appear — it was rolled up via leaves.
    assert "the entire parent body that must not be duplicated" not in prompt
    # Both leaf bodies do appear.
    assert "child one body" in prompt
    assert "child two body" in prompt
    # source_levels reflect what was actually used.
    assert result.page.source_levels == ["paragraph"]


def test_compile_surfaces_parent_as_structural_path_context() -> None:
    """Parents appear in the prompt as structural-path context, not as bodies."""
    parent = Fragment(
        id="frag-parent",
        title="The Capricorn Moon",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level="document",
        child_ids=["frag-child"],
    )
    child = _make_child_fragment(
        frag_id="frag-child",
        parent_id="frag-parent",
        title="On grief",
    )
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A claim from the leaf.",
                "fragment_ids": ["frag-child"],
            },
        ],
    )
    llm, prompts = _make_llm([response])

    compile_fragments(
        [(parent, "parent body"), (child, "child body")],
        llm=llm,
        target_kind="thread",
        target_id="thread-c",
        target_title="C",
    )

    prompt = prompts[0]
    # The leaf body flows; the parent's title surfaces as breadcrumb context.
    assert "child body" in prompt
    assert "The Capricorn Moon" in prompt
    assert "parent body" not in prompt


def test_compile_level_policy_all_keeps_pre_feat025_behaviour() -> None:
    """An explicit ``all`` policy mirrors the flat-vault path."""
    parent = Fragment(
        id="frag-parent",
        title="A document",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level="document",
        child_ids=["frag-child"],
    )
    child = _make_child_fragment(frag_id="frag-child", parent_id="frag-parent")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A claim.",
                "fragment_ids": ["frag-parent", "frag-child"],
            },
        ],
    )
    llm, prompts = _make_llm([response])

    result = compile_fragments(
        [(parent, "parent body"), (child, "child body")],
        llm=llm,
        target_kind="thread",
        target_id="thread-a",
        target_title="A",
        level_policy="all",
    )

    prompt = prompts[0]
    assert "parent body" in prompt
    assert "child body" in prompt
    assert result.page.level_policy == "all"
    assert set(result.page.source_levels) == {"document", "paragraph"}


def test_compile_to_vault_persists_level_policy_in_frontmatter(vault: Path) -> None:
    """Frontmatter on disk carries the level_policy + source_levels fields."""
    frag = _make_fragment(frag_id="frag-aaa")
    _write_fragment_to_vault(vault, frag, body="body")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "A claim.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
    )
    llm, _ = _make_llm([response])

    written = compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-d",
        target_title="D",
        llm=llm,
    )

    post = frontmatter.load(str(written))
    assert post.metadata["level_policy"] == "leaves"
    assert post.metadata["source_levels"] == ["document"]


def test_compile_flat_vault_regression(vault: Path) -> None:
    """A flat-fragment vault produces the same provenance as before FEAT-025."""
    frag_a = _make_fragment(frag_id="frag-aaa")
    frag_b = _make_fragment(frag_id="frag-bbb")
    _write_fragment_to_vault(vault, frag_a, body="body A")
    _write_fragment_to_vault(vault, frag_b, body="body B")
    response = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "Shared claim.",
                "fragment_ids": ["frag-aaa", "frag-bbb"],
            },
        ],
    )
    llm, _ = _make_llm([response])

    written = compile_to_vault(
        fragment_ids=["frag-aaa", "frag-bbb"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-flat",
        target_title="Flat",
        llm=llm,
    )

    post = frontmatter.load(str(written))
    provenance = post.metadata["provenance"]
    assert len(provenance) == 1
    assert provenance[0]["fragment_ids"] == ["frag-aaa", "frag-bbb"]
