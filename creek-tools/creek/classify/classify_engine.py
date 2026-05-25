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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.classify.constants import (
    CLASSIFICATION_METHOD_KEY,
    CLASSIFICATION_REASONING_KEY,
    CLASSIFICATION_REASONING_MAX_CHARS,
    CLASSIFIED_AT_KEY,
    CLASSIFY_TRACE_LOG_FILENAME,
    LLM_METHOD,
    MANUAL_METHOD,
    RULES_METHOD,
)
from creek.classify.llm import LLMClassifier
from creek.classify.reatomize import (
    ClassificationTree,
    Classifier,
    ReatomizeConfig,
    classify_reatomize,
)
from creek.classify.rules import RuleClassifier
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

if TYPE_CHECKING:
    from collections.abc import Iterator

    from creek.config import ClassificationConfig, CreekConfig

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
    """

    total: int
    classified: int
    preserved_manual: int
    preserved_llm: int
    skipped_high_confidence: int
    errors: tuple[str, ...]


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
    errors: list[str] = field(default_factory=list)


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
    llm = LLMClassifier(config=config.llm) if method == "llm" else None
    if llm is not None and not llm.available:
        # Refuse to iterate when the provider is unreachable so the
        # vault is never stamped with ``classification_method: llm``
        # for fragments the LLM never actually saw. The orchestrator
        # already logged the provider-specific reason (missing API
        # key, missing consent env var, Ollama down) as a WARNING;
        # we capture it here for the operator-facing message so the
        # remediation hint is not buried in stderr.
        raise LLMProviderUnavailableError(
            provider=config.llm.provider,
            detail=_describe_llm_unavailability(config.llm.provider),
        )
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

    for md_file in sorted(fragments_root.rglob("*.md")):
        _process_file(
            md_file=md_file,
            method=method,
            force=force,
            rules=rules,
            llm=llm,
            classification_config=config.classification,
            counts=counts,
            progress_path=progress_path,
            trace_log_path=trace_log_path,
            vault_writer=vault_writer,
        )

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
    )


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


def _process_file(
    *,
    md_file: Path,
    method: str,
    force: bool,
    rules: RuleClassifier,
    llm: LLMClassifier | None,
    classification_config: ClassificationConfig,
    counts: _RunCounts,
    progress_path: Path | None,
    trace_log_path: Path | None,
    vault_writer: VaultWriter | None,
) -> None:
    """Classify a single fragment file and update ``counts`` in place.

    Args:
        md_file: The file to consider.
        method: ``"rules"`` or ``"llm"``.
        force: Whether to overwrite previously-classified fragments.
        rules: Shared :class:`RuleClassifier` instance.
        llm: Shared :class:`LLMClassifier` (when ``method == "llm"``).
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
    """
    record = _load_classifiable_fragment(
        md_file=md_file,
        counts=counts,
    )
    if record is None:
        return
    fragment, body, raw = record

    # Skip fragments whose previous run already settled the classification.
    # Tracked on distinct counters so the CLI summary can label "manual
    # preserved" and "previously LLM-classified preserved" honestly
    # (issue #321).
    if not force and _record_if_preserved(raw, counts):
        return

    new_fragment, was_skipped, reasoning = _classify_one(
        fragment=fragment,
        body=body,
        method=method,
        rules=rules,
        llm=llm,
        confidence_threshold=classification_config.confidence_threshold,
    )

    # FEAT-023 wire-up (issue #318): when ``classification.reatomize`` is
    # True we run the orchestrator on the root fragment and persist any
    # newly-derived child fragments. The root's frontmatter still flows
    # through the existing write path below so the per-run provenance
    # stamps (method, classified_at, reasoning) stay consistent with the
    # non-reatomize code path.
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
    applied by :func:`_record_if_preserved` in :func:`_process_file`
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
        )
    except OSError as exc:
        counts.errors.append(f"failed to update {md_file}: {exc}")
        return
    counts.classified += 1
    if was_skipped:
        counts.skipped += 1
    elif progress_path is not None and write_method == LLM_METHOD:
        _record_llm_progress(progress_path, new_fragment.id)


def _classify_one(
    *,
    fragment: Fragment,
    body: str,
    method: str,
    rules: RuleClassifier,
    llm: LLMClassifier | None,
    confidence_threshold: float,
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
    llm_result = llm.classify_with_reasoning(rule_result, content=body)
    return llm_result.fragment, False, llm_result.reasoning


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
    _persist_reatomized_children(
        tree=tree,
        root_id=root_fragment.id,
        vault_writer=vault_writer,
        counts=counts,
    )


def _persist_reatomized_children(
    *,
    tree: ClassificationTree,
    root_id: str,
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
        vault_writer: Shared :class:`VaultWriter` instance.
        counts: Mutable per-run counters; write failures are recorded
            on :attr:`_RunCounts.errors`.
    """
    for node in _walk_tree(tree):
        child_fragment = node.fragment.fragment
        if child_fragment.id == root_id:
            continue
        try:
            vault_writer.write_fragment(child_fragment, body=node.fragment.body)
        except (OSError, KeyError) as exc:
            counts.errors.append(
                f"failed to write reatomized child {child_fragment.id}: {exc}",
            )


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
    """
    new_metadata = raw.copy()
    new_metadata.update(fragment.model_dump(mode="json"))
    new_metadata[CLASSIFICATION_METHOD_KEY] = method
    # BUG-002: route through the shared LA helper rather than calling
    # ``datetime.now(tz=LA_TZ)`` directly, so every classified_at
    # timestamp written to disk uses the same code path as every
    # other LA-anchored timestamp in the pipeline.
    new_metadata[CLASSIFIED_AT_KEY] = now_la().isoformat()
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
