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
    CLASSIFIED_AT_KEY,
    LLM_METHOD,
    MANUAL_METHOD,
    RULES_METHOD,
)
from creek.classify.llm import LLMClassifier
from creek.classify.rules import RuleClassifier
from creek.models import Fragment, Frequency
from creek.time import now_la
from creek.vault.reader import try_load_fragment

LLM_PROGRESS_FILENAME = "llm-progress.jsonl"
"""Per-vault progress log filename written under ``00-Creek-Meta/Processing-Log/``.

Newline-delimited JSON (one ``{"id": ...}`` object per line); the
``.jsonl`` suffix signals the format honestly to an operator opening
the file.
"""

_RESUMABLE_METHODS: frozenset[str] = frozenset({MANUAL_METHOD, LLM_METHOD})

if TYPE_CHECKING:
    from creek.config import CreekConfig

logger = logging.getLogger(__name__)


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
        preserved_manual: Fragments left unchanged because a prior run
            already settled the classification and ``--force`` was not
            passed. Covers both ``classification_method: manual`` (the
            original operator-curated case) and ``classification_method:
            llm`` (OPS-001 resume — re-running ``--method llm`` after a
            crash skips fragments already classified by the LLM so the
            operator does not re-pay for tokens). The field name is
            kept for backwards compatibility; rename to ``preserved`` is
            tracked alongside the next CLI message refresh.
        skipped_high_confidence: Subset of ``classified`` for which
            the LLM was not invoked because the rule classifier
            produced a high-confidence answer. The fragment is still
            stamped ``classification_method: rules`` on disk.
        errors: Human-readable error messages (one per failure).
    """

    total: int
    classified: int
    preserved_manual: int
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
    preserved: int = 0
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
    """
    fragments_root = vault_path / "01-Fragments"
    if not fragments_root.exists():
        return ClassifySummary(0, 0, 0, 0, ())

    rules = RuleClassifier()
    llm = LLMClassifier(config=config.llm) if method == "llm" else None
    counts = _RunCounts()
    progress_path: Path | None = None
    if method == LLM_METHOD:
        progress_dir = vault_path / "00-Creek-Meta" / "Processing-Log"
        # Create the directory once, not per fragment — a 10k-fragment
        # vault would otherwise issue 10k redundant ``mkdir`` syscalls
        # in ``_record_llm_progress``.
        progress_dir.mkdir(parents=True, exist_ok=True)
        progress_path = progress_dir / LLM_PROGRESS_FILENAME

    for md_file in sorted(fragments_root.rglob("*.md")):
        _process_file(
            md_file=md_file,
            method=method,
            force=force,
            rules=rules,
            llm=llm,
            confidence_threshold=config.classification.confidence_threshold,
            counts=counts,
            progress_path=progress_path,
        )

    return ClassifySummary(
        total=counts.total,
        classified=counts.classified,
        preserved_manual=counts.preserved,
        skipped_high_confidence=counts.skipped,
        # Snapshot-by-tuple so the frozen dataclass is genuinely
        # immutable: the caller can't accidentally append to the
        # underlying list and reach into completed-run state.
        errors=tuple(counts.errors),
    )


def _process_file(
    *,
    md_file: Path,
    method: str,
    force: bool,
    rules: RuleClassifier,
    llm: LLMClassifier | None,
    confidence_threshold: float,
    counts: _RunCounts,
    progress_path: Path | None,
) -> None:
    """Classify a single fragment file and update ``counts`` in place.

    Args:
        md_file: The file to consider.
        method: ``"rules"`` or ``"llm"``.
        force: Whether to overwrite previously-classified fragments.
        rules: Shared :class:`RuleClassifier` instance.
        llm: Shared :class:`LLMClassifier` (when ``method == "llm"``).
        confidence_threshold: Threshold below which the LLM is invoked.
        counts: Mutable per-run counters; mutated in place.
        progress_path: Optional path to the LLM-progress checkpoint file
            (OPS-001). When set, the fragment ID is appended after a
            successful LLM classification. Manual / rules-shortcircuit
            paths are not appended — only paid LLM calls need to be
            recovered on resume.
    """
    try:
        record = _read_fragment(md_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        counts.errors.append(f"unreadable fragment {md_file}: {exc}")
        return
    if record is None:
        # File parsed cleanly but is not a Creek fragment (no
        # ``type: fragment`` field, or schema mismatch). Silently skip —
        # markdown notes coexist with fragments in the vault, and it
        # would be misleading to count them in ``total``.
        return
    # Only count files we actually identify as Creek fragments.
    counts.total += 1
    fragment, body, raw = record

    # Skip fragments whose previous run already settled the classification:
    # ``manual`` is operator-curated; ``llm`` is a paid LLM call we don't
    # want to repeat after a crash (OPS-001 resume contract). Pass
    # ``--force`` to re-classify everything.
    if not force and raw.get(CLASSIFICATION_METHOD_KEY) in _RESUMABLE_METHODS:
        counts.preserved += 1
        return

    new_fragment, was_skipped = _classify_one(
        fragment=fragment,
        body=body,
        method=method,
        rules=rules,
        llm=llm,
        confidence_threshold=confidence_threshold,
    )
    # When ``--method llm`` short-circuits because the rule classifier
    # already produced a confident answer, the provenance stamp must
    # reflect what actually classified the fragment ("rules"), not
    # the user's CLI choice ("llm"). Either way the fragment IS
    # persisted — we never skip the write, only the LLM call.
    write_method = RULES_METHOD if was_skipped else method

    try:
        _write_fragment(
            md_file=md_file,
            fragment=new_fragment,
            body=body,
            method=write_method,
            raw=raw,
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
) -> tuple[Fragment, bool]:
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
        Tuple of ``(updated_fragment, skipped)``. ``skipped`` is
        ``True`` when the LLM was not invoked because the rule
        classifier already produced a confident answer.
    """
    if method == "rules":
        return rules.classify(fragment, content=body), False

    rule_result = rules.classify(fragment, content=body)
    if rule_result.frequency.primary != Frequency.UNCLASSIFIED:
        confidence = rules.confidence_score(rule_result, content=body)
        if confidence >= confidence_threshold:
            return rule_result, True

    if llm is None:  # pragma: no cover  # no issue: defensive guard, unreachable
        msg = "LLM classifier required when method='llm'"
        raise RuntimeError(msg)
    return llm.classify(rule_result, content=body), False


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
) -> None:
    """Persist updated fragment metadata back to its file.

    Preserves the existing classification provenance keys so that
    ``classified_at`` reflects the most recent update and the prior
    ``method`` is replaced.

    Args:
        md_file: Destination file (rewritten in place).
        fragment: Updated fragment metadata.
        body: Markdown body to retain below the frontmatter.
        method: ``"rules"``, ``"llm"``, or ``"manual"``.
        raw: Original frontmatter dict — used to preserve any
            non-Fragment keys (e.g. operator-applied tags).
    """
    new_metadata = dict(raw)
    new_metadata.update(fragment.model_dump(mode="json"))
    new_metadata[CLASSIFICATION_METHOD_KEY] = method
    # BUG-002: route through the shared LA helper rather than calling
    # ``datetime.now(tz=LA_TZ)`` directly, so every classified_at
    # timestamp written to disk uses the same code path as every
    # other LA-anchored timestamp in the pipeline.
    new_metadata[CLASSIFIED_AT_KEY] = now_la().isoformat()

    post = frontmatter.Post(content=body, **new_metadata)
    md_file.write_text(frontmatter.dumps(post), encoding="utf-8")


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
