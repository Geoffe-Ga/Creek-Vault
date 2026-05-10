# Audit Workflow

A repeatable process for auditing every user-facing error message in an application. Use this when the task is "go wide" rather than "fix this one message".

## 1. Inventory

Cast a wide net. The goal is to collect every string that could ever reach a user in a failure state.

### Where user-facing errors live

- **Form validation**: inline field errors, form-level summary errors
- **Toast / snackbar / flash messages**: ephemeral notifications
- **Alert / banner / modal dialogs**: blocking or prominent errors
- **4xx / 5xx pages**: 400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504
- **Empty states with failure context**: "We couldn't load your projects"
- **Email / SMS / push failures**: both the user's copy and the admin's copy
- **API error bodies**: if the API is consumed by a third-party UI or CLI
- **CLI stderr**: for developer-facing tools, the CLI *is* the UI
- **Webhook failure notifications**: delivery retries, dead-letter queues
- **Payment / billing failures**: card declines, subscription lapses

### Grep patterns by stack

Adjust to your codebase. Start broad, then narrow.

```bash
# Python (Flask/Django/FastAPI)
rg -n 'flash\(|messages\.error|raise.*Error\(|HTTPException\(|abort\(' --glob '*.py'

# JavaScript / TypeScript
rg -n 'toast\.|showError|throw new \w*Error|res\.status\(\d+\)\.(json|send)' --glob '*.{ts,tsx,js,jsx}'

# Templates (Jinja2 / ERB / Blade)
rg -n 'error|alert' --glob '*.{html,jinja,erb,blade.php}'

# Generic: quoted strings near error handling
rg -B1 -A1 'error|failed|invalid|unable' --glob '!*.{lock,min.js}'
```

### Output shape

Put the inventory in a spreadsheet or markdown table — one row per message:

| # | File:Line | Trigger path | Current text | User-facing? | Frequency (30d) |
|---|-----------|--------------|--------------|--------------|-----------------|
| 1 | `auth/login.py:42` | POST /login bad creds | "Login failed." | Yes | 18,400 |
| 2 | `api/users.py:88` | 500 fallback | "Internal server error" | Yes | 230 |

## 2. Classify

Mark each message:

- **User-facing** — rendered to a human through the product UI. In scope.
- **Developer-facing** — logs, stack traces, telemetry. Out of scope for this audit (see `error-handling` skill).
- **Mixed** — e.g., an API error body that both logs emit *and* consumers render. Treat as user-facing; developers can read user-friendly text too.

If classification is unclear, ask: *"If my parent hit this, would they see this exact string?"*

## 3. Score

For each user-facing message, score 0 or 1 on each rubric component:

- [ ] **What**: names what failed in human terms
- [ ] **Why**: gives a cause the user can act on (skip safely if N/A)
- [ ] **Next**: specifies a concrete next action
- [ ] **Escape**: offers a path when Next fails (trace ID, status page, support link)

Also flag any message containing an anti-pattern (shrug, opaque code, blame, dev leak, dead-end, jargon, panic, liar, thief — see SKILL.md Step 4).

A message passing fewer than 3/4 components or containing any anti-pattern is a rewrite candidate.

## 4. Triage

Sort rewrite candidates by **blast radius**:

```
priority_score = frequency × severity × stuck_factor
```

- **Frequency**: how often this message is seen (from logs, analytics, or support tickets).
- **Severity**: 3 for data loss / payment / auth, 2 for core workflow, 1 for edge cases.
- **Stuck factor**: 3 if no self-serve path exists, 2 if partial, 1 if easy workaround.

Fix the top decile first. A full audit that ships nothing is worse than a focused audit that ships the top 20 messages.

## 5. Rewrite

Use templates from `rewrite-templates.md`. Each rewrite includes:

- New message text
- Trace ID surface (add one if missing — even a short hash of request ID helps support)
- Severity / tone check (calm vs. serious)
- Preservation statement if applicable ("your draft is saved")

Keep the old error code or create a new one; map it in a lookup table so support can still match tickets to log lines.

## 6. Ship in Reviewable Chunks

- One PR per subsystem (auth, billing, forms, API, etc.), not one mega-PR.
- Include before/after in the PR description so reviewers can feel the improvement.
- Add a regression test or snapshot where feasible, so future commits can't quietly downgrade a message back to "An error occurred".

## 7. Measure

After shipping, watch:

- Support-ticket volume tagged with the subsystem.
- "Same user retried same action" rate (are they getting unstuck?).
- Error-toast dismiss time (too fast = they didn't read; too slow = confusion).

A good audit reduces support-ticket volume in the audited subsystem within the first month.
