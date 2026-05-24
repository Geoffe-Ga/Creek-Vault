"""Concurrent batch dispatch for the LLM classifier.

Holds the :class:`BatchStats` aggregator plus the thread-pool driver
that :meth:`creek.classify.llm.LLMClassifier.classify_batch` delegates
to. Keeping the concurrency loop out of the orchestrator class lets
each module stay focused on one concern.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from tqdm import tqdm

from creek.models import Fragment, Frequency

if TYPE_CHECKING:
    from creek.config import LLMConfig

logger = logging.getLogger(__name__)


@dataclass
class BatchStats:
    """Aggregated statistics for a batch classification run.

    Attributes:
        total: Total number of fragments in the batch.
        classified: Fragments successfully classified.
        failed: Fragments that failed after all retries.
    """

    total: int = 0
    classified: int = 0
    failed: int = 0


class _BatchClassifier(Protocol):
    """Minimal subset of :class:`LLMClassifier` used by the batch driver."""

    config: LLMConfig
    RATE_LIMIT_DELAY: float

    def classify(self, fragment: Fragment, content: str = "") -> Fragment:
        """Classify one fragment; provider-agnostic."""


def run_batch(
    classifier: _BatchClassifier,
    fragments: list[Fragment],
    *,
    progress: bool,
) -> list[Fragment]:
    """Drive a concurrent batch classification through ``classifier``.

    Submits one ``classifier.classify`` task per fragment to a thread
    pool sized by ``classifier.config.max_concurrent``, paces
    submissions by ``classifier.RATE_LIMIT_DELAY`` to avoid bursting
    the provider, and aggregates per-fragment outcomes into a
    :class:`BatchStats` summary logged on completion.

    Args:
        classifier: Object satisfying :class:`_BatchClassifier`.
        fragments: Fragments to classify. The returned list preserves
            the input order even though tasks complete out of order.
        progress: Whether to wrap the completion stream in a
            :class:`tqdm.tqdm` progress bar.

    Returns:
        The same fragments, each replaced by its classified counterpart
        when classification succeeded.
    """
    stats = BatchStats(total=len(fragments))
    ordered: list[Fragment] = fragments.copy()

    with ThreadPoolExecutor(
        max_workers=classifier.config.max_concurrent,
    ) as executor:
        futures: dict[Future[Fragment], int] = {}
        for idx, frag in enumerate(fragments):
            future = executor.submit(classifier.classify, frag)
            futures[future] = idx
            if classifier.RATE_LIMIT_DELAY > 0 and idx < len(fragments) - 1:
                time.sleep(classifier.RATE_LIMIT_DELAY)

        completed_futures = as_completed(futures)
        future_iter = (
            tqdm(
                completed_futures,
                total=len(fragments),
                desc="Classifying",
            )
            if progress
            else completed_futures
        )

        for future in future_iter:
            idx = futures[future]
            _collect_one_result(
                future,
                idx,
                ordered,
                stats,
                provider=classifier.config.provider,
            )

    logger.info(
        "Batch complete: %d total, %d classified, %d failed",
        stats.total,
        stats.classified,
        stats.failed,
    )
    return ordered


def _collect_one_result(
    future: Future[Fragment],
    idx: int,
    ordered: list[Fragment],
    stats: BatchStats,
    *,
    provider: str,
) -> None:
    """Pull a future's result into ``ordered`` and update ``stats`` (OPS-003).

    Extracted so the per-result error path can include the failing
    fragment's ID and source path without inflating the parent
    function's complexity.

    Args:
        future: Completed classify-future from :func:`run_batch`.
        idx: Slot in ``ordered`` the result should occupy.
        ordered: Mutable result buffer; the original fragment is
            replaced in place when classification succeeds.
        stats: Counters updated as side effects.
        provider: Provider identifier (``"ollama"`` / ``"anthropic"``)
            logged in the OPS-003 prefix so multi-provider runs are
            disambiguated.
    """
    try:
        result = future.result()
        ordered[idx] = result
        if result.frequency.primary != Frequency.UNCLASSIFIED:
            stats.classified += 1
    except Exception:
        failing = ordered[idx]
        logger.exception(
            "[fragment=%s path=%s provider=%s] "
            "Unexpected error classifying fragment %d",
            failing.id,
            failing.source.original_file or "<inline>",
            provider,
            idx,
        )
        stats.failed += 1
