"""Care boundary: medication policy + acute-distress detector (#753).

Creek's voice-generating tools (``creek.reflect`` / ``draft`` / ``author`` /
``compile``) must be safe **by construction** — not only when a careful client
(Adepthood) runs its own pre-check — because other consumers (CrawDad) call the
same surface. This module is the single source of that safety:

- :data:`CARE_POLICY` — prose woven into every voice prompt. It forbids advising
  the writer to change/stop/reduce psychiatric medication (deferring to them and
  their prescriber), tells the model to point toward human + professional support
  on acute distress rather than counsel as a chatbot, and to keep its ordinary
  non-directive voice — explicitly *not* pathologizing honest, dark, or
  reflective writing.
- :func:`acute_distress_guard` — a deliberately narrow, **acute-only** detector
  that returns a stable reason code on first-person, imminent self-harm /
  suicidal *intent* (not affect), else ``None``. It is a backstop, biased toward
  false negatives so ordinary reflective journaling is never flagged.
- :data:`CARE_SIGNAL` — the structured "reach a human" envelope a tool returns
  instead of model prose when the guard fires; it never dead-ends.
"""

from __future__ import annotations

import re
from typing import Any, Final

CARE_POLICY: Final[str] = (
    "CARE BOUNDARY (non-negotiable): You are not a clinician or a crisis "
    "service. Never advise the writer to start, stop, reduce, or change any "
    "psychiatric medication or treatment — those decisions belong to them and "
    "their prescriber, and you defer to that relationship. If the writing shows "
    "acute distress or risk of harm, do not try to counsel them through it as a "
    "chatbot; gently point them toward a trusted person and professional support. "
    "Otherwise hold your ordinary warm, non-directive voice and do NOT pathologize "
    "honest, dark, grieving, or reflective writing — bleakness is not an emergency."
)
"""Policy text woven into every voice-generating prompt (the prompt-level half)."""


# Acute, first-person, imminent self-harm / suicidal-INTENT phrasings only.
# Deliberately narrow: these match stated intent or a plan, never affect words
# ("hopeless", "exhausted", "grief", "thinking about death") or hyperbole
# ("the deadline is killing me"), so ordinary dark/reflective journaling does
# not trip the guard. Biased toward false negatives by design — Adepthood runs
# the primary acute pre-check; this is Creek's safe-by-construction backstop.
_ACUTE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bkill(?:ing)?\s+myself\b", re.IGNORECASE),
    re.compile(r"\b(?:end|ending|take|taking)\s+my\s+(?:own\s+)?life\b", re.IGNORECASE),
    re.compile(r"\bgoing\s+to\s+(?:kill|hurt|harm)\s+myself\b", re.IGNORECASE),
    re.compile(r"\bcommit(?:ting)?\s+suicide\b", re.IGNORECASE),
)

_ACUTE_REASON: Final[str] = "acute_distress_markers"


def acute_distress_guard(text: str) -> str | None:
    """Return a reason code for acute self-harm intent, else ``None``.

    Matches only narrow, first-person imminent-intent phrasings (see
    :data:`_ACUTE_PATTERNS`); affect, grief, and reflection on death are
    deliberately not matched. Returns the stable code ``"acute_distress_markers"``
    — never the matched text — so the signal is privacy- and audit-safe.

    Args:
        text: The journal entry / candidate content.

    Returns:
        ``"acute_distress_markers"`` when an acute-intent pattern matches, else
        ``None``.
    """
    return _ACUTE_REASON if any(p.search(text) for p in _ACUTE_PATTERNS) else None


CARE_SIGNAL: Final[dict[str, Any]] = {
    "kind": "acute_distress",
    "message": (
        "This sounds urgent and heavy, and you deserve real human support right "
        "now — more than a chatbot can give. Please reach out to someone you "
        "trust, or contact a crisis service. You are not alone in this."
    ),
    "resources": [
        {
            "name": "988 Suicide & Crisis Lifeline (US)",
            "contact": "Call or text 988",
        },
        {
            "name": "Find a Helpline (international directory)",
            "contact": "https://findahelpline.com",
        },
    ],
}
"""Structured "reach a human" signal returned in place of model prose on acute
distress (the response-level half). Clients render it; it never dead-ends."""
