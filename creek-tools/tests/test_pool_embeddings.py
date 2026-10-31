"""Grounding embeddings from the attested pool — the contract tests (#1904).

ADR-0016 assigns Decision 1 item 13 no new T-number: option (i) runs "under
every item above", so the embedding route must satisfy the existing contract
tests, extended to it. Each class here names the test it extends:

- **T1 identity** — the pool's identity listing is checked *before* any text
  is sent; an answer naming another model, or of another dimensionality, is
  discarded; and a parquet written in one vector space is never read in
  another.
- **T6 INTIMATE** — each item's own tier reaches the chokepoint; an INTIMATE
  or untiered item is in no request body, and a batch of nothing but refused
  items dials nothing at all.
- **T20 narrow API** — at most 256 texts, at most 1 MiB, and the answer sized
  to fit its own cap; the body is exactly the texts and the closed envelope.

Plus the parts of the issue that are not T-numbered: no retries, partial
progress kept so a re-embed resumes, and the route decision itself.

Hermetic: every pool is a loopback listener this suite opened.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from creek.author.agents import _cosine, _cosine_pure
from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.classify.llm.pool_boundary import (
    POOL_EMBED_JSON_FLOAT_BYTES,
    POOL_MAX_EMBED_BATCH,
    POOL_MAX_EMBED_RESPONSE_BYTES,
    POOL_MAX_REQUEST_BYTES,
    POOL_REQUEST_ID_HEADER,
    POOL_URL_ENV,
)
from creek.classify.llm.pool_embeddings import (
    PoolEmbedder,
    PoolEmbeddingPin,
    _batch_item_cap,
    parse_embeddings,
    plan_batches,
    pool_wire_tier_for_item,
)
from creek.classify.llm.request_key import RequestKey
from creek.classify.llm.router import ModelRouter, refuses_off_device
from creek.config import EmbeddingsConfig, LLMConfig, LLMRoutingConfig
from creek.link.embeddings import (
    LOCAL_EMBEDDING_ROUTE,
    POOL_EMBEDDING_ROUTE,
    CachedEmbedding,
    EmbeddingLinker,
    EmbeddingRefusedError,
    EmbeddingSpace,
)
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from creek_mcp.grounding_route import (
    GroundingUnavailableError,
    grounding_embedder,
    link_embedder,
)
from creek_mcp.model_package import ModelPackageManifest
from tests.pool_support import (
    PINNED_EMBED_DIMENSIONS,
    PINNED_EMBED_MODEL,
    PINNED_EMBED_THRESHOLD,
    PINNED_MODEL,
    EmbeddingPool,
    SocketSpy,
    embedding_pin,
    fake_vector,
    pin_pool_manifest,
    pinned_identity_digest,
    pool_listing,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

PIN: Final[PoolEmbeddingPin] = PoolEmbeddingPin(
    model_name=PINNED_EMBED_MODEL,
    dimensions=PINNED_EMBED_DIMENSIONS,
    similarity_threshold=PINNED_EMBED_THRESHOLD,
    runtime_inventory_digest=pinned_identity_digest(PINNED_EMBED_MODEL),
)
"""The pin every :class:`EmbeddingPool` here matches."""

_INTIMATE_TITLE: Final[str] = "intimate-title-1904-never-on-the-wire"
_UNTIERED_TITLE: Final[str] = "untiered-title-1904-never-on-the-wire"
_UNKNOWN_TITLE: Final[str] = "unknown-tier-title-1904-never-on-the-wire"
_OPEN_TITLE: Final[str] = "an open title"
_PERSONAL_TITLE: Final[str] = "a personal title"
_UNCLASSIFIED_TITLE: Final[str] = "an unclassified title"

_TWO_BATCHES: Final[int] = 300
"""More than one batch at the pinned four dimensions."""

_BIG_TEXT: Final[int] = 400_000
"""Bytes of a text two of which fit one request and three of which do not."""


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[EmbeddingPool]:
    """A pinned pool whose listing offers the pinned embedding model."""
    served = EmbeddingPool({"/v1/models": pool_listing(PINNED_EMBED_MODEL)})
    served.pin(monkeypatch)
    try:
        yield served
    finally:
        served.close()


def _paths(served: EmbeddingPool) -> list[str]:
    """Return the path of every request *served* read, in order."""
    return [path for _method, path in served.requests]


def _all_bodies_text(served: EmbeddingPool) -> str:
    """Return every request body *served* read, as one string."""
    return json.dumps(served.bodies)


# --------------------------------------------------------------------------- #
# T6 — the tier reaches the chokepoint per item
# --------------------------------------------------------------------------- #


class TestTierAtTheChokepoint:
    """ADR-0016 T6 extended: per-fragment, never batch-level."""

    @pytest.mark.parametrize(
        "tier",
        [PrivacyTier.INTIMATE, "intimate", None, "secret", 7],
        ids=["intimate", "intimate-value", "none", "unknown", "foreign"],
    )
    def test_intimate_and_untiered_items_are_refused(self, tier: object) -> None:
        """The same fail-closed rule as the router (#2016): no wire tier."""
        assert pool_wire_tier_for_item(tier) is None
        assert refuses_off_device(tier, "pool")
        assert PoolEmbedder(PIN).refuses(tier)

    @pytest.mark.parametrize(
        ("tier", "wire"),
        [
            (PrivacyTier.OPEN, "open"),
            ("open", "open"),
            (PrivacyTier.PERSONAL, "personal"),
            (PrivacyTier.UNCLASSIFIED, "personal"),
            ("unclassified", "personal"),
        ],
    )
    def test_admitted_tiers_declare_their_own_wire_tier(
        self, tier: object, wire: str
    ) -> None:
        """``unclassified`` is declared as the more sensitive admitted tier."""
        assert pool_wire_tier_for_item(tier) == wire

    def test_the_chokepoint_predicate_is_the_routers_own(self) -> None:
        """A local provider may be shown INTIMATE; the pool never may."""
        assert not refuses_off_device(PrivacyTier.INTIMATE, "ollama")
        assert refuses_off_device(PrivacyTier.INTIMATE, "pool")
        assert not refuses_off_device(PrivacyTier.OPEN, "pool")

    def test_a_mixed_batch_carries_no_refused_text_and_no_mixed_tier(
        self, pool: EmbeddingPool
    ) -> None:
        """Every body's declared tier is the tier of every text in it."""
        items: list[tuple[str, object]] = [
            (_OPEN_TITLE, PrivacyTier.OPEN),
            (_INTIMATE_TITLE, PrivacyTier.INTIMATE),
            (_PERSONAL_TITLE, PrivacyTier.PERSONAL),
            (_UNTIERED_TITLE, None),
            (_UNCLASSIFIED_TITLE, "unclassified"),
            (_UNKNOWN_TITLE, "secret"),
        ]

        vectors = PoolEmbedder(PIN).embed(items)

        wire = _all_bodies_text(pool)
        for refused in (_INTIMATE_TITLE, _UNTIERED_TITLE, _UNKNOWN_TITLE):
            assert refused not in wire
        bodies = pool.embedding_bodies()
        assert {body["tier"]: body["input"] for body in bodies} == {
            "open": [_OPEN_TITLE],
            "personal": [_PERSONAL_TITLE, _UNCLASSIFIED_TITLE],
        }
        assert [vector is not None for vector in vectors] == [
            True,
            False,
            True,
            False,
            True,
            False,
        ]
        assert vectors[0] == fake_vector(_OPEN_TITLE)

    def test_only_refused_items_dial_nothing_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not even the identity listing: the socket spy records no dial."""
        monkeypatch.setenv(POOL_URL_ENV, "http://pool.internal:8443")
        spy = SocketSpy(monkeypatch)

        vectors = PoolEmbedder(PIN).embed(
            [(_INTIMATE_TITLE, PrivacyTier.INTIMATE), (_UNTIERED_TITLE, None)]
        )

        assert vectors == [None, None]
        assert spy.destinations == []


# --------------------------------------------------------------------------- #
# T1 — identity before content, and the vector space pinned
# --------------------------------------------------------------------------- #


class TestIdentity:
    """ADR-0016 T1 extended to the embedding model."""

    @pytest.mark.parametrize(
        "listing",
        [
            pool_listing(PINNED_MODEL),
            {"runtime": {"name": "other", "version": "9"}, "models": []},
        ],
        ids=["model-not-offered", "other-runtime"],
    )
    def test_a_different_identity_is_refused_before_a_batch_is_sent(
        self, monkeypatch: pytest.MonkeyPatch, listing: dict[str, object]
    ) -> None:
        """Only the listing is read; no embeddings request is made."""
        served = EmbeddingPool({"/v1/models": listing})
        served.pin(monkeypatch)
        try:
            vectors = PoolEmbedder(PIN).embed([(_OPEN_TITLE, PrivacyTier.OPEN)])
        finally:
            served.close()

        assert vectors == [None]
        assert _paths(served) == ["/v1/models"]
        assert _OPEN_TITLE not in _all_bodies_text(served)

    def test_a_pinned_digest_for_another_runtime_is_refused(
        self, pool: EmbeddingPool
    ) -> None:
        """The digest binds runtime and version, not only the model name."""
        stale = PoolEmbeddingPin(
            model_name=PINNED_EMBED_MODEL,
            dimensions=PINNED_EMBED_DIMENSIONS,
            similarity_threshold=PINNED_EMBED_THRESHOLD,
            runtime_inventory_digest="0" * 64,
        )

        assert PoolEmbedder(stale).embed([(_OPEN_TITLE, "open")]) == [None]
        assert _paths(pool) == ["/v1/models"]

    @pytest.mark.parametrize(
        ("embed_model", "dimensions"),
        [("another-embed", PINNED_EMBED_DIMENSIONS), (PINNED_EMBED_MODEL, 8)],
        ids=["model-echo", "dimensionality"],
    )
    def test_an_answer_from_another_space_is_discarded(
        self, monkeypatch: pytest.MonkeyPatch, embed_model: str, dimensions: int
    ) -> None:
        """The batch was sent, but no vector from it is kept."""
        served = EmbeddingPool(
            {"/v1/models": pool_listing(PINNED_EMBED_MODEL)},
            embed_model=embed_model,
            dimensions=dimensions,
        )
        served.pin(monkeypatch)
        try:
            vectors = PoolEmbedder(PIN).embed([(_OPEN_TITLE, "open")])
        finally:
            served.close()

        assert vectors == [None]

    def test_the_space_pins_model_dimensions_and_threshold(self) -> None:
        """The three facts ADR-0016 Decision 1 item 13 names, plus the route."""
        assert PoolEmbedder(PIN).space == EmbeddingSpace(
            route=POOL_EMBEDDING_ROUTE,
            model=PINNED_EMBED_MODEL,
            dimensions=PINNED_EMBED_DIMENSIONS,
            similarity_threshold=PINNED_EMBED_THRESHOLD,
        )


class TestAnswerParsing:
    """An answer is all-or-nothing."""

    @staticmethod
    def _answer(rows: list[object], model: str = PINNED_EMBED_MODEL) -> object:
        """Return an answer envelope around *rows*."""
        return {"model": model, "data": rows}

    def test_vectors_are_returned_in_request_order(self) -> None:
        """Rows are matched by ``index``, not by position."""
        payload = self._answer(
            [
                {"index": 1, "embedding": [2.0, 2.0]},
                {"index": 0, "embedding": [1, 1.5]},
            ]
        )

        assert parse_embeddings(
            payload, model=PINNED_EMBED_MODEL, count=2, dimensions=2
        ) == [[1.0, 1.5], [2.0, 2.0]]

    @pytest.mark.parametrize(
        "rows",
        [
            [{"index": 0, "embedding": [1.0, 1.0]}],
            [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0, 1.0]}],
            [{"index": 0, "embedding": [1.0, 1.0]}, {"index": 0, "embedding": [1, 1]}],
            [{"index": 0, "embedding": [1.0, 1.0]}, {"index": 2, "embedding": [1, 1]}],
            [{"index": 0, "embedding": [1.0, 1.0]}, {"index": True, "embedding": [1]}],
            [
                {"index": 0, "embedding": [1.0, math.nan]},
                {"index": 1, "embedding": [1]},
            ],
            [{"index": 0, "embedding": [1.0, True]}, {"index": 1, "embedding": [1, 1]}],
            [{"index": 0, "embedding": [1.0, "1"]}, {"index": 1, "embedding": [1, 1]}],
            [{"index": 0, "embedding": [1.0, 1.0]}, "not a row"],
        ],
        ids=[
            "short",
            "wrong-length",
            "repeated-index",
            "index-out-of-range",
            "bool-index",
            "non-finite",
            "bool-component",
            "string-component",
            "non-dict-row",
        ],
    )
    def test_any_defect_discards_the_whole_answer(self, rows: list[object]) -> None:
        """Nothing partial: a half-trusted batch is an untrusted batch."""
        assert (
            parse_embeddings(
                self._answer(rows), model=PINNED_EMBED_MODEL, count=2, dimensions=2
            )
            is None
        )

    @pytest.mark.parametrize("payload", [[], {"model": "other", "data": []}, {}])
    def test_a_bad_envelope_is_discarded(self, payload: object) -> None:
        """Not a dict, another model, or no ``data`` list."""
        assert (
            parse_embeddings(payload, model=PINNED_EMBED_MODEL, count=0, dimensions=2)
            is None
        )


# --------------------------------------------------------------------------- #
# T20 — the narrow API, vault half
# --------------------------------------------------------------------------- #


class TestNarrowApi:
    """ADR-0016 T20 extended: batches under both caps, and nothing else sent."""

    def test_a_large_pass_is_split_at_the_batch_cap(self, pool: EmbeddingPool) -> None:
        """300 open titles: two batches, neither over 256."""
        items = [(f"title {n}", PrivacyTier.OPEN) for n in range(_TWO_BATCHES)]

        vectors = PoolEmbedder(PIN).embed(items)

        sizes = [len(body["input"]) for body in pool.embedding_bodies()]
        assert sizes == [POOL_MAX_EMBED_BATCH, _TWO_BATCHES - POOL_MAX_EMBED_BATCH]
        assert all(vector is not None for vector in vectors)

    def test_the_body_is_exactly_the_texts_and_the_closed_envelope(
        self, pool: EmbeddingPool
    ) -> None:
        """No parquet, no vectors, no ids: the texts to embed and four keys."""
        PoolEmbedder(PIN).embed([(_OPEN_TITLE, PrivacyTier.OPEN)])

        (body,) = pool.embedding_bodies()
        assert set(body) == {"model", "input", "tier", "deadline_ms"}
        assert body["model"] == PINNED_EMBED_MODEL
        assert body["input"] == [_OPEN_TITLE]
        posts = [
            headers
            for (method, _path), headers in zip(
                pool.requests, pool.headers, strict=True
            )
            if method == "POST"
        ]
        assert posts[0][POOL_REQUEST_ID_HEADER.lower()]

    def test_the_byte_cap_splits_and_an_oversized_text_is_dropped(self) -> None:
        """Two big texts share a batch, a third starts one, a huge one is dropped."""
        texts = ["x" * _BIG_TEXT] * 3 + ["y" * POOL_MAX_REQUEST_BYTES]

        batches, oversized = plan_batches(
            texts, range(len(texts)), item_cap=POOL_MAX_EMBED_BATCH
        )

        assert batches == [[0, 1], [2]]
        assert oversized == 1

    def test_the_item_cap_also_splits(self) -> None:
        """Small texts are bounded by the item cap alone."""
        batches, oversized = plan_batches(["a"] * 5, range(5), item_cap=2)

        assert batches == [[0, 1], [2, 3], [4]]
        assert oversized == 0

    @pytest.mark.parametrize("dimensions", [4, 768, 2560, 4096])
    def test_a_batch_answer_fits_the_response_cap(self, dimensions: int) -> None:
        """At any pinned width, a full batch's worst-case answer fits its cap."""
        cap = _batch_item_cap(dimensions)

        assert 1 <= cap <= POOL_MAX_EMBED_BATCH
        assert cap * dimensions * POOL_EMBED_JSON_FLOAT_BYTES < (
            POOL_MAX_EMBED_RESPONSE_BYTES
        )

    def test_a_wide_space_gets_smaller_batches(self) -> None:
        """2560 floats a vector cannot ride 256 to a batch under 8 MiB."""
        assert _batch_item_cap(2560) < POOL_MAX_EMBED_BATCH
        assert _batch_item_cap(PINNED_EMBED_DIMENSIONS) == POOL_MAX_EMBED_BATCH


# --------------------------------------------------------------------------- #
# No retries; partial progress kept
# --------------------------------------------------------------------------- #


class TestFailure:
    """One attempt per batch; the first failure ends the pass."""

    def test_a_failed_batch_is_never_retried_and_ends_the_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two tiers to send, the first refuses: exactly one embeddings dial."""
        served = EmbeddingPool(
            {"/v1/models": pool_listing(PINNED_EMBED_MODEL)},
            fail_embeddings_after=0,
        )
        served.pin(monkeypatch)
        try:
            vectors = PoolEmbedder(PIN).embed(
                [(_OPEN_TITLE, "open"), (_PERSONAL_TITLE, "personal")]
            )
        finally:
            served.close()

        assert vectors == [None, None]
        assert _paths(served) == ["/v1/models", "/v1/embeddings"]

    def test_vectors_before_the_failure_are_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resumable half: what the pool answered survives the failure."""
        served = EmbeddingPool(
            {"/v1/models": pool_listing(PINNED_EMBED_MODEL)},
            fail_embeddings_after=1,
        )
        served.pin(monkeypatch)
        items = [(f"t{n}", PrivacyTier.OPEN) for n in range(_TWO_BATCHES)]
        try:
            vectors = PoolEmbedder(PIN).embed(items)
        finally:
            served.close()

        kept = [vector for vector in vectors if vector is not None]
        assert len(kept) == POOL_MAX_EMBED_BATCH

    def test_an_unreachable_pool_refuses_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The listing dial fails: nothing sent, nothing raised."""
        monkeypatch.setenv(POOL_URL_ENV, "http://pool.internal:8443")
        spy = SocketSpy(monkeypatch)

        assert PoolEmbedder(PIN).embed([(_OPEN_TITLE, "open")]) == [None]
        assert spy.hosts == ["pool.internal"]


# --------------------------------------------------------------------------- #
# The linker and the parquet: never compare across spaces
# --------------------------------------------------------------------------- #


class _StaticEmbedder:
    """A non-local route with no network, for the parquet tests."""

    def __init__(self, space: EmbeddingSpace, *, answer: bool = True) -> None:
        self._space = space
        self._answer = answer

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    def refuses(self, tier: object) -> bool:
        return refuses_off_device(tier, "pool")

    def embed(self, items: Sequence[tuple[str, object]]) -> list[list[float] | None]:
        dims = self._space.dimensions or 1
        return [
            fake_vector(text, dims) if self._answer and not self.refuses(tier) else None
            for text, tier in items
        ]


def _pool_space(
    *,
    model: str = PINNED_EMBED_MODEL,
    dimensions: int = PINNED_EMBED_DIMENSIONS,
    similarity_threshold: float = PINNED_EMBED_THRESHOLD,
) -> EmbeddingSpace:
    """Return the pinned pool space, or one with a single fact moved."""
    return EmbeddingSpace(
        route=POOL_EMBEDDING_ROUTE,
        model=model,
        dimensions=dimensions,
        similarity_threshold=similarity_threshold,
    )


def _row(fragment_id: str, linker: EmbeddingLinker) -> CachedEmbedding:
    """Return one cache row written by *linker*'s own space."""
    space = linker.space
    vector = fake_vector(fragment_id, space.dimensions or 3)
    return CachedEmbedding(
        fragment_id=fragment_id,
        content_hash="h",
        model_name=space.model,
        vector=vector,
        computed_at=datetime(2026, 10, 31, tzinfo=UTC),
        route=space.route,
        dimensions=len(vector),
        similarity_threshold=linker.config.similarity_threshold,
    )


def _fragment(fragment_id: str, tier: PrivacyTier) -> Fragment:
    """Return a minimal fragment of *tier*."""
    return Fragment(
        id=fragment_id,
        title=fragment_id,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        privacy_tier=tier,
    )


class TestVectorSpaces:
    """A space switch is a detectable mismatch that re-embeds, never a mix."""

    def _saved(self, path: Path, linker: EmbeddingLinker) -> None:
        """Write one row in *linker*'s space to *path*."""
        linker.save_cache({"f1": _row("f1", linker)}, path)

    def test_a_pool_parquet_reads_back_in_its_own_space(self, tmp_path: Path) -> None:
        """Round trip: route, dimensions and threshold are persisted."""
        path = tmp_path / "embeddings.parquet"
        linker = EmbeddingLinker(
            EmbeddingsConfig(), embedder=_StaticEmbedder(_pool_space())
        )
        self._saved(path, linker)

        loaded = linker.load_cache(path)

        assert set(loaded) == {"f1"}
        assert loaded["f1"].route == POOL_EMBEDDING_ROUTE
        assert loaded["f1"].dimensions == PINNED_EMBED_DIMENSIONS
        assert loaded["f1"].similarity_threshold == PINNED_EMBED_THRESHOLD

    @pytest.mark.parametrize(
        "moved_space",
        [
            _pool_space(model="another-embed"),
            _pool_space(dimensions=8),
            _pool_space(similarity_threshold=0.6),
        ],
        ids=["model", "dimensions", "threshold"],
    )
    def test_a_changed_pin_reads_nothing_so_the_corpus_re_embeds(
        self, tmp_path: Path, moved_space: EmbeddingSpace
    ) -> None:
        """Any one of the pinned facts moving invalidates every row."""
        path = tmp_path / "embeddings.parquet"
        self._saved(
            path,
            EmbeddingLinker(
                EmbeddingsConfig(), embedder=_StaticEmbedder(_pool_space())
            ),
        )
        moved = EmbeddingLinker(
            EmbeddingsConfig(), embedder=_StaticEmbedder(moved_space)
        )

        assert moved.load_cache(path) == {}

    def test_local_and_pool_never_read_each_others_rows(self, tmp_path: Path) -> None:
        """Both directions of the migration consequence ADR-0018 records."""
        pool_path = tmp_path / "pool.parquet"
        local_path = tmp_path / "local.parquet"
        pool_linker = EmbeddingLinker(
            EmbeddingsConfig(model=PINNED_EMBED_MODEL),
            embedder=_StaticEmbedder(_pool_space()),
        )
        local_linker = EmbeddingLinker(EmbeddingsConfig(model=PINNED_EMBED_MODEL))
        self._saved(pool_path, pool_linker)
        self._saved(local_path, local_linker)

        assert local_linker.load_cache(pool_path) == {}
        assert pool_linker.load_cache(local_path) == {}
        assert set(local_linker.load_cache(local_path)) == {"f1"}

    def test_a_parquet_from_before_the_space_columns_is_a_local_one(
        self, tmp_path: Path
    ) -> None:
        """Self-hosted caches written before #1904 keep loading unchanged."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = tmp_path / "legacy.parquet"
        table = pa.table(
            {
                "fragment_id": ["f1"],
                "content_hash": ["h"],
                "model_name": ["all-MiniLM-L6-v2"],
                "embedding": pa.array([[1.0, 2.0]], type=pa.list_(pa.float32())),
                "computed_at": pa.array(
                    [datetime(2026, 1, 1, tzinfo=UTC)],
                    type=pa.timestamp("us", tz="UTC"),
                ),
            }
        )
        pq.write_table(table, path)

        local = EmbeddingLinker(EmbeddingsConfig()).load_cache(path)
        pooled = EmbeddingLinker(
            EmbeddingsConfig(), embedder=_StaticEmbedder(_pool_space())
        ).load_cache(path)

        assert local["f1"].route == LOCAL_EMBEDDING_ROUTE
        assert local["f1"].dimensions is None
        assert pooled == {}

    def test_a_pool_linker_reads_its_threshold_and_model_from_the_pin(self) -> None:
        """A writable vault config cannot move a pinned space."""
        linker = EmbeddingLinker(
            EmbeddingsConfig(model="all-MiniLM-L6-v2", similarity_threshold=0.9),
            embedder=_StaticEmbedder(_pool_space()),
        )

        assert linker.config.model == PINNED_EMBED_MODEL
        assert linker.config.similarity_threshold == PINNED_EMBED_THRESHOLD

    def test_a_route_with_no_vector_raises_the_refusal(self) -> None:
        """``generate_embedding`` on a refused query is a typed degradation."""
        linker = EmbeddingLinker(
            EmbeddingsConfig(), embedder=_StaticEmbedder(_pool_space())
        )

        with pytest.raises(EmbeddingRefusedError):
            linker.generate_embedding("q", tier=PrivacyTier.INTIMATE)
        assert linker.generate_embedding("q", tier="open") == fake_vector("q", 4)

    def test_the_bulk_route_skips_what_it_did_not_answer(self) -> None:
        """INTIMATE fragments get no vector; the rest are embedded."""
        open_fragment = _fragment("frag-open", PrivacyTier.OPEN)
        intimate = _fragment("frag-int", PrivacyTier.INTIMATE)
        linker = EmbeddingLinker(
            EmbeddingsConfig(), embedder=_StaticEmbedder(_pool_space())
        )

        vectors = linker.generate_embeddings([open_fragment, intimate])

        assert set(vectors) == {"frag-open"}

    def test_a_missing_parquet_reader_is_only_a_cost_on_the_pool_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The managed image has no pyarrow; the pool route embeds live instead."""
        path = tmp_path / "embeddings.parquet"
        path.write_bytes(b"not read")
        monkeypatch.setitem(sys.modules, "pyarrow", None)
        monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)

        pooled = EmbeddingLinker(
            EmbeddingsConfig(), embedder=_StaticEmbedder(_pool_space())
        )
        assert pooled.load_cache(path) == {}
        with pytest.raises(ImportError):
            EmbeddingLinker(EmbeddingsConfig()).load_cache(path)


def test_pure_python_cosine_matches_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The managed image has no numpy; ranking must not change without it."""
    left, right = [1.0, 2.0, 3.0], [3.0, -1.0, 0.5]

    assert _cosine_pure(left, right) == pytest.approx(_cosine(left, right))
    assert _cosine_pure([0.0, 0.0], [1.0, 1.0]) == 0.0
    monkeypatch.setitem(sys.modules, "numpy", None)
    assert _cosine(left, right) == pytest.approx(_cosine_pure(left, right))


# --------------------------------------------------------------------------- #
# The route decision
# --------------------------------------------------------------------------- #


def _router(generation: str = "pool") -> ModelRouter:
    """Return a router whose ``generation`` stage is *generation*."""
    return ModelRouter(
        LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model=PINNED_MODEL),
            generation=LLMConfig(provider=generation, model=PINNED_MODEL),
        )
    )


@pytest.fixture
def managed_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed Machine with a pinned pool and an embedding pin."""
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    monkeypatch.setenv(POOL_URL_ENV, "http://pool.internal:8443")
    pin_pool_manifest(tmp_path, monkeypatch, embedding=embedding_pin())


class TestRouteDecision:
    """Pool embeddings only where the pool answers, never a silent fallback."""

    @pytest.mark.usefixtures("managed_pool")
    def test_a_keyless_managed_pool_request_is_grounded_by_the_pool(self) -> None:
        """The R4-4 case: the pinned embedder, with the manifest's pin."""
        embedder = grounding_embedder(_router(), PrivacyTier.PERSONAL)

        assert isinstance(embedder, PoolEmbedder)
        assert embedder.pin == PIN

    def test_a_self_hosted_vault_keeps_the_local_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0004 is amended for managed vaults only."""
        monkeypatch.setenv(POOL_URL_ENV, "http://pool.internal:8443")
        pin_pool_manifest(tmp_path, monkeypatch, embedding=embedding_pin())

        assert grounding_embedder(_router(), PrivacyTier.OPEN) is None

    @pytest.mark.usefixtures("managed_pool")
    def test_a_writers_key_keeps_the_local_route(self) -> None:
        """T10: with a writer's key the pool is never dialled, for grounding too."""
        key = RequestKey("anthropic", "grounding-route-test-1904")

        assert grounding_embedder(_router(), PrivacyTier.OPEN, key) is None

    @pytest.mark.usefixtures("managed_pool")
    def test_an_intimate_reflection_is_grounded_where_it_is_answered(self) -> None:
        """Redirected local by the router, so not the pool."""
        assert grounding_embedder(_router(), PrivacyTier.INTIMATE) is None

    @pytest.mark.usefixtures("managed_pool")
    def test_generation_off_the_pool_keeps_the_local_route(self) -> None:
        """T15: pool embeddings only on a request the pool also answers."""
        assert grounding_embedder(_router("ollama"), PrivacyTier.OPEN) is None

    @pytest.mark.usefixtures("managed_pool")
    def test_an_intimate_request_with_no_local_route_is_not_pool_grounded(
        self,
    ) -> None:
        """The router refuses outright; grounding follows, it does not egress."""
        router = ModelRouter(
            LLMRoutingConfig(
                default=LLMConfig(provider="pool", model=PINNED_MODEL),
                generation=LLMConfig(provider="pool", model=PINNED_MODEL),
            )
        )

        assert grounding_embedder(router, PrivacyTier.INTIMATE) is None

    def test_no_embedding_pin_is_option_iii(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The vendor offered no attested model: ungrounded, not local."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        monkeypatch.setenv(POOL_URL_ENV, "http://pool.internal:8443")
        pin_pool_manifest(tmp_path, monkeypatch)

        with pytest.raises(GroundingUnavailableError):
            grounding_embedder(_router(), PrivacyTier.OPEN)
        assert link_embedder(_router()) is None

    def test_no_pool_destination_is_option_iii(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pin with nowhere pinned to send it is no route at all."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        pin_pool_manifest(tmp_path, monkeypatch, embedding=embedding_pin())

        with pytest.raises(GroundingUnavailableError):
            grounding_embedder(_router(), PrivacyTier.OPEN)

    def test_an_unloadable_manifest_is_option_iii(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pin that cannot be read fails closed, content-free."""
        monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
        monkeypatch.setenv(POOL_URL_ENV, "http://pool.internal:8443")
        pin_pool_manifest(tmp_path, monkeypatch, embedding=embedding_pin())
        (tmp_path / "model-package" / "manifest.json").write_text("{", "utf-8")

        with pytest.raises(GroundingUnavailableError) as caught:
            grounding_embedder(_router(), PrivacyTier.OPEN)
        assert "pool.internal" not in str(caught.value)

    @pytest.mark.usefixtures("managed_pool")
    def test_the_bulk_pass_follows_the_keyless_rule(self) -> None:
        """``POST /v1/links`` carries no key, so a managed pool vault uses it."""
        assert isinstance(link_embedder(_router()), PoolEmbedder)
        assert link_embedder(_router("ollama")) is None


class TestManifestPin:
    """The embedding pin is additive and optional on the manifest."""

    def test_a_manifest_without_it_still_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every manifest written before #1904 is unchanged."""
        assert pin_pool_manifest(tmp_path, monkeypatch).embedding is None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"dimensions": 0},
            {"dimensions": 100_000},
            {"similarity_threshold": 1.5},
            {"runtime_inventory_digest": "not-a-digest"},
            {"model_name": "has space"},
            {"extra": True},
        ],
        ids=["zero-dims", "huge-dims", "threshold", "digest", "model", "extra"],
    )
    def test_a_malformed_pin_is_refused(self, overrides: dict[str, object]) -> None:
        """Every pinned fact is validated; nothing extra is admitted."""
        fields = {
            "schema_version": 1,
            "runtime_name": "r",
            "runtime_version": "1",
            "runtime_digest": "c" * 64,
            "model_name": PINNED_MODEL,
            "model_blob_sha256": "b" * 64,
            "runtime_inventory_digest": "a" * 64,
            "quantization": "Q4",
            "parameter_count": 1,
            "size_bytes": 1,
            "license_spdx": "Apache-2.0",
            "license_url": "https://example.test/LICENSE",
            "embedding": embedding_pin(**overrides),
        }

        with pytest.raises(ValueError, match="embedding"):
            ModelPackageManifest.model_validate_json(json.dumps(fields))
