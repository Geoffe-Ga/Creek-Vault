"""Judge, inside Creek, whether a revoice delivered the owner's voice.

A model that returns well-formed text has not necessarily written in the
owner's voice. Whether it did is Creek's call, so the verdict lives here
rather than in client heuristics, and it is made on the **end state**: the
finished rewrite's voice rating against the vault's fingerprint
(:mod:`creek.generate.ai_style.rating`). How many words changed is not the
question. A rewrite that changed little but already reads as the owner has
succeeded; a sweeping rewrite into fluent generic model prose has not.

Until this module was rebased on the rating, the gate counted changed words,
rebuilt sentences and a changed opening. Those measures could not tell a
heavy rewrite into generic prose from a real voice, and they refused a light
touch on a passage that was already the owner's.

Two checks, each deterministic, with no model call and only numbers and
closed codes in the verdict:

* **Voice.** The rewrite's rating should be in voice (``voice_distance`` at or
  under ``voice_distance_upper``). An off-voice rewrite earns the one bounded
  retry and an ``off_voice`` diagnostic, but it is **returned, not refused**:
  the rating is reported and logged for monitoring
  (:func:`creek.generate.ai_style.rating.record_voice_rating`).
* **Fidelity.** Every heading and every citation, footnote reference and link
  target in the source must survive (:mod:`creek.author.revoice_style`).
  Removing apparatus is a correctness defect, not a voice, so a rewrite that
  still loses it after the retry is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from creek.author.revoice_style import citation_markers, headings

if TYPE_CHECKING:
    from creek.generate.ai_style.rating import VoiceRating


class RevoiceReason(StrEnum):
    """The closed, top-level reason a revoice is refused."""

    NO_OWNER_VOICE = "no_owner_voice"
    """No owner-authored style or admitted owner samples could ground it."""
    FIDELITY_VIOLATION = "fidelity_violation"
    """The rewrite lost headings or citation apparatus."""


class Diagnostic(StrEnum):
    """A closed, content-free finding behind a refusal."""

    OFF_VOICE = "off_voice"
    HEADINGS_CHANGED = "headings_changed"
    CITATIONS_DROPPED = "citations_dropped"


@dataclass(frozen=True)
class RevoiceCheck:
    """Creek's verdict on one rewrite, with the rating behind it.

    Attributes:
        rating: The finished rewrite's end-state voice rating, or ``None``
            when the vault has no fingerprint that can judge it.
        diagnostics: Every finding, in a stable order; empty when clean.
    """

    rating: VoiceRating | None
    diagnostics: tuple[Diagnostic, ...]

    @property
    def reason(self) -> RevoiceReason | None:
        """The refusal reason, or ``None`` when the rewrite may be returned.

        Only lost apparatus refuses. Off-voice never does.
        """
        if Diagnostic.HEADINGS_CHANGED in self.diagnostics or (
            Diagnostic.CITATIONS_DROPPED in self.diagnostics
        ):
            return RevoiceReason.FIDELITY_VIOLATION
        return None

    @property
    def clean(self) -> bool:
        """Whether no check found anything, so a retry cannot improve it."""
        return not self.diagnostics

    @property
    def distance(self) -> float:
        """The rated distance, or ``0.0`` when unrated, for ranking attempts."""
        return 0.0 if self.rating is None else self.rating.voice_distance

    def as_dict(self) -> dict[str, Any]:
        """Project the verdict onto JSON-ready numbers and closed codes only.

        ``voice_applied`` is the rating's ``in_voice``, or ``None`` when the
        rewrite could not be rated.
        """
        return {
            "voice_applied": None if self.rating is None else self.rating.in_voice,
            "diagnostics": [finding.value for finding in self.diagnostics],
            "voice_rating": None if self.rating is None else self.rating.as_dict(),
        }


def _fidelity(source: str, output: str) -> list[Diagnostic]:
    """Return the apparatus-preservation findings."""
    findings: list[Diagnostic] = []
    if headings(source) != headings(output):
        findings.append(Diagnostic.HEADINGS_CHANGED)
    if not citation_markers(source) <= citation_markers(output):
        findings.append(Diagnostic.CITATIONS_DROPPED)
    return findings


def check_revoice(
    source: str, output: str, *, rating: VoiceRating | None
) -> RevoiceCheck:
    """Judge whether *output* is a faithful rewrite of *source* in the owner's voice.

    Args:
        source: The passage the caller supplied.
        output: The model's rewrite.
        rating: *output*'s end-state voice rating, or ``None`` when unrated.

    Returns:
        The verdict, with every finding and the rating behind it.
    """
    findings = (
        [Diagnostic.OFF_VOICE] if rating is not None and not rating.in_voice else []
    )
    findings += _fidelity(source, output)
    return RevoiceCheck(rating=rating, diagnostics=tuple(findings))


def main(argv: list[str] | None = None) -> int:
    """Judge a local source/rewrite pair exactly as ``creek.revoice`` would.

    Usage: ``python -m creek.author.revoice_check SOURCE OUTPUT --vault V``.
    The vault's voice fingerprint rates the rewrite. Prints the content-free
    verdict as JSON.

    Args:
        argv: Arguments after the program name; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` when the rewrite is clean, ``1`` when any check found something
        (off-voice or lost apparatus).
    """
    from creek.generate.ai_style.rating import load_voice_scorer

    parser = argparse.ArgumentParser(prog="python -m creek.author.revoice_check")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--vault", type=Path, required=True)
    args = parser.parse_args(argv)
    scorer = load_voice_scorer(args.vault)
    output = args.output.read_text(encoding="utf-8")
    check = check_revoice(
        args.source.read_text(encoding="utf-8"),
        output,
        rating=None if scorer is None else scorer.rate(output),
    )
    reason = check.reason
    report = {
        "reason": None if reason is None else reason.value,
        "voice_check": check.as_dict(),
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if check.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
