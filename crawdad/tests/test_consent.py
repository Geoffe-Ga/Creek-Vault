"""Tests for ``crawdad.consent`` — pending-batch store, classification, factories."""

from __future__ import annotations

from pathlib import Path

import pytest

from crawdad.consent import (
    DEFAULT_ABANDON_TOKENS,
    DEFAULT_CONSENT_TOKENS,
    VALID_INGEST_TYPES,
    PendingBatch,
    PendingBatchStore,
    PendingFile,
    build_pending_batch,
    classify_followup_message,
    format_type_question,
)


def _file(
    *,
    filename: str = "note.md",
    inferred_type: str | None = "markdown",
    content_hash: str = "abc",
) -> PendingFile:
    """Return a :class:`PendingFile` with sensible test defaults."""
    return PendingFile(
        filename=filename,
        original_filename=filename,
        staged_path=Path(f"/tmp/{filename}"),
        content_hash=content_hash,
        inferred_type=inferred_type,
    )


def _batch(
    *,
    files: tuple[PendingFile, ...] = (),
    channel_id: int = 999,
    created_at: float = 0.0,
    state: str = "awaiting_consent",
    ingested_hashes: frozenset[str] = frozenset(),
) -> PendingBatch:
    """Return a :class:`PendingBatch` with sensible test defaults."""
    return PendingBatch(
        channel_id=channel_id,
        staging_dir=Path("/tmp/stage"),
        files=files or (_file(),),
        privacy_tier_ceiling="personal",
        created_at=created_at,
        state=state,  # type: ignore[arg-type]
        ingested_hashes=ingested_hashes,
    )


# ---------------------------------------------------------------------------
# PendingBatch
# ---------------------------------------------------------------------------


def test_unresolved_files_returns_only_files_without_inferred_type() -> None:
    """Files with ``inferred_type=None`` are surfaced by ``unresolved_files``."""
    resolved = _file(filename="a.md", inferred_type="markdown")
    unresolved = _file(filename="weird.xyz", inferred_type=None)
    batch = _batch(files=(resolved, unresolved))

    assert batch.unresolved_files == (unresolved,)
    assert batch.needs_type_disambiguation is True


def test_unresolved_files_empty_when_every_file_has_type() -> None:
    """Fully typed batches do not need disambiguation."""
    batch = _batch(files=(_file(inferred_type="markdown"),))

    assert batch.unresolved_files == ()
    assert batch.needs_type_disambiguation is False


def test_is_expired_compares_age_against_ttl() -> None:
    """A batch older than its TTL is expired."""
    batch = _batch(created_at=0.0)

    assert batch.is_expired(now=10.0, ttl_seconds=5.0) is True
    assert batch.is_expired(now=4.0, ttl_seconds=5.0) is False
    # Boundary: exactly equal to TTL is not yet expired.
    assert batch.is_expired(now=5.0, ttl_seconds=5.0) is False


def test_with_resolved_types_fills_unresolved_only() -> None:
    """``with_resolved_types`` applies the type to ``None`` files only."""
    resolved = _file(filename="a.md", inferred_type="markdown")
    unresolved = _file(filename="weird.xyz", inferred_type=None)
    batch = _batch(files=(resolved, unresolved))

    updated = batch.with_resolved_types("document")

    assert updated.files[0].inferred_type == "markdown"  # untouched
    assert updated.files[1].inferred_type == "document"
    assert updated.needs_type_disambiguation is False


def test_with_state_returns_new_batch_with_updated_state() -> None:
    """``with_state`` produces an immutable copy with the new state."""
    batch = _batch()

    updated = batch.with_state("awaiting_type")

    assert batch.state == "awaiting_consent"
    assert updated.state == "awaiting_type"


def test_with_ingested_unions_hashes() -> None:
    """``with_ingested`` unions the new hashes into the existing set."""
    batch = _batch(ingested_hashes=frozenset({"a"}))

    updated = batch.with_ingested(frozenset({"b", "c"}))

    assert updated.ingested_hashes == frozenset({"a", "b", "c"})


def test_all_ingested_false_when_any_hash_missing() -> None:
    """Partial ingestion does not satisfy ``all_ingested``."""
    files = (
        _file(filename="a.md", content_hash="h1"),
        _file(filename="b.md", content_hash="h2"),
    )
    batch = _batch(files=files, ingested_hashes=frozenset({"h1"}))

    assert batch.all_ingested is False


def test_all_ingested_true_when_every_hash_present() -> None:
    """Complete ingestion sets ``all_ingested``."""
    files = (
        _file(filename="a.md", content_hash="h1"),
        _file(filename="b.md", content_hash="h2"),
    )
    batch = _batch(files=files, ingested_hashes=frozenset({"h1", "h2"}))

    assert batch.all_ingested is True


def test_all_ingested_false_for_empty_batch() -> None:
    """A batch with no files is never considered ingested."""
    batch = PendingBatch(
        channel_id=1,
        staging_dir=Path("/tmp"),
        files=(),
        privacy_tier_ceiling="personal",
        created_at=0.0,
    )

    assert batch.all_ingested is False


def test_resolved_groups_skips_unresolved_files_and_sorts_by_type() -> None:
    """``resolved_groups`` returns files grouped by type, alphabetically."""
    files = (
        _file(filename="img.png", inferred_type="image", content_hash="i"),
        _file(filename="a.md", inferred_type="markdown", content_hash="ma"),
        _file(filename="b.md", inferred_type="markdown", content_hash="mb"),
        _file(filename="weird.xyz", inferred_type=None, content_hash="w"),
    )
    batch = _batch(files=files)

    groups = batch.resolved_groups()

    # Sorted alphabetically by type — image then markdown.
    assert [t for t, _ in groups] == ["image", "markdown"]
    assert groups[0][1] == (files[0],)
    assert groups[1][1] == (files[1], files[2])


# ---------------------------------------------------------------------------
# PendingBatchStore
# ---------------------------------------------------------------------------


def test_store_record_and_get_round_trip() -> None:
    """A recorded batch can be retrieved by channel id."""
    store = PendingBatchStore(ttl_seconds=60.0, clock=lambda: 0.0)
    batch = _batch()

    store.record(batch)

    assert store.get(999) is batch


def test_store_get_returns_none_for_unknown_channel() -> None:
    """Unknown channel ids return ``None`` (no implicit creation)."""
    store = PendingBatchStore(clock=lambda: 0.0)

    assert store.get(404) is None


def test_store_get_evicts_expired_batches() -> None:
    """An expired batch is removed on lookup and not resurrected later."""
    clock = [0.0]
    store = PendingBatchStore(ttl_seconds=5.0, clock=lambda: clock[0])
    store.record(_batch(created_at=0.0))

    clock[0] = 10.0  # past TTL
    assert store.get(999) is None

    # Even rolling the clock back does not bring it back.
    clock[0] = 1.0
    assert store.get(999) is None


def test_store_record_overwrites_previous_batch_for_same_channel() -> None:
    """A new attachment turn supersedes the prior batch on the same channel."""
    store = PendingBatchStore(clock=lambda: 0.0)
    first = _batch(files=(_file(filename="a.md"),))
    second = _batch(files=(_file(filename="b.md"),))

    store.record(first)
    store.record(second)

    got = store.get(999)
    assert got is second


def test_store_clear_removes_the_batch() -> None:
    """``clear`` drops the stored batch; idempotent on missing channel ids."""
    store = PendingBatchStore(clock=lambda: 0.0)
    store.record(_batch())

    store.clear(999)
    store.clear(999)  # idempotent

    assert store.get(999) is None


def test_store_now_returns_the_clock_value() -> None:
    """``store.now()`` exposes the injected clock for callers to mint timestamps."""
    store = PendingBatchStore(clock=lambda: 42.0)

    assert store.now() == 42.0


def test_store_ttl_seconds_is_readable() -> None:
    """``ttl_seconds`` is exposed for tests / introspection."""
    store = PendingBatchStore(ttl_seconds=123.0, clock=lambda: 0.0)

    assert store.ttl_seconds == 123.0


def test_store_uses_monotonic_clock_by_default() -> None:
    """Production default clock is ``time.monotonic`` (smoke check)."""
    store = PendingBatchStore()

    first = store.now()
    second = store.now()
    assert second >= first


# ---------------------------------------------------------------------------
# classify_followup_message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["ingest", "Ingest", "INGEST!", "  ingest. "])
def test_classify_message_returns_consent_for_consent_tokens(text: str) -> None:
    """Affirmative tokens classify as ``consent`` regardless of case / punctuation."""
    kind, payload = classify_followup_message(text)

    assert kind == "consent"
    assert payload is None


@pytest.mark.parametrize("text", ["yes", "go ahead", "proceed", "ok", "Sure!"])
def test_classify_message_handles_full_default_consent_set(text: str) -> None:
    """Every documented consent token is recognised."""
    kind, _ = classify_followup_message(text)

    assert kind == "consent"


@pytest.mark.parametrize("text", ["cancel", "drop", "nevermind", "never mind", "ABORT"])
def test_classify_message_returns_abandon_for_abandon_tokens(text: str) -> None:
    """Abandonment tokens classify as ``abandon``."""
    kind, payload = classify_followup_message(text)

    assert kind == "abandon"
    assert payload is None


@pytest.mark.parametrize("text", ["markdown", "Markdown", "document!", "  image  "])
def test_classify_message_returns_type_for_valid_ingest_types(text: str) -> None:
    """Valid ingest-type names classify as ``type`` and surface the payload."""
    kind, payload = classify_followup_message(text)

    assert kind == "type"
    assert payload in VALID_INGEST_TYPES


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "hello world",
        "what's surfacing in my vault today?",
        "yes please go ahead",  # extra words → not exact match
    ],
)
def test_classify_message_returns_none_for_unrelated_text(text: str) -> None:
    """Anything that doesn't normalise to a known token falls through."""
    kind, payload = classify_followup_message(text)

    assert kind == "none"
    assert payload is None


def test_classify_message_type_match_wins_over_consent_token_set() -> None:
    """A type word never falls into the consent bucket by mistake."""
    # ``markdown`` is not in the default consent token set; this guards
    # against a future operator widening the consent set to include a
    # type name by accident — the type branch must still win.
    kind, payload = classify_followup_message(
        "markdown",
        consent_tokens=frozenset({"markdown", "ingest"}),
    )

    assert kind == "type"
    assert payload == "markdown"


def test_classify_message_with_custom_token_sets() -> None:
    """Operators can override the default token sets."""
    kind, _ = classify_followup_message(
        "do it now",
        consent_tokens=frozenset({"do it now"}),
        abandon_tokens=frozenset(),
    )

    assert kind == "consent"


def test_default_token_sets_are_lowercase_and_non_empty() -> None:
    """Sanity-check the bundled defaults: lowercase, non-empty, no overlap."""
    for token in DEFAULT_CONSENT_TOKENS:
        assert token == token.lower()
    for token in DEFAULT_ABANDON_TOKENS:
        assert token == token.lower()
    assert DEFAULT_CONSENT_TOKENS.isdisjoint(DEFAULT_ABANDON_TOKENS)


# ---------------------------------------------------------------------------
# format_type_question + build_pending_batch
# ---------------------------------------------------------------------------


def test_format_type_question_lists_unresolved_filenames_and_valid_types() -> None:
    """The disambiguation question mentions each unresolved file and every type."""
    files = (
        _file(filename="report.xyz", inferred_type=None),
        _file(filename="notes.qux", inferred_type=None),
        _file(filename="resolved.md", inferred_type="markdown"),
    )
    batch = _batch(files=files)

    question = format_type_question(batch)

    assert "`report.xyz`" in question
    assert "`notes.qux`" in question
    # Resolved file is not in the question.
    assert "resolved.md" not in question
    for t in VALID_INGEST_TYPES:
        assert t in question
    # Plural agreement: two unresolved files → "are"
    assert "are" in question
    assert "cancel" in question


def test_format_type_question_uses_singular_verb_for_one_unresolved_file() -> None:
    """Singular grammar when only one file needs disambiguation."""
    batch = _batch(files=(_file(filename="report.xyz", inferred_type=None),))

    question = format_type_question(batch)

    # Singular verb agreement: " is " (with spaces to avoid matching "is" in "list").
    assert " is " in question


def test_build_pending_batch_starts_in_awaiting_consent_state() -> None:
    """The factory always returns a freshly-recorded ``awaiting_consent`` batch."""
    files = (_file(),)

    batch = build_pending_batch(
        channel_id=42,
        staging_dir=Path("/vault/inbound/42/100"),
        accepted_files=files,
        privacy_tier_ceiling="intimate",
        now=99.0,
    )

    assert batch.channel_id == 42
    assert batch.staging_dir == Path("/vault/inbound/42/100")
    assert batch.files == files
    assert batch.privacy_tier_ceiling == "intimate"
    assert batch.created_at == 99.0
    assert batch.state == "awaiting_consent"
    assert batch.ingested_hashes == frozenset()
