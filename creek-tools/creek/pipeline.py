"""Pipeline orchestrator -- wire all Creek processing stages end-to-end.

The :class:`Pipeline` class initialises every subsystem (redaction, ingestion,
classification, linking, indexing) and executes them in sequence against a
source directory, producing a :class:`PipelineResult` with aggregate counts.

Processing is gated on user consent via :class:`~creek.consent.ConsentManager`:
if consent has not been recorded for a source, the pipeline skips ingestion.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from creek.classify.llm import LLMClassifier
from creek.classify.review import ReviewQueueGenerator
from creek.classify.rules import RuleClassifier
from creek.config import CreekConfig
from creek.consent import ConsentManager
from creek.generate.indexes import IndexGenerator
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.base import IngestedFragment, assemble_ingested_fragment
from creek.link.linker import LinkingPipeline
from creek.models import Fragment
from creek.redact.scanner import RedactionScanner
from creek.vault.writer import VaultWriter

logger = logging.getLogger(__name__)


class PipelineResult(BaseModel):
    """Aggregate counts from a full pipeline run.

    Attributes:
        files_scanned: Number of source files scanned for sensitive data.
        fragments_created: Number of fragments produced by ingestion.
        classifications_made: Number of fragments classified.
        links_found: Total link count across all linking stages.
        indexes_generated: Number of index notes generated.
        errors: Human-readable error messages collected from each stage.
            Each entry is prefixed with its source ingestor name so the
            user can trace the failure back to the offending file.
    """

    files_scanned: int = 0
    fragments_created: int = 0
    classifications_made: int = 0
    links_found: int = 0
    indexes_generated: int = 0
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
        llm_classifier: LLM-based classifier stub.
        review_generator: Review queue generator for uncertain fragments.
        linking_pipeline: Orchestrator for all four linking stages.
        consent_manager: Optional consent manager for gating processing.
    """

    def __init__(
        self,
        config: CreekConfig,
        consent_manager: ConsentManager | None = None,
    ) -> None:
        """Initialise the pipeline and all subsystem components.

        Args:
            config: Top-level Creek configuration.
            consent_manager: Optional consent manager. When provided,
                ``run()`` checks for prior consent before ingesting.
        """
        self.config = config
        self.consent_manager = consent_manager
        self.scanner = RedactionScanner(config=config.redaction)
        self.rule_classifier = RuleClassifier()
        self.llm_classifier = LLMClassifier(config=config.llm)
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

        logger.info("Pipeline complete: %s", result)
        return result

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
        """Scan source files for sensitive data.

        Args:
            source_path: Directory to scan.
            result: Pipeline result (unused directly but kept for symmetry).

        Returns:
            Number of files scanned.
        """
        if not source_path.exists():
            logger.warning("Source path does not exist: %s", source_path)
            return 0

        files = list(source_path.rglob("*"))
        file_count = sum(1 for f in files if f.is_file())

        if self.config.redaction.enabled:
            matches = self.scanner.scan_directory(source_path)
            if matches:
                logger.info(
                    "Redaction scan found %d potential PII match(es)",
                    len(matches),
                )

        return file_count

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
        """Classify fragments through rules, LLM, and review queue.

        Classification operates on the inner :class:`Fragment`; the body
        is preserved unchanged on the returned :class:`IngestedFragment`.

        Args:
            ingested: Ingested fragment+body bundles to classify.
            vault_path: Vault path for writing the review queue.
            result: Pipeline result (unused; kept for symmetry).

        Returns:
            List of classified :class:`IngestedFragment` items.
        """
        if not ingested:
            logger.info("No fragments to classify.")
            return []

        classified: list[IngestedFragment] = []
        for item in ingested:
            frag = self.rule_classifier.classify(item.fragment)
            frag = self.llm_classifier.classify(frag)
            classified.append(IngestedFragment(fragment=frag, body=item.body))

        self.review_generator.generate_queue(
            [item.fragment for item in classified],
            vault_path,
        )
        return classified

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

        Args:
            fragments: Classified fragments to link.
            vault_path: Vault path for linking output.
            result: Pipeline result (unused directly but kept for symmetry).

        Returns:
            Total link count across all linking stages.
        """
        if not fragments:
            logger.info("No fragments to link.")
            return 0

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
        index_gen = IndexGenerator(vault_path=vault_path)
        generated = index_gen.generate_all()
        return len(generated)
