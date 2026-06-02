## Role

You are a senior Python engineer working in `crawdad/`, fluent in its user+channel allowlist and
its graceful-degradation behavior when creek-tools is unreachable.

## Goal

Ensure the new author routes obey the security and resilience contract: non-allowlisted callers get
no response; an unreachable desk / creek-tools subprocess yields a soft error reply, never a crash.

## Context

- **Parent epic:** #EPIC_03_NUMBER
- **Predecessor issue(s):** #EPIC_03_ISSUE_03_NUMBER (routes exist).
- **SPEC section:** §10 (CrawDad), and CrawDad's existing security model (allowlist; `crawdad.yaml` trusted input; no exit on MCP failure).
- **Files involved:**
  - `crawdad/crawdad/` — apply allowlist + degradation to `/crawdad ask` and `/crawdad draft`.
  - `crawdad/` tests.
- **Prior decisions:** Allowlist is enforced before any desk call. On MCP/desk failure, reply with a soft error and stay alive. `crawdad.yaml` is trusted input.
- **State of the world:** Allowlist + degradation exist for current commands; the new routes must inherit them.

## Output Format

A single PR containing:

- [ ] Allowlist enforced on the author routes (non-allowlisted → no response).
- [ ] Soft-error reply when the desk/creek-tools is unreachable; process stays up.
- [ ] Tests: non-allowlisted user gets nothing; simulated MCP failure yields a soft error, not an exception.

## Examples

```python
# Non-allowlisted caller:
assert handle_ask(non_allowlisted_ctx, "F6?") is None     # no response

# Desk unreachable:
reply = handle_ask(allowlisted_ctx_with_dead_mcp, "F6?")
assert "temporarily unavailable" in reply.lower()         # soft error, no crash
```

## Constraints

**Scope fence:** No new features — only security + resilience for the author routes. Do not change
the desk or MCP verb.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** CrawDad remains demoable and crash-resistant; security parity with
existing commands.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (CrawDad's quality gate).
- [ ] Pre-commit clean for the `crawdad/` repo.
- [ ] Negative-path tests (allowlist + degradation) included.
- [ ] PR body includes `Refs #EPIC_03_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `crawdad`
