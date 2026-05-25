# Creek-Tools v1 Readiness — Gap Analysis

Date: 2026-05-24
Branch: `claude/creek-tools-v1-gaps-cWHC1`
Scope: read-only second-order review against the four prod-readiness criteria
(data safety / crash recovery / doc honesty / unattended reliability).

---

## Verdict

**Not a prod-ready v1 today — but close.** The pipeline (ingest → redact →
classify → link → compile → generate) is in genuinely good shape and would
survive normal use on real-world-messy input. The blockers are concentrated in
exactly the place a personal-use CLI cannot afford them: `creek purge`. The
right-to-be-forgotten subsystem advertises stronger guarantees than it delivers
— most visibly, the purge audit log records an `embeddings_removed` count for
embeddings that are never actually removed, and `creek purge vault` has neither
transactional protection nor a "is this actually a Creek vault?" check. Until
those three things are addressed, the README's headline "scrubbing every
reference along the way" is not honest, and `creek purge vault` is one typo
away from destroying an unrelated directory.

---

## What's genuinely solid

Briefly, and only the things that earned it:

- **Atomic writes everywhere they matter.** `creek/save/writer.py` and
  `creek/vault/writer.py` use `O_CREAT|O_EXCL` and `os.replace`, so a crash
  during a single fragment write does not leave a half-written file on disk.
- **The hash-chained audit log (`creek/audit/log.py`) is real, tested, and
  reused.** Purge, redact, and MCP audits all sit on top of it; tamper
  invalidates the chain and `AuditLog.verify` detects it.
- **The compile layer is fully wired and tested**, not the half-flight feature
  that surrounded similar projects at this stage. `mine` and `draft` route
  through it, the MCP tool exposes it, `compile-needed` gaps are logged, and
  the engine handles fenced JSON, paradox routing, and privacy-tier filtering
  with 90+ tests.
- **MCP boundary is hardened.** Tier ceilings are required on every read tool
  (`creek_mcp/tier_ceiling.py`), destructive tools require
  `CREEK_MCP_ELEVATED_TOKEN` compared with `hmac.compare_digest`, and an
  audit entry is appended before every destructive call returns.
- **The quality gate surface is mostly credible.** MyPy strict is genuinely
  strict (`pyproject.toml:190-203`), `coverage.run.branch = true` (line 168) and
  `fail_under = 90` (line 181) are both set, only two ingest modules carry
  coverage waivers (`scripts/coverage-waivers.txt`), and the
  Refurb/Tryceratops zero-violation floors really do gate the build.
- **Failure-mode fixtures exist** (`tests/test_ingest_failure_modes.py`,
  `tests/fixtures/corrupt/`, three property-based test files) and cover the
  most realistic ingest pathologies: truncated JSON, malformed YAML, mixed
  encodings, gdrive partial download. Resume-after-crash is wired and tested
  for classification (`tests/test_classify_engine_resume.py`).

---

## The gap list

Ranked Critical → High → Medium. IDs encode priority.

### GAP-001 — Purge audit log fabricates `embeddings_removed` for embeddings that were never removed

- **Severity:** Critical
- **Criterion threatened:** data safety, doc honesty
- **Evidence:**
  - `creek-tools/creek/purge/engine.py:693` sets
    `embeddings_removed=fragments_deleted` on every purge audit entry.
  - The function's own docstring at
    `creek-tools/creek/purge/engine.py:660-664` admits the value is fictional:
    *"embeddings_removed mirrors fragments_deleted because every deleted
    fragment's cached embedding is invalidated at the next `creek link` run;
    the engine does not maintain the embedding cache directly."*
  - `creek-tools/creek/link/embeddings.py:50` defines
    `EMBEDDINGS_CACHE_FILENAME = "embeddings.parquet"`. A grep for that path,
    `pyarrow`, or `parquet` inside `creek-tools/creek/purge/` returns nothing
    — purge truly never opens the cache.
  - `creek-tools/docs/security/threat-model.md:123-125` admits the cache is
    *not* automatically purged: *"Wipe the embedding cache when you wipe
    vault content. It is not automatically purged when you `creek purge
    vault`. Delete the configured cache directory manually."*
  - The threat model itself (line 98-100) flags that embeddings are
    partially invertible by an attacker with the cache file.
- **Why it matters:** The README's headline RTBF claim is "scrubbing every
  reference along the way." Per-fragment, per-source, per-daterange, and even
  vault-scope purge all leave the embeddings parquet file untouched on disk
  while writing an audit line that claims otherwise. For a user invoking RTBF
  on intimate-tier content, a compliance log that lies is worse than no log.
- **Suggested remediation:** Either (a) delete the matching rows from the
  parquet file inside the purge engine and report the actual count, or (b)
  rename the audit field to `embeddings_invalidated_at_next_link` and have
  `purge_vault` delete the parquet file outright. (b) is the smaller diff;
  (a) is the correct behavior.
- **Files affected:** `creek/purge/engine.py`, `creek/purge/audit.py`,
  `creek/link/embeddings.py`, `docs/cleaning-and-purge.md` lines 192,
  `docs/security/threat-model.md` lines 123-125, README.md line 20.

### GAP-002 — `creek purge vault` has no transactional protection and writes its audit *after* destruction

- **Severity:** Critical
- **Criterion threatened:** data safety, crash recovery
- **Evidence:**
  - `creek-tools/creek/purge/engine.py:380-397` walks
    `_VAULT_CONTENT_FOLDERS` and calls `_wipe_folder_contents` on each, then
    on line 396 calls `self._write_audit(result)`. The audit is the last
    statement, not the first.
  - `_wipe_folder_contents` (lines 646-653) iterates `sorted(folder.iterdir())`
    and unlinks each entry in place; there is no staging directory, no
    rename-to-trash, no journal.
  - If a `shutil.rmtree` or `entry.unlink` raises mid-loop (read-only file,
    permission error, OOM, container reclaim), every folder already wiped is
    permanently gone, every folder remaining is intact, and no audit entry
    is written — so the user has no record of what was destroyed.
  - `tests/test_purge.py` has 63 test functions covering audit-migration
    OSError and orphaned legacy files, but a grep for `_wipe_folder_contents`
    mid-fail shows no test simulates a crash mid-deletion.
- **Why it matters:** "Crash recovery" was one of the four criteria. The
  nuclear command has no recovery story at all — and its audit log, the only
  artifact a user could use to reconstruct what was lost, is not written on
  the crash path.
- **Suggested remediation:** Two minimum changes: (1) write a pre-destruction
  audit entry containing the *intended* scope, then a post-destruction entry
  containing the *actual* count, so a partial failure leaves a trace; (2)
  delete each folder by renaming it into a per-run `.creek-purge-staging/`
  directory inside the vault first and only `rmtree` the staging directory at
  the very end — a crash mid-rename costs at most one folder, not the rest.
- **Files affected:** `creek/purge/engine.py`,
  `tests/test_purge.py`, `docs/cleaning-and-purge.md`.

### GAP-003 — `creek purge vault` does not verify that the target path is a Creek vault

- **Severity:** Critical
- **Criterion threatened:** data safety
- **Evidence:**
  - `creek-tools/creek/cli.py:2691-2696` prompts the operator to type the
    *absolute resolved path* that the engine computed, and proceeds if the
    typed string matches. The check verifies "did the user type back what
    we printed", not "is this a Creek vault."
  - `creek-tools/creek/purge/engine.py:380-397` (`purge_vault`) begins
    wiping `_VAULT_CONTENT_FOLDERS` immediately on confirmation; it does
    not check for `00-Creek-Meta/creek_config.yaml`, `00-Creek-Meta/`, or
    any other vault-marker file.
  - `_VAULT_CONTENT_FOLDERS` (per engine.py:37-41 and the purge audit
    findings) covers `01-Fragments` through `10-Liminal`. If a user mistypes
    `--vault ~/Documents` and accepts the prompt, every `01-…` through `10-…`
    directory inside `~/Documents` (if any happen to exist) is wiped.
  - No test in `tests/test_purge.py` covers the "called against a
    non-Creek directory" case.
- **Why it matters:** A single `--vault` typo on the command line propagates
  into the confirmation prompt, because the prompt echoes the engine's
  resolved path. The interactive guard only catches a typo in the *prompt
  response*, not in the *invocation*.
- **Suggested remediation:** Refuse to start `purge_vault` unless
  `<vault>/00-Creek-Meta/creek_config.yaml` (or another distinctive marker
  set by `creek init`) exists. Print the marker path it looked for in the
  error so the user can self-correct. Add a `tests/test_purge.py` case that
  passes a `tmp_path` with no marker and asserts a `ValueError`/`typer.Exit`.
- **Files affected:** `creek/purge/engine.py`, `creek/cli.py`,
  `tests/test_purge.py`, `docs/cleaning-and-purge.md`.

### GAP-004 — Per-fragment / per-source purge skips reference scrubbing in half the vault

- **Severity:** High
- **Criterion threatened:** data safety, doc honesty
- **Evidence:**
  - `creek-tools/creek/purge/engine.py:143-190` (`_purge_single`, called by
    `purge_fragment` and `purge_source`) decrements counts in `02-Threads/`
    and `03-Eddies/` only (lines 177-184) and scrubs wikilinks via
    `_scrub_wikilinks` (line 176) which the engine restricts to those same
    two roots plus the fragment's own file.
  - The vault contains ten top-level content folders (per
    `_VAULT_CONTENT_FOLDERS`). The remaining eight —
    `04-Praxis/`, `05-Wavelength/`, `06-Frequencies/`, `07-Voice/` (incl.
    `Drafts/`), `08-Decisions/`, `09-Reference/`, `10-Liminal/`, and the
    Voice Skill Tree under `<vault>/creek-skills/` — are not scrubbed.
  - The README claim (line 20): *"`creek purge` removes a fragment, source,
    date range, or the entire vault, scrubbing every reference along the
    way."* The cleaning-and-purge doc (line 6) repeats it.
  - `docs/generation.md` shows `creek draft` writes drafts to
    `07-Voice/Drafts/` with provenance including the source fragment IDs.
    If a fragment is purged, any draft that cited it now contains a stale
    fragment ID with no scrubbing.
- **Why it matters:** RTBF is binary for the user — either the trace is
  gone or it isn't. The current contract silently exempts the folders most
  likely to carry generated/derived content of the deleted fragment.
- **Suggested remediation:** Either widen `_scrub_wikilinks` to walk every
  folder in `_VAULT_CONTENT_FOLDERS` plus `creek-skills/`, or document the
  restriction explicitly in `docs/cleaning-and-purge.md` and the
  CLI `--help` so the user knows derived content survives purge.
- **Files affected:** `creek/purge/engine.py`, `tests/test_purge.py`,
  `docs/cleaning-and-purge.md`, README.md.

### GAP-005 — Top-level README "Key capabilities" list contradicts the package README, the registry, and the threat model

- **Severity:** High
- **Criterion threatened:** doc honesty
- **Evidence:**
  - README.md:17 advertises *"Twelve source platforms … Claude, ChatGPT,
    Discord, Google Drive, code, documents (DOCX/PDF), markdown,
    spreadsheets (XLSX/CSV), presentations (PPTX), images (OCR), generic
    text, plus a fallback `other`."*
  - `creek-tools/README.md:172-176` says the truthful version: *"11
    registered Ingestors plus a read-only Google Drive downloader … The
    `other` enum value on `SourcePlatform` is reserved for downstream
    consumers (e.g. fragments synthesised from praxes) and **has no
    parser**."*
  - `creek-tools/creek/ingest/__init__.py:55-67` (per the key-capability
    audit) registers `claude`, `chatgpt`, `discord`, `code`, `document`,
    `markdown`, `spreadsheet`, `presentation`, `image`, `substack`, and
    `generic`. Substack is not listed in the top README; `other` is listed
    in the top README and is not in the registry.
  - README.md:20 promises purge "scrubs every reference along the way";
    `docs/security/threat-model.md:123-125` admits the embeddings cache is
    *not* purged. (Cross-referenced as GAP-001 / GAP-004.)
- **Why it matters:** The top-level README is the first thing a new user
  reads, and a v1 launch will be judged by it. Three of its capability
  bullets — the platform count, the `other` fallback, the "scrubs every
  reference" line — are currently false against the code as it ships.
- **Suggested remediation:** Rewrite the bullet to mirror the package
  README's honest framing (11 ingestors + gdrive downloader; substack
  named; `other` described as a *reserved enum value*, not a fallback).
  Soften the RTBF bullet to match the threat model (or fix GAP-001
  /GAP-004 and tighten the threat model instead). Replace the "Status"
  paragraph's tense to past or "complete" where applicable.
- **Files affected:** README.md.

### GAP-006 — Documentation cross-references point to a `plans/git-issues/` directory that does not exist in the repo

- **Severity:** High
- **Criterion threatened:** doc honesty, unattended reliability
- **Evidence:**
  - `creek-tools/docs/security/threat-model.md:144-152` enumerates
    `SEC-002 / SEC-005 / SEC-006 / OPS-002` and says: *"Issue files live at
    the repository root under `plans/git-issues/`. … search the issue
    tracker or the in-repo `plans/git-issues/` directory for the full
    text."*
  - `find . -path ./.git -prune -o -name "SEC-005*" -print` returns
    nothing. `find . -type d -name git-issues` returns nothing. The
    directory does not exist.
  - The corresponding GitHub repository (`geoffe-ga/creek-vault`) has 4
    open issues — none labeled `SEC-*`, `INC-*`, or matching those IDs.
    `INC-006` is referenced inline in code (`creek/link/embeddings.py:7,
    51`) and changelog but is not findable via either route from the docs.
  - `creek-tools/CLAUDE.md:5.3` ("Significant architectural decisions
    live in `docs/architecture/ADR/`") plus the cleaning-and-purge doc and
    threat-model both assume readers can reach issue text from short IDs.
- **Why it matters:** Doc honesty isn't just "are the present-tense claims
  true" — it's also "if I follow the breadcrumbs, do I land somewhere." A
  v1 user who reads the threat model, hits "see SEC-005 for the audit
  tamper-evidence story," and discovers there is nowhere to go, will
  reasonably conclude that the rigor advertised is performative.
- **Suggested remediation:** Either restore `plans/git-issues/` (or its
  successor) with the SEC-/INC- text, or rewrite the cross-references to
  point to live GitHub issue URLs, or strike the cross-references
  entirely and inline the caveat where it applies.
- **Files affected:** `docs/security/threat-model.md`,
  `docs/cleaning-and-purge.md`, code comments that reference SEC-* /
  INC-*.

### GAP-007 — `./scripts/check-all.sh` does not match CI; CLAUDE.md §1.7 promises that it does

- **Severity:** High
- **Criterion threatened:** doc honesty, unattended reliability
- **Evidence:**
  - `creek-tools/scripts/check-all.sh:101-111` runs lint → format →
    typecheck → pylint → security → complexity → refurb → tryceratops →
    test → coverage → per-file coverage → state-budget. It does **not**
    invoke `interrogate`.
  - `.github/workflows/ci.yml:109-112` runs
    `interrogate -vv --fail-under=95 creek/` as a hard gate on every CI
    job.
  - `creek-tools/scripts/security.sh:67` (per the gates audit) runs
    `bandit -r creek/` (all severities). CI at
    `.github/workflows/ci.yml:119-121` runs `bandit -r creek/ -ll`
    (medium and above only). A local pass with a low-severity finding
    that CI ignores is *less* strict than CI on docstring coverage and
    *more* strict than CI on Bandit — divergent in both directions.
  - `creek-tools/scripts/state-budget.sh:1-68` `exit 0`s silently if
    `CREEK_VAULT` is unset; CI does not invoke it at all.
  - `creek-tools/CLAUDE.md` §1.7: *"Run `./scripts/check-all.sh` before
    every commit. Only commit if exit code is 0."* and §2.1: *"a fresh
    checkout runs `./scripts/check-all.sh` to the same result CI does on
    the same commit."*
- **Why it matters:** Issue #206 was filed precisely to close this gap on
  fresh-checkout determinism, and `dev-setup.sh` addresses the install
  path. The gate-surface drift is the next link in the same chain — and
  CLAUDE.md's prose still promises a one-to-one match that does not hold.
  v1 reliability rests on this script being trustworthy.
- **Suggested remediation:** Add a `lint-interrogate.sh` (or inline the
  call) into `check-all.sh` after tryceratops. Reconcile Bandit severity
  by either passing `-ll` locally or dropping `-ll` in CI. Either invoke
  `state-budget.sh` in CI when a fixture vault is available, or document
  it as opt-in inside check-all.sh's `--help`.
- **Files affected:** `creek-tools/scripts/check-all.sh`,
  `creek-tools/scripts/security.sh`, `.github/workflows/ci.yml`,
  `creek-tools/CLAUDE.md`.

### GAP-008 — No test exercises mid-run interruption, LLM 5xx, or embedding model unavailability

- **Severity:** High
- **Criterion threatened:** crash recovery, unattended reliability
- **Evidence:**
  - `tests/test_ingest_failure_modes.py` covers truncated JSON,
    malformed YAML, empty files, mixed encodings.
    `tests/test_classify_engine_resume.py` covers
    LLM-classify resume from `llm-progress.jsonl`. `tests/test_purge.py`
    covers OSError during audit-log migration.
  - `grep -rn "KeyboardInterrupt\|SIGTERM\|SIGINT\|signal\." tests/`
    returns no result anywhere in the suite.
  - The failure-mode coverage audit confirmed: no test exercises an LLM
    5xx response (only mocked malformed JSON), no test exercises
    `sentence-transformers` failing to load (only the lazy-import
    interface tests for OCR), no test exercises an encrypted/corrupt PDF.
  - `creek-tools/creek/pipeline.py:594` is the full pipeline; it has no
    `except KeyboardInterrupt:` handler. A SIGINT mid-classify leaves the
    progress file in whatever flushed state it was in (acceptable since
    classify resumes from that file), but a SIGINT mid-link or mid-save
    is untested behavior on real data.
- **Why it matters:** Unattended reliability is the user running this
  thing overnight on a 30k-fragment Drive mirror. The most likely
  failure modes — laptop sleep, container reclaim, Anthropic API outage,
  Ollama OOM-killed — are precisely the ones not tested. The pipeline
  may well degrade gracefully; without coverage, the user finds out the
  hard way.
- **Suggested remediation:** Three minimum tests: (a) raise
  `KeyboardInterrupt` partway through `classify_engine` and assert the
  progress file is consistent; (b) mock the Anthropic client to return
  `httpx.HTTPStatusError(500)` and assert the pipeline records the
  failure and continues (or fails loud — whichever the contract says);
  (c) mock `sentence_transformers.SentenceTransformer` to raise on init
  and assert a clear actionable error, not a stack trace mid-link.
- **Files affected:** `tests/test_pipeline.py`, `tests/test_classify.py`,
  `tests/test_link.py`, `tests/test_embeddings.py`,
  `creek/pipeline.py` (if any handler is added).

### GAP-009 — Implemented features absent from user-facing docs (FEAT-022, FEAT-024, FEAT-027, FEAT-023)

- **Severity:** Medium
- **Criterion threatened:** doc honesty
- **Evidence:** `creek-tools/creek/config.py:114-151` defines
  `burst_similarity_threshold`, `exchange_max_gap_minutes`,
  `session_max_gap_minutes`, `hierarchy_sibling_skip_window`,
  `cross_source_aggregation` — all FEAT-022 / FEAT-024 / FEAT-027 knobs.
  `creek-tools/creek/config.py:171-207` defines the FEAT-023 re-atomize
  block. None of these appears in `docs/linking.md`,
  `docs/classification.md`, or `docs/configuration.md` — the docs the
  user reads to tune the pipeline.
- **Why it matters:** A user who finds the pipeline grouping things
  weirdly has no way to find the knob short of reading
  `config.py`. Not a blocker; a v1 quality-of-docs miss.
- **Suggested remediation:** Add a short "Aggregation tuning" section
  to `docs/linking.md` listing the four config keys with one-line
  semantics, and a "Re-atomization" section to `docs/classification.md`.
- **Files affected:** `creek-tools/docs/linking.md`,
  `creek-tools/docs/classification.md`,
  `creek-tools/docs/configuration.md`.

### GAP-010 — Audit log written after every destruction, not before (cross-cuts purge ops, not just vault)

- **Severity:** Medium
- **Criterion threatened:** crash recovery
- **Evidence:** Every `_write_audit(result)` call in `purge/engine.py`
  (lines 166, 189, 213, 261, 314, 355, 396) runs after the destructive
  pass completes. `purge_fragment` (lines 143-190) deletes the fragment
  file and modifies thread/eddy counts before writing the audit; a crash
  in `_scrub_wikilinks` or `_decrement_counts` leaves the fragment
  scrubbed-but-not-deleted with no audit record.
- **Why it matters:** Distinct from GAP-002 in scope — GAP-002 is the
  vault-wide nuclear case; this is every other purge op. Same root
  cause. Medium because the per-fragment blast radius is small, but it
  is the audit-log contract that compliance docs rely on.
- **Suggested remediation:** Write an "intent" audit entry before
  mutation, an "outcome" entry after; cover the gap with a test that
  injects `OSError` in `_scrub_wikilinks` and asserts both lines exist.
- **Files affected:** `creek/purge/engine.py`,
  `creek/purge/audit.py` (entry schema may need an `intent` vs
  `outcome` discriminator).

### GAP-011 — `cli.py` is a 2,897-line monolith

- **Severity:** Medium
- **Criterion threatened:** unattended reliability (indirectly — every
  bug fix touches one giant file)
- **Evidence:** `wc -l creek-tools/creek/cli.py` → 2,897. The complexity
  gate `xenon --max-absolute B` passes per `complexity.sh`, so this is a
  size-not-complexity gap. The other large module
  (`creek/config.py:795`) is dominated by Pydantic class definitions,
  which is reasonable; `cli.py` is end-to-end Typer commands and is
  routinely the file every PR has to touch.
- **Why it matters:** Half of the gaps above involve `cli.py` edits;
  the larger it is, the higher the chance of merge conflicts and
  cross-feature regressions. Not blocking.
- **Suggested remediation:** Split per command group
  (`cli/redact.py`, `cli/purge.py`, `cli/skills.py`) the way the
  domain modules are already split. Post-v1.

---

## Reconciliation with tracked work

**Open issues (4) and open PRs (2)** were reviewed via the GitHub MCP. The
open backlog is small and uniformly labeled `enhancement`. No issue is
labeled `bug`, `v1-blocker`, `blocker`, `critical`, `P0`, `security`,
`good-first-issue`, `INC-*`, `SEC-*`, `STYLE-*`, `REFACTOR-*`, `CI-*`,
`DEP-*`, `TEST-*`, or `ONTOLOGY-*` — every open severity label
referenced in the docs lives somewhere the reader cannot find (see
GAP-006).

**Candidates for mis-prioritization:**

- **#263 (FEAT-031 — Authored-date extraction contract):** Body says
  authored-date conflation is *"the root cause of behaviours that
  mislead the user"* — wrong State-report buckets, broken temporal
  linker, broken synchronicity. This reads like a correctness / data-
  integrity issue, not an enhancement. The current
  `enhancement` label understates risk. Recommend a `bug` or
  `correctness` co-label and a v1 decision on whether to ship around
  it.

**Candidates for open PRs that would introduce a v1 gap if merged:**

- **None observed.** #308 (FEAT-034 conversational consent in CrawDad)
  is additive and self-contained; #209 is a one-line skill template
  field addition. Neither touches purge semantics, vault layout, or CLI
  contracts.

**Possibly-prematurely-closed prior work:**

- **INC-006 (freshness-aware embeddings cache):** the changelog lists
  this in *Unreleased / Added*, and the code at
  `creek/link/embeddings.py` clearly persists vectors to parquet with
  content_hash freshness. The implementation is fine. The gap is that
  INC-006 was closed without updating the purge engine to actually
  delete from the cache file it now produces — that omission is
  GAP-001.
- **OPS-002 (non-interactive purge refusal):** closed; the
  cleaning-and-purge doc walks through the migration. The path-vs-
  vault-marker check (GAP-003) is a separate concern that was *not*
  part of OPS-002's stated scope. So OPS-002 is fine — but the close
  could have prompted GAP-003 as a follow-up and didn't.

---

## Out of scope

Polish that does not threaten any of the four criteria, noted only
because it surfaced during the pass:

- `cli.py` split into per-command modules — code health, not v1
  (already captured as GAP-011 medium for visibility).
- The "Status" paragraph in the top README runs in present tense
  ("Phase-3 of the implementation plan is complete: …") and lists
  things like "voice skill tree" alongside "right-to-be-forgotten
  purges" — minor tense / framing inconsistency with the rewritten
  Key Capabilities block; merge with GAP-005's fix.
- Coverage waivers reference `plans/git-issues/TEST-002-...` — same
  broken cross-reference family as GAP-006; will be fixed by the same
  remediation.
- Top-level `Creek-Vault/CLAUDE.md` references `creek-tools/CLAUDE.md`
  as authoritative; the latter's §3 ("The Maximum Quality Engineering
  Mindset") and §1.5 prose is twelve paragraphs of self-praise that
  could be three sentences without losing information. Style, not
  honesty.
- `tests/integration/` directory is empty — all tests labeled
  `integration` (per the `pytest.ini` markers) actually live alongside
  unit tests with the marker applied inline. Cosmetic.

---

## Confidence and coverage

**Thoroughly assessed:**

- The README's six "Key capabilities" claims, all traced to code with
  file:line citations (delegated agent + my own spot-checks against
  `creek/consent.py`, `creek/purge/engine.py`, `creek/audit/log.py`,
  `creek/link/embeddings.py`).
- The full `creek purge` surface (engine, audit, CLI confirmation,
  test suite) — this is the highest-stakes area and got the deepest
  pass.
- Every CI quality gate vs. its local-script counterpart vs. its
  pyproject configuration.
- All 12 specific failure scenarios in the parent prompt against the
  test suite, including a search for `KeyboardInterrupt`, `SIGTERM`,
  and `OSError` mid-pipeline.
- The compile layer (engine, provenance, CLI, MCP exposure, mining
  and drafting consumption, tests) — verdict: fully wired, not a
  half-built feature.
- Open GitHub backlog (4 issues, 2 PRs) reconciled.

**Partially assessed — worth a second look before declaring v1:**

- I did *not* run `./scripts/check-all.sh` end-to-end. Issue #206
  motivated `dev-setup.sh`; I read the resulting script but did not
  install dependencies and execute the suite. Spurious-failure
  regressions on a fresh checkout are not impossible.
- Actual aggregate branch coverage was not measured by me; I relied
  on the configured threshold (`fail_under = 90`) and CI's history of
  passing. If a CI run from this week is available, confirm.
- MCP server caching behavior on purged fragments was traced via
  `creek_mcp/tier_ceiling.py` and `tools/purge.py` but not exercised.
  If the MCP server retains an in-memory index of fragment IDs across
  requests, a purged fragment may be visible to a running MCP client
  until the server restarts — worth a 30-minute follow-up.
- The "context.mode handling of other people's content" code path
  (`ContextConfig` at `config.py:210-249`) has a validator and three
  modes; I did not trace each mode end-to-end through the pipeline.

**Did not reach:**

- The CrawDad subproject (`crawdad/`) — the prompt was scoped to
  `creek-tools`, and CrawDad has its own CI job that already passes.
- The Obsidian-side experience after a real `creek init` — I did not
  scaffold a vault and inspect what the user actually sees.
- The slash-command surface (`creek-tools/.claude/commands/`) was
  not exercised against an MCP server.
- I did not attempt to verify the README's Tech-Stack table claim
  that "Ruff" enforces zero violations *on this commit* — only that
  the gate exists.

The findings above stand on what I confirmed; the items in the second
list are the most likely places for an additional gap to surface.
