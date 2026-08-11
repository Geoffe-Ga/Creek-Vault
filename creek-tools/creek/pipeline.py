"""Pipeline orchestrator -- wire all Creek processing stages end-to-end.

The :class:`Pipeline` class initialises every subsystem (redaction, ingestion,
classification, linking, indexing) and executes them in sequence against a
source directory, producing a :class:`PipelineResult` with aggregate counts.

Processing is gated on user consent via :class:`~creek.consent.ConsentManager`:
if consent has not been recorded for a source, the pipeline skips ingestion.

The redaction stage is **fail-loud**: when ``redaction.enabled`` is true and
the scanner finds unresolved sensitive content, the pipeline raises
:class:`RedactionRequiredError` and refuses to ingest. The user must run
``creek redact --apply`` first. This trades a small ergonomic cost for the
guarantee that ``creek process`` can never silently leak secrets into the
vault.

The pipeline runs in three named passes (FEAT-005):

* **Pass 1 (deterministic, local):** redaction scan, ingestion, rules-based
  classification, frontmatter generation. No network egress.
* **Pass 2 (local model-based):** embeddings, OCR, future Whisper. Local
  model inference; still no network egress.
* **Pass 3 (network if opted in):** LLM classification of residue. Skipped
  entirely when ``no_llm`` is set, in which case the privacy claim
  "network egress only in Pass 3" is enforced by construction.
"""

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from creek.audit.yield_summary import (
    PreLLMYieldSummary,
    format_yield_line,
    write_yield_summary,
)
from creek.classify.audience import AudienceClassifier
from creek.classify.classify_engine import build_tier_classifiers
from creek.classify.praxis_pass import apply_praxis
from creek.classify.privacy import PrivacyClassifier
from creek.classify.privacy_filter import tier_of
from creek.classify.privacy_pass import PRIVACY_TIER_KEY, apply_tier, reassess
from creek.classify.review import ReviewQueueGenerator
from creek.classify.rules import RuleClassifier
from creek.config import CreekConfig
from creek.consent import ConsentManager
from creek.generate.indexes import IndexGenerator
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.base import IngestedFragment, assemble_ingested_fragment
from creek.ingest.ledger import LedgerRecord
from creek.link.linker import LinkingPipeline
from creek.models import Fragment, Frequency
from creek.redact.scanner import RedactionScanner
from creek.vault.writer import VaultWriter

logger = logging.getLogger(__name__)


# Map a ``creek sync`` source name -> the ingestor type it routes to.
_SYNC_INGEST_TYPE: dict[str, str] = {
    "journal": "markdown",
    "gdrive": "gdrive",
    "discord": "discord",
    "chatgpt": "chatgpt",
    "claude": "claude",
    "essays": "substack",
}


def sync_ingest_type(source: str) -> str:
    """Return the ingestor type a ``creek sync`` source routes to (#676)."""
    return _SYNC_INGEST_TYPE.get(source, source)


def resolve_tier_a_plan(source: str) -> list[str]:
    """Return the ordered Tier-A subcommand plan for *source* (#676).

    Tier A is the cheap, per-source pass: pull the source, incrementally
    ingest it, then run the offline rules classifier. This is a *plan* (an
    ordered list of human-readable step strings) — the skeleton command echoes
    it; real execution lands in a later issue.
    """
    ingest_type = sync_ingest_type(source)
    return [
        "pull",
        f"ingest --type {ingest_type} --since <ledger>",
        "classify --method rules",
    ]


def resolve_tier_b_plan() -> list[str]:
    """Return the ordered Tier-B subcommand plan (#676).

    Tier B is the nightly, global pass: LLM classification then the expensive
    link + index rebuilds. Returned as an ordered list of step strings.
    """
    return ["classify --method llm", "link", "index"]


def _as_aware(value: datetime) -> datetime:
    """Return *value* as an aware datetime, assuming UTC when naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def unit_is_changed(
    timestamp: datetime,
    content_hash: str,
    record: LedgerRecord | None,
    since: datetime | None,
) -> bool:
    """Return whether a source unit should be (re)processed by incremental ingest.

    Two cursors, used by the two incremental modes (#677):

    * ``since`` given (``creek ingest --since``): a unit is changed when its
      timestamp is strictly newer than the cutoff (mtime-style, generalising
      the Drive mtime-skip to every file source). Naive timestamps are treated
      as UTC.
    * ``since`` is ``None`` (``--incremental``, ledger-driven): a unit is
      changed when the ledger has no record for it, or its recorded
      ``content_hash`` differs from *content_hash* — so a touch-without-edit
      (same hash) is still skipped.

    Args:
        timestamp: The source unit's timestamp (fragment mtime/authored time).
        content_hash: SHA-256 of the unit's current content.
        record: The prior ledger record for this unit, or ``None``.
        since: Explicit cutoff datetime, or ``None`` for ledger-driven mode.

    Returns:
        ``True`` when the unit should be processed; ``False`` to skip it.
    """
    if since is not None:
        return _as_aware(timestamp) > _as_aware(since)
    if record is not None:
        return record.content_hash != content_hash
    return True


class RedactionRequiredError(RuntimeError):
    """Raised when ``creek process`` finds unresolved redaction matches.

    The pipeline refuses to ingest until the user runs
    ``creek redact --apply --source <path>`` (or accepts the matches by
    setting ``redaction.dry_run: true``). Carrying the match count lets
    the CLI render an exact remediation message.

    Attributes:
        match_count: Number of unresolved redaction matches found.
        source_path: The source directory that was scanned.
    """

    def __init__(self, match_count: int, source_path: Path) -> None:
        """Build a fail-loud redaction error with a remediation hint.

        Args:
            match_count: Number of unresolved sensitive matches.
            source_path: Source directory the scanner walked.
        """
        self.match_count = match_count
        self.source_path = source_path
        super().__init__(
            f"Redaction scan found {match_count} unresolved match(es) in "
            f"{source_path}. Run `creek redact --apply --source "
            f"{source_path}` (or set redaction.dry_run: true to skip "
            "this gate) before re-running `creek process`."
        )


class SymlinkEscapedSourceError(RuntimeError):
    """Raised when the redaction scan declined to read part of the source tree.

    The read tools skip an escaping symlink; ``creek process`` refuses over
    it, and the asymmetry is the point (#1087).

    This was once the *only* containment check standing between an
    out-of-tree file and the vault, because the ingestor walks had none of
    their own. Since #1294 they do:
    :func:`creek._containment.assert_source_contained` gates
    ``Ingestor.ingest`` for every registered ingestor. This refusal is kept
    all the same, and is not redundant. It fires earlier — before any
    ingestor runs — and it carries the count of files the *scan* declined,
    which is the number an operator needs to distinguish one stale link
    from a whole unscanned subtree. Skipping here would still turn a
    blocked ingest into a silent partial one, with the gate that used to
    stop it reporting success.

    Attributes:
        skipped_count: Number of files the scan declined to read.
        source_path: The source directory that was scanned.
    """

    def __init__(self, skipped_count: int, source_path: Path) -> None:
        """Build a path-traversal refusal that names the offending tree.

        Args:
            skipped_count: Number of symlinked files the scan declined.
            source_path: Source directory the scanner walked.
        """
        self.skipped_count = skipped_count
        self.source_path = source_path
        super().__init__(
            f"Redaction scan declined to read {skipped_count} file(s) under "
            f"{source_path}: each is a symlink whose target resolves outside "
            "that tree, so the scanner could not check content that "
            "ingestion would still have read. Remove or re-point the "
            f"offending link(s) under {source_path} before re-running "
            "`creek process`."
        )


class PipelineResult(BaseModel):
    """Aggregate counts from a full pipeline run.

    Attributes:
        files_scanned: Number of source files scanned for sensitive data.
        fragments_created: Number of fragments produced by ingestion.
        classifications_made: Number of fragments classified.
        links_found: Total link count across all linking stages.
        indexes_generated: Number of index notes generated.
        deterministic_classified: Fragments confidently resolved by Pass 1
            (rules + ``human_review_sources`` short-circuit). Reported on
            every run so the audit report (FEAT-006) can graph the
            deterministic-pass yield over time.
        local_model_processed: Fragments that traversed Pass 2 (embeddings
            / OCR). Equals the count handed to the linking pipeline; the
            ``--no-llm`` flag does not change this number.
        residue: Fragments the rule classifier left uncertain — i.e.
            would have been routed to the LLM if Pass 3 were enabled.
            Always reported, even on normal runs that did dispatch the
            residue.
        errors: Human-readable error messages collected from each stage.
            Each entry is prefixed with its source ingestor name so the
            user can trace the failure back to the offending file.
    """

    files_scanned: int = 0
    fragments_created: int = 0
    classifications_made: int = 0
    links_found: int = 0
    indexes_generated: int = 0
    deterministic_classified: int = 0
    local_model_processed: int = 0
    residue: int = 0
    errors: list[str] = Field(default_factory=list)


class Pipeline:
    """Orchestrate the full Creek processing pipeline.

    Wires redaction, ingestion, classification, linking, and indexing
    stages together.  Handles gracefully the case where no ingestor is
    registered for a given source type (the INGESTOR_REGISTRY may be
    empty during the skeleton phase).

    Processing is gated on user consent: if a ``consent_manager`` is
    provided and consent has not been recorded for the source, the
    pipeline skips ingestion and downstream stages.

    Attributes:
        config: The Creek configuration governing all subsystems.
        scanner: The redaction scanner for PII detection.
        rule_classifier: Keyword-based classifier.
        privacy_classifier: Assigns each fragment's privacy tier before the
            per-tier router reads it (#876).
        audience_classifier: Stamps the #634 audience axis on every fragment
            this pipeline classifies (#937), because the classify engine is
            not the only writer of that axis.
        tier_classifiers: Per-tier LLM classifiers (#666/#706) — the fragment's
            privacy tier selects the provider so Intimate stays local.
        review_generator: Review queue generator for uncertain fragments.
        linking_pipeline: Orchestrator for all four linking stages.
        consent_manager: Optional consent manager for gating processing.
    """

    def __init__(
        self,
        config: CreekConfig,
        consent_manager: ConsentManager | None = None,
        *,
        no_llm: bool = False,
    ) -> None:
        """Initialise the pipeline and all subsystem components.

        Args:
            config: Top-level Creek configuration.
            consent_manager: Optional consent manager. When provided,
                ``run()`` checks for prior consent before ingesting.
            no_llm: When ``True``, skip Pass 3 (LLM classification)
                entirely. The deterministic and local-model passes still
                run; the run summary records ``no_llm: true`` and the
                pipeline never invokes the LLM classifier —
                guaranteeing zero network egress along the LLM path.
                The flag wins over ``LLMConfig.provider`` so passing
                ``no_llm=True`` with ``provider: anthropic`` is safe.
        """
        self.config = config
        self.consent_manager = consent_manager
        self.no_llm = no_llm
        self.scanner = RedactionScanner(config=config.redaction)
        self.rule_classifier = RuleClassifier()
        # Issue #876: a freshly-ingested fragment is always ``unclassified``,
        # so without this the per-tier router below saw "not INTIMATE" for
        # every fragment and shipped journal entries to the cloud.
        self.privacy_classifier = PrivacyClassifier()
        # Issue #937: the classify engine is not the only writer of the #634
        # audience axis, and ``creek process`` never calls it — so this entry
        # point needs its own instance or every fragment it ingests keeps the
        # model default. Built bare, exactly as the engine builds it: the rules
        # path needs no provider, which keeps ``process --no-llm`` egress-free.
        self.audience_classifier = AudienceClassifier()
        # Per-tier routing (#666/#706): each fragment is classified by the
        # provider its privacy tier resolves to — Intimate stays local even when
        # ``classification`` is cloud. Building here is safe with any config:
        # build_tier_classifiers defers IntimateRoutingError (it never raises at
        # construction), so a rules-only or all-cloud ``process`` run is fine.
        self.tier_classifiers = build_tier_classifiers(config)
        self.review_generator = ReviewQueueGenerator(config=config.classification)
        self.linking_pipeline = LinkingPipeline(
            config=config.embeddings,
            linking_config=config.linking,
        )

    def run(self, source_path: Path, vault_path: Path) -> PipelineResult:
        """Execute the full pipeline from source to vault.

        If a ``consent_manager`` is configured and no prior consent exists
        for the source, ingestion and all downstream stages are skipped.
        Redaction scanning and index generation still run regardless.

        Stages:
            0. Consent check (if consent_manager configured)
            1. Redaction scan on source files
            2. Ingestion (discover ingestors for source type)
            3. Classification (rule -> LLM -> review queue)
            4. Vault write (persist classified fragments + bodies)
            5. Linking (embeddings, temporal, threads, eddies)
            6. Index generation

        Args:
            source_path: Directory containing source files to process.
            vault_path: Obsidian vault root to write results into.

        Returns:
            A :class:`PipelineResult` with aggregate counts.
        """
        result = PipelineResult()

        # Stage 0: Consent check
        has_consent = self._check_consent(source_path)

        # Stage 1: Redaction scan
        files_scanned = self._run_redaction(source_path, result)
        result.files_scanned = files_scanned

        if not has_consent:
            logger.warning(
                "Consent not granted for %s — skipping ingestion.",
                source_path,
            )
            # Still run indexing on existing vault content
            index_count = self._run_indexing(vault_path, result)
            result.indexes_generated = index_count
            return result

        # Stage 2: Ingestion
        ingested = self._run_ingestion(source_path, result)
        result.fragments_created = len(ingested)

        # Stage 3: Classification
        classified = self._run_classification(ingested, vault_path, result)
        result.classifications_made = len(classified)

        # Stage 4: Vault write -- persist classified fragments+bodies.
        self._write_to_vault(classified, vault_path, result)

        # Stage 5: Linking
        fragments_only = [item.fragment for item in classified]
        link_total = self._run_linking(fragments_only, vault_path, result)
        result.links_found = link_total

        # Stage 6: Indexing
        index_count = self._run_indexing(vault_path, result)
        result.indexes_generated = index_count

        # Stage 7: Pre-LLM yield summary (FEAT-005). Always emit — even on
        # consent-skipped runs the audit report wants a row, so callers
        # writing dashboards do not silently lose the bookkeeping.
        self._emit_yield_summary(vault_path, result)

        logger.info("Pipeline complete: %s", result)
        return result

    def _emit_yield_summary(
        self,
        vault_path: Path,
        result: PipelineResult,
    ) -> None:
        """Persist a one-line yield summary to ``run-summary.jsonl``.

        Failure to write the summary must NOT abort the pipeline — the
        audit substrate is observability, not a precondition for the
        run. We log the exception and move on. The line itself is also
        logged at INFO so operators reading stdout still see the yield.

        Args:
            vault_path: Vault root the run wrote into.
            result: Populated pipeline result with yield counts.
        """
        summary = PreLLMYieldSummary(
            run_id=uuid.uuid4().hex,
            deterministic_classified=result.deterministic_classified,
            local_model_processed=result.local_model_processed,
            residue=result.residue,
            no_llm=self.no_llm,
        )
        try:
            write_yield_summary(vault_path=vault_path, summary=summary)
        except OSError:
            logger.exception(
                "Failed to write pre-LLM yield summary under %s",
                vault_path,
            )
        logger.info(format_yield_line(summary))

    def _check_consent(self, source_path: Path) -> bool:
        """Check if consent has been granted for the given source.

        If no consent_manager is configured, consent is assumed.

        Args:
            source_path: The source directory to check consent for.

        Returns:
            ``True`` if consent exists or no consent_manager is set.
        """
        if self.consent_manager is None:
            return True

        return self.consent_manager.check_consent(
            source_type="pipeline",
            source_path=str(source_path),
        )

    def _run_redaction(self, source_path: Path, result: PipelineResult) -> int:
        """Scan source files for sensitive data and abort if any are found.

        When ``redaction.enabled`` is true and the scanner reports any
        unresolved matches, this method raises
        :class:`RedactionRequiredError` rather than ingest sensitive
        content into the vault. Set ``redaction.dry_run: true`` to keep
        the scan informational (matches are logged but the pipeline
        proceeds); the documented remediation is to run
        ``creek redact --apply`` first.

        A source tree holding a symlink whose target escapes it is refused
        outright via :class:`SymlinkEscapedSourceError` — before the
        ``dry_run`` arm, because a path-traversal guard admits no waiver and
        the scan came back clean only because it declined to look. Setting
        ``redaction.enabled: false`` still bypasses this whole stage, symlink
        check included — but no longer bypasses containment: since #1294 the
        ingestors refuse such a tree themselves, so the posture no longer
        depends on a config toggle. What ``redaction.enabled: false`` now
        costs is the PII scan, not the path-traversal guard.

        Args:
            source_path: Directory to scan.
            result: Pipeline result (unused directly but kept for symmetry).

        Returns:
            Number of files scanned.

        Raises:
            SymlinkEscapedSourceError: When the scan declined to read any
                file because its symlink target escapes *source_path*.
            RedactionRequiredError: When matches are found and
                ``redaction.dry_run`` is false.
        """
        if not source_path.exists():
            logger.warning("Source path does not exist: %s", source_path)
            return 0

        files = list(source_path.rglob("*"))
        file_count = sum(1 for f in files if f.is_file())

        if not self.config.redaction.enabled:
            return file_count

        summary = self.scanner.scan_batch(source_path)

        if summary.files_skipped_symlink:
            logger.error(
                "Redaction scan declined to read %d file(s) in %s whose "
                "symlink targets escape it; refusing to ingest past an "
                "unscanned file.",
                summary.files_skipped_symlink,
                source_path,
            )
            raise SymlinkEscapedSourceError(
                summary.files_skipped_symlink,
                source_path,
            )

        matches = summary.matches
        if not matches:
            return file_count

        if self.config.redaction.dry_run:
            logger.warning(
                "Redaction scan found %d potential PII match(es) "
                "(dry_run=true; pipeline will continue without applying)",
                len(matches),
            )
            return file_count

        logger.error(
            "Redaction scan found %d unresolved match(es) in %s; "
            "refusing to ingest until they are applied.",
            len(matches),
            source_path,
        )
        raise RedactionRequiredError(len(matches), source_path)

    def _run_ingestion(
        self, source_path: Path, result: PipelineResult
    ) -> list[IngestedFragment]:
        """Discover and run ingestors, returning structured fragment+body bundles.

        For each ingestor in :data:`INGESTOR_REGISTRY`, this method:

        1. Calls ``ingest()`` to produce parsed fragments and any errors.
        2. Forwards each error onto :attr:`PipelineResult.errors`,
           prefixed with the ingestor's registry key for traceability.
        3. Assembles each parsed fragment into an :class:`IngestedFragment`
           carrying a deterministic ``frag-`` ID and the converted body.
        4. Treats per-fragment assembly failures as recoverable: the
           failure is recorded on ``result.errors`` and skipped, rather
           than aborting the whole pipeline.

        Args:
            source_path: Directory containing source files.
            result: Pipeline result; mutated to collect error messages.

        Returns:
            List of :class:`IngestedFragment` ready for classification
            and vault writing.
        """
        if not INGESTOR_REGISTRY:
            logger.warning(
                "No ingestors registered -- ingestion stage skipped. "
                "Register concrete ingestors in creek.ingest.INGESTOR_REGISTRY."
            )
            return []

        ingested: list[IngestedFragment] = []
        for name, ingestor_cls in INGESTOR_REGISTRY.items():
            logger.info("Running ingestor: %s", name)
            ingestor = ingestor_cls()
            ingest_result = ingestor.ingest(source_path)
            for err in ingest_result.errors:
                result.errors.append(f"[{name}] {err}")
            for parsed in ingest_result.fragments:
                try:
                    ingested.append(assemble_ingested_fragment(parsed))
                except (KeyError, ValueError) as exc:
                    # Surface contract / validation failures as user-visible
                    # errors instead of silently dropping the fragment.
                    result.errors.append(
                        f"[{name}] failed to assemble fragment from "
                        f"{parsed.source_path}: {exc}",
                    )
                    logger.exception(
                        "Failed to assemble fragment from %s",
                        parsed.source_path,
                    )

        return ingested

    def _run_classification(
        self,
        ingested: list[IngestedFragment],
        vault_path: Path,
        result: PipelineResult,
    ) -> list[IngestedFragment]:
        """Classify fragments through rules, optional LLM, and review queue.

        Runs the rule-based classifier first, then escalates to the LLM
        only for fragments that the rules left below
        ``ClassificationConfig.confidence_threshold`` or with an
        unclassified primary frequency. Fragments whose source platform
        is in ``human_review_sources`` are never sent to the LLM — they
        always go to the review queue for a human decision.

        When ``self.no_llm`` is true, the LLM dispatch is skipped
        regardless of confidence. The fragment keeps whatever the rule
        classifier gave it, and the residue counter still increments so
        the run summary records what *would* have been routed to Pass 3.

        Every fragment is stamped with a real privacy tier between the
        rules pass and the LLM dispatch (#876), because the per-tier
        router reads that tier to keep Intimate content off cloud
        providers and a freshly-ingested fragment carries none.

        Every fragment is also stamped with the #634 audience axis once the
        tier is known (#937). ``creek process`` never calls the classify
        engine, so without this every one-shot-processed fragment keeps
        ``audience: mixed`` — which the voice fingerprint reads as neutral
        (``_audience_factor``), so the user's audience-facing writing never
        earns its weight and their private writing is never discounted. The
        pass runs after the tier because the score reads it, and it is
        deterministic and idempotent, so re-processing never churns the axis.

        Every fragment is likewise scored for praxis potential once the
        classifiers are done (#877). ``creek process`` never calls the
        classify engine, so without this the one-shot command would leave
        every fragment it ingests at ``praxis_potential: none`` and keep
        ``04-Praxis`` / ``08-Decisions`` unreachable for anyone who uses
        it. The pass is escalate-only, so running it after the optional
        LLM dispatch cannot undo a ``latent`` verdict the model produced.

        Every fragment then gets one more privacy look, and that look is
        the last mutation before the write (#974). The pre-pass above has
        to precede the LLM for the router's sake, so it cannot see
        ``voice_register`` / ``confidence``, and self-authored +
        confessional + conviction is ``classify_tier``'s third INTIMATE
        trigger (``creek/classify/privacy.py:141``). Without this second
        look ``creek process`` stamped ``personal`` +
        ``voice_proxy_eligible: true`` on content ``creek classify``
        stamps ``intimate`` + ``false`` — so which command the user
        happened to run decided whether intimate content stayed
        voice-proxy eligible. Running it last is safe because it is
        escalate-only (:func:`~creek.classify.privacy_pass.escalate`): it
        can never lower a tier, so it can never regress the tier→router
        ordering above.

        That second look is unconditional, because ``reassess`` carries
        its own gate (#1105): it acts only when *this run's*
        classification made the privacy heuristic **strictly more
        restrictive** than it was on the fragment as ingested. So a tier
        already on record — which ``apply_tier`` correctly declines to
        overwrite when ``force`` is ``False`` — is disturbed only by
        evidence that did not exist when it was set, never by a re-run
        over the same input. The predicate replaces an ``owns_tier``
        guard that asked "did this frontmatter still owe us a tier?";
        that proxy answered ``False`` for every already-tiered fragment,
        so it discarded exactly the confessional verdicts the second look
        exists to catch.

        The ``baseline`` handed to ``reassess`` is computed on
        ``item.fragment`` *before* the rule classifier runs, mirroring the
        engine, which computes it on the fragment as loaded from disk
        before any of its own classification
        (``creek.classify.classify_engine._prepare_fragment``). Both
        therefore mean "the heuristic's verdict on untouched input", and
        the two entry points stay gated identically — which is the whole
        point of an issue about them disagreeing. Taking it *after* the
        rules pass here would make a rules-derived confessional register
        count as new evidence on this path but not on the engine's, and
        re-open the divergence in a subtler place.

        Classification operates on the inner :class:`Fragment`; the body
        is preserved unchanged on the returned :class:`IngestedFragment`.

        Args:
            ingested: Ingested fragment+body bundles to classify.
            vault_path: Vault path for writing the review queue.
            result: Pipeline result; mutated to track per-pass yield
                counts (``deterministic_classified`` / ``residue``).

        Returns:
            List of classified :class:`IngestedFragment` items.
        """
        if not ingested:
            logger.info("No fragments to classify.")
            return []

        classified: list[IngestedFragment] = []
        for item in ingested:
            # Issue #1105: the heuristic's verdict on the fragment as ingested,
            # taken before ANY of this run's classification touches the axes it
            # reads. The reassess below compares against it to tell new evidence
            # from an echo of what was already there. Mirrors the point at which
            # the classify engine takes its own baseline.
            baseline = self.privacy_classifier.classify_tier(
                item.fragment,
                content=item.body,
            )
            frag = self.rule_classifier.classify(item.fragment, content=item.body)
            # Issue #876: assign a real privacy tier BEFORE ``for_tier`` picks
            # a provider, or an Intimate fragment routes to the cloud on the
            # very run meant to classify it (Intimate-never-cloud, ADR-0003).
            # There is no frontmatter on disk yet, so the fragment's own tier
            # is the "raw" state a manual override would have to come through.
            raw: dict[str, object] = {PRIVACY_TIER_KEY: frag.privacy_tier}
            frag = apply_tier(
                frag,
                item.body,
                raw=raw,
                force=False,
                classifier=self.privacy_classifier,
            )
            if self._needs_llm(frag, item.body):
                result.residue += 1
                if not self.no_llm:
                    classifier = self.tier_classifiers.for_tier(tier_of(frag))
                    frag = classifier.classify(frag, content=item.body)
            else:
                result.deterministic_classified += 1
            # Issue #937: stamp the #634 audience axis here, after ``apply_tier``
            # above — the score reads ``privacy_tier``, and a fragment still
            # ``unclassified`` contributes nothing, so an earlier call would
            # return a different verdict than ``creek classify`` does. This
            # placement mirrors the engine's (classify -> audience -> praxis)
            # so the two entry points agree fragment for fragment.
            frag = self.audience_classifier.classify_and_enforce(frag, item.body)
            # Issue #877: score the praxis axis after the optional LLM dispatch
            # above, so the escalate-only merge sees whatever the model
            # produced, and before the reassess below, so the #876 privacy
            # pass stays the last mutation before the write (mirrors the
            # engine's ordering).
            frag = apply_praxis(frag, item.body)
            # Issue #974: the tier pass above had to run before the LLM, so it
            # never saw the voice axes Pass 3 just filled in. Look once more,
            # last and escalate-only, exactly where the engine looks. Issue
            # #1105: unconditional — ``reassess`` compares against ``baseline``
            # itself and declines unless this run made the heuristic stricter,
            # so a tier already on record moves only on new evidence.
            frag = reassess(
                frag,
                item.body,
                baseline=baseline,
                classifier=self.privacy_classifier,
            )
            classified.append(IngestedFragment(fragment=frag, body=item.body))

        self.review_generator.generate_queue(
            [item.fragment for item in classified],
            vault_path,
        )
        return classified

    def _needs_llm(self, fragment: Fragment, content: str) -> bool:
        """Return ``True`` when the rule classifier left this fragment uncertain.

        A fragment is sent to the LLM when:

        * its source platform is not in
          :attr:`ClassificationConfig.human_review_sources` (those go
          straight to the review queue without LLM assistance), and
        * the rule classifier left its primary frequency unclassified
          *or* its rule-derived confidence sits below
          :attr:`ClassificationConfig.confidence_threshold`.

        Fragments whose source platform is in ``auto_classify_sources``
        and whose rules already produced a confident answer are skipped,
        which is the cost-saving fast path for cleanly-classified bulk
        imports.

        Args:
            fragment: Fragment after the rule-classifier pass.
            content: Markdown body the rule classifier scored.

        Returns:
            ``True`` if :class:`LLMClassifier` should be invoked.
        """
        classification = self.config.classification

        if fragment.source.platform in classification.human_review_sources:
            return False

        if fragment.frequency.primary == Frequency.UNCLASSIFIED:
            return True

        confidence = self.rule_classifier.confidence_score(
            fragment,
            content=content,
        )
        return confidence < classification.confidence_threshold

    def _write_to_vault(
        self,
        classified: list[IngestedFragment],
        vault_path: Path,
        result: PipelineResult,
    ) -> None:
        """Persist classified fragments to the vault, body and all.

        Each fragment is written via :class:`VaultWriter`, which routes
        to the correct ``01-Fragments/<subfolder>/`` directory based on
        the fragment's source platform. Idempotency is enforced by the
        writer's existing ID-based duplicate detection: re-running the
        pipeline against the same source produces zero new files.

        Failures are appended to :attr:`PipelineResult.errors` so the
        user can see which fragments were not persisted.

        Args:
            classified: Classified fragment+body bundles.
            vault_path: Root of the Obsidian vault to write into.
            result: Pipeline result; mutated to collect error messages.
        """
        if not classified:
            return

        try:
            writer = VaultWriter(vault_path=vault_path)
        except FileNotFoundError as exc:
            result.errors.append(f"[vault-writer] {exc}")
            logger.exception("Vault writer initialisation failed")
            return

        for item in classified:
            try:
                writer.write_fragment(item.fragment, body=item.body)
            except (OSError, KeyError) as exc:
                # OSError covers filesystem-level failures (permissions,
                # disk full). KeyError covers a missing platform mapping —
                # ``_PLATFORM_SUBFOLDER`` is enforced as total by a unit
                # test, but if a future enum value slips through review
                # the writer's bare ``dict[]`` access would otherwise
                # crash the whole run. Record either as a per-fragment
                # error and keep processing the rest.
                result.errors.append(
                    f"[vault-writer] failed to write {item.fragment.id}: {exc}",
                )
                logger.exception(
                    "Failed to write fragment %s to vault",
                    item.fragment.id,
                )

    def _run_linking(
        self,
        fragments: list[Fragment],
        vault_path: Path,
        result: PipelineResult,
    ) -> int:
        """Run the linking pipeline on classified fragments.

        Treated as Pass 2 work for yield-summary purposes: every fragment
        handed to the linker exercises local-model inference (sentence
        transformers for embeddings, plus the deterministic temporal /
        thread / eddy detectors that operate on those embeddings).
        :attr:`PipelineResult.local_model_processed` is bumped here so
        the audit report can attribute Pass-2 work back to the run.

        Args:
            fragments: Classified fragments to link.
            vault_path: Vault path for linking output.
            result: Pipeline result; mutated to record
                ``local_model_processed``.

        Returns:
            Total link count across all linking stages.
        """
        if not fragments:
            logger.info("No fragments to link.")
            return 0

        result.local_model_processed += len(fragments)
        link_result = self.linking_pipeline.run(fragments, vault_path)
        return (
            link_result.resonance_count
            + link_result.temporal_count
            + link_result.thread_count
            + link_result.eddy_count
        )

    def _run_indexing(self, vault_path: Path, result: PipelineResult) -> int:
        """Generate Dataview index notes in the vault.

        Args:
            vault_path: Vault root for index generation.
            result: Pipeline result (unused directly but kept for symmetry).

        Returns:
            Number of index files generated.
        """
        generated = IndexGenerator(vault_path=vault_path).generate_all()
        return len(generated)
