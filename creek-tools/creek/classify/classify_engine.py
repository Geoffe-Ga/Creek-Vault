"""Vault-driven classification engine for the ``creek classify`` command.

Loads every fragment from ``<vault>/01-Fragments/``, dispatches to the
configured classifier (rules or LLM), and rewrites each fragment file
in place with the updated frontmatter. Records the chosen ``method``
and ``classified_at`` timestamp on each fragment so that subsequent
``creek classify`` runs can preserve manual decisions and resume
mid-run after a crash.

Resume contract (OPS-001)
-------------------------

``creek classify --method llm`` is **resumable by default**:

1. Each fragment's frontmatter is rewritten the moment the LLM call
   returns, so progress is durable on per-fragment granularity.
2. Re-running the command short-circuits any fragment whose
   ``classification_method`` is already ``llm`` (or ``manual``). Pass
   ``--force`` to re-classify everything from scratch.
3. The set of fragment IDs touched during the current run is appended
   to ``<vault>/00-Creek-Meta/Processing-Log/llm-progress.jsonl`` for
   observability (newline-delimited JSON, one ``{"id": ...}`` object
   per line); this file is informational and **not** the source of
   truth — the per-fragment frontmatter is.

Privacy-tier pass (issue #876)
------------------------------

Every fragment leaves this engine carrying a real
:class:`~creek.models.PrivacyTier`. **Invariant: the classify engine
never persists ``privacy_tier: unclassified``.** The policy lives in
:mod:`creek.classify.privacy_pass`; the engine only decides *when* to
call it:

* **Before the router.** The tier is assigned immediately after the
  fragment is loaded and *before*
  :meth:`TierClassifiers.for_tier` picks a provider. That ordering is
  the security payload: the router reads the fragment's tier to honour
  Intimate-never-cloud (#666 / ADR-0003), so an untiered journal entry
  used to resolve to "not INTIMATE" and get shipped to the configured
  cloud provider on the very run meant to classify it.
* **Above the resume short-circuit.** A fragment already stamped
  ``classification_method: llm`` or ``manual`` still gets a tier
  *without* ``--force``, and is persisted through
  :func:`_write_tier_only`, which touches only ``privacy_tier`` and the
  derived ``voice_proxy_eligible``. This is a deliberate exception to
  the OPS-001 resume contract above: that short-circuit exists to
  protect operator curation and paid LLM tokens, and a deterministic,
  free, local tier assignment threatens neither. (The 35k-fragment demo
  vault is entirely ``llm``-stamped, so gating this behind ``--force``
  would mean re-paying for 35k cloud calls to fix a bug that needs
  zero.)
* **Escalate-only, never auto-downgrade.** ``--force`` may raise a tier
  that is too light, and the post-classification reassess may raise one
  the classifier just hardened, but neither ever lowers a tier —
  lowering is the one direction that leaks content.
* **A settled tier moves only on new evidence** (#1105). A concrete
  non-``unclassified`` tier on disk survives a non-``--force`` run
  *assignment* untouched — :func:`~creek.classify.privacy_pass.apply_tier`
  will not overwrite it. The post-classification reassess can still raise
  it, but only when *this run's* classification made the privacy
  heuristic strictly more restrictive than it was on the fragment as
  loaded (:attr:`_PreparedFragment.tier_baseline`). So a re-run over
  unchanged evidence never disturbs a tier the operator chose, while a
  freshly-revealed INTIMATE signal is no longer discarded just because
  some tier was already on record. A fragment the resume short-circuit
  preserves is not reassessed at all: this run produced no evidence
  about it.

Praxis pass (issue #877)
------------------------

Every fragment this run actually (re-)classifies also leaves the engine
carrying a scored :class:`~creek.models.PraxisPotential`. Before #877 the
field had a default and **no producer at all**, so the three consumers
gated on ``explicit`` (:mod:`creek.generate.decisions`,
:mod:`creek.generate.mining`, :mod:`creek.generate.compost`) were
structurally unreachable. The policy lives in
:mod:`creek.classify.praxis_pass`; the engine only decides *when*:

* **After the classifier, before the privacy reassess.** Running after
  :func:`_classify_one` means the escalate-only merge already sees any
  LLM verdict, and running before :func:`~creek.classify.privacy_pass.reassess`
  keeps the #876 privacy reassess as the last mutation before the write.
  It must NOT be hoisted into :func:`_prepare_fragment`: that would put
  it between the tier assignment and
  :meth:`TierClassifiers.for_tier`, where the #876 ordering above is
  load-bearing security.
* **NOT on the preserved short-circuit** — a deliberate departure from
  the tier precedent one section up. :func:`_write_tier_only` makes a
  narrow, auditable promise ("changing ONLY the privacy-tier fields")
  that is itself load-bearing when reviewing the #876 path, and the
  justification for the tier exception does not transfer: an untiered
  fragment is a live cloud-egress hole, whereas a missing praxis verdict
  is a missing feature. So an ``llm``/``manual``-stamped vault backfills
  this axis only under an explicit ``creek classify --method llm
  --force``. ``creek fill`` surfaces the outstanding count and names that
  command's token cost (:func:`creek.cli._hint_praxis_backfill`).
* **Escalate-only.** A rules re-run can raise ``none`` to ``explicit``
  but can never demote a verdict the LLM or the operator put on record.

:attr:`ClassifySummary.praxis_marked` reports how many fragments the run
actually raised — the observability that was missing when this field had
an invisible (in fact absent) producer.

Tags pass (issue #878)
----------------------

Same shape, one axis over: :attr:`~creek.models.Fragment.tags` had no
real producer either, so 2000/2000 sampled fragments of the operator's
35,330-fragment vault read ``tags: []`` and the Tag Garden read "*No tags
found in vault.*". The policy lives in :mod:`creek.classify.tags_pass`
and there are exactly **two** call sites for it in the whole toolchain:

* :func:`creek.ingest.base.assemble_ingested_fragment`, the universal
  ingest chokepoint, which tags everything arriving from now on; and
* this engine, beside the praxis pass, which is the *backfill* half —
  and the half that matters today, because the operator's vault was
  ingested long before ``tags`` had any producer at all.

There is deliberately **no third call** in
:func:`creek.pipeline.run_pipeline` (``creek/pipeline.py:655``), where
the praxis pass does need one. Praxis is a classification verdict, so it
has to run after the LLM dispatch; tags come from the body, which no
stage between ingest and write mutates. The ingest chokepoint is
therefore strictly earlier and strictly sufficient for the ``creek
process`` path, and a second call there would be pure duplication.

* **Union-merge, never replace.** :func:`~creek.classify.tags_pass.merge`
  keeps every tag already on record and appends what the body yields, so
  a re-classify can never delete a tag an operator hand-wrote in Obsidian
  (or the ``low-priority`` literal
  :mod:`creek.clean.context` appends). That makes the pass idempotent: a
  second run over an unchanged vault reports ``0``.
* **NOT on the preserved short-circuit**, for the same reason as praxis
  and more so — :func:`_write_tier_only` is not widened, and ``tags`` is
  a field operators curate by hand. An ``llm``/``manual``-stamped vault
  backfills it only under an explicit ``creek classify --method llm
  --force``; ``creek fill`` surfaces the outstanding count
  (:func:`creek.cli._hint_tags_backfill`).

:attr:`ClassifySummary.tags_extracted` reports how many fragments the run
actually gained a tag on.

Known residual (not fixable by ordering)
----------------------------------------

One class of fragment still reaches the cloud once before it is known to
be intimate: a self-authored, non-journal fragment carrying no recovery
keyword whose INTIMATE status is revealed *only* by a confessional +
conviction voice register — because that register is produced by the
very LLM call in question.
:meth:`~creek.classify.privacy.PrivacyClassifier._is_high_confidence_confessional`
needs inputs that do not exist before the call, so no single-pass
ordering can prevent that first egress. The escalation still prevents
every *future* egress for that fragment (re-runs, ``draft``, ``mine``,
voice-proxy generation, the Writing Desk), which is a strict improvement
on the prior behaviour where 100% of intimate content routed to the
cloud, every time. Fixing the residual properly needs a local
pre-classification pass, which is out of scope here.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.classify.audience import AudienceClassifier
from creek.classify.constants import (
    CLASSIFICATION_METHOD_KEY,
    CLASSIFICATION_PROVIDER_KEY,
    CLASSIFICATION_REASONING_KEY,
    CLASSIFICATION_REASONING_MAX_CHARS,
    CLASSIFIED_AT_KEY,
    CLASSIFY_TRACE_LOG_FILENAME,
    LLM_METHOD,
    MANUAL_METHOD,
    RULES_METHOD,
)
from creek.classify.llm import LLMClassifier
from creek.classify.llm.router import IntimateRoutingError
from creek.classify.praxis_pass import apply_praxis
from creek.classify.privacy import PrivacyClassifier
from creek.classify.privacy_filter import tier_of
from creek.classify.privacy_pass import (
    PRIVACY_TIER_KEY,
    apply_tier,
    needs_tier,
    reassess,
)
from creek.classify.reatomize import (
    ClassificationTree,
    Classifier,
    ReatomizeConfig,
    bubble_up_weighted,
    classify_reatomize,
)
from creek.classify.rules import RuleClassifier
from creek.classify.tags_pass import apply_tags
from creek.classify.weighted import classify_weighted
from creek.ingest.base import IngestedFragment
from creek.models import Fragment, Frequency, PrivacyTier
from creek.time import now_la
from creek.vault.reader import try_load_fragment
from creek.vault.writer import VaultWriter

LLM_PROGRESS_FILENAME = "llm-progress.jsonl"
"""Per-vault progress log filename written under ``00-Creek-Meta/Processing-Log/``.

Newline-delimited JSON (one ``{"id": ...}`` object per line); the
``.jsonl`` suffix signals the format honestly to an operator opening
the file.
"""

_PROCESSING_LOG_SUBDIR = ("00-Creek-Meta", "Processing-Log")
"""Per-vault subdirectory carrying the LLM progress + trace logs."""

_VOICE_PROXY_ELIGIBLE_KEY = "voice_proxy_eligible"
"""Frontmatter key derived from ``privacy_tier`` (BUG-009).

A :func:`~pydantic.computed_field` on :class:`~creek.models.Fragment`, so
it is serialised to disk and would go stale if a tier-only rewrite
(:func:`_write_tier_only`) left it behind.
"""

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from creek.config import ClassificationConfig, CreekConfig, LLMConfig

    # Annotation-only: the praxis verdict is compared, never constructed here.
    from creek.models import PraxisPotential

logger = logging.getLogger(__name__)


class LLMProviderUnavailableError(RuntimeError):
    """Raised when ``creek classify --method llm`` cannot reach its provider.

    Surfaced by :func:`run_classify` *before* any fragment is rewritten
    so that:

    1. The CLI can exit non-zero instead of pretending the run
       succeeded with zero work done.
    2. The vault stays clean — no fragments end up stamped with
       ``classification_method: llm`` when the LLM never actually ran.

    The configured provider name is preserved on the exception so the
    CLI message can name it ("anthropic" / "ollama") and point the
    operator at the right environment variable to fix.

    Attributes:
        provider: The configured ``llm.provider`` (e.g. ``"anthropic"``).
    """

    def __init__(self, provider: str, detail: str) -> None:
        """Build the exception with a CLI-ready message.

        Args:
            provider: The configured LLM provider name.
            detail: Provider-specific reason (env var missing, health
                check failed, etc.) — embedded in the message so the
                operator does not have to scroll back through warnings
                to see what went wrong.
        """
        self.provider = provider
        super().__init__(
            f"LLM provider {provider!r} is unavailable: {detail}",
        )


@dataclass(frozen=True)
class ClassifySummary:
    """Counts produced by a single ``creek classify`` run.

    The dataclass is genuinely immutable: ``errors`` is a tuple, not a
    list, so ``frozen=True`` actually prevents mutation. Callers that
    want to mutate the per-run state should work with the private
    :class:`_RunCounts` accumulator instead.

    Attributes:
        total: Number of Creek fragments visited (non-fragment markdown
            files in ``01-Fragments`` are silently skipped and not
            counted here).
        classified: Fragments whose frontmatter was updated. A
            ``--method llm`` run that short-circuits to the rule
            result still counts as classified — the work is real and
            the file is rewritten.
        preserved_manual: Fragments left unchanged because a human
            previously stamped ``classification_method: manual`` and
            ``--force`` was not passed. This counter reflects genuine
            operator curation only; fragments preserved by the OPS-001
            LLM resume path are reported on ``preserved_llm`` so the
            CLI summary can label the two reasons honestly (issue
            #321).
        preserved_llm: Fragments left unchanged because a prior
            ``--method llm`` run already stamped them with
            ``classification_method: llm`` and ``--force`` was not
            passed. Re-running ``--method llm`` after a crash skips
            these so the operator does not re-pay for tokens (OPS-001
            resume contract). Distinct from :attr:`preserved_manual`
            because the user never touched these files.
        skipped_high_confidence: Subset of ``classified`` for which
            the LLM was not invoked because the rule classifier
            produced a high-confidence answer. The fragment is still
            stamped ``classification_method: rules`` on disk.
        errors: Human-readable error messages (one per failure).
        privacy_tiers_assigned: Fragments whose privacy tier this run
            owned and (re-)derived — i.e. the frontmatter carried no
            tier (or an explicit ``unclassified``), or ``--force`` was
            passed (issue #876). A fragment carrying a deliberate
            operator tier that the run left alone is **not** counted.
            This counts the decision, not the bytes: a fragment whose
            subsequent write fails is counted here *and* reported on
            :attr:`errors`.
        praxis_marked: Fragments this run raised to a stronger
            :class:`~creek.models.PraxisPotential` (issue #877) — almost
            always ``none`` → ``explicit`` from the keyword pass. The
            pass is escalate-only and idempotent, so a second run over an
            unchanged vault reports ``0``. Fragments the resume
            short-circuit preserved are **not** counted: they are not
            given a praxis verdict at all without ``--force`` (see the
            module docstring). Like :attr:`privacy_tiers_assigned` this
            counts the decision, not the bytes — a fragment whose write
            then fails is counted here *and* reported on :attr:`errors`.
        tags_extracted: Fragments this run added at least one
            :attr:`~creek.models.Fragment.tags` entry to (issue #878).
            Counts *fragments changed*, not tags written, and only
            genuine additions: re-deriving a hashtag the fragment already
            records is not work the operator needs reported, so the merge
            being a union makes a second run over an unchanged vault
            report ``0``. Fragments the resume short-circuit preserved are
            **not** counted — they are not given tags at all without
            ``--force`` (see the module docstring).
    """

    total: int
    classified: int
    preserved_manual: int
    preserved_llm: int
    skipped_high_confidence: int
    errors: tuple[str, ...]
    # Appended last, with defaults, so the positional
    # ``ClassifySummary(0, 0, 0, 0, 0, ())`` construction below still
    # compiles unchanged.
    privacy_tiers_assigned: int = 0
    praxis_marked: int = 0
    tags_extracted: int = 0


@dataclass
class _RunCounts:
    """Mutable counters threaded through the per-file dispatch.

    Pulled out of :func:`run_classify` so the loop body can update
    counts without inflating that function's cyclomatic complexity.
    """

    total: int = 0
    classified: int = 0
    preserved_manual: int = 0
    preserved_llm: int = 0
    skipped: int = 0
    privacy_tiers_assigned: int = 0
    praxis_marked: int = 0
    tags_extracted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TierClassifiers:
    """Per-tier LLM classifiers for one classify run (#666).

    ``non_intimate`` serves OPEN / PERSONAL / UNCLASSIFIED fragments with the
    configured ``classification`` provider; ``intimate`` serves INTIMATE
    fragments, redirected off any cloud provider to a local one by the
    :class:`~creek.classify.llm.router.ModelRouter` chokepoint (Intimate-never-
    cloud, #647). The two are the **same instance** when the configured
    ``classification`` provider is already local — the backward-compatible case.

    When ``classification`` is cloud and no local ``default`` exists, the Intimate
    route cannot be honoured: ``intimate`` is ``None`` and ``routing_error`` holds
    the :class:`IntimateRoutingError`, **deferred** so it fires only if an actual
    Intimate fragment is reached — a run with no Intimate content stays valid.
    """

    non_intimate: LLMClassifier
    intimate: LLMClassifier | None
    routing_error: IntimateRoutingError | None

    def for_tier(self, tier: PrivacyTier) -> LLMClassifier:
        """Return the classifier for *tier* — the local one for INTIMATE.

        Raises:
            IntimateRoutingError: For an INTIMATE fragment when the config has no
                local backend to route it to (the deferred build-time error).
        """
        if tier is not PrivacyTier.INTIMATE:
            return self.non_intimate
        if self.intimate is None:
            raise self._deferred_routing_error()
        return self.intimate

    def distinct(self) -> tuple[LLMClassifier, ...]:
        """The unique classifiers to availability-check before iterating.

        Excludes the Intimate classifier when it shares the configured instance
        or is deferred (``None``) — the non-Intimate fail-fast is unchanged from
        the pre-#666 single-provider check.
        """
        if self.intimate is None or self.intimate is self.non_intimate:
            return (self.non_intimate,)
        return (self.non_intimate, self.intimate)

    def _deferred_routing_error(self) -> IntimateRoutingError:
        """Return the captured deferred routing error (invariant: set here)."""
        if self.routing_error is None:  # pragma: no cover - defensive invariant
            msg = "Intimate route unavailable but no routing error was captured."
            raise RuntimeError(msg)
        return self.routing_error


def build_tier_classifiers(config: CreekConfig) -> TierClassifiers:
    """Build the per-tier classifiers via the ModelRouter chokepoint (#666).

    Resolves ``classification`` tier-less for non-Intimate fragments and with
    ``PrivacyTier.INTIMATE`` for Intimate ones, so the Intimate route is
    redirected to a local provider. When both resolve to the same config (a local
    ``classification`` stage), a single classifier instance is shared — identical
    to the pre-#666 behaviour. When no local backend exists, the resulting
    :class:`IntimateRoutingError` is captured and **deferred** (see
    :class:`TierClassifiers`) rather than failing a run that has no Intimate
    content.

    Args:
        config: The loaded Creek configuration (carrying the model router).

    Returns:
        The :class:`TierClassifiers` for this run.
    """
    router = config.model_router
    non_intimate = LLMClassifier(config=router.resolve("classification"))
    try:
        intimate_cfg = router.resolve("classification", PrivacyTier.INTIMATE)
    except IntimateRoutingError as exc:
        return TierClassifiers(
            non_intimate=non_intimate, intimate=None, routing_error=exc
        )
    intimate = (
        non_intimate
        if intimate_cfg == non_intimate.config
        else LLMClassifier(config=intimate_cfg)
    )
    return TierClassifiers(
        non_intimate=non_intimate, intimate=intimate, routing_error=None
    )


def run_classify(
    *,
    vault_path: Path,
    config: CreekConfig,
    method: str,
    force: bool,
) -> ClassifySummary:
    """Classify every fragment in *vault_path* using the chosen method.

    Args:
        vault_path: Vault root.
        config: Loaded Creek configuration.
        method: ``"rules"`` or ``"llm"``.
        force: When ``True``, overwrite ``classification_method:
            manual`` decisions.

    Returns:
        A :class:`ClassifySummary` reporting per-method counts.

    Raises:
        LLMProviderUnavailableError: When ``method == "llm"`` and the
            configured provider fails its availability check. Raised
            *before* any fragment is processed so the vault is not
            polluted with bogus ``classification_method: llm`` stamps
            when the LLM never ran.
    """
    fragments_root = vault_path / "01-Fragments"
    if not fragments_root.exists():
        return ClassifySummary(0, 0, 0, 0, 0, ())

    rules = RuleClassifier()
    audience = AudienceClassifier()
    # Per-stage + per-tier routing (#646/#666): non-Intimate fragments use the
    # configured ``classification`` provider; Intimate fragments are redirected
    # to a local provider by the ModelRouter chokepoint (Intimate-never-cloud,
    # #647). Resolving here also fails the run loudly (IntimateRoutingError) when
    # classification is cloud and no local default exists to route Intimate to.
    tier_classifiers = build_tier_classifiers(config) if method == LLM_METHOD else None
    _assert_classifiers_available(tier_classifiers)
    counts = _RunCounts()
    progress_path: Path | None = None
    trace_log_path: Path | None = None
    if method == LLM_METHOD:
        progress_dir = vault_path.joinpath(*_PROCESSING_LOG_SUBDIR)
        # Create the directory once, not per fragment — a 10k-fragment
        # vault would otherwise issue 10k redundant ``mkdir`` syscalls
        # in ``_record_llm_progress``.
        progress_dir.mkdir(parents=True, exist_ok=True)
        progress_path = progress_dir / LLM_PROGRESS_FILENAME
        trace_log_path = progress_dir / CLASSIFY_TRACE_LOG_FILENAME

    # The vault writer is only needed when re-atomization is enabled —
    # building it eagerly avoids per-fragment construction cost and
    # surfaces a missing ``01-Fragments`` once, here, rather than
    # mid-loop.
    reatomize_enabled = method == LLM_METHOD and config.classification.reatomize
    vault_writer: VaultWriter | None = (
        VaultWriter(vault_path=vault_path) if reatomize_enabled else None
    )

    # Concurrency (#764/#783): the per-fragment LLM call is network-bound, so
    # running files through a thread pool lets the calls overlap — previously
    # this loop was serial and ``max_concurrent`` had no effect. Each distinct
    # provider gets its OWN bounded semaphore sized by its own stage's
    # ``max_concurrent``: INTIMATE fragments route to a local provider (#666),
    # which must not inherit the cloud stage's width (#783). The outer pool is
    # sized as the SUM of the distinct providers' widths so both tiers can
    # saturate their own cap at the same time without oversubscribing either.
    # Re-atomization shares one writer, so it stays serial (workers forced to 1);
    # the rules path builds no throttle and also stays serial.
    throttle, workers = _build_classify_throttle(tier_classifiers)
    if vault_writer is not None:
        workers = 1

    files = sorted(fragments_root.rglob("*.md"))
    process_one = partial(
        _process_file,
        method=method,
        force=force,
        rules=rules,
        audience=audience,
        # Issue #876: stateless and read-only for the duration of the
        # run, so one instance is safe to share across worker threads.
        privacy=PrivacyClassifier(),
        tier_classifiers=tier_classifiers,
        classification_config=config.classification,
        counts=counts,
        progress_path=progress_path,
        trace_log_path=trace_log_path,
        vault_writer=vault_writer,
        throttle=throttle,
        lock=threading.Lock(),
    )
    _dispatch_files(files, process_one, workers=workers)

    return ClassifySummary(
        total=counts.total,
        classified=counts.classified,
        preserved_manual=counts.preserved_manual,
        preserved_llm=counts.preserved_llm,
        skipped_high_confidence=counts.skipped,
        # Snapshot-by-tuple so the frozen dataclass is genuinely
        # immutable: the caller can't accidentally append to the
        # underlying list and reach into completed-run state.
        errors=tuple(counts.errors),
        privacy_tiers_assigned=counts.privacy_tiers_assigned,
        praxis_marked=counts.praxis_marked,
        tags_extracted=counts.tags_extracted,
    )


def _build_classify_throttle(
    tier_classifiers: TierClassifiers | None,
) -> tuple[dict[int, threading.BoundedSemaphore], int]:
    """Size the classify pool and build per-provider concurrency permits (#783).

    Each distinct classifier (non-Intimate vs the local Intimate route, #666)
    gets its own :class:`threading.BoundedSemaphore` sized by that classifier's
    ``max_concurrent``, keyed by ``id(classifier)`` so a fragment's resolved
    classifier maps to its own permit. The pool width is the **sum** of the
    distinct widths, so both tiers can run at their own cap simultaneously
    without either oversubscribing its provider. When ``distinct()`` returns a
    single classifier — the shared-instance / single-provider case — this
    collapses to exactly the pre-#783 single-stage width (no regression).

    Note that the pool width sums **all** distinct classifiers' ``max_concurrent``
    up front, even when a run contains fragments for only one tier (e.g. an
    all-OPEN vault with a configured-but-unused local Intimate route still gets
    ``cloud_width + local_width`` threads). Those extra threads sit idle rather
    than oversubscribe any provider — harmless, but worth knowing if the
    thread count ever surprises someone profiling (#799).

    Args:
        tier_classifiers: The per-tier classifier set, or ``None`` for the
            rules path (which runs serially with no provider to throttle).

    Returns:
        ``(throttle, workers)``: the ``id(classifier) -> semaphore`` map and the
        summed pool width. ``({}, 1)`` for the rules path.
    """
    if tier_classifiers is None:
        return {}, 1
    throttle: dict[int, threading.BoundedSemaphore] = {}
    workers = 0
    for classifier in tier_classifiers.distinct():
        max_concurrent = classifier.config.max_concurrent
        throttle[id(classifier)] = threading.BoundedSemaphore(max_concurrent)
        workers += max_concurrent
    return throttle, workers


@contextmanager
def _classify_permit(
    throttle: dict[int, threading.BoundedSemaphore],
    llm: LLMClassifier | None,
) -> Iterator[None]:
    """Hold the resolved classifier's concurrency permit for the classify call.

    Wraps only the leaf ``_classify_one`` call (#783): the permit is acquired
    OUTSIDE the shared ``lock`` — exactly where the expensive provider call
    already runs unlocked (#764) — and released on the way out, including on
    exception. When there is no matching semaphore (the rules path, where
    ``llm`` is ``None``, or a provider with no registered permit) this yields
    without throttling.

    Args:
        throttle: The ``id(classifier) -> semaphore`` map from
            :func:`_build_classify_throttle`.
        llm: The classifier resolved for this fragment's tier, or ``None`` on
            the rules path.

    Yields:
        ``None`` while the permit (if any) is held.
    """
    semaphore = throttle.get(id(llm)) if llm is not None else None
    if semaphore is None:
        yield
        return
    with semaphore:
        yield


def _record_if_preserved(
    raw: dict[str, object],
    counts: _RunCounts,
) -> bool:
    """Update *counts* in place when *raw* names a preserved prior method.

    Returns ``True`` (caller should short-circuit) when the fragment's
    existing ``classification_method`` is either ``manual`` (operator
    curation) or ``llm`` (paid LLM call from a prior OPS-001 resume
    point). The two reasons are tallied separately so the CLI summary
    can label them distinctly (issue #321) rather than blaming the
    operator for state actually left behind by automation.

    Args:
        raw: Frontmatter dict for the fragment under consideration.
        counts: Mutable per-run counters; mutated in place when the
            fragment is preserved.

    Returns:
        ``True`` when the fragment is preserved (caller must not
        re-classify it); ``False`` when classification should proceed.
    """
    existing_method = raw.get(CLASSIFICATION_METHOD_KEY)
    if existing_method == MANUAL_METHOD:
        counts.preserved_manual += 1
        return True
    if existing_method == LLM_METHOD:
        counts.preserved_llm += 1
        return True
    return False


def _record_praxis(
    before: PraxisPotential,
    fragment: Fragment,
    counts: _RunCounts,
) -> None:
    """Tally a fragment whose praxis verdict this run raised (issue #877).

    Kept as its own function so the ``if`` lives here rather than in
    :func:`_process_file`, whose cyclomatic complexity is already at its
    working ceiling. Comparing before/after is safe precisely because
    :func:`~creek.classify.praxis_pass.apply_praxis` is escalate-only:
    the values can only ever differ upwards, so this counts real work and
    never churn.

    Args:
        before: The verdict the fragment carried before the praxis pass.
        fragment: The fragment after the pass.
        counts: Mutable per-run counters; mutated in place.
    """
    if fragment.praxis_potential != before:
        counts.praxis_marked += 1


def _record_tags(
    before: list[str],
    fragment: Fragment,
    counts: _RunCounts,
) -> None:
    """Tally a fragment this run added a tag to (issue #878).

    Kept as its own function for the same reason as :func:`_record_praxis`
    — :func:`_process_file`'s cyclomatic complexity is already at its
    working ceiling and does not need another ``if``. Comparing
    before/after is safe precisely because
    :func:`~creek.classify.tags_pass.apply_tags` merges as a union that
    never loses a tag: the list can only grow, or be re-canonicalised in
    place — which can shorten it, when two spellings of one tag collapse
    to the single tag they always named — so any difference is real work
    and never churn.

    Args:
        before: The tags the fragment carried before the pass.
        fragment: The fragment after the pass.
        counts: Mutable per-run counters; mutated in place.
    """
    if fragment.tags != before:
        counts.tags_extracted += 1


def _assert_classifiers_available(tier_classifiers: TierClassifiers | None) -> None:
    """Raise if any per-tier classifier's provider is unreachable (#666).

    Refuses to iterate when a provider is unreachable so the vault is never
    stamped with ``classification_method: llm`` for fragments the LLM never saw.
    The orchestrator already logged the provider-specific reason (missing key,
    missing consent, Ollama down) as a WARNING; this surfaces the remediation
    hint instead of burying it in stderr.

    Args:
        tier_classifiers: The per-tier classifier set, or ``None`` for rules.

    Raises:
        LLMProviderUnavailableError: When a configured provider is unavailable.
    """
    if tier_classifiers is None:
        return
    for classifier in tier_classifiers.distinct():
        if not classifier.available:
            raise LLMProviderUnavailableError(
                provider=classifier.config.provider,
                detail=_describe_llm_unavailability(classifier.config.provider),
            )


def _dispatch_files(
    files: list[Path],
    process_one: Callable[[Path], None],
    *,
    workers: int,
) -> None:
    """Run *process_one* over *files* — concurrently when ``workers > 1`` (#764).

    A thread pool sized by *workers* lets the network-bound per-fragment LLM
    calls overlap; ``workers == 1`` preserves the original strictly-serial loop.
    The map is drained so every file completes (and any error surfaces) before
    returning.

    Args:
        files: Fragment files to process, in order.
        process_one: Callback classifying and writing one file.
        workers: Thread-pool width (1 = serial).
    """
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process_one, files))
    else:
        for md_file in files:
            process_one(md_file)


@dataclass(frozen=True)
class _PreparedFragment:
    """A loaded, tiered fragment that still needs classifying (issue #876).

    The return shape of :func:`_prepare_fragment` — everything the
    classify-and-write half of :func:`_process_file` needs, so that half
    never has to re-derive the tier decision.

    Attributes:
        fragment: The fragment as loaded, carrying a concrete privacy
            tier.
        body: Its markdown body.
        raw: Its frontmatter exactly as read from disk (non-Fragment
            keys are preserved through the rewrite).
        llm: The classifier the fragment's tier resolved to, or ``None``
            on the rules path.
        tier_baseline: The privacy heuristic's verdict on the fragment
            **as loaded**, taken before this run classified anything.
            :func:`~creek.classify.privacy_pass.reassess` compares its
            own verdict against this to answer the only question that
            licenses touching a settled tier: did *this run* make the
            heuristic stricter? (#1105)
    """

    fragment: Fragment
    body: str
    raw: dict[str, object]
    llm: LLMClassifier | None
    tier_baseline: PrivacyTier


def _prepare_fragment(
    *,
    md_file: Path,
    force: bool,
    privacy: PrivacyClassifier,
    tier_classifiers: TierClassifiers | None,
    counts: _RunCounts,
) -> _PreparedFragment | None:
    """Load *md_file*, tier it, and resolve its classifier — or short-circuit.

    Runs under :func:`_process_file`'s lock. Ordering here is load-bearing
    (issue #876): the privacy tier is assigned *before*
    :meth:`TierClassifiers.for_tier` reads it, so an Intimate fragment
    routes to the local provider on its very first classification rather
    than after the cloud has already seen the body; and it is assigned
    *above* the OPS-001 / issue-#321 preserved short-circuit, so the
    ``llm``/``manual``-stamped fragments that make up a mature vault can
    acquire a tier without ``--force`` re-paying for their classification.

    One more ordering is load-bearing (issue #1105):
    :attr:`_PreparedFragment.tier_baseline` is captured *first of all*, on
    the fragment exactly as loaded. It is the reference point
    :func:`~creek.classify.privacy_pass.reassess` needs to judge whether
    this run learned anything new about the fragment's privacy, so any
    mutation taken before it — the tier stamp here, the classifier's voice
    verdict later — would fold this run's own output back into the
    baseline and blind the gate to it.

    Args:
        md_file: The file to consider.
        force: Whether to re-derive settled classifications and tiers.
        privacy: Shared :class:`PrivacyClassifier`.
        tier_classifiers: Per-tier :class:`LLMClassifier` set, or ``None``
            on the rules path.
        counts: Mutable per-run counters; mutated in place.

    Returns:
        The :class:`_PreparedFragment` to classify, or ``None`` when the
        file is not a Creek fragment, is unreadable, or was preserved by
        the resume short-circuit (in which case any newly-assigned tier
        has already been persisted here).
    """
    record = _load_classifiable_fragment(md_file=md_file, counts=counts)
    if record is None:
        return None
    fragment, body, raw = record

    # #1105: record the heuristic's verdict on the fragment as loaded, BEFORE
    # ``apply_tier`` (and the classifier below) change anything it reads. The
    # post-classification reassess needs this to tell "the model just revealed
    # something new" from "the model echoed what was already on disk"; only the
    # former licenses raising a tier that is already on record.
    tier_baseline = privacy.classify_tier(fragment, content=body)

    # #876: stamp a real tier before anything else looks at one. ``force``
    # merges escalate-only, so an existing ``intimate`` is never lowered.
    #
    # Since #1105 this answers only "did this run *assign* the tier?", and its
    # two remaining consumers are the ``privacy_tiers_assigned`` counter just
    # below and the preserved-path :func:`_persist_tier_only` write. It no
    # longer gates the post-classification reassess — ``tier_baseline`` does
    # that — because "a tier is already on record" turned out to say nothing
    # about whether a *person* put it there.
    owns_tier = needs_tier(raw) or force
    fragment = apply_tier(fragment, body, raw=raw, force=force, classifier=privacy)
    if owns_tier:
        counts.privacy_tiers_assigned += 1

    # Per-tier routing (#666): pick the classifier for this fragment's
    # privacy tier. ``tier_of`` fails closed to INTIMATE for an unrecognised
    # tier, so an unknown fragment is classified locally rather than risking
    # cloud egress.
    llm = (
        tier_classifiers.for_tier(tier_of(fragment))
        if tier_classifiers is not None
        else None
    )

    # Skip fragments whose previous run already settled the classification.
    # Tracked on distinct counters so the CLI summary can label "manual
    # preserved" and "previously LLM-classified preserved" honestly
    # (issue #321). A tier assigned above still lands, through the narrow
    # writer that touches nothing else.
    if not force and _record_if_preserved(raw, counts):
        if owns_tier:
            _persist_tier_only(
                md_file=md_file,
                fragment=fragment,
                body=body,
                raw=raw,
                counts=counts,
            )
        return None
    return _PreparedFragment(
        fragment=fragment,
        body=body,
        raw=raw,
        llm=llm,
        tier_baseline=tier_baseline,
    )


def _process_file(
    md_file: Path,
    *,
    method: str,
    force: bool,
    rules: RuleClassifier,
    audience: AudienceClassifier,
    privacy: PrivacyClassifier,
    tier_classifiers: TierClassifiers | None,
    classification_config: ClassificationConfig,
    counts: _RunCounts,
    progress_path: Path | None,
    trace_log_path: Path | None,
    vault_writer: VaultWriter | None,
    throttle: dict[int, threading.BoundedSemaphore],
    lock: threading.Lock,
) -> None:
    """Classify a single fragment file and update ``counts`` in place.

    Args:
        md_file: The file to consider.
        method: ``"rules"`` or ``"llm"``.
        force: Whether to overwrite previously-classified fragments.
        rules: Shared :class:`RuleClassifier` instance.
        audience: Shared :class:`AudienceClassifier`; stamps the #634
            audience axis on every (re)classified fragment, deterministically,
            regardless of method.
        privacy: Shared :class:`PrivacyClassifier`; assigns the #876
            privacy tier before the router reads it and re-checks it once
            classification has hardened the fragment's voice signals.
        tier_classifiers: Per-tier :class:`LLMClassifier` set (when
            ``method == "llm"``); the fragment's tier selects the provider so
            Intimate content is classified locally (#666). ``None`` for rules.
        classification_config: Full FEAT-023-aware classification config.
            Drives both the LLM-vs-rules threshold and the
            ``reatomize`` opt-in.
        counts: Mutable per-run counters; mutated in place.
        progress_path: Optional path to the LLM-progress checkpoint file
            (OPS-001). When set, the fragment ID is appended after a
            successful LLM classification. Manual / rules-shortcircuit
            paths are not appended — only paid LLM calls need to be
            recovered on resume.
        trace_log_path: Optional path to the FEAT-017 reasoning trace
            log. When set, ``intimate``-tier fragments route their full
            reasoning trace here instead of into frontmatter; other
            tiers store a truncated trace in frontmatter regardless.
        vault_writer: Shared :class:`VaultWriter` for persisting
            FEAT-023 children. ``None`` when re-atomization is not
            enabled for this run.
        throttle: Per-provider concurrency permits keyed by
            ``id(classifier)`` (#783). The classifier resolved for this
            fragment's tier acquires its own bounded semaphore around the
            classify call, so a mixed-tier vault never applies the cloud
            stage's width to the local Intimate provider. Empty on the rules
            path (no provider to throttle).
        lock: Guards the shared-state sections (``counts`` mutation, the
            progress/trace-log appends, and the re-atomization writer) so this
            function is safe to run on many worker threads at once. The
            expensive, network-bound classify call runs *outside* the lock — and
            under the resolved provider's permit — so calls from different
            threads overlap up to each provider's own cap (the fix for
            #764/#783). An uncontended lock is passed on the serial path.
    """
    with lock:
        prepared = _prepare_fragment(
            md_file=md_file,
            force=force,
            privacy=privacy,
            tier_classifiers=tier_classifiers,
            counts=counts,
        )
    if prepared is None:
        return
    body, raw, llm = prepared.body, prepared.raw, prepared.llm

    # The provider call is the expensive, network-bound step (#764). It runs
    # WITHOUT the lock so calls from different worker threads overlap — that is
    # what makes ``max_concurrent`` effective — but UNDER the resolved provider's
    # own bounded semaphore (#783), so the local Intimate provider is capped by
    # its own width, not the cloud stage's. The permit wraps only this leaf call
    # (never the locked write block below) and is released even on error. The
    # classifiers are pre-warmed (their availability was checked before the loop)
    # and read-only during the run, and each fragment is an independent input, so
    # concurrent calls are safe.
    with _classify_permit(throttle, llm):
        new_fragment, was_skipped, reasoning = _classify_one(
            fragment=prepared.fragment,
            body=body,
            method=method,
            rules=rules,
            llm=llm,
            confidence_threshold=classification_config.confidence_threshold,
            weighted_classification=classification_config.weighted_classification,
        )

    # Stamp the #634 audience axis on every (re)classified fragment. It is a
    # deterministic heuristic independent of the frequency/phase method above,
    # so it runs on both the rules and LLM paths and stays idempotent.
    new_fragment = audience.classify_and_enforce(new_fragment, body)

    # #877: score the praxis axis from the free keyword pass. Unconditional
    # by design — the purity of ``apply_praxis`` absorbs the decision (it
    # returns the same object when nothing escalates), so this adds no
    # branch to this function. It runs AFTER ``_classify_one`` so the
    # escalate-only merge already sees any LLM verdict, and BEFORE the
    # reassess below so the #876 privacy pass stays the last mutation
    # before the write.
    praxis_before = new_fragment.praxis_potential
    new_fragment = apply_praxis(new_fragment, body)

    # #878: extract the body's hashtags into ``tags``. Unconditional for the
    # same reason as the praxis pass above — ``apply_tags`` is pure and
    # returns the same object when the union adds nothing, so this adds no
    # branch here. Placed beside praxis rather than in ``_prepare_fragment``
    # so the #876 tier-then-router ordering stays untouched; the merge is a
    # union, so it is order-independent with respect to everything around it.
    tags_before = new_fragment.tags.copy()
    new_fragment = apply_tags(new_fragment, body)

    # #876: classification may have hardened the fragment's voice signals
    # (confessional + conviction is one of the INTIMATE triggers), which the
    # pre-pass could not see. Take the stricter of the two verdicts —
    # escalate-only, so nothing here can lower a tier.
    #
    # #1105: unconditional, because ``reassess`` now carries its own gate.
    # Its ``baseline`` is the heuristic's verdict on the fragment as loaded,
    # so it fires only where this run's classification made the heuristic
    # strictly stricter — new evidence, which no tier on disk predates. The
    # gate this replaces asked "does this run own the tier?", which is False
    # for every already-tiered fragment, i.e. for the whole of a mature
    # vault; that answer discarded exactly the verdicts it was here to catch.
    new_fragment = reassess(
        new_fragment,
        body,
        baseline=prepared.tier_baseline,
        classifier=privacy,
    )

    with lock:
        _record_praxis(praxis_before, new_fragment, counts)
        _record_tags(tags_before, new_fragment, counts)

        # FEAT-023 wire-up (issue #318): when ``classification.reatomize`` is
        # True we run the orchestrator on the root fragment and persist any
        # newly-derived child fragments. The root's frontmatter still flows
        # through the existing write path below so the per-run provenance
        # stamps (method, classified_at, reasoning) stay consistent with the
        # non-reatomize code path. (Re-atomization runs serially — see
        # run_classify — so the shared VaultWriter is never touched concurrently.)
        if vault_writer is not None and not was_skipped:
            _maybe_reatomize_and_persist(
                root_fragment=new_fragment,
                body=body,
                rules=rules,
                llm=llm,
                classification_config=classification_config,
                vault_writer=vault_writer,
                counts=counts,
            )

        _finalise_fragment_write(
            md_file=md_file,
            new_fragment=new_fragment,
            body=body,
            raw=raw,
            reasoning=reasoning,
            method=method,
            # The fragment's tier picked the classifier (#666); record its
            # provider so a later quality-aware run can rank local-LLM vs
            # cloud-LLM. Only an actual ``llm`` write keeps it (the writer
            # clears it otherwise).
            provider=llm.config.provider if llm is not None else None,
            was_skipped=was_skipped,
            counts=counts,
            progress_path=progress_path,
            trace_log_path=trace_log_path,
        )


def _load_classifiable_fragment(
    *,
    md_file: Path,
    counts: _RunCounts,
) -> tuple[Fragment, str, dict[str, object]] | None:
    """Read ``md_file`` and decide whether it needs a fresh classification.

    Returns the ``(fragment, body, raw)`` triple when the file should
    flow into the classifier. Returns ``None`` when the file is not a
    Creek fragment or is unreadable. Updates ``counts`` in place for
    the ``total`` and ``errors`` cases so the caller only handles the
    happy path. The OPS-001 / issue #321 "preserved" short-circuit is
    applied by :func:`_record_if_preserved` in :func:`_prepare_fragment`
    so the per-reason counters (``preserved_manual`` vs
    ``preserved_llm``) stay the single source of truth.

    Args:
        md_file: Vault fragment to consider.
        counts: Mutable per-run counters; mutated in place when the
            fragment is identified or proves unreadable.
    """
    try:
        record = _read_fragment(md_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        counts.errors.append(f"unreadable fragment {md_file}: {exc}")
        return None
    if record is None:
        # File parsed cleanly but is not a Creek fragment (no
        # ``type: fragment`` field, or schema mismatch). Silently skip —
        # markdown notes coexist with fragments in the vault, and it
        # would be misleading to count them in ``total``.
        return None
    # Only count files we actually identify as Creek fragments.
    counts.total += 1
    return record


def _finalise_fragment_write(
    *,
    md_file: Path,
    new_fragment: Fragment,
    body: str,
    raw: dict[str, object],
    reasoning: str,
    method: str,
    provider: str | None = None,
    was_skipped: bool,
    counts: _RunCounts,
    progress_path: Path | None,
    trace_log_path: Path | None,
) -> None:
    """Persist the (re-)classified root fragment back to its source file.

    Pulled out of :func:`_process_file` so the FEAT-023 wire-up does
    not push that function past the cyclomatic-complexity ceiling.
    """
    # When ``--method llm`` short-circuits because the rule classifier
    # already produced a confident answer, the provenance stamp must
    # reflect what actually classified the fragment ("rules"), not
    # the user's CLI choice ("llm"). Either way the fragment IS
    # persisted — we never skip the write, only the LLM call.
    write_method = RULES_METHOD if was_skipped else method

    reasoning_for_frontmatter = _route_reasoning(
        fragment=new_fragment,
        reasoning=reasoning,
        method=write_method,
        trace_log_path=trace_log_path,
    )
    try:  # noqa: TRY101  # ARG: clear failure-mode separation (read vs. write)
        _write_fragment(
            md_file=md_file,
            fragment=new_fragment,
            body=body,
            method=write_method,
            raw=raw,
            reasoning=reasoning_for_frontmatter,
            provider=provider,
        )
    except OSError as exc:
        counts.errors.append(f"failed to update {md_file}: {exc}")
        return
    counts.classified += 1
    if was_skipped:
        counts.skipped += 1
    elif progress_path is not None and write_method == LLM_METHOD:
        _record_llm_progress(progress_path, new_fragment.id)


def _persist_tier_only(
    *,
    md_file: Path,
    fragment: Fragment,
    body: str,
    raw: dict[str, object],
    counts: _RunCounts,
) -> None:
    """Write a preserved fragment's new privacy tier, recording any failure.

    A single unwritable file must not abort a 35k-file run, so an
    :class:`OSError` is appended to :attr:`_RunCounts.errors` and the
    remaining fragments still classify normally — the same contract the
    main write path (:func:`_finalise_fragment_write`) honours.

    Args:
        md_file: Destination file, rewritten in place.
        fragment: The fragment carrying the freshly-assigned tier.
        body: Markdown body to retain, byte-identical, below the
            frontmatter.
        raw: The frontmatter as read from disk.
        counts: Mutable per-run counters; write failures land on
            :attr:`_RunCounts.errors`.
    """
    try:
        _write_tier_only(md_file=md_file, fragment=fragment, body=body, raw=raw)
    except OSError as exc:
        counts.errors.append(
            f"failed to update privacy tier for {fragment.id} ({md_file}): {exc}",
        )


def _write_tier_only(
    *,
    md_file: Path,
    fragment: Fragment,
    body: str,
    raw: dict[str, object],
) -> None:
    """Rewrite *md_file* changing ONLY the privacy-tier fields (issue #876).

    Used exclusively on the preserved short-circuit path, where the whole
    point is that the run did *not* re-classify the fragment. So this
    writer must leave ``classification_method``, ``classified_at``,
    ``classification_provider``, ``classification_reasoning`` and the body
    exactly as they were, and touch only:

    * ``privacy_tier`` — the newly-assigned tier; and
    * ``voice_proxy_eligible`` — a ``@computed_field`` on
      :class:`~creek.models.Fragment` derived from the tier (BUG-009).
      Because it *is* serialised into frontmatter, leaving it behind
      would let a fragment freshly marked ``intimate`` keep advertising
      itself as voice-proxy eligible.

    Args:
        md_file: Destination file, rewritten in place.
        fragment: The fragment carrying the freshly-assigned tier.
        body: Markdown body to retain unchanged.
        raw: Original frontmatter; every other key is copied verbatim.

    Raises:
        OSError: When the file cannot be written. The caller
            (:func:`_persist_tier_only`) records it and carries on.
    """
    new_metadata = raw.copy()
    # ``.value`` because ``Fragment`` uses ``use_enum_values=True`` but
    # ``model_copy`` bypasses that coercion — YAML's SafeDumper cannot
    # represent a bare StrEnum member.
    new_metadata[PRIVACY_TIER_KEY] = PrivacyTier(fragment.privacy_tier).value
    new_metadata[_VOICE_PROXY_ELIGIBLE_KEY] = fragment.voice_proxy_eligible

    post = frontmatter.Post(content=body)
    post.metadata.update(new_metadata)
    md_file.write_text(frontmatter.dumps(post), encoding="utf-8")


def _classify_one(
    *,
    fragment: Fragment,
    body: str,
    method: str,
    rules: RuleClassifier,
    llm: LLMClassifier | None,
    confidence_threshold: float,
    weighted_classification: bool = False,
) -> tuple[Fragment, bool, str]:
    """Run the chosen classifier on a single fragment.

    Args:
        fragment: Fragment to classify.
        body: Markdown body for keyword scoring.
        method: ``"rules"`` or ``"llm"``.
        rules: Shared :class:`RuleClassifier` instance.
        llm: :class:`LLMClassifier` instance when ``method == "llm"``.
        confidence_threshold: Threshold below which the LLM is invoked
            during ``--method llm`` runs.
        weighted_classification: When ``True``, the LLM path
            dispatches through
            :func:`creek.classify.weighted.classify_weighted`, persists
            the resulting weighted profile to
            :attr:`Fragment.weighted`, and derives the legacy
            single-pick fields via
            :meth:`WeightedFragmentClassification.to_legacy` so
            downstream consumers stay synchronised. Has no effect on
            the rules path or on ``--method llm`` runs where the rule
            classifier already cleared the confidence floor; for those
            fragments :attr:`Fragment.weighted` stays ``None``.

    Returns:
        ``(updated_fragment, skipped, reasoning)``. ``skipped`` is
        ``True`` when the LLM was not invoked because the rule
        classifier already produced a confident answer. ``reasoning``
        is the LLM's reasoning trace (FEAT-017); empty string for the
        rules path or when the LLM produced no preamble.
    """
    if method == "rules":
        return rules.classify(fragment, content=body), False, ""

    rule_result = rules.classify(fragment, content=body)
    if rule_result.frequency.primary != Frequency.UNCLASSIFIED:
        confidence = rules.confidence_score(rule_result, content=body)
        if confidence >= confidence_threshold:
            return rule_result, True, ""

    if llm is None:  # pragma: no cover  # no issue: defensive guard, unreachable
        msg = "LLM classifier required when method='llm'"
        raise RuntimeError(msg)
    if weighted_classification:
        return _classify_one_weighted(rule_result, body, llm.config)
    llm_result = llm.classify_with_reasoning(rule_result, content=body)
    if not llm_result.succeeded:
        # The LLM call failed (provider unavailable / retries exhausted) and
        # returned the fragment unchanged. Treat it like the rules-sufficed skip
        # so the write path stamps ``rules`` (the result that actually stands),
        # NOT a lying ``classification_method: llm`` — and the fragment stays
        # eligible for a later re-classify rather than being skipped (#744).
        return llm_result.fragment, True, ""
    return llm_result.fragment, False, llm_result.reasoning


def _classify_one_weighted(
    fragment: Fragment,
    body: str,
    llm_config: LLMConfig,
) -> tuple[Fragment, bool, str]:
    """Dispatch the LLM call through the weighted classifier.

    Replaces the single-pick LLM path when
    :attr:`ClassificationConfig.weighted_classification` is set.
    Populates :attr:`Fragment.weighted` with the full weighted
    profile and derives the legacy single-pick fields from the
    profile's top entries via
    :meth:`WeightedFragmentClassification.to_legacy` so existing
    consumers (lint, compile, voice-skill generation) stay
    synchronised. When the underlying call fails soft to an empty
    profile (provider unavailable, transport error, malformed YAML)
    the legacy fields collapse to ``UNCLASSIFIED`` — the operator
    sees the same "no signal" verdict the single-pick path produces
    on failure.

    Args:
        fragment: Fragment carrying any rule-classifier output that
            the LLM call is about to override.
        body: Markdown body, supplied because :class:`Fragment` does
            not carry its content text on the model itself.
        llm_config: Provider configuration; honours the same Ollama /
            Anthropic selection the legacy LLM path uses.

    Returns:
        ``(updated_fragment, False, reasoning)``: the weighted profile
        plus its derived legacy fields land on the returned Fragment;
        ``False`` reflects "the LLM was actually invoked"; the
        reasoning trace mirrors the model's preamble for FEAT-017
        observability.
    """
    ingested = IngestedFragment(fragment=fragment, body=body)
    weighted = classify_weighted(ingested, llm_config)
    freq, wave, voice = weighted.to_legacy()
    updated = fragment.model_copy(
        update={
            "weighted": weighted,
            "frequency": freq,
            "wavelength": wave,
            "voice": voice,
        },
    )
    return updated, False, weighted.reasoning


def _build_reatomize_classifier(
    *,
    rules: RuleClassifier,
    llm: LLMClassifier | None,
    confidence_threshold: float,
) -> Classifier:
    """Build a :class:`Classifier` closure for :func:`classify_reatomize`.

    Each call reuses the same rule + LLM dispatch logic as the legacy
    single-pass path (:func:`_classify_one`), but returns the
    ``(IngestedFragment, confidence)`` shape FEAT-023 expects so the
    orchestrator can recurse on low-confidence results.

    Args:
        rules: Shared :class:`RuleClassifier`.
        llm: Shared :class:`LLMClassifier`; ``None`` short-circuits to
            the rule result + the rule-derived confidence score.
        confidence_threshold: Floor below which the LLM is invoked.

    Returns:
        A callable matching the FEAT-023 :data:`Classifier` protocol.
    """

    def _classify(
        ingested: IngestedFragment,
    ) -> tuple[IngestedFragment, float]:
        classified, _was_skipped, _reasoning = _classify_one(
            fragment=ingested.fragment,
            body=ingested.body,
            method=LLM_METHOD if llm is not None else RULES_METHOD,
            rules=rules,
            llm=llm,
            confidence_threshold=confidence_threshold,
        )
        confidence = rules.confidence_score(classified, content=ingested.body)
        return (
            IngestedFragment(fragment=classified, body=ingested.body),
            confidence,
        )

    return _classify


def _maybe_reatomize_and_persist(
    *,
    root_fragment: Fragment,
    body: str,
    rules: RuleClassifier,
    llm: LLMClassifier | None,
    classification_config: ClassificationConfig,
    vault_writer: VaultWriter,
    counts: _RunCounts,
) -> None:
    """Run FEAT-023 orchestrator on ``root_fragment`` and persist new children.

    Only invoked when ``classification.reatomize`` is True. The root
    file itself is rewritten by the surrounding caller via the normal
    write path; this helper's responsibility is the recursive children
    only — they are net-new files that need a fresh on-disk identity
    via the platform-routing :class:`VaultWriter`.

    Persistence errors are appended to :attr:`_RunCounts.errors` so
    a single failed child does not abort the run.

    Args:
        root_fragment: The already-classified root fragment.
        body: The root fragment's markdown body (for the splitter).
        rules: Shared :class:`RuleClassifier`.
        llm: Shared :class:`LLMClassifier`.
        classification_config: Drives the FEAT-023 knobs (threshold,
            max-depth, direction).
        vault_writer: Reusable writer for child fragment files.
        counts: Mutable per-run counters; child write failures are
            recorded as errors here.
    """
    reatomize_config = ReatomizeConfig.from_classification_config(
        classification_config,
    )
    classifier = _build_reatomize_classifier(
        rules=rules,
        llm=llm,
        confidence_threshold=classification_config.confidence_threshold,
    )
    tree = classify_reatomize(
        IngestedFragment(fragment=root_fragment, body=body),
        classifier,
        config=reatomize_config,
    )
    # Legacy single-pick fields on each fragment are untouched here;
    # only ``Fragment.weighted`` is rolled up via the holonic combiner.
    if classification_config.weighted_classification:
        tree = bubble_up_weighted(tree)
    _persist_reatomized_children(
        tree=tree,
        root_id=root_fragment.id,
        root_tier=tier_of(root_fragment),
        vault_writer=vault_writer,
        counts=counts,
    )


def _persist_reatomized_children(
    *,
    tree: ClassificationTree,
    root_id: str,
    root_tier: PrivacyTier,
    vault_writer: VaultWriter,
    counts: _RunCounts,
) -> None:
    """Walk *tree* and write every descendant fragment to the vault.

    The root node (``fragment.id == root_id``) is **not** written here —
    the surrounding engine already rewrites the source file in place.
    Every other node is a child produced by the FEAT-021 splitter (or
    FEAT-022 aggregator) and needs its own file under the appropriate
    ``01-Fragments/<subfolder>/`` directory.

    Args:
        tree: Output of :func:`classify_reatomize`.
        root_id: ID of the fragment whose file the engine already owns.
        root_tier: The root's privacy tier, inherited fail-closed by any
            child that arrives untiered (issue #876).
        vault_writer: Shared :class:`VaultWriter` instance.
        counts: Mutable per-run counters; write failures are recorded
            on :attr:`_RunCounts.errors`.
    """
    for node in _walk_tree(tree):
        if node.fragment.fragment.id == root_id:
            continue
        child_fragment = _child_with_concrete_tier(node.fragment.fragment, root_tier)
        try:
            vault_writer.write_fragment(child_fragment, body=node.fragment.body)
        except (OSError, KeyError) as exc:
            counts.errors.append(
                f"failed to write reatomized child {child_fragment.id}: {exc}",
            )


def _child_with_concrete_tier(child: Fragment, root_tier: PrivacyTier) -> Fragment:
    """Return *child* guaranteed to carry a real tier, inheriting the root's.

    The FEAT-021 splitter derives children with ``parent.model_copy``, so
    they normally inherit the tier the engine just assigned. This guard
    covers the paths that do not — a future operator that mints a child
    fresh, or a :class:`~creek.models.Fragment` default that survives —
    because these children are written through :class:`VaultWriter`, a
    path the root never travels and where an ``unclassified`` row would
    silently re-open the fail-open hole (issue #876) in a vault the
    classify run had just finished cleaning.

    Args:
        child: A re-atomized descendant fragment.
        root_tier: The already-assigned tier of the fragment it was
            derived from.

    Returns:
        *child* unchanged when it already carries a tier, otherwise a
        copy at *root_tier*.
    """
    if tier_of(child) is not PrivacyTier.UNCLASSIFIED:
        return child
    return child.model_copy(update={"privacy_tier": root_tier})


def _walk_tree(
    tree: ClassificationTree,
) -> Iterator[ClassificationTree]:
    """Yield every node of ``tree`` in depth-first pre-order.

    Pulled out so the persistence loop reads as a flat ``for`` over
    nodes rather than a hand-rolled recursive walker — the latter would
    have to share state with persistence and would push the function
    over the cyclomatic-complexity ceiling.
    """
    yield tree
    for child in tree.children:
        yield from _walk_tree(child)


def _route_reasoning(
    *,
    fragment: Fragment,
    reasoning: str,
    method: str,
    trace_log_path: Path | None,
) -> str:
    """Persist the LLM reasoning trace per FEAT-017 tier-routing rules.

    For ``intimate``-tier fragments the full reasoning is appended to
    ``trace_log_path`` (gitignored per FEAT-019) and the frontmatter
    receives an empty string — keeping intimate model traces out of
    files that may be synced or shared. For all other tiers the trace
    is truncated to :data:`CLASSIFICATION_REASONING_MAX_CHARS` and
    returned for direct embedding in frontmatter.

    Args:
        fragment: The fragment whose tier dictates routing.
        reasoning: The raw reasoning preamble; may be empty.
        method: ``"rules"`` (no-op routing), ``"llm"``, or
            ``"manual"``.
        trace_log_path: Destination for intimate-tier full traces.

    Returns:
        The string the engine should store in
        ``classification_reasoning`` frontmatter. Empty when there is
        no reasoning to persist (rules path) or the fragment is
        intimate (the full trace goes to the log file instead).
    """
    if method != LLM_METHOD or not reasoning:
        return ""
    if fragment.privacy_tier == PrivacyTier.INTIMATE.value:
        if trace_log_path is not None:
            _append_trace_log(trace_log_path, fragment, reasoning)
        return ""
    return _truncate_reasoning(reasoning)


def _truncate_reasoning(text: str) -> str:
    """Truncate *text* to :data:`CLASSIFICATION_REASONING_MAX_CHARS`.

    The truncation marker is a single ``…`` so a downstream reader can
    tell the trace was cropped without parsing the byte length.
    """
    if len(text) <= CLASSIFICATION_REASONING_MAX_CHARS:
        return text
    return text[: CLASSIFICATION_REASONING_MAX_CHARS - 1] + "…"


def _append_trace_log(
    trace_log_path: Path,
    fragment: Fragment,
    reasoning: str,
) -> None:
    """Append a full reasoning trace to the intimate-tier log file.

    Failing to write the trace is logged at ``WARNING`` and the
    classification proceeds — losing the log entry costs observability,
    never a re-classification.

    Args:
        trace_log_path: Destination JSONL file.
        fragment: Source fragment for the ID + tier in the record.
        reasoning: Full reasoning text (un-truncated).
    """
    record = {
        "id": fragment.id,
        "tier": fragment.privacy_tier,
        "reasoning": reasoning,
    }
    try:
        with trace_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning(
            "[fragment=%s trace=%s] Could not append to classify-trace log: %s",
            fragment.id,
            trace_log_path,
            exc,
        )


def _read_fragment(md_file: Path) -> tuple[Fragment, str, dict[str, object]] | None:
    """Thin alias for :func:`creek.vault.reader.try_load_fragment`.

    Kept as a private name so existing patches in
    ``tests/test_classify_engine.py`` continue to target the engine
    module rather than the shared reader. The semantics are
    identical: ``None`` means "not a Creek fragment, silently skip",
    while real I/O failures propagate so the engine's outer
    ``except`` block can record them on
    :attr:`ClassifySummary.errors`.

    Args:
        md_file: Markdown file to load.

    Returns:
        ``(fragment, body, raw_metadata)`` for valid fragments, or
        ``None`` when the file is well-formed YAML but is **not** a
        Creek fragment.

    Raises:
        OSError: When the file cannot be opened.
        ValueError: When the YAML cannot be parsed.
        yaml.YAMLError: When the YAML parser rejects the document.
    """
    return try_load_fragment(md_file)


def _write_fragment(
    *,
    md_file: Path,
    fragment: Fragment,
    body: str,
    method: str,
    raw: dict[str, object],
    reasoning: str,
    provider: str | None = None,
) -> None:
    """Persist updated fragment metadata back to its file.

    Preserves the existing classification provenance keys so that
    ``classified_at`` reflects the most recent update and the prior
    ``method`` is replaced. The reasoning trace (FEAT-017) is stored
    in :data:`creek.classify.constants.CLASSIFICATION_REASONING_KEY`
    when non-empty, and removed from the frontmatter otherwise so an
    intimate-tier fragment never inherits a stale trace from a
    previous run at a different tier.

    Args:
        md_file: Destination file (rewritten in place).
        fragment: Updated fragment metadata.
        body: Markdown body to retain below the frontmatter.
        method: ``"rules"``, ``"llm"``, or ``"manual"``.
        raw: Original frontmatter dict — used to preserve any
            non-Fragment keys (e.g. operator-applied tags).
        reasoning: Per-FEAT-017 ``classification_reasoning`` value
            already truncated / tier-routed by :func:`_route_reasoning`.
            Empty string clears the field on disk.
        provider: The LLM provider behind an ``llm`` classification
            (e.g. ``anthropic`` / ``ollama``). Stamped as
            ``classification_provider`` for ``llm`` writes so a
            quality-aware re-run can tell local-LLM from cloud-LLM;
            cleared for ``rules`` / ``manual`` so no stale value lingers.
    """
    new_metadata = raw.copy()
    new_metadata.update(fragment.model_dump(mode="json"))
    new_metadata[CLASSIFICATION_METHOD_KEY] = method
    # BUG-002: route through the shared LA helper rather than calling
    # ``datetime.now(tz=LA_TZ)`` directly, so every classified_at
    # timestamp written to disk uses the same code path as every
    # other LA-anchored timestamp in the pipeline.
    new_metadata[CLASSIFIED_AT_KEY] = now_la().isoformat()
    if method == LLM_METHOD and provider:
        new_metadata[CLASSIFICATION_PROVIDER_KEY] = provider
    else:
        new_metadata.pop(CLASSIFICATION_PROVIDER_KEY, None)
    if reasoning:
        new_metadata[CLASSIFICATION_REASONING_KEY] = reasoning
    else:
        new_metadata.pop(CLASSIFICATION_REASONING_KEY, None)

    post = frontmatter.Post(content=body)
    post.metadata.update(new_metadata)
    md_file.write_text(frontmatter.dumps(post), encoding="utf-8")


def _describe_llm_unavailability(provider: str) -> str:
    """Build a remediation hint for :class:`LLMProviderUnavailableError`.

    The hint is provider-specific so a first-time user can act on it
    without scrolling back through orchestrator WARNING logs. Anthropic
    needs two env vars (API key + consent); Ollama needs the local
    daemon to be reachable.

    Args:
        provider: ``llm.provider`` from the loaded config.

    Returns:
        Human-readable sentence the CLI can render verbatim.
    """
    if provider == "anthropic":
        return (
            "set ANTHROPIC_API_KEY and CREEK_ANTHROPIC_CONSENT=1 to "
            "authorise sending fragment content to Anthropic's servers"
        )
    if provider == "ollama":
        return (
            "ensure the Ollama daemon is running and reachable at the "
            "URL configured under `llm.url` in creek_config.yaml"
        )
    return (
        "check the `llm.*` settings in creek_config.yaml and the "
        "provider-specific credentials / health-check requirements"
    )


def _record_llm_progress(progress_path: Path, fragment_id: str) -> None:
    """Append *fragment_id* to the per-vault LLM-progress checkpoint (OPS-001).

    The progress file is informational: the per-fragment frontmatter is
    the source of truth for "this was classified by the LLM". Failing
    to write the checkpoint is logged at ``WARNING`` and the
    classification proceeds — losing the line costs at most extra log
    output on the next run, never a re-classification.

    The parent directory is created up-front by :func:`run_classify`,
    so this writer never issues ``mkdir`` per fragment.

    Args:
        progress_path: Destination path under
            ``<vault>/00-Creek-Meta/Processing-Log/``.
        fragment_id: ID of the fragment just classified by the LLM.
    """
    try:
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": fragment_id}) + "\n")
    except OSError as exc:
        # OPS-003: keep the fragment-id prefix so this WARNING is
        # grep-able alongside other per-fragment failures. ``checkpoint=``
        # rather than ``path=`` because the file path here is the
        # progress checkpoint, not the fragment's source — the OPS-003
        # convention reserves ``path=`` for ``source.original_file``.
        logger.warning(
            "[fragment=%s checkpoint=%s] "
            "Could not append to llm-progress checkpoint: %s",
            fragment_id,
            progress_path,
            exc,
        )
