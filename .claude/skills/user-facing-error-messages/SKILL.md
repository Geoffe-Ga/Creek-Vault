---
name: user-facing-error-messages
description: >-
  Audit and rewrite user-facing error messages so users can self-serve instead
  of emailing support. Use when auditing error messages across an app,
  improving or rewriting error wording, reviewing toasts, alerts, flash
  messages, form validation copy, or empty/failure states, or when a message
  feels opaque, blame-y, or ends in "contact support". Covers the four-part
  rubric (what / why / next / escape), an inventory-and-triage audit workflow,
  rewrite templates, and security-safe disclosure rules.
  Do NOT use for exception architecture, typed errors, or developer-facing
  logging (use error-handling skill); for the security rules around what
  errors may safely disclose in auth/crypto paths (use security skill); or
  for the visual styling of alerts, toasts, and banners (use
  frontend-aesthetics skill).
metadata:
  author: Geoff
  version: 1.0.0
---

# User-Facing Error Messages

Write every error message as if you are the user reading it at 2am trying to get unstuck. The goal is self-service, not a support ticket.

## Core Principle

If a user hits this error, they have exactly one question: **"What do I do now?"** An error message that cannot answer that question has failed, no matter how technically accurate it is.

"Contact support" is never a primary call-to-action. It is the last-resort escape hatch after you have done everything you can to let the user self-serve.

## Instructions

### Step 1: Adopt the User Empathy Lens

Before writing or reviewing any message, answer these out loud:

1. **Who hits this?** (End user? Admin? Developer integrating the API?)
2. **What were they trying to do?** (The task, not the code path.)
3. **What can they actually change?** (Their input? A setting? Wait and retry? Nothing?)
4. **What is the emotional state?** (Lost work = high stakes. Typo in a form = low stakes. Tone must match.)

If the answer to #3 is "nothing", the message must say so explicitly and offer an escape (status page, support link with a pre-filled trace ID, retry button).

### Step 2: Apply the Four-Part Rubric

Every user-facing error message is scored on four components. A passing message hits all four in plain language.

| Component | Question it answers | Example |
|-----------|--------------------|---------|
| **What** | What went wrong, in human terms? | "We couldn't save your draft." |
| **Why** | Why it happened — only if the user can act on it | "Your network dropped mid-save." |
| **Next** | The single most useful next action | "Check your connection and tap Retry." |
| **Escape** | Where to go if Next fails | "Still stuck? [Open a ticket] (ref: 7F3A2)" |

Skip **Why** when it leaks internals, blames the user, or gives them no lever. Skip it; don't lie or filler.

### Step 3: Run the Audit Workflow

For a full-app audit, follow `references/audit-workflow.md`. The short version:

1. **Inventory** every user-facing error source: `raise`/`throw`, toast/flash/snackbar calls, HTTP error responses rendered to users, form validation strings, empty-state copy, 4xx/5xx pages, email/SMS failures.
2. **Classify** each string: user-facing vs. developer-facing (logs, stack traces). Only user-facing strings are in scope.
3. **Score** each against the four-part rubric. Flag any message that scores below 3/4 or contains an anti-pattern from Step 4.
4. **Triage** by blast radius: high-traffic paths first (login, checkout, save), then happy-path adjacent, then rare edge cases.
5. **Rewrite** using the templates in `references/rewrite-templates.md`.
6. **Preserve debuggability**: surface a short trace ID/error code so support can find the log line, but never make the code the whole message.

### Step 4: Eliminate the Anti-Patterns

Reject and rewrite any message matching these:

- **The shrug**: "An error occurred." / "Something went wrong." / "Oops!"
- **The opaque code**: "Error 0x80042108" with no human sentence.
- **The blame**: "You entered an invalid email." (Use "That email is missing an @ — did you mean `name@example.com`?")
- **The dev leak**: "NullPointerException in UserService.load()" / "ECONNREFUSED 127.0.0.1:5432".
- **The dead-end**: ends in "contact support" with no trace ID, no link, and no attempt at self-serve.
- **The jargon**: "Malformed payload", "Constraint violation", "Upstream 502" — translate.
- **The panic**: ALL CAPS, stacked exclamation points, red-on-red screaming banners for recoverable cases.
- **The liar**: "Please try again" when the request will deterministically fail again (e.g., invalid credentials, validation errors).
- **The thief**: silently loses the user's work. Always confirm what was saved/preserved.

### Step 5: Respect Security Boundaries

Some errors intentionally stay vague. Coordinate with the `security` skill for these categories:

- **Auth failures**: "Email or password is incorrect" (do not confirm which). Still actionable: "Reset password" link.
- **Authorization**: "You don't have access to this project. Ask the project owner to invite you." Do not reveal existence of protected resources to unauthenticated users.
- **Rate limits on sensitive endpoints**: give a cooldown window, not the exact counter state.

Security vagueness is not an excuse for useless messages — the **Next** and **Escape** components still apply.

### Step 6: Apply the Self-Service Sanity Check

Before shipping, read the rewritten message aloud and ask:

- [ ] Can a non-technical user name what broke?
- [ ] Does it tell them exactly what to try next?
- [ ] Does it preserve their work, or tell them it's lost?
- [ ] Is there a trace ID or error code they can quote to support?
- [ ] Does the tone match the severity (calm for recoverable, serious for data loss)?
- [ ] Would I, half-asleep at 2am, know what to do?

If any checkbox fails, rewrite.

## Examples

### Example 1: Form Validation — Specific, Actionable, Kind

**Before:** `Invalid input.`

**After:** `Phone number must be 10 digits — you entered 9. Example: (555) 123-4567.`

- **What**: phone number wrong length
- **Why**: 9 vs 10 digits (specific)
- **Next**: implicit — add the missing digit, format example shown
- **Escape**: not needed at this severity

### Example 2: Server Error — Preserve Work, Offer Trace

**Before:** `An unexpected error occurred. Please try again or contact support.`

**After:**
> We couldn't publish your post, but your draft is saved.
>
> Our servers hiccuped on the way to the database. Try again in a minute — if it happens twice, [open a ticket] and mention **ref: 7F3A2-B9**.

- **What**: publish failed, draft safe
- **Why**: brief, non-blaming, non-technical
- **Next**: wait and retry
- **Escape**: ticket link with pre-filled trace ID

### Example 3: Auth Failure — Security-Safe Without Being Useless

**Before:** `Login failed.`

**After:** `That email and password don't match. [Reset password] or [create an account] if you're new here.`

Note: does not confirm which field is wrong (security), but still points to the two real next actions.

### Example 4: Full-App Audit Kickoff

User says: *"We keep getting support emails that are just screenshots of error toasts. Do an audit."*

1. Run Step 3 inventory (`references/audit-workflow.md` for grep patterns by stack).
2. Produce a spreadsheet: message text, file:line, trigger frequency (from logs if available), rubric score, proposed rewrite.
3. Sort by frequency × severity; fix the top decile first.
4. For each rewrite, add a trace ID surface if one doesn't exist.
5. Open a PR per subsystem, not one giant PR, so each is reviewable.

## Troubleshooting

### Error: The "why" would leak implementation details

Drop the **Why** line. Keep **What** / **Next** / **Escape**. "Why" is optional; it is never worth a security or confusion cost to force one in.

### Error: There's genuinely nothing the user can do

Say so, and redirect energy usefully: "Our payments provider is down. You don't need to do anything — we'll retry automatically and email you when the charge goes through. [Status page]." The user's next action is "close this and stop worrying", which is a real, valid **Next**.

### Error: Product/legal pushes back on specifics

Push back with data: support-ticket volume traceable to opaque messages. If specifics are truly blocked (compliance, trade secret), the compromise is a trace ID the user can quote — never just "contact support".

### Error: The message has to be short (toast, inline)

Layer the disclosure: short message + "Details" link or expandable. The 4 components don't have to be in one sentence, they have to be in one experience.

### Error: Message is internationalized and rewrite must pass translation

Avoid idioms, contractions, and puns. Keep trace IDs and example values out of the translated string (interpolate them). See `references/rewrite-templates.md` for i18n-safe patterns.
