"""Vault-driven classification engine for the ``creek classify`` command.

Loads every fragment from ``<vault>/01-Fragments/``, dispatches to the
configured classifier (rules or LLM), and rewrites each fragment file
in place with the updated frontmatter. Records the chosen ``method``
and ``classified_at`` timestamp on each fragment so that subsequent
``creek classify`` runs can preserve manual decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING

import frontmatter
import yaml
from pydantic import ValidationError

from creek.classify.constants import (
    CLASSIFICATION_METHOD_KEY,
    CLASSIFIED_AT_KEY,
    MANUAL_METHOD,
    RULES_METHOD,
)
from creek.classify.llm import LLMClassifier
from creek.classify.rules import RuleClassifier
from creek.ingest.base import LA_TZ
from creek.models import Fragment, Frequency

if TYPE_CHECKING:
    from creek.config import CreekConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClassifySummary:
    """Counts produced by a single ``creek classify`` run.

    Attributes:
        total: Number of Creek fragments visited (non-fragment markdown
            files in ``01-Fragments`` are silently skipped and not
            counted here).
        classified: Fragments whose frontmatter was updated. A
            ``--method llm`` run that short-circuits to the rule
            result still counts as classified — the work is real and
            the file is rewritten.
        preserved_manual: Fragments left unchanged because the operator
            previously stamped them ``method: manual`` and ``--force``
            was not passed.
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
    errors: list[str]


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
        return ClassifySummary(0, 0, 0, 0, [])

    rules = RuleClassifier()
    llm = LLMClassifier(config=config.llm) if method == "llm" else None
    counts = _RunCounts()

    for md_file in sorted(fragments_root.rglob("*.md")):
        _process_file(
            md_file=md_file,
            method=method,
            force=force,
            rules=rules,
            llm=llm,
            confidence_threshold=config.classification.confidence_threshold,
            counts=counts,
        )

    return ClassifySummary(
        total=counts.total,
        classified=counts.classified,
        preserved_manual=counts.preserved,
        skipped_high_confidence=counts.skipped,
        errors=counts.errors,
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
) -> None:
    """Classify a single fragment file and update ``counts`` in place.

    Args:
        md_file: The file to consider.
        method: ``"rules"`` or ``"llm"``.
        force: Whether to overwrite manual classifications.
        rules: Shared :class:`RuleClassifier` instance.
        llm: Shared :class:`LLMClassifier` (when ``method == "llm"``).
        confidence_threshold: Threshold below which the LLM is invoked.
        counts: Mutable per-run counters; mutated in place.
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

    if not force and raw.get(CLASSIFICATION_METHOD_KEY) == MANUAL_METHOD:
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
    """Load a fragment file's metadata, body, and raw frontmatter dict.

    Args:
        md_file: Markdown file to load.

    Returns:
        ``(fragment, body, raw_metadata)`` for valid fragments, or
        ``None`` when the file is well-formed YAML but is **not** a
        Creek fragment (no ``type: fragment`` key, or a schema
        mismatch). Real I/O failures are propagated to the caller so
        they can be recorded on :attr:`ClassifySummary.errors`.

    Raises:
        OSError: When the file cannot be opened.
        ValueError: When the YAML cannot be parsed.
        yaml.YAMLError: When the YAML parser rejects the document.
    """
    post = frontmatter.load(str(md_file))
    metadata = dict(post.metadata)
    if metadata.get("type") != "fragment":
        return None
    try:
        fragment = Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None
    return fragment, str(post.content), metadata


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
    new_metadata[CLASSIFIED_AT_KEY] = datetime.now(tz=LA_TZ).isoformat()

    post = frontmatter.Post(content=body, **new_metadata)
    md_file.write_text(frontmatter.dumps(post), encoding="utf-8")
