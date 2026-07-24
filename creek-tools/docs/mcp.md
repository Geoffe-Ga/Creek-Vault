# `creek-tools-mcp` registration guide (FEAT-010)

The `creek-tools-mcp` server exposes the read-only `creek` CLI surface
as MCP tools. Both the developer's Claude Code and the CrawDad Discord
bot (FEAT-013+) consume the same surface, so privacy-tier enforcement
and audit-log writes happen at one boundary.

## Installation

```bash
pip install -e .   # from creek-tools/
```

The entry point is registered by `pyproject.toml` as
`creek-tools-mcp`. The server speaks JSON-RPC over stdio; running it
interactively will appear to hang, which is correct.

## Tools

### Handshake (epic #748)

| Tool              | Purpose                                                                   |
|-------------------|---------------------------------------------------------------------------|
| `creek.handshake` | Negotiate vault presence, contract/ontology versions, tier model, and the live capability list. |

A connecting client (the Adepthood app) calls `creek.handshake` first. It is
read-only and needs no LLM provider, so the negotiation succeeds on any host and
even on a fresh/absent vault. It returns at least `available`,
`contract_version`, `ontology_version`, `tiers` (`open`/`personal`/`intimate`),
and `capabilities` (the names of the tools actually registered), plus the
`tier_model` and `transport`. Versions come from `creek_mcp/contract.py`; the
cross-repo contract is
[`docs/decisions/2026-06-30-adepthood-creek-mcp-contract.md`](../../docs/decisions/2026-06-30-adepthood-creek-mcp-contract.md).

### Read tools (FEAT-010)

| Tool                  | Purpose                                                    |
|-----------------------|------------------------------------------------------------|
| `creek.state.read`    | Return the latest `00-Creek-Meta/State/latest.md` content. |
| `creek.state.render`  | Re-render the audit report (expensive).                    |
| `creek.lint`          | Run the unified hygiene lint pass (FEAT-008).              |
| `creek.mine`          | Surface essay seeds from the compiled vault layer.         |
| `creek.draft`         | Draft an essay from a mined idea (requires an LLM).        |

### Author tools (FEAT-041)

| Tool           | Inputs                                                                  | Purpose                                                                       |
|----------------|------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| `creek.author` | `query`, `medium?` (default `research`), `max_rounds?`, `dry_run?`      | Author a draft for a query via the Creek Writing Desk.                         |

Mirrors the `creek author` CLI args and returns an `AuthoredDraft` shape:
`medium`, `query`, `body`, `provenance` (a list of provenance entries),
`verdict` (one of `PASS` / `REVISE` / `ESCALATE`), and `rounds`. The response
is a typed **stub** today (the skeleton from #456); issue #460 wires it to the
real desk. Only the `research` medium is wired — any other `medium` returns
`status: error`.

### Write tools (FEAT-011)

| Tool                    | Inputs                                                      | Write-side tier rule                                              |
|-------------------------|-------------------------------------------------------------|-------------------------------------------------------------------|
| `creek.save`            | `target`, `body`, `title?`, `tier`, `provenance?`           | Caller's `ceiling` must admit `tier` — intimate via open refused. |
| `creek.ingest`          | `source_type`, `input_path`                                 | Default ingest tier is `personal`; `ceiling=open` is refused.     |
| `creek.classify`        | `method` (`rules`\|`llm`), `force`                          | Rewrites in place; no new tier produced — any ceiling permitted.  |
| `creek.link`            | `method` (`embeddings`\|`temporal`\|`eddies`), `rebuild`    | Links existing artefacts in place — any ceiling permitted.        |
| `creek.report`          | `report_type` (`tags`\|`voice`)                             | Renders a vault-state report — any ceiling permitted.             |
| `creek.skills.refresh`  | none beyond `ceiling`                                       | Voice-skill tree regen; intimate exemplars already excluded.      |
| `creek.compile`         | `fragment_ids`, `target_kind`, `target_id`, `target_title`  | Idempotent per FEAT-003; no-op re-runs do not log a duplicate.    |
| `creek.journal`         | `content`, `external_id`, `timestamp?`, `tier?`             | Stages an Adepthood entry then ledger-ingests it (#754); `external_id` is the idempotency key and `tier` defaults to `open`. The entry's tier is honored — a ceiling that would not admit it is refused. |

### Purge tools (FEAT-012, elevated authorization required)

| Tool                            | Inputs                                              | Authorization                                              |
|---------------------------------|-----------------------------------------------------|------------------------------------------------------------|
| `creek.purge.fragment`          | `fragment_id`, `auth_token`, `dry_run?`             | `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.source`            | `source_type`, `auth_token`, `dry_run?`             | `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.classifications`   | `auth_token`, `dry_run?`                            | `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.daterange`         | `start`, `end` (ISO dates), `auth_token`, `dry_run?`| `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.vault`             | `confirm_vault_path`, `auth_token`, `dry_run?`      | BOTH the token AND `confirm_vault_path` matching the vault |

Purge tools deliberately do not accept a `privacy_tier_ceiling`
parameter: they do not return vault content, so the ceiling
invariant from FEAT-010 does not apply. Authorization is the only
gate, and refusals are themselves audited so a hostile client
cannot probe the gate silently.

Every tool requires a `privacy_tier_ceiling` parameter
(`open` | `personal` | `intimate` | `all`); default is `open`. Note
that `open` is the *most restrictive* setting — it restricts the
caller to open-tier (publishable) content only, not "open access".
The ladder goes `open` (publishable) → `personal` (summarised
personal allowed) → `intimate` (everything self-authored) → `all`
(every tier including unclassified). Content above the ceiling is
omitted or returned as a title-only stub — the ceiling cannot be
bypassed by the caller.

### Write-side tier-ceiling rule (FEAT-011)

A write tool that would *create* content at tier `T` requires the
caller's `privacy_tier_ceiling` to admit `T`. So `creek.save` with
`ceiling=open` and `tier=intimate` is refused with `status="refused"`
rather than silently downgraded — the body never lands in the vault
and the audit entry records the refusal without the body. The same
gate applies to `creek.ingest` because the default ingestor tier is
`personal`.

### Elevated-authorization model (FEAT-012)

The `creek.purge.*` family is gated by a separate token from the
tier-ceiling system. The server reads `CREEK_MCP_ELEVATED_TOKEN`
from its environment at startup; callers present a matching string
via the `auth_token` parameter on each purge tool. Comparison runs
through `hmac.compare_digest` (constant-time), not `==`, so a hostile
client cannot probe the token byte-by-byte through timing.

Operational rules:

- **CrawDad does not get the token.** The Discord bot's MCP client
  (FEAT-013+) is launched without `CREEK_MCP_ELEVATED_TOKEN`, and its
  MCP requests omit `auth_token`. Every purge call from CrawDad
  therefore returns `status="refused"` — there is no Discord command
  surface that could accidentally destroy vault content.
- **The developer's Claude Code can be configured with the token.**
  Generate one with high entropy — this is the same recipe the startup
  check prints if the configured token is too weak:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  Add `"CREEK_MCP_ELEVATED_TOKEN": "<generated-token>"` to the `env`
  block of `.mcp.json` (alongside `CREEK_MCP_CONSUMER`). Treat the
  token like any other vault secret — keep it out of public dotfiles
  and shared shells.
- **The token must be at least 32 characters (#907).** A configured
  value below the floor aborts server startup on *both* transports with
  the rotation recipe on stderr (the token value itself is never
  printed), and — for embedders that bypass `main` — is denied silently
  by the gate itself. Leaving `CREEK_MCP_ELEVATED_TOKEN` unset remains
  fully supported: that is the "purge disabled" posture, not an error.
- **Breaking change for operators upgrading (#907).** This floor is
  new: a server that starts fine today with a `CREEK_MCP_ELEVATED_TOKEN`
  under 32 characters will refuse to start the next time it is launched
  — on both transports — with no grace period, warning-only mode, or
  opt-out. If you rely on `creek.purge.*`, rotate the token with the
  recipe above *before* upgrading.
- **`creek.purge.vault` requires both the token AND
  `confirm_vault_path`.** The confirmation must match the resolved
  absolute path of the target vault, mirroring the CLI's interactive
  "type the vault path to proceed" prompt. Either guard alone
  refuses the call.
- **Refusals are audited.** A refused purge attempt still appends an
  entry to `mcp.jsonl`, so a token-less probe leaves a trail. The
  `auth_token` value never enters the audit log — only the
  refusal-or-success outcome and the structured args summary.

Example `.mcp.json` for a Claude Code instance configured for
destructive ops (replace the token with a freshly generated one — the
sample shown here is high-entropy from `secrets.token_urlsafe(32)` and
must not be reused):

```json
{
  "mcpServers": {
    "creek-tools": {
      "command": "creek-tools-mcp",
      "env": {
        "CREEK_MCP_CONSUMER": "claude-code",
        "CREEK_MCP_ELEVATED_TOKEN": "REPLACE_WITH_secrets.token_urlsafe(32)"
      }
    }
  }
}
```

> Do **not** copy this `env` block into the CrawDad host config. The
> token is deliberately withheld from CrawDad so a Discord-side
> intent can never escalate into a vault deletion. If you need to
> rotate the token, rotate it on the developer's Claude Code only.

## Claude Code

Add an entry to your project's `.mcp.json` (or to user-level
`~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "creek-tools": {
      "command": "creek-tools-mcp",
      "env": {"CREEK_MCP_CONSUMER": "claude-code"}
    }
  }
}
```

`CREEK_MCP_CONSUMER` is recorded on every audit-log entry so we can
tell apart calls from Claude Code, CrawDad, or operator-driven runs.

## Claude Desktop / Cursor / Zed

All three read the same `mcp.json` schema; the registration entry
above works verbatim. Drop it in the host's MCP config location.

## CrawDad

CrawDad (FEAT-013+) treats this server's tool registry as its
`intents` schema (FEAT-014). The Discord bot spawns the server as a
stdio child process. Set `CREEK_MCP_CONSUMER=crawdad` in the bot's
environment so the audit trail distinguishes Discord-driven calls.

## Network transport (authenticated, epic #757 / #759 / #837)

Local consumers (Claude Code, CrawDad) speak JSON-RPC over **stdio** — the
default, unchanged. To reach a user's per-user-VM vault from a remote
Adepthood backend, the server also serves an **authenticated
streamable-http** transport:

```bash
# Generate each consumer token as high-entropy hex, once, per consumer:
#   python -c "import secrets; print(secrets.token_hex(32))"
export CREEK_MCP_CONSUMER_TOKENS="adepthood=<secrets.token_hex(32)>;other=<token>"
creek-tools-mcp --transport network --host 127.0.0.1 --port 8000
```

### TLS is enforced for non-loopback binds (#837)

`--host` defaults to `127.0.0.1`. A **loopback** bind — `127.0.0.0/8`,
`::1`, or the literal hostname `localhost` (case-insensitive) — may still
serve plain HTTP, for local dev; anything else is refused unless
`--tls-cert`/`--tls-key` are both supplied and point at existing files:

```bash
creek-tools-mcp --transport network --host 0.0.0.0 --port 8443 \
  --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem
```

With both flags set, the server serves the Starlette app directly under
`uvicorn` with `ssl_certfile`/`ssl_keyfile` — no reverse proxy is required.
Alternatively, keep the server bound to `127.0.0.1` (or `localhost`) and
terminate TLS in a reverse proxy in front of it; either path keeps bearer
tokens off the wire in cleartext. A non-loopback bind without TLS exits
immediately with a nonzero status and an error on stderr, before any socket
opens — no partial startup. Note that only an IP literal or the exact
hostname `localhost` is recognised as loopback; any other hostname (even one
that happens to resolve to `127.0.0.1`, e.g. via `/etc/hosts`) is treated as
routable by design and requires TLS.

- **No anonymous access.** Network mode refuses to start unless
  `CREEK_MCP_CONSUMER_TOKENS` is set. It holds `consumer=token` pairs,
  `;`-separated; tokens live in the **environment only** (never in code or
  config), mirroring the `CREEK_MCP_ELEVATED_TOKEN` precedent. Generate each
  token as high-entropy hex — `secrets.token_hex(32)` — so a token is never
  guessable; the constant-time comparison only matters against a strong secret.
  A configured token below 32 characters is refused at startup, the same
  floor `CREEK_MCP_ELEVATED_TOKEN` now enforces (#907) — both surfaces share
  one minimum defined once in `creek_mcp/token_policy.py`.
- **Per-consumer identity.** Each request must present its bearer token
  (`Authorization: Bearer <token>`). The token maps to a consumer name that
  is stamped on every audit-log entry — so remote calls are attributable the
  same way `CREEK_MCP_CONSUMER` attributes stdio calls. A missing or unknown
  token is rejected `401` before any tool runs; comparison is constant-time.
- **Bearer tokens carry a finite lifetime (#837).** Each verified bearer is
  issued an `AccessToken` that expires `CREEK_MCP_TOKEN_TTL_SECONDS` after
  the moment it was verified (default `3600`, i.e. one hour); an unset,
  non-integer, or non-positive TTL value falls back to the default rather
  than issuing a non-expiring token. In practice the SDK's bearer middleware
  re-verifies the `Authorization` header on every request rather than
  caching a session-scoped token, so a consumer that keeps presenting the
  same configured `CREEK_MCP_CONSUMER_TOKENS` secret is re-verified and
  granted a fresh `AccessToken`/`expires_at` on each call; the TTL bounds how
  long any *individually captured* `AccessToken` (e.g. one logged or cached
  outside the server) would remain valid, not how often the underlying
  shared secret must be rotated.
- **A consumer token grants remote _write_ access, by design.** A valid
  `CREEK_MCP_CONSUMER_TOKENS` entry can reach every non-purge tool at or below
  the `personal` ceiling — including the **write** tools (`creek.journal`,
  `creek.ingest`, `creek.classify`, `creek.link`, `creek.report`,
  `creek.compile`, `creek.save`, `creek.skills.refresh`), not just reads. This
  is intentional (Adepthood writes journal entries remotely), so treat each
  token as a write-capable credential — a meaningfully larger attack surface
  than "remote read-only." Purge tools stay gated separately by
  `CREEK_MCP_ELEVATED_TOKEN` (a per-consumer bearer alone cannot purge).
- **INTIMATE is never reachable remotely.** The boundary caps a remote
  caller at `personal`: a request for a `privacy_tier_ceiling` above it
  (`intimate` / `all`, or any unrecognised value) is **refused before
  dispatch**, so intimate content is never even read for a network consumer.
  Stdio calls are unaffected — the per-tool `open` default still applies
  locally, and `intimate` remains reachable for the local owner.

The transport is a thin wrapper around the MCP SDK's `TokenVerifier` /
streamable-http app; the tool registry, tier-ceiling rules, and hash-chained
audit are identical to the stdio path.

## Audit log

Every tool invocation appends one entry to
`<vault>/00-Creek-Meta/audit/mcp.jsonl`:

```json
{
  "tool": "creek.mine",
  "args_summary": {"phase": "rising", "limit": 10},
  "tier_ceiling": "open",
  "consumer": "claude-code",
  "timestamp": "2026-05-11T12:34:56+00:00",
  "prev_hash": "..."
}
```

`args_summary` captures the MCP-supplied arguments to the tool (the
ceiling is already a top-level field, so it's not duplicated here).
The vault path is *not* recorded — it's resolved internally from
`load_config()` and never enters the audit trail. Compact summary
rules: long strings become `{"len": N}`, lists become `{"count": N}`,
dicts become `{"keys": [...]}`. A draft request for an
`intimate`-tier fragment never leaks the body into the audit
trail. Hash chaining is provided by `creek.audit.AuditLog`; FEAT-012
adds the per-entry `entry_hash` and a verifier on top — see below.

#### Tamper-evidence (FEAT-012)

Every entry carries two integrity fields:

- `prev_hash` — SHA-256 of the previous line's bytes (chain link).
  Removing or reordering an entry invalidates the chain at the next
  line.
- `entry_hash` — SHA-256 of the entry's payload (excluding
  `entry_hash` and `prev_hash`). Mutating any other field — `tool`,
  `consumer`, `tier_ceiling`, the args summary — invalidates this
  hash on its own, even if the line position survives.

`creek_mcp.audit.verify_mcp_audit_chain(vault_path)` walks both
invariants and raises `MCPAuditChainBrokenError` on the first
mismatch. The walk is cheap (one read pass, one hash per line) and
is safe to call from any operator script. Writes hold an exclusive
`flock` on the log so two processes appending in parallel cannot
interleave half-written lines; concurrent-write safety is exercised
by `tests/test_mcp_audit.py::test_concurrent_process_appends_do_not_corrupt_log`.

#### Write-tool audit fields (FEAT-011)

Write-tool entries add three optional fields on top of the read-tool
schema:

```json
{
  "tool": "creek.save",
  "args_summary": {"target": "thread", "tier": "open", "body": {"len": 4096}},
  "tier_ceiling": "open",
  "consumer": "crawdad",
  "timestamp": "2026-05-12T12:34:56+00:00",
  "created_path": "02-Threads/Active/2026-05-12-saved-thread.md",
  "created_tier": "open",
  "affected_fragment_ids": ["frag-a", "frag-b"]
}
```

`created_path` is the relative path of the produced file (or the
container directory for batch tools like `creek.skills.refresh`);
`created_tier` is the tier the content was written at;
`affected_fragment_ids` is an ID list — never fragment bodies. Tools
that update artefacts **in place** (`creek.classify`, `creek.link`) do
not produce new files, so they omit `created_path` and `created_tier`
from their audit entry.

For tools that accept a `body` argument (`creek.save`), the audit entry
records `body_len` rather than `body` — on both the success and the
refusal path — so a fragment body never lands in `mcp.jsonl` verbatim,
regardless of length.

`creek.compile` skips the audit append on no-op re-runs (idempotent
per FEAT-003) — the engine still runs (the LLM call is not yet
skipped), but the audit log does not grow when a re-compile produces
an identical target page.

## Troubleshooting

- **Server appears to hang:** correct. It speaks JSON-RPC over stdio.
  Use `python -m creek_mcp.server` for the same effect.
- **`No module named "mcp"`:** install with `pip install -e .` — FEAT-010
  added the `mcp` SDK to `pyproject.toml`.
- **`creek.draft` returns "LLM provider unavailable":** the server
  loads the LLM lazily so only `draft` requires it. Configure
  `ANTHROPIC_API_KEY` or a running Ollama instance.
- **`--transport network` exits with "refusing to serve on non-loopback
  host ... without TLS":** bind `127.0.0.1`/`localhost` for local dev, or
  pass `--tls-cert`/`--tls-key` (both, pointing at existing files) for a
  routable bind.
- **Fewer mine seeds than expected:** the ceiling is filtering intimate
  fragments by design. Raise the ceiling to `intimate` or `all` only
  when the caller is authorised.
