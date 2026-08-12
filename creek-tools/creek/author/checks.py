"""The six deterministic reflection checks for the Creek Writing Desk (#473).

Each check inspects a drafted body (and its evidence) for exactly one rubric
dimension and returns zero or more :class:`~creek.author.models.ReflectionFinding`
records. The checks are *deterministic* — no LLM — so the mutation tests in
``tests/test_reflection.py`` can assert the exact verdict and dimension a defect
produces. Citation completeness and privacy compliance are HARD gates for the
research medium; the rest are softer rubric divergences.

The privacy check resolves each cited fragment's
:class:`~creek.models.PrivacyTier` with
:func:`creek.vault.reader.try_load_fragment`, walking the corpus subtrees
file by file rather than through :func:`~creek.vault.reader.iter_vault_fragments`
(or the desk's own ``_load_corpus``), both of which materialise every fragment
in the vault into a list before the caller sees the first record. The voice
check reuses the FEAT-040.x AI-style scanner
(:func:`creek.generate.ai_style.scanner.scan`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from creek.author.models import ReflectionFinding
from creek.config import load_config, resolve_config_path
from creek.generate.ai_style.scanner import scan
from creek.generate.grounding import scan_biographical_sentences
from creek.generate.jargon import detect_unglossed_jargon

# Reuse the canonical INC-019 alias vocabulary (public constants) rather than
# duplicating it, so the ontological-accuracy check and the model validators
# stay in sync.
from creek.models import (
    FREQUENCY_LEGACY_ALIASES,
    MODE_LEGACY_ALIASES,
    PHASE_LEGACY_ALIASES,
    PrivacyTier,
)
from creek.vault.reader import CORPUS_SUBDIRS, try_load_fragment

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author.models import EvidenceBundle
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint
    from creek.generate.grounding import EmbeddingFn
    from creek.models import MediumContract

logger = logging.getLogger(__name__)

#: Privacy tiers from least to most restrictive; index = restrictiveness rank.
#: ``UNCLASSIFIED`` is treated as the most restrictive (fail closed) — this
#: table deliberately differs from :mod:`creek.classify.privacy_filter` (which
#: ranks ``UNCLASSIFIED`` with ``PERSONAL``, #876) and from
#: :mod:`creek_mcp.tier_ceiling` (which now matches ``privacy_filter``, #961).
#: This is the *leak gate* ordering: a cited fragment nobody has vouched for
#: must always count as over-tier here, regardless of what the caller's
#: ceiling admits on read, so it is pinned at the top rather than alongside
#: ``PERSONAL``. All four tables (this one, ``privacy_filter``,
#: ``tier_ceiling``, and the escalate-only merge in
#: :mod:`creek.classify.privacy_pass`, which ranks ``UNCLASSIFIED`` *below*
#: ``OPEN``) are pinned together by ``tests/test_mcp_tier_ceiling.py`` (see
#: ``test_unclassified_ranks_differ_by_context_on_purpose``).
_TIER_RANK: dict[PrivacyTier, int] = {
    PrivacyTier.OPEN: 0,
    PrivacyTier.PERSONAL: 1,
    PrivacyTier.INTIMATE: 2,
    PrivacyTier.UNCLASSIFIED: 3,
}

_MOST_RESTRICTIVE_RANK = max(_TIER_RANK.values())
"""Fail-closed rank for an unranked *fragment* tier (#509).

A future :class:`~creek.models.PrivacyTier` not yet added to ``_TIER_RANK`` is
treated as the most restrictive — so an unknown cited tier still trips the
leak gate rather than raising ``KeyError`` (consistent with the
``UNCLASSIFIED = most-restrictive`` convention)."""

_LEAST_RESTRICTIVE_RANK = min(_TIER_RANK.values())
"""Fail-closed rank for an unranked *ceiling* tier (#509).

An unrecognised contract ``default_privacy_tier`` gates as strictly as
possible (the lowest ceiling), so more cited tiers count as over-tier rather
than fewer — the conservative direction for a HARD gate."""

_NO_CONTRACT_CEILING: PrivacyTier = PrivacyTier.OPEN
"""Fail-closed ceiling for a caller that supplied no contract at all (#1310).

A *different* condition from the one :data:`_LEAST_RESTRICTIVE_RANK` covers:
there the contract exists and its ``default_privacy_tier`` is merely unranked;
here there is no contract to read a ceiling from. Both fail closed in the same
direction — gate at the strictest tier — so a contract-less caller gets the
HARD gate rather than no gate. Every shipped medium template declares
``default_privacy_tier: open``, so this is a no-op for contract-bearing
callers. Skipping instead is how ``creek author`` shipped an intimate
fragment's protected text to stdout with ``verdict=PASS``."""

#: Legacy alias keys whose presence in a body signals non-canonical taxonomy
#: (INC-019). Maps each deprecated alias to its canonical replacement.
_LEGACY_ALIASES: dict[str, str] = (
    PHASE_LEGACY_ALIASES | MODE_LEGACY_ALIASES | FREQUENCY_LEGACY_ALIASES
)


def check_citation_completeness(evidence: EvidenceBundle) -> list[ReflectionFinding]:
    """Flag any claim that asserts something without a source fragment (HARD).

    A research draft may make no unsupported assertion: a claim whose
    ``source_fragments`` is empty is an uncited claim and fails the hard
    citation gate.

    Args:
        evidence: The evidence the draft was rendered from.

    Returns:
        One ``HIGH`` finding per uncited claim, dimension
        ``"citation_completeness"``.
    """
    return [
        ReflectionFinding(
            dimension="citation_completeness",
            severity="HIGH",
            message=f"Uncited claim has no source fragments: {claim.claim!r}.",
        )
        for claim in evidence.claims
        if not claim.source_fragments
    ]


def check_biographical_grounding(
    body: str,
    evidence: EvidenceBundle,
    *,
    embedding_fn: EmbeddingFn | None,
    grounding_lower: float,
    voice_core: str | None = None,
) -> list[ReflectionFinding]:
    """Flag invented first-person biographical claims in the body (HARD).

    A research/desk draft must not assert a biographical fact about the owner
    (an event, an upbringing, a past state) that no source supports — e.g.
    amplifying a brief's "the LDS Christ I was handed" into a childhood the
    corpus never mentions (issue #515). Each grounded claim's text plus the
    optional voice-core brief form the source corpus; a first-person
    biographical sentence whose cosine similarity falls below *grounding_lower*
    against all of them is fabrication.

    The check is dormant — and returns no findings — when *embedding_fn* is
    ``None``, mirroring how :func:`check_voice_fidelity` stays dormant without
    a fingerprint. This keeps the deterministic reflection node free of the
    sentence-transformer import unless a caller wires the embedder in.

    Args:
        body: The drafted prose under review.
        evidence: The evidence the draft was rendered from; each claim's text
            is a legitimate grounding source.
        embedding_fn: Embedding callable, or ``None`` to skip the check.
        grounding_lower: Cosine floor a biographical sentence must clear
            against some source to count as grounded.
        voice_core: The voice-core brief text, treated as tone guidance the
            claim may also trace to, or ``None``.

    Returns:
        One ``HIGH`` ``biographical_grounding`` finding per ungrounded
        biographical sentence.
    """
    if embedding_fn is None:
        return []
    source_texts = [claim.claim for claim in evidence.claims]
    if voice_core:
        source_texts.append(voice_core)
    return [
        ReflectionFinding(
            dimension="biographical_grounding",
            severity="HIGH",
            message=(
                "First-person biographical claim is not grounded in any "
                f"source (similarity {finding.max_similarity:.2f} < "
                f"{grounding_lower:.2f}): {finding.sentence!r}."
            ),
        )
        for finding in scan_biographical_sentences(
            body,
            source_texts=source_texts,
            embedding_fn=embedding_fn,
            threshold=grounding_lower,
        )
    ]


def _as_tier(value: PrivacyTier) -> PrivacyTier:
    """Coerce a tier loaded from a fragment back into the enum.

    :class:`~creek.models.Fragment` is configured with ``use_enum_values=True``,
    so Pydantic hands back the plain ``str`` behind the member even though the
    annotation says ``PrivacyTier``. Left as a ``str`` it misses every
    :data:`_TIER_RANK` lookup — the gate would then fail closed at the most
    restrictive rank for *every* cited fragment — and rendering the finding
    would raise on the missing ``.value``.

    Args:
        value: A ``privacy_tier`` as loaded from a fragment's frontmatter.

    Returns:
        The corresponding :class:`~creek.models.PrivacyTier` member.
    """
    return PrivacyTier(value)


def _fragment_rank(tier: PrivacyTier) -> int:
    """Return the restrictiveness rank of a cited *fragment's* tier.

    :data:`_TIER_RANK` is read at call time rather than captured, so a test can
    shrink the table to stand in for a future tier nobody has ranked yet; such a
    tier fails closed at :data:`_MOST_RESTRICTIVE_RANK` (#509).

    Args:
        tier: The fragment's privacy tier.

    Returns:
        The tier's rank; higher is more restrictive.
    """
    return _TIER_RANK.get(tier, _MOST_RESTRICTIVE_RANK)


@dataclass(frozen=True, slots=True)
class _CitedFragment:
    """One cited fragment id's resolved tier and every body stored under it.

    A vault id is not unique: the same id can sit in more than one corpus
    subtree (an import, a copy, a re-export), and those records can disagree
    about both tier and body. This type folds the sightings together for the
    leak gate (#1341).

    The fold is **monotone** — the tier never becomes less restrictive and no
    body is ever dropped — and **commutative**, since rank-max and
    append-if-absent both are. Walk order therefore cannot change the verdict,
    which is what lets the resolver widen from one subtree to three without
    acquiring a tie-break rule to argue about.

    Its accepted cost: a genuinely ``open`` body is refused when a same-id
    record above the ceiling exists, because the tier is treated as a property
    of the *id*. That trade is a false-positive revise round in exchange for
    never shipping a leak — the safe direction for a one-way ratchet.

    Attributes:
        tier: The most restrictive tier seen for this id.
        bodies: Every distinct body seen for this id, at *every* tier rather
            than only the winning one. Dropping the below-winner bodies would
            reopen the hole one rank down: a draft could reproduce the ``open``
            twin of an id that resolved ``intimate``.
    """

    tier: PrivacyTier
    bodies: tuple[str, ...]

    def merged_with(self, tier: PrivacyTier, body: str) -> _CitedFragment:
        """Return a new record with one more sighting of this id folded in.

        Args:
            tier: The sighted record's privacy tier (coerced via
                :func:`_as_tier`, since it arrives as a plain ``str``).
            body: The sighted record's stored body.

        Returns:
            A new frozen record carrying the more restrictive of the two tiers
            and every body of both. ``self`` is left untouched.
        """
        sighted = _as_tier(tier)
        stricter = (
            sighted
            if _fragment_rank(sighted) > _fragment_rank(self.tier)
            else self.tier
        )
        bodies = self.bodies if body in self.bodies else (*self.bodies, body)
        return _CitedFragment(tier=stricter, bodies=bodies)


def _scan_subtree_for_cited(
    root: Path,
    cited: set[str],
    resolved: dict[str, _CitedFragment],
) -> None:
    """Fold every cited fragment found under *root* into *resolved*, in place.

    The walk is sorted, mirroring :func:`~creek.vault.reader.iter_vault_fragments`,
    so a run is reproducible across hosts. Correctness does not depend on it —
    :class:`_CitedFragment`'s fold is order-independent — but a host-dependent
    walk order in a HARD gate is a debugging trap worth not setting.

    Unreadable files and markdown that is not a Creek fragment are skipped with
    exactly the tolerance :func:`~creek.vault.reader.try_load_fragment` callers
    already use. That parity is load-bearing now that the walk reaches beyond
    ``01-Fragments``: the reference notes and the ``_author.md`` manifests
    living in the other two subtrees must skip, not raise.

    Args:
        root: An existing corpus subtree to walk.
        cited: The fragment ids the draft cites.
        resolved: Accumulator mapping cited id to its folded record; mutated.
    """
    for md_file in sorted(root.rglob("*.md")):
        try:
            record = try_load_fragment(md_file)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if record is None:
            continue
        fragment, body, _raw = record
        if fragment.id not in cited:
            continue
        # No `tier_within_override` here, deliberately. The admission ceiling
        # decides what the specialists may *gather*; this gate must read the
        # fragment's TRUE tier, or it goes blind to exactly the content it
        # exists to catch. Filtering here is the single most tempting wrong
        # edit in this file.
        seen = resolved.get(fragment.id)
        resolved[fragment.id] = (
            seen.merged_with(fragment.privacy_tier, body)
            if seen is not None
            else _CitedFragment(tier=_as_tier(fragment.privacy_tier), bodies=(body,))
        )


def _resolve_cited_tiers(
    evidence: EvidenceBundle,
    vault: Path,
) -> dict[str, _CitedFragment]:
    """Resolve each cited fragment id against every corpus subtree in *vault*.

    Searches all of :data:`~creek.vault.reader.CORPUS_SUBDIRS` —
    ``01-Fragments``, ``09-Reference`` and ``11-Other-Authors`` — which is the
    same tuple the desk's specialists gather evidence from. Sharing the object
    is the point: while the gate kept its own one-entry list it policed a
    subtree the specialists had long since outgrown, and an over-tier fragment
    cited out of either other subtree resolved to nothing at all (#1341). An
    absent subtree is skipped on its own; the absence of one never disables the
    others.

    Records sharing an id are folded by :class:`_CitedFragment`: most
    restrictive tier wins and every body is kept, an order-independent merge.
    The fragment's own tier is read — never
    :func:`~creek.classify.privacy_filter.tier_within_override`'s admitted view.

    No containment check is applied to symlinked or otherwise out-of-tree files,
    and none is wanted. Under a monotone fold an extra record can only raise a
    tier or add a body, so a planted shadow cannot downgrade or mask anything;
    adding a skip path would introduce the one genuinely unsafe move available
    here — dropping a body, and with it a leak.

    Honest cost: worst case this parses all three subtrees once per ``review``
    call, and the conductor may call ``review`` once per round up to
    ``max_author_rounds`` (default 3, capped at 10). The common miss — an LLM
    inventing a fragment id — is now always exhaustive, where the old
    single-root walk could exit early. No memo is kept on purpose: a cache with
    wrong invalidation on a HARD leak gate is worse than a slow gate.

    Args:
        evidence: The evidence whose cited fragment ids are resolved.
        vault: The vault root to load fragments from.

    Returns:
        A mapping of cited fragment id to its folded :class:`_CitedFragment`.
        Ids with no matching file anywhere in the corpus are omitted.
    """
    cited = set(evidence.all_source_fragments())
    resolved: dict[str, _CitedFragment] = {}
    if not cited:
        return resolved
    for sub in CORPUS_SUBDIRS:
        subtree = vault / sub
        # Per-root, never global: bailing on the whole walk because the first
        # root is missing left a vault whose corpus lives under `09-Reference`
        # with no privacy gate at all.
        if subtree.is_dir():
            _scan_subtree_for_cited(subtree, cited, resolved)
    return resolved


_MIN_PROTECTED_LEAK_WORDS = 4
"""Shortest protected snippet that can trip the HARD privacy gate (#508).

A one-to-three-word over-tier fragment body (a single common word or phrase)
can appear verbatim in innocuous prose by coincidence; treating that as a leak
would force a REVISE/ESCALATE on a draft that disclosed nothing. Substantive
verbatim overlap (>= this many words) remains the deterministic leak signal;
paraphrase and shorter snippets are left to the semantic judge (#474).
"""

_WHITESPACE_RE = re.compile(r"\s+")

_WORD_CHAR_RE = re.compile(r"\w")
r"""Matches a single word character, testing whether an edge can carry a boundary.

A ``\b`` anchor asserts a word/non-word *transition*, so it can never hold on an
edge whose own character is already punctuation — as a whole fragment body's
first/last character usually is (#939). :func:`_is_verbatim_leak` uses this to
apply a boundary assertion to exactly the edges where one is meaningful.
"""


def _is_verbatim_leak(protected: str, body: str) -> bool:
    """Return whether *protected* appears in *body* as a substantive phrase.

    Tightens the prior raw-substring test to cut coincidental hits (#508):

    * the protected snippet must be at least :data:`_MIN_PROTECTED_LEAK_WORDS`
      words long (a single common word/phrase is too coincidence-prone for a
      HARD gate), and
    * it must not be glued into a larger word. The boundary is asserted only on
      an edge whose own character is a word character, so a snippet that starts
      or ends with punctuation is still detected rather than silently missed
      (#939).

    Whitespace is normalised on both sides first, so a reflowed line break in
    the draft cannot hide an otherwise-verbatim leak.

    Args:
        protected: The over-tier fragment's protected body text.
        body: The drafted prose under review.

    Returns:
        ``True`` when the snippet is substantive and present as a bounded phrase.
    """
    if len(protected.split()) < _MIN_PROTECTED_LEAK_WORDS:
        return False
    normalized_protected = _WHITESPACE_RE.sub(" ", protected).strip()
    normalized_body = _WHITESPACE_RE.sub(" ", body)
    # The word-count guard above makes the snippet non-empty here, so its edge
    # characters are safe to index. Assert a boundary only where one can hold:
    # a `\b` on a punctuation edge never matches when the draft's adjacent
    # character is also punctuation or a string end, which let an exact
    # reproduction of a protected body slip through the HARD gate (#939).
    prefix = r"(?<!\w)" if _WORD_CHAR_RE.match(normalized_protected[0]) else ""
    suffix = r"(?!\w)" if _WORD_CHAR_RE.match(normalized_protected[-1]) else ""
    pattern = f"{prefix}{re.escape(normalized_protected)}{suffix}"
    return re.search(pattern, normalized_body) is not None


def _leaks_any_body(cited: _CitedFragment, body: str) -> bool:
    """Return whether the draft reproduces any text stored under a cited id.

    Every stored body counts, not only the one on the record that won the tier
    fold: the tier is a property of the *id*, so once that id resolves above the
    ceiling its lower-tier twin's text is protected too (#1341).

    Args:
        cited: The resolved record for one cited fragment id.
        body: The drafted prose under review.

    Returns:
        ``True`` when at least one stored body appears verbatim in the draft.
    """
    return any(_is_verbatim_leak(stored.strip(), body) for stored in cited.bodies)


_CONFIG_SOURCE = "author.max_reproduced_tier"
"""Names the operator's ``creek_config.yaml`` key as the ceiling's source.

Every ``_..._SOURCE`` constant is rendered verbatim into a finding message, and
``creek/cli.py``'s ``_emit_author_result`` prints those through Rich — so none
of them may contain square brackets, which Rich would read as markup
(``test_author_refusal_survives_markup_in_a_fragment_id``)."""

_CONTRACT_SOURCE = "the medium contract"
"""Names the medium contract as the ceiling's source.

Deliberately silent about *which* file: a contract is either the shipped
template or a vault copy shadowing it, and naming one path would send an
operator to edit a file that may not be the one in play."""

_NO_CONTRACT_SOURCE = "no medium contract"
"""Names :data:`_NO_CONTRACT_CEILING` as the ceiling's source.

Kept distinct from :data:`_CONTRACT_SOURCE` so the message says whether a
contract was read and declared this tier or none was found at all — two
different repairs."""


def _ceiling_rank(tier: PrivacyTier) -> int:
    """Return the restrictiveness rank of a *ceiling* tier.

    The counterpart of :func:`_fragment_rank`, and the fail-closed direction is
    the opposite one: an unranked ceiling drops to
    :data:`_LEAST_RESTRICTIVE_RANK` (rank 0, the strictest ceiling there is), so
    more cited tiers count as over-tier rather than fewer (#509).

    Both sides of the ceiling fold in :func:`_resolve_effective_ceiling` go
    through here, so an unranked tier on *either* side fails closed rather than
    winning the comparison by accident.

    Args:
        tier: A candidate ceiling tier.

    Returns:
        The tier's rank; higher is more permissive as a ceiling.
    """
    return _TIER_RANK.get(tier, _LEAST_RESTRICTIVE_RANK)


@dataclass(frozen=True, slots=True)
class _EffectiveCeiling:
    """The tier the leak gate enforces, plus which authority set it.

    The source travels with the tier because the finding message has to name the
    file an operator would edit to change the verdict: the two authorities live
    in different places (``creek_config.yaml`` outside the vault, the medium
    contract inside it) and pointing at the wrong one is a wasted debugging
    session.

    Attributes:
        tier: The effective ceiling — the more restrictive of the two sources.
        source: One of :data:`_CONFIG_SOURCE`, :data:`_CONTRACT_SOURCE` or
            :data:`_NO_CONTRACT_SOURCE`.
    """

    tier: PrivacyTier
    source: str


def _configured_max_reproduced_tier(vault: Path) -> PrivacyTier:
    """Read ``author.max_reproduced_tier`` from *vault*'s config, failing closed.

    This is the half of the ceiling an in-vault medium contract cannot reach:
    ``creek_config.yaml`` lives at ``00-Creek-Meta/creek_config.yaml`` and no
    template deploys it with a raised value (#1354).

    A config this function cannot read gates at :attr:`PrivacyTier.OPEN` rather
    than propagating: the MCP ``creek.author`` verb must not surface a traceback
    from inside a HARD gate. The caught set is exact, never widened — a stale
    ``CREEK_CONFIG`` raises ``FileNotFoundError`` (``creek/config.py:83-89``),
    which is an ``OSError``; pydantic's ``ValidationError`` subclasses
    ``ValueError``; ``yaml.YAMLError`` subclasses neither.

    The conversion itself cannot raise, and needs no second coercion:
    ``max_reproduced_tier`` is a ``Literal`` of exactly the four tier names,
    whose ``before`` validator already fails closed to ``"open"`` for anything
    else. The ``except`` exists for I/O and YAML failure alone.

    Args:
        vault: The vault root whose config is consulted.

    Returns:
        The operator's configured reproduction ceiling, or
        :attr:`PrivacyTier.OPEN` when the config cannot be read.
    """
    try:
        config = load_config(resolve_config_path(vault, None), warn_on_missing=False)
    except (OSError, ValueError, yaml.YAMLError):
        logger.warning(
            "Could not read author.max_reproduced_tier from the config for "
            "vault %s; failing closed to %r.",
            vault,
            PrivacyTier.OPEN.value,
        )
        return PrivacyTier.OPEN
    return PrivacyTier(config.author.max_reproduced_tier)


def _resolve_effective_ceiling(
    contract: MediumContract | None,
    configured: PrivacyTier,
) -> _EffectiveCeiling:
    """Fold the two ceiling authorities into the one the gate enforces.

    "More restrictive" is the **lower** :data:`_TIER_RANK` — a ``min``, never a
    ``max``. Word-association runs the other way ("take the more restrictive
    tier" reads like ``max``), and taking ``max`` here ships either a no-op or a
    widening of a HARD privacy gate;
    ``test_the_effective_ceiling_is_the_more_restrictive_not_the_more_permissive``
    is the direct detector for that inversion.

    Ranks are compared, not tiers, so an unranked tier on either side falls to
    :data:`_LEAST_RESTRICTIVE_RANK` and fails closed (#509).

    Ties go to the config (``<=``, not ``<``) for two reasons. At the shipped
    default both sources are ``open``, and the message should then name the
    authority that a vault-authored contract cannot widen. And an unranked
    contract tier ties at rank 0 with a configured ``open`` — it must never be
    *named* as the ceiling, since its rank is a fail-closed guess rather than
    something the contract declared.

    Args:
        contract: The medium contract, or ``None`` for a contract-less caller.
        configured: The operator's ``author.max_reproduced_tier``.

    Returns:
        The effective ceiling and the authority that set it.
    """
    declared, source = (
        (_NO_CONTRACT_CEILING, _NO_CONTRACT_SOURCE)
        if contract is None
        else (PrivacyTier(contract.default_privacy_tier), _CONTRACT_SOURCE)
    )
    if _ceiling_rank(configured) <= _ceiling_rank(declared):
        return _EffectiveCeiling(configured, _CONFIG_SOURCE)
    return _EffectiveCeiling(declared, source)


def check_privacy_compliance(
    body: str,
    evidence: EvidenceBundle,
    vault: Path | None,
    contract: MediumContract | None,
) -> list[ReflectionFinding]:
    """Flag a draft that leaks text above the effective privacy ceiling (HARD).

    The ceiling has **two** sources and is the more restrictive of them: the
    operator's ``author.max_reproduced_tier`` in ``creek_config.yaml`` and the
    medium contract's ``default_privacy_tier`` (see
    :func:`_resolve_effective_ceiling`). Until #1354 the contract was the only
    source — and a contract is authored *inside the vault*, so one edited YAML
    line in a skill file disarmed this gate. A contract can now only narrow it.

    For each cited fragment whose resolved tier is *more restrictive* than that
    ceiling, the draft breaches privacy iff the body still contains that
    fragment's protected text. The check is deterministic; only *vault* being
    ``None`` skips it, because without a vault there is no file to resolve a
    tier, a protected body, or a configured ceiling from. A missing *contract*
    does **not** skip: it gates at :data:`_NO_CONTRACT_CEILING` (#1310). A cited
    fragment id with no matching file in the vault is unresolvable (its tier
    cannot be known) and is skipped rather than guessed — the hard citation gate
    still requires every claim to carry an id.

    Args:
        body: The drafted prose under review.
        evidence: The evidence the draft was rendered from.
        vault: The vault root, or ``None`` to skip the check. Also the root the
            configured half of the ceiling is read from.
        contract: The medium contract whose ``default_privacy_tier`` is one of
            the two ceiling sources — it applies only where it is stricter than
            the configured tier — or ``None`` to fold
            :data:`_NO_CONTRACT_CEILING` in instead.

    Returns:
        One ``HIGH`` finding per leaked over-tier fragment, dimension
        ``"privacy_compliance"``.
    """
    if vault is None:
        return []
    # Reads the vault config on every call. Measured at 18.6ms against a real
    # config and 0.5ms with none, and `review` runs once per round, bounded by
    # `author.max_author_rounds` (<= 10) — at most ten reads per run, each
    # alongside an LLM call. No module-level memo on purpose: it would go stale
    # across vaults within one pytest session and hide a bad read, and the
    # value of this chokepoint is that exactly one place decides the ceiling.
    effective = _resolve_effective_ceiling(
        contract, _configured_max_reproduced_tier(vault)
    )
    ceiling = _ceiling_rank(effective.tier)
    findings: list[ReflectionFinding] = []
    # Sorted by fragment id so a draft leaking several fragments always reports
    # them in the same order, whichever subtree resolved each one first.
    for frag_id, cited in sorted(_resolve_cited_tiers(evidence, vault).items()):
        # Deterministic scope: this catches substantive verbatim leakage of the
        # protected body (see :func:`_is_verbatim_leak`). A paraphrase of
        # over-tier content — or a snippet too short to be a reliable signal —
        # is NOT caught here; that needs the semantic LLM judge (#474).
        rank = _fragment_rank(cited.tier)
        if rank > ceiling and _leaks_any_body(cited, body):
            findings.append(
                ReflectionFinding(
                    dimension="privacy_compliance",
                    severity="HIGH",
                    message=(
                        f"Cited fragment {frag_id!r} is {cited.tier.value!r} "
                        f"(above the effective {effective.tier.value!r} ceiling, "
                        f"set by {effective.source}) yet its protected text "
                        "appears in the draft."
                    ),
                )
            )
    return findings


def check_ontological_accuracy(body: str) -> list[ReflectionFinding]:
    """Flag legacy (non-canonical) taxonomy terms in the body (INC-019).

    A word-boundary regex match of any deprecated phase/mode/frequency alias
    key in the body raises a ``MID`` finding naming the canonical replacement.

    Caveat: a few alias keys are ordinary English words (e.g. ``"origins"``),
    so this word-boundary match can false-positive on non-ontological prose. It
    is a ``MID`` (soft) finding, so a false hit costs at most one revise round,
    not a hard block; semantic disambiguation is deferred to #474.

    Args:
        body: The drafted prose under review.

    Returns:
        One ``MID`` finding per legacy alias used, dimension
        ``"ontological_accuracy"``.
    """
    return [
        ReflectionFinding(
            dimension="ontological_accuracy",
            severity="MID",
            message=(
                f"Legacy taxonomy term {alias!r} used; the canonical "
                f"term is {canonical!r} (INC-019)."
            ),
        )
        for alias, canonical in _LEGACY_ALIASES.items()
        if re.search(rf"\b{re.escape(alias)}\b", body, flags=re.IGNORECASE)
    ]


def check_unglossed_jargon(body: str) -> list[ReflectionFinding]:
    """Flag bespoke ontology terms used on first mention without a gloss.

    Wraps the conservative :func:`detect_unglossed_jargon` detector: each
    un-glossed first mention of a bespoke term (a Frequency, Phase, Mode,
    altitude, or named concept) becomes a ``MID`` ``unglossed_jargon`` finding so
    a newcomer-facing draft can be revised to weave the gloss in. The detector is
    biased toward false negatives (it never flags a term with any nearby
    explanatory phrasing), so a finding here is a soft nudge, not a hard block.

    Args:
        body: The drafted prose under review.

    Returns:
        One ``MID`` finding per un-glossed first mention, dimension
        ``"unglossed_jargon"``.
    """
    return [
        ReflectionFinding(
            dimension="unglossed_jargon",
            severity="MID",
            message=finding.message,
        )
        for finding in detect_unglossed_jargon(body)
    ]


def _paradox_is_preserved(body: str, description: str) -> bool:
    """Return whether *body* keeps the tension of a paradox *description*.

    Deterministic heuristic: the paradox is preserved iff the body engages the
    paradox's own vocabulary in a both-sides way — either (a) it carries a
    contrast/tension cue (``but``, ``yet``, ``however``, ``while``,
    ``although``, ``tension``, ``paradox``, ``both``) AND mentions at least one
    significant (>=4-char) word of the paradox description, or (b) it mentions
    every significant word of the description. Requiring topical overlap
    alongside the contrast cue closes the bare-conjunction bypass — a stray
    "but" elsewhere in the draft no longer counts as preserving *this* tension.

    This is a deterministic approximation; genuine semantic both-sides
    detection is deferred to the LLM judge (#474). It can still miss a paraphrase
    that reframes the tension without the description's words.

    Args:
        body: The drafted prose under review.
        description: The paradox's neutral one-sentence description.

    Returns:
        ``True`` when the tension is preserved, ``False`` when flattened.
    """
    lowered = body.lower()
    key_terms = re.findall(r"[a-z]{4,}", description.lower())
    if not key_terms:
        return True  # no vocabulary to check against — don't flag spuriously
    mentions_topic = any(term in lowered for term in key_terms)
    contrast_cues = (
        "but",
        "yet",
        "however",
        "while",
        "although",
        "tension",
        "paradox",
        "both",
    )
    has_contrast = any(re.search(rf"\b{cue}\b", lowered) for cue in contrast_cues)
    if has_contrast and mentions_topic:
        return True
    return all(term in lowered for term in key_terms)


def check_paradox_preservation(
    body: str,
    evidence: EvidenceBundle,
) -> list[ReflectionFinding]:
    """Flag a surfaced paradox the draft flattened to one side.

    The Ontology specialist *names* tensions rather than resolving them; the
    draft must keep both sides visible. For each
    :class:`~creek.author.models.OntologyParadox`, a flattened tension (per
    :func:`_paradox_is_preserved`) raises a ``MID`` finding.

    Args:
        body: The drafted prose under review.
        evidence: The evidence whose ``ontology.paradoxes`` are checked.

    Returns:
        One ``MID`` finding per flattened paradox, dimension
        ``"paradox_preservation"``.
    """
    if evidence.ontology is None:
        return []
    return [
        ReflectionFinding(
            dimension="paradox_preservation",
            severity="MID",
            message=(
                f"Paradox flattened — the draft surfaces only one side "
                f"of: {paradox.description!r}."
            ),
        )
        for paradox in evidence.ontology.paradoxes
        if not _paradox_is_preserved(body, paradox.description)
    ]


def check_attribution_correctness(
    body: str,
    evidence: EvidenceBundle,
) -> list[ReflectionFinding]:
    """Flag a borrowed idea presented as the owner's own.

    A claim carrying an ``author_slug`` (drawn from ``11-Other-Authors/``)
    must be attributed in the body: if neither the slug nor its de-slugged
    full name appears (on a word boundary), the borrowed idea is uncredited.

    Limitation: only the slug and its full de-slugged form are recognised — an
    attribution by partial name alone (e.g. surname-only "Ravikant" for
    ``naval-ravikant``) is not matched and would still flag. Tightening this to
    accept partial-name attribution is a semantic concern for the LLM judge
    (#474); the deterministic check intentionally errs toward demanding the full
    name a borrowed-author folder is keyed on.

    Args:
        body: The drafted prose under review.
        evidence: The evidence whose attributed claims are checked.

    Returns:
        One ``HIGH`` finding per unattributed borrowed claim, dimension
        ``"attribution_correctness"``.
    """
    lowered = body.lower()
    findings: list[ReflectionFinding] = []
    for claim in evidence.claims:
        slug = claim.author_slug
        if slug is None:
            continue
        # Word-boundary match (not substring) so a slug appearing inside a
        # longer word — e.g. "naval" within "navalny" — is not mistaken for an
        # attribution, mirroring check_ontological_accuracy.
        name = slug.replace("-", " ").lower()
        attributed = any(
            re.search(rf"\b{re.escape(token)}\b", lowered)
            for token in (slug.lower(), name)
        )
        if not attributed:
            findings.append(
                ReflectionFinding(
                    dimension="attribution_correctness",
                    severity="HIGH",
                    message=(
                        f"Claim borrowed from {slug!r} is presented without "
                        f"attribution: {claim.claim!r}."
                    ),
                )
            )
    return findings


def check_voice_fidelity(
    body: str,
    fingerprint: VoiceFingerprint | None,
    config: AIStyleConfig | None,
) -> list[ReflectionFinding]:
    """Flag voice divergences via the FEAT-040.x AI-style scanner.

    Reuses :func:`creek.generate.ai_style.scanner.scan`: each
    :class:`~creek.generate.ai_style.model.Finding` becomes a ``MID``
    ``voice_fidelity`` finding, and a ``voice_distance`` over the config's
    ``voice_distance_upper`` bound adds one ``HIGH`` finding. When no
    fingerprint or config is available the voice cannot be measured, so no
    finding is raised.

    Args:
        body: The drafted prose under review.
        fingerprint: The owner's voice fingerprint, or ``None`` to skip.
        config: The AI-style configuration, or ``None`` to skip.

    Returns:
        ``voice_fidelity`` findings mapped from the scan report.
    """
    if fingerprint is None or config is None:
        return []
    report = scan(body, fingerprint=fingerprint, config=config)
    findings = [
        ReflectionFinding(
            dimension="voice_fidelity",
            severity="MID",
            message=finding.message,
        )
        for finding in report.findings
    ]
    if report.voice_distance > config.voice_distance_upper:
        findings.append(
            ReflectionFinding(
                dimension="voice_fidelity",
                severity="HIGH",
                message=(
                    f"Voice distance {report.voice_distance:.2f} exceeds the "
                    f"configured ceiling {config.voice_distance_upper:.2f}."
                ),
            )
        )
    return findings
