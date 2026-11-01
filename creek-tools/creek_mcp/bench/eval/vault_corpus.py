"""The owner-run corpus: a bounded, tier-gated sample of a real Creek vault.

#1875 asks whether a candidate model sounds like the owner's Higher Self. The
synthetic corpus in :mod:`creek_mcp.bench.eval.voice_corpus` cannot answer
that — it is one invented voice — so ``bench eval --corpus-vault`` reads the
owner's own vault instead. It is an **owner-run** path: the command refuses it
under CI and without an explicit acknowledgement, and nothing in this
repository's tests ever points it at a real vault (they build synthetic vault
fixtures under ``tmp_path``).

**Admission, before anything is built.** A fragment enters the corpus only
when all of these hold:

- its tier, read by :func:`creek.classify.privacy_filter.raw_privacy_tier`
  (the shared fail-closed reader: a missing, empty or unrecognised
  ``privacy_tier`` reads as INTIMATE), is ``open`` or ``personal``. An explicit
  ``unclassified`` is excluded too: content nobody has vouched for is not
  shipped to a provider on a guess;
- :attr:`creek.models.Fragment.voice_proxy_eligible` holds — self-authored
  and not INTIMATE;
- its ``voice_weight`` is positive. Ingest stamps ``0.0`` on AI turns as a
  second, independent quarantine (see :mod:`creek.generate.voice`'s
  ``_eligible_register``), so a mis-set authorship axis still cannot admit AI
  prose.

Fragments are walked through :func:`creek.vault.reader.iter_vault_fragments`,
the shared loader, so the symlink containment guard and schema validation
apply unchanged; a file that fails validation is never seen at all.

**Bounded.** The admitted fragments are shuffled by the run's seed and taken
in that order until :data:`VAULT_MAX_ENTRIES` (or the caller's smaller cap) or
:data:`VAULT_MAX_TOTAL_WORDS` is reached. Each body is whitespace-collapsed
and truncated to :data:`VAULT_MAX_ENTRY_WORDS`. So one run's prompt is bounded
in size and spend whatever the vault holds, and one seed always draws the same
sample from the same vault.

**Opaque in the prompt.** Each sampled fragment appears under an alias
(``entry-0000`` …) rather than its fragment id, which can be derived from a
title. Grounding is scored against the aliases; the alias-to-id map stays on
:class:`VaultCorpus` and is written only to the owner's private review file.

Read-only: nothing here writes to the vault. Counts are the only thing this
module reports about what it read.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from creek.classify.privacy_filter import max_source_tier, raw_privacy_tier
from creek.models import PrivacyTier
from creek.vault.reader import iter_vault_fragments
from creek_mcp.bench.eval.voice_corpus import MAX_ENTRIES, MIN_ENTRIES, VoiceCorpus
from creek_mcp.tools.handshake import vault_available

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Fragment

VAULT_MAX_ENTRIES: Final[int] = MAX_ENTRIES
"""Most fragments one run samples: the top of #1875's 10-20 range."""

VAULT_MAX_ENTRY_WORDS: Final[int] = 300
"""Words kept from one fragment; a longer body is truncated, never skipped."""

VAULT_MAX_TOTAL_WORDS: Final[int] = 4000
"""Words the whole sample may carry — the bound on prompt size and spend."""

ADMITTED_TIERS: Final[frozenset[PrivacyTier]] = frozenset(
    {PrivacyTier.OPEN, PrivacyTier.PERSONAL}
)
"""The only tiers that may reach a candidate provider from a real vault."""

FRAGMENTS_ROOT: Final[str] = "01-Fragments"
"""The vault subtree the shared loader walks."""

_ALIAS: Final[str] = "entry-{:04d}"
_NOT_A_VAULT: Final[str] = "--corpus-vault does not name a Creek vault"
_BAD_CAP: Final[str] = f"entries must be between {MIN_ENTRIES} and {VAULT_MAX_ENTRIES}"
_TOO_FEW: Final[str] = (
    "the vault yields {sampled} admissible fragments within the bounds "
    f"({{admitted}} admitted in all); at least {MIN_ENTRIES} are needed"
)


class VaultCorpusError(ValueError):
    """The vault cannot supply a bounded, admissible voice corpus."""


@dataclass(frozen=True, slots=True)
class VaultCorpusCounts:
    """What the loader saw, as counts only — never text, ids or paths.

    Attributes:
        scanned: Valid fragments the shared loader returned.
        admitted: Fragments that passed every admission rule.
        excluded_by_tier: Refused because their tier is not open or personal
            (INTIMATE, untiered, missing or unrecognised).
        excluded_by_authorship: Of the remainder, refused because they are
            not self-authored or carry a zero ``voice_weight``.
        sampled: Fragments in the bounded sample.
        words: Words in the bounded sample.
    """

    scanned: int
    admitted: int
    excluded_by_tier: int
    excluded_by_authorship: int
    sampled: int
    words: int

    def to_json_dict(self) -> dict[str, Any]:
        """Return the counts as a JSON-serialisable mapping."""
        return {
            "scanned": self.scanned,
            "admitted": self.admitted,
            "excluded_by_tier": self.excluded_by_tier,
            "excluded_by_authorship": self.excluded_by_authorship,
            "sampled": self.sampled,
            "words": self.words,
        }


@dataclass(frozen=True, slots=True)
class VaultCorpus:
    """A bounded sample of a real vault, ready to prompt and score against.

    Attributes:
        corpus: The sample in the shape every scorer reads. Its
            ``fragment_ids`` are the opaque aliases the prompt shows.
        source_ids: The real fragment id behind each alias, in alias order.
            Owner-only: written to the private review file, never the artifact.
        tier: The most sensitive tier in the sample, which is the tier a
            candidate provider is resolved at.
        counts: What the loader saw.
    """

    corpus: VoiceCorpus
    source_ids: tuple[str, ...]
    tier: PrivacyTier
    counts: VaultCorpusCounts


def tier_admitted(raw: dict[str, object]) -> bool:
    """Return whether a note's raw tier may reach a provider from a real vault.

    Args:
        raw: The note's raw frontmatter, before model defaults.

    Returns:
        ``True`` only for an explicit ``open`` or ``personal`` tier.
    """
    return raw_privacy_tier(raw) in ADMITTED_TIERS


def voice_admitted(fragment: Fragment) -> bool:
    """Return whether *fragment* is the owner's own, un-quarantined prose.

    Args:
        fragment: A validated fragment.

    Returns:
        ``True`` when it is voice-proxy eligible and its voice weight is
        positive.
    """
    return fragment.voice_proxy_eligible and fragment.voice_weight > 0


def _bounded(body: str) -> str:
    """Collapse *body*'s whitespace and keep its first words, up to the cap."""
    return " ".join(body.split()[:VAULT_MAX_ENTRY_WORDS])


def _sample(
    admitted: list[tuple[str, PrivacyTier, str]], *, seed: int, cap: int
) -> list[tuple[str, PrivacyTier, str]]:
    """Return a seeded, bounded sample of ``(id, tier, body)`` records."""
    order = list(range(len(admitted)))
    random.Random(seed).shuffle(order)
    picked: list[tuple[str, PrivacyTier, str]] = []
    words = 0
    for index in order:
        ident, tier, body = admitted[index]
        size = len(body.split())
        if size == 0 or words + size > VAULT_MAX_TOTAL_WORDS:
            continue
        picked.append((ident, tier, body))
        words += size
        if len(picked) == cap:
            break
    return picked


def _admit(
    records: list[tuple[Path, Fragment, str, dict[str, object]]],
) -> tuple[list[tuple[str, PrivacyTier, str]], int]:
    """Return the admitted ``(id, tier, bounded body)`` records and the tier refusals.

    Args:
        records: Every fragment the shared loader returned.

    Returns:
        The admitted records in loader order, and how many were refused on
        tier alone.
    """
    by_tier = [record for record in records if tier_admitted(record[3])]
    admitted = [
        (fragment.id, raw_privacy_tier(raw), _bounded(body))
        for _path, fragment, body, raw in by_tier
        if voice_admitted(fragment)
    ]
    return admitted, len(records) - len(by_tier)


def _corpus(
    vault: Path, picked: list[tuple[str, PrivacyTier, str]], counts: VaultCorpusCounts
) -> VaultCorpus:
    """Assemble the sampled records into a :class:`VaultCorpus` under aliases."""
    return VaultCorpus(
        corpus=VoiceCorpus(
            root=vault.resolve(),
            fragment_ids=tuple(_ALIAS.format(index) for index in range(len(picked))),
            voice_sample="\n\n".join(body for _ident, _tier, body in picked),
            entry_count=len(picked),
        ),
        source_ids=tuple(ident for ident, _tier, _body in picked),
        tier=max_source_tier(tier for _ident, tier, _body in picked),
        counts=counts,
    )


def load_vault_corpus(
    vault: Path, *, seed: int, max_entries: int = VAULT_MAX_ENTRIES
) -> VaultCorpus:
    """Read *vault* and return its bounded, tier-gated voice sample.

    Args:
        vault: The owner's Creek vault root. Read, never written.
        seed: The run's seed, which selects the sample.
        max_entries: Most fragments to sample, inside #1875's 10-20 range.

    Returns:
        The sample, its aliases' sources, its routing tier and its counts.

    Raises:
        VaultCorpusError: When *vault* is not a Creek vault, *max_entries* is
            out of range, or too few admissible fragments fit the bounds.
            Messages carry counts only.
    """
    if not MIN_ENTRIES <= max_entries <= VAULT_MAX_ENTRIES:
        raise VaultCorpusError(_BAD_CAP)
    if not vault_available(vault):
        raise VaultCorpusError(_NOT_A_VAULT)
    records = iter_vault_fragments(vault / FRAGMENTS_ROOT)
    admitted, excluded_by_tier = _admit(records)
    picked = _sample(admitted, seed=seed, cap=max_entries)
    if len(picked) < MIN_ENTRIES:
        raise VaultCorpusError(
            _TOO_FEW.format(sampled=len(picked), admitted=len(admitted))
        )
    counts = VaultCorpusCounts(
        scanned=len(records),
        admitted=len(admitted),
        excluded_by_tier=excluded_by_tier,
        excluded_by_authorship=len(records) - excluded_by_tier - len(admitted),
        sampled=len(picked),
        words=sum(len(body.split()) for _ident, _tier, body in picked),
    )
    return _corpus(vault, picked, counts)
