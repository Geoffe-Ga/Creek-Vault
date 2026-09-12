"""Consumer-scoped journal withdrawal over ``DELETE /v1`` (#1799).

Withdrawal is the destructive inverse of journal upsert, not a shorthand for
deleting the staged markdown file.  A complete response means the source copy,
fragment, ingest-ledger membership, references, and retrieval-facing state are
all gone.  The operation is idempotent, carries no request prose, and treats an
identifier owned by another authenticated consumer exactly like an absent one.

These tests deliberately drive the public HTTP surface.  The purge engine has
its own exhaustive unit suite; this module proves the adapter selects the right
consumer-owned source, delegates to that erasure primitive, refuses to
over-claim partial work, and publishes the new contract honestly.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import frontmatter

import creek.link.embeddings as embeddings_module
from creek._fslock import vault_lock
from creek.config import EmbeddingsConfig
from creek.link.embeddings import (
    CachedEmbedding,
    EmbeddingLinker,
    embeddings_cache_path,
)
from creek.purge.engine import PurgeEngine, PurgeResult
from creek.vault.writer import INDEX_FILENAME, VaultWriter
from creek_mcp.api.models import ErrorCode
from creek_mcp.api.openapi import build_openapi
from creek_mcp.httpapi import journal as journal_http
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.journal import (
    journal_ingest_tool,
    journal_mutation_lock_path,
)
from tests.v1_api_support import (
    CONSUMER,
    OTHER_CONSUMER,
    OTHER_TOKEN,
    STRONG_TOKEN,
    client,
    envelope,
    headers,
    seed_vault,
    verifier,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    import httpx
    import pytest

_OK: Final[int] = 200
_INVALID: Final[int] = 422
_INCOMPATIBLE: Final[int] = 409
_RETRYABLE: Final[int] = 503
_CURRENT_MINOR: Final[str] = "0.16"
_PREVIOUS_MINOR: Final[str] = "0.15"
_EXTERNAL_ID: Final[str] = "adepthood:entry:withdrawal-contract-1799"
_CONTENT: Final[str] = "synthetic withdrawal body 1799 that must not survive"
_TIMESTAMP: Final[str] = "2026-09-11T12:34:56+00:00"
_PATH: Final[str] = f"/v1/journal-entries/{_EXTERNAL_ID}"
_EXPECTED_SUCCESS: Final[dict[str, str]] = {
    "status": "ok",
    "tier_ceiling": "personal",
    "action": "withdrawn",
}


def _vault(tmp_path: Path) -> Path:
    """Return one scaffolded vault suitable for the HTTP vertical."""
    return seed_vault(tmp_path / "vault")


def _put(
    test_client: httpx.Client,
    *,
    token: str = STRONG_TOKEN,
    external_id: str = _EXTERNAL_ID,
    content: str = _CONTENT,
) -> httpx.Response:
    """Create one personal journal entry through the public adapter."""
    return test_client.put(
        f"/v1/journal-entries/{external_id}",
        json={"content": content, "timestamp": _TIMESTAMP, "tier": "personal"},
        headers=headers(token=token, minor=_CURRENT_MINOR, ceiling="personal"),
    )


def _delete(
    test_client: httpx.Client,
    *,
    token: str = STRONG_TOKEN,
    external_id: str = _EXTERNAL_ID,
    minor: str = _CURRENT_MINOR,
    ceiling: str | None = "personal",
    content: bytes | None = None,
) -> httpx.Response:
    """Withdraw one journal id with explicitly controlled contract headers."""
    return test_client.request(
        "DELETE",
        f"/v1/journal-entries/{external_id}",
        headers=headers(token=token, minor=minor, ceiling=ceiling),
        content=content,
    )


def _markdown_files(vault: Path) -> list[Path]:
    """Return every markdown file under *vault*, sorted for stable diagnostics."""
    return sorted(vault.rglob("*.md"))


def _audit_text(vault: Path) -> str:
    """Return all persistent audit bytes as text for a disclosure sweep."""
    audit_root = vault / "00-Creek-Meta" / "audit"
    if not audit_root.exists():
        return ""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(audit_root.rglob("*"))
        if path.is_file()
    )


def _provenance(vault: Path) -> list[dict[str, object]]:
    """Return the operational provenance chain as parsed records."""
    path = vault / "00-Creek-Meta" / "Processing-Log" / "provenance.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _index_text(vault: Path) -> str:
    """Return every persistent fragment-index record, including dotfiles."""
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(vault.rglob(INDEX_FILENAME))
    )


def _fragment(vault: Path) -> Path:
    """Return the one journal fragment created by :func:`_put`."""
    fragments = sorted((vault / "01-Fragments").rglob("*.md"))
    assert len(fragments) == 1
    return fragments[0]


def _seed_embedding(vault: Path, fragment_id: str) -> dict[str, CachedEmbedding]:
    """Persist one real cache row and return the stale snapshot a linker holds."""
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    entries = {
        fragment_id: CachedEmbedding(
            fragment_id=fragment_id,
            content_hash="0" * 64,
            model_name="all-MiniLM-L6-v2",
            vector=[0.1, 0.2, 0.3],
            computed_at=datetime(2026, 9, 11, tzinfo=UTC),
        )
    }
    EmbeddingLinker(EmbeddingsConfig()).save_cache(entries, cache_path)
    return entries


def test_current_contract_advertises_withdrawal_and_previous_minor_does_not(
    tmp_path: Path,
) -> None:
    """Capability negotiation and route admission share the 0.16 boundary."""
    vault = _vault(tmp_path)
    with client(vault_path=vault) as test_client:
        current = test_client.get(
            "/v1/capabilities",
            headers=headers(minor=_CURRENT_MINOR, ceiling="personal"),
        )
        previous = test_client.get(
            "/v1/capabilities",
            headers=headers(minor=_PREVIOUS_MINOR, ceiling="personal"),
        )
        refused = _delete(test_client, minor=_PREVIOUS_MINOR)

    assert current.status_code == _OK
    assert "journal-withdraw" in envelope(current)["capabilities"]
    assert previous.status_code == _OK
    assert "journal-withdraw" not in envelope(previous)["capabilities"]
    assert refused.status_code == _INCOMPATIBLE
    assert envelope(refused)["code"] == ErrorCode.INCOMPATIBLE_VERSION.value


def test_withdrawal_removes_source_fragment_ledger_and_references(
    tmp_path: Path,
) -> None:
    """A complete response leaves no primary or reference-bearing markdown."""
    vault = _vault(tmp_path)
    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        fragment_id = str(envelope(created)["fragment_id"])
        fragment = _fragment(vault)
        title = str(frontmatter.load(fragment).get("title", fragment.stem))
        derived = vault / "02-Threads" / "withdrawal-reference.md"
        derived.write_text(
            "---\n"
            "id: thread-withdrawal-1799\n"
            f"source_fragments: [{fragment_id}]\n"
            "fragment_count: 1\n"
            "---\n\n"
            f"[[{title}]]\n\n{fragment_id}\n",
            encoding="utf-8",
        )

        withdrawn = _delete(test_client)
        wheel = test_client.get(
            "/v1/wheel",
            headers=headers(minor=_CURRENT_MINOR, ceiling="personal"),
        )

    assert withdrawn.status_code == _OK
    assert envelope(withdrawn) == _EXPECTED_SUCCESS
    assert not any((vault / "01-Fragments").rglob("*.md"))
    assert not any((vault / "00-Creek-Meta/adepthood/journal").rglob("*.md"))
    assert fragment_id not in derived.read_text(encoding="utf-8")
    ledger_root = vault / "00-Creek-Meta" / "State" / "ingest"
    assert all(
        fragment_id not in path.read_text(encoding="utf-8")
        for path in ledger_root.rglob("*.jsonl")
    )
    assert fragment_id not in _index_text(vault)
    provenance = [row for row in _provenance(vault) if row.get("id") == fragment_id]
    assert provenance[-1]["type"] == "withdrawal"
    assert "path" not in provenance[-1]
    assert wheel.status_code == _OK
    assert envelope(wheel)["total_classified"] == 0
    assert all(item["count"] == 0 for item in envelope(wheel)["wheel"].values())


def test_repeated_and_absent_withdrawals_have_the_same_stable_success(
    tmp_path: Path,
) -> None:
    """Delete is idempotent and does not disclose whether the id ever existed."""
    vault = _vault(tmp_path)
    with client(vault_path=vault) as test_client:
        assert _put(test_client).status_code == _OK
        first = _delete(test_client)
        second = _delete(test_client)
        absent = _delete(test_client, external_id="never-present-1799")

    assert first.status_code == second.status_code == absent.status_code == _OK
    assert envelope(first) == envelope(second) == envelope(absent) == _EXPECTED_SUCCESS


def test_another_consumer_sees_absence_and_cannot_withdraw_the_owner_entry(
    tmp_path: Path,
) -> None:
    """The same external-id text is scoped by authenticated consumer identity."""
    vault = _vault(tmp_path)
    configured = verifier(
        {
            CONSUMER: (STRONG_TOKEN,),
            OTHER_CONSUMER: (OTHER_TOKEN,),
        }
    )
    with client(vault_path=vault, verifier=configured) as test_client:
        owner_write = _put(test_client)
        assert owner_write.status_code == _OK
        owner_fragment = str(envelope(owner_write)["fragment_id"])

        foreign = _delete(test_client, token=OTHER_TOKEN)
        genuinely_absent = _delete(
            test_client,
            token=OTHER_TOKEN,
            external_id="never-present-for-other-consumer",
        )
        assert _fragment(vault).exists()

        owner = _delete(test_client)

    assert foreign.status_code == genuinely_absent.status_code == _OK
    assert envelope(foreign) == envelope(genuinely_absent) == _EXPECTED_SUCCESS
    assert owner.status_code == _OK
    assert owner_fragment not in "\n".join(
        path.read_text(encoding="utf-8") for path in _markdown_files(vault)
    )


def test_two_consumers_may_use_the_same_external_id_without_collision(
    tmp_path: Path,
) -> None:
    """Consumer scoping namespaces writes as well as withdrawal lookup."""
    vault = _vault(tmp_path)
    configured = verifier(
        {
            CONSUMER: (STRONG_TOKEN,),
            OTHER_CONSUMER: (OTHER_TOKEN,),
        }
    )
    with client(vault_path=vault, verifier=configured) as test_client:
        first = _put(test_client, content="consumer one private journal body")
        second = _put(
            test_client,
            token=OTHER_TOKEN,
            content="consumer two private journal body",
        )
        assert first.status_code == second.status_code == _OK
        first_id = str(envelope(first)["fragment_id"])
        second_id = str(envelope(second)["fragment_id"])

        assert _delete(test_client).status_code == _OK

    remaining = "\n".join(
        path.read_text(encoding="utf-8") for path in _markdown_files(vault)
    )
    assert first_id != second_id
    assert first_id not in remaining
    assert second_id in remaining
    assert "consumer one private journal body" not in remaining
    assert "consumer two private journal body" in remaining


def test_single_consumer_can_withdraw_a_pre_016_legacy_entry(tmp_path: Path) -> None:
    """The contract upgrade can erase entries written at the legacy root path."""
    vault = _vault(tmp_path)
    legacy = journal_ingest_tool(
        vault_path=vault,
        content=_CONTENT,
        external_id=_EXTERNAL_ID,
        timestamp=_TIMESTAMP,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer=CONSUMER,
    )
    assert legacy["status"] == "ok"
    configured = verifier({CONSUMER: (STRONG_TOKEN,)})

    with client(vault_path=vault, verifier=configured) as test_client:
        response = _delete(test_client)

    assert response.status_code == _OK
    assert envelope(response) == _EXPECTED_SUCCESS
    assert _CONTENT not in "\n".join(
        path.read_text(encoding="utf-8") for path in _markdown_files(vault)
    )


def test_multi_consumer_server_does_not_guess_an_owner_for_a_legacy_entry(
    tmp_path: Path,
) -> None:
    """Unscoped legacy data stays inaccessible when ownership is ambiguous."""
    vault = _vault(tmp_path)
    legacy = journal_ingest_tool(
        vault_path=vault,
        content=_CONTENT,
        external_id=_EXTERNAL_ID,
        timestamp=_TIMESTAMP,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer=CONSUMER,
    )
    assert legacy["status"] == "ok"

    with client(vault_path=vault) as test_client:
        owner_like = _delete(test_client)
        other = _delete(test_client, token=OTHER_TOKEN)

    assert owner_like.status_code == other.status_code == _OK
    assert envelope(owner_like) == envelope(other) == _EXPECTED_SUCCESS
    assert _fragment(vault).exists()


def test_partial_purge_is_retryable_and_never_reported_as_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial engine result returns 503; a later retry completes safely."""
    vault = _vault(tmp_path)
    original = PurgeEngine.purge_fragment
    calls = 0

    def partial_once(engine: PurgeEngine, fragment_id: str) -> PurgeResult:
        """Simulate one truthful derived-artifact shortfall, then recover."""
        nonlocal calls
        calls += 1
        if calls == 1:
            return PurgeResult(
                operation="fragment",
                target=fragment_id,
                criteria={"fragment_id": fragment_id},
                voice_body_undecodable=[fragment_id],
            )
        return original(engine, fragment_id)

    monkeypatch.setattr(PurgeEngine, "purge_fragment", partial_once)
    with client(vault_path=vault) as test_client:
        assert _put(test_client).status_code == _OK
        partial = _delete(test_client)
        retried = _delete(test_client)

    assert partial.status_code == _RETRYABLE
    assert envelope(partial)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert retried.status_code == _OK
    assert envelope(retried) == _EXPECTED_SUCCESS
    assert _CONTENT not in "\n".join(
        path.read_text(encoding="utf-8") for path in _markdown_files(vault)
    )


def test_provenance_tombstone_failure_is_retryable_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary erasure without its membership tombstone is never complete."""
    vault = _vault(tmp_path)
    original = journal_http._tomb_provenance
    calls = 0

    def fail_once(path: Path, fragment_ids: list[str]) -> None:
        """Interrupt the first provenance append, then permit exact retry."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic provenance interruption")
        original(path, fragment_ids)

    monkeypatch.setattr(journal_http, "_tomb_provenance", fail_once)
    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        fragment_id = str(envelope(created)["fragment_id"])
        interrupted = _delete(test_client)
        retried = _delete(test_client)

    assert interrupted.status_code == _RETRYABLE
    assert envelope(interrupted)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert retried.status_code == _OK
    assert envelope(retried) == _EXPECTED_SUCCESS
    provenance = [row for row in _provenance(vault) if row.get("id") == fragment_id]
    assert provenance[-1]["type"] == "withdrawal"


def test_index_compaction_failure_is_retryable_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale live index mapping makes erasure incomplete until retry."""
    vault = _vault(tmp_path)
    original = VaultWriter.compact_index
    calls = 0

    def fail_once(writer: VaultWriter, target_dir: Path) -> int:
        """Interrupt the first index compaction, then permit exact retry."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic index interruption")
        return original(writer, target_dir)

    monkeypatch.setattr(VaultWriter, "compact_index", fail_once)
    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        fragment_id = str(envelope(created)["fragment_id"])
        interrupted = _delete(test_client)
        retried = _delete(test_client)

    assert interrupted.status_code == _RETRYABLE
    assert envelope(interrupted)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert retried.status_code == _OK
    assert envelope(retried) == _EXPECTED_SUCCESS
    assert fragment_id not in _index_text(vault)


def test_a_false_complete_result_is_refused_while_plaintext_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postcondition verification defeats a lying or regressed purge primitive."""
    vault = _vault(tmp_path)

    def false_complete(_engine: PurgeEngine, fragment_id: str) -> PurgeResult:
        """Return complete without touching the addressed vault."""
        return PurgeResult(
            operation="fragment",
            target=fragment_id,
            criteria={"fragment_id": fragment_id},
        )

    monkeypatch.setattr(PurgeEngine, "purge_fragment", false_complete)
    with client(vault_path=vault) as test_client:
        assert _put(test_client).status_code == _OK
        response = _delete(test_client)

    assert response.status_code == _RETRYABLE
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert _CONTENT in _fragment(vault).read_text(encoding="utf-8")


def test_embedding_membership_is_a_required_success_postcondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two lying cache scrub seams cannot turn a surviving vector into 200."""
    vault = _vault(tmp_path)
    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        fragment_id = str(envelope(created)["fragment_id"])
        _seed_embedding(vault, fragment_id)

        def remove_nothing(
            _path: Path,
            _fragment_ids: object,
            *,
            dry_run: bool = False,
        ) -> int:
            """Claim no matching rows without rewriting the real parquet."""
            del dry_run
            return 0

        monkeypatch.setattr(
            embeddings_module,
            "purge_fragment_ids_from_cache",
            remove_nothing,
        )
        monkeypatch.setattr(
            journal_http,
            "purge_fragment_ids_from_cache",
            remove_nothing,
        )
        response = _delete(test_client)

    assert response.status_code == _RETRYABLE
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    cached = EmbeddingLinker(EmbeddingsConfig()).load_cache(
        embeddings_cache_path(vault)
    )
    assert fragment_id in cached


def test_an_unreadable_cache_cannot_be_claimed_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opaque retrieval bytes leave the recovery marker and return 503."""
    vault = _vault(tmp_path)
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    def remove_nothing(*_args: object, **_kwargs: object) -> int:
        """Leave the cache unchanged while claiming no row matched."""
        return 0

    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        cache_path.write_bytes(b"not a parquet file")

        # Isolate the success postcondition: model a row scrubber that reports
        # no match while leaving bytes whose membership cannot be inspected.
        monkeypatch.setattr(
            embeddings_module,
            "purge_fragment_ids_from_cache",
            remove_nothing,
        )
        monkeypatch.setattr(
            journal_http,
            "purge_fragment_ids_from_cache",
            remove_nothing,
        )
        response = _delete(test_client)

    assert response.status_code == _RETRYABLE
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert cache_path.read_bytes() == b"not a parquet file"
    recovery = vault / "00-Creek-Meta" / "adepthood" / "journal-withdrawals"
    assert list(recovery.glob("*.json"))


def test_a_concurrent_stale_cache_save_cannot_resurrect_a_withdrawn_row(
    tmp_path: Path,
) -> None:
    """A linker snapshot taken before DELETE stays unable to restore its row."""
    vault = _vault(tmp_path)
    cache_path = embeddings_cache_path(vault)
    linker = EmbeddingLinker(EmbeddingsConfig())
    ready = threading.Event()
    resume = threading.Event()
    errors: list[BaseException] = []

    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        fragment_id = str(envelope(created)["fragment_id"])
        _seed_embedding(vault, fragment_id)
        stale = linker.load_cache(cache_path)
        assert fragment_id in stale

        def save_stale_snapshot() -> None:
            """Model a live linker paused after its pre-withdraw cache load."""
            try:
                ready.set()
                if not resume.wait(timeout=5):
                    raise AssertionError("withdrawal did not release stale saver")
                linker.save_cache(stale, cache_path)
            except BaseException as exc:  # captured and re-raised below
                errors.append(exc)

        saver = threading.Thread(target=save_stale_snapshot)
        saver.start()
        assert ready.wait(timeout=5)
        assert _delete(test_client).status_code == _OK
        resume.set()
        saver.join(timeout=5)

    assert not saver.is_alive()
    assert errors == []
    assert fragment_id not in EmbeddingLinker(EmbeddingsConfig()).load_cache(cache_path)


def test_a_later_reingest_clears_the_cache_withdrawal_tombstone(
    tmp_path: Path,
) -> None:
    """The same stable identity becomes cacheable after a later journal write."""
    vault = _vault(tmp_path)
    cache_path = embeddings_cache_path(vault)
    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        original_id = str(envelope(created)["fragment_id"])
        assert _delete(test_client).status_code == _OK
        recreated = _put(test_client)
        assert recreated.status_code == _OK
        fragment_id = str(envelope(recreated)["fragment_id"])
        assert fragment_id == original_id

    _seed_embedding(vault, fragment_id)

    assert fragment_id in EmbeddingLinker(EmbeddingsConfig()).load_cache(cache_path)


def test_withdrawal_lock_contention_is_retryable_and_changes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy shared PUT/DELETE lock earns a bounded 503, never partial work."""
    vault = _vault(tmp_path)
    real_vault_lock = vault_lock

    def impatient_lock(path: Path) -> AbstractContextManager[None]:
        """Keep the production lock implementation but shorten this fault wait."""
        return real_vault_lock(path, timeout=0.05)

    monkeypatch.setattr(journal_http, "vault_lock", impatient_lock)
    with client(vault_path=vault) as test_client:
        created = _put(test_client)
        assert created.status_code == _OK
        fragment_id = str(envelope(created)["fragment_id"])
        with real_vault_lock(journal_mutation_lock_path(vault), timeout=1):
            response = _delete(test_client)

    assert response.status_code == _RETRYABLE
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert _fragment(vault).exists()
    assert fragment_id in _index_text(vault)
    assert _CONTENT in _fragment(vault).read_text(encoding="utf-8")


def test_withdrawal_requires_an_explicit_ceiling_and_accepts_no_body(
    tmp_path: Path,
) -> None:
    """The new destructive call cannot silently default policy or ingest prose."""
    vault = _vault(tmp_path)
    with client(vault_path=vault) as test_client:
        assert _put(test_client).status_code == _OK
        missing_ceiling = _delete(test_client, ceiling=None)
        prose = _delete(test_client, content=b'{"content":"do not accept me"}')

    assert missing_ceiling.status_code == _INVALID
    assert envelope(missing_ceiling)["code"] == ErrorCode.INVALID_REQUEST.value
    assert prose.status_code == _INVALID
    assert envelope(prose)["code"] == ErrorCode.INVALID_REQUEST.value
    assert _fragment(vault).exists()


def test_audits_persist_neither_external_id_nor_journal_content(tmp_path: Path) -> None:
    """Both upsert and withdrawal trails use bounded facts, never caller prose."""
    vault = _vault(tmp_path)
    with client(vault_path=vault) as test_client:
        assert _put(test_client).status_code == _OK
        assert _delete(test_client).status_code == _OK

    audit = _audit_text(vault)
    assert audit
    assert _EXTERNAL_ID not in audit
    assert _CONTENT not in audit
    for line in (line for line in audit.splitlines() if line):
        json.loads(line)


def test_openapi_publishes_a_body_free_closed_withdrawal_shape() -> None:
    """The generated contract names DELETE, no request body, and three facts."""
    operation = build_openapi()["paths"]["/v1/journal-entries/{external_id}"]["delete"]
    success = operation["responses"]["200"]
    schema = success["content"]["application/json"]["schema"]

    assert "requestBody" not in operation
    assert schema == {"$ref": "#/components/schemas/JournalWithdrawResponse"}
    model = build_openapi()["components"]["schemas"]["JournalWithdrawResponse"]
    assert model["additionalProperties"] is False
    assert set(model["required"]) == {"status", "tier_ceiling", "action"}
    assert "external_id" not in json.dumps(model)
    assert "content" not in json.dumps(model)
