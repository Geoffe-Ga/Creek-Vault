"""Idiolect profiler: measure how *this* user actually writes (FEAT-040.2).

Builds a :class:`~creek.generate.ai_style.model.VoiceFingerprint` from
genuinely user-authored vault content — the false-positive authority and
distance baseline every detector consults. The single most important
property is the **authorship filter**: only self-authored text feeds the
fingerprint, and never the assistant's reply — otherwise the baseline
would be poisoned with the very AI accent we are trying to detect.

For a conversation platform (ChatGPT / Claude) the filter is not "keep
the user-turn portion" but a decision per body *shape*, made by
:func:`creek.ingest.turns.split_conversation_body` (#1426):

* a **per-turn split** human fragment — the shape both ingestors have
  written since #1333/#553, a plain blockquote with no ``**User**:``
  marker — contributes **in full**;
* a **merged** pre-split body contributes **only its human half**;
* an **assistant-only** body, where no human half can be identified,
  contributes **nothing**.

Reading only the marker, as this module did until #1426, silently
excluded the first of those three — every chat turn the operator has
written since per-turn attribution shipped.

The catalogue of on-disk body shapes, the reachability of each, and the
reasoning behind each verdict are **not** restated here.
:mod:`creek.ingest.turns` is their single source; the three bullets
above are a summary of it, kept short deliberately, and if they ever
disagree with it the parser's own docstring is the one that is right.

Pure and deterministic. Every *measured* feature is computed from
fragment bodies alone; frontmatter is read only to decide eligibility and
weight (``source.author``/``source.platform``, ``privacy_tier``,
``audience``, ``representativeness``), never as a feature value. The tier
read among those goes through
:func:`creek.classify.privacy_filter.raw_privacy_tier` so this module has
no tier opinion of its own (#1529). Writes a single JSON artifact under
the vault.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.classify.privacy_filter import raw_privacy_tier
from creek.generate.ai_style.features import FINGERPRINT_FEATURES
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.ingest.turns import CONVERSATION_PLATFORMS, split_conversation_body
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import AIStyleConfig, VoiceAudienceWeightingConfig

logger = logging.getLogger(__name__)

FINGERPRINT_VERSION = 2
"""Schema version stamped into the persisted fingerprint JSON.

v2 is #1426: the *corpus* changed, not the JSON layout. Split-era human
chat turns now contribute where they were previously dropped, so every
rate in a v1 artifact was measured over a strictly smaller sample and is
no longer comparable with a freshly built one. Bumping the version makes
:func:`load_fingerprint` ignore a persisted v1 file and log that
``creek report --type fingerprint`` should rebuild it, rather than let a
stale baseline keep scoring drafts.
"""

_FRAGMENTS_SUBDIR = "01-Fragments"
_SELF = "self"

# The *merged* pre-split rendering marked its turns inside the blockquote:
#   > **User**: ...        > **Assistant**: ...
# These two patterns serve `extract_user_turns` alone, which is off the
# fingerprint path since #1426; a split-era body carries neither marker.
_USER_MARKER = re.compile(r"^\**user\**\s*:\s*(.*)$", re.IGNORECASE)
_ASSISTANT_MARKER = re.compile(r"^\**assistant\**\s*:", re.IGNORECASE)


def _strip_quote(line: str) -> str:
    """Return *line* with leading Markdown blockquote markers removed."""
    return line.lstrip(">").strip()


def extract_user_turns(body: str) -> str | None:
    """Return only the user-turn text from a marker-delimited *body*.

    Walks the ``> **User**:`` / ``> **Assistant**:`` blockquote structure,
    accumulating text under user turns and dropping everything under
    assistant turns.

    **This is the legacy marker parser and it is no longer on the
    fingerprint path** (#1426). It is retained as public API — it is
    exported from :mod:`creek.generate.ai_style` — but
    :func:`_user_text` now dispatches on body shape via
    :func:`creek.ingest.turns.split_conversation_body` instead, for two
    reasons this function cannot be fixed into:

    * It treats any line's *contents* as a speaker cue. A bare ``user:``
      inside the operator's own prose — a pasted log line, say — is read
      as the start of a turn, silently deleting everything written
      before it. The loss is invisible: the body is still non-``None``
      and the fragment still counts.
    * It cannot see the split-era shapes at all. A per-turn human
      fragment carries no ``**User**:`` marker, so it returns ``None``
      for the shape that is now the common case.

    Args:
        body: A conversation fragment body.

    Returns:
        The joined user-turn text, or ``None`` when no ``**User**:``
        marker is present. That ``None`` no longer means "excluded from
        the fingerprint" — nothing on the fingerprint path consults it.
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

    A conversation platform yields the human half its body *shape*
    identifies — the whole turn when the body is already per-turn split,
    the operator's half alone when it is merged, and nothing at all when
    only the model's words can be found. Every other platform yields the
    whole body. The discriminator is the shape, not the presence of a
    ``**User**:`` marker.

    The rejected alternative was the one the issue proposed: fall back to
    the whole body when no ``**Assistant**:`` marker is present. It was
    measured and it fails on the bodies it was meant to protect, because
    a *merged pre-split Claude* body never carried an ``Assistant``
    marker either — the human turn was blockquoted and the reply followed
    as plain prose. Under that fallback such a body measures **173.9
    AI-vocabulary hits per 1000 words**, and the legacy ``"> \\n\\n{AI
    reply}"`` shape (an image-only or tool-only human send, whose human
    half is ``""``) measures **217.4**. Keep the human half whatever the
    shape, or keep nothing.

    Args:
        platform: The fragment's source platform.
        body: The fragment body.

    Returns:
        The user-authored text, or ``None`` when a conversation body has no
        recoverable human half (and when a body is empty once stripped).
    """
    if platform not in CONVERSATION_PLATFORMS:
        return body.strip() or None
    return split_conversation_body(platform, body).human or None


def _audience_factor(
    metadata: dict[str, object],
    platform: str,
    weighting: VoiceAudienceWeightingConfig | None,
) -> float:
    """Return the audience-authority multiplier for a fragment's frontmatter.

    The primary signal is the persisted ``audience`` axis from the #634
    classifier (``audience-facing`` dominates, ``private`` is down-weighted,
    ``mixed`` is neutral). It is layered on top of the #633 existing-signal
    heuristic — ``source.platform``, ``privacy_tier`` and ``representativeness``
    — which still carries an unclassified vault (every fragment ``mixed`` →
    ``1.0``) until the audience classifier has run. Returns ``1.0`` (no scoping)
    when *weighting* is absent or disabled, so the historical flat-average path
    is preserved. Missing values fall back to ``1.0`` so a new enum member never
    silently zeroes a fragment.

    ``privacy_tier`` is the one axis exempt from that fall-back, because on
    this path a zero is a **membership gate** (see :func:`_eligible_texts`)
    and the tier is a one-way ratchet. It is resolved through
    :func:`creek.classify.privacy_filter.raw_privacy_tier`, the repo's single
    fail-closed raw-frontmatter reader, so an *absent*, empty, or unparseable
    key weighs exactly as a declared ``intimate`` rather than as the
    ``unclassified`` this defaulted to until #1529. The point is that the
    multiplier cannot disagree with the admission gate above it about the same
    file: two tier opinions inside one module is the bug class #1529 is, not a
    style question. An explicit ``unclassified`` is untouched — it says out
    loud what it is, and ranks with ``personal`` (#876/#961).

    Args:
        metadata: The fragment's frontmatter mapping.
        platform: The fragment's ``source.platform``.
        weighting: The audience-weighting config, or ``None`` to disable scoping.

    Returns:
        A non-negative multiplier applied on top of the platform authorship
        weight.
    """
    if weighting is None or not weighting.enabled:
        return 1.0
    audience = weighting.audience_authority.get(
        str(metadata.get("audience", "mixed")),
        1.0,
    )
    privacy = weighting.privacy_tier_authority.get(
        raw_privacy_tier(metadata).value,
        1.0,
    )
    representativeness = weighting.representativeness_authority.get(
        str(metadata.get("representativeness", "self")),
        1.0,
    )
    platform_authority = weighting.platform_authority.get(platform, 1.0)
    return audience * privacy * representativeness * platform_authority


def _eligible_texts(
    vault_path: Path,
    config: AIStyleConfig,
    *,
    include_intimate: bool,
    audience_weighting: VoiceAudienceWeightingConfig | None = None,
) -> list[tuple[float, str]]:
    """Collect ``(weight, user_text)`` pairs for self-authored fragments.

    Applies the authorship filter (self-authored only; for a conversation
    platform, the human half its body shape identifies — see
    :func:`_user_text`) and the privacy policy (intimate excluded unless
    *include_intimate*, where "intimate" is whatever
    :func:`creek.classify.privacy_filter.raw_privacy_tier` says — so a
    fragment with no ``privacy_tier`` key, an empty one, or an unparseable
    one is excluded too, #1529). When *audience_weighting* is supplied,
    each fragment's weight is additionally multiplied by its
    audience-authority factor so audience-facing documents dominate the
    fingerprint (#633).

    Args:
        vault_path: Vault root.
        config: AI-style configuration (authorship weights).
        include_intimate: When ``True``, intimate-tier fragments are kept.
        audience_weighting: When supplied and enabled, scope the fingerprint to
            audience-facing documents; ``None`` keeps the flat-average path.
            Note the semantics below: ``if weight > 0.0`` makes a zero
            authority a **membership gate** on this path — a fragment weighted
            to zero is dropped from the corpus outright. On the exemplar path
            (:meth:`creek.generate.voice.VoiceExemplarCollector.rank_exemplars`)
            the same zero only de-ranks, because that method returns
            ``scored[:max]`` whatever the scores are. That difference is
            semantic, not a defaulting accident (#1313).

    Returns:
        One ``(weight, text)`` pair per eligible fragment.
    """
    out: list[tuple[float, str]] = []
    fragments_root = vault_path / _FRAGMENTS_SUBDIR
    if not fragments_root.exists():
        return out
    for md_file in sorted(fragments_root.rglob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except (OSError, yaml.YAMLError):
            logger.warning("Skipping unreadable fragment: %s", md_file)
            continue
        source = post.metadata.get("source", {})
        # This gate is the *only* thing keeping a ChatGPT AI turn out. Once a
        # conversation is split per-turn, ChatGPT's renderer blockquotes both
        # roles with no heading and no marker, so an AI turn's body is
        # shape-indistinguishable from a human one — `_user_text` would hand
        # back the model's prose in full. There is no body-shape backstop for
        # it; only `source.author != self`.
        if not isinstance(source, dict) or source.get("author") != _SELF:
            continue
        # Fail closed on the tier, through the one shared raw reader. This walk
        # never builds a `Fragment`, so it cannot use `fragment_tier`; asking
        # the frontmatter directly (`.get("privacy_tier") == "intimate"`, as
        # this did until #1529) answers `False` for a file with no key at all
        # and *admits* it — a raw read that fails open. `raw_privacy_tier`
        # resolves absent/empty/unparseable to INTIMATE, which is refusal here.
        tier = raw_privacy_tier(post.metadata)
        if not include_intimate and tier is PrivacyTier.INTIMATE:
            continue
        platform = str(source.get("platform", "other"))
        text = _user_text(platform, post.content)
        if not text:
            continue
        weight = config.authorship_weights.get(
            platform,
            config.authorship_default_weight,
        )
        weight *= _audience_factor(post.metadata, platform, audience_weighting)
        if weight > 0.0:
            out.append((weight, text))
    return out


def build_fingerprint(
    vault_path: Path,
    config: AIStyleConfig,
    *,
    include_intimate: bool = False,
    audience_weighting: VoiceAudienceWeightingConfig | None = None,
) -> VoiceFingerprint:
    """Build a :class:`VoiceFingerprint` from the user's own vault writing.

    Each feature in :data:`FINGERPRINT_FEATURES` is measured per eligible
    fragment and combined into a weighted mean. When *audience_weighting* is
    supplied, audience-facing documents (essays, Substack) dominate over private
    journals/DMs/chats, so the fingerprint encodes the user's audience-facing
    voice rather than their texting average (#633). Omitting it preserves the
    historical platform-only weighting.

    Args:
        vault_path: Vault root.
        config: AI-style configuration.
        include_intimate: When ``True``, include intimate-tier fragments.
        audience_weighting: When supplied and enabled, scope the fingerprint to
            audience-facing documents; ``None`` keeps the flat-average path —
            that is, ``None`` means **off** here, the opposite of
            :class:`~creek.generate.voice.VoiceExemplarCollector`, whose
            ``None`` selects the enabled default. Neither default moved in
            #1313: flipping the collector's would silently disable the shipped
            #632/#633/#634 epic, and flipping this one is a behaviour change
            for library callers with no issue behind it.

            Be careful what you conclude from that. On the *exemplar* side the
            default is unreachable in production, because
            ``test_production_voice_callers_always_state_an_audience_weighting``
            requires every construction there to state the value. **No such
            guard covers this function.** ``build_fingerprint`` is deliberately
            absent from ``_AUDIENCE_WEIGHTING_CALL_NAMES``, and one production
            caller does still rely on the ``None`` default:
            ``creek/cli.py``'s ``_resolve_voice_fingerprint``, the fallback
            ``creek voice-check`` takes when a vault has no persisted
            ``voice-fingerprint.json``. So that command can score against an
            unweighted fingerprint while ``report --type fingerprint``
            persists a weighted one. Tracked in #1410; not fixed here because
            it is pre-existing and outside #1313's inventory.

            The load-bearing difference between the two paths is not the
            default but the semantics — see :func:`_eligible_texts` on why a
            ``0.0`` authority excludes here and merely de-ranks there.

    Returns:
        A fingerprint whose ``fragment_count`` is the number of eligible
        fragments; empty when none qualify.
    """
    texts = _eligible_texts(
        vault_path,
        config,
        include_intimate=include_intimate,
        audience_weighting=audience_weighting,
    )
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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "Voice fingerprint at %s is unreadable; ignoring it. "
            "Re-run `creek report --type fingerprint` to rebuild.",
            path,
        )
        return VoiceFingerprint(features={}, fragment_count=0)
    if data.get("version") != FINGERPRINT_VERSION:
        logger.warning(
            "Voice fingerprint at %s is schema v%s (expected v%s); ignoring "
            "it. Re-run `creek report --type fingerprint` to rebuild.",
            path,
            data.get("version"),
            FINGERPRINT_VERSION,
        )
        return VoiceFingerprint(features={}, fragment_count=0)
    features = {
        key: FeatureStat(rate=float(value["rate"]), support=int(value["support"]))
        for key, value in data.get("features", {}).items()
    }
    return VoiceFingerprint(
        features=features,
        fragment_count=int(data.get("fragment_count", 0)),
    )
