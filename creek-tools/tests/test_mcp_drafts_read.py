"""``creek.drafts.read``: hand a saved draft or mission note back to the caller.

The tool reads only ``07-Voice/Drafts/*.md`` and ``07-Voice/Mission/*.md``,
admits a document by its **own** ``privacy_tier`` stamp (an unstamped document
counts as intimate, the ``creek.state.read`` rule), and returns the body
without its frontmatter, which carries prompts and provenance.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.drafts_read import (
    MAX_BODY_CHARS,
    PATH_REASON,
    TOO_LARGE_REASON,
    TOOL_NAME,
    drafts_read_tool,
)

if TYPE_CHECKING:
    from pathlib import Path


def _doc(
    vault: Path,
    rel: str,
    *,
    tier: str | None,
    title: str = "A title",
    body: str = "# Heading\n\nThe body.\n",
    extra: str = "",
) -> Path:
    """Write a markdown document with optional ``privacy_tier`` frontmatter."""
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = "" if tier is None else f"privacy_tier: {tier}\n"
    target.write_text(
        f"---\ntype: draft\ntitle: {title}\n{stamp}{extra}---\n{body}",
        encoding="utf-8",
    )
    return target


# --- read --------------------------------------------------------------------


def test_reads_a_stamped_draft_body_without_its_frontmatter(tmp_path: Path) -> None:
    """An admitted draft comes back as its body, title and kind, no frontmatter."""
    _doc(
        tmp_path,
        "07-Voice/Drafts/2026-11-09-essay.md",
        tier="open",
        title="Why Skills Stick",
        extra="prompt: SECRET PROMPT TEXT\n",
    )

    result = drafts_read_tool(
        vault_path=tmp_path, path="07-Voice/Drafts/2026-11-09-essay.md"
    )

    assert result == {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": "open",
        "path": "07-Voice/Drafts/2026-11-09-essay.md",
        "kind": "draft",
        "title": "Why Skills Stick",
        "markdown": "# Heading\n\nThe body.",
    }
    assert "SECRET PROMPT TEXT" not in json.dumps(result)


def test_reads_a_mission_note(tmp_path: Path) -> None:
    """Mission notes are readable and reported as kind ``mission``."""
    _doc(tmp_path, "07-Voice/Mission/2026-11-10-mission.md", tier="personal")

    result = drafts_read_tool(
        vault_path=tmp_path,
        path="07-Voice/Mission/2026-11-10-mission.md",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert result["status"] == "ok"
    assert result["kind"] == "mission"


@pytest.mark.parametrize(
    ("tier", "ceiling"),
    [
        ("personal", TierCeiling.OPEN),
        ("intimate", TierCeiling.PERSONAL),
        (None, TierCeiling.PERSONAL),
        ("not-a-tier", TierCeiling.PERSONAL),
    ],
)
def test_refuses_a_document_above_the_ceiling_without_naming_its_tier(
    tmp_path: Path, tier: str | None, ceiling: TierCeiling
) -> None:
    """Above-ceiling, unstamped and unrecognised stamps all get one generic refusal."""
    _doc(tmp_path, "07-Voice/Drafts/d.md", tier=tier, body="PRIVATE BODY\n")

    result = drafts_read_tool(
        vault_path=tmp_path, path="07-Voice/Drafts/d.md", privacy_tier_ceiling=ceiling
    )

    assert result["status"] == "refused"
    assert result["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert "PRIVATE BODY" not in json.dumps(result)
    assert "intimate" not in result["reason"]


def test_ceiling_all_admits_an_unstamped_document(tmp_path: Path) -> None:
    """Nothing is permanently unreachable: ``all`` reads an unstamped draft."""
    _doc(tmp_path, "07-Voice/Drafts/legacy.md", tier=None)

    result = drafts_read_tool(
        vault_path=tmp_path,
        path="07-Voice/Drafts/legacy.md",
        privacy_tier_ceiling=TierCeiling.ALL,
    )

    assert result["status"] == "ok"


@pytest.mark.parametrize(
    "path",
    [
        "07-Voice/Drafts/../../00-Creek-Meta/creek_config.yaml",
        "07-Voice/Drafts/../Mission/x.md",
        "01-Fragments/Notes/frag.md",
        "07-Voice/Lexicon/words.md",
        "/etc/passwd",
        "07-Voice/Drafts/sub/nested.md",
        "07-Voice/Drafts/notes.txt",
        "07-Voice/Drafts",
        "",
        "07-Voice\\Drafts\\x.md",
        "07-Voice/Drafts/" + "a" * 600 + ".md",
    ],
)
def test_refuses_any_path_outside_the_two_folders(tmp_path: Path, path: str) -> None:
    """Only a ``.md`` file directly under Drafts or Mission can be named."""
    _doc(tmp_path, "00-Creek-Meta/creek_config.yaml", tier="open")

    result = drafts_read_tool(
        vault_path=tmp_path, path=path, privacy_tier_ceiling=TierCeiling.ALL
    )

    assert result["status"] == "refused"
    assert result["reason"] == PATH_REASON


def test_a_missing_document_is_not_found(tmp_path: Path) -> None:
    """A missing document gets the above-ceiling refusal, byte for byte.

    Draft file names are slugs of their titles, so a distinct "missing" answer
    would let a caller confirm that an above-ceiling draft on a guessed topic
    exists without reading it.
    """
    result = drafts_read_tool(vault_path=tmp_path, path="07-Voice/Drafts/none.md")

    assert result["status"] == "refused"
    assert result["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert "markdown" not in result


def test_missing_and_above_ceiling_answers_are_identical(tmp_path: Path) -> None:
    """Probing a guessed file name reveals nothing about whether it exists."""
    _doc(tmp_path, "07-Voice/Drafts/2026-11-10-private-topic.md", tier="intimate")

    present = drafts_read_tool(
        vault_path=tmp_path, path="07-Voice/Drafts/2026-11-10-private-topic.md"
    )
    absent = drafts_read_tool(
        vault_path=tmp_path, path="07-Voice/Drafts/2026-11-10-other-topic.md"
    )

    assert present == absent


@pytest.mark.parametrize(
    "header",
    ["1: one\n", "true: yes\n", "2024-05-01: dated\n"],
)
def test_a_non_string_frontmatter_key_degrades_instead_of_crashing(
    tmp_path: Path, header: str
) -> None:
    """A key YAML reads as an int, bool or date makes ``frontmatter`` raise TypeError.

    The tier stamp is then unreadable, so the document ranks intimate and only
    ``ceiling=all`` reaches it; there it reads back whole, and lists by its
    file name, rather than crashing the tool.
    """
    target = tmp_path / "07-Voice" / "Drafts" / "2026-11-10-odd.md"
    target.parent.mkdir(parents=True)
    target.write_text(f"---\n{header}title: Odd\n---\nBody.\n", encoding="utf-8")

    read = drafts_read_tool(
        vault_path=tmp_path,
        path="07-Voice/Drafts/2026-11-10-odd.md",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    listed = drafts_read_tool(vault_path=tmp_path, privacy_tier_ceiling=TierCeiling.ALL)

    assert read["status"] == "ok"
    assert read["title"] == "2026-11-10-odd"
    assert "Body." in read["markdown"]
    assert listed["documents"] == [
        {
            "path": "07-Voice/Drafts/2026-11-10-odd.md",
            "kind": "draft",
            "title": "2026-11-10-odd",
        }
    ]


def test_a_symlinked_document_is_not_followed(tmp_path: Path) -> None:
    """A symlink inside Drafts that points elsewhere is treated as absent."""
    secret = _doc(tmp_path, "01-Fragments/secret.md", tier="open", body="SECRET\n")
    link = tmp_path / "07-Voice" / "Drafts" / "link.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)

    result = drafts_read_tool(
        vault_path=tmp_path,
        path="07-Voice/Drafts/link.md",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    listed = drafts_read_tool(vault_path=tmp_path, privacy_tier_ceiling=TierCeiling.ALL)

    assert result["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert "SECRET" not in json.dumps(result)
    assert listed["documents"] == []


def test_a_symlinked_drafts_folder_is_not_followed(tmp_path: Path) -> None:
    """``07-Voice/Drafts`` itself pointing outside the vault reads nothing."""
    outside = tmp_path / "outside"
    _doc(outside, "x.md", tier="open", body="OUTSIDE\n")
    vault = tmp_path / "vault"
    (vault / "07-Voice").mkdir(parents=True)
    (vault / "07-Voice" / "Drafts").symlink_to(outside)

    read = drafts_read_tool(
        vault_path=vault,
        path="07-Voice/Drafts/x.md",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    listed = drafts_read_tool(vault_path=vault, privacy_tier_ceiling=TierCeiling.ALL)

    assert read["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert listed["documents"] == []


def test_refuses_a_body_too_large_to_return(tmp_path: Path) -> None:
    """An admitted body over the cap is refused rather than truncated silently."""
    _doc(
        tmp_path,
        "07-Voice/Drafts/big.md",
        tier="open",
        body="x" * (MAX_BODY_CHARS + 1),
    )

    result = drafts_read_tool(vault_path=tmp_path, path="07-Voice/Drafts/big.md")

    assert result["status"] == "refused"
    assert result["reason"] == TOO_LARGE_REASON


def test_the_audit_log_records_no_path(tmp_path: Path) -> None:
    """The trail records that a path was asked for, never which one."""
    _doc(tmp_path, "07-Voice/Drafts/probe-target.md", tier="intimate")

    drafts_read_tool(vault_path=tmp_path, path="07-Voice/Drafts/probe-target.md")

    audit = (tmp_path / "00-Creek-Meta" / "audit" / "mcp.jsonl").read_text()
    assert TOOL_NAME in audit
    assert "probe-target" not in audit
    assert '"has_path": true' in audit


# --- list --------------------------------------------------------------------


def test_lists_only_documents_within_the_ceiling(tmp_path: Path) -> None:
    """The listing names admitted documents, newest file name first."""
    _doc(tmp_path, "07-Voice/Drafts/2026-11-08-a.md", tier="open", title="Older")
    _doc(tmp_path, "07-Voice/Drafts/2026-11-09-b.md", tier="open", title="Newer")
    _doc(tmp_path, "07-Voice/Drafts/2026-11-09-c.md", tier="intimate", title="Hidden")
    _doc(tmp_path, "07-Voice/Drafts/2026-11-09-d.md", tier=None, title="Legacy")
    _doc(tmp_path, "07-Voice/Mission/2026-11-10-m.md", tier="open", title="Mission")
    _doc(tmp_path, "07-Voice/Lexicon/words.md", tier="open", title="Lexicon")

    result = drafts_read_tool(vault_path=tmp_path)

    assert result["status"] == "ok"
    assert result["documents"] == [
        {
            "path": "07-Voice/Mission/2026-11-10-m.md",
            "kind": "mission",
            "title": "Mission",
        },
        {"path": "07-Voice/Drafts/2026-11-09-b.md", "kind": "draft", "title": "Newer"},
        {"path": "07-Voice/Drafts/2026-11-08-a.md", "kind": "draft", "title": "Older"},
    ]
    assert "Hidden" not in json.dumps(result)


def test_an_empty_listing_is_empty(tmp_path: Path) -> None:
    """No admitted documents answers ``empty`` with an empty list."""
    result = drafts_read_tool(vault_path=tmp_path)

    assert result["status"] == "empty"
    assert result["documents"] == []


# --- stamping what MCP writes ------------------------------------------------


@pytest.mark.parametrize(
    ("ceiling", "stamp"),
    [
        ("open", "open"),
        ("personal", "personal"),
        ("intimate", "intimate"),
        ("all", "intimate"),
        (None, "intimate"),
        ("bogus", "intimate"),
    ],
)
def test_a_saved_desk_draft_is_stamped_with_the_tier_it_was_written_under(
    tmp_path: Path, ceiling: str | None, stamp: str
) -> None:
    """``creek.author``'s saved draft carries ``privacy_tier``, failing closed."""
    import frontmatter

    from creek_mcp.tools.author import save_authored_draft

    response = {
        "medium": "essay",
        "body": "# T\n\nbody",
        "verdict": "PASS",
        "rounds": 1,
        "claims": [],
    }
    if ceiling is not None:
        response["tier_ceiling"] = ceiling

    saved = frontmatter.load(save_authored_draft(tmp_path, response))

    assert saved["privacy_tier"] == stamp


def test_a_desk_draft_written_at_open_is_readable_at_open(tmp_path: Path) -> None:
    """End to end: what an open-ceiling job saved, an open caller can read."""
    from creek_mcp.tools.author import save_authored_draft

    response = {
        "medium": "essay",
        "tier_ceiling": "open",
        "body": "# Skills\n\nThe essay.",
        "verdict": "PASS",
        "rounds": 1,
        "claims": [],
    }
    saved = save_authored_draft(tmp_path, response)

    result = drafts_read_tool(
        vault_path=tmp_path, path=str(saved.relative_to(tmp_path))
    )

    assert result["status"] == "ok"
    assert result["title"] == "Skills"
    assert result["markdown"] == "# Skills\n\nThe essay."
