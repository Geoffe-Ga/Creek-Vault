"""Migration: re-split merged AI-chat fragments into per-turn attributed ones.

Pre-split Claude/ChatGPT vaults fused a human and an AI turn into one
``source.author=self`` fragment, leaking AI prose into the voice corpus. The
opt-in re-split migration recovers the two turns from the stored body and
writes them as a human (``self``) fragment and a quarantined AI (``ai``,
``voice_weight=0.0``) fragment. It must be idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.ingest.base import ParsedFragment
from creek.ingest.claude import ClaudeIngestor
from creek.ingest.refresh import resplit_merged_ai_chat
from creek.models import Authorship, Fragment, FragmentSource, SourcePlatform
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from pathlib import Path


def _write_merged(
    vault: Path,
    *,
    platform: SourcePlatform,
    body: str,
    name: str,
) -> Path:
    """Write a pre-split merged AI-chat fragment (``author=self``) to the vault."""
    fragment = Fragment(
        id=f"frag-merged-{name}",
        title="Conversation (turn 0)",
        source=FragmentSource(
            platform=platform,
            author=Authorship.SELF,
            conversation_id="conv-1",
            original_file=f"/exports/{name}.json",
        ),
        created=datetime(2024, 11, 15, 10, 0, tzinfo=UTC),
    )
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    fragments_dir = vault / "01-Fragments" / "Conversations"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    target = fragments_dir / f"{name}.md"
    payload = fragment.model_dump(mode="json", exclude={"voice_proxy_eligible"})
    target.write_text(
        frontmatter.dumps(frontmatter.Post(body, **payload)),
        encoding="utf-8",
    )
    return target


_CLAUDE_BODY = "> How should I organize knowledge?\n\nMirror how you think about it."
_CHATGPT_BODY = (
    "# Test Chat\n\n> **User**: What is a fragment?\n>\n"
    "> **Assistant**: An atomic unit of content."
)


def _eligible_by_author(vault: Path) -> dict[str, list[Fragment]]:
    """Group voice-loaded fragments by ``source.author`` value."""
    groups: dict[str, list[Fragment]] = {}
    for _path, fragment, _body, _raw in iter_vault_fragments(vault / "01-Fragments"):
        groups.setdefault(str(fragment.source.author), []).append(fragment)
    return groups


def test_claude_merged_fragment_is_resplit(tmp_path: Path) -> None:
    """A merged Claude fragment becomes a self human + ai assistant pair."""
    vault = tmp_path / "vault"
    merged = _write_merged(
        vault,
        platform=SourcePlatform.CLAUDE,
        body=_CLAUDE_BODY,
        name="claude_turn",
    )

    result = resplit_merged_ai_chat(vault)

    assert result.resplit == 1
    assert not merged.exists()
    by_author = _eligible_by_author(vault)
    assert len(by_author.get("self", [])) == 1
    assert len(by_author.get("ai", [])) == 1
    ai_fragment = by_author["ai"][0]
    assert ai_fragment.voice_weight == 0.0
    assert ai_fragment.voice_proxy_eligible is False
    human_fragment = by_author["self"][0]
    assert human_fragment.voice_proxy_eligible is True
    # Provenance preserved for threading.
    assert ai_fragment.source.conversation_id == "conv-1"


def test_chatgpt_merged_fragment_is_resplit(tmp_path: Path) -> None:
    """A merged ChatGPT fragment becomes a self human + ai assistant pair."""
    vault = tmp_path / "vault"
    _write_merged(
        vault,
        platform=SourcePlatform.CHATGPT,
        body=_CHATGPT_BODY,
        name="chatgpt_turn",
    )

    result = resplit_merged_ai_chat(vault)

    assert result.resplit == 1
    by_author = _eligible_by_author(vault)
    assert len(by_author.get("self", [])) == 1
    assert len(by_author.get("ai", [])) == 1


def test_resplit_is_idempotent(tmp_path: Path) -> None:
    """Running the migration twice yields the same vault state as once."""
    vault = tmp_path / "vault"
    _write_merged(
        vault,
        platform=SourcePlatform.CLAUDE,
        body=_CLAUDE_BODY,
        name="claude_turn",
    )

    first = resplit_merged_ai_chat(vault)
    after_first = sorted(p.name for p in (vault / "01-Fragments").rglob("*.md"))
    second = resplit_merged_ai_chat(vault)
    after_second = sorted(p.name for p in (vault / "01-Fragments").rglob("*.md"))

    assert first.resplit == 1
    assert second.resplit == 0
    assert after_first == after_second


def test_native_fragment_is_untouched(tmp_path: Path) -> None:
    """A non-AI-chat fragment is never re-split."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    fragments_dir = vault / "01-Fragments" / "Writing"
    fragments_dir.mkdir(parents=True)
    fragment = Fragment(
        id="frag-native",
        title="Essay",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN, author=Authorship.SELF),
        created=datetime(2024, 1, 1, tzinfo=UTC),
    )
    target = fragments_dir / "essay.md"
    target.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                "Plain essay prose.",
                **fragment.model_dump(mode="json", exclude={"voice_proxy_eligible"}),
            ),
        ),
        encoding="utf-8",
    )

    result = resplit_merged_ai_chat(vault)

    assert result.resplit == 0
    assert result.skipped == 1
    assert target.exists()


def test_already_split_human_fragment_is_skipped(tmp_path: Path) -> None:
    """An already-split (all-blockquote) human Claude fragment is not re-split."""
    vault = tmp_path / "vault"
    _write_merged(
        vault,
        platform=SourcePlatform.CLAUDE,
        body="> Just my own words, fully quoted, no assistant turn.",
        name="already_split",
    )

    result = resplit_merged_ai_chat(vault)

    assert result.resplit == 0
    assert result.skipped == 1


def test_merged_run_human_fragment_is_skipped(tmp_path: Path) -> None:
    """A human fragment holding a merged message run is not re-split (#1333).

    Consecutive human messages now merge into one turn separated by a blank
    line, so a human fragment body is no longer a single paragraph — and
    ``_parse_claude_merged`` reads any *non-blank* line that escapes the
    blockquote as the assistant's prose. (A blank separator is safe whether
    or not it carries the ``>``; the parser drops blank lines from that
    bucket. A non-blank one is not.) So the guarantee this test pins is
    that ``ClaudeIngestor.convert_to_markdown`` blockquotes **every** line
    of a multi-paragraph human turn. The body is produced by that method
    rather than hand-written, so a future change that lets any line through
    unprefixed fails here instead of silently making the migration
    destructive: it would otherwise split this fragment and re-attribute
    the second half of the operator's own words to the AI.
    """
    vault = tmp_path / "vault"
    merged_run_text = "First half of my thought\n\nSecond half of my thought"
    body = ClaudeIngestor().convert_to_markdown(
        ParsedFragment(
            content=merged_run_text,
            metadata={"turn_text": merged_run_text, "author_role": "self"},
            source_path="/exports/claude.json",
            timestamp=datetime(2024, 11, 15, 10, 0, tzinfo=UTC),
        ),
    )
    # Guard the precondition the safety argument rests on.
    assert all(line.startswith(">") for line in body.split("\n"))
    target = _write_merged(
        vault,
        platform=SourcePlatform.CLAUDE,
        body=body,
        name="merged_run",
    )

    result = resplit_merged_ai_chat(vault)

    assert result.resplit == 0
    assert result.skipped == 1
    assert target.exists()
    assert target.read_text(encoding="utf-8").endswith(body)


def test_empty_vault_is_unchanged(tmp_path: Path) -> None:
    """A vault with no fragments tree yields an empty, error-free result."""
    result = resplit_merged_ai_chat(tmp_path / "vault")
    assert result.scanned == 0
    assert result.resplit == 0
    assert result.errors == []


def test_cli_refresh_ai_chat_runs_migration(tmp_path: Path) -> None:
    """``creek ingest --refresh-ai-chat`` re-splits merged fragments."""
    from typer.testing import CliRunner

    from creek.cli import app

    vault = tmp_path / "vault"
    _write_merged(
        vault,
        platform=SourcePlatform.CLAUDE,
        body=_CLAUDE_BODY,
        name="claude_turn",
    )

    result = CliRunner().invoke(
        app,
        ["ingest", "--refresh-ai-chat", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "Re-split 1" in result.output
    by_author = _eligible_by_author(vault)
    assert len(by_author.get("ai", [])) == 1
