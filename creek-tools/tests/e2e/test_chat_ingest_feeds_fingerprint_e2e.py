"""End-to-end: a chat export ingested for real must reach the fingerprint.

Issue #1426. Per-turn attribution (#1333/#553) changed what a chat fragment's
body *looks like*: a human turn is now a plain blockquote with no
``> **User**:`` marker. ``extract_user_turns`` only ever recognised that
marker, so from the day per-turn attribution shipped, every freshly ingested
Claude and ChatGPT human turn returned ``None`` and was dropped from the voice
corpus — silently, with the fingerprint reporting a smaller
``fragment_count`` and nothing reporting why.

Every unit test for that seam builds its vault by hand, which is exactly how
the regression survived: a hand-written body encodes what the test author
believes the ingestor writes. This journey therefore runs the **real**
``run_ingest`` for each chat ingestor against real export JSON, writes real
fragments through the real ``VaultWriter``, and only then measures the
fingerprint. The two ends of the wire are joined by nothing but production
code.

``run_ingest`` is called once per ingestor rather than once over a shared
directory: each call's ``written`` tally is then attributable to one ingestor,
so a chat platform that stops emitting fragments cannot hide inside a combined
total.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import _eligible_texts, build_fingerprint
from creek.ingest.chatgpt import ChatGPTIngestor
from creek.ingest.claude import ClaudeIngestor
from creek.ingest.pipeline import run_ingest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e

_HUMAN_PROSE = (
    "I keep circling the same knot about how my notes should sit next to "
    "each other, and every time I try to force a folder tree on it the "
    "thing goes stiff and I stop writing in it at all for a week."
)
"""The operator's own turn: >20 words, no AI-vocabulary hit, no em-dash."""

_AI_PROSE = (
    "It is important to note that a vibrant tapestry of intricate systems "
    "underscores the pivotal realm you navigate as you delve into it."
)
"""The model's turn, dense with AI vocabulary.

Carries ``tapestry``, ``delve``, ``pivotal``, ``underscore`` and ``realm``, so
any leak into the corpus shows up both as a non-zero ``ai_vocab_density`` and
as a findable substring.
"""

_AI_TELLS = ("tapestry", "delve", "pivotal", "underscore", "realm")
"""Individual words that must not appear in any eligible corpus text."""


def _claude_export(turns: list[tuple[str, str]]) -> bytes:
    """Build a one-conversation Claude export from (human, assistant) turns.

    Args:
        turns: ``(human_text, assistant_text)`` pairs, in order.

    Returns:
        The export JSON as UTF-8 bytes, in the shape ``ClaudeIngestor.discover``
        accepts.
    """
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
        "uuid": "conv-1426",
        "name": "How my notes should sit",
        "created_at": "2024-11-15T10:00:00Z",
        "model": "claude-3-opus-20240229",
        "messages": messages,
    }
    return json.dumps({"conversations": [conv]}).encode("utf-8")


def _chatgpt_export(turns: list[tuple[str, str]]) -> bytes:
    """Build a one-conversation ChatGPT export from (user, assistant) turns.

    Args:
        turns: ``(user_text, assistant_text)`` pairs, in order.

    Returns:
        The export JSON as UTF-8 bytes, in the shape ``ChatGPTIngestor.discover``
        accepts (a top-level list of conversation dicts).
    """
    mapping: dict[str, Any] = {
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
        mapping[prev]["children"] = [uid]
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
    conv = {
        "title": "How my notes should sit",
        "create_time": 1700000000.0,
        "mapping": mapping,
    }
    return json.dumps([conv]).encode("utf-8")


def _write_export(source: Path, name: str, payload: bytes) -> Path:
    """Write *payload* as ``<source>/<name>/conversations.json``.

    Each ingestor gets its own directory because both discover by globbing
    ``*.json`` in the source dir, and a shared directory would make each run's
    ``written`` tally depend on the other ingestor's sniffing.

    Args:
        source: The e2e source root.
        name: Sub-directory name for this ingestor.
        payload: The export bytes.

    Returns:
        The directory the export was written into.
    """
    directory = source / name
    directory.mkdir()
    (directory / "conversations.json").write_bytes(payload)
    return directory


def _corpus(vault: Path) -> list[str]:
    """Return the fingerprint's eligible corpus texts for *vault*.

    Args:
        vault: Vault root.

    Returns:
        The user-authored text of every voice-eligible fragment.
    """
    texts = _eligible_texts(vault, AIStyleConfig(), include_intimate=False)
    return [text for _weight, text in texts]


def test_ingested_chat_human_turns_reach_the_fingerprint(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Both chat ingestors put their human turn — and only it — in the corpus.

    The full journey: export JSON on disk -> ``run_ingest`` -> fragments in
    ``01-Fragments`` -> ``build_fingerprint``. Pre-fix the two human turns are
    written to the vault correctly and then discarded at the fingerprint, so
    ``fragment_count`` is 0 and the corpus is empty. The tallies are asserted
    per ingestor so the human/AI pair from each is accounted for separately.
    """
    claude_dir = _write_export(
        synthetic_source, "claude", _claude_export([(_HUMAN_PROSE, _AI_PROSE)])
    )
    chatgpt_dir = _write_export(
        synthetic_source, "chatgpt", _chatgpt_export([(_HUMAN_PROSE, _AI_PROSE)])
    )

    claude_result = run_ingest(
        ingestor_cls=ClaudeIngestor,
        source_type="claude",
        input_path=claude_dir,
        vault_path=synthetic_vault,
    )
    chatgpt_result = run_ingest(
        ingestor_cls=ChatGPTIngestor,
        source_type="chatgpt",
        input_path=chatgpt_dir,
        vault_path=synthetic_vault,
    )

    assert claude_result.errors == []
    assert chatgpt_result.errors == []
    # One human fragment + one AI fragment per ingestor.
    assert claude_result.written == 2
    assert chatgpt_result.written == 2

    fingerprint = build_fingerprint(synthetic_vault, AIStyleConfig())

    assert fingerprint.fragment_count == 2
    assert _corpus(synthetic_vault) == [_HUMAN_PROSE, _HUMAN_PROSE]


def test_ingested_chat_ai_turns_never_reach_the_fingerprint(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """The model's half of a real ingest stays out of the voice corpus.

    The correctness property that outranks recovering the human turn: it is
    better to fingerprint nothing than to fingerprint the model. Asserted
    three ways — the measured ``ai_vocab_density``, the whole AI sentence, and
    each individual tell word — because a partial leak (one stray line of the
    reply) would move the density without reproducing the sentence.
    """
    claude_dir = _write_export(
        synthetic_source, "claude", _claude_export([(_HUMAN_PROSE, _AI_PROSE)])
    )
    chatgpt_dir = _write_export(
        synthetic_source, "chatgpt", _chatgpt_export([(_HUMAN_PROSE, _AI_PROSE)])
    )
    run_ingest(
        ingestor_cls=ClaudeIngestor,
        source_type="claude",
        input_path=claude_dir,
        vault_path=synthetic_vault,
    )
    run_ingest(
        ingestor_cls=ChatGPTIngestor,
        source_type="chatgpt",
        input_path=chatgpt_dir,
        vault_path=synthetic_vault,
    )

    fingerprint = build_fingerprint(synthetic_vault, AIStyleConfig())
    corpus = _corpus(synthetic_vault)

    assert corpus, "an empty corpus would make every exclusion below vacuous"
    assert fingerprint.rate_for("ai_vocab_density") == 0.0
    assert all(_AI_PROSE not in text for text in corpus)
    for tell in _AI_TELLS:
        assert all(tell not in text for text in corpus), tell
