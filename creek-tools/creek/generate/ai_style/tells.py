"""The tell catalog: typed, registrable detectors of writing features.

A :class:`Tell` pairs metadata (which feature it measures, its family,
how it should be handled) with two pure functions: ``measure`` (the
draft's rate for the feature) and ``locate`` (where the feature occurs,
for building findings). The scanner combines a tell's measured rate with
the user's stored rate for the same ``feature_key`` to decide whether the
draft diverges enough to flag — so a tell never decides on its own that
something is "bad"; it only measures. Whether a divergence matters is
always relative to the user's fingerprint.

This framework module ships exactly one seed tell (a literal
``2025-xx-xx`` placeholder date) to prove the engine end to end. The real
catalogs are registered by later issues (FEAT-040.3 through .7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from creek.generate.ai_style.model import Span

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from creek.generate.ai_style.model import Category

Handling = Literal["autofix", "prevent", "surface"]
"""How a fired tell should be remediated downstream: deterministic repair,
prompt-side prevention, or human/LLM surfacing."""

Polarity = Literal["avoid", "signature"]
"""``avoid``: over-using the feature relative to the user is the concern.
``signature``: under-using a feature the user characteristically employs
is the concern."""

_WORD_RE = re.compile(r"\b\w+\b")


def word_count(text: str) -> int:
    """Return the number of word tokens in *text* (minimum 1).

    Clamped to at least 1 so rate computations never divide by zero on
    empty or whitespace-only input.

    Args:
        text: The body of text to count.

    Returns:
        The word count, never less than 1.
    """
    return max(len(_WORD_RE.findall(text)), 1)


def rate_per_kwords(occurrences: int, text: str) -> float:
    """Return *occurrences* normalised to a per-1000-words rate.

    Length-normalisation is the core defence against a long document being
    punished for a single stray feature.

    Args:
        occurrences: Raw count of the feature in *text*.
        text: The text the count came from (for its word count).

    Returns:
        Occurrences per 1000 words.
    """
    return occurrences * 1000.0 / word_count(text)


@dataclass(frozen=True)
class Tell:
    """A registrable detector for one measurable writing feature.

    Attributes:
        id: Stable unique identifier (e.g. ``"placeholder_date"``).
        category: The tell's family.
        feature_key: The fingerprint key this tell measures against (e.g.
            ``"placeholder_date_rate"``). Multiple tells may share a key.
        handling: How a fired tell is remediated (``autofix`` / ``prevent``
            / ``surface``).
        polarity: Whether over- or under-use is the concern.
        description: One-line human-readable summary.
        caveat: The legitimate-use / false-positive note from the field
            guide. Surfaced in reports so operators don't witch-hunt.
        generic_prior: The expected human rate when the fingerprint has no
            measurement for this feature (the sparse-corpus fallback).
        margin: Optional per-tell divergence margin overriding the config
            default. ``0.0`` means "any divergence in the bad direction
            fires" (use for features that are never legitimate, like
            placeholder text). A ``0.0`` margin also locks the tell's
            polarity (it can never become a per-user signature).
        lock_polarity: When ``True``, the tell keeps its declared
            :attr:`polarity` regardless of the user's measured rate — the
            per-user signature derivation (#635) is skipped. Defaults to
            ``False``; a ``margin`` of ``0.0`` locks polarity implicitly too,
            so never-legitimate artifacts need not set this.
        signature_threshold: Optional per-tell override for the rate at or
            above which the user is deemed to *characteristically* use this
            feature (so under-use becomes the concern). ``None`` falls back to
            ``AIStyleConfig.signature_polarity_threshold``.
        measure: ``measure(text) -> float`` returning the draft's rate.
        locate: ``locate(text) -> list[Span]`` returning occurrence spans
            for findings. Empty list yields a single doc-level finding.
        contexts: The scan contexts this tell applies to (e.g.
            ``frozenset({"comment"})`` for a comment-only tell). ``None``
            (the default) means the tell applies in every context.
    """

    id: str
    category: Category
    feature_key: str
    handling: Handling
    polarity: Polarity
    description: str
    caveat: str
    measure: Callable[[str], float]
    locate: Callable[[str], list[Span]]
    generic_prior: float = 0.0
    margin: float | None = None
    lock_polarity: bool = False
    signature_threshold: float | None = None
    contexts: frozenset[str] | None = None

    def polarity_is_locked(self) -> bool:
        """Return whether this tell's polarity is exempt from #635 derivation.

        Never-legitimate artifacts are intrinsic ``avoid`` tells: a fabricated
        DOI or a placeholder date can never be one of the user's signatures, so
        they are exempt from per-user polarity derivation. These are exactly
        the tells pinned with a ``0.0`` margin (any over-use fires), plus any
        tell that opts out explicitly via :attr:`lock_polarity`.

        Returns:
            ``True`` when the declared :attr:`polarity` must be used as-is.
        """
        return self.lock_polarity or self.margin == 0.0

    def applies_in(self, context: str) -> bool:
        """Return whether this tell runs in the given scan *context*.

        Args:
            context: The scan context (e.g. ``"article"`` or ``"comment"``).

        Returns:
            ``True`` when the tell is context-agnostic or lists *context*.
        """
        return self.contexts is None or context in self.contexts


def effective_polarity(
    tell: Tell,
    user_rate: float,
    *,
    signature_threshold: float,
) -> Polarity | None:
    """Return *tell*'s polarity for this user, derived from their rate (#635).

    Polarity is derived per-user rather than fixed at registration, but with
    discernment — the declared :attr:`Tell.polarity` gates *which* derivation
    applies, so the AI-tell catalog is never wholesale-reinforced (a
    triad-loving writer should not have triad padding *reinforced*, only
    tolerated):

    * **avoid** tells (the AI-tell catalog) always stay ``avoid``. Over-use is
      the only concern; the existing vault-relative suppression (a draft below
      the user's rate scores ``0``) is unchanged.
    * **signature** tells (genuine authentic-voice features, e.g. the one-line
      rhythm) derive per-user: at or above ``signature_threshold`` the user
      characteristically employs it → ``signature`` (under-use raises voice
      distance, reinforcing the voice); below the threshold the tell is
      **inert** (``None``) — the user does not use it, so neither over- nor
      under-use is *their* concern, and it never fires spuriously on a draft
      that happens to use the feature.

    Never-legitimate artifacts (:meth:`Tell.polarity_is_locked`) are exempt and
    always return their declared polarity.

    Args:
        tell: The tell whose effective polarity is wanted.
        user_rate: The user's measured rate for the feature (or generic prior).
        signature_threshold: The rate at or above which the feature counts as a
            user signature (a per-tell override or the config default).

    Returns:
        ``"avoid"`` / ``"signature"`` to score with that polarity, or ``None``
        when the tell is inert for this user (no contribution, no finding).
    """
    if tell.polarity_is_locked():
        return tell.polarity
    if tell.polarity == "signature":
        return "signature" if user_rate >= signature_threshold else None
    return "avoid"


TELL_REGISTRY: dict[str, Tell] = {}
"""The global, append-only registry of tells, keyed by :attr:`Tell.id`."""


def register(tell: Tell) -> Tell:
    """Register *tell* in :data:`TELL_REGISTRY` and return it.

    Args:
        tell: The tell to register.

    Returns:
        The same tell, so callers can register at module scope and keep a
        reference in one expression.

    Raises:
        ValueError: If a tell with the same ``id`` is already registered.
    """
    if tell.id in TELL_REGISTRY:
        msg = f"duplicate tell id: {tell.id!r}"
        raise ValueError(msg)
    TELL_REGISTRY[tell.id] = tell
    return tell


def get_tells(categories: Iterable[str]) -> list[Tell]:
    """Return registered tells whose category is in *categories*.

    Args:
        categories: The enabled category names.

    Returns:
        Matching tells, in registration order.
    """
    allowed = set(categories)
    return [t for t in TELL_REGISTRY.values() if t.category in allowed]


# --- Seed tell: literal placeholder dates ---------------------------------
# A draft that still contains ``2025-xx-xx`` (or ``access-date=2025-XX-XX``)
# left an LLM fill-in-the-blank unfilled. This is never legitimate human
# output, so its margin is 0 and its generic prior is 0.

_PLACEHOLDER_DATE_RE = re.compile(r"\b\d{4}-[xX]{2}-[xX]{2}\b")


def _measure_placeholder_date(text: str) -> float:
    """Return the per-1000-words rate of placeholder dates in *text*."""
    return rate_per_kwords(len(_PLACEHOLDER_DATE_RE.findall(text)), text)


def _locate_placeholder_date(text: str) -> list[Span]:
    """Return spans of every placeholder date occurrence in *text*."""
    return [Span(m.start(), m.end()) for m in _PLACEHOLDER_DATE_RE.finditer(text)]


PLACEHOLDER_DATE = register(
    Tell(
        id="placeholder_date",
        category="mechanical",
        feature_key="placeholder_date_rate",
        handling="autofix",
        polarity="avoid",
        description="Unfilled LLM placeholder date such as 2025-xx-xx.",
        caveat="Never legitimate in finished prose; not affected by the "
        "user's voice, so its margin is fixed at 0.",
        measure=_measure_placeholder_date,
        locate=_locate_placeholder_date,
        generic_prior=0.0,
        margin=0.0,
    ),
)
