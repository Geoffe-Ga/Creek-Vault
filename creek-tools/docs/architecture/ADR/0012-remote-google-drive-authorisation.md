# ADR-0012: Remote Google Drive authorisation keeps the redirect at the caller

- **Status**: Accepted
- **Date**: 2026-08-22
- **Driving issues**: #1568, closing the last unmet clause of the seeding epic
  #1523. Builds on #1527 (the three `drive-connector` routes) and #1524 (the
  `upload` capability those routes were shaped after).

## Context

`/v1` can sync Google Drive and disconnect it, and cannot **connect** it.
`POST /v1/connectors/drive/syncs` refuses with the single
`NOT_CONNECTED_REASON` until a credential exists on the host, and the only way
to mint one today is `creek gdrive --download` on that host:

```python
# creek/ingest/gdrive.py, in GoogleDriveDownloader._get_service
flow = InstalledAppFlow.from_client_secrets_file(
    self.config.credentials_file,
    self.config.scopes,
)
creds = flow.run_local_server(port=0)
```

`run_local_server(port=0)` opens a browser **on the server** and listens on a
loopback port for the redirect. Neither half of that is reachable by a remote
consumer, so the seeding promise — "a user connects Drive over the network,
with no CLI and no shell access" — fails on its first clause and only its
first clause. `docs/seeding.md` has said so in as many words since #1528.

Two facts about this deployment decide the shape of the answer.

### 1. `/v1` has no anonymous path, and a browser redirect carries no bearer

`BearerAuthMiddleware.__call__` (`creek_mcp/httpapi/auth.py`) has no path
allowlist at all. It sits above the router in the stack `create_app` builds and
authenticates the whole `http` scope; `/`, `/v1/health` and an unrouted
`/v1/nonsense` all answer the same `401`. `build_verifier` refuses to *start* a
deployment that has no consumer configured, and the sentence it raises is the
invariant:

> `/v1` has no anonymous access, so it refuses to serve without authentication
> configured.

An OAuth redirect is a browser navigation issued by Google's servers. It
carries no `Authorization` header and never will. So **any callback endpoint
mounted on this server would be the first anonymous exemption this gate has
ever had.** That is not a status-code detail: it converts a middleware whose
correctness argument is "it is total" into one whose correctness argument is "it
is total except here", and every later reviewer has to re-derive why the
exception is safe. `tests/test_v1_api_auth.py::test_no_path_is_exempt_from_the_bearer_gate`
pins the current state, callback-shaped paths included, so the exemption cannot
be added without editing a test that names what it is giving up.

### 2. The host already holds a Google client secret — but a *public* one

`GoogleDriveConfig.credentials_file` defaults to `credentials.json`
(`creek/config.py`) and `InstalledAppFlow.from_client_secrets_file` reads it on
every unauthorised run. So "the server would hold the operator's client secret"
is not the change; the file is already expected to be there.

What changes is the client **type**. An installed-app client is a *public*
client: OAuth 2.0 treats its `client_secret` as non-confidential by design,
because it ships inside distributable software and cannot be kept. A web client
is a *confidential* client, and its secret is the one value standing between a
stolen authorization code and a minted refresh token. The honest statement of
the posture delta is therefore:

> **A value that was never a secret becomes one.**

That dictates storage (owner-only, never in the vault, never in an audit line),
rotation (an operator action, at Google's console), and blast radius (a leaked
web-client secret plus any intercepted code is a Drive credential; a leaked
installed-app secret alone is not).

## Decision

**Option C. Creek holds the web client secret and mints the authorization URL;
the *caller* owns the redirect URI and the browser leg; the authorization code
comes back to Creek over the caller's existing bearer.**

Two authenticated routes, both under the existing `drive-connector`
capability, at contract `0.11`:

| Route | Body | Answers |
|---|---|---|
| `POST /v1/connectors/drive/authorizations` | `{redirect_uri}` | `{authorization_url, state}` |
| `POST /v1/connectors/drive/authorizations/{state}` | `{code}` | `DriveConnectorStatusResponse` |

The flow: the consumer asks Creek for an authorization URL naming the
consumer's own redirect URI; it sends its user there; Google redirects the
user's browser back to **the consumer**, which is a host the consumer already
operates with TLS it already owns; the consumer relays the `code` to Creek on
the second route, authenticated as itself. Creek exchanges the code for a
credential server-side — the exchange carries the client secret, so it never
leaves this host — writes it through the existing `_write_token_file`, and
answers with the connector's *state*, which is what the caller actually needs
to render its next screen.

No new capability name. `drive-connector` already covers "the connector's state
and its verbs", and the reasoning `Capability` gives for keeping status and
sync together applies verbatim to connecting it: a consumer cannot usefully
negotiate "may I sync" apart from "may I connect", and a server advertising
sync-without-connect would be advertising half a connector.

### The options that were not taken

**Option A — the caller supplies its own Google credential per request.**
Creek holds nothing; every consumer registers its own Google client and passes
a token in. Rejected on two counts. It puts a live OAuth credential in a `/v1`
request body, which is precisely the material `DriveConnectorStatusResponse`
and the three #1527 refusals were shaped to keep off this wire; and it makes
every consumer a Google OAuth verification applicant, which is a weeks-long
process per consumer for a connector the vault owner already owns.

**Option B — server-held web client secret and a server-owned public callback.**
The textbook web flow. Rejected because its only distinguishing costs are
exactly the two this deployment cannot pay cheaply: a public hostname with TLS
**this** server controls, which a self-hosted per-user Creek (ADR-0007) does not
have; and the anonymous-path exemption of §1, which is a permanent widening of
the authentication surface bought to serve one endpoint, once, per connection.

**Option C pays neither.** It satisfies every acceptance criterion Option B
does, at strictly lower cost, because the leg that needs a public address is
the leg the caller was always going to own anyway.

### What `state` is for here, and what it is not

`state` is minted by `secrets.token_urlsafe`, stored server-side beside the
Drive ledger — with the `redirect_uri` and the PKCE verifier it was issued
under — single-use and expiring. In Option B it would be the CSRF
defence for a browser Creek never authenticated. Here it is not load-bearing
against a browser at all — Creek never sees one. It binds the code to an
authorization *this server issued*, so a code obtained against some other
client, or replayed after use, or presented after the window closed, is refused
without an exchange being attempted.

Because it is not a CSRF token, the refusals it produces are free to be
uniform, and they are: unknown, expired and already-consumed states earn one
constant reason, byte-identical but for the correlation id. Neither route reads
the token file, so no refusal from either can disclose whether a credential
already exists.

### PKCE spans the two legs, so the verifier is stored with the `state`

`google_auth_oauthlib` puts a `code_challenge` in every authorization URL it
builds and sends the matching `code_verifier` at exchange time, generating one
lazily onto the `Flow` object if it was not given one. That default is written
for a single-process installed-app flow, where one `Flow` does both legs. Here
the legs are two HTTP requests and therefore two `Flow` objects, so a verifier
left to the library would be published as a challenge by the first leg,
discarded with that object, and absent from the second — and Google would
refuse **every** authorization with `invalid_grant`, indistinguishably from a
state this server had forgotten.

So the verifier is minted up front by `creek.ingest.gdrive_grant.new_code_verifier`,
passed explicitly into `build_flow` (which pins
`autogenerate_code_verifier=False`, so no flow can quietly mint its own), and
persisted in the store entry beside the `redirect_uri`. That is the second
reason the store is written `0o600` from byte zero: on its own a verifier buys
nothing, but paired with an intercepted code it is half of a redemption. The
relation is pinned end to end — `S256(verifier) == challenge` across the two
requests — by
`tests/test_v1_api_drive_authorisation.py::test_the_exchange_presents_the_verifier_the_url_committed_to`.

### Scopes route through the config validator, not around it

`GoogleDriveConfig.validate_readonly_scopes` refuses to construct a config
naming anything but `drive.readonly`. An authorization URL builds its own
scope list, and a remote connect button is the one place a write scope could
be requested without a config edit — so the grant re-runs that same validator
on the scope list it is about to send, rather than trusting that the config it
was read from was validated. The check is one call and the failure is a refusal,
not a widened grant.

## Consequences

- **`credentials.json` must now be a *web* client**, not an installed-app one,
  for the remote grant to work. The CLI path (`creek gdrive --download`) is
  unchanged and still works with either; a deployment that never uses the
  remote grant need not change its client at all. The remote routes refuse —
  with the operator-actionable `unavailable` — when the file is absent,
  unreadable, or not a web client.
- **The operator now holds a confidential secret.** `credentials.json` should
  be owner-only and outside the vault, and rotating it at Google's console
  invalidates outstanding authorizations, which is the intended behaviour.
- **The authentication gate is untouched.** No middleware exemption, no
  anonymous route, no second token registry. `/v1` still refuses every
  unauthenticated request on every path.
- **The caller gains a responsibility**: it must serve the redirect URI, and it
  must not hand the `code` to anyone else. A code is single-use and expires in
  minutes, and it is worthless without the client secret this server holds, so
  the failure mode of leaking one is bounded — but it is not nil.
- **Nothing about what a sync fetches changes.** #1568 mints a credential; it
  opens no new fetch path. The tier machinery a synced fragment passes through
  is the one #1527 pinned, at
  `tests/test_v1_api_drive.py::test_a_synced_fragment_carries_the_tier_creek_ingest_would_give_it`.
- **Contract `0.11` is additive.** A client pinned below `0.11` is neither told
  the routes exist nor served them, exactly as `upload` (0.8), `drive-connector`
  (0.9) and `pipeline` (0.10) were.

## Revisit predicate

Revisit this ADR when **Creek gains a public, TLS-terminated hostname it
controls per user** — the deployment ADR-0007 describes but does not yet
provision. At that point Option B's first cost disappears and only the
anonymous-path exemption remains, which may then be worth paying to spare every
consumer the redirect leg.

Revisit it sooner if a consumer appears that **cannot** serve a redirect URI at
all. That consumer needs Option B, and the exemption it requires should be
argued on its own terms here, in this file, rather than added to
`BearerAuthMiddleware` as a special case.

Until then, do **not** mount an anonymous callback route "just for OAuth". The
gate's correctness argument is that it is total; the moment it is not, the
argument has to be rebuilt from scratch every time someone reads it.
