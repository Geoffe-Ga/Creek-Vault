"""``creek.mission`` MCP tool — a personal mission and values document for Reconcile.

The wrapper admits the corpus under the caller's ceiling, routes the one model
turn by the more sensitive of that ceiling and the sources' own tiers, and
answers with the saved note and its paste-ready Markdown. Every refusal is a
fixed reason that names no fragment, tier or vendor text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest

from creek.classify.llm.providers import (
    REQUEST_KEY_REJECTED_REASON,
    RequestKeyRejectedError,
)
from creek.classify.llm.router import IntimateRoutingError
from creek.generate.mission import MAX_MISSION_LIMIT, MISSION_SUBDIR
from creek.models import PrivacyTier
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.provenance import InferenceProvenance
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.mission import (
    MISSION_PROVIDER_FAILED_REASON,
    MISSION_PROVIDER_UNAVAILABLE_REASON,
    MISSION_UNREADABLE_REASON,
    TOOL_NAME,
    mission_tool,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_OPEN_WORDS = "I believe in keeping the promises I make to the quiet ones."
_PERSONAL_WORDS = "I value the slow repair of trust after it breaks at home."
_INTIMATE_WORDS = "SENTINEL-INTIMATE-MISSION what I value most in the dark."
_VENDOR_TEXT = "vendor-said-something-secret https://internal.example"

_ANSWER = json.dumps(
    {
        "title": "What I stand for",
        "mission": "I keep my word and make room for the quiet ones.",
        "values": [
            {"name": "Honesty", "statement": "I say what I mean."},
            {"name": "Care", "statement": "I check on people first."},
            {"name": "Patience", "statement": "I let repair take time."},
        ],
    }
)


def _write_fragment(
    vault: Path, frag_id: str, body: str, tier: str, *, title: str | None = None
) -> None:
    """Write one self-authored fragment under ``01-Fragments/Notes``."""
    when = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": title or f"Note {frag_id}",
        "created": when,
        "ingested": when,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": tier,
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a vault with one open, one personal and one intimate value note."""
    _write_fragment(tmp_path, "frag-open", _OPEN_WORDS, "open")
    _write_fragment(tmp_path, "frag-personal", _PERSONAL_WORDS, "personal")
    _write_fragment(tmp_path, "frag-intimate", _INTIMATE_WORDS, "intimate")
    return tmp_path


class _Factory:
    """A tier-keyed factory recording the tiers, budgets and prompts it saw."""

    def __init__(
        self,
        answer: str = _ANSWER,
        *,
        provenance: InferenceProvenance | None = None,
        build_error: Exception | None = None,
        call_error: Exception | None = None,
    ) -> None:
        """Answer with *answer*, or raise *build_error* / *call_error*."""
        self.tiers: list[PrivacyTier] = []
        self.budgets: list[int] = []
        self.prompts: list[str] = []
        self._answer = answer
        self._provenance = provenance
        self._build_error = build_error
        self._call_error = call_error

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> Callable[[str], str]:
        """Record the tier and budget, then return the completion callable."""
        self.tiers.append(tier)
        self.budgets.append(max_tokens)
        if self._build_error is not None:
            raise self._build_error
        if self._provenance is not None:
            self._provenance.record("local")
        return self._complete

    def _complete(self, prompt: str) -> str:
        """Record the prompt and answer."""
        self.prompts.append(prompt)
        if self._call_error is not None:
            raise self._call_error
        return self._answer


def _mission_files(vault: Path) -> list[Path]:
    """Return every saved mission note."""
    folder = vault / MISSION_SUBDIR
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


def _audit_rows(vault: Path) -> list[dict[str, Any]]:
    """Return the parsed MCP audit rows."""
    log = vault / MCP_AUDIT_RELPATH
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_mission_answers_with_the_saved_paste_ready_document(vault: Path) -> None:
    """The envelope carries the note's path, its title and its exact body."""
    factory = _Factory()

    response = mission_tool(vault_path=vault, llm_factory=factory)

    assert response["status"] == "ok"
    assert response["tool"] == TOOL_NAME
    assert response["tier_ceiling"] == "open"
    assert response["title"] == "What I stand for"
    assert response["source_fragments"] == ["frag-open"]
    assert response["source_count"] == 1
    assert response["value_count"] == 3
    saved = vault / response["mission_path"]
    assert saved.parent == vault / MISSION_SUBDIR
    assert frontmatter.load(saved).content + "\n" == response["markdown"]
    assert response["markdown"].startswith("## Mission\nI keep my word")


def test_mission_response_never_names_the_routing_tier(vault: Path) -> None:
    """The derived tier is a routing signal and stays inside the tool."""
    response = mission_tool(
        vault_path=vault,
        llm_factory=_Factory(),
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert set(response) == {
        "status",
        "tool",
        "tier_ceiling",
        "mission_path",
        "title",
        "markdown",
        "source_fragments",
        "source_count",
        "value_count",
    }


def test_mission_leaves_above_ceiling_fragments_out_of_the_prompt(
    vault: Path,
) -> None:
    """At ``open`` neither the personal nor the intimate note reaches the model."""
    factory = _Factory()

    mission_tool(vault_path=vault, llm_factory=factory)

    (prompt,) = factory.prompts
    assert _OPEN_WORDS in prompt
    assert "frag-personal" not in prompt
    assert _PERSONAL_WORDS not in prompt
    assert "SENTINEL-INTIMATE-MISSION" not in prompt


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [
        (TierCeiling.OPEN, PrivacyTier.OPEN),
        (TierCeiling.PERSONAL, PrivacyTier.PERSONAL),
        (TierCeiling.ALL, PrivacyTier.INTIMATE),
    ],
)
def test_mission_routes_by_the_more_sensitive_of_ceiling_and_sources(
    vault: Path, ceiling: TierCeiling, expected: PrivacyTier
) -> None:
    """The tier keys the router; an all-tier mission is intimate and stays local."""
    factory = _Factory()

    mission_tool(vault_path=vault, llm_factory=factory, privacy_tier_ceiling=ceiling)

    assert factory.tiers == [expected]


def test_mission_routes_by_the_sources_even_under_a_broad_ceiling(
    tmp_path: Path,
) -> None:
    """A personal source under a personal ceiling routes personal."""
    _write_fragment(tmp_path, "frag-personal", _PERSONAL_WORDS, "personal")
    factory = _Factory()

    mission_tool(
        vault_path=tmp_path,
        llm_factory=factory,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert factory.tiers == [PrivacyTier.PERSONAL]
    assert _PERSONAL_WORDS in factory.prompts[0]


def test_mission_bounds_the_model_output(vault: Path) -> None:
    """The one turn has a hard output ceiling."""
    factory = _Factory()

    mission_tool(vault_path=vault, llm_factory=factory)

    assert factory.budgets == [2048]


def test_mission_of_an_empty_vault_builds_no_provider(tmp_path: Path) -> None:
    """With nothing to draw on, the answer is ``empty`` and no model is reached."""
    factory = _Factory()

    response = mission_tool(vault_path=tmp_path, llm_factory=factory)

    assert response["status"] == "empty"
    assert response["tool"] == TOOL_NAME
    assert factory.tiers == []
    assert _mission_files(tmp_path) == []


@pytest.mark.parametrize("limit", [0, MAX_MISSION_LIMIT + 1])
def test_mission_refuses_a_limit_out_of_range(vault: Path, limit: int) -> None:
    """A limit outside 1..40 is refused before the corpus is read."""
    factory = _Factory()

    response = mission_tool(vault_path=vault, llm_factory=factory, limit=limit)

    assert response["status"] == "refused"
    assert response["reason"] == f"limit must be between 1 and {MAX_MISSION_LIMIT}"
    assert factory.tiers == []


def test_mission_audits_the_call_without_its_tier(vault: Path) -> None:
    """The audit row carries the limit and the caller's own ceiling only."""
    mission_tool(vault_path=vault, llm_factory=_Factory(), limit=5, consumer="tester")

    (row,) = _audit_rows(vault)
    assert row["tool"] == TOOL_NAME
    assert "limit" in json.dumps(row["args_summary"])
    assert row["tier_ceiling"] == "open"
    assert row["consumer"] == "tester"
    assert "personal" not in json.dumps(row)
    assert "frag-open" not in json.dumps(row)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError(_VENDOR_TEXT), MISSION_PROVIDER_UNAVAILABLE_REASON),
        (OSError(_VENDOR_TEXT), MISSION_PROVIDER_UNAVAILABLE_REASON),
        (RequestKeyRejectedError(_VENDOR_TEXT), REQUEST_KEY_REJECTED_REASON),
    ],
)
def test_mission_refuses_a_provider_it_cannot_build(
    vault: Path, error: Exception, reason: str
) -> None:
    """A provider that cannot be built is a fixed refusal; nothing is saved."""
    response = mission_tool(vault_path=vault, llm_factory=_Factory(build_error=error))

    assert response["status"] == "refused"
    assert response["reason"] == reason
    assert _VENDOR_TEXT not in json.dumps(response)
    assert _mission_files(vault) == []


def test_mission_passes_the_routers_intimate_refusal_through(vault: Path) -> None:
    """The router's own message is the one residual draft accepts too."""
    error = IntimateRoutingError("anthropic")

    response = mission_tool(
        vault_path=vault,
        llm_factory=_Factory(build_error=error),
        privacy_tier_ceiling=TierCeiling.ALL,
    )

    assert response["status"] == "refused"
    assert response["reason"] == str(error)


def test_mission_refuses_a_failed_call_without_vendor_text(vault: Path) -> None:
    """A provider that was built but failed is a different fixed refusal."""
    response = mission_tool(
        vault_path=vault,
        llm_factory=_Factory(call_error=RuntimeError(_VENDOR_TEXT)),
    )

    assert response["status"] == "refused"
    assert response["reason"] == MISSION_PROVIDER_FAILED_REASON
    assert _VENDOR_TEXT not in json.dumps(response)
    assert _mission_files(vault) == []


def test_mission_refuses_an_answer_it_cannot_read(vault: Path) -> None:
    """An answer with no mission in it is refused and nothing is saved."""
    response = mission_tool(vault_path=vault, llm_factory=_Factory("I'd rather not."))

    assert response["status"] == "refused"
    assert response["reason"] == MISSION_UNREADABLE_REASON
    assert _mission_files(vault) == []


def test_mission_refuses_a_vault_config_that_cannot_load(vault: Path) -> None:
    """A broken config is refused by type name before any provider is built."""
    config = vault / "00-Creek-Meta" / "creek_config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("draft: [unclosed\n", encoding="utf-8")
    factory = _Factory()

    response = mission_tool(vault_path=vault, llm_factory=factory)

    assert response["status"] == "refused"
    assert response["reason"].startswith("mission unavailable: ")
    assert str(config) not in response["reason"]
    assert factory.tiers == []


def test_mission_stamps_and_echoes_the_recorded_route(vault: Path) -> None:
    """The route label the factory recorded is echoed and saved; never a key."""
    provenance = InferenceProvenance()

    response = mission_tool(
        vault_path=vault,
        llm_factory=_Factory(provenance=provenance),
        provenance=provenance,
    )

    assert response["inference_source"] == "local"
    saved = frontmatter.load(vault / response["mission_path"])
    assert saved["inference_source"] == "local"


def test_mission_records_no_route_it_did_not_resolve(vault: Path) -> None:
    """An injected factory records nothing, so nothing is stamped or echoed."""
    response = mission_tool(
        vault_path=vault,
        llm_factory=_Factory(),
        provenance=InferenceProvenance(),
    )

    assert "inference_source" not in response
    saved = frontmatter.load(vault / response["mission_path"])
    assert "inference_source" not in saved.metadata
