# FEAT-013: CrawDad — Discord bot skeleton + MCP client wiring

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~450
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-008-haiku-router-sonnet-composer.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-008-haiku-router-sonnet-composer.md) (part 1 of 3)
**Dependencies:** FEAT-010, FEAT-011, FEAT-012 (the MCP surface CrawDad consumes)
**Parallelizable with peers:** no (FEAT-014 and FEAT-015 extend this)
**Wave:** 5 (CrawDad v1.0)

## Goal

Stand up the CrawDad project as a sibling to `creek-tools/` — a Discord bot that connects to the creek-tools MCP server and reads `00-Creek-Meta/State/latest.md` at session start. No agent loop yet; this PR is the wiring scaffold.

## Files to touch

- `crawdad/` (new top-level directory, sibling to `creek-tools/`).
- `crawdad/pyproject.toml` (new) — Python ≥3.11, deps: `discord.py`, `mcp` (Anthropic SDK), `anthropic`, `pydantic`, `pyyaml`.
- `crawdad/crawdad/__init__.py` (new).
- `crawdad/crawdad/bot.py` (new) — `discord.py` Client subclass with on-message + on-ready handlers.
- `crawdad/crawdad/mcp_client.py` (new) — async MCP client wrapper around the Anthropic `mcp` SDK; connects to `creek-tools-mcp` over stdio.
- `crawdad/crawdad/state.py` (new) — `load_session_state()` reads `<vault>/00-Creek-Meta/State/latest.md` once at session start (Graphify-style PreToolUse pattern).
- `crawdad/crawdad/config.py` (new) — Pydantic config: Discord token, Anthropic API key, vault path, MCP server command, allowed user IDs, allowed channel IDs.
- `crawdad/crawdad/cli.py` (new) — `crawdad run` entry point.
- `crawdad/CLAUDE.md` (new) — project standards (mirrors `creek-tools/CLAUDE.md`'s quality bar at a smaller scale).
- `crawdad/README.md` (new).
- `crawdad/scripts/check-all.sh` (new) — same gate structure as creek-tools.
- `crawdad/tests/test_bot.py`, `tests/test_mcp_client.py`, `tests/test_state.py` (new).

## Pre-decided choices

- **Project location:** new top-level `crawdad/` directory (sibling to `creek-tools/`). Not nested under creek-tools because it's a separate deployable.
- **Discord library:** `discord.py` (de facto standard, async, well-maintained).
- **MCP transport:** stdio (matches FEAT-010's server). The MCP server runs as a subprocess of CrawDad in v1.0; Hostinger VPS deployment topology is out of scope (separate prompt, per the comparative-analysis framing).
- **Config:** environment variables + a `crawdad.yaml` file. Discord token and Anthropic API key only via env (`DISCORD_BOT_TOKEN`, `ANTHROPIC_API_KEY`).
- **Allowlist:** v1.0 hard-codes a single allowed Discord user ID (the developer's) and an allowed channel list. Multi-user is deferred to v1.3+ (per the personal-use framing).
- **Session boundaries:** one Discord conversation = one CrawDad session. Session state (loaded `state.md` content) is held in memory for the session; FEAT-015's loop reuses it.
- **Session-state load is read-only and cheap:** just a file read + parse; no MCP call needed for the initial load. The MCP server's `creek.state.read` tool is for *re-reads* mid-session.

## Test plan

- Unit: `MCPClient.connect()` against a fixture stdio server completes and exposes a `list_tools()` method.
- Unit: `load_session_state(vault_path)` against a fixture `latest.md` returns a structured model with the wavelength snapshot, eddies, threads, and suggested questions.
- Integration: `crawdad run` boots, connects to a fixture MCP server, posts a "hello" reply when DM'd by an allowlisted user, and refuses non-allowlisted users.
- Regression: a non-allowlisted user gets no response (not even a refusal — silent ignore, per personal-use scoping).
- Regression: missing `latest.md` is handled gracefully (CrawDad replies with "no audit report yet — run `creek state`" rather than crashing).

## Acceptance criteria

- `crawdad/` package exists with the documented structure.
- `crawdad run` boots, connects to creek-tools MCP, and handles allowlisted Discord messages with a stub reply.
- `load_session_state` reads `latest.md` at session start.
- Allowlist is enforced — non-allowlisted users get no response.
- Quality bar mirrors `creek-tools/`: ≥90% branch coverage on the new code, MyPy strict clean, Ruff zero violations, conventional commits.
- **MCP subprocess resilience:** if the `creek-tools-mcp` subprocess exits or stops responding, the bot does *not* exit. It posts a graceful error reply to the active Discord channel ("creek-tools is unreachable; try again in a moment") and attempts a single restart with exponential backoff (capped at 3 retries / 30s) before staying disconnected and surfacing a status command response. Verified by a regression test that kills the subprocess mid-tool-call and asserts the bot keeps running.
- `crawdad/CLAUDE.md` and `crawdad/README.md` exist.

## References

- Source candidate: ADOPT-008 (the agent loop these next FEATs build on).
- FEAT-014 (Haiku router + dispatcher).
- FEAT-015 (Sonnet composer + 5-round loop).
- FEAT-010 (the MCP server CrawDad connects to).
- INTEGRATION-PLAN.md "CrawDad design implications" section.
