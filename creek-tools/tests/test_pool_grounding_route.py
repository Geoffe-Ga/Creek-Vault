"""Pool-grounded reflections and the pool bulk pass, end to end (#1904).

The route half of ADR-0016 Decision 1 item 13 (Q2 option (i)) and release
decision R4-4: on a managed vault, a reflection with no writer's key is
grounded by the attested pool's embedding model, through ``POST
/v1/reflections`` exactly as production serves it — router, model pin,
grounding session, pool boundary — against a loopback pool that records what
crossed the wire.

- **T15 provenance, extended.** A pool-grounded answer still reports the
  pool as its ``inference_source``, and every request the vault made went to
  that one pinned destination.
- **The parquet stays in the vault.** A sentinel planted in the vault's
  ``embeddings.parquet`` appears in no request body.
- **T6, route half.** An INTIMATE fragment's title is in no embeddings
  request, and a reflection routed INTIMATE spends no embeddings dial.
- **T10, extended.** A writer's key keeps the pool undialled — grounding
  included.
- **Option (iii).** With no attested embedding model pinned, the reflection
  is still answered, ungrounded, and no embeddings request is made.
- **The bulk pass.** ``creek.link`` over a managed pool vault persists its
  vectors in the pool's vector space and sends no INTIMATE title.

Nothing is billed: the pool is a loopback listener and the vendor is an
:class:`httpx.MockTransport` behind the real SDK.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest
import yaml

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.pool_boundary import POOL_VENDOR_ENV
from creek.config import CreekConfig, EmbeddingsConfig
from creek.link.embeddings import (
    POOL_EMBEDDING_ROUTE,
    CachedEmbedding,
    EmbeddingLinker,
    embeddings_cache_path,
)
from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER
from creek_mcp.tools.link import link_tool
from tests.pool_support import (
    PINNED_EMBED_DIMENSIONS,
    PINNED_EMBED_MODEL,
    PINNED_MODEL,
    EmbeddingPool,
    FakeVendor,
    embedding_pin,
    generation,
    pin_pool_manifest,
    pool_listing,
)
from tests.v1_api_support import REFLECTIONS_PATH, build_app, headers, seed_vault

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx

ENTRY: Final[str] = "I keep saying yes to things I do not want, and I feel it later."
"""A benign entry the care guard does not escalate."""

QUOTE: Final[str] = "saying yes to things I do not want"

_TURN: Final[str] = json.dumps(
    {"notes": [{"quote": QUOTE, "kind": "pattern", "note": "It costs you."}]}
)

_OPEN_TITLE: Final[str] = "Notes on saying yes too easily"
_PERSONAL_TITLE: Final[str] = "What the river taught me about time"
_INTIMATE_TITLE: Final[str] = "intimate-title-1904-route-sentinel"
_PARQUET_SENTINEL: Final[str] = "parquet-sentinel-1904-never-on-the-wire"

_CONFIG_SUBPATH: Final[tuple[str, ...]] = ("00-Creek-Meta", "creek_config.yaml")
_VENDOR: Final[str] = "privatemode"

HTTP_OK: Final[int] = 200


def _write_fragment(vault: Path, frag_id: str, title: str, tier: str) -> None:
    """Write one model-valid corpus fragment of *tier*."""
    stamp = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": stamp,
        "ingested": stamp,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": tier,
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="a body", **metadata)),
        encoding="utf-8",
    )


def _vault(tmp_path: Path) -> Path:
    """Scaffold a vault routed to the pool, with one fragment per tier."""
    vault = seed_vault(tmp_path / "vault")
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["default"].update(model=PINNED_MODEL)
    data["llm"]["generation"] = {
        **data["llm"]["default"],
        "provider": "pool",
        "model": PINNED_MODEL,
    }
    path = vault.joinpath(*_CONFIG_SUBPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    _write_fragment(vault, "frag-open", _OPEN_TITLE, "open")
    _write_fragment(vault, "frag-personal", _PERSONAL_TITLE, "personal")
    _write_fragment(vault, "frag-intimate", _INTIMATE_TITLE, "intimate")
    return vault


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed Machine, pool vendor named, generation and embedding pinned."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    monkeypatch.setenv(POOL_VENDOR_ENV, _VENDOR)
    pin_pool_manifest(tmp_path, monkeypatch, embedding=embedding_pin())


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[EmbeddingPool]:
    """A pinned pool serving the listing, embeddings and one generation."""
    served = EmbeddingPool(
        {
            "/v1/models": pool_listing(PINNED_MODEL, PINNED_EMBED_MODEL),
            "/v1/generate": generation(text=_TURN),
        }
    )
    served.pin(monkeypatch)
    try:
        yield served
    finally:
        served.close()


def _reflect(
    vault: Path, *, ceiling: str = "personal", extra: dict[str, str] | None = None
) -> httpx.Response:
    """POST one reflection over *vault*."""
    from starlette.testclient import TestClient

    with TestClient(build_app(vault_path=vault)) as http:
        response: httpx.Response = http.post(
            REFLECTIONS_PATH,
            json={"content": ENTRY},
            headers={**headers(ceiling=ceiling), **(extra or {})},
        )
    return response


def _paths(served: EmbeddingPool) -> list[str]:
    """Return every requested path, in order."""
    return [path for _method, path in served.requests]


def _prompt(served: EmbeddingPool) -> str:
    """Return the generation prompt the pool received."""
    for (_method, path), body in zip(
        served.requests, served._bodies_by_request(), strict=True
    ):
        if path == "/v1/generate" and isinstance(body, dict):
            return str(body["prompt"])
    msg = "no generation request was made"
    raise AssertionError(msg)


@pytest.mark.usefixtures("managed")
class TestPoolGroundedReflection:
    """The R4-4 case: keyless, managed, pool-answered — and pool-grounded."""

    def test_the_reflection_is_grounded_by_the_pool_and_labelled_truthfully(
        self, tmp_path: Path, pool: EmbeddingPool
    ) -> None:
        """Embeddings precede generation, on the one pinned destination."""
        response = _reflect(_vault(tmp_path))

        assert response.status_code == HTTP_OK, response.text
        assert response.json()["inference_source"] == f"pool:attested:{_VENDOR}"
        paths = _paths(pool)
        assert "/v1/embeddings" in paths
        assert paths.index("/v1/embeddings") < paths.index("/v1/generate")
        # Every request the vault made went to this one listener, which is
        # what makes the single published label true of the grounding too.
        assert set(paths) <= {"/v1/models", "/v1/embeddings", "/v1/generate"}
        prompt = _prompt(pool)
        assert _OPEN_TITLE in prompt
        assert _PERSONAL_TITLE in prompt

    def test_no_intimate_title_reaches_any_embeddings_request(
        self, tmp_path: Path, pool: EmbeddingPool
    ) -> None:
        """The corpus refresh is tier-blind; the chokepoint is not."""
        assert _reflect(_vault(tmp_path)).status_code == HTTP_OK

        assert _INTIMATE_TITLE not in json.dumps(pool.bodies)
        for body in pool.embedding_bodies():
            assert body["tier"] in {"open", "personal"}

    def test_the_vaults_parquet_is_never_transmitted(
        self, tmp_path: Path, pool: EmbeddingPool
    ) -> None:
        """A sentinel in the parquet appears in no request; only texts are sent."""
        vault = _vault(tmp_path)
        cache = embeddings_cache_path(vault)
        cache.parent.mkdir(parents=True, exist_ok=True)
        linker = EmbeddingLinker(EmbeddingsConfig(model=_PARQUET_SENTINEL))
        linker.save_cache({"frag-x": _sentinel_row()}, cache)

        assert _reflect(vault).status_code == HTTP_OK

        assert _PARQUET_SENTINEL not in json.dumps(pool.bodies)
        for body in pool.embedding_bodies():
            assert set(body) == {"model", "input", "tier", "deadline_ms"}
            assert all(isinstance(text, str) for text in body["input"])

    def test_an_intimate_reflection_spends_no_embeddings_dial(
        self, tmp_path: Path, pool: EmbeddingPool
    ) -> None:
        """Routed INTIMATE, the query is refused before anything is embedded."""
        _reflect(_vault(tmp_path), ceiling="intimate")

        assert "/v1/embeddings" not in _paths(pool)

    def test_a_writers_key_keeps_grounding_off_the_pool(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: EmbeddingPool
    ) -> None:
        """T10 extended: the vendor answers and the pool sees nothing at all."""
        vendor = FakeVendor(monkeypatch, turn=_TURN)

        response = _reflect(
            _vault(tmp_path),
            extra={
                PROVIDER_HEADER: "anthropic",
                PROVIDER_KEY_HEADER: "grounding-route-header-1904-never-logged",
            },
        )

        assert response.status_code == HTTP_OK, response.text
        assert response.json()["inference_source"] == "request_key:anthropic"
        assert len(vendor.requests) == 1
        assert pool.accepted == 0


def _sentinel_row() -> CachedEmbedding:
    """Return one parquet row whose model name is the wire sentinel."""
    return CachedEmbedding(
        fragment_id="frag-x",
        content_hash="h",
        model_name=_PARQUET_SENTINEL,
        vector=[1.0, 2.0, 3.0, 4.0],
        computed_at=datetime(2026, 10, 31, tzinfo=UTC),
    )


def test_with_no_attested_embedding_model_the_reflection_is_ungrounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: EmbeddingPool
) -> None:
    """Option (iii): answered, ungrounded, and no embeddings request made."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    pin_pool_manifest(tmp_path, monkeypatch)

    response = _reflect(_vault(tmp_path))

    assert response.status_code == HTTP_OK, response.text
    assert "/v1/embeddings" not in _paths(pool)
    prompt = _prompt(pool)
    assert _OPEN_TITLE not in prompt
    assert _PERSONAL_TITLE not in prompt


@pytest.mark.usefixtures("managed")
def test_the_bulk_pass_persists_pool_vectors_and_sends_no_intimate_title(
    tmp_path: Path, pool: EmbeddingPool
) -> None:
    """``creek.link`` on a managed pool vault: the pool's space, per-item tiers."""
    vault = _vault(tmp_path)

    result = link_tool(vault_path=vault, method="embeddings")

    assert result["status"] == "ok", result
    assert _INTIMATE_TITLE not in json.dumps(pool.bodies)
    pooled = EmbeddingLinker(EmbeddingsConfig())
    import pyarrow.parquet as pq

    rows = pq.read_table(embeddings_cache_path(vault)).to_pylist()
    assert {row["fragment_id"] for row in rows} == {"frag-open", "frag-personal"}
    assert {row["route"] for row in rows} == {POOL_EMBEDDING_ROUTE}
    assert {row["dimensions"] for row in rows} == {PINNED_EMBED_DIMENSIONS}
    # A local linker reading the same parquet sees none of it.
    assert pooled.load_cache(embeddings_cache_path(vault)) == {}
