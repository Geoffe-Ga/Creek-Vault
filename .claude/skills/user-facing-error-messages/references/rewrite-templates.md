# Rewrite Templates

Copy-pasteable before/after pairs organized by error category. Each template applies the four-part rubric (What / Why / Next / Escape) from `SKILL.md`.

## Form & Input Validation

### Required field missing

- **Bad:** `This field is required.`
- **Good:** `Email address is required — we'll send your receipt here.`

Include the *purpose* of the field so the user sees why we're asking.

### Format mismatch

- **Bad:** `Invalid date.`
- **Good:** `Use the format MM/DD/YYYY — for example, 04/13/2026.`

Always show one concrete example in the right format.

### Length / range

- **Bad:** `Password does not meet requirements.`
- **Good:** `Passwords need at least 12 characters. Yours has 7 — try a memorable passphrase like "tree-river-sunset".`

Name the requirement, name the actual value, and suggest an approach.

### Uniqueness / taken

- **Bad:** `Username unavailable.`
- **Good:** `"geoff" is taken. Try **geoff2026**, **geoff-g**, or [sign in] if it's you.`

Offer suggestions derived from the user's input.

## Authentication & Authorization

### Bad credentials

- **Bad:** `Authentication failed.`
- **Good:** `That email and password don't match. [Reset password] or [create an account].`

Do not disclose which field was wrong. Both next actions still work.

### Session expired

- **Bad:** `Unauthorized.`
- **Good:** `You were signed out after 30 minutes of inactivity. [Sign in again] — your draft is saved.`

Name the cause in user terms. Confirm preserved work.

### Permission denied

- **Bad:** `403 Forbidden.`
- **Good:** `You don't have access to the "Marketing" workspace. Ask its owner, **Sam Rivera**, to invite you — or [switch workspaces].`

Name the missing grant and the person who can give it.

### MFA / 2FA failure

- **Bad:** `Invalid code.`
- **Good:** `That 6-digit code didn't match. Codes expire every 30 seconds — open your authenticator app and try the newest one. [Lost access?]`

Explain the time-boxing and give the lost-device escape hatch.

## Network / Service Failures

### Transient server error

- **Bad:** `Something went wrong. Please try again.`
- **Good:**
  > We couldn't save your changes — our server hiccuped.
  >
  > Your work is still in the form. [Try again] — if it fails twice, check [status.example.com] or [open a ticket] (ref: 7F3A2).

Confirm preserved state; give retry, status page, and ticket path.

### Offline / network down

- **Bad:** `Network error.`
- **Good:** `You're offline. We'll save your changes as soon as you're back on the internet — don't close this tab.`

Own the user's fear (data loss). Tell them the action they *shouldn't* take.

### Upstream/3rd-party down

- **Bad:** `Gateway timeout.`
- **Good:** `Our payment processor (Stripe) is slow right now. Your card wasn't charged. [Try again] in a minute, or [choose a different payment method].`

Name the upstream plainly. Confirm no side effects.

### Rate limited

- **Bad:** `429 Too Many Requests.`
- **Good:** `You've hit the limit of 60 messages per minute. You can send again in **42 seconds**.`

Quantify the limit and the countdown. Avoid giving attackers exact state on sensitive endpoints — coordinate with `security` skill.

## Payment & Billing

### Card declined

- **Bad:** `Payment failed.`
- **Good:** `Your card ending in **1234** was declined by the issuing bank. Try a different card or contact your bank — we never see why they declined.`

Say what you know and what you *don't* know. Prevent users from blaming you.

### Subscription lapsed

- **Bad:** `Access denied.`
- **Good:** `Your Pro subscription ended on April 1. [Renew now] to restore team members, integrations, and analytics — nothing has been deleted.`

Reassure about data. List what they're missing to create a clear value case.

## Data / Storage

### Not found

- **Bad:** `404 Not Found.`
- **Good:** `We couldn't find a project at that link. It may have been deleted or renamed. [See all your projects] or [search].`

Offer two ways to rediscover.

### Conflict / stale edit

- **Bad:** `409 Conflict.`
- **Good:** `Alex edited this page after you opened it. [See their changes], [overwrite with yours], or [merge both].`

Name the other actor. Give a real choice.

### Upload too large

- **Bad:** `File too large.`
- **Good:** `That file is **18 MB** — the limit is **10 MB**. [Compress it here] or email it as a link instead.`

Actual size, actual limit, actual alternative.

## CLI / Developer Tools

CLIs *are* a UI. The same rubric applies, adjusted for a technical audience.

- **Bad:** `ENOENT`
- **Good:**
  ```
  ✗ Couldn't find config file at ./well-worn.toml

    We checked: .  ../  ~/
    Run `well-worn init` to create one, or pass --config <path>.
  ```

Show the search path. Name the fix command.

## Empty / Zero-State Failures

Failure isn't always red. Sometimes it's an empty page.

- **Bad:** `No results.`
- **Good:** `No projects match "archive 2019". Try removing a filter, or [clear all filters] to start over.`

Name the query, name the levers.

## i18n-Safe Patterns

Messages that must translate cleanly:

- Interpolate variables — don't concatenate strings.
- Avoid contractions, idioms, puns.
- Keep trace IDs, example values, and numbers out of the translatable string.

```python
# Bad
msg = "Couldn't find " + name + " — did you mean " + suggestion + "?"

# Good
msg = _("We couldn't find {name}. Did you mean {suggestion}?").format(
    name=name, suggestion=suggestion
)
```

## Tone Ladder

Pick severity, match tone:

| Severity | Example scenario | Tone | Example opening |
|----------|-----------------|------|-----------------|
| Low | Typo in form | Helpful, light | "Almost! …" |
| Medium | Request failed, recoverable | Calm, factual | "We couldn't save …" |
| High | Data loss possible | Serious, direct | "Your changes weren't saved …" |
| Critical | Security / account takeover | Urgent, action-first | "Sign out of all devices now …" |

Never use exclamation points on Medium+. Never use "Oops" or "Uh oh" on High+.
