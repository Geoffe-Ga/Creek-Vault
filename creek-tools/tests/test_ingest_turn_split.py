"""Per-turn attribution for AI-chat ingests (FEAT #553, epic #551).

Proves the core fix: ingesting a Claude or ChatGPT conversation emits the
human turn and the AI turn as *separately attributed* fragments — the human
turn is the owner's voice (``source.author=self``, full ``voice_weight``) and
the AI turn is AI-authored (``source.author=ai``, ``voice_weight=0.0``) so it
can never feed the voice proxy. Also proves the diagnostic effect: a freshly
ingested chat no longer leaks AI prose into the voice corpus.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frontmatter

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import _eligible_texts
from creek.generate.voice_authenticity import build_voice_authenticity_report
from creek.ingest.base import RawDocument, assemble_ingested_fragment
from creek.ingest.chatgpt import ChatGPTIngestor
from creek.ingest.claude import ClaudeIngestor
from creek.models import Authorship, Fragment

if TYPE_CHECKING:
    from pathlib import Path

    from creek.ingest.base import Ingestor


# --- Fixture builders -------------------------------------------------------


def _claude_export(turns: list[tuple[str, str]]) -> bytes:
    """Build a one-conversation Claude export from (human, assistant) turns."""
    messages: list[dict[str, object]] = []
    for human, assistant in turns:
        messages.append(
            {"role": "human", "content": human, "created_at": "2024-11-15T10:00:00Z"}
        )
        messages.append(
            {
                "role": "assistant",
                "content": assistant,
                "created_at": "2024-11-15T10:00:15Z",
            }
        )
    conv = {
        "uuid": "conv-553",
        "name": "Voice attribution chat",
        "created_at": "2024-11-15T10:00:00Z",
        "model": "claude-3-opus-20240229",
        "messages": messages,
    }
    return json.dumps({"conversations": [conv]}).encode("utf-8")


def _chatgpt_export(turns: list[tuple[str, str]]) -> bytes:
    """Build a one-conversation ChatGPT export from (user, assistant) turns."""
    mapping: dict[str, object] = {
        "root": {"id": "root", "message": None, "parent": None, "children": ["sys"]},
        "sys": {
            "id": "sys",
            "message": {
                "id": "sys",
                "author": {"role": "system"},
                "content": {"content_type": "text", "parts": ["System."]},
                "create_time": 1700000000.0,
            },
            "parent": "root",
            "children": [],
        },
    }
    prev = "sys"
    base = 1700000010.0
    for idx, (user, assistant) in enumerate(turns):
        uid, aid = f"u{idx}", f"a{idx}"
        mapping[prev]["children"] = [uid]  # type: ignore[index]
        mapping[uid] = {
            "id": uid,
            "message": {
                "id": uid,
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [user]},
                "create_time": base + idx * 100,
            },
            "parent": prev,
            "children": [aid],
        }
        mapping[aid] = {
            "id": aid,
            "message": {
                "id": aid,
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [assistant]},
                "create_time": base + idx * 100 + 10,
            },
            "parent": uid,
            "children": [],
        }
        prev = aid
    conv = {"title": "Voice chat", "create_time": 1700000000.0, "mapping": mapping}
    return json.dumps([conv]).encode("utf-8")


def _ingest_fragments(
    ingestor: Ingestor,
    export: bytes,
    filename: str,
) -> list[Fragment]:
    """Run an export through parse → convert → frontmatter → assemble."""
    raw = RawDocument(
        path=filename,  # type: ignore[arg-type]
        content=export,
        metadata={},
        detected_encoding="utf-8",
    )
    fragments: list[Fragment] = []
    for parsed in ingestor.parse(raw):
        parsed.metadata["markdown"] = ingestor.convert_to_markdown(parsed)
        parsed.metadata["frontmatter"] = ingestor.generate_frontmatter(parsed)
        fragments.append(assemble_ingested_fragment(parsed).fragment)
    return fragments


def _write_vault(fragments: list[Fragment], vault: Path) -> None:
    """Persist *fragments* into ``<vault>/01-Fragments`` as markdown."""
    fragments_dir = vault / "01-Fragments"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    for fragment in fragments:
        payload = fragment.model_dump(mode="json", exclude={"voice_proxy_eligible"})
        post = frontmatter.Post(f"body for {fragment.id}", **payload)
        (fragments_dir / f"{fragment.id}.md").write_text(
            frontmatter.dumps(post),
            encoding="utf-8",
        )


# --- Attribution tests ------------------------------------------------------


def test_claude_turn_splits_into_self_and_ai_fragments() -> None:
    """A Claude turn yields a self human fragment and a zero-weight AI one."""
    frags = _ingest_fragments(
        ClaudeIngestor(),
        _claude_export([("How do I organize knowledge?", "Mirror how you think.")]),
        "claude_export.json",
    )

    assert len(frags) == 2
    human, ai = frags
    assert human.source.author == Authorship.SELF
    assert human.voice_weight == 1.0
    assert human.voice_proxy_eligible is True

    assert ai.source.author == Authorship.AI
    assert ai.voice_weight == 0.0
    assert ai.voice_proxy_eligible is False


def test_chatgpt_turn_splits_into_self_and_ai_fragments() -> None:
    """A ChatGPT turn yields a self human fragment and a zero-weight AI one."""
    frags = _ingest_fragments(
        ChatGPTIngestor(),
        _chatgpt_export([("What is a fragment?", "An atomic content unit.")]),
        "chatgpt_export.json",
    )

    assert len(frags) == 2
    human, ai = frags
    assert human.source.author == Authorship.SELF
    assert human.voice_weight == 1.0
    assert human.voice_proxy_eligible is True

    assert ai.source.author == Authorship.AI
    assert ai.voice_weight == 0.0
    assert ai.voice_proxy_eligible is False


def test_split_preserves_conversation_id_for_threading() -> None:
    """Both halves of a turn share the conversation id so threading survives."""
    frags = _ingest_fragments(
        ClaudeIngestor(),
        _claude_export([("Q1", "A1"), ("Q2", "A2")]),
        "claude_export.json",
    )

    assert len(frags) == 4
    assert {f.source.conversation_id for f in frags} == {"conv-553"}


# --- Voice-corpus exclusion -------------------------------------------------


def test_ai_turn_excluded_from_voice_corpus_human_turn_not(tmp_path: Path) -> None:
    """The AI turn is excluded from the voice corpus; the human turn is not."""
    frags = _ingest_fragments(
        ClaudeIngestor(),
        _claude_export([("My own question phrasing", "Assistant generated prose")]),
        "claude_export.json",
    )
    vault = tmp_path / "vault"
    _write_vault(frags, vault)

    human = next(f for f in frags if f.source.author == Authorship.SELF)
    ai = next(f for f in frags if f.source.author == Authorship.AI)
    # The human turn remains eligible voice material; the AI turn never is.
    assert human.voice_proxy_eligible is True
    assert ai.voice_proxy_eligible is False

    # The AI prose must not appear anywhere in the fingerprint's eligible texts.
    eligible = _eligible_texts(vault, AIStyleConfig(), include_intimate=False)
    assert all("Assistant generated prose" not in text for _weight, text in eligible)


def test_fresh_chat_ai_corpus_leak_is_zero(tmp_path: Path) -> None:
    """A freshly-ingested chat reports ≈0 AI-corpus leak (DoD for #553)."""
    frags = _ingest_fragments(
        ClaudeIngestor(),
        _claude_export([("Q1", "A1"), ("Q2", "A2"), ("Q3", "A3")]),
        "claude_export.json",
    )
    vault = tmp_path / "vault"
    _write_vault(frags, vault)

    report = build_voice_authenticity_report(vault, draft_path=None)

    # The three human turns are the eligible corpus; none counts as a leak
    # because each turn now carries a quarantined AI sibling.
    assert report.ai_corpus_leak.eligible_total == 3
    assert report.ai_corpus_leak.leaked == 0
    assert report.ai_corpus_leak.fraction == 0.0


def test_fresh_chatgpt_chat_ai_corpus_leak_is_zero(tmp_path: Path) -> None:
    """The leak drop also holds for ChatGPT ingests (no export conversation id)."""
    frags = _ingest_fragments(
        ChatGPTIngestor(),
        _chatgpt_export([("Q1", "A1"), ("Q2", "A2")]),
        "chatgpt_export.json",
    )
    vault = tmp_path / "vault"
    _write_vault(frags, vault)

    report = build_voice_authenticity_report(vault, draft_path=None)

    assert report.ai_corpus_leak.eligible_total == 2
    assert report.ai_corpus_leak.leaked == 0
    assert report.ai_corpus_leak.fraction == 0.0
