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
from creek.classify.classify_engine import build_tier_classifiers
from creek.classify.privacy_filter import tier_of
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

        Args:
            source_path: Directory to scan.
            result: Pipeline result (unused directly but kept for symmetry).

        Returns:
            Number of files scanned.

        Raises:
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

        matches = self.scanner.scan_directory(source_path)
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
            frag = self.rule_classifier.classify(item.fragment, content=item.body)
            if self._needs_llm(frag, item.body):
                result.residue += 1
                if not self.no_llm:
                    classifier = self.tier_classifiers.for_tier(tier_of(frag))
                    frag = classifier.classify(frag, content=item.body)
            else:
                result.deterministic_classified += 1
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
