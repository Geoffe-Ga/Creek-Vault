# Claude Code Project Context: crawdad

CrawDad is a Discord bot that consumes the creek-tools MCP surface.
It is a sibling of `creek-tools/`, not nested under it (FEAT-013
§Pre-decided choices).

## 1. Critical principles

These mirror `creek-tools/CLAUDE.md` at a smaller scale:

1. **Use project scripts, not direct tools.** Always invoke
   `./scripts/*.sh` rather than calling `ruff`, `mypy`, or `pytest`
   directly. Scripts ensure CI parity.
2. **No shortcuts.** Never add `# type: ignore`, `# noqa`, or
   `--no-verify` without an issue reference. Fix root causes.
3. **Stay green.** Never push, request review, or merge with red checks.
   See [4. Stay Green Workflow](#4-stay-green-workflow).
4. **Quality bar is non-negotiable.**
   - Branch coverage ≥ 90%
   - Docstring coverage ≥ 95% (`interrogate`)
   - Cyclomatic complexity ≤ 10 per function (Xenon `--max-absolute B`)
   - MyPy strict mode, zero violations
   - Ruff lint + format, zero violations
   - Bandit, zero medium-or-above findings
   - pip-audit, zero known vulnerabilities across both the installed
     environment and the exported `uv.lock` — no suppression without
     a tracked issue

## 2. Project overview

CrawDad is the Discord-side interface to a Creek vault. v1.0 is
complete and ships:

- Connection to `creek-tools-mcp` (FEAT-010/011/012) over stdio.
- The two-LLM agent loop: Haiku router (FEAT-014) → MCP dispatcher →
  Sonnet composer (FEAT-015), capped at `MAX_LOOP_ROUNDS` (default 5;
  operator-configurable via `crawdad.yaml::max_loop_rounds`, bounded
  `[1, 50]` — FEAT-036) with paradox auto-routing to
  `10-Liminal/Paradoxes/`.
- Voice-skill activation per session from `<vault>/creek-skills/`
  (voice-core + phase + register).
- Seven `/crawdad` Discord slash commands (FEAT-016 + FEAT-029 +
  ADAPT-003): `reflect`, `checkin`, `surface`, `draft`, `save`,
  `register`, `workflow`. `workflow` supports `list` (enumerate) and
  `run <name>` (deterministic walk over a YAML file) — see
  `crawdad/crawdad/workflows.py` and
  `docs/adr/2026-05-24_workflow-file-location.md`.
- A user + channel allowlist; non-allowlisted users get *no* response
  (silent ignore, per FEAT-013's personal-use scoping).
- Session-state load from `<vault>/00-Creek-Meta/State/latest.md` at
  startup.

## 3. Layout

```
crawdad/
├── crawdad/                # Source package
│   ├── __init__.py
│   ├── bot.py              # discord.Client subclass + pure-logic handler
│   ├── cli.py              # `crawdad run` entry point + agent components
│   ├── composer.py         # Sonnet composer (FEAT-015)
│   ├── config.py           # Pydantic config (env secrets + YAML file)
│   ├── dispatcher.py       # Intent → MCP tool dispatcher (FEAT-014)
│   ├── history.py          # Conversation history with bounded truncation
│   ├── intents.py          # Pydantic intent schema + builder
│   ├── loop.py             # 5-round agent loop (FEAT-015)
│   ├── mcp_client.py       # async MCP stdio wrapper
│   ├── router.py           # Haiku intent router (FEAT-014)
│   ├── skill_loader.py     # Voice-skill stack loader (FEAT-015)
│   ├── slash_commands.py   # /crawdad Discord slash commands (FEAT-016)
│   ├── state.py            # load_session_state — latest.md parser
│   ├── workflows.py        # Authored workflow DSL (ADAPT-003)
│   └── builtin_workflows/  # Reference workflows shipped with the package
├── tests/                  # One test file per source module
├── scripts/                # check-all, lint, test, etc.
├── docs/
├── CLAUDE.md
├── README.md
└── pyproject.toml
```

## 4. Stay Green workflow

Four gates, each must pass before the next:

1. **TDD.** Write the failing test first. Tests pin acceptance criteria.
2. **Local checks.** `./scripts/check-all.sh` exits 0.
3. **CI.** All jobs green on the PR.
4. **Review.** LGTM with no reservations.

## 5. Configuration

- Secrets ONLY via env: `DISCORD_BOT_TOKEN` plus the selected provider's API
  key. The backend is chosen by `CRAWDAD_PROVIDER` (default `anthropic`;
  also `openai`, `gemini` — #610); the matching key
  (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY`) must be set and
  is validated at load time but **never stored** on `CrawDadConfig` — each SDK
  reads it from env. A missing provider key fails fast naming the variable.
  Per-provider router/composer model tiers live in `config.py`, overridable via
  `CRAWDAD_ROUTER_MODEL` / `CRAWDAD_COMPOSER_MODEL`. See the
  [README provider-selection table](README.md#llm-provider-selection) and
  [ADR-0003](../creek-tools/docs/architecture/ADR/0003-decoupled-provider-abstractions.md).
- Everything else in `crawdad.yaml`:
  - `vault_path` — Obsidian vault root.
  - `mcp_server_command` — argv for the MCP subprocess (default
    `[creek-tools-mcp]`).
  - `allowed_user_ids` — Discord user ids that may message the bot.
  - `allowed_channel_ids` — channels the bot will respond in.
  - `max_loop_rounds` — optional FEAT-036 override for the agent-loop
    round cap (default `MAX_LOOP_ROUNDS = 5`, bounded `[1, 50]`).
  - `capture_enabled` — bot-capture toggle (#687), default `False`.
    Opt-in per deployment; see [§5.2](#52-bot-capture-boundary).
  - `capture_subpath` — vault-relative dir the capture writer appends
    to, default `discord-capture`. Must stay inside the vault (same
    absolute/`..` validation as `attachments.staging_subpath`).
  - `attachments.channel_privacy_tiers` — per-channel declared ceiling
    (`open` / `personal` / `intimate` / `all`), validated at
    config-parse time. See [§5.2](#52-bot-capture-boundary) for how
    bot-capture reads it.

Both allowlists must be non-empty — an empty list is a configuration
error, not "open to everyone".

### 5.1 Trust boundary

`crawdad.yaml` is **trusted input**. `mcp_server_command` is executed
verbatim as a subprocess argv, so anyone with write access to
`crawdad.yaml` can run arbitrary code as the bot user. For the
single-user / personal-tool deployment target this is acceptable, but
it means:

- The YAML file lives wherever the operator runs the bot — keep it out
  of any directory writable by less-trusted processes.
- Do *not* feed user-controlled content into `crawdad.yaml`.
- Multi-user / shared deployments are out of scope for v1.0; revisit
  this assumption before broadening access.

See also [§5.2 Bot-capture boundary](#52-bot-capture-boundary) for the
narrower trust boundary bot-capture applies on top of this one.

### 5.2 Bot-capture boundary

Bot-capture (#687) writes messages to `<vault>/<capture_subpath>/` for
later Tier-A ingest. A message is captured only when **both** hold:

1. It passes `_passes_allowlist` — the same gate the command path
   uses: not the bot's own message, not `author.bot`, and both
   `allowed_user_ids` and `allowed_channel_ids` admit it. **The
   capture boundary is the command boundary** (#1052) — before this
   fix, capture ran ahead of the allowlist gate and logged strangers,
   other bots, and channels the operator never allowlisted.
2. The channel's declared privacy tier (`_channel_tier`, reading
   `attachments.channel_privacy_tiers`) is in `CAPTURE_ADMITTED_TIERS`
   = `{"open", "personal"}`:

   | Declared tier | Captured? |
   |---|---|
   | `open` | yes (narrower than needed once landed) |
   | `personal` | yes (exact match) |
   | unset (no `channel_privacy_tiers` entry) | yes — `_channel_tier` falls back to `personal` |
   | `intimate` | **no** |
   | `all` | **no** — admits intimate content by definition |

   A capture record carries no tier field, and the creek-tools side
   that stages the capture dir drops channel metadata, so a captured
   message lands downstream as `unclassified`, which ranks *with*
   `personal` (`creek_mcp/tier_ceiling.py`, #961). Writing an
   `intimate` channel into capture would be a silent privacy
   de-escalation — capture must not carry content whose ceiling it
   cannot represent.

The tier gate is **capture-scoped only**. `channel_privacy_tiers` has
never gated whether the bot *replies* in a channel and still does not
— an `intimate` channel still gets `/crawdad` and free-text replies;
it is just never written to the capture log.

The gate (`_capture_allowed`) is evaluated *inside*
`CrawDadClient._capture_message`'s `try`, so a gate that raises (a
malformed message, an attribute that errors) fails closed to "not
captured" while the command path keeps running unaffected — capture
is best-effort and must never take a command turn down with it.

**Standing constraint for future work:** `MessageCapture.backfill` has
no production caller today. If it is ever wired in (#1057), it MUST
evaluate `_capture_allowed` **per message inside the history loop**,
not once per channel — a channel's history contains messages from
non-allowlisted users and other bots that a channel-level check would
wave through.

`CAPTURE_ADMITTED_TIERS` may only widen once #1262 lands: carrying the
tier end-to-end from the capture record through staging into fragment
frontmatter, so an `intimate` channel can be captured faithfully
instead of refused outright.

### 5.3 Redaction-scan gate (FEAT-027, #1054)

**Policy: record the batch, refuse at dispatch.** An attachment turn
whose `creek.redact.scan` could not run still stages the files and
still records a `PendingBatch` — so the user can see what landed and
`cancel` it — but that batch can never be dispatched to `creek.ingest`.

Rejected alternatives: *withholding the consent prompt* (relies on the
user never typing `ingest` unprompted — prompt suppression is not
enforcement), and *a distinct opt-in token* (a documented override is
a fail-open path, and this is the one gate standing between unscanned
content and the vault).

**Mechanism.** `PendingBatch.scanned` is a **required field with no
default**, placed ahead of the defaulted fields. There is deliberately
no permissive default and no `ConsentConfig` knob: either would let a
construction site — or an operator — silently reopen the hole.
`_run_safety_scan` returns a `_ScanOutcome(scanned, text)` rather than
a bare string, so the caller reads a status instead of string-matching
the reply text against `_MCP_UNAVAILABLE_REPLY` (shared with the ingest
and state paths). All four failure modes — no MCP client, the tool
unadvertised, `MCPUnavailableError`, and any other exception — yield
`scanned=False`.

Enforcement is **one branch in `_dispatch_ingest_for_batch`**, the sole
convergence point of the consent flow. Both `_apply_consent_reply` and
`_apply_disambiguation_reply` reach ingest through it, so one guard
closes the `ingest` route, the `ingest` → type-question → type-word
route, and any future caller. Do not add a second guard at a caller: two
gates drift, one cannot. `with_resolved_types` / `with_state` /
`with_ingested` are all `dataclasses.replace` copies, so `scanned`
survives every transition and cannot be laundered.

**Explicit non-goal.** The gate enforces *"the scan could not run"* —
**not** *"the scan found nothing"*. `creek_mcp/tools/redact.py` computes
`status = "empty" if not scanned.matches else "ok"`, so there is no
failing status to read: a scan that runs and reports secrets still
clears the gate. User-facing copy must claim only what is enforced —
`_SCAN_BLOCKED_REPLY` says "I couldn't run the redaction scan", never
"these files are clean". Substituting a new false claim for the old one
would miss the point of the fix.

Related, explicitly **not** covered here: #1087 (`scan_batch` follows
symlinks out of the scan root — a scan that *runs* but under-reports,
and this gate marks such a batch `scanned=True`). #1088
(`attachments.staging_subpath` outside the scan's canonical scope) *is*
a fifth way the scan fails to run; this design absorbs it with one more
`_ScanOutcome(scanned=False, …)` arm, so sequence it next rather than
touching `_run_safety_scan` twice.

**Revisit predicate:** when `creek.redact.scan` gains a failing status
for non-empty findings, revisit whether `scanned` should become a
three-state outcome and whether findings should block ingest.

## 6. MCP subprocess resilience

The bot does **not** exit on MCP subprocess failure. The pattern is:

1. `MCPClient(config.mcp_server_command).connect()` is an async
   context manager. SDK failures surface as `MCPUnavailableError`.
2. The handler catches `MCPUnavailableError` and replies with the
   documented soft error (`creek-tools is unreachable; try again in a
   moment.`).
3. FEAT-014's dispatcher will add the exponential-backoff restart loop
   (capped at 3 retries / 30s) on top of this skeleton.

## 7. Quality thresholds (mirrors creek-tools)

| Gate | Threshold | Tool |
|---|---|---|
| Branch coverage | ≥ 90% | `pytest --cov-fail-under=90` |
| Docstring coverage | ≥ 95% | `interrogate --fail-under=95` |
| Cyclomatic complexity | ≤ 10 / function | `xenon --max-absolute B` |
| Type checking | strict, zero | `mypy --strict` |
| Lint + format | zero | `ruff check` + `ruff format` |
| Security | zero medium+ | `bandit -r crawdad/ -ll` |
| Dependency vulnerabilities | zero known | `pip-audit` (installed env + exported `uv.lock` — `./scripts/security.sh`) |

`scripts/security.sh` runs `pip-audit` twice because crawdad has two
distinct dependency surfaces and each answers a different question.
CI provisions with `pip install -e ".[dev]"`, and pip honours neither
`uv.lock` nor `[tool.uv].constraint-dependencies` — both are invisible
to it — so a bare `pip-audit` audits only what CI actually imports.
The second run exports `uv.lock` (`uv export --quiet --locked
--all-extras --no-emit-project`) and audits that instead, since
`uv.lock` is the reproducibility contract `uv sync` users install and
is where all eight advisories of #979 lived; an environment-only audit
would have reported clean while the lock carried eight. `--locked`
doubles as a lock-freshness gate — if `pyproject.toml` and `uv.lock`
have drifted, the export fails with "The lockfile at `uv.lock` needs
to be updated" and the fix is `uv lock`, never dropping `--locked`.
Do not add `--strict` to either `pip-audit` call: the local `crawdad`
package isn't published to PyPI, so pip-audit always reports it as a
benign SKIP, and `--strict` would turn that permanent skip into a
permanent false failure.
