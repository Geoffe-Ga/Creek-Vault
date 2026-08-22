# ADR-0009: MCP consumer token rotation — overlapping static tokens, not short-lived credentials

- **Status**: Accepted
- **Date**: 2026-08-09
- **Driving issue**: #895 (P1, security). Builds on #759 (per-consumer bearer
  auth for the network transport), #837 (verified-token TTL), #838 / #907
  (shared 32-char entropy floor).

## Context

`CREEK_MCP_CONSUMER_TOKENS` maps each remote consumer (Adepthood, in
practice) to one static bearer secret. Rotating that secret was a hard
cutover: swap the value, restart, and every instance of the consumer still
holding the old token loses access until it redeploys with the new one.
There was no way to widen the credential and then narrow it — only replace.

The #837 TTL stamped on a verified `AccessToken` does not help here. It
bounds how long one *captured* token object stays valid after
`verify_token` issues it; it says nothing about the *configured* secret,
which the SDK's bearer middleware re-checks against `verify_token` fresh on
every request. A consumer that keeps presenting the same configured value is
re-verified indefinitely. Confusing the two — reading the TTL as a rotation
mechanism — is exactly the gap #895 exists to close.

A fix had to satisfy three things at once: it could not require the
Adepthood client to change what it puts on the wire (a coordinated
cross-repo change is out of scope for a P1 fix landing in this repo), it
could not weaken the existing entropy floor (#838) or the constant-time,
never-short-circuiting comparison the verifier already does, and it had to
make an operator's rotation *safe by construction* rather than merely
possible if done carefully.

## Decision

A consumer maps to an **ordered set of currently-valid tokens**, not a
single token. The wire format for `CREEK_MCP_CONSUMER_TOKENS` gains one new
separator: a `,`-separated list inside one consumer's entry —
`adepthood=<old>,<new>` — names every token that currently authenticates as
that consumer. A single-token entry is unchanged and is the steady state; an
operator who never rotates anything sees no difference.

Rotation is then: widen the set (add the new token alongside the old),
restart, let the consumer redeploy onto the new token, narrow the set back
to one (drop the old token), restart again. No cutover moment exists —
both tokens work throughout the window — and the window closes by an
explicit, later action rather than by an initial swap. The
`docs/mcp.md` network-transport section carries the numbered, copy-pasteable
version of these steps as the operational contract this decision commits to;
an ADR that only describes the shape of a runbook without publishing it
would not actually fix the operational hazard #895 reports.

Two configurations that this widened grammar makes *possible* are refused at
load time rather than silently resolved, because both previously had a
silent-but-wrong behaviour and now have an available syntax for what the
operator actually means:

- **A consumer named twice** (`adepthood=a;adepthood=b`) used to have the
  second entry silently overwrite the first — discarding a credential the
  operator believed was live. Now that a consumer can legitimately hold
  several tokens, the alternative reading (accumulate both entries) is
  exactly as wrong: it invents a rotation window nobody asked for. Both
  readings rewrite intent, so this is refused and the error names the
  supported comma spelling.
- **One token value configured for more than one consumer** — across two
  consumers, or twice within one consumer's set — is refused because
  `ConsumerTokenVerifier.verify_token` scans every configured token without
  breaking early (a deliberate anti-timing-oracle property) and keeps the
  *last* match. A shared value would therefore audit a call under whichever
  consumer the scan happens to visit last, which is a wrong identity on the
  audit log, not just a redundant configuration.

Startup announces, on stderr, the name and token count of every consumer
holding more than one token — never a value — so an open rotation window is
visible in logs, terminals, and process supervisors rather than a fact only
the environment variable itself remembers. The 32-character floor from #838
applies to every token in a set, not only the first, because a rotation is
exactly the moment a fresh secret is typed in.

## Rejected

- **Short-lived derived credentials.** Instead of a static secret, mint a
  time-boxed, HMAC-signed credential from the configured secret (as OAuth
  bearer/refresh pairs or signed capability tokens do), so a leaked
  credential expires on its own. This is the more thorough fix and is not
  dismissed on its merits — it is **deferred**, because it changes what the
  Adepthood client puts on the wire: today's client sends the configured
  secret verbatim as a bearer token, and a derived-credential scheme would
  require it to first exchange that secret for a short-lived one (a token
  endpoint, a refresh flow, or equivalent). That is a coordinated
  client-contract change across two repositories, which cannot ride inside a
  P1 security fix landing unilaterally in `creek-tools`. This ADR is the
  record of why it was set aside rather than silently forgotten: the
  short-lived-credential path is real future work, scoped as a coordinated
  change with the Adepthood client, and is tracked as **#1267** (P2,
  `follow-up` / `scan:security`) rather than folded into #895.
- **Per-token expiry timestamps in the env grammar** (for example
  `adepthood=<token>@<unix-ts>`). This would let a rotation window close
  itself without a second restart, which is attractive on its face. It was
  rejected because it adds a **clock-dependent refusal path**: a token
  parsed as "expired" on a host with a skewed clock, or restored from a
  stale backup of the env file, silently drops a consumer's access with no
  local signal beyond a 401 the operator has to go debug — trading a
  visible, operator-driven revocation step for an invisible, clock-driven
  one. The chosen design keeps every expiry decision an explicit, restart-
  gated operator action instead.
- **An ADR that accepts the status quo.** Recording "this is a known
  limitation, no change" was on the table only in the sense that doing
  nothing is always an option. It is rejected outright: #895 is a P1
  security item because the previous design had no non-disruptive rotation
  path at all, and a decision record that ratifies the absence of a fix is
  not a decision, it is a non-fix wearing a decision's format.

## Consequences

- **Positive**: a consumer's secret can be rotated with no downtime and no
  window where the consumer is locked out; a repeated consumer name and a
  shared token value — both previously silent misconfigurations — are now
  load-time errors; the 32-character floor and the constant-time,
  non-short-circuiting comparison are preserved unchanged for every token in
  a set, not weakened to accommodate the set.
- **Negative, stated plainly**:
  - The credentials are still **static, long-lived secrets** copied into an
    environment variable. This decision does not make them short-lived,
    does not add signing or scoping beyond what existed, and does not
    change the trust model of the network transport.
  - **Revocation still requires an env edit and a restart.** Nothing here
    adds revocation-without-restart; a consumer's access cannot be pulled
    instantly without stopping the process that serves it.
  - **The rotation window is operator-bounded, not system-bounded.** Widening
    a token set is the only automatic part; narrowing it back to one is a
    manual pair of steps (steps 5–6 of the runbook — step 5 edits the
    environment variable, and **step 6, the second restart, is what actually
    revokes the retired secret**) that nothing enforces. An operator
    who runs the widening restart and never runs the narrowing one has
    **permanently** widened the consumer's valid-credential set — the old
    token keeps authenticating indefinitely. The startup rotation notice is
    the only pressure against that outcome; it is a reminder, not a
    guardrail, and an operator who stops reading startup logs loses it.
  - **Global token-value uniqueness is now mandatory.** An operator who was
    (knowingly or not) relying on one value covering two consumer names —
    or two entries for one consumer, where the second silently won — must
    now give each consumer its own distinct token(s); the old configuration
    fails closed at startup rather than serving with a surprising identity
    mapping.
  - **A repeated consumer name is now a hard startup error** where it was
    previously accepted and silently resolved (by overwrite). Any deployment
    script that regenerated `CREEK_MCP_CONSUMER_TOKENS` by naive
    concatenation, relying on the last write winning, now fails fast instead
    of quietly keeping only its last entry.

## Revisit when

- The Adepthood client contract is renegotiated for another reason (a new
  auth scheme, a token-exchange endpoint, a move off bearer-in-env
  entirely). At that point, re-open **#1267** — the deferred
  short-lived-derived-credential option above — rather than adding it as an
  unplanned addendum to a static scheme.
- An operator incident traces back to a rotation window left open past its
  intended lifetime — the deferred risk this ADR names explicitly under
  "Negative" above — which would argue for the window to be system-enforced
  (a maximum-age refusal, for example) rather than only operator-remembered.
- A regulatory or product requirement demands revocation-without-restart.
  That is a materially different transport/verification design (likely
  requiring the derived-credential path above) and would be its own ADR, not
  an amendment to this one.
