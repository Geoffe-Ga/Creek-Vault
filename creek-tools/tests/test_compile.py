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

from creek.classify.llm.router import IntimateRoutingError, ModelRouter
from creek.cli import app
from creek.compile.engine import (
    PARADOX_LOG_RELPATH,
    CompileLLM,
    CompileResult,
    compile_fragments,
    compile_to_vault,
    default_llm,
)
from creek.compile.provenance import (
    ProvenanceEntry,
    merge_provenance,
)
from creek.config import LLMConfig, LLMRoutingConfig, load_config
from creek.models import (
    CompiledPage,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)
from creek.vault.reader import iter_vault_fragments

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
        llm_factory=lambda _tier: llm,
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
        llm_factory=lambda _tier: llm,
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
        llm_factory=lambda _tier: llm,
    )
    written = compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-z",
        target_title="Z",
        llm_factory=lambda _tier: llm,
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
            llm_factory=lambda _tier: llm,
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

    monkeypatch.setattr("creek.compile.engine.default_llm", lambda _config: fake_llm)
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


def test_compile_fragments_rejects_string_claim_elements() -> None:
    """Bare-string ``claims`` elements are rejected at the parse boundary.

    An LLM returning ``{"claims": ["insight one"]}`` — prose strings
    where claim objects belong — cleared the list-shape check and then
    crashed downstream in ``_filter_valid_claims`` with
    ``AttributeError: 'str' object has no attribute 'get'``. Element
    types are part of the payload schema, so the rejection belongs at
    the parse boundary with the other schema errors.
    """
    frag = _make_fragment(frag_id="frag-aaa")
    bad = json.dumps({"claims": ["insight one", "insight two"], "paradoxes": []})
    llm, _ = _make_llm([bad])
    with pytest.raises(ValueError, match="must contain JSON objects"):
        compile_fragments(
            [(frag, "body")],
            llm=llm,
            target_kind="thread",
            target_id="t",
            target_title="T",
        )


def test_compile_fragments_rejects_string_paradox_elements() -> None:
    """Bare-string ``paradoxes`` elements fail under the same guard.

    ``_payload_to_paradox_entries`` calls ``item.get`` exactly the way
    the claim filter does, so a string paradox crashes identically.
    Well-formed claims must not rescue a payload whose paradoxes are
    malformed — both arrays are validated, not just the first one.
    """
    frag = _make_fragment(frag_id="frag-aaa")
    bad = json.dumps(
        {
            "claims": [{"id": "c1", "text": "X", "fragment_ids": ["frag-aaa"]}],
            "paradoxes": ["they contradict"],
        },
    )
    llm, _ = _make_llm([bad])
    with pytest.raises(ValueError, match="must contain JSON objects"):
        compile_fragments(
            [(frag, "body")],
            llm=llm,
            target_kind="thread",
            target_id="t",
            target_title="T",
        )


def test_compile_fragments_rejects_mixed_claim_elements() -> None:
    """One bad element rejects the whole payload; the good claim is not kept.

    Whole-payload rejection is the deliberate design decision here, and
    this test is where it is encoded. The alternative — silently
    dropping the bad element and compiling the survivors — would let a
    truncated or prose-contaminated response write a compiled page whose
    body is wiped down to just the title while stale provenance survives
    in frontmatter. A quietly corrupted note is worse than a loud
    failure the operator can retry, so the parser refuses the payload
    whole rather than salvaging part of it.

    This is distinct from ``_filter_valid_claims``, which keeps its
    silent-drop semantics for structurally valid claim objects that are
    merely incomplete (see
    ``test_compile_fragments_skips_claim_with_missing_id``).
    """
    frag = _make_fragment(frag_id="frag-aaa")
    payload: dict[str, object] = {
        "claims": [
            {"id": "c1", "text": "X", "fragment_ids": ["frag-aaa"]},
            "a bare string",
        ],
        "paradoxes": [],
    }
    llm, _ = _make_llm([json.dumps(payload)])
    with pytest.raises(ValueError, match="must contain JSON objects"):
        compile_fragments(
            [(frag, "body")],
            llm=llm,
            target_kind="thread",
            target_id="t",
            target_title="T",
        )


def test_compile_to_vault_leaves_existing_page_untouched_on_string_claims(
    vault: Path,
) -> None:
    """A malformed re-compile aborts before it touches the page on disk.

    ``compile_to_vault`` parses the LLM payload before ``_write_compiled_page``
    runs, so a schema violation on a re-run must leave the previously
    compiled page byte-identical. This pins the conservative
    abort-before-write contract and guards against any future slide
    toward drop-the-bad-element-and-continue, which would rewrite the
    page with a hollowed-out body.
    """
    frag = _make_fragment(frag_id="frag-aaa")
    _write_fragment_to_vault(vault, frag, "body")
    good = _llm_response(
        claims=[
            {
                "id": "claim-001",
                "text": "Stable claim.",
                "fragment_ids": ["frag-aaa"],
            },
        ],
    )
    bad = json.dumps({"claims": ["insight one"], "paradoxes": []})
    llm, _ = _make_llm([good, bad])

    target = compile_to_vault(
        fragment_ids=["frag-aaa"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-untouched",
        target_title="Untouched",
        llm_factory=lambda _tier: llm,
    )
    before = target.read_text(encoding="utf-8")
    assert "Stable claim." in before

    with pytest.raises(ValueError, match="must contain JSON objects"):
        compile_to_vault(
            fragment_ids=["frag-aaa"],
            vault_path=vault,
            target_kind="thread",
            target_id="thread-untouched",
            target_title="Untouched",
            llm_factory=lambda _tier: llm,
        )
    assert target.read_text(encoding="utf-8") == before


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
    # The engine filters empty-text claims out of the rendered body but
    # keeps them in provenance (the filter key is ``id``, not ``text``);
    # pin both halves of that contract so a future reorder is caught.
    assert any(p.claim_id == "claim-001" for p in result.page.provenance)
    assert any(p.claim_id == "claim-002" for p in result.page.provenance)


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
        llm_factory=lambda _tier: llm,
    )
    assert "extra body" not in prompts[0]


def test_resolve_target_path_rejects_escaping_target_id(vault: Path) -> None:
    """``target_id`` that resolves outside the compiled-layer dir is rejected.

    The CLI passes ``target_id`` straight through from operator input;
    a value like ``"../escape"`` would land the synthesis page outside
    the intended ``02-Threads/Active/`` directory. The guard fires
    before any write touches disk.
    """
    from creek.compile import engine as engine_module

    with pytest.raises(ValueError, match="escapes the compiled-layer directory"):
        engine_module._resolve_target_path(vault, "thread", "../escape")
    # No file should have been written under the target directory.
    assert not (vault / "02-Threads" / "Active" / "..escape.md").exists()
    assert not (vault / "02-Threads" / "escape.md").exists()


def test_compile_to_vault_rejects_escaping_target_id(vault: Path) -> None:
    """The high-level entry point surfaces the escape guard's ValueError."""
    frag = _make_fragment(frag_id="frag-aaa")
    _write_fragment_to_vault(vault, frag, "body")
    response = _llm_response(
        claims=[{"id": "claim-001", "text": "X", "fragment_ids": ["frag-aaa"]}],
    )
    llm, _ = _make_llm([response])
    with pytest.raises(ValueError, match="escapes the compiled-layer directory"):
        compile_to_vault(
            fragment_ids=["frag-aaa"],
            vault_path=vault,
            target_kind="thread",
            target_id="../escape",
            target_title="X",
            llm_factory=lambda _tier: llm,
        )


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
        llm_factory=lambda _tier: llm,
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
    """The CLI entry point's LLM factory routes through ``build_provider`` (#646).

    No longer hard-wired to Anthropic: ``default_llm`` builds the configured
    provider via the factory and drives it through the provider-neutral
    ``complete`` method.
    """
    from creek.compile import engine as engine_module
    from creek.config import LLMConfig

    captured: dict[str, object] = {}

    class _StubProvider:
        def complete(self, prompt: str, **_kw: object) -> object:
            return type("C", (), {"text": f"echo:{prompt}"})()

    def _fake_build(config: object) -> _StubProvider:
        captured["config"] = config
        return _StubProvider()

    monkeypatch.setattr("creek.classify.llm.build_provider", _fake_build)
    config = LLMConfig(provider="ollama")
    llm = engine_module.default_llm(config)
    assert callable(llm)
    assert llm("hi") == "echo:hi"
    assert captured["config"] is config


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
        llm_factory=lambda _tier: llm,
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
        llm_factory=lambda _tier: llm,
    )

    post = frontmatter.load(str(written))
    provenance = post.metadata["provenance"]
    assert len(provenance) == 1
    assert provenance[0]["fragment_ids"] == ["frag-aaa", "frag-bbb"]


# ---------------------------------------------------------------------------
# #962: the compile LLM is built FOR the sources' privacy tier
#
# ``creek/cli.py`` resolved the compile client with
# ``config.model_router.resolve("generation")`` — no tier. A tier-less
# ``resolve`` makes ``ModelRouter._enforce_local_for_intimate`` a no-op, so the
# ``Intimate``-never-cloud invariant (#647) was simply not enforced on
# ``creek compile``. The engine therefore has to take a *tier-keyed factory*
# rather than a pre-built client, derive the tier from the fragments it loaded,
# and call the factory before anything touches disk.
#
# THE ANTI-VACUITY RULE for everything below: a test asserting "intimate
# content routes local" passes for free on a config whose ``generation`` stage
# is already local. Every routing test here therefore uses
# ``generation=anthropic`` (cloud) with ``default=ollama`` (local) AND asserts
# explicitly that the *unrouted* resolution would have been the cloud one,
# before asserting the routed one is local. Without that line the test proves
# nothing; do not drop it.
# ---------------------------------------------------------------------------


_INTIMATE_TITLE = "Therapy session with Dana"
_INTIMATE_BODY = "The intimate body text that must never leave this machine."
_EMPTY_COMPILE_PAYLOAD = '{"claims": [], "paradoxes": []}'


def _write_routing_config(vault: Path, *, default: str, generation: str) -> Path:
    """Write ``<vault>/00-Creek-Meta/creek_config.yaml`` with two LLM stages.

    The CLI discovers this file through
    :func:`creek.config.resolve_config_path`, so writing it is what makes a
    ``creek compile`` invocation exercise the *real*
    :class:`~creek.classify.llm.router.ModelRouter` rather than a stub.

    Args:
        vault: Vault root the CLI will be invoked against.
        default: Provider for the routing ``default`` stage — the local
            backend the ``Intimate``-never-cloud rule redirects to.
        generation: Provider for the ``generation`` stage, which is the
            stage ``creek compile`` resolves.

    Returns:
        The path of the written config file.
    """
    path = vault / "00-Creek-Meta" / "creek_config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "llm:\n"
        f"  default:\n    provider: {default}\n"
        f"  generation:\n    provider: {generation}\n",
        encoding="utf-8",
    )
    return path


def _write_fragment_without_privacy_tier(
    vault: Path,
    *,
    frag_id: str,
    title: str = "A legacy fragment",
    body: str = "legacy body",
) -> Path:
    """Write a fragment whose frontmatter omits ``privacy_tier`` entirely.

    Deliberately hand-rolled rather than routed through
    :func:`_write_fragment_to_vault`, which serialises a validated
    :class:`~creek.models.Fragment` and therefore always emits the key.
    A hand-edited or legacy vault file has no key at all, and
    :class:`~creek.models.Fragment` defaults the field to ``unclassified``
    — so the two cases are distinguishable *only* in the raw frontmatter.

    Args:
        vault: Vault root; the file lands under ``01-Fragments/Notes``.
        frag_id: The fragment id, also used as the filename stem.
        title: Fragment title.
        body: Markdown body beneath the frontmatter.

    Returns:
        The path of the written fragment file.
    """
    metadata: dict[str, object] = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": datetime(2026, 5, 1, 12, 0, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, 12, 0, tzinfo=UTC).isoformat(),
        "source": {"platform": SourcePlatform.MARKDOWN.value},
    }
    root = vault / "01-Fragments" / "Notes"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{frag_id}.md"
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return path


class _CompletionStub:
    """A provider completion exposing only the ``.text`` field callers read."""

    def __init__(self, text: str) -> None:
        """Store the completion *text*."""
        self.text = text


class _TierRecordingFactory:
    """Compile LLM factory recording the tier it was keyed with, and the prompt.

    The engine-side counterpart of
    ``tests/test_mcp_write_tools.py::_TierRecordingLLMFactory``. ``tiers``
    is typed to admit ``None`` on purpose: a regression that hands the
    factory no tier — the exact shape of #962, where
    :meth:`~creek.classify.llm.router.ModelRouter.resolve` was called
    without one and the tier gate silently became a no-op — must trip an
    assertion rather than quietly rank as non-intimate.
    """

    def __init__(self, response: str = _EMPTY_COMPILE_PAYLOAD) -> None:
        """Start with no recordings and a canned LLM *response*."""
        self.tiers: list[PrivacyTier | None] = []
        self.prompts: list[str] = []
        self.response = response

    def __call__(self, tier: PrivacyTier) -> CompileLLM:
        """Record *tier* and return the recording stub LLM."""
        self.tiers.append(tier)
        return self._complete

    def _complete(self, prompt: str) -> str:
        """Record *prompt* and return the canned response."""
        self.prompts.append(prompt)
        return self.response


class _RecordingDefaultLLM:
    """A ``creek.compile.engine.default_llm`` stand-in recording both halves.

    Captures the :class:`~creek.config.LLMConfig` the router resolved (the
    ``Intimate``-never-cloud decision) *and* the prompt text that reached
    the client it returned. Asserting only the first would leave "the
    prompt was harmless anyway" unproven; asserting only the second would
    leave the routing unproven.
    """

    def __init__(self, response: str) -> None:
        """Start with empty recordings and a canned LLM *response*."""
        self.configs: list[LLMConfig] = []
        self.prompts: list[str] = []
        self.response = response

    def __call__(self, config: LLMConfig) -> CompileLLM:
        """Record the resolved *config* and return the recording client."""
        self.configs.append(config)
        return self._complete

    def _complete(self, prompt: str) -> str:
        """Record *prompt* and return the canned response."""
        self.prompts.append(prompt)
        return self.response

    @property
    def provider_names(self) -> list[str]:
        """Return the ``provider`` of every recorded config, in call order."""
        return [config.provider for config in self.configs]


class _BuildProviderSpy:
    """A ``build_provider`` stand-in recording every config it is handed.

    A refusal on its own cannot prove nothing egressed: the provider
    factory is the last step before a real backend client exists, so an
    empty ``configs`` list is the evidence that the router refused
    *before* anything was built.
    """

    def __init__(self) -> None:
        """Start with no recorded configs."""
        self.configs: list[LLMConfig] = []

    def __call__(self, config: LLMConfig) -> _BuildProviderSpy:
        """Record *config* and return this spy as the constructed provider."""
        self.configs.append(config)
        return self

    def complete(self, _prompt: str) -> _CompletionStub:
        """Return an empty-but-parseable compile payload for the prompt."""
        return _CompletionStub(_EMPTY_COMPILE_PAYLOAD)


def test_cli_compile_routes_intimate_source_to_the_local_provider(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek compile`` keys the generation LLM with the source's tier (#962).

    The flagship regression. ``creek/cli.py`` built the compile client from
    ``config.model_router.resolve("generation")`` with no tier argument, and
    :meth:`~creek.classify.llm.router.ModelRouter._enforce_local_for_intimate`
    returns its input unchanged when the tier is ``None`` — so an
    ``intimate`` fragment compiled straight to whatever cloud provider the
    ``generation`` stage named.

    **Anti-vacuity.** This vault's config routes ``generation`` to a *cloud*
    provider and only ``default`` locally, and assertion (a) below pins that
    the *unrouted* resolution really would have picked ``anthropic``.
    Without it, "intimate routed to ollama" would pass for free on any
    config whose generation stage happens to be local already, and the test
    would prove nothing at all.

    Assertion (c) is the other half. ``_fragment_excerpt_for_prompt``
    redacts an intimate *body* but puts the title straight back as
    ``[Intimate-tier summary: <title>]``, and ``_build_prompt`` emits a bare
    ``title:`` line for every source unconditionally — so an intimate
    fragment's title is live egress payload, not a hypothetical. A provider
    name on its own would not show that anything sensitive was at stake.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv("CREEK_LLM", raising=False)
    intimate = _make_fragment(
        frag_id="frag-int",
        title=_INTIMATE_TITLE,
        privacy=PrivacyTier.INTIMATE,
    )
    _write_fragment_to_vault(vault, intimate, _INTIMATE_BODY)
    config_path = _write_routing_config(
        vault,
        default="ollama",
        generation="anthropic",
    )
    recorder = _RecordingDefaultLLM(
        _llm_response(
            claims=[
                {"id": "claim-001", "text": "A claim.", "fragment_ids": ["frag-int"]},
            ],
        ),
    )
    monkeypatch.setattr("creek.compile.engine.default_llm", recorder)

    # (a) ANTI-VACUITY: with no tier, this config resolves to the CLOUD.
    config = load_config(config_path)
    assert config.model_router.resolve("generation").provider == "anthropic"

    result = CliRunner().invoke(
        app,
        [
            "compile",
            "frag-int",
            "--vault",
            str(vault),
            "--target-kind",
            "thread",
            "--target-id",
            "thread-int",
            "--target-title",
            "Sessions",
        ],
    )

    assert result.exit_code == 0, result.stdout
    # (b) the tier was threaded, so the router redirected to the local default.
    assert recorder.provider_names == ["ollama"]
    # (c) and what stayed local is the real payload, not a placeholder.
    assert len(recorder.prompts) == 1
    assert _INTIMATE_TITLE in recorder.prompts[0]
    assert f"[Intimate-tier summary: {_INTIMATE_TITLE}]" in recorder.prompts[0]
    assert _INTIMATE_BODY not in recorder.prompts[0]


@pytest.mark.parametrize(
    ("sources", "expected_tier"),
    [
        ((("frag-a-int", PrivacyTier.INTIMATE),), PrivacyTier.INTIMATE),
        ((("frag-a-open", PrivacyTier.OPEN),), PrivacyTier.OPEN),
        (
            (
                ("frag-a-open", PrivacyTier.OPEN),
                ("frag-b-int", PrivacyTier.INTIMATE),
            ),
            PrivacyTier.INTIMATE,
        ),
    ],
    ids=["intimate-only", "open-only", "open-first-then-intimate"],
)
def test_compile_to_vault_keys_the_factory_with_the_max_source_tier(
    vault: Path,
    sources: tuple[tuple[str, PrivacyTier], ...],
    expected_tier: PrivacyTier,
) -> None:
    """The engine derives one routing tier from all sources, taking the maximum.

    ``compile_to_vault`` no longer accepts a pre-built ``llm``: it accepts a
    ``llm_factory`` and keys it with the most sensitive tier among the
    fragments it actually loaded. Only the engine has seen those fragments,
    so only the engine can compute the tier — the CLI knows nothing but an
    id list.

    The mixed row is the one that bites. Its ids are chosen so ``open``
    comes first in *both* the requested order and the vault's sorted walk
    order, which means a "tier of the first source" implementation returns
    ``OPEN`` and fails here, while a ``max`` implementation returns
    ``INTIMATE`` and passes. A single intimate fragment in a batch of a
    hundred open ones has to pin the whole call to the local model.

    The factory must also be invoked exactly once (a second build is a
    second provider handshake that could resolve differently) and never
    with ``None``, which
    :meth:`~creek.classify.llm.router.ModelRouter.resolve` treats as "apply
    no tier gate at all" — the precise shape of the #962 bug.
    """
    for frag_id, tier in sources:
        _write_fragment_to_vault(
            vault,
            _make_fragment(frag_id=frag_id, privacy=tier),
            f"body of {frag_id}",
        )
    factory = _TierRecordingFactory()

    compile_to_vault(
        fragment_ids=[frag_id for frag_id, _ in sources],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-tier",
        target_title="Tier",
        llm_factory=factory,
    )

    assert factory.tiers == [expected_tier]
    assert None not in factory.tiers


def test_compile_to_vault_fails_closed_when_no_fragments_are_requested(
    vault: Path,
) -> None:
    """An empty ``fragment_ids`` keys the factory ``INTIMATE``, not ``OPEN``.

    With nothing loaded there is no evidence about what the call would
    carry, and a ``max(..., default=...)`` reduction has to pick *something*
    for the empty case. The safe answer is the worst one, matching
    :func:`creek.classify.privacy_filter.max_source_tier`. Choosing ``OPEN``
    instead would be the classic fail-open reduction: an empty list is
    exactly what an id-typo request produces, and it must not buy cloud
    routing.
    """
    factory = _TierRecordingFactory()

    compile_to_vault(
        fragment_ids=[],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-empty",
        target_title="Empty",
        llm_factory=factory,
    )

    assert factory.tiers == [PrivacyTier.INTIMATE]


def test_compile_to_vault_fails_closed_when_privacy_tier_key_is_absent(
    vault: Path,
) -> None:
    """A fragment with no ``privacy_tier`` key at all routes ``INTIMATE``.

    The engine's routing tier is read from the *raw* frontmatter, not off
    the validated model, and this test is why. The first two assertions pin
    that the model alone cannot see the difference:
    :class:`~creek.models.Fragment` defaults a missing ``privacy_tier`` to
    ``unclassified``, which is emphatically not ``intimate`` — so an
    implementation that read ``fragment.privacy_tier`` would pass a test
    that merely asserted "some tier was computed" while routing this file
    to the cloud.

    A hand-edited or legacy fragment with no key carries even less
    assurance than a pipeline-written one that at least says
    ``unclassified`` out loud, so it fails all the way closed. Mirrors
    :func:`creek.classify.privacy_filter.fragment_tier`, which the MCP surface
    already applies to the same files.
    """
    path = _write_fragment_without_privacy_tier(vault, frag_id="frag-legacy")
    raw = frontmatter.load(str(path)).metadata
    assert "privacy_tier" not in raw
    assert Fragment.model_validate(raw).privacy_tier is PrivacyTier.UNCLASSIFIED

    factory = _TierRecordingFactory()
    compile_to_vault(
        fragment_ids=["frag-legacy"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-legacy",
        target_title="Legacy",
        llm_factory=factory,
    )

    assert factory.tiers == [PrivacyTier.INTIMATE]


def test_compile_to_vault_routes_on_a_parent_the_level_policy_dropped(
    vault: Path,
) -> None:
    """An intimate parent sets the routing tier after ``leaves`` drops it.

    The trap this test exists for: the routing tier must be derived from
    *every* loaded fragment, **before** ``_apply_level_policy`` runs. Under
    the default ``leaves`` policy a parent whose child is also in the set is
    dropped from the prompt pairs, so an implementation that derived the
    tier from the surviving pairs would see an ``open`` child alone and
    route this compile to the cloud.

    Assertion (ii) is what makes (i) necessary rather than merely paranoid.
    The dropped parent is not actually absent from the prompt: its *title*
    reappears as the child's ``structural_path:`` breadcrumb via
    :func:`creek.hierarchy.structural_path_context`, which walks
    ``parent_id`` through the pre-policy lookup whenever the persisted
    ``structural_path`` is empty. The intimate parent's title therefore
    reaches the prompt on a call whose only surviving source is ``open``.

    **#931 did not change this row, and assertion (ii) deliberately still
    asserts the title is present.** This test names *both* fragments, so
    #848's named-id gate already covers it on the MCP side and assertion (i)
    already covers it on the CLI side: the breadcrumb is safe here because
    the call is keyed ``INTIMATE`` and therefore never reaches a cloud
    provider — not because the breadcrumb was stripped. Retaining it is the
    ``creek compile`` operator's due; they own the vault. What #931 fixed is
    the case this test cannot see, where the caller names only the child and
    the breadcrumb comes from the *persisted* field with no ancestor loaded
    at all:
    ``test_compile_to_vault_routes_intimate_on_a_persisted_ancestor_not_named``.
    """
    parent = Fragment(
        id="frag-parent",
        title=_INTIMATE_TITLE,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level="document",
        child_ids=["frag-child"],
        privacy_tier=PrivacyTier.INTIMATE,
    )
    child = Fragment(
        id="frag-child",
        title="On grief",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level="paragraph",
        parent_id="frag-parent",
        privacy_tier=PrivacyTier.OPEN,
    )
    # The breadcrumb must come from the parent_id walk, not a persisted field.
    assert child.structural_path == []
    _write_fragment_to_vault(vault, parent, "the intimate parent body")
    _write_fragment_to_vault(vault, child, "the open child body")
    factory = _TierRecordingFactory()

    compile_to_vault(
        fragment_ids=["frag-parent", "frag-child"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-hier",
        target_title="Hier",
        llm_factory=factory,
    )

    # (i) the dropped parent still set the routing tier.
    assert factory.tiers == [PrivacyTier.INTIMATE]
    prompt = factory.prompts[0]
    # the policy really did drop it — its body is gone, the child's is not.
    assert "the intimate parent body" not in prompt
    assert "the open child body" in prompt
    # (ii) and yet its title egressed anyway, as the child's breadcrumb.
    assert f"structural_path: {_INTIMATE_TITLE}" in prompt


def _write_ancestry_pair(
    vault: Path,
    *,
    ancestor_tier: PrivacyTier,
    child_tier: PrivacyTier = PrivacyTier.PERSONAL,
) -> None:
    """Write an ancestor/child pair whose link is a *persisted* breadcrumb (#931).

    The child carries ``structural_path=[_INTIMATE_TITLE]``, so
    :func:`creek.hierarchy.structural_path_context` returns the ancestor's
    heading from its persisted-field branch — the branch that fires even
    when the ancestor is absent from the prompt builder's ``by_id`` lookup,
    which is exactly the case when the caller names only the child.

    Args:
        vault: Vault root.
        ancestor_tier: Tier of ``frag-ancestor``.
        child_tier: Tier of ``frag-child``.
    """
    ancestor = Fragment(
        id="frag-ancestor",
        title=_INTIMATE_TITLE,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level="document",
        child_ids=["frag-child"],
        privacy_tier=ancestor_tier,
    )
    child = Fragment(
        id="frag-child",
        title="On grief",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        created=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        ingested=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        level="paragraph",
        parent_id="frag-ancestor",
        structural_path=[_INTIMATE_TITLE],
        privacy_tier=child_tier,
    )
    # The breadcrumb must come from the PERSISTED field, not the by_id walk:
    # the caller never names the ancestor, so it is never in ``by_id``.
    assert child.structural_path == [_INTIMATE_TITLE]
    _write_fragment_to_vault(vault, ancestor, _INTIMATE_BODY)
    _write_fragment_to_vault(vault, child, "the personal child body")


def test_compile_to_vault_routes_intimate_on_a_persisted_ancestor_not_named(
    vault: Path,
) -> None:
    """An unnamed intimate ancestor sets the CLI's routing tier (#931).

    The ceiling-less ``creek compile`` CLI deliberately **keeps** the
    breadcrumb — its operator is the vault owner, and the orienting value of
    the ancestry is real — and escalates the *routing* tier instead, so the
    ancestor's heading only ever reaches a model through
    :class:`~creek.classify.llm.router.ModelRouter`'s ``Intimate``-never-cloud
    chokepoint (#647/#962). The MCP wrapper makes the opposite (and equally
    correct) choice for its caller: it refuses the whole call.

    Distinct from
    ``test_compile_to_vault_routes_on_a_parent_the_level_policy_dropped``,
    which names *both* fragments and so is already covered by the pre-#931
    reduction over loaded fragments. Here only the child is named, so the
    ancestor is never loaded, never in ``by_id``, and contributed nothing to
    the routing tier before this fix — while its heading egressed anyway.
    """
    _write_ancestry_pair(vault, ancestor_tier=PrivacyTier.INTIMATE)
    factory = _TierRecordingFactory()

    compile_to_vault(
        fragment_ids=["frag-child"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-anc",
        target_title="Anc",
        llm_factory=factory,
    )

    # (i) the unnamed ancestor set the routing tier.
    assert factory.tiers == [PrivacyTier.INTIMATE]
    # (ii) and the breadcrumb is deliberately retained, not redacted — which
    # is precisely why (i) has to hold.
    assert f"structural_path: {_INTIMATE_TITLE}" in factory.prompts[0]


def test_compile_to_vault_keeps_open_routing_for_a_within_tier_ancestor(
    vault: Path,
) -> None:
    """Ancestry ranking escalates only when the ancestry warrants it.

    Anti-vacuity for #931: with the ancestor at ``open`` and the child at
    ``open`` the call still routes ``OPEN``. Without this row the fix could
    be "route every fragment with a parent as INTIMATE" and the escalation
    test above would still pass.
    """
    _write_ancestry_pair(
        vault,
        ancestor_tier=PrivacyTier.OPEN,
        child_tier=PrivacyTier.OPEN,
    )
    factory = _TierRecordingFactory()

    compile_to_vault(
        fragment_ids=["frag-child"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-anc-open",
        target_title="Anc Open",
        llm_factory=factory,
    )

    assert factory.tiers == [PrivacyTier.OPEN]


def test_compile_to_vault_refuses_an_unnamed_intimate_ancestor_with_no_local_backend(
    vault: Path,
) -> None:
    """All-cloud config + an unnamed intimate ancestor raises instead of egressing.

    The #931 sibling of
    ``test_compile_to_vault_propagates_the_intimate_routing_refusal``: with
    ``default`` also cloud there is nowhere local to redirect to, so the
    escalated routing tier becomes a loud
    :class:`~creek.classify.llm.router.IntimateRoutingError` rather than a
    silent cloud call carrying the ancestor's heading.

    **Anti-vacuity.** The assertion before the ``raises`` block pins that the
    tier-less resolution on this same config is perfectly happy and hands
    back the cloud provider, so the raise can only come from the tier the
    engine derived. The trailing assertion pins that a refused compile leaves
    no page behind — ``_resolve_target_path`` ``mkdir``s unconditionally, so
    the gate has to win before it.
    """
    _write_ancestry_pair(vault, ancestor_tier=PrivacyTier.INTIMATE)
    router = ModelRouter(
        LLMRoutingConfig(
            default=LLMConfig(provider="anthropic"),
            generation=LLMConfig(provider="anthropic"),
        ),
    )
    # ANTI-VACUITY: with no tier this config resolves happily, to the CLOUD.
    assert router.resolve("generation").provider == "anthropic"

    with pytest.raises(IntimateRoutingError, match="cannot route to cloud provider"):
        compile_to_vault(
            fragment_ids=["frag-child"],
            vault_path=vault,
            target_kind="thread",
            target_id="thread-anc-refused",
            target_title="Refused",
            llm_factory=lambda tier: default_llm(router.resolve("generation", tier)),
        )

    assert not (vault / "02-Threads" / "Active" / "thread-anc-refused.md").exists()


def test_compile_to_vault_walks_the_vault_exactly_once(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ranking ancestry must not cost a second corpus walk (#931).

    ``iter_vault_fragments`` rglobs and parses every file under
    ``01-Fragments`` into a list before returning; at the 35k-fragment bar a
    second pass doubles the pre-LLM wall clock. The ancestor index is
    therefore built from the records ``_load_fragments_for_compile`` already
    walked, and this counter is what stops a future lane reintroducing the
    convenient-but-quadratic ``ancestry_tiers(vault_path, ...)`` call here.
    """
    _write_ancestry_pair(vault, ancestor_tier=PrivacyTier.INTIMATE)
    real = iter_vault_fragments
    calls: list[Path] = []

    def _counting(root: Path) -> list[tuple[Path, Fragment, str, dict[str, object]]]:
        """Record the walked root and delegate to the real loader."""
        calls.append(root)
        return real(root)

    monkeypatch.setattr("creek.compile.engine.iter_vault_fragments", _counting)

    compile_to_vault(
        fragment_ids=["frag-child"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-walks",
        target_title="Walks",
        llm_factory=_TierRecordingFactory(),
    )

    assert calls == [vault / "01-Fragments"]


def test_compile_to_vault_propagates_the_intimate_routing_refusal(
    vault: Path,
) -> None:
    """All-cloud config + intimate source raises instead of egressing (#962).

    When ``default`` is *also* cloud there is no local backend to redirect
    to, and :class:`~creek.classify.llm.router.IntimateRoutingError` is the
    router's loud refusal. Because the engine now calls the factory itself,
    that refusal has to travel out of ``compile_to_vault`` — the caller
    cannot pre-build a client and discover the problem earlier.

    **Anti-vacuity.** The assertion before the ``raises`` block pins that
    the tier-less resolution on this same config is perfectly happy and
    hands back the cloud provider. The raise below can therefore only come
    from the tier the engine threaded through, not from a config that was
    broken to begin with.

    The trailing assertion pins the ordering: the factory must be called
    after ``_load_fragments_for_compile`` but *before*
    ``_resolve_target_path``, which ``mkdir``s unconditionally. A refused
    compile leaves no page behind.
    """
    intimate = _make_fragment(
        frag_id="frag-int",
        title=_INTIMATE_TITLE,
        privacy=PrivacyTier.INTIMATE,
    )
    _write_fragment_to_vault(vault, intimate, _INTIMATE_BODY)
    router = ModelRouter(
        LLMRoutingConfig(
            default=LLMConfig(provider="anthropic"),
            generation=LLMConfig(provider="anthropic"),
        ),
    )
    # ANTI-VACUITY: with no tier this config resolves happily, to the CLOUD.
    assert router.resolve("generation").provider == "anthropic"

    with pytest.raises(IntimateRoutingError, match="cannot route to cloud provider"):
        compile_to_vault(
            fragment_ids=["frag-int"],
            vault_path=vault,
            target_kind="thread",
            target_id="thread-refused",
            target_title="Refused",
            llm_factory=lambda tier: default_llm(router.resolve("generation", tier)),
        )

    assert not (vault / "02-Threads" / "Active" / "thread-refused.md").exists()


def test_cli_compile_refuses_intimate_when_no_local_backend_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI turns the router's refusal into exit 1 and leaves no state.

    Uses a bare vault — only ``01-Fragments/Notes`` and the config dir — so
    ``02-Threads`` not existing afterwards is real evidence rather than an
    artefact of the shared fixture pre-creating it. That directory is
    created by ``_resolve_target_path``, so its absence pins the ordering:
    the ``llm_factory`` call happens *before* the first ``mkdir``, and a
    privacy refusal leaves nothing at all behind.

    **Anti-vacuity.** The assertion before the invocation pins that the
    tier-less resolution on this config succeeds and returns the cloud
    provider — so the non-zero exit can only be the tier gate firing, not a
    malformed config failing to load.

    The refusal text is pinned to the router's own wording because a
    missing-API-key ``RuntimeError`` would also produce a non-zero exit, and
    a bare exit-code assertion could not tell the two apart.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv("CREEK_LLM", raising=False)
    bare = tmp_path / "bare-vault"
    (bare / "01-Fragments" / "Notes").mkdir(parents=True)
    intimate = _make_fragment(
        frag_id="frag-int",
        title=_INTIMATE_TITLE,
        privacy=PrivacyTier.INTIMATE,
    )
    _write_fragment_to_vault(bare, intimate, _INTIMATE_BODY)
    config_path = _write_routing_config(
        bare,
        default="anthropic",
        generation="anthropic",
    )
    spy = _BuildProviderSpy()
    monkeypatch.setattr("creek.classify.llm.build_provider", spy)
    monkeypatch.setattr("creek.classify.llm.providers.build_provider", spy)

    # ANTI-VACUITY: with no tier this config resolves happily, to the CLOUD.
    config = load_config(config_path)
    assert config.model_router.resolve("generation").provider == "anthropic"

    result = CliRunner().invoke(
        app,
        [
            "compile",
            "frag-int",
            "--vault",
            str(bare),
            "--target-kind",
            "thread",
            "--target-id",
            "thread-refused",
            "--target-title",
            "Refused",
        ],
    )

    # Rich wraps long console lines; normalise whitespace before matching.
    stdout = " ".join(result.stdout.split())
    assert result.exit_code == 1, result.stdout
    assert "cannot route to cloud provider" in stdout
    assert "anthropic" in stdout
    # No provider was ever constructed, so nothing could have egressed.
    assert spy.configs == []
    # A refused compile writes no page AND creates no target directory.
    assert not (bare / "02-Threads").exists()
    assert not (bare / "00-Creek-Meta" / "Processing-Log").exists()
