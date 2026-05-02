"""Vault-driven classification engine for the ``creek classify`` command.

Loads every fragment from ``<vault>/01-Fragments/``, dispatches to the
configured classifier (rules or LLM), and rewrites each fragment file
in place with the updated frontmatter. Records the chosen ``method``
and ``classified_at`` timestamp on each fragment so that subsequent
``creek classify`` runs can preserve manual decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path  # noqa: TC003 — runtime use in dataclass field
from typing import TYPE_CHECKING, Final

import frontmatter
import yaml
from pydantic import ValidationError

from creek.classify.llm import LLMClassifier
from creek.classify.rules import RuleClassifier
from creek.ingest.base import LA_TZ
from creek.models import Fragment, Frequency

if TYPE_CHECKING:
    from creek.config import CreekConfig

logger = logging.getLogger(__name__)

_CLASSIFICATION_METHOD_KEY: Final[str] = "classification_method"
"""Frontmatter key carrying ``rules | llm | manual``."""

_CLASSIFIED_AT_KEY: Final[str] = "classified_at"
"""Frontmatter key carrying the ISO-8601 classification timestamp."""

_MANUAL_METHOD: Final[str] = "manual"


@dataclass(frozen=True)
class ClassifySummary:
    """Counts produced by a single ``creek classify`` run.

    Attributes:
        total: Total fragment files visited.
        classified: Fragments whose frontmatter was updated.
        preserved_manual: Fragments left unchanged because the operator
            previously stamped them ``method: manual`` and ``--force``
            was not passed.
        skipped_high_confidence: Fragments the LLM was not asked about
            because the rules classifier already produced a confident
            answer.
        errors: Human-readable error messages (one per failure).
    """

    total: int
    classified: int
    preserved_manual: int
    skipped_high_confidence: int
    errors: list[str]


def run_classify(
    *,
    vault_path: Path,
    config: CreekConfig,
    method: str,
    batch_size: int,
    force: bool,
) -> ClassifySummary:
    """Classify every fragment in *vault_path* using the chosen method.

    Args:
        vault_path: Vault root.
        config: Loaded Creek configuration.
        method: ``"rules"`` or ``"llm"``.
        batch_size: Reserved; the current LLM classifier processes
            fragments individually because the per-vault scan is
            already streaming.
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

    total = 0
    classified = 0
    preserved = 0
    skipped = 0
    errors: list[str] = []

    for md_file in sorted(fragments_root.rglob("*.md")):
        total += 1
        record = _read_fragment(md_file)
        if record is None:
            errors.append(f"unreadable fragment: {md_file}")
            continue
        fragment, body, raw = record

        if not force and raw.get(_CLASSIFICATION_METHOD_KEY) == _MANUAL_METHOD:
            preserved += 1
            continue

        new_fragment, was_skipped = _classify_one(
            fragment=fragment,
            body=body,
            method=method,
            rules=rules,
            llm=llm,
            confidence_threshold=config.classification.confidence_threshold,
        )
        if was_skipped:
            skipped += 1
            continue

        try:
            _write_fragment(
                md_file=md_file,
                fragment=new_fragment,
                body=body,
                method=method,
                raw=raw,
            )
        except OSError as exc:
            errors.append(f"failed to update {md_file}: {exc}")
            continue
        classified += 1

    return ClassifySummary(
        total=total,
        classified=classified,
        preserved_manual=preserved,
        skipped_high_confidence=skipped,
        errors=errors,
    )


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

    if llm is None:  # pragma: no cover — guarded by ``method == "llm"``
        msg = "LLM classifier required when method='llm'"
        raise RuntimeError(msg)
    return llm.classify(rule_result, content=body), False


def _read_fragment(md_file: Path) -> tuple[Fragment, str, dict[str, object]] | None:
    """Load a fragment file's metadata, body, and raw frontmatter dict.

    Args:
        md_file: Markdown file to load.

    Returns:
        ``(fragment, body, raw_metadata)`` or ``None`` when the file is
        not a valid fragment record.
    """
    try:
        post = frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown file: %s", md_file)
        return None
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
    new_metadata[_CLASSIFICATION_METHOD_KEY] = method
    new_metadata[_CLASSIFIED_AT_KEY] = datetime.now(tz=LA_TZ).isoformat()

    post = frontmatter.Post(content=body, **new_metadata)
    md_file.write_text(frontmatter.dumps(post), encoding="utf-8")
