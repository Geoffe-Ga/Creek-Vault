"""The vault writes a margin note's Voice Draft letter (#1862).

A vault-bound Adepthood writer's AI operations must stay inside their vault, so
the letter that expands one margin note is written here rather than by an
app-side provider, and stored as the same AI-attributed Voice Draft
``PUT /v1/voice-drafts/{external_id}`` writes. This module is the
transport-neutral half; :mod:`creek_mcp.httpapi.letters` maps its outcomes onto
the wire.

**The gates, in order, and why that order.**

1. *Ceiling.* The declared entry tier must be writable under the caller's
   ceiling (:func:`creek_mcp.tier_ceiling.write_tier_allowed`) — a ``personal``
   entry under an ``open`` ceiling is refused before anything else happens.
2. *Care.* :func:`creek.care.guardrail.acute_distress_guard` runs on the entry
   and on the note before any model call; an acute-distress source escalates
   instead of being written to.
3. *Existing letter.* A slot already holding an admitted letter is returned as
   stored, with no model call: a note's first letter is the letter, and a retry
   after a lost acknowledgement costs nothing. A slot above the ceiling is
   refused, exactly as the Voice Draft read is.
4. *Routing.* The prompt carries only the caller's own entry and note, so its
   content tier is the declared one. :func:`creek_mcp.tier_ceiling.routing_tier`
   takes the more sensitive of that and the ceiling-derived tier, and the
   tier-keyed factory resolves the ``generation`` stage through the vault's
   :class:`~creek.classify.llm.router.ModelRouter`, so an ``INTIMATE`` call is
   forced local or refused (``IntimateRoutingError``), and a cloud provider is
   built only with cloud consent. This module never picks a provider.
5. *Write.* The letter is stored first-writer-wins
   (:func:`creek.voice_drafts.create_voice_draft_if_absent`), so two concurrent
   letters for one id store exactly one.

**Disclosure.** The audit row and the log records carry lengths, the declared
tier and a closed-vocabulary outcome only — never the entry, the note, the
prompt or the letter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, TypeAlias

from creek._fslock import VaultLockTimeoutError
from creek.care.guardrail import CARE_POLICY
from creek.voice_drafts import (
    VoiceDraftAccessDeniedError,
    VoiceDraftAction,
    VoiceDraftDocument,
    VoiceDraftRecord,
    VoiceDraftStorageError,
    VoiceDraftWriteResult,
    create_voice_draft_if_absent,
    read_voice_draft,
)
from creek_mcp.api.models import MAX_LETTER_CHARS
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import (
    TierCeiling,
    routing_tier,
    tier_allowed,
    write_tier_allowed,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.models import PrivacyTier

__all__ = [
    "LETTER_MAX_OUTPUT_TOKENS",
    "MAX_LETTER_CHARS",
    "LetterCall",
    "LetterEscalation",
    "LetterLLMFactory",
    "LetterRefusal",
    "LetterSource",
    "LetterWrite",
    "build_letter_prompt",
    "write_letter",
]

TOOL_NAME: Final[str] = "creek.letter.write"
"""The audit-log tool name of one letter attempt."""

LETTER_MAX_OUTPUT_TOKENS: Final[int] = 1_500
"""Hard output budget for one letter turn.

Sized for a short letter of a few hundred words with headroom; a turn that
reaches it is refused as truncated by the production completion rather than
stored half-written.
"""

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class LetterRefusal(StrEnum):
    """The closed vocabulary of reasons a letter is not written.

    Attributes:
        ABOVE_CEILING: The declared tier, or the stored letter's tier, is above
            the caller's ceiling.
        PROVIDER_UNAVAILABLE: No provider may serve this routing tier — it is
            unavailable, unconsented, unpinned, or the router refused to egress.
        EMPTY_LETTER: The model's turn was blank; nothing is stored.
        STORAGE_BUSY: The Voice Draft mutation lock could not be taken in time.
        STORAGE_INVALID: The addressed slot exists but is not a valid draft.
    """

    ABOVE_CEILING = "above_ceiling"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    EMPTY_LETTER = "empty_letter"
    STORAGE_BUSY = "storage_busy"
    STORAGE_INVALID = "storage_invalid"


@dataclass(frozen=True, slots=True)
class LetterSource:
    """The caller's own material a letter is written from.

    Attributes:
        content: The full journal entry.
        note: The margin note being expanded.
        anchor_text: The passage the note anchors to.
        kind: The note's kind token.
        tier: The source entry's tier, never ``INTIMATE`` from the wire.
        title: Optional stored title.
    """

    content: str
    note: str
    anchor_text: str
    kind: str
    tier: PrivacyTier
    title: str | None


@dataclass(frozen=True, slots=True)
class LetterEscalation:
    """The care guard fired; the caller is handed the care signal instead.

    Attributes:
        reason: The guard's stable reason code, never matched text.
    """

    reason: str


LetterWrite: TypeAlias = VoiceDraftWriteResult
"""A stored letter: ``CREATED``, or ``UNCHANGED`` when the id held one."""

LetterOutcome: TypeAlias = LetterWrite | LetterEscalation | LetterRefusal
"""Everything :func:`write_letter` can answer."""


class _LetterLLM(Protocol):
    """A prompt-completion callable returning the letter text."""

    def __call__(self, prompt: str, /) -> str:
        """Return the completion for *prompt*."""


class LetterLLMFactory(Protocol):
    """Tier-keyed builder for the letter LLM client.

    The production factory resolves the ``generation`` stage through the
    vault's :class:`~creek.classify.llm.router.ModelRouter`; see
    :func:`creek_mcp.server._build_letter_llm_factory`.
    """

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> _LetterLLM:
        """Return a routed completion callable with a hard output ceiling."""


@dataclass(frozen=True, slots=True)
class LetterCall:
    """Everything one :func:`write_letter` call needs besides its source.

    Attributes:
        vault_path: The vault root.
        external_id: The caller-owned id the letter is stored under.
        ceiling: The caller's admitted ceiling.
        consumer: The authenticated consumer, for the audit row.
        build_factory: Zero-argument thunk returning the tier-keyed factory.
            Invoked only once every gate has passed, so a provider that cannot
            be built costs nothing on a refused call.
        care_guard: ``(text) -> reason | None``.
    """

    vault_path: Path
    external_id: str
    ceiling: TierCeiling
    consumer: str
    build_factory: Callable[[], LetterLLMFactory]
    care_guard: Callable[[str], str | None]


def build_letter_prompt(source: LetterSource) -> str:
    """Compose the letter prompt from the caller's own entry and note.

    Instructions first, then each caller field inside its own named delimiter,
    so the model can tell the material it is grounded in from the task.

    Args:
        source: The entry and note to write from.

    Returns:
        The prompt.
    """
    instructions = (
        "You are writing a short, warm letter to the person whose journal this "
        "is, in their own Higher Self's voice, expanding on a margin note left on "
        "their entry: their words and wisdom handed back, never advice or a plan. "
        "Stay grounded in the passage the note anchors to and in the entry around "
        "it; speak in second person, plainly, like a smart friend. If a stage or "
        "phase of the wavelength fits, name it as a capacity or a season, never as "
        "who they are and never as above or below another. Be honest about what is "
        "hard; promise no arrival and no permanence. Leave them free to take or "
        "leave what you notice. Never refer to yourself as an AI. Return only the "
        "letter's text: no title, preamble, or Markdown fence."
    )
    material = (
        f"Margin note kind: {source.kind}\n"
        f"<note>\n{source.note}\n</note>\n\n"
        f"The passage it anchors to:\n<passage>\n{source.anchor_text}\n</passage>\n\n"
        f"The full entry for context:\n<entry>\n{source.content}\n</entry>"
    )
    return f"{CARE_POLICY}\n\n{instructions}\n\n{material}"


def _bounded_letter(completion: str) -> str:
    """Return the completion stripped and capped to :data:`MAX_LETTER_CHARS`."""
    return completion.strip()[:MAX_LETTER_CHARS].rstrip()


def _audit(call: LetterCall, source: LetterSource) -> None:
    """Append the one body-free audit row for this attempt."""
    MCPAuditLog(call.vault_path).append(
        tool=TOOL_NAME,
        args={
            "has_external_id": True,
            "has_title": source.title is not None,
            "content_len": len(source.content),
            "note_len": len(source.note),
            "tier": source.tier.value,
        },
        tier_ceiling=call.ceiling,
        consumer=call.consumer,
    )


def _log_outcome(call: LetterCall, outcome: str) -> None:
    """Record one closed-vocabulary outcome, keyed by consumer only."""
    _LOGGER.info(
        "letter outcome",
        extra={"letter_outcome": outcome, "consumer": call.consumer},
    )


def _existing_letter(
    call: LetterCall,
) -> VoiceDraftRecord | LetterRefusal | None:
    """Return the admitted letter already in the slot, a refusal, or ``None``."""
    try:
        existing = read_voice_draft(
            vault_path=call.vault_path, external_id=call.external_id
        )
    except (OSError, VoiceDraftStorageError):
        return LetterRefusal.STORAGE_INVALID
    if existing is not None and not tier_allowed(existing.tier, call.ceiling):
        return LetterRefusal.ABOVE_CEILING
    return existing


def _generate(call: LetterCall, source: LetterSource) -> str | LetterRefusal:
    """Dial the tier-routed model once and return the bounded letter."""
    tier = routing_tier(call.ceiling, source.tier)
    try:
        llm = call.build_factory()(tier, max_tokens=LETTER_MAX_OUTPUT_TOKENS)
        completion = llm(build_letter_prompt(source))
    except (RuntimeError, OSError, ValueError):
        # ``IntimateRoutingError`` is a ``RuntimeError``: the router refused to
        # egress and there is no local backend. It never falls back to cloud.
        return LetterRefusal.PROVIDER_UNAVAILABLE
    letter = _bounded_letter(completion)
    return letter or LetterRefusal.EMPTY_LETTER


def _store(
    call: LetterCall, source: LetterSource, letter: str
) -> LetterWrite | LetterRefusal:
    """Store *letter* first-writer-wins under the caller's id."""
    try:
        return create_voice_draft_if_absent(
            vault_path=call.vault_path,
            external_id=call.external_id,
            document=VoiceDraftDocument(
                content=letter, title=source.title, tier=source.tier
            ),
            existing_is_admitted=lambda tier: tier_allowed(tier, call.ceiling),
        )
    except VoiceDraftAccessDeniedError:
        return LetterRefusal.ABOVE_CEILING
    except VaultLockTimeoutError:
        return LetterRefusal.STORAGE_BUSY
    except (OSError, VoiceDraftStorageError):
        return LetterRefusal.STORAGE_INVALID


def _care_reason(call: LetterCall, source: LetterSource) -> str | None:
    """Return the care guard's reason for the entry or the note, if either fires."""
    return call.care_guard(source.content) or call.care_guard(source.note)


def _admitted_outcome(call: LetterCall, source: LetterSource) -> LetterOutcome:
    """Run the gates after the ceiling check, in source order."""
    reason = _care_reason(call, source)
    if reason:
        return LetterEscalation(reason=reason)
    existing = _existing_letter(call)
    if isinstance(existing, LetterRefusal):
        return existing
    if existing is not None:
        return VoiceDraftWriteResult(VoiceDraftAction.UNCHANGED, existing)
    letter = _generate(call, source)
    if isinstance(letter, LetterRefusal):
        return letter
    return _store(call, source, letter)


def write_letter(call: LetterCall, source: LetterSource) -> LetterOutcome:
    """Write, store and return one margin note's Voice Draft letter.

    Args:
        call: The vault, id, ceiling, consumer, factory thunk and care guard.
        source: The caller's own entry and note.

    Returns:
        The stored letter (``CREATED`` or ``UNCHANGED``), an escalation, or a
        closed-vocabulary refusal. Nothing is stored on any non-``CREATED``
        path.
    """
    _audit(call, source)
    outcome: LetterOutcome
    if not write_tier_allowed(source.tier, call.ceiling):
        outcome = LetterRefusal.ABOVE_CEILING
    else:
        outcome = _admitted_outcome(call, source)
    _log_outcome(call, _outcome_word(outcome))
    return outcome


def _outcome_word(outcome: LetterOutcome) -> str:
    """Return the closed-vocabulary word naming *outcome* for the log."""
    if isinstance(outcome, LetterRefusal):
        return outcome.value
    if isinstance(outcome, LetterEscalation):
        return "escalated"
    return outcome.action.value
