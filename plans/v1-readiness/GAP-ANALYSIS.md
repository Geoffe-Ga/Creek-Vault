# Creek-Tools v1 Readiness — Gap Analysis (Second-Order Pass)

Date: 2026-05-29
Branch: `claude/creek-tools-v1-gaps-0CnzP`
Scope: read-only gap pass against the four prod-readiness criteria
(data safety / crash recovery / doc honesty / unattended reliability).

This is the **second** v1-readiness pass. The first pass (2026-05-24, PR #323,
in this same directory's git history) filed GAP-001…GAP-011; GAP-001 through
GAP-010 were remediated in PRs #337, #341, #358, #361, #374, #377, #385, #386,
#402, #406, and I re-verified those fixes hold today (see Reconciliation).
GAP-011 (cli.py monolith) remains open and has grown. This pass looks *forward
from today's code* for what the prior pass missed, what drifted since, and what
half-built work remains. New finding IDs continue at **GAP-012** to avoid
colliding with the prior pass's read-only finding files.

---

## 1. Verdict

**Yes, with one named caveat that should be closed first.** `creek-tools` is
materially closer to a defensible personal-use v1 than at the last pass: the
prior Critical purge findings are genuinely fixed, the privacy-tier system that
was once "substantially aspirational" is now wired end-to-end and tested, the
compile layer and all generation features are fully wired, branch coverage is a
real 93.66%, and the security scan is clean. **No Critical gaps remain.** The
one finding I would close before calling this v1 for its stated use case
(trusting Creek with intimate journal content) is **GAP-012**: a scoped `creek
purge` deletes the title-only note but leaves the *full intimate body* behind in
`10-Liminal/Compost/intimate-stubs/`, so right-to-be-forgotten silently retains
the most sensitive content. Everything else is reliability/determinism polish
that can ship with eyes open.

## 2. What's genuinely solid

Brief and earned:

- **The prior Critical purge gaps are really fixed.** Embeddings rows are
  dropped on scoped purge and the cache file is deleted on vault purge
  (`creek/link/embeddings.py:157-247`); a Creek-vault marker is required before
  any destructive wipe (`creek/purge/engine.py:505-529`); an intent audit is
  written *before* destruction and an outcome (`complete`/`partial`) after, with
  a test that injects a mid-wipe `OSError` (`tests/test_purge.py:2002-2046`);
  the reference scrub now walks every `.md` file in the vault.
- **The privacy-tier system is no longer aspirational.** `PrivacyTier`
  (`creek/models.py:325-343`) is populated during classification; ingestion is
  consent-gated to `consent-log.json` and blocks the first run per source
  (`creek/cli.py:168-249`, `creek/pipeline.py:202-216`); mining, drafts, save,
  and the MCP boundary all filter by tier through a single shared function
  (`creek/classify/privacy_filter.py`, `creek_mcp/tier_ceiling.py`); tier
  overrides are audited to `privacy.jsonl`. All tested.
- **Compile + generation are fully wired, not half-built.** `creek compile`,
  weekly *and* monthly wavelength reports, index notes, the vault-data-derived
  Voice Skill Tree, `creek mine`, and `creek draft` all have CLI entry points,
  engines, and tests — including the compiled-page-missing fallback path and its
  `compile-gaps.jsonl` logging.
- **The quality-gate surface is honest now.** `check-all.sh` matches CI gate
  for gate (interrogate `--fail-under=95`, bandit `-ll`, complexity, refurb,
  tryceratops all present and aligned — prior GAP-007 closed); coverage is
  branch-based and the 93.66% aggregate is real; pip-audit is clean with a
  single justified, dated ignore.
- **System-dependency failure is mostly loud.** Ollama-unavailable raises
  `LLMProviderUnavailableError` *before* touching any fragment, with an
  actionable hint (`creek/classify/classify_engine.py:209-221, 916`); `creek
  ingest code` guards `git` with `shutil.which` before calling it.

## 3. The gap list

Ranked Critical → High → Medium. IDs encode priority within this pass.

### Critical

**None.** No data-loss or correctness failure was found. Idempotency holds
(deterministic IDs from `generate_fragment_id(source, timestamp, content)`,
`creek/ingest/base.py:322-337`), atomic writes hold, and the prior Critical
purge gaps are remediated. Said plainly so the absence is on the record.

### GAP-012 — Scoped `creek purge` leaves the full intimate body behind (High)

- **Severity:** High
- **Criterion threatened:** data safety, doc honesty
- **Evidence:** `creek save` of an `intimate` answer writes the full body to
  `10-Liminal/Compost/intimate-stubs/<slug>.md` and a title-only note carrying
  `intimate_body_pointer` (`creek/classify/privacy_filter.py:262-267`,
  `creek/save/_constants.py:15`, `creek/save/writer.py:94-102,173`). The purge
  engine never references `intimate`, `stub`, `Compost`, or
  `intimate_body_pointer` (grep of `creek/purge/` is empty) — it deletes the
  fragment's own file and *scrubs references* in `.md` files only. So `creek
  purge fragment|source|date-range` removes the title-only note but leaves the
  verbatim intimate body on disk. Only `creek purge vault` removes it (it wipes
  all of `10-Liminal/`). `README.md:20` promises purge "scrubs every reference
  along the way"; no doc discloses the surviving stub.
- **Why it matters:** RTBF is the operation a user runs to erase exactly this
  content, and the scoped variants are the natural way to do it. The most
  sensitive artifact survives, with no audit line and no warning.
- **Suggested remediation:** Have the engine follow `intimate_body_pointer` (or
  sweep orphaned `intimate-stubs/` files) on scoped purge; report the count in
  the audit; add a test; document the behavior in `cleaning-and-purge.md` and
  `--help`.
- **Files affected:** `creek/purge/engine.py`, `creek/save/writer.py`,
  `tests/test_purge.py`, `docs/cleaning-and-purge.md`, `docs/save.md`,
  `README.md`.
- **Detail:** `findings/GAP-012-intimate-stub-survives-scoped-purge.md`.

### GAP-013 — Unit suite is RED on a fresh checkout due to unpinned terminal width (High)

- **Severity:** High
- **Criterion threatened:** unattended reliability, doc honesty
- **Evidence:** `tests/test_purge.py:784` and `tests/test_pipeline.py:496`
  assert on CLI-output substrings that Rich wraps mid-word at the 80-column
  default it uses whenever stdout is not a TTY. Measured this pass:
  `./scripts/test.sh --coverage` on a fresh checkout → `2 failed, 4585 passed`
  (coverage 93.66%); `COLUMNS=200 …` → `2 passed`; CI is green at HEAD. No width
  is pinned in `conftest.py`, `ci.yml`, or `pyproject.toml`. `CLAUDE.md §2.1`
  promises a fresh checkout reproduces CI's result; it does not.
- **Why it matters:** The project's whole workflow rests on a trustworthy green
  baseline. Redirecting `check-all.sh` to a log (a non-TTY, width-80) shows a RED
  suite on untouched `main` — the precise determinism failure issue #206 set out
  to kill.
- **Suggested remediation:** Pin the console width for CLI tests (autouse
  `COLUMNS` fixture or an explicit-width `CliRunner`); audit the other 13
  `result.output`-asserting files.
- **Files affected:** `tests/conftest.py`, `tests/test_purge.py`,
  `tests/test_pipeline.py`, possibly `CLAUDE.md`.
- **Detail:** `findings/GAP-013-test-suite-nondeterministic-on-terminal-width.md`.

### GAP-014 — Pre-commit hook tool versions drift far from `uv.lock`/CI (Medium)

- **Severity:** Medium
- **Criterion threatened:** unattended reliability, doc honesty
- **Evidence:** `.pre-commit-config.yaml` pins `ruff` at **v0.2.0** while
  `uv.lock` resolves **0.15.13** (`pyproject` specifier is an unpinned
  `>=0.1.0`); `bandit` **1.7.7** vs locked **1.9.4**; `tryceratops` **v2.3.2** vs
  locked **2.4.1**. Only `mypy` is deliberately synced (v2.1.0 everywhere, per
  the comment at `.pre-commit-config.yaml:42-44`, issue #206).
- **Why it matters:** `CLAUDE.md` instructs developers to install and run
  pre-commit, and bills the scripts as a single source of truth. Ruff's
  formatter and lint rules changed enormously between 0.2.0 and 0.15.13, so a
  pre-commit run can format/flag code differently than CI's `ruff format
  --check`/`ruff check` — a contributor can be green locally via pre-commit and
  red in CI (or vice-versa). The #206 mypy-only sync left this open.
- **Suggested remediation:** Bump the ruff/bandit/tryceratops `rev`s to match
  `uv.lock`, and either pin the `pyproject` specifiers or add a check that the
  three install paths agree (as already done for mypy).
- **Files affected:** `.pre-commit-config.yaml`, `pyproject.toml`.

### GAP-015 — `images.py:is_available()` returns True without the Tesseract binary; OCR ingestion degrades to silent per-file errors (Medium)

- **Severity:** Medium
- **Criterion threatened:** unattended reliability, doc honesty
- **Evidence:** `creek/ingest/images.py:157` `is_available()` and `:166`
  `extract_text()` only catch `ImportError` for the *Python* packages. With
  `pytesseract` installed but the **`tesseract` binary absent**, `is_available()`
  returns `True`; `pytesseract.image_to_string()` (`images.py:254`) then raises
  `TesseractNotFoundError` (not an `ImportError`), so the curated
  `PytesseractUnavailableError` with the install-the-binary message
  (`images.py:184-188`) never fires. `creek/ingest/base.py:609` catches it as a
  generic per-file parse error. `README.md:17` lists "images (OCR)" as a key
  capability.
- **Why it matters:** A user who points Creek at a folder of screenshots with no
  `tesseract` installed gets *every image* recorded as a parse error rather than
  one clear "install Tesseract" message — and any caller trusting
  `is_available()` is misled. The pipeline does not crash (good), but the
  diagnostic is poor for a documented capability.
- **Suggested remediation:** Probe `shutil.which("tesseract")` (or
  `pytesseract.get_tesseract_version()`) in `is_available()`, and map
  `TesseractNotFoundError` to `PytesseractUnavailableError` in `extract_text()`.
- **Files affected:** `creek/ingest/images.py`, `tests/test_ingest*`.

### GAP-016 — Quality scripts assume an activated venv and fail with a bare, unactionable error otherwise (Medium)

- **Severity:** Medium
- **Criterion threatened:** unattended reliability
- **Evidence:** `scripts/test.sh` / `scripts/check-all.sh` invoke bare `python
  -m pytest` (etc.) with no venv-activation or `uv run` guard (grep for
  `venv|activate|uv run|VIRTUAL` returns nothing). Run with system Python they
  fail with `No module named pytest`. `dev-setup.sh` and `CLAUDE.md §2.1`
  document the intended setup, so the contract exists — but the scripts
  themselves give a generic Python error instead of "run `./scripts/dev-setup.sh`
  / activate `.venv` first."
- **Why it matters:** This is the first thing a fresh checkout hits, and it
  compounds GAP-013 in making "get to a clean green baseline" harder than the
  docs imply. Low blast radius, easy fix.
- **Suggested remediation:** Add a one-line guard at the top of the scripts that
  checks for the venv / a resolvable `pytest` and prints the actionable next
  step, or route tool invocation through `uv run`.
- **Files affected:** `scripts/test.sh`, `scripts/check-all.sh`, other
  `scripts/*.sh` that call tools directly.

## 4. Reconciliation with tracked work

- **Open issues: 0. Open PRs: 0.** Verified via the GitHub MCP against
  `geoffe-ga/creek-vault`. There is nothing to re-file against, and nothing to
  flag as mis-prioritized or as a gap-introducing open PR. Every finding above
  is therefore *untracked* by definition.
- **Prior pass GAP-001…GAP-010 — re-verified fixed today**, not re-litigated:
  embeddings row/file deletion (`embeddings.py:157-247`), intent/outcome audit
  pair (`engine.py` `_run_audited`, tested at `test_purge.py:2002-2046`), vault
  marker (`engine.py:505-529`), vault-wide reference scrub
  (`docs/cleaning-and-purge.md:73`), README capability rewrite, the
  `plans/git-issues/` dangling-reference family (now explained at
  `docs/security/threat-model.md:150` — the directory was retired in #243 and
  IDs survive as commit tags), and the check-all↔CI gate alignment.
- **Prior pass GAP-011 (cli.py monolith) — still open and worse.** It was
  2,897 lines at the last pass; it is **3,440 lines** today
  (`creek/cli.py`). Still Medium / code-health, still not a v1 blocker, but the
  trend is the wrong direction and there is no open issue tracking it.
- **Note for whoever triages this pass:** because the backlog is empty, the
  known-remaining work (GAP-011, and the prior pass's FEAT-031 authored-date
  concern) is not tracked anywhere. An empty issue tracker on a project this
  size is itself a mild reconciliation smell, not a code gap.

## 5. Out of scope

Polish noticed but consciously excluded — threatens none of the four criteria:

- `cli.py` 3,440-line monolith — code health (prior GAP-011), one line here.
- `tests/integration/` is an empty directory; `integration`-marked tests live
  inline. Cosmetic.
- `CLAUDE.md §3` ("Maximum Quality Engineering Mindset") is ~twelve paragraphs
  of culture prose that could be three. Style, not honesty.
- README "not-encrypted-at-rest" / "not tamper-evident yet — see SEC-005"
  framing is *conservative but honest*: the hash chain exists
  (`AuditLog.verify`) but does not defend against a local attacker who rewrites
  the whole chain, so the hedge is defensible. Not a finding.
- CLI ruff lint locally lacks CI's `--exit-non-zero-on-fix`; same violations
  gate either way, only auto-fixable items differ. Trivial.

## 6. Confidence and coverage

**Thoroughly assessed (ran code where possible):**

- Ran `uv sync --all-extras` and the full unit suite with coverage in this
  environment: `2 failed, 4585 passed`, branch coverage **93.66%** — both the
  red-suite finding (GAP-013) and the honest coverage number are first-hand.
- Re-traced the entire `creek purge` surface against today's code, including the
  intimate-stub path that the prior pass did not reach (GAP-012). Confirmed the
  prior GAP-001…GAP-010 remediations by reading the shipped code and tests.
- Privacy-tier system traced end-to-end across classify, ingest consent, mine,
  draft, save, and MCP, with test references — classified all sub-claims as
  fully-wired-and-tested.
- Generation + compile layer traced to CLI + engine + tests, including the
  compiled-page-missing fallback.
- Quality gates: compared `check-all.sh` to `ci.yml` line by line; verified
  branch coverage config; ran the security scan (clean); confirmed CI green at
  HEAD via the GitHub MCP; verified pre-commit/uv.lock version drift directly.
- README headline claims spot-checked to code: 11-ingestor registry,
  deterministic IDs, local-first default provider (`provider = "ollama"`),
  weekly+monthly reports.

**Partially assessed — worth a second look:**

- I dismissed an earlier lead that fragment IDs survive purge in
  `run-summary.jsonl` / `compile-gaps.jsonl`: those logs store counts and
  thread/eddy IDs respectively, **not** fragment IDs (`yield_summary.py:53-58`,
  `compile_routing.py:181-187`). If a future change adds fragment IDs to either
  log, re-open the question.
- I did not exhaustively audit all 14 `result.output`-asserting test files for
  *other* width-fragile assertions beyond the two that failed at width 80.
- The Tesseract path (GAP-015) was confirmed by reading code and the install
  state, not by running OCR on a real image with the binary truly absent
  end-to-end through the CLI.

**Did not reach:**

- The CrawDad subproject (`crawdad/`) — out of scope; its own CI jobs are green.
- A real `creek init` + Obsidian round-trip on a scaffolded vault.
- The MCP server exercised against a live client (in-memory fragment-cache
  staleness after purge remains the prior pass's open question).

The findings above stand on what I confirmed first-hand; the "second look" items
are the most likely places another gap surfaces.
