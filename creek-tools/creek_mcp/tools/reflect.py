"""``creek.reflect`` MCP tool — anchored Higher-Self margin notes (#751).

Takes one journal entry (raw ``content`` or an ``entry_ref`` fragment id) plus a
privacy-tier ceiling and returns ``{notes: [{quote, kind, note}], essay?}`` —
warm, second-person, anti-guru reflections that mirror the user's own wisdom,
grounded in the *titles* of the ceiling-admitted corpus fragments nearest the
entry (titles only, never body text). That grounding has been live only since
#964: before it, the default grounder raised into a silent ``except`` and every
reflection was ungrounded. Distinct from the essay-shaped
``draft``/``author``/``mine`` surface; this is the reflection surface
Adepthood's journal needs.

Since #873 an ``ok``/``empty`` response may also carry two **optional**,
bounded fields — ``related_praxis`` and ``related_eddies`` — naming the
compiled-layer structures the entry and its grounding belong to. They are
absent whenever nothing qualifies, so a consumer written against the older
shape parses an unchanged response. Their admission rule is *not* the
reflection's: a compiled page carries no ``privacy_tier`` of its own and is
synthesised from fragments the caller may not be entitled to, so
:mod:`creek_mcp.compiled_pages` publishes one only when every contributing
fragment is within the ceiling, and withholds any page whose provenance it
cannot enumerate in full.

Four guarantees this module enforces:

- **The ceiling is enforced on read (#846).** An ``entry_ref`` resolves a vault
  fragment the caller did not supply, so its classified tier is checked against
  the caller's ``privacy_tier_ceiling`` before anything is done with its text: an
  above-ceiling fragment is refused outright rather than reflected. Raw inline
  ``content`` is the caller's own text and carries no classification, so it is
  never gated here.
- **INTIMATE never egresses.** The LLM callable is obtained from a *tier-keyed
  factory* (``llm_factory(tier)``). The production factory resolves through
  :class:`creek.classify.llm.router.ModelRouter`, whose
  ``_enforce_local_for_intimate`` chokepoint redirects an INTIMATE call to the
  local ``default`` model — or raises :class:`IntimateRoutingError` rather than
  egressing. This module never picks a provider or re-checks the tier itself; it
  only derives the routing tier and hands it over. The routing tier is the
  **more sensitive** of the entry's own classified ``privacy_tier`` (when it came
  from a vault fragment via ``entry_ref``) and the ceiling-derived tier — so an
  admitted INTIMATE fragment still routes local under a broader ceiling, which is
  defense in depth behind the read-side gate above. Raw inline ``content`` carries
  no classification, so there the guarantee is bounded by the caller's declared
  ceiling (a caller cannot mark intimate content ``open`` and also have it treated
  as intimate without classifying it first). Both fail closed to INTIMATE on
  anything unrecognised.
- **Quotes are verbatim.** Every returned ``quote`` is validated to be a
  substring of the entry (whitespace-normalised). Model-supplied spans that are
  not are dropped — the client re-anchors verbatim quotes to character offsets
  itself, so a hallucinated span must never reach it.
- **Care boundary (#753).** A ``care_guard`` seam is injected; when it flags the
  entry, the tool escalates (returning the structured ``CARE_SIGNAL`` pointing to
  human support) and never calls the model. The production guard is
  :func:`creek.care.guardrail.acute_distress_guard`, threaded in by
  ``build_server``; the seam stays injectable so tests can substitute their own.

Underneath those guarantees, every read and parse failure answers with a
structured dict rather than raising: a malformed or unreadable file under
``01-Fragments`` is skipped during ``entry_ref`` resolution (#847), and a
malformed model turn degrades to no notes. No parse error crosses the MCP
boundary.

The LLM and retrieval are both injectable seams so the tool is unit-testable
with no live calls. ``build_server`` supplies the production LLM factory and
leaves ``retrieve`` unset, so production grounding is :func:`_default_retrieve`
— the ceiling-bounded title retrieval described above.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Protocol

from creek._containment import escaping_child
from creek.care.guardrail import CARE_POLICY, CARE_SIGNAL
from creek.vault.reader import CORPUS_SUBDIRS, try_validate_fragment
from creek_mcp.audit import MCPAuditLog
from creek_mcp.compiled_pages import RelatedCompiled, related_compiled
from creek_mcp.tier_ceiling import (
    TierCeiling,
    frontmatter_tier,
    refusal_response,
    routing_tier,
    tier_allowed,
    to_privacy_override,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from creek.author.agents import RetrievalSpecialist
    from creek.author.models import EvidenceBundle
    from creek.classify.privacy_filter import PrivacyTierOverride
    from creek.models import Fragment, PrivacyTier

logger = logging.getLogger(__name__)

TOOL_NAME = "creek.reflect"

_DEFAULT_MAX_NOTES = 6
_ALLOWED_KINDS = {"reframe", "fear", "longing", "value", "pattern", "tension", "gift"}

_MAX_LIVE_EMBEDS: Final[int] = 256
"""Cache misses one interactive grounding pass may embed from scratch (#1034).

Bounds the **first** call against a cold vault, which is the unbounded case:
with no ``embeddings.parquet``, ranking previously live-embedded the entire
admitted corpus on every request, and ``POST /v1/reflections`` is published as a
read under a 30 s deadline whose caller is *shed* while the abandoned worker
thread keeps embedding — so a retrying caller stacked detached grounding passes.

Why a cap and not the alternatives, recorded so the choice is not re-litigated
silently. *Refuse-with-reason* would remove working grounding from every vault
that has not run ``creek link --method embeddings`` — the normal state of a new
vault. *Persist-back* bounds call two while leaving call one unbounded, and puts
a vault **write** on a route ``docs/api.md`` publishes as mutating no vault
state, racing ``creek link``. A cap is the only option that is a strict
narrowing, adds no write, bounds the first call, and leaves every vault smaller
than the budget bit-identical.

256 is chosen so it exceeds any vault this bound is not meant to change while
staying far below the corpus sizes that made the pass pathological; the batch
producer (``creek link``) remains the way to ground a large vault fully.
"""


class _CorpusDocument(NamedTuple):
    """One readable Markdown document from a reflection corpus subtree."""

    metadata: dict[str, Any]
    body: str


class _CorpusFragment(NamedTuple):
    """One model-valid fragment plus the subtree it was loaded from."""

    subdir: str
    fragment: Fragment
    body: str
    metadata: dict[str, Any]


class _ReflectionCorpus(NamedTuple):
    """One request's tier-neutral, single-parse view of the corpus.

    ``entries`` includes readable non-Fragment documents because ``entry_ref``
    has always resolved from raw front matter. ``fragments`` is the narrower,
    model-valid projection used by retrieval and compiled-page provenance.
    Nothing is filtered by a caller's ceiling here; each consumer applies its
    own admission rule to this same raw snapshot.
    """

    entries: tuple[_CorpusDocument, ...]
    fragments: tuple[_CorpusFragment, ...]

    def retrieval_records(self) -> list[tuple[Fragment, str]]:
        """Return every model-valid corpus record, without tier filtering."""
        return [(record.fragment, record.body) for record in self.fragments]

    def compiled_records(self) -> list[tuple[Fragment, dict[str, Any]]]:
        """Return the unfiltered ``01-Fragments`` provenance projection."""
        return [
            (record.fragment, record.metadata)
            for record in self.fragments
            if record.subdir == CORPUS_SUBDIRS[0]
        ]


def _load_reflection_corpus(vault_path: Path) -> _ReflectionCorpus:
    """Parse each corpus Markdown file at most once for one reflection.

    The snapshot is deliberately request-scoped: caching it on the server
    would make edits invisible and, if filtered first, could carry one caller's
    tier override into another.  The full raw view is safe to share only among
    consumers in this request; admission remains independently re-decided.
    """
    import frontmatter

    entries: list[_CorpusDocument] = []
    fragments: list[_CorpusFragment] = []
    for subdir in CORPUS_SUBDIRS:
        root = vault_path / subdir
        if not root.exists():
            continue
        resolved_root = root.resolve(strict=False)
        for path in sorted(root.rglob("*.md")):
            if escaping_child(path, resolved_root):
                logger.warning(
                    "Skipping a fragment whose symlink leaves %s: %s", root, path
                )
                continue
            try:
                post = frontmatter.load(path)
            except Exception as exc:
                logger.debug(
                    "Skipping unreadable fragment %s (%s)", path, type(exc).__name__
                )
                continue
            metadata = post.metadata.copy()
            document = _CorpusDocument(metadata, post.content)
            if subdir == CORPUS_SUBDIRS[0]:
                entries.append(document)
            fragment = try_validate_fragment(metadata, path)
            if fragment is None:
                continue
            fragments.append(_CorpusFragment(subdir, fragment, post.content, metadata))
    return _ReflectionCorpus(tuple(entries), tuple(fragments))


class GroundingSession:
    """One owner's reused :class:`RetrievalSpecialist`, held for a server or app.

    The #1034 defect is one of **lifetime**, not of memoisation:
    :class:`~creek.author.agents.RetrievalSpecialist` already memoises its model
    handle and its parquet map per instance (pinned by
    ``test_retrieval_reuses_linker_across_gather_calls`` and
    ``test_retrieval_loads_cache_once_across_gather_calls`` in
    ``tests/test_real_agents.py``), but :func:`_default_retrieve` built a fresh
    one *inline in the call expression* and dropped it, so every
    ``creek.reflect`` paid a sentence-transformer load from disk plus a full
    parquet read before any corpus work. This class supplies the missing
    lifetime and owns the bounded, in-memory vectors produced during it.

    **Owner-scoped, never a module global.** One instance per ``build_server``
    and one per ``create_app``, passed down explicitly. A process global or an
    ``lru_cache`` would carry one test's autouse model mock into the next —
    the hazard ``RetrievalSpecialist``'s own docstring names.

    **Exactly one specialist at a time.** A different vault *replaces* the slot
    rather than accumulating one entry per path. That matters on ``/v1``, where
    production passes no ``vault_path`` and ``configured_vault`` re-reads
    ``creek_config.yaml`` per request: a per-vault dict on a multi-vault or
    reconfigured host would grow one loaded model plus one full id→vector map
    per distinct path, for the life of the process — unbounded per-request
    growth introduced by a change whose purpose is bounding cost.

    **All shared mutation happens under one lock.** ``/v1`` serves reads in
    worker threads with several concurrency slots, so two first requests really
    can race. Building, warming, gathering, and filling the live-vector cache
    under the lock makes each cold fragment embed a single spend rather than a
    duplicated miss in both workers.

    **No override-derived decision is reused.** The session keys on the vault
    only. Tier admission is re-decided per call from that caller's own override;
    what is shared is the tier-blind model handle, parquet map, and at most
    :data:`_MAX_LIVE_EMBEDS` content-hash-keyed live vectors. The live map is
    reconciled from a deterministic prefix of the unfiltered corpus before the
    override is applied, so neither call order nor a prior ceiling selects its
    membership. Ranking consults vectors only for ids the current call then
    admits; stale vectors are replaced, and all are dropped on a vault switch.
    """

    def __init__(self) -> None:
        """Create an empty session; the first :meth:`specialist` call fills it."""
        self._lock = threading.RLock()
        self._specialist: RetrievalSpecialist | None = None
        self._vault: Path | None = None
        self._live_vectors: dict[str, tuple[str, list[float]]] = {}

    def specialist(self, vault: Path) -> RetrievalSpecialist:
        """Return this session's warmed specialist for *vault*, building once.

        The import is deferred to here, not module scope, so importing this tool
        does not drag in the author agents and their embedding stack — the same
        reason :func:`_default_retrieve` imports lazily, which keeps the server
        bootable when those heavier deps are missing.

        Failure stores nothing: if the model or the parquet cannot be loaded the
        exception propagates to :func:`_default_retrieve`'s swallow, the slot is
        left as it was, and the next call retries rather than caching a broken
        session for the life of the process.

        :data:`_MAX_LIVE_EMBEDS` is read **here, at construction time**, not
        captured as a parameter default when this class was defined. That is
        deliberate and load-bearing for testability: a default argument binds
        once at import, so the published cap could not be reached from a test
        without passing it explicitly — which would have tested a value the
        production path never uses. Reading the module constant at call time
        means a test can monkeypatch it and exercise the real production path,
        and it is why this method takes no budget argument at all.

        Args:
            vault: The vault this grounding pass will read.

        Returns:
            The warmed specialist, freshly built when this session held none or
            held one for a different vault.
        """
        from creek.author.agents import RetrievalSpecialist

        with self._lock:
            if self._specialist is not None and self._vault == vault:
                return self._specialist
            built = RetrievalSpecialist(max_live_embeds=_MAX_LIVE_EMBEDS)
            built.warm(vault)
            self._specialist = built
            self._vault = vault
            self._live_vectors.clear()
            return built

    def gather(
        self,
        query: str,
        vault: Path,
        override: PrivacyTierOverride,
        *,
        corpus: Sequence[tuple[Fragment, str]] | None = None,
    ) -> EvidenceBundle:
        """Gather with one bounded live-vector cache under the session lock.

        Serialising the whole gather is intentional. ``/v1`` may run two reads
        concurrently; without this lock both cold calls can miss the same
        in-memory row and duplicate every live embed before either stores it.
        The cache holds at most :data:`_MAX_LIVE_EMBEDS` tier-blind vectors,
        reconciled from the unfiltered *corpus* before this call independently
        applies its own tier override for ranking. It clears on a vault switch.

        Args:
            query: Entry text whose neighbours are requested.
            vault: Vault root owning the corpus and cache.
            override: This call's privacy admission override.
            corpus: Optional request snapshot, already parsed but unfiltered.

        Returns:
            The specialist's provenance-carrying evidence bundle.
        """
        with self._lock:
            specialist = self.specialist(vault)
            return specialist.gather(
                query,
                vault,
                override=override,
                corpus=corpus,
                live_cache=self._live_vectors,
                max_live_cache_entries=_MAX_LIVE_EMBEDS,
            )


class _Grounding(NamedTuple):
    """One grounding pass: the prompt lines, and the fragments they came from.

    Attributes:
        lines: The grounding snippets fed to :func:`_build_prompt` — corpus
            fragment *titles* under the default grounder, never body text.
        source_ids: The ids of the fragments the compiled-layer lookup may
            *select* from. :func:`_default_retrieve` populates this with the
            fragments its own lines were drawn from, in retrieval order;
            :func:`_gather_grounding` returns the same list led by the
            reflected entry's own ``entry_ref`` when it resolved a fragment.
            Carried so :mod:`creek_mcp.compiled_pages` can
            *select* candidate eddy and praxis pages from the same pass rather
            than running a second embedding sweep (issue #873's constraint).
            They select only: every candidate page's provenance is re-checked
            against the ceiling there, so these ids confer no authority.
    """

    lines: list[str]
    source_ids: list[str]


class _LLM(Protocol):
    """A prompt-completion callable returning the model's raw text."""

    def __call__(self, prompt: str) -> str:
        """Return the completion for *prompt*."""


class _LLMFactory(Protocol):
    """Builds a tier-routed LLM callable; INTIMATE is forced local by the router."""

    def __call__(self, tier: PrivacyTier) -> _LLM:
        """Return an LLM callable routed for *tier* (may raise to refuse)."""


def _routing_tier(ceiling: TierCeiling, entry_tier: PrivacyTier | None) -> PrivacyTier:
    """Pick the routing tier — never below the entry's *actual* classification.

    The router's cloud gate keys on :class:`PrivacyTier`, never on
    :class:`TierCeiling`. Two signals are reconciled by taking the **more
    sensitive**:

    - *entry_tier* — the entry's own classified ``privacy_tier`` when it came
      from a vault fragment (``None`` for raw inline ``content``, which carries
      no classification). This is the load-bearing signal: an INTIMATE fragment
      must route local even if the caller declared a lower ceiling.
    - the *ceiling*-derived tier (its most-sensitive admitted tier), which is the
      only signal available for raw ``content``.

    Failing closed: an unrecognised ceiling, or an *entry_tier* outside the known
    ranks, routes as INTIMATE (local-only).

    An above-ceiling *entry_tier* is now refused upstream by
    :func:`_above_ceiling` before this function ever runs (#846), so in practice
    this only reconciles an *admitted* ``entry_ref`` (or raw ``content``, which
    has no *entry_tier*) against the ceiling. It stays in place as defense in
    depth — the load-bearing INTIMATE-never-egresses guarantee — and must not be
    removed.

    The reconciliation itself lives in
    :func:`creek_mcp.tier_ceiling.routing_tier`, shared with ``creek.compile``
    (#928); this wrapper keeps the reflect-specific reasoning above attached to
    the call site that depends on it.
    """
    return routing_tier(ceiling, entry_tier)


def _above_ceiling(entry_tier: PrivacyTier | None, ceiling: TierCeiling) -> bool:
    """Return whether a resolved entry's classified tier exceeds *ceiling*.

    Delegates the rank comparison to :func:`creek_mcp.tier_ceiling.tier_allowed`,
    the canonical read-side admission predicate, so this tool cannot drift from
    the rest of the MCP surface.

    Args:
        entry_tier: The entry's classified ``privacy_tier``, or ``None`` for raw
            inline ``content`` — which carries no classification and is the
            caller's own text, so it is never above the ceiling.
        ceiling: The caller's declared ceiling.

    Returns:
        ``True`` when the entry must be refused rather than reflected.
    """
    return entry_tier is not None and not tier_allowed(entry_tier, ceiling)


def _normalise(text: str) -> str:
    """Collapse runs of whitespace so quote matching tolerates LLM reflow."""
    return " ".join(text.split())


def _is_verbatim(quote: str, entry: str) -> bool:
    """Return whether *quote* appears in *entry* (whitespace-normalised)."""
    needle = _normalise(quote)
    return bool(needle) and needle in _normalise(entry)


def _parse_notes(response_text: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the model response into raw notes + an optional essay.

    Strips a single code fence, then parses JSON (falling back to a YAML
    safe-load for fenced YAML), and reads ``notes`` / ``essay``. Any structural
    problem degrades to ``([], None)`` rather than raising — a malformed model
    turn yields no notes, never a crash or unvalidated output.
    """
    import json

    import yaml

    text = response_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
        text = text.removesuffix("```")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        try:
            data = yaml.safe_load(text)
        except (ValueError, TypeError, yaml.YAMLError):
            # ``yaml.YAMLError`` (ScannerError/ParserError) is NOT a ValueError,
            # so it must be caught explicitly or malformed YAML would crash the
            # tool across the MCP boundary, breaking the never-raises contract.
            return [], None
    if not isinstance(data, dict):
        return [], None
    raw_notes = data.get("notes")
    notes = (
        [n for n in raw_notes if isinstance(n, dict)]
        if isinstance(raw_notes, list)
        else []
    )
    essay = data.get("essay")
    return notes, essay if isinstance(essay, str) and essay.strip() else None


def _clean_notes(
    raw_notes: list[dict[str, Any]], entry: str, *, max_notes: int
) -> list[dict[str, str]]:
    """Keep only well-formed notes whose quote is a verbatim entry substring.

    A surviving note carries exactly ``quote`` / ``kind`` / ``note``; the kind is
    constrained to the known vocabulary (unknown kinds fall back to ``pattern``).
    """
    cleaned: list[dict[str, str]] = []
    for note in raw_notes:
        quote, kind, body = note.get("quote"), note.get("kind"), note.get("note")
        if not isinstance(quote, str) or not isinstance(body, str) or not body.strip():
            continue
        if not _is_verbatim(quote, entry):
            continue
        safe_kind = (
            kind if isinstance(kind, str) and kind in _ALLOWED_KINDS else "pattern"
        )
        cleaned.append({"quote": quote, "kind": safe_kind, "note": body})
        if len(cleaned) >= max_notes:
            break
    return cleaned


def _build_prompt(entry: str, grounding: list[str]) -> str:
    """Compose the margin-note prompt from the entry and grounding snippets."""
    sources = "\n\n".join(f"- {snippet}" for snippet in grounding) or "(none)"
    kinds = ", ".join(sorted(_ALLOWED_KINDS))
    schema = (
        'Return JSON: {"notes": [{"quote": <verbatim span copied from the ENTRY>, '
        '"kind": <one of: ' + kinds + '>, "note": <a few warm sentences>}]}. '
        "Every quote MUST be copied verbatim from the ENTRY."
    )
    return (
        CARE_POLICY
        + "\n\n"
        + "You are the writer's own Higher Self leaving warm, second-person margin "
        "notes on their journal entry. Mirror their wisdom back; never advise from "
        "above, never diagnose, never console with platitudes. Ground every note in "
        "their own words and the source fragments below.\n\n"
        + schema
        + f"\n\nSOURCE FRAGMENTS:\n{sources}\n\nENTRY:\n{entry}"
    )


def _fragment_tier(metadata: dict[str, Any]) -> PrivacyTier:
    """Return a fragment's classified ``privacy_tier``, failing closed to INTIMATE.

    Mirrors :func:`creek.classify.privacy_filter.tier_of`: a recognised value
    (including ``unclassified``) is honoured; anything unrecognised — or a
    missing field — is treated as the most-restrictive tier so an unknown
    classification is never routed to a cloud provider.

    The rule itself lives in :func:`creek_mcp.tier_ceiling.frontmatter_tier`,
    shared with :mod:`creek_mcp.compiled_pages` (#873), which has to rank the
    *contributors* to an eddy or praxis page by the identical reading — a
    second copy that fails closed differently would admit a compiled page built
    from a fragment this tool would refuse to reflect on.
    """
    return frontmatter_tier(metadata)


def _resolve_entry(
    content: str | None,
    entry_ref: str | None,
    vault_path: Path,
    corpus: _ReflectionCorpus | None = None,
) -> tuple[str, PrivacyTier | None]:
    """Return the entry text plus its *classified* tier (``None`` for raw content).

    Raw *content* carries no classification, so its tier is ``None`` and the
    caller falls back to the ceiling. An *entry_ref* is resolved to a fragment
    markdown file under ``01-Fragments``; its body becomes the entry **and its
    persisted ``privacy_tier`` becomes both the admission tier and the routing
    tier** — the caller checks the admission tier against the ceiling via
    :func:`_above_ceiling` (#846) before doing anything else with the text, and
    separately folds it into :func:`_routing_tier` so an admitted INTIMATE
    fragment still routes local. A fragment with the ``privacy_tier`` key
    missing entirely fails closed to INTIMATE (:func:`_fragment_tier`) — but
    that only fires for hand-edited or legacy files, and is refused at
    every ceiling below ``intimate``. A normally pipeline-written,
    not-yet-classified fragment carries an *explicit*
    ``privacy_tier: unclassified`` (the ``Fragment`` model default,
    serialised by every write), which ranks with ``personal`` (#876,
    extended to the MCP ceiling by #961) and is refused by this same
    #846 gate at ``ceiling=open`` — it needs at least ``personal`` to be
    admitted. The distinction between the two cases still matters and is
    now visible at ``ceiling=personal``: there, an explicit
    ``unclassified`` entry is admitted while one with a missing key is
    still refused (routing INTIMATE only, per :func:`_fragment_tier`).
    That ranking is owned by ``creek_mcp.tier_ceiling._TIER_RANK`` and
    matches ``creek.classify.privacy_filter._TIER_RANK``, not this
    fail-closed path. A blank result yields ``("", None)``, which the
    caller turns into a refusal.

    A fragment file that cannot be parsed or even read is skipped, debug-logged,
    and the scan continues (#847) — one hand-edited or half-written file must not
    take down ``entry_ref`` resolution for the whole vault. The skip is
    fail-closed: such a fragment can never be *returned* as an entry either, so
    it is unreadable-by-anyone rather than readable-without-a-tier. Its front
    matter never became usable metadata — the file may not even have been read —
    so its ``privacy_tier`` is unknown and unknowable, and salvaging the id or
    the body out of the raw text would hand an ``open``-ceiling caller content
    nobody can vouch for — walking around the #846 gate rather than through it.
    The skip is also deliberately indistinguishable from not-found (both land on
    the caller's ``"entry_ref not found"`` refusal): a distinct "unreadable"
    reason would confirm to an unadmitted caller that a fragment with that id
    exists, a new existence oracle over the corpus in the one place where the
    tier is unknown.
    """
    if content and content.strip():
        return content, None
    if entry_ref:
        snapshot = corpus if corpus is not None else _load_reflection_corpus(vault_path)
        for document in snapshot.entries:
            if str(document.metadata.get("id", "")) == entry_ref:
                return document.body, _fragment_tier(document.metadata)
    return "", None


def _admit_entry(
    *,
    content: str | None,
    entry_ref: str | None,
    vault_path: Path,
    ceiling: TierCeiling,
    care_guard: Callable[[str], str | None] | None,
    corpus: _ReflectionCorpus | None = None,
) -> tuple[str, PrivacyTier | None] | dict[str, Any]:
    """Resolve the entry and run the three admission gates, in source order.

    The order is load-bearing, and keeping the three gates in one function
    is what keeps it reviewable in one screen: resolve-and-empty-check,
    then the read-side ceiling gate (#846), then the care seam (#753). The
    ceiling gate sits *above* the care seam on purpose -- see the comment
    on it below.

    The audit append deliberately stays in :func:`reflect_tool`, above the
    call to this function, so a refused above-ceiling attempt is still
    recorded and there is still exactly one append per call.

    Args:
        content: The raw entry text, or ``None`` to resolve *entry_ref*.
        entry_ref: A fragment id whose body is the entry, when *content*
            is absent.
        vault_path: Vault root, for fragment resolution.
        ceiling: The caller's declared ceiling.
        care_guard: ``(entry) -> reason | None``; the #753 seam, or
            ``None`` when no guard is wired.

    Returns:
        Either a ``dict``, which **is** the terminal response for the whole
        call and must be returned to the caller verbatim with nothing added
        to it, or the ``(entry, entry_tier)`` pair of an admitted entry.
    """
    entry, entry_tier = _resolve_entry(content, entry_ref, vault_path, corpus)
    if not entry.strip():
        reason = "entry_ref not found" if entry_ref else "no entry content supplied"
        return refusal_response(tool=TOOL_NAME, ceiling=ceiling, reason=reason)

    # The read-side ceiling gate (#846). It sits *above* the care seam on
    # purpose: an ``escalate`` response is a one-bit oracle telling a caller who
    # is not admitted to this fragment that it carries acute-distress markers.
    # Care still runs for every raw-``content`` call and every within-ceiling
    # ``entry_ref`` — only unadmitted reads skip it, and they skip the model too.
    if _above_ceiling(entry_tier, ceiling):
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=ceiling,
            # Unlike ``save``'s refusal, this reason does NOT echo the offending
            # tier. There the tier came from the caller's own input, so echoing
            # it tells them nothing new; here it is derived from content the
            # caller is not admitted to, so echoing it would turn the refusal
            # into a tier-classification oracle over the corpus.
            #
            # ACCEPTED RESIDUAL RISK: keeping this reason distinct from
            # "entry_ref not found" is itself a coarse existence-and-rank oracle
            # across repeated probes (refused at ``open`` implies tier >=
            # personal; refused at ``personal`` implies the entry *ranks*
            # intimate -- which includes one whose ``privacy_tier`` is missing
            # or unrecognised and fails closed, not only classified content).
            #
            # *Id unguessability is not a control here.* Both generators are
            # deterministic: :func:`creek.ingest.base.generate_fragment_id`
            # hashes source+timestamp+content, and
            # :func:`creek.ingest.base.generate_child_fragment_id` hashes only
            # ``f"{parent_id}:{level}:{index}"`` over an eight-value
            # :data:`creek.models.FragmentLevel` (five minted today; #1342
            # retired the other three). So from a single admitted
            # parent id a caller enumerates every child id knowing nothing
            # about the content. The id space around known content IS
            # sweepable; only a blind sweep of the full 48-bit space is
            # infeasible.
            #
            # *Probe cost is not a control either*, and here the property is
            # the converse of ``creek.compile``'s: its survey walks the
            # non-short-circuiting :func:`creek.classify.privacy_filter.
            # ancestry_tiers` (the walk moved out of that module in #958),
            # whereas :func:`_resolve_entry` above walks a lazy rglob and
            # returns at the match. So an above-ceiling probe is never *more*
            # expensive than a miss, and is cheaper for every fragment but the
            # last one the walk happens to yield.
            #
            # What is left is deliberately weak, and it is the whole of the
            # acceptance. The oracle yields existence and a rank bound, never
            # content. :func:`reflect_tool` appends exactly one audit row per
            # call, unconditionally and outcome-free, so a sweep surfaces only
            # as an elevated rate of ``has_entry_ref`` calls by a named
            # consumer -- it is NOT distinguishable from legitimate
            # ``entry_ref`` traffic, and there is no per-probe outcome to
            # correlate; see the comment on that append. And the distinct
            # not-found reason is what makes a legitimate client's bug
            # debuggable. This residual is confined to the MCP surface: the
            # HTTP application API collapses both reasons into one
            # ``privacy_refused`` and does not carry it
            # (:func:`creek_mcp.httpapi.reflect.reflect_refusal_code`).
            # If the two reasons are ever unified, the scan must be
            # equalised too: the not-found path attempts to parse every
            # fragment file whereas this path early-returns at the match, so
            # the timing difference would preserve the oracle as a side
            # channel.
            reason="entry_ref tier exceeds ceiling",
        )

    if care_guard is not None:
        care_reason = care_guard(entry)
        if care_reason:
            return {
                "status": "escalate",
                "tool": TOOL_NAME,
                "tier_ceiling": ceiling.value,
                "reason": care_reason,
                "care_signal": CARE_SIGNAL,
            }

    return entry, entry_tier


def _gather_grounding(
    *,
    entry: str,
    entry_ref: str | None,
    entry_tier: PrivacyTier | None,
    vault_path: Path,
    ceiling: TierCeiling,
    retrieve: Callable[[str, Path, PrivacyTierOverride], list[str]] | None,
    session: GroundingSession | None = None,
    corpus: _ReflectionCorpus | None = None,
) -> _Grounding:
    """Run one grounding pass and compose the compiled-layer seed list.

    Args:
        entry: The admitted entry text, used as the retrieval query.
        entry_ref: The reflected fragment's id, or ``None`` for raw
            ``content``. Leads the seed list when it resolved a fragment.
        entry_tier: That fragment's classified tier, or ``None`` for raw
            ``content`` -- which contributes no seed because it is not a
            corpus fragment.
        vault_path: Vault root.
        ceiling: The caller's declared ceiling, converted to the grounder's
            privacy override.
        retrieve: The injected grounder, or ``None`` for
            :func:`_default_retrieve`. An injected callable *replaces* the
            default outright and returns no fragment ids, so it contributes
            no seeds.
        session: The owner's :class:`GroundingSession`, supplying the *lifetime*
            of the default grounder's retrieval specialist — not the grounder
            itself. Ignored when *retrieve* is injected, because an injected
            grounder replaces the default outright and owns its own retrieval.

    Returns:
        The grounding lines for the prompt, and the seed ids that select
        candidate compiled pages.
    """
    override = to_privacy_override(ceiling)
    if retrieve is not None:
        grounding, retrieved_ids = _Grounding(retrieve(entry, vault_path, override), [])
    else:
        grounding, retrieved_ids = _default_retrieve(
            entry, vault_path, override, session=session, corpus=corpus
        )
    # Seeds *select* candidate compiled pages; they never authorize one. An
    # ``entry_ref`` seed is admitted by the #846 gate above, and a retrieval
    # seed by the grounder's hard tier cutoff — but neither fact is relied on
    # here, because ``related_compiled`` re-checks the tier of every fragment
    # each candidate page was compiled from.
    from_entry = [entry_ref] if entry_ref and entry_tier is not None else []
    return _Grounding(grounding, from_entry + retrieved_ids)


def _reflection_response(
    *,
    notes: list[dict[str, str]],
    essay: str | None,
    related: RelatedCompiled,
    ceiling: TierCeiling,
    tier: PrivacyTier,
) -> dict[str, Any]:
    """Render the answered-reflection response, omitting what does not qualify.

    Only the ``ok`` / ``empty`` paths render through here. A ``refused`` or
    ``escalate`` response is built by :func:`_admit_entry` (or by the
    provider-unavailable handler) and deliberately carries a *smaller* key
    set -- no ``routed_tier``, ``notes`` or ``essay_grounded`` -- because it
    never reached the model. Routing an escalation through this function
    would invent all three.

    Args:
        notes: The cleaned, verbatim-verified notes; empty means ``empty``.
        essay: The model's free prose, or ``None`` when it offered none.
        related: The admitted compiled structures; empty on both axes when
            nothing qualifies.
        ceiling: The caller's declared ceiling, echoed back.
        tier: The tier the model call was actually routed at.

    Returns:
        The ``ok`` / ``empty`` response dict.
    """
    result: dict[str, Any] = {
        "status": "ok" if notes else "empty",
        "tool": TOOL_NAME,
        "tier_ceiling": ceiling.value,
        "routed_tier": tier.value,
        "notes": notes,
        # ``essay`` is free model prose and is NOT verbatim/grounding-checked the
        # way ``notes[].quote`` is — a client must not treat it as grounded.
        "essay_grounded": False,
    }
    if essay is not None:
        result["essay"] = essay
    # Both keys are OMITTED when nothing qualifies, never present-and-empty: a
    # consumer written before #873 must parse a reflection with no compiled
    # neighbours byte-for-byte as it did before.
    if related.praxis:
        result["related_praxis"] = related.praxis
    if related.eddies:
        result["related_eddies"] = related.eddies
    return result


def _corpus_for_entry(
    content: str | None,
    entry_ref: str | None,
    vault_path: Path,
) -> _ReflectionCorpus | None:
    """Load the snapshot only when resolving an entry reference requires it."""
    has_inline_content = bool(content and content.strip())
    if entry_ref is None or has_inline_content:
        return None
    return _load_reflection_corpus(vault_path)


def _corpus_for_grounding(
    corpus: _ReflectionCorpus | None,
    retrieve: Callable[[str, Path, PrivacyTierOverride], list[str]] | None,
    vault_path: Path,
) -> _ReflectionCorpus | None:
    """Ensure the production grounder has this request's corpus snapshot."""
    if retrieve is not None or corpus is not None:
        return corpus
    return _load_reflection_corpus(vault_path)


def reflect_tool(
    *,
    vault_path: Path,
    llm_factory: _LLMFactory,
    content: str | None = None,
    entry_ref: str | None = None,
    retrieve: Callable[[str, Path, PrivacyTierOverride], list[str]] | None = None,
    related_lookup: (
        Callable[[Sequence[str], Path, TierCeiling], RelatedCompiled] | None
    ) = None,
    care_guard: Callable[[str], str | None] | None = None,
    session: GroundingSession | None = None,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    max_notes: int = _DEFAULT_MAX_NOTES,
) -> dict[str, Any]:
    """Return anchored Higher-Self margin notes for a single journal entry.

    Args:
        vault_path: Vault root (for retrieval grounding + the audit log).
        llm_factory: Tier-keyed LLM builder; ``llm_factory(tier)`` returns the
            completion callable. The production factory routes INTIMATE local.
        content: The raw entry text. Mutually exclusive-ish with *entry_ref*;
            *content* wins when both are given.
        entry_ref: A fragment id whose body is the entry, when *content* is
            absent.
        retrieve: ``(query, vault, override) -> grounding lines`` source. The
            default (:func:`_default_retrieve`) returns ceiling-bounded corpus
            fragment **titles**, never body text. An injected callable
            *replaces* the default outright — the two are never merged — so a
            deployment wanting richer grounding owns its own admission
            filtering. Its ``list[str]`` return carries no fragment ids, so an
            injected grounder contributes **no** seeds to the compiled-layer
            lookup below; only the reflected entry's own ``entry_ref`` does.
        related_lookup: ``(seed_ids, vault, ceiling) -> RelatedCompiled``
            source for the optional ``related_praxis`` / ``related_eddies``
            fields (#873). Defaults to
            :func:`creek_mcp.compiled_pages.related_compiled`, which admits a
            compiled page only when **every** fragment it was compiled from is
            within *privacy_tier_ceiling*. Injectable so a test can drive the
            projection without a corpus; the admission rule itself is not a
            policy this tool implements.
        care_guard: ``(entry) -> reason | None``; a non-``None`` reason escalates
            to a human and skips the model entirely (#753 seam).
        session: The owner-scoped :class:`GroundingSession` whose warmed
            retrieval specialist the default grounder reuses (#1034). A
            **sibling** of *retrieve*, not a replacement for it: *retrieve*
            supplies a different grounder, *session* supplies the lifetime of
            the default one, and the two do not interact. ``None`` — the
            default, and what every ``retrieve=``-injecting test and both
            read-gate probes pass — builds a cold specialist per call, exactly
            as before.
        privacy_tier_ceiling: The ceiling; gates admission of the *entry itself*
            when it came from an ``entry_ref`` (see :func:`_above_ceiling`),
            corpus admission for grounding retrieval, and — via
            :func:`_routing_tier` — the local-vs-cloud routing tier.
        consumer: Free-form consumer id for the audit log.
        max_notes: Cap on returned notes.

    Returns:
        ``{status, tool, tier_ceiling, ...}`` — ``ok`` with ``notes`` (+ optional
        ``essay``), ``escalate`` with a ``reason``, or a structured ``refused``
        response for one of: no *entry_ref*/*content* resolves to text
        (``"entry_ref not found"`` — which also covers a fragment whose front
        matter is unparseable or unreadable, see :func:`_resolve_entry` — or
        ``"no entry content supplied"``); the
        *entry_ref*'s classified tier exceeds *privacy_tier_ceiling*
        (``"entry_ref tier exceeds ceiling"``, #846 — see :func:`_above_ceiling`);
        or the LLM provider is unavailable/raises (``"reflection unavailable:
        ..."``). Each ``notes[].quote`` is a verified verbatim span of the entry;
        the optional ``essay`` is free model prose and is **not**
        grounding-checked, flagged by ``essay_grounded: False`` so a client never
        treats it as grounded.

        An ``ok`` / ``empty`` result may additionally carry ``related_praxis``
        (≤3 ``{title, praxis_type, status, excerpt}``) and ``related_eddies``
        (≤2 ``{title, description, fragment_count, formed}``) — the compiled
        structures the reflected entry and its grounding belong to (#873). Both
        keys are **absent** when nothing qualifies, so the response a
        pre-#873 consumer parses is unchanged whenever the vault has no
        admitted compiled neighbours. Neither is a new read privilege: a
        compiled page is published only when every fragment it was compiled
        from is itself within *privacy_tier_ceiling*, and a page whose
        provenance cannot be enumerated in full is withheld
        (:mod:`creek_mcp.compiled_pages`).
    """
    # Logged unconditionally, above the ceiling gate in :func:`_admit_entry`, so
    # a refused above-ceiling attempt is still recorded (tool, ceiling, consumer,
    # has_entry_ref, timestamp) — this is the only append for this call; no
    # second append happens on refusal. The probed entry_ref itself and the
    # call's outcome are deliberately NOT logged, so this record cannot
    # answer "did consumer X read fragment F?" in either direction: a
    # probing consumer shows up as an elevated rate of has_entry_ref calls,
    # never as a named target.
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"has_entry_ref": entry_ref is not None},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )

    corpus = _corpus_for_entry(content, entry_ref, vault_path)
    admitted = _admit_entry(
        content=content,
        entry_ref=entry_ref,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
        care_guard=care_guard,
        corpus=corpus,
    )
    if isinstance(admitted, dict):
        return admitted
    entry, entry_tier = admitted

    corpus = _corpus_for_grounding(corpus, retrieve, vault_path)
    tier = _routing_tier(privacy_tier_ceiling, entry_tier)
    grounding = _gather_grounding(
        entry=entry,
        entry_ref=entry_ref,
        entry_tier=entry_tier,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
        retrieve=retrieve,
        session=session,
        corpus=corpus,
    )

    try:
        llm = llm_factory(tier)
        response_text = llm(_build_prompt(entry, grounding.lines))
    except RuntimeError as exc:
        # Covers a missing/unavailable provider AND ``IntimateRoutingError``
        # (a RuntimeError subclass) — the router raises the latter rather than
        # egressing intimate content when no local backend exists.
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"reflection unavailable: {type(exc).__name__}",
        )

    raw_notes, essay = _parse_notes(response_text)
    notes = _clean_notes(raw_notes, entry, max_notes=max_notes)
    related = _related(
        grounding.source_ids,
        vault_path,
        privacy_tier_ceiling,
        related_lookup,
        corpus,
    )
    return _reflection_response(
        notes=notes,
        essay=essay,
        related=related,
        ceiling=privacy_tier_ceiling,
        tier=tier,
    )


def _related(
    seeds: Sequence[str],
    vault_path: Path,
    ceiling: TierCeiling,
    lookup: Callable[[Sequence[str], Path, TierCeiling], RelatedCompiled] | None,
    corpus: _ReflectionCorpus | None = None,
) -> RelatedCompiled:
    """Look up the compiled layer, degrading to nothing on any failure.

    Runs **after** the model call on purpose, so a refused or unavailable
    provider costs no corpus walk at all, and so a failure here can only ever
    subtract the optional fields — never veto a reflection that was already
    produced. That inversion is the point: this is an enrichment step, and an
    enrichment step that can take down the answer it decorates is worse than
    one that is silently absent.

    The catch is broad for the same reason :func:`_default_retrieve`'s is: the
    lookup walks user-managed vault markdown through several tolerant readers,
    so its residual failure surface is open-ended, and none of it may cross the
    MCP boundary. Only the exception's *type name* is logged — never
    ``str(exc)`` — because a YAML error stringifies with the offending source
    snippet, which would write tier-unknown vault content into an untiered log.

    Args:
        seeds: Fragment ids that select candidate pages; may be empty.
        vault_path: Vault root.
        ceiling: The caller's declared ceiling, enforced inside the lookup.
        lookup: The injected seam, or ``None`` for the production default.

    Returns:
        The bounded, admitted compiled structures, or empty on both axes.
    """
    try:
        if lookup is not None:
            return lookup(seeds, vault_path, ceiling)
        records = corpus.compiled_records() if corpus is not None else None
        return related_compiled(seeds, vault_path, ceiling, corpus_records=records)
    except Exception as exc:
        logger.debug("Related compiled layer degraded to none (%s)", type(exc).__name__)
        return RelatedCompiled([], [])


def _default_retrieve(
    query: str,
    vault_path: Path,
    override: PrivacyTierOverride,
    session: GroundingSession | None = None,
    corpus: _ReflectionCorpus | None = None,
) -> _Grounding:
    """Production grounding: the *titles* of the corpus fragments nearest *query*.

    Returns the titles of the top ``author.retrieval_top_k`` (default 5) corpus
    fragments semantically nearest *query*, drawn from ``01-Fragments``,
    ``09-Reference`` and ``11-Other-Authors``. The author retrieval specialist
    is imported lazily so the server still boots when its (heavier) deps are
    unavailable.

    **Titles, not bodies — and that is the decision, not an accident.** Three
    reasons, recorded because "grounding" reads like it ought to mean body text:

    - :func:`_build_prompt` validates every returned ``notes[].quote`` as a
      verbatim span of the *entry*, never of a source, so grounding supplies
      thematic context and is structurally unquotable — titles serve that role.
    - Bodies would mean changing ``creek.author.agents._fragment_claim``, whose
      "one assertion, one short sentence" contract is shared by
      ``GraphSpecialist`` and the Writing Desk's citation and leak-gate
      machinery.
    - :func:`reflect_tool`'s ``retrieve=`` is an injectable seam, so the
      *default* is deliberately the conservative one; a deployment wanting
      richer grounding supplies its own callable.

    **The ceiling invariant.** ``creek.author.agents._load_corpus`` applies
    ``tier_within_override`` — a **hard rank cutoff** — so an above-ceiling
    fragment contributes *nothing*: no title, no body, no id. This is
    explicitly NOT ``filter_fragments_by_tier``'s summarise-the-body path,
    which is the shape #931/#1032 found leaking titles for fragments whose
    bodies policy had already dropped. Verified admitted sets: ``open`` admits
    ``open`` only; ``personal`` admits ``open`` + ``personal`` +
    ``unclassified``; ``intimate`` and ``all`` admit all four.

    **The routing consequence.** The most sensitive tier that can reach the
    prompt is therefore bounded by the ceiling, and :func:`reflect_tool` keys
    the model with :func:`creek_mcp.tier_ceiling.routing_tier`, whose
    ceiling-derived floor is the most sensitive tier that ceiling admits. So
    the routing tier always dominates every retrieved fragment's tier, and
    intimate titles can only be retrieved under a ceiling that already forces
    INTIMATE (local-only) routing. Pinned by
    ``test_routing_tier_dominates_every_retrieved_fragment_tier``.

    **The cost, honestly.** What a *session* bounds, and what it does not.

    With a *session*, the retrieval specialist is built and warmed once per
    server/app and reused, so the sentence-transformer load from disk and the
    embeddings-parquet read are each paid **once per process**, not once per
    call. That load is corpus-size independent and is paid even on a
    two-fragment vault, because ``_load_sentence_transformer`` carries no
    ``lru_cache`` and ``EmbeddingLinker._model`` is a per-instance slot.
    **Measured, so it is not overstated:** re-instantiating the model costs
    ~60 ms once the weights are in the OS page cache (~2 s on the first load in
    a process, ~20 s on a genuinely cold one), against ~13 ms per embed. So on a
    warm host the dominant per-call cost of a cold-parquet reflection is the
    embed pass, not the model load — which is why the cap below matters at least
    as much as the shared lifetime. Live
    embedding of cache misses is bounded per call by
    :data:`_MAX_LIVE_EMBEDS`, so a cold vault no longer embeds its whole
    admitted corpus in one request.

    **Cold once, warm thereafter.** Vectors computed live are retained in a
    session-only map capped at the same :data:`_MAX_LIVE_EMBEDS` bound. The
    second request therefore embeds its query but reuses every unchanged
    fragment vector from the first. Membership is the deterministic id-ordered
    prefix of unfiltered parquet misses, never the first caller's admitted view,
    and cannot advance past the fixed cap. The session lock serialises cold
    gathers, so simultaneous first requests cannot duplicate that work. Rows
    are keyed by fragment id plus the same content hash as the parquet cache,
    replaced in the same call when a fragment changes, and cleared when the
    session switches vaults. This is an
    in-memory optimisation only: ``POST /v1/reflections`` still writes no vault
    state and cannot race ``creek link``. Filling the parquet with
    ``creek link --method embeddings`` remains the way to ground a large vault
    fully rather than against the bounded prefix.

    With no *session* — every ``retrieve=``-injecting test, both read-gate
    probes, and any caller that does not pass one — this is unchanged: a fresh
    ``RetrievalSpecialist`` per call, an unbounded cold-cache pass, model load
    included.

    **One parse per request, not three.** Production builds one request-scoped,
    unfiltered :class:`_ReflectionCorpus`. Entry resolution, tier-filtered
    retrieval, and compiled-page provenance project from that same snapshot, so
    each corpus Markdown file crosses ``frontmatter.load`` at most once. The
    snapshot is intentionally not cached between requests: doing so would hide
    live Obsidian edits. It contains no override-derived view, so the retrieval
    cutoff and compiled-page fail-closed check are still evaluated independently
    for the current caller.

    A known asymmetry: a fragment with **no** ``privacy_tier`` key at all is
    admitted here from ``ceiling=personal`` (the ``Fragment`` model default is
    ``unclassified``), while :func:`_fragment_tier` fails that same file closed
    to INTIMATE as an *entry*. Tracked by #1033.

    **The provenance ride-along (#873).** The same claims already carry the
    fragment ids they were drawn from, and the return type now keeps them
    (:class:`_Grounding`) instead of dropping them on the floor. That is what
    lets ``related_praxis`` / ``related_eddies`` be *selected* from this one
    pass, honouring #873's "no second embedding sweep on the hot path"
    constraint. The ids are a selection signal only — the tier of every
    fragment a compiled page was built from is re-checked in
    :func:`creek_mcp.compiled_pages.related_compiled`, so a widened id set
    cannot widen what is returned.

    Args:
        query: The entry text to retrieve against.
        vault_path: Vault root whose corpus subtrees are searched.
        override: The ceiling-derived admission override, applied as a hard
            cutoff inside the corpus walk.
        session: The owner-scoped :class:`GroundingSession` holding the warmed
            specialist to reuse, or ``None`` to build a cold one for this call
            (the pre-#1034 behaviour, and still the live path for every caller
            that passes none). The session supplies a *lifetime* only: it is
            keyed on the vault and holds nothing override-derived, so *override*
            below is still applied per call, by this call, inside ``gather``.
        corpus: The request-scoped, unfiltered corpus snapshot. Production
            supplies one so entry resolution, retrieval, and compiled-page
            provenance parse each Markdown file at most once.

    Returns:
        Fragment titles, most relevant first, paired with the ids they came
        from — both empty when retrieval fails.
    """
    try:
        records = corpus.retrieval_records() if corpus is not None else None
        if session is not None:
            bundle = session.gather(query, vault_path, override, corpus=records)
        else:
            from creek.author.agents import RetrievalSpecialist

            specialist = RetrievalSpecialist()
            bundle = specialist.gather(query, vault_path, override=override)
        return _Grounding(
            lines=[claim.claim for claim in bundle.claims],
            source_ids=[
                fragment_id
                for claim in bundle.claims
                for fragment_id in claim.source_fragments
            ],
        )
    except Exception as exc:
        # This log line is the #964 remedy. The attribute this comprehension
        # reads was wrong for the entire life of the feature, so grounding
        # raised on every production call and every reflection was ungrounded —
        # undetected precisely because this swallow was silent. The log is what
        # makes the next such failure findable.
        #
        # The catch stays broad on purpose: ``RetrievalSpecialist.gather``
        # reaches ``load_config``, a full corpus parse and the
        # sentence-transformer model load, so its failure surface is open-ended
        # (ImportError, OSError, yaml errors, pydantic ``ValidationError``,
        # model-load failures) and none of it may cross the MCP boundary.
        #
        # Building and warming the session's specialist happens *inside* this
        # try for exactly that reason: it reaches the same import, the same
        # config load and the same model load, so a session that cannot be
        # built degrades to an ungrounded-but-successful reflection like every
        # other retrieval failure. ``GroundingSession.specialist`` stores
        # nothing when it raises, so the next call retries rather than serving
        # a permanently broken session.
        #
        # Log the exception's *type name only* — never ``str(exc)``,
        # ``exc_info``, or ``logger.exception``, for the reason given at
        # ``_resolve_entry`` above: these are the same user-managed corpus
        # files, and ``yaml.MarkedYAMLError`` stringifies with the offending
        # source snippet, so the message text would write tier-unknown vault
        # content into a log that carries no tier and is not covered by the
        # ceiling.
        logger.debug("Grounding degraded to none (%s)", type(exc).__name__)
        return _Grounding([], [])
