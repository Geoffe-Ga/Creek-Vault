"""Idiolect profiler: measure how *this* user actually writes (FEAT-040.2).

Builds a :class:`~creek.generate.ai_style.model.VoiceFingerprint` from
genuinely user-authored vault content — the false-positive authority and
distance baseline every detector consults. The single most important
property is the **authorship filter**: only self-authored text feeds the
fingerprint, and for conversation platforms (ChatGPT / Claude) only the
*user-turn* portion is used, never the assistant's reply — otherwise the
baseline would be poisoned with the very AI accent we are trying to detect.

Pure and deterministic; reads fragment bodies only (never frontmatter
values), and writes a single JSON artifact under the vault.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import frontmatter

from creek.generate.ai_style.features import FINGERPRINT_FEATURES
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import AIStyleConfig

FINGERPRINT_VERSION = 1
"""Schema version stamped into the persisted fingerprint JSON."""

_FRAGMENTS_SUBDIR = "01-Fragments"
_CONVERSATION_PLATFORMS = frozenset({"chatgpt", "claude"})
_SELF = "self"
_INTIMATE = "intimate"

# Conversation bodies render turns as blockquotes:
#   > **User**: ...        > **Assistant**: ...
_USER_MARKER = re.compile(r"^\**user\**\s*:\s*(.*)$", re.IGNORECASE)
_ASSISTANT_MARKER = re.compile(r"^\**assistant\**\s*:", re.IGNORECASE)


def _strip_quote(line: str) -> str:
    """Return *line* with leading Markdown blockquote markers removed."""
    return line.lstrip(">").strip()


def extract_user_turns(body: str) -> str | None:
    """Return only the user-turn text from a conversation *body*.

    Walks the ``> **User**:`` / ``> **Assistant**:`` blockquote structure,
    accumulating text under user turns and dropping everything under
    assistant turns.

    Args:
        body: A conversation fragment body.

    Returns:
        The joined user-turn text, or ``None`` when no ``**User**:`` marker
        is present (the body cannot be split cleanly, so it is excluded
        rather than risk fingerprinting AI text).
    """
    parts: list[str] = []
    speaker: str | None = None
    saw_user = False
    for raw in body.splitlines():
        line = _strip_quote(raw)
        user_match = _USER_MARKER.match(line)
        if user_match is not None:
            speaker = "user"
            saw_user = True
            remainder = user_match.group(1).strip()
            if remainder:
                parts.append(remainder)
        elif _ASSISTANT_MARKER.match(line) is not None:
            speaker = "assistant"
        elif speaker == "user" and line:
            parts.append(line)
    if not saw_user:
        return None
    return "\n".join(parts).strip() or None


def _user_text(platform: str, body: str) -> str | None:
    """Return the fingerprint-eligible text for a fragment body.

    Conversation platforms yield only their user-turn; everything else
    yields the whole body.

    Args:
        platform: The fragment's source platform.
        body: The fragment body.

    Returns:
        The user-authored text, or ``None`` when a conversation body has no
        recoverable user turn.
    """
    if platform in _CONVERSATION_PLATFORMS:
        return extract_user_turns(body)
    return body.strip() or None


def _eligible_texts(
    vault_path: Path,
    config: AIStyleConfig,
    *,
    include_intimate: bool,
) -> list[tuple[float, str]]:
    """Collect ``(weight, user_text)`` pairs for self-authored fragments.

    Applies the authorship filter (self-authored only; conversation
    user-turn only) and the privacy policy (intimate excluded unless
    *include_intimate*).

    Args:
        vault_path: Vault root.
        config: AI-style configuration (authorship weights).
        include_intimate: When ``True``, intimate-tier fragments are kept.

    Returns:
        One ``(weight, text)`` pair per eligible fragment.
    """
    out: list[tuple[float, str]] = []
    fragments_root = vault_path / _FRAGMENTS_SUBDIR
    if not fragments_root.exists():
        return out
    for md_file in sorted(fragments_root.rglob("*.md")):
        post = frontmatter.load(str(md_file))
        source = post.metadata.get("source", {})
        if not isinstance(source, dict) or source.get("author") != _SELF:
            continue
        if not include_intimate and post.metadata.get("privacy_tier") == _INTIMATE:
            continue
        platform = str(source.get("platform", "other"))
        text = _user_text(platform, post.content)
        if not text:
            continue
        weight = config.authorship_weights.get(
            platform,
            config.authorship_default_weight,
        )
        if weight > 0.0:
            out.append((weight, text))
    return out


def build_fingerprint(
    vault_path: Path,
    config: AIStyleConfig,
    *,
    include_intimate: bool = False,
) -> VoiceFingerprint:
    """Build a :class:`VoiceFingerprint` from the user's own vault writing.

    Each feature in :data:`FINGERPRINT_FEATURES` is measured per eligible
    fragment and combined into a platform-weighted mean — so journals and
    posts shape the baseline more than the noisier conversation user-turns.

    Args:
        vault_path: Vault root.
        config: AI-style configuration.
        include_intimate: When ``True``, include intimate-tier fragments.

    Returns:
        A fingerprint whose ``fragment_count`` is the number of eligible
        fragments; empty when none qualify.
    """
    texts = _eligible_texts(vault_path, config, include_intimate=include_intimate)
    if not texts:
        return VoiceFingerprint(features={}, fragment_count=0)

    total_weight = sum(weight for weight, _ in texts)
    features: dict[str, FeatureStat] = {}
    for key, extractor in FINGERPRINT_FEATURES.items():
        weighted = sum(weight * extractor(text) for weight, text in texts)
        features[key] = FeatureStat(
            rate=weighted / total_weight,
            support=len(texts),
        )
    return VoiceFingerprint(features=features, fragment_count=len(texts))


def save_fingerprint(
    fingerprint: VoiceFingerprint,
    vault_path: Path,
    config: AIStyleConfig,
) -> Path:
    """Persist *fingerprint* as JSON under the vault and return its path.

    Args:
        fingerprint: The fingerprint to write.
        vault_path: Vault root.
        config: AI-style configuration (supplies ``fingerprint_path``).

    Returns:
        The path written.
    """
    path = vault_path / config.fingerprint_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": FINGERPRINT_VERSION,
        "fragment_count": fingerprint.fragment_count,
        "features": {
            key: {"rate": stat.rate, "support": stat.support}
            for key, stat in fingerprint.features.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_fingerprint(vault_path: Path, config: AIStyleConfig) -> VoiceFingerprint:
    """Load a persisted fingerprint, or an empty one when absent.

    Args:
        vault_path: Vault root.
        config: AI-style configuration (supplies ``fingerprint_path``).

    Returns:
        The loaded fingerprint, or an empty fingerprint when no file exists.
    """
    path = vault_path / config.fingerprint_path
    if not path.exists():
        return VoiceFingerprint(features={}, fragment_count=0)
    data = json.loads(path.read_text(encoding="utf-8"))
    features = {
        key: FeatureStat(rate=float(value["rate"]), support=int(value["support"]))
        for key, value in data.get("features", {}).items()
    }
    return VoiceFingerprint(
        features=features,
        fragment_count=int(data.get("fragment_count", 0)),
    )
