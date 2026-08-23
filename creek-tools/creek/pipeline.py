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
from typing import Final

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
from creek.ingest.base import (
    IngestedFragment,
    ParsedFragment,
    assemble_ingested_fragment,
)
from creek.ingest.ledger import LedgerRecord
from creek.ingest.pipeline import record_source_provenance, run_ingestor
from creek.ingest.routing import SKIPPED_DIRECTORY_NAMES, Arbitration, arbitrate
from creek.link.link_engine import LinkSummary, run_link
from creek.models import Fragment, Frequency
from creek.redact.scanner import RedactionScanner
from creek.vault.writer import VaultWriter

logger = logging.getLogger(__name__)

_UNCLAIMED_LOG_SAMPLE: Final[int] = 10
"""How many unclaimed source paths to name in the warning log line.

The full list is always on :attr:`PipelineResult.unclaimed_sources`; the
log only samples it, so a source tree with thousands of unreadable files
cannot flood the console.
"""

_PROCESS_LINK_METHODS: Final[tuple[str, ...]] = ("eddies", "threads")
"""Linker stages ``creek process`` runs, in order — and why only these two.

**Why not all four.** ``creek link``'s four methods do not all persist
anything. ``temporal`` computes proximity links and returns a count with
no write of any kind; ``embeddings`` persists only the *vector* parquet —
the resonance edges it counts are discarded, there is no ``Resonance``
writer anywhere in the codebase, and :class:`~creek.models.Fragment` has
no ``resonances`` field. Running them here would reproduce the very
overclaim #1303 is about, and ``find_resonances`` is additionally the
O(n^2) hotspot (#857/#858) that issue declares out of scope. The vector
cache they would have warmed is written by the eddies pass anyway.

**Why two calls and not one shared load.** The tuple is ordered and each
entry is a *separate* :func:`~creek.link.link_engine.run_link` call
precisely because ``run_link`` re-reads fragments from disk every time.
``_persist_fragment_link_updates`` rewrites frontmatter from the
in-memory model's full dump, so it overwrites *every* Fragment-owned key
— including the link list the other stage owns. A design that loaded the
corpus once and fed both detectors would have the second stage write
``eddies: []`` over the wiki-links the first stage had just persisted,
and it would still pass a page-existence assertion. The per-call reload
is the correctness boundary, guarded by the both-lists assertion in
``tests/e2e/test_full_pipeline_linking.py``.

Deliberately *not* ``creek.surface_modes.LINK_METHODS``: that tuple is
the ``creek link`` surface and must keep all four.
"""


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
    ordered list of human-readable step strings), which is all
    ``creek sync --tier A --dry-run`` prints. A bare ``creek sync --tier A``
    does not use this function at all: #678 shipped real execution, and the
    equivalent steps are run for real by
    :func:`~creek.cli._sync_run_tier_a`. Keep the two in step — this list is
    what the operator is shown when they ask what the tier *would* do.
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


def _is_authored_content(path: Path, source_path: Path) -> bool:
    """Decide whether *path* is a file a user meant to have ingested.

    The exclusion contract behind
    :attr:`PipelineResult.unclaimed_sources`, kept deliberately narrow
    so the report stays trustworthy: directories are not files, and
    anything beneath a directory named in
    :data:`~creek.ingest.routing.SKIPPED_DIRECTORY_NAMES` or beneath a
    dot-directory is machinery. The test is on the *parent* directories
    only — a dotfile the user put in the source root is still their
    file, and gets reported.

    Args:
        path: Candidate path, anywhere under *source_path*.
        source_path: Root of the scanned source tree.

    Returns:
        ``True`` when *path* is a file worth reporting on.
    """
    if not path.is_file():
        return False
    return not any(
        part in SKIPPED_DIRECTORY_NAMES or part.startswith(".")
        for part in path.relative_to(source_path).parts[:-1]
    )


def _unclaimed_files(source_path: Path, seen: set[str]) -> list[str]:
    """List source files that no ingestor produced a fragment from.

    Args:
        source_path: Root of the scanned source tree.
        seen: Source paths every ingestor produced output for, taken
            before arbitration so a losing claimant still counts as
            having seen the file.

    Returns:
        Sorted absolute paths, excluding machinery per
        :func:`_is_authored_content`.
    """
    return [
        str(path)
        for path in sorted(source_path.rglob("*"))
        if str(path) not in seen and _is_authored_content(path, source_path)
    ]


class PipelineResult(BaseModel):
    """Aggregate counts from a full pipeline run.

    Attributes:
        files_scanned: Number of source files scanned for sensitive data.
        fragments_created: Number of fragments produced by ingestion.
        classifications_made: Number of fragments classified.
        links_found: Link artefacts this run **persisted to the vault**:
            eddy pages written under ``03-Eddies/`` plus thread pages
            written under ``02-Threads/{Active,Dormant,Resolved}/``.
            Sourced from ``eddies_written``/``threads_written``, never
            from ``*_detected``, so a cluster the writer failed on is not
            reported as a success. It deliberately excludes resonance and
            temporal edges: ``creek process`` does not compute them, and
            nothing in this codebase can persist them (#1303). Before
            #1303 this field summed four in-memory counts and the whole
            graph was then discarded, so a large number meant nothing.
        link_summaries: Per-stage :class:`LinkSummary` objects, one per
            entry in :data:`_PROCESS_LINK_METHODS`, in execution order.
            Empty when the run had no fragments to link.
        indexes_generated: Number of index notes generated.
        deterministic_classified: Fragments confidently resolved by Pass 1
            (rules + ``human_review_sources`` short-circuit). Reported on
            every run so the audit report (FEAT-006) can graph the
            deterministic-pass yield over time.
        local_model_processed: Pass-2 local-model work done by this run —
            in practice the embedding vectors the sentence-transformer
            actually computed. Fragments served from the embeddings
            parquet cache are *not* counted, so a re-run over an
            unchanged corpus honestly reports near-zero local-model work
            rather than re-billing the whole vault (#1303). Nothing adds
            an OCR count here today, despite Pass 2 nominally covering
            OCR; the field's older docstring claimed otherwise. The
            ``--no-llm`` flag does not change this number.
        residue: Fragments the rule classifier left uncertain — i.e.
            would have been routed to the LLM if Pass 3 were enabled.
            Always reported, even on normal runs that did dispatch the
            residue.
        errors: Human-readable error messages collected from each stage.
            Each entry is prefixed with its source ingestor name so the
            user can trace the failure back to the offending file.
        contested_sources: Absolute paths of source files that more than
            one ingestor produced fragments for. One ingestor's output
            was kept (see :mod:`creek.ingest.routing`); the rest was
            discarded before it reached the vault. Reported because a
            vault ingested before #1304 already holds the discarded
            ingestor's fragments and nothing removes them.
        unclaimed_sources: Absolute paths of source files that produced
            no fragments at all, under the exclusion contract in
            :meth:`Pipeline._record_source_coverage`. Surfaces a
            pre-existing silent drop rather than a new one.
    """

    files_scanned: int = 0
    fragments_created: int = 0
    classifications_made: int = 0
    links_found: int = 0
    link_summaries: list[LinkSummary] = Field(default_factory=list)
    indexes_generated: int = 0
    deterministic_classified: int = 0
    local_model_processed: int = 0
    residue: int = 0
    errors: list[str] = Field(default_factory=list)
    contested_sources: list[str] = Field(default_factory=list)
    unclaimed_sources: list[str] = Field(default_factory=list)


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
            5. Linking (eddies then threads, each persisted to the vault)
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
        ingested = self._run_ingestion(source_path, result, vault_path)
        result.fragments_created = len(ingested)

        # Stage 3: Classification
        classified = self._run_classification(ingested, vault_path, result)
        result.classifications_made = len(classified)

        # Stage 4: Vault write -- persist classified fragments+bodies.
        self._write_to_vault(classified, vault_path, result)

        # Stage 5: Linking
        fragments_only = [item.fragment for item in classified]
        summaries = self._run_linking(fragments_only, vault_path, result)
        result.link_summaries = summaries
        # ``*_written``, never ``*_detected``: a cluster the writer could
        # not materialise must not be reported as a persisted artefact.
        result.links_found = sum(
            summary.eddies_written + summary.threads_written for summary in summaries
        )

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

        The returned bool no longer carries every outcome, and that is
        the point. An I/O failure reading the consent log raises rather
        than returning ``False``, so fail-closed behaviour is now
        enforced *by the type*: a destroyed or unreadable record can no
        longer reach :meth:`run`'s ``if not has_consent:`` branch (skip
        ingestion, still index) and masquerade there as a deliberate
        operator refusal. The run aborts instead.

        One residual ambiguity is honest and remains: a *corrupt*
        (unparsable) log is quarantined rather than raised on, so it
        legitimately still yields ``False`` and still takes that
        branch. What distinguishes "the record was destroyed" from
        "consent was never granted" in that single case is out-of-band
        — the WARNING the quarantine emits, which names both the
        original and the quarantine path, plus the
        ``consent-log.json.corrupt-<timestamp>-<rand>`` artifact left
        on disk for inspection.

        Args:
            source_path: The source directory to check consent for.

        Returns:
            ``True`` if consent exists or no consent_manager is set;
            ``False`` when consent was never recorded, or when the log
            was corrupt and has been quarantined.

        Raises:
            ConsentLogUnavailableError: When the consent log cannot be
                read — an unreadable file, an unreadable parent
                directory, or a directory sitting at the log path. This
                aborts the run instead of being downgraded to a bool.
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
        self, source_path: Path, result: PipelineResult, vault_path: Path
    ) -> list[IngestedFragment]:
        """Run every ingestor, then keep one ingestor's output per source file.

        Each entry in :data:`INGESTOR_REGISTRY` still sees the whole
        source tree, because several ingestors identify their input by
        sniffing its structure or content rather than by its extension —
        a Discord export is a directory shape, a ChatGPT export is a
        particular JSON envelope — and no dispatch table can express
        that. What changed in issue #1304 is what happens next: the
        collected fragments are arbitrated by
        :func:`creek.ingest.routing.arbitrate`, so a file claimed by two
        ingestors is written to the vault once, by the higher-priority
        one, instead of twice.

        Arbitration is on **written output**, not on parsing: contested
        text files are still parsed by every claimant, and the losers'
        fragments are discarded before assembly. That ordering is
        deliberate — an ingestor that discovered a file but failed to
        parse it forfeits, so a malformed spreadsheet still reaches the
        vault as text instead of vanishing. See
        :mod:`creek.ingest.routing` for the priority order and the
        reasoning behind each contested extension.

        The steps:

        1. Call ``ingest()`` on every registered ingestor.
        2. Forward **every** ingestor's errors onto
           :attr:`PipelineResult.errors`, prefixed with its registry key
           — including the losers', so arbitration never suppresses a
           failure report.
        3. Arbitrate, recording the contested paths on
           :attr:`PipelineResult.contested_sources` and any file no
           ingestor claimed on :attr:`PipelineResult.unclaimed_sources`.
        4. Assemble each surviving fragment into an
           :class:`IngestedFragment` carrying a deterministic ``frag-``
           ID and the converted body.
        5. Treat per-fragment assembly failures as recoverable: the
           failure is recorded on ``result.errors`` and skipped, rather
           than aborting the whole pipeline.

        Step 4 also runs :func:`creek.ingest.pipeline.record_source_provenance`
        (#1575). ``creek process`` assembles here rather than through
        ``run_ingest``, so it does not pass through
        ``establish_source_identity`` — without this call it stayed the one
        CLI surface still writing the operator's absolute host path into
        ``source.original_file``, which is exactly the disclosure #1575
        closed everywhere else.

        Args:
            source_path: Directory containing source files.
            result: Pipeline result; mutated to collect error messages,
                contested paths and unclaimed paths.
            vault_path: Vault root, used to derive the recorded source key.
                Taken as an argument rather than off ``self.config`` because
                :meth:`run` is the authority on where this run writes.

        Returns:
            List of :class:`IngestedFragment` ready for classification
            and vault writing, with duplicates already removed — so
            ``len()`` of it is an honest fragment count.
        """
        if not INGESTOR_REGISTRY:
            logger.warning(
                "No ingestors registered -- ingestion stage skipped. "
                "Register concrete ingestors in creek.ingest.INGESTOR_REGISTRY."
            )
            return []

        claims: dict[str, list[ParsedFragment]] = {}
        for name, ingestor_cls in INGESTOR_REGISTRY.items():
            logger.info("Running ingestor: %s", name)
            # ``self.config.ocr`` rather than nothing: ``creek process``
            # reaches the image ingestor through this loop, so leaving it
            # un-threaded would honour ``ocr.enabled`` on ``creek ingest``
            # and ignore it here — half a fix, and the half that is silent
            # (#1517).
            ingest_result = run_ingestor(ingestor_cls, source_path, ocr=self.config.ocr)
            for err in ingest_result.errors:
                result.errors.append(f"[{name}] {err}")
            claims[name] = ingest_result.fragments.copy()

        arbitration = arbitrate(claims)
        self._record_source_coverage(source_path, claims, arbitration, result)

        ingested: list[IngestedFragment] = []
        for name, fragments in arbitration.winners.items():
            for parsed in fragments:
                try:
                    assembled = assemble_ingested_fragment(parsed)
                    record_source_provenance(parsed, assembled.fragment, vault_path)
                    ingested.append(assembled)
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

    @staticmethod
    def _record_source_coverage(
        source_path: Path,
        claims: dict[str, list[ParsedFragment]],
        arbitration: Arbitration,
        result: PipelineResult,
    ) -> None:
        """Record which source files were contested and which went unclaimed.

        Both lists are diagnostics, not control flow — nothing is
        skipped, deleted or retried on the strength of them.

        *Contested* paths are the migration story for issue #1304. A
        vault ingested before that change already holds the losing
        ingestor's fragment, and nothing here removes it or ever
        re-derives it: fragment ids hash source path, timestamp and
        content, and the two ingestors disagree on the latter two, so
        the survivor resolves to its own pre-existing id and the loser's
        file is simply left behind. Naming the contested paths is what
        makes those strays findable
        (``creek purge source --source-path <path> --dry-run``).

        *Unclaimed* paths are files no ingestor produced anything from.
        The exclusion contract, deliberately narrow: directories are not
        files; anything under a directory named in
        :data:`~creek.ingest.routing.SKIPPED_DIRECTORY_NAMES` or under a
        dot-directory is machinery rather than authored content. Note
        this reports a pre-existing gap rather than a new one — a plain
        ``.json`` that is not a chat export has always yielded nothing,
        because the sniffers reject it and ``GenericIngestor`` excludes
        the extension.

        Args:
            source_path: Root of the scanned source tree.
            claims: Every ingestor's fragments *before* arbitration — a
                loser still proves the file was seen.
            arbitration: The resolved claims.
            result: Pipeline result; mutated in place.
        """
        result.contested_sources = list(arbitration.contested)
        if arbitration.contested:
            logger.warning(
                "%d source file(s) were claimed by more than one ingestor; "
                "one ingestor's output was kept for each. A vault ingested "
                "before issue #1304 may still hold the other's fragments as "
                "strays -- they are not removed automatically.",
                len(arbitration.contested),
            )

        if not source_path.is_dir():
            return
        seen = {
            fragment.source_path
            for fragments in claims.values()
            for fragment in fragments
        }
        unclaimed = _unclaimed_files(source_path, seen)
        result.unclaimed_sources = unclaimed
        if unclaimed:
            logger.warning(
                "%d source file(s) produced no fragments -- no ingestor "
                "claimed them: %s",
                len(unclaimed),
                ", ".join(unclaimed[:_UNCLAIMED_LOG_SAMPLE]),
            )

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
            # ``extra_frontmatter`` is carried through, not rebuilt: this bundle
            # is a re-wrap of ``item`` around a classified ``Fragment``, and the
            # passthrough keys are provenance the ingestor already resolved
            # (#1392). Dropping them here silently emptied the dict the write
            # stage forwards, so ``creek process`` wrote a workbook fragment
            # with no ``sheet``/``rows``/``columns`` while ``creek ingest``
            # wrote all three for the same file. Classification never touches
            # them, so copying is the whole of the contract.
            classified.append(
                IngestedFragment(
                    fragment=frag,
                    body=item.body,
                    extra_frontmatter=item.extra_frontmatter,
                ),
            )

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
                # Forwarded so `creek process` writes the same frontmatter
                # `creek ingest` does for the same source (#1392); the
                # allowlist upstream decides which keys exist at all.
                writer.write_fragment(
                    item.fragment,
                    body=item.body,
                    extra_frontmatter=item.extra_frontmatter,
                )
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
    ) -> list[LinkSummary]:
        """Run — and persist — the link stage over the vault.

        Delegates to :func:`~creek.link.link_engine.run_link`, the same
        entry point ``creek link`` uses, once per entry in
        :data:`_PROCESS_LINK_METHODS`. Before #1303 this stage ran a
        second, count-only orchestrator that wrote nothing at all.

        **Scope is the whole vault, not just this run's fragments.**
        ``run_link`` re-reads ``01-Fragments/`` itself, so a freshly
        ingested note can join an eddy of notes ingested months ago —
        which is the only way membership-derived cluster ids stay
        coherent. The *fragments* argument is therefore a trigger, not an
        input set: an empty list short-circuits so a run that ingested
        nothing never re-clusters the corpus. The accepted cost is one
        DBSCAN pass plus one union-find pass per ``creek process``,
        i.e. exactly ``creek link --method eddies`` followed by
        ``--method threads``, with no ``find_resonances``.

        Pass-2 accounting is now honest: ``local_model_processed`` is
        bumped by the vectors the model actually computed, not by the
        fragment count. The second ``run_link`` call reads the parquet
        the first one wrote, so it contributes ``0`` and there is no
        double count.

        Args:
            fragments: Classified fragments from this run. Used only to
                decide whether there is anything to link.
            vault_path: Vault root to link over and write into.
            result: Pipeline result; mutated to record
                ``local_model_processed``.

        Returns:
            One :class:`LinkSummary` per method, in execution order, or
            an empty list when there was nothing to link.
        """
        if not fragments:
            logger.info("No fragments to link.")
            return []

        summaries: list[LinkSummary] = []
        for method in _PROCESS_LINK_METHODS:
            summary = run_link(
                vault_path=vault_path,
                config=self.config,
                method=method,
                rebuild=False,
            )
            result.local_model_processed += summary.fragments_embedded
            summaries.append(summary)
        return summaries

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
