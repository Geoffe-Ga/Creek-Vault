"""Grounding embeddings from the attested shared pool (#1904, ADR-0018).

ADR-0016 Decision 1 item 13, Q2 option (i), as the owner ratified it and as
release decision R4-4 scoped it: a managed vault whose request carries no
writer's key has its grounding embeddings computed by the same pinned,
attested pool that answers its reflections, instead of by a local
sentence-transformer the managed image cannot hold. This module is that
route's client. It builds requests; :mod:`creek.classify.llm.pool_boundary`
alone dials them.

Every item of Decision 1 applies, and each is owned somewhere specific:

- **Per-fragment tier at the chokepoint.** :meth:`PoolEmbedder.embed` hands
  each item's own tier to
  :func:`creek.classify.llm.router.refuses_off_device` — the predicate the
  :class:`~creek.classify.llm.router.ModelRouter` gate itself uses — before
  any request is built. An INTIMATE item, and an untiered one (``None`` or a
  value naming no tier, #2016), is dropped there and never reaches a body. A
  batch is never given a tier of its own: items are grouped by their wire
  tier, so every item in a batch carries exactly the tier the batch declares.
- **Identity before content.** The pool's identity listing must report the
  pinned runtime serving the pinned embedding model *before* the first text
  is sent (T1); every answer must echo that model and return vectors of the
  pinned dimensionality, or the whole batch is discarded.
- **Narrow API.** At most :data:`~creek.classify.llm.pool_boundary.POOL_MAX_EMBED_BATCH`
  texts and at most :data:`~creek.classify.llm.pool_boundary.POOL_MAX_REQUEST_BYTES`
  per request, and few enough that the expected answer fits the response
  cap. The body is exactly ``model``, ``input``, ``tier`` and
  ``deadline_ms``: the texts to embed, never the vault's parquet.
- **No retries, no fallback.** The first failure stops the pass. Vectors
  already returned are kept — so an interrupted bulk re-embed resumes where
  it stopped — and nothing is ever sent anywhere else. A pin that cannot be
  met is option (iii): ungrounded, with nothing sent.

Everything logged here is a count, a status or an exception *type*; no text,
title, vector or destination.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from creek.classify.llm.pool_boundary import (
    POOL_EMBED_JSON_FLOAT_BYTES,
    POOL_MAX_DEADLINE_MS,
    POOL_MAX_EMBED_BATCH,
    POOL_MAX_EMBED_RESPONSE_BYTES,
    POOL_MAX_REQUEST_BYTES,
    POOL_OPEN_TIER,
    POOL_PERSONAL_TIER,
    POOL_REQUEST_ID_HEADER,
    PoolBoundaryError,
    current_pool_scope,
    pool_embed,
    pool_response_ok,
)
from creek.classify.llm.providers import check_pool_available
from creek.classify.llm.router import recognised_tier, refuses_off_device
from creek.link.embeddings import POOL_EMBEDDING_ROUTE, EmbeddingSpace
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

POOL_PROVIDER: Final[str] = "pool"
"""The provider string the chokepoint is asked about for every item."""

_REQUEST_ID_BYTES: Final[int] = 16

_ENVELOPE_BYTES: Final[int] = 1024
"""Bytes reserved in every request for everything except the texts.

``model``, ``tier``, ``deadline_ms`` and the JSON punctuation around them. A
generous constant rather than an exact measurement: under-filling a batch
costs one more request, over-filling one is a refusal before the dial.
"""

_RESPONSE_ITEM_OVERHEAD: Final[int] = 64
"""Bytes one answered item costs beyond its floats (``index``, keys, braces)."""

_PERSONAL_WIRE_TIERS: Final[frozenset[PrivacyTier]] = frozenset(
    {PrivacyTier.PERSONAL, PrivacyTier.UNCLASSIFIED}
)
"""Recognised non-intimate tiers declared ``personal`` on the wire.

``unclassified`` ranks equal to ``personal`` everywhere a tier is ranked
(:data:`creek_mcp.tier_ceiling._TIER_RANK`) and the pool contract has no
spelling for it, so it is declared as the more sensitive of the two admitted
tiers, never as ``open``.
"""


@dataclass(frozen=True)
class PoolEmbeddingPin:
    """The pinned embedding identity, read from the operator's manifest.

    Attributes:
        model_name: The vendor's embedding model identifier.
        dimensions: The vector length every answer must have.
        similarity_threshold: The resonance threshold calibrated for this
            model's cosine distribution.
        runtime_inventory_digest: The identity digest the pool's listing must
            imply for this model under the manifest's runtime.
    """

    model_name: str
    dimensions: int
    similarity_threshold: float
    runtime_inventory_digest: str


def pool_wire_tier_for_item(tier: object) -> str | None:
    """Return the tier *tier* is declared as on the embeddings wire, or ``None``.

    Args:
        tier: One item's privacy tier, as a member, its value, or anything.

    Returns:
        ``open`` or ``personal`` for content the chokepoint admits to the
        pool; ``None`` for content it refuses — INTIMATE, ``None``, and any
        value naming no tier.
    """
    if refuses_off_device(tier, POOL_PROVIDER):
        return None
    known = recognised_tier(tier)
    return POOL_PERSONAL_TIER if known in _PERSONAL_WIRE_TIERS else POOL_OPEN_TIER


def _text_bytes(text: str) -> int:
    """Return what *text* costs inside the request body's ``input`` array."""
    return len(json.dumps(text).encode("utf-8")) + 1


def _batch_item_cap(dimensions: int) -> int:
    """Return how many items one batch may carry at *dimensions*.

    Args:
        dimensions: The pinned vector length.

    Returns:
        The smaller of the contract's batch cap and the count whose
        worst-case answer still fits the embeddings response cap; at least 1.
    """
    per_item = dimensions * POOL_EMBED_JSON_FLOAT_BYTES + _RESPONSE_ITEM_OVERHEAD
    return max(1, min(POOL_MAX_EMBED_BATCH, POOL_MAX_EMBED_RESPONSE_BYTES // per_item))


def plan_batches(
    texts: Sequence[str], indices: Sequence[int], *, item_cap: int
) -> tuple[list[list[int]], int]:
    """Split *indices* into batches under both the item and the byte cap.

    Args:
        texts: Every item's text, indexed by position.
        indices: The positions to batch, in order.
        item_cap: The most items one batch may hold.

    Returns:
        The batches, and how many items were too large to send at all — those
        are dropped, never truncated.
    """
    budget = POOL_MAX_REQUEST_BYTES - _ENVELOPE_BYTES
    batches: list[list[int]] = []
    current: list[int] = []
    used = 0
    oversized = 0
    for index in indices:
        cost = _text_bytes(texts[index])
        if cost > budget:
            oversized += 1
            continue
        if current and (len(current) >= item_cap or used + cost > budget):
            batches.append(current)
            current, used = [], 0
        current.append(index)
        used += cost
    if current:
        batches.append(current)
    return batches, oversized


def _component(value: object) -> float | None:
    """Return *value* as a finite float, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _vector(raw: object, dimensions: int) -> list[float] | None:
    """Return *raw* as a vector of exactly *dimensions* finite floats, or ``None``."""
    if not isinstance(raw, list) or len(raw) != dimensions:
        return None
    vector = [_component(value) for value in raw]
    if any(value is None for value in vector):
        return None
    return [value for value in vector if value is not None]


def _answer_rows(payload: object, model: str, count: int) -> list[object] | None:
    """Return the answer's ``data`` rows when its envelope is usable."""
    if not isinstance(payload, dict) or payload.get("model") != model:
        return None
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != count:
        return None
    return rows


def parse_embeddings(
    payload: object, *, model: str, count: int, dimensions: int
) -> list[list[float]] | None:
    """Return the *count* vectors a pool answer carries, or ``None`` if unusable.

    The answer is the OpenAI-compatible list shape: ``model`` and ``data``,
    each row an ``index`` and an ``embedding``. Anything else — another
    model, a missing or repeated index, a vector of the wrong length, a
    non-finite component — discards the whole answer. Nothing partial is
    returned, because a batch half of whose vectors came from somewhere
    unexpected is not a batch whose other half can be trusted.

    Args:
        payload: The decoded answer.
        model: The pinned model the answer must name.
        count: How many texts were sent.
        dimensions: The pinned vector length.

    Returns:
        The vectors, in request order, or ``None``.
    """
    rows = _answer_rows(payload, model, count)
    if rows is None:
        return None
    vectors: list[list[float] | None] = [None] * count
    for row in rows:
        placed = _indexed_vector(row, count, dimensions)
        if placed is None or vectors[placed[0]] is not None:
            return None
        vectors[placed[0]] = placed[1]
    return [vector for vector in vectors if vector is not None]


def _indexed_vector(
    row: object, count: int, dimensions: int
) -> tuple[int, list[float]] | None:
    """Return one answer row's ``(index, vector)``, or ``None`` when unusable."""
    if not isinstance(row, dict):
        return None
    index = row.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
        return None
    vector = _vector(row.get("embedding"), dimensions)
    return None if vector is None else (index, vector)


class PoolEmbedder:
    """Embed texts through the attested pool, one chokepoint check per item.

    Constructed only by the route that decided this request belongs on the
    pool (:mod:`creek_mcp.grounding_route`); it performs no I/O until
    :meth:`embed` is called.

    Attributes:
        pin: The pinned embedding identity.
    """

    def __init__(self, pin: PoolEmbeddingPin) -> None:
        """Bind this embedder to *pin*.

        Args:
            pin: The manifest's embedding identity.
        """
        self.pin = pin

    @property
    def space(self) -> EmbeddingSpace:
        """The vector space every vector this route returns belongs to."""
        return EmbeddingSpace(
            route=POOL_EMBEDDING_ROUTE,
            model=self.pin.model_name,
            dimensions=self.pin.dimensions,
            similarity_threshold=self.pin.similarity_threshold,
        )

    def refuses(self, tier: object) -> bool:
        """Return whether content of *tier* is refused at the chokepoint.

        Args:
            tier: The content's privacy tier.

        Returns:
            ``True`` for INTIMATE and for anything untiered.
        """
        return pool_wire_tier_for_item(tier) is None

    def embed(self, items: Sequence[tuple[str, object]]) -> list[list[float] | None]:
        """Return one vector per ``(text, tier)`` item, ``None`` where none came.

        Refused items cost no dial. When every item is refused, nothing is
        dialled at all — not even the identity listing.

        Args:
            items: ``(text, tier)`` pairs.

        Returns:
            Vectors aligned with *items*.
        """
        results: list[list[float] | None] = [None] * len(items)
        groups = self._admitted_groups(items)
        refused = len(items) - sum(len(group) for group in groups.values())
        if refused:
            logger.info("Embedding chokepoint refused %d item(s) by tier", refused)
        if not groups:
            return results
        if not self._identity_matches():
            logger.warning("Pool embedding identity is not the pinned one; ungrounded")
            return results
        texts = [text for text, _tier in items]
        for wire_tier, indices in sorted(groups.items()):
            if not self._embed_group(texts, indices, wire_tier, results):
                break
        return results

    @staticmethod
    def _admitted_groups(items: Sequence[tuple[str, object]]) -> dict[str, list[int]]:
        """Group admitted item positions by the wire tier each one declares."""
        groups: dict[str, list[int]] = {}
        for index, (_text, tier) in enumerate(items):
            wire_tier = pool_wire_tier_for_item(tier)
            if wire_tier is not None:
                groups.setdefault(wire_tier, []).append(index)
        return groups

    def _identity_matches(self) -> bool:
        """Return whether the pool reports the pinned embedding identity."""
        return check_pool_available(
            model_name=self.pin.model_name,
            inventory_digest=self.pin.runtime_inventory_digest,
        )

    def _embed_group(
        self,
        texts: Sequence[str],
        indices: Sequence[int],
        wire_tier: str,
        results: list[list[float] | None],
    ) -> bool:
        """Send one tier's items in capped batches, filling *results*.

        Returns:
            ``False`` when a batch failed, so the caller stops the pass.
        """
        batches, oversized = plan_batches(
            texts, indices, item_cap=_batch_item_cap(self.pin.dimensions)
        )
        if oversized:
            logger.warning("Skipped %d item(s) over the request cap", oversized)
        for batch in batches:
            vectors = self._send([texts[index] for index in batch], wire_tier)
            if vectors is None:
                return False
            for index, vector in zip(batch, vectors, strict=True):
                results[index] = vector
        return True

    def _send(self, texts: list[str], wire_tier: str) -> list[list[float]] | None:
        """Send one batch and return its vectors, or ``None`` on any failure."""
        scope = current_pool_scope()
        payload: dict[str, object] = {
            "model": self.pin.model_name,
            "input": texts,
            "tier": wire_tier,
            "deadline_ms": (
                POOL_MAX_DEADLINE_MS if scope is None else scope.deadline_ms()
            ),
        }
        request_id = (
            secrets.token_hex(_REQUEST_ID_BYTES) if scope is None else scope.request_id
        )
        try:
            response = pool_embed(payload, headers={POOL_REQUEST_ID_HEADER: request_id})
        except PoolBoundaryError as exc:
            logger.warning("Pool embedding batch refused (%s)", type(exc).__name__)
            return None
        if not pool_response_ok(response):
            logger.warning("Pool embedding batch answered HTTP %d", response.status)
            return None
        vectors = parse_embeddings(
            response.payload,
            model=self.pin.model_name,
            count=len(texts),
            dimensions=self.pin.dimensions,
        )
        if vectors is None:
            logger.warning("Pool embedding answer was unusable; discarded")
        return vectors
