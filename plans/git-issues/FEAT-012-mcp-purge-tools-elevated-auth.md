# FEAT-012: MCP purge tools + elevated authorization + audit hardening

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~400
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADAPT-004-mcp-server-as-interface.md`](../2026-05-05_comparative-analysis/candidates/ADAPT-004-mcp-server-as-interface.md) (part 3 of 3)
**Dependencies:** FEAT-010, FEAT-011
**Parallelizable with peers:** no
**Wave:** 4 (closes the MCP wave)

## Goal

Expose the `creek purge.*` family via MCP behind an elevated-authorization gate so that CrawDad cannot accidentally purge while the developer's Claude Code can. Harden the MCP audit log (tamper-evident, append-only, locking).

## Files to touch

- `creek-tools/creek_mcp/tools/purge.py` (new) — wraps `creek purge fragment`, `creek purge source`, `creek purge classifications`, `creek purge daterange`, `creek purge vault`.
- `creek-tools/creek_mcp/auth.py` (new) — elevated-auth checker; reads `CREEK_MCP_ELEVATED_TOKEN` from env and matches against the request's `Authorization` header (or MCP-equivalent metadata).
- `creek-tools/creek_mcp/audit.py` (new — was inline in FEAT-010/011, now factored out) — tamper-evident JSONL writer with hash chaining and an exclusive-lock during write.
- `creek-tools/creek_mcp/server.py` — wire purge tools and elevated-auth check.
- `creek-tools/tests/test_mcp_purge.py` (new).
- `creek-tools/tests/test_mcp_audit.py` (new) — verifies tamper-evidence and locking.
- `creek-tools/docs/mcp.md` — document the elevated-auth model and how to provision the token.

## Pre-decided choices

- **Elevated-auth mechanism:** environment variable `CREEK_MCP_ELEVATED_TOKEN` set at server startup. Requests that include matching `Authorization: Bearer <token>` (or MCP-equivalent metadata field) can call purge tools. Without the token, every purge tool returns a structured refusal.
- **Token comparison uses `hmac.compare_digest`**, not `==`. Plain string equality is vulnerable to timing-based token inference; `compare_digest` is constant-time. Wire it correctly from day one.
- **CrawDad does not get the token.** The Discord bot's MCP client connects with no `Authorization` header; purge calls fail-closed.
- **The developer's Claude Code can be configured with the token.** Documented in `docs/mcp.md` as the deliberate setup step for destructive ops.
- **`creek purge vault` requires *both* the token and a confirmation parameter** (`confirm_vault_path: <absolute-path>`). Mirrors the existing CLI behaviour (which prompts for the absolute vault path interactively).
- **Audit log hardening:** each entry is a JSON object with `prev_hash` (SHA-256 of the previous entry's content) and `entry_hash`. Tampering breaks the chain. An exclusive `flock` during writes prevents concurrent corruption. (This solves SEC-005 for the MCP audit log specifically; the broader audit log work in SEC-005 is separate.)

## Test plan

- Unit: `creek_mcp.auth.is_elevated(request)` returns `True` only when the env token matches the request token.
- Integration: a purge tool call without the elevated token returns the documented refusal.
- Integration: `creek.purge.vault` without `confirm_vault_path` is refused even with the token.
- Regression: tampering with a JSONL entry breaks the next-entry hash check (verified by mutating a fixture and asserting the verifier flags it).
- Regression: concurrent write attempts from two processes don't corrupt the log (uses `pytest-flaky` or a small subprocess fixture).
- Regression: CrawDad's MCP client (no token) cannot purge anything — verified end-to-end against a fixture vault.

## Acceptance criteria

- All five `creek.purge.*` tools exposed and gated by the elevated-auth check.
- Without the token, every purge call returns a structured refusal — never a silent partial purge.
- `creek.purge.vault` requires both token and `confirm_vault_path`.
- Audit log entries have `prev_hash` / `entry_hash`; a verifier function exists and is exercised by tests.
- Concurrent-write safety is tested.
- `docs/mcp.md` documents the elevated-auth model with explicit "do not give this token to CrawDad" guidance.
- ≥90% branch coverage on `creek_mcp/{auth,audit,tools/purge}.py`.
- `creek_mcp/auth.py` uses `hmac.compare_digest(expected, actual)` — not `==`. Verified by a unit test that asserts plain `==` is *not* used in the hot path (e.g., a static check or a compare-call interceptor).

## References

- Source candidate: ADAPT-004 (elevated-authorization is the named adaptation for destructive ops).
- Existing CLI purge: `creek/purge/` and `creek-tools/docs/cleaning-and-purge.md`.
- Existing MCP audit log: created in FEAT-010, hardened here.
- Related issue: SEC-005 (broader audit-log integrity work for the non-MCP audit logs).
