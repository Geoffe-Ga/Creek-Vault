# Batch H — Operational polish and documentation alignment

## Role

You are the engineer who runs the polish pass before launch: structured logging, progress bars, timezone correctness, sensible defaults, and the documentation that keeps the codebase honest. Your changes are small, individually obvious, and collectively raise the floor on operability.

## Goal

Close the remaining 14 medium-and-low items: checkpoint/resume for LLM classification, structured logging with fragment IDs, progress reporting, the LA_TZ timezone sweep, voice-proxy-eligible cleanup, CSV encoding warning, gdrive ingestor or doc clarification, config silent-fallback warning, privacy-tier naming alignment, `purge --match substring`, ingestor count fix, clean-modules documentation, decision-pipeline docs, emergence-feature docs.

This is the launch-readiness "everything else" pass. None of the items here is structurally hard; many are doc-only. Done well, this batch shrinks the divergence between docs and code to ~zero.

## Context

Independent of every other batch, but most useful **after** Batches A–G have landed (so the documentation reflects post-fix behaviour rather than current behaviour).

Most items here come in two flavours: small focused code change, or doc update with a verification test. Where a doc change is sufficient, **also add a regression test** (or pin behaviour with an existing test) so the doc stays accurate as the code evolves.

**Read these issue files before starting** (in `plans/git-issues/`):
- `OPS-001-no-resume-checkpoint-for-classification.md`
- `OPS-003-logs-lack-fragment-ids.md`
- `OPS-004-no-progress-on-linking-and-indexing.md`
- `BUG-002-naive-datetimes-throughout-pipeline.md`
- `BUG-009-voice-proxy-eligible-flag-stale.md`
- `BUG-010-csv-cp1252-fallback-silent-mojibake.md`
- `ARCH-001-gdrive-not-an-ingestor.md`
- `ARCH-002-config-silent-fallback.md`
- `INC-003-privacy-tier-naming-divergence.md`
- `INC-008-purge-source-match-substring-flag-missing.md`
- `INC-012-twelve-vs-eleven-ingestors.md`
- `INC-013-clean-modules-undocumented.md`
- `INC-017-decision-detection-undocumented.md`
- `INC-018-paradox-tag-garden-compost-emergence.md`

**Files you will primarily change:**
- `creek-tools/creek/classify/llm.py` — checkpoint/resume; per-fragment write
- `creek-tools/creek/cli.py` — `purge source --match` flag, `--accept-defaults`, `creek init`
- `creek-tools/creek/config.py` — silent-fallback warning, `load_config(...)` plumbing
- `creek-tools/creek/models.py` — `PrivacyTier` rename / alias; remove `voice_proxy_eligible` storage in favour of derived property
- `creek-tools/creek/vault/writer.py` — naive datetime sweep
- `creek-tools/creek/link/threads.py` — naive `_now` default
- `creek-tools/creek/classify/review.py` — naive datetime sweep
- `creek-tools/creek/ingest/spreadsheets.py` — chardet probe + warning
- `creek-tools/creek/ingest/gdrive.py` — either add `GoogleDriveIngestor` or remove `--type gdrive` from docs (decide once)
- `creek-tools/creek/link/`, `creek-tools/creek/generate/` — `tqdm` progress bars
- `creek-tools/creek/time.py` (new) — `now_la()` helper to centralise LA_TZ usage
- `creek-tools/docs/{cleaning-pipeline.md,decisions.md,emergence.md}` (new) — documentation
- `creek-tools/docs/{ingestion.md,classification.md,configuration.md,README.md}` — alignment edits

## Output format

Roughly 14 commits, scoped to one issue each. Suggested order (top items unblock multiple downstream changes):

1. **`creek/time.py:now_la()`** — central LA_TZ helper.
2. **BUG-002 timezone sweep** — replace every `datetime.now()` in production code with `now_la()` (or `datetime.now(tz=UTC)` where UTC is the deliberate choice). Add a regression test that sets `TZ=Asia/Tokyo` and re-runs the suite.
3. **OPS-001 checkpoint/resume** — `LLMClassifier.classify_batch` writes each classified fragment to disk immediately; maintains `<vault>/00-Creek-Meta/Processing-Log/llm-progress.json` with completed IDs. `creek classify --resume` is the implicit default; existing-progress files are honoured automatically.
4. **OPS-003 structured logging** — sweep `creek/{classify,ingest,redact,purge}/` for `logger.{info,warning,error,exception}` calls; ensure each per-fragment failure includes the fragment ID and source path. Adopt a consistent `[fragment=… path=… provider=…]` prefix.
5. **OPS-004 progress bars** — wrap loops in `creek/link/embeddings.py`, `link/threads.py`, `link/eddies.py`, `generate/voice.py`, `generate/wavelength.py`, `generate/indexes.py` with `tqdm`, `disable=not sys.stderr.isatty()`.
6. **BUG-009 voice_proxy_eligible** — convert to `@computed_field` derived from `privacy_tier`; remove the stored field.
7. **BUG-010 CSV encoding warning** — add `chardet` probe between utf-8-sig and cp1252 fallback; emit `WARNING` log when cp1252 is used.
8. **ARCH-001 gdrive ingestor decision** — pick A (add `GoogleDriveIngestor` wrapping the downloader+router) or B (remove `--type gdrive` from CLI/docs and require users to pass per-format types after staging). Document the choice.
9. **ARCH-002 config silent fallback** — `load_config()` warns on missing config file; `creek init --vault <vault>` writes a starter config; commands that touch a vault require `--accept-defaults` if the config is missing.
10. **INC-003 privacy-tier renaming** — `PrivacyTier.OPEN = "open"`, with a one-release deprecation alias accepting `"public"` and emitting a warning.
11. **INC-008 purge source --match** — add `--source-path` and `--match {exact,substring,regex}` to `creek purge source`; engine plumbs through.
12. **INC-012 ingestor count** — README count matches `INGESTOR_REGISTRY` size; add a unit test pinning the count.
13. **INC-013 clean modules doc** — `creek-tools/docs/cleaning-pipeline.md` documenting each `creek/clean/*.py` module and its config knob.
14. **INC-017 decisions doc** — `creek-tools/docs/decisions.md` documenting the lifecycle, the §12.4 anti-manipulation guardrail, and the CLI route.
15. **INC-018 emergence doc** — verify each §10.1–10.5 criterion has matching code; document them in `creek-tools/docs/emergence.md`. File a follow-up bug for any criterion that's missing.

## Examples

A typical timezone-sweep regression test:

```python
def test_fragment_default_timestamps_are_la_tz():
    f = Fragment(title="t", source=FragmentSource(platform=SourcePlatform.OTHER))
    assert f.created.tzinfo is not None
    assert f.created.utcoffset() == ZoneInfo("America/Los_Angeles").utcoffset(f.created)


@pytest.mark.parametrize("tz_env", ["UTC", "Asia/Tokyo", "America/New_York"])
def test_thread_status_independent_of_host_tz(tz_env, monkeypatch):
    monkeypatch.setenv("TZ", tz_env)
    time.tzset()
    ...  # build fragments, run ThreadDetector, assert status
```

A logging convention check:

```python
def test_classify_failure_logs_include_fragment_id(caplog):
    fragment = make_fragment(id="frag-test12345678", title="anything")
    classifier = LLMClassifier(config=make_config())
    classifier._provider = AlwaysFailingProvider()
    with caplog.at_level(logging.ERROR):
        classifier.classify_batch([fragment])
    assert any("frag-test12345678" in r.message for r in caplog.records)
```

The ingestor-count pin:

```python
def test_ingestor_registry_size_matches_readme():
    from creek.ingest import INGESTOR_REGISTRY
    assert len(INGESTOR_REGISTRY) == 10  # update both this and README together
```

## Requirements

- **Use `/stay-green`**: each fix gets a regression test that pins the post-fix behaviour. The doc-only items still get a test that asserts the documented behaviour exists in code (e.g., a test for the synchronicity criteria from spec §10.3).
- **Use `/max-quality-no-shortcuts`**: when sweeping `datetime.now()`, fix every call site. Don't leave `# legacy, fixme` markers.
- For the privacy-tier rename (INC-003): add a one-release deprecation alias and a `DeprecationWarning`. Plan removal in the next minor version.
- For OPS-001 (checkpoint/resume): the simplest correct version is "write each fragment to disk after the LLM call returns". The progress JSON is an optimisation on top, not a prerequisite.
- For ARCH-001 (gdrive ingestor): pick option B (remove `--type gdrive` from docs and CLI) unless there's a strong user-experience reason to keep it. Option B is smaller and matches how the code actually works.
- For INC-018 (emergence-feature docs): if you find a §10 criterion that has *no* matching code path (e.g., the synchronicity "filter out 'still working on X' noise"), file it as a new BUG-* under `plans/git-issues/` rather than leaving it un-tracked.
- Maintain `mypy --strict` clean.
- Maintain ≥90% branch coverage; per-file ≥80% if Batch F's per-file gate is in place.
- Documentation lives next to the existing docs; do not create new top-level directories.

## Definition of done

`./scripts/check-all.sh` exits 0. The 14 issues are individually closed. New docs exist for cleaning-pipeline, decisions, emergence, and (from Batch G) the threat model. README, CLAUDE.md, classification, generation, configuration, and ingestion docs are aligned to code. A grep for `datetime\.now\(\)` in production code returns zero hits. Logs from a `creek classify --method llm` failure include the offending fragment ID. Progress bars appear in `creek link` and `creek report --type voice`.
