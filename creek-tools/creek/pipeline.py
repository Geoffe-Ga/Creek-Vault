"""Pipeline orchestrator -- wire all Creek processing stages end-to-end.

The :class:`Pipeline` class initialises every subsystem (redaction, ingestion,
classification, linking, indexing) and executes them in sequence against a
source directory, producing a :class:`PipelineResult` with aggregate counts.
"""

import logging
from pathlib import Path

from pydantic import BaseModel

from creek.classify.llm import LLMClassifier
from creek.classify.review import ReviewQueueGenerator
from creek.classify.rules import RuleClassifier
from creek.config import CreekConfig
from creek.generate.indexes import IndexGenerator
from creek.ingest import INGESTOR_REGISTRY
from creek.link.linker import LinkingPipeline
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.redact.scanner import RedactionScanner

logger = logging.getLogger(__name__)


class PipelineResult(BaseModel):
    """Aggregate counts from a full pipeline run.

    Attributes:
        files_scanned: Number of source files scanned for sensitive data.
        fragments_created: Number of fragments produced by ingestion.
        classifications_made: Number of fragments classified.
        links_found: Total link count across all linking stages.
        indexes_generated: Number of index notes generated.
    """

    files_scanned: int = 0
    fragments_created: int = 0
    classifications_made: int = 0
    links_found: int = 0
    indexes_generated: int = 0


class Pipeline:
    """Orchestrate the full Creek processing pipeline.

    Wires redaction, ingestion, classification, linking, and indexing
    stages together.  Handles gracefully the case where no ingestor is
    registered for a given source type (the INGESTOR_REGISTRY may be
    empty during the skeleton phase).

    Attributes:
        config: The Creek configuration governing all subsystems.
        scanner: The redaction scanner for PII detection.
        rule_classifier: Keyword-based classifier.
        llm_classifier: LLM-based classifier stub.
        review_generator: Review queue generator for uncertain fragments.
        linking_pipeline: Orchestrator for all four linking stages.
    """

    def __init__(self, config: CreekConfig) -> None:
        """Initialise the pipeline and all subsystem components.

        Args:
            config: Top-level Creek configuration.
        """
        self.config = config
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

        Stages:
            1. Redaction scan on source files
            2. Ingestion (discover ingestors for source type)
            3. Classification (rule -> LLM -> review queue)
            4. Linking (embeddings, temporal, threads, eddies)
            5. Index generation

        Args:
            source_path: Directory containing source files to process.
            vault_path: Obsidian vault root to write results into.

        Returns:
            A :class:`PipelineResult` with aggregate counts.
        """
        result = PipelineResult()

        # Stage 1: Redaction scan
        files_scanned = self._run_redaction(source_path, result)
        result.files_scanned = files_scanned

        # Stage 2: Ingestion
        fragments = self._run_ingestion(source_path, result)
        result.fragments_created = len(fragments)

        # Stage 3: Classification
        classified = self._run_classification(fragments, vault_path, result)
        result.classifications_made = len(classified)

        # Stage 4: Linking
        link_total = self._run_linking(classified, vault_path, result)
        result.links_found = link_total

        # Stage 5: Indexing
        index_count = self._run_indexing(vault_path, result)
        result.indexes_generated = index_count

        logger.info("Pipeline complete: %s", result)
        return result

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
    ) -> list[Fragment]:
        """Discover and run ingestors for available source types.

        If the INGESTOR_REGISTRY is empty (skeleton phase), logs a
        warning and returns an empty list.

        Args:
            source_path: Directory containing source files.
            result: Pipeline result (unused directly but kept for symmetry).

        Returns:
            List of Fragment models created by ingestion.
        """
        if not INGESTOR_REGISTRY:
            logger.warning(
                "No ingestors registered -- ingestion stage skipped. "
                "Register concrete ingestors in creek.ingest.INGESTOR_REGISTRY."
            )
            return []

        fragments: list[Fragment] = []
        for name, ingestor_cls in INGESTOR_REGISTRY.items():
            logger.info("Running ingestor: %s", name)
            ingestor = ingestor_cls()
            ingest_result = ingestor.ingest(source_path)
            for parsed in ingest_result.fragments:
                fragment = Fragment(
                    title=parsed.source_path,
                    source=FragmentSource(platform=SourcePlatform.OTHER),
                )
                fragments.append(fragment)

        return fragments

    def _run_classification(
        self,
        fragments: list[Fragment],
        vault_path: Path,
        result: PipelineResult,
    ) -> list[Fragment]:
        """Classify fragments through rules, LLM, and review queue.

        Args:
            fragments: Fragments to classify.
            vault_path: Vault path for writing review queue.
            result: Pipeline result (unused directly but kept for symmetry).

        Returns:
            List of classified Fragment models.
        """
        if not fragments:
            logger.info("No fragments to classify.")
            return []

        classified: list[Fragment] = []
        for fragment in fragments:
            frag = self.rule_classifier.classify(fragment)
            frag = self.llm_classifier.classify(frag)
            classified.append(frag)

        self.review_generator.generate_queue(classified, vault_path)
        return classified

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
