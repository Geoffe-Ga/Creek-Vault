# `creek-tools-api` — the Adepthood `/v1` HTTP application API

`creek-tools-api` serves Creek's Adepthood-facing capabilities over
authenticated HTTP/JSON. It is the **application** adapter.
[`creek-tools-mcp`](mcp.md) remains the **agent** adapter and is unchanged by
this surface — both call the same `creek_mcp.tools.*` functions, so privacy
admission, auditing and idempotency exist exactly once.

The contract is ratified in
[`docs/decisions/2026-07-31-adepthood-http-application-api.md`](../../docs/decisions/2026-07-31-adepthood-http-application-api.md).
Where this page and that document disagree, the ADR wins. The wire vocabulary is
`creek_mcp/api/models.py`; the published fixture bundle Adepthood vendors and
hash-pins is [`docs/contracts/adepthood-v1/`](../../docs/contracts/adepthood-v1/).

## What is implemented today (#1077)

This is a **tracer**: the thinnest end-to-end path that is honest about what it
does and does not yet do.

| Route | Status |
|---|---|
| `GET /v1/capabilities` | **Implemented.** Real handshake — versions, vault readiness, tier model, capability list. |
| `GET /v1/health` | **Implemented.** Readiness only. Not part of the published contract. |
| `PUT /v1/journal-entries/{external_id}` | **Implemented.** Idempotent journal write over the shared `creek.journal` tool — same tier gates, same audit records, same fragment identity as MCP (#1075). |
| `POST /v1/reflections` | **Implemented.** Anchored margin notes over the shared `creek.reflect` tool. `ok` / `empty` / `escalate` / every refusal are distinguishable from a closed `status` or `code` enum — **no client needs to parse prose to branch**. `notes[].quote` is the only verbatim-guaranteed field: each is validated as a whitespace-normalised span of the submitted or referenced entry, and a span that is not is dropped rather than returned. `essay` is free model prose and is **never** grounding-checked, which is what `essay_grounded: false` (always present, always `false`) tells you — a client must not present it as the writer's own words. A care-flagged entry returns `status: "escalate"` at HTTP **200** with the full `care_signal`, and the model is never called; an escalation is not an error, because a person in acute distress must not land in a client's error path. An `entry_ref` that is above your ceiling and one that does not resolve are **deliberately indistinguishable** — both are `403 privacy_refused` with the same message, because a caller who could tell them apart could enumerate the corpus (#1077). |
| `POST /v1/uploads` | **Implemented.** Idempotent **document** upload over the shared `creek.upload` tool — JSON + base64, never multipart. Body is `{filename, content_base64, external_id, timestamp?, tier}`; the extension of `filename` picks the ingestor and there is deliberately **no `source_type` override** (naming a directory-only ingestor for one file is a silent no-op; whole-archive upload is #1525). `external_id` is the idempotency key: re-sending it updates in place and never mints a second fragment. The response publishes **no `tier`**, on purpose — classification is escalate-only, so a `.md` declaring `intimate` in its own frontmatter lands at `intimate` however modest a tier you declared, and a field claiming the resulting tier could only be false or an oracle. A format Creek must not flatten into one blob (`.json`, `.zip`, `.doc`, …) is `415 unsupported_source` carrying the remedy, never a `500` and never a fragment (#1526). Published at contract `0.8.0` (#1524). |
| `GET /v1/connectors/drive` | **Implemented.** The read-only Google Drive connector's *state*: `connected` / `not_connected` / `expired` / `unsupported`, the granted OAuth scopes (always `.readonly` — the config refuses anything else), and `can_sync`. **No credential is published and none is accepted**: the response has no field a token could sit in, and the model forbids extras. This is the one route that discloses connection state deliberately — a client cannot render a connect button without it — which is exactly why the other two verbs' refusals disclose nothing. Published at contract `0.9.0` (#1527). |
| `POST /v1/connectors/drive/syncs` | **Implemented.** One **incremental** Drive sync over the existing downloader, followed by an ordinary ledger-backed ingest of whatever it fetched. Takes **no request body** — there is no parameter you could usefully set, and a caller-supplied path or file id would be a way to steer the server at part of the owner's Drive they never asked it to touch. The response is **counts only**: no Drive file name, folder name or id, and no `affected_fragment_ids` — a sync's fragments are the vault owner's content, and a list of them would be a corpus enumeration primitive. Files land at the tier `creek ingest` would give them; the route passes **no** `privacy_tier`, so it cannot make any tier less restrictive. A second sync over an unchanged Drive fetches nothing and writes nothing. Refuses with `503 unavailable` when there is no usable credential, rather than falling through to an OAuth flow that would try to open a browser on the server. Published at contract `0.9.0` (#1527). |
| `DELETE /v1/connectors/drive` | **Implemented.** Revoke and erase: posts the refresh token to Google's revocation endpoint (the one journey the credential makes, and it is back to its issuer), then overwrites and unlinks the local token file. Idempotent — disconnecting an already-disconnected connector is a `200` reporting the same state. `remote_revoked: false` means finish the job at Google's account page; it does **not** mean the local erase failed, which is a refusal rather than a success. After this, a sync refuses. Published at contract `0.9.0` (#1527). |
| `GET /v1/wheel` | **Implemented.** The **frequency distribution over the classified corpus** — how many ceiling-admitted fragments sit at each APTITUDE frequency F1–F10, and each frequency's share of the classified total. **This is not a curriculum-progress or fullness measure.** Adepthood's Map validates a ten-member `{aspects: [{stage_number, aspect, fullness}]}` shape from its own 36-week curriculum; that projection is Adepthood's and is owned there (Geoffe-Ga/adepthood#1937). Ten members on both sides is a coincidence of cardinality, not a shared meaning — reading one as the other renders confidently wrong numbers. An empty or missing corpus is `200` with all ten entries at `count: 0`, never `404` and never an error (#1076). |

**Every published route is now built, and none of them was ever allowed to
fake it on the way here.** While a route was unbuilt it answered `501
unsupported_capability` rather than a plausible empty success — no fabricated
`fragment_id`, no `action: "unchanged"`, no zeroed wheel that could be misread
as an empty corpus. A stub that looks like success is worse than one that
admits it is unbuilt: the whole reason epic #1071 exists is that every failure
used to collapse into "vault unavailable", indistinguishable from "no vault
configured", and two repositories shipped a contract neither implemented.

The machinery that enforced it is still in place and still tested. A fifth
capability added to `Capability` before its handler exists is mounted to the
same honest `501` and is left out of the advertised list, because both are
driven by the one `IMPLEMENTED_CAPABILITIES` constant.

`GET /v1/capabilities` advertises only the capabilities actually implemented —
today, all four: `["capabilities", "journal-upsert", "reflections", "wheel"]`.
The advertised list and the
set of routes that answer `501` are driven by a single constant,
`IMPLEMENTED_CAPABILITIES` in `creek_mcp/api/routes.py`, so they cannot
disagree.

> **On the issue's `not_implemented` spelling.** Issue #1074 was written before
> the contract was ratified and asked for `code: "not_implemented"`. `ErrorCode`
> is a closed, published enum and its member for HTTP 501 is
> `unsupported_capability` — "this capability is not implemented here". Minting
> a second member would change every published JSON Schema and the manifest
> hashes Adepthood pins, for no semantic gain, so `/v1` uses
> `unsupported_capability`.

## Running it

```bash
cd creek-tools
uv sync --all-extras

export CREEK_MCP_CONSUMER_TOKENS="adepthood=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
creek-tools-api --config /path/to/creek_config.yaml
```

Equivalent module form, for environments without the console script:

```bash
python -m creek_mcp.httpapi
```

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Bind host. A non-loopback host **requires** TLS; see below. |
| `--port` | `8823` | Bind port. |
| `--config` | unset | Path to `creek_config.yaml`; sets `CREEK_CONFIG` for the process so the vault is resolved the same way the CLI resolves it. |
| `--tls-cert` / `--tls-key` | unset | PEM cert and key. Required together. |
| `--print-openapi` | — | Write the generated OpenAPI document to stdout and exit 0, binding no socket. |

**The default port is deliberately not 8000.** 8000 is both Adepthood's own
backend port and `creek-tools-mcp --transport network`'s default, and the two
Creek adapters are expected to run side by side on one host. A test asserts the
two defaults differ.

## Authentication

`/v1` reuses the MCP surface's bearer-token registry — **the same environment
variable, the same verifier, the same length floor.** It introduces no second
registry and no second floor; two of either is two places to drift out of
lock-step, which is what this epic exists to prevent.

| Variable | Meaning |
|---|---|
| `CREEK_MCP_CONSUMER_TOKENS` | `consumer=token` entries, `;`-separated; a consumer's value may itself be a `,`-separated set of currently-valid tokens (`adepthood=<old>,<new>`) while rotating a secret (#895). Required — there is no anonymous access. |
| `CREEK_MCP_TOKEN_TTL_SECONDS` | Lifetime stamped on a verified token (default 3600). |
| `CREEK_CONFIG` | Path to `creek_config.yaml` (also settable with `--config`). |

Tokens are compared in constant time against every configured token, so a match
leaks no timing signal about which consumer matched. Every token must be at
least **32 characters** (`creek_mcp.token_policy.MIN_TOKEN_LEN`); a shorter one
is refused **at load time** and the server exits non-zero rather than serving a
guessable secret. The refusal names the consumer and the lengths, never the
token value.

Generate and rotate:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Revocation and rotation are different operations, and both end in a
restart.** `CREEK_MCP_TOKEN_TTL_SECONDS` bounds how long an individually
captured `AccessToken` object stays valid; it does **not** revoke the
underlying shared secret, because the bearer is re-verified and re-issued on
every request.

- **Revocation** is immediate and hard: remove a consumer's entry (or a
  single token from its `,`-separated set) from `CREEK_MCP_CONSUMER_TOKENS`
  and restart. The credential stops working the moment the restarted process
  is serving — there is no overlap and no grace period.
- **Rotation** replaces a secret without downtime by holding two
  currently-valid tokens for one consumer at once (`adepthood=<old>,<new>`)
  while the consumer redeploys onto the new one, then dropping the old one.
  It still costs two restarts, but the consumer is never locked out
  mid-swap. Follow the numbered runbook in
  [`docs/mcp.md`](mcp.md#rotating-a-consumers-secret-no-downtime); see also
  [ADR-0009](architecture/ADR/0009-mcp-consumer-token-rotation.md) for why
  the credential is still a static, long-lived secret rather than something
  short-lived.

### The authenticated consumer *is* the audit identity

`/v1` derives the audited `consumer` from the verified token's `client_id` and
**accepts no client-supplied `consumer` field** — not in a body, not in a query
string, not in a header. This fixes a live cross-repo bug in which Adepthood
sent the literal string `CREEK_MCP_CONSUMER` (the name of an environment
variable, not a value) as a `consumer` parameter.

### Authentication happens before routing

The bearer check is ASGI middleware mounted **above the router**. An
unauthenticated request to `/v1/wheel`, to `/v1/nonsense` and to `/` all receive
a byte-identical `401` (but for the correlation id):

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="creek"
Content-Type: application/json

{"code":"unauthenticated","message":"the request carried no valid consumer credential","request_id":"…"}
```

No version strings, no capability list, no vault state, no path. An
unauthenticated caller therefore cannot tell an implemented route from an
unimplemented one from a route that does not exist — a `401`-versus-`501`
difference before authentication would be a free surface map.

`GET /v1/health` is behind the same gate. **Operational consequences, both
deliberate:** a load-balancer liveness probe must present a token, or probe TCP
connect instead — there is deliberately no unauthenticated `/healthz`. And a
probe that sends an inadmissible `X-Creek-Tier-Ceiling` gets `422` rather than
the liveness body, because the ceiling gate has no per-route exemption either.
**Configure the probe to send no ceiling header at all**; absent is the
supported spelling and fails closed to `open`.

## Transport confidentiality

Loopback may serve plaintext. A non-loopback bind **without** `--tls-cert` and
`--tls-key` refuses to start — it exits non-zero naming the missing flags, and
never a credential — rather than starting partially and putting bearer tokens on
the wire in cleartext.

This is the same posture `creek-tools-mcp` has enforced since #837, and it is
literally the same code: both adapters call
`creek_mcp.transport_posture.require_transport_confidentiality`. A second copy
would be a second gate free to drift.

**TLS is transport confidentiality only.** It protects bytes in flight. It is
not encryption at rest, not attestation, and not key custody, and it does not
satisfy an intimate-transit contract. That is entirely
[#757](https://github.com/Geoffe-Ga/Creek-Vault/issues/757)'s.

## Caching

Every response `creek-tools-api` builds carries both of these, on every status:

```
Vary: X-Creek-Tier-Ceiling, Authorization, X-Creek-Contract-Version
Cache-Control: no-store
```

Every `/v1` body is computed from three things a cache cannot see in the URL:
the **declared tier ceiling**, the **authenticated consumer**, and the
**declared contract minor**. An entry keyed on none of them — or on only some —
may be handed to a caller the server would have answered differently. The
version token is the concrete one to picture
([#1144](https://github.com/Geoffe-Ga/Creek-Vault/issues/1144)):
`GET /v1/capabilities` answers `status: ok` with the full capability list to a
client declaring a served minor and `status: incompatible` with an empty list to
one declaring a stale minor, from requests identical in every other respect —
so without that token a cache may hand a current client the refusal minted for
an outdated one, on the endpoint every client calls first.
`Cache-Control: no-store` says do not keep it;
`Vary` says that if you keep it anyway, these are the headers that decide
whether it matches. The two answer different failure modes and neither replaces
the other: drop the directive and a compliant cache is free to store, drop the
tokens and a non-compliant one is free to mismatch.

**These are unconditional, not "on authenticated responses".** Bearer
authentication sits above the router, so all nine operations are authenticated
anyway; and refusals need the treatment as much as successes. A stored `404`
is the concrete case — it is the one status this surface returns that both is
reachable on any unrouted path and is *heuristically cacheable* under RFC 9110
§15.1, so a conforming shared cache may store and reuse it with no freshness
information at all.

**Scope.** This is a promise about responses the application builds — and since
[#1369](https://github.com/Geoffe-Ga/Creek-Vault/issues/1369) and
[#1370](https://github.com/Geoffe-Ga/Creek-Vault/issues/1370) that is every
response the application emits. The two paths that used to escape it both now
enter the builder: a trailing slash is `404 not_found` rather than the router's
own `307`, and a fault in the outermost middleware is the published
`500 internal_error` rather than a bare `text/plain` page. Both therefore carry
these headers.

**If you front this server with a reverse proxy, do not enable caching on
`/v1`.** The [access-log scope note](#request-logging) applies here too: a proxy
that ignores `no-store` is outside this process and nothing here can stop it.

## Privacy tiers

The ceiling arrives on `X-Creek-Tier-Ceiling` and defaults to `open` when absent
— fail closed. See [Caching](#caching) for the two response headers that stop an
intermediary serving one caller's ceiling-filtered response to another.

**`intimate` and `all` are refused at the edge, before any handler runs and
before any vault read is attempted.** `/v1` is remote by construction, so every
caller is capped at `personal` by
`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS` — the transport-neutral owner of
that rule since #1073, shared with the MCP surface rather than reimplemented
here. An inadmissible ceiling is `422 invalid_request`, **not** `403
privacy_refused`: the value came from the caller's own header and no vault
object was resolved or ranked, so `403`'s message ("resolved content exceeds the
declared tier ceiling") would simply be false.

An unrecognised ceiling is refused, never coerced to `open`. Coercion is how a
typo'd or hostile value ends up admitted at *some* ceiling rather than at none.

`intimate` is additionally unreachable *in the type*: `WireTierCeiling` has
exactly two members, so there is no admissible value a producer or consumer
could put on any wire position that names it.

## Versioning

`/v1` is the HTTP major. Below it, one `contract_version` covers both this
surface and MCP.

The seven capability routes — `PUT /v1/journal-entries/{external_id}`,
`POST /v1/reflections`, `GET /v1/wheel`, `POST /v1/uploads` and the three
`/v1/connectors/drive` verbs — require
`X-Creek-Contract-Version: <major.minor>`, for example `0.9`. The comparison is
strict membership against the server's `supported_contract_minors`: a missing
header, a full patch version like `0.2.0`, or anything unrecognised is `409
incompatible_version`, refused before any vault read.

That set is a **window, and it widens before it narrows**. It currently holds
`0.9`, `0.8`, `0.7`, `0.6`, `0.5`, `0.4`, `0.3` and `0.2`. The `0.3.0`, `0.4.0` and `0.6.0` moves all
came from the MCP surface and changed no `/v1` shape — `0.3.0` added
`creek.upload` (#1023), `0.4.0` gave `creek.purge.*` its `partial` status
(#1246), and `0.6.0` gave `creek.purge.*` the `ledger_rows_removed` and
`meta_artifacts_removed` erasure counters (#1453). `0.5.0` (#1372) is the only
one that moved a `/v1` shape: `JournalUpsertResponse` gained an *optional*
`warnings` field, omitted from the payload entirely when the write produced no
advisory. Every client still sending `0.5`, `0.4`, `0.3` or `0.2` is served
exactly as before.

`0.8.0` (#1524) is the first bump since `0.2.0` that **adds a route**:
`POST /v1/uploads` and the `upload` capability. It still costs an older client
nothing, and that is enforced rather than asserted — `CAPABILITY_SINCE_MINOR`
drives both what `GET /v1/capabilities` lists for a given caller and which
callers `POST /v1/uploads` will answer, so a client pinned below `0.8` is not
told the capability exists and is refused `409 incompatible_version` if it
tries the path anyway. Every shape such a client already knows is
byte-identical. The same bump brings `415` into the published status set, with
the `unsupported_source` code; a client pinned below `0.8` cannot meet it,
because the only route that emits it is the one it cannot reach.

`0.9.0` (#1527) is the second such bump, and the first to publish a capability
served by **three** routes: `drive-connector`, over `GET`/`DELETE
/v1/connectors/drive` and `POST /v1/connectors/drive/syncs`. It is additive in
the same enforced sense — one `CAPABILITY_SINCE_MINOR` entry drives both the
advertised list and the route refusal — and it adds **no error code and no new
status**, so a `0.8` client meets nothing new on any route it already calls. It
is also the first template serving two methods; `GET` and `DELETE` on
`/v1/connectors/drive` are separate published operations.

Read the window off `GET /v1/capabilities` rather than assuming the newest
minor is the only one accepted.

**Two routes are exempt.** `GET /v1/capabilities` requires nothing on this axis
because the negotiation endpoint must never itself be able to fail to
negotiate — that is the ADR's rule. When a client sends a version this server
cannot serve, capabilities answers `200` with `status: "incompatible"` and the
server's real version strings, so the client can render "upgrade required"
instead of collapsing to "vault unavailable".

`GET /v1/health` is exempt for a different reason: it is not part of the
published contract at all, so there is no contract version for it to negotiate.
Gating liveness on a contract header would mean a client on the wrong version
could not distinguish "the server is down" from "the server is up and we
disagree about versions" — which are the two facts a probe exists to tell
apart.

## Capability states

`GET /v1/capabilities` is always `200` when the server is reachable, the caller
is authenticated, and the request is well formed. Readiness lives in the body's
`status`, never in the status line — **no condition of the server** can move it
off `200`.

A malformed *request* still fails as it would anywhere else: an inadmissible
`X-Creek-Tier-Ceiling` is `422 invalid_request` here too, because the ceiling
gate has no per-route exemption. Omitting the header always works, so a client
is one step from `200`, and `invalid_request` is a distinct code it can tell
apart from every server-side state.

The ADR records the full reasoning. The short version is that **any** per-route
exemption would make an otherwise route-blind security gate route-aware, and
route-blindness is the property worth keeping — what the gate does is a function
of the declared header alone, so reviewing it never means enumerating the route
table. A *degrade-to-`open`* exemption would leak nothing on its own, and is
refused separately: it would repair a bad value instead of leaving it refused,
breaking the standing rule that an absent ceiling fails closed while a bad one
is never coerced into meaning something.

| `status` | `vault.available` | `capabilities` | Meaning |
|---|---|---|---|
| `ok` | `true` | implemented set | Vault present and usable. |
| `uninitialized` | `false` | `[]` | Reachable; no vault scaffolded. Both version strings still present. |
| `incompatible` | — | `[]` | The requested contract minor is not served here. |
| *no body at all* | — | — | Unreachable. A client must map this to its own distinct state and must **not** fold it into `uninitialized`. |

**A capabilities call against an absent vault is not audited.** There is nowhere
honest to audit it to: `MCPAuditLog.append` creates its own directory tree, so
writing the audit entry would create `00-Creek-Meta/` — the very marker whose
absence is being reported — and the *next* call would then answer `available:
true` for a vault nobody ever initialised. So the handler probes with
`creek_mcp.tools.handshake.vault_available` and only enters `handshake_tool`
once a vault genuinely exists. The equivalent bug on the MCP `creek.handshake`
tool is tracked separately.

## Errors

Every error body is exactly `{code, message, request_id}` and nothing else.
`message` is a constant looked up from the code — never interpolated with caller
or vault data, because a refusal that varies with its input is an
existence-and-rank oracle. Clients must branch on `code`, never parse prose.

| `code` | HTTP | Retry |
|---|---|---|
| `unauthenticated` | 401 | terminal |
| `invalid_request` | 422 | terminal |
| `incompatible_version` | 409 | terminal |
| `privacy_refused` | 403 | terminal |
| `not_found` | 404 | terminal |
| `unsupported_capability` | 501 | terminal |
| `unsupported_source` | 415 | terminal |
| `unavailable` | 503 | retry after operator action |
| `temporarily_unavailable` | 503 | retry with backoff |
| `internal_error` | 500 | retry with backoff |

Retry policy is the static published table (`retry-policy.json`), a pure
function of `code`. There is no `retryable` field on the wire, ever.

**`unsupported_source` is about the caller's own filename, never the vault.**
It is the only code added since contract `0.2.0` (it arrived at `0.8.0`,
#1524) and the only one that can be emitted by exactly one route,
`POST /v1/uploads`. It means the extension names a structured format Creek
must not flatten into one document — a conversation export, an archive, a
legacy binary Office file — and its message is the one place on this surface
where a refusal carries a *remedy*, because a refusal that does not say what to
do instead is one the caller retries verbatim. It is `terminal`: the same bytes
under the same name will never succeed.

**`not_found` is a routing code, never a content code.** It means "no such
endpoint on this server". It is never emitted for a vault object: a caller who
could distinguish "no such fragment" from "you may not see this fragment" could
enumerate the corpus one id at a time without reading a byte of it. Every
vault-object non-answer collapses to `403 privacy_refused` with one fixed
reason. A method mismatch (`PUT /v1/wheel`) also renders `404`, because `405` is
not in the contract's published status set.

**`HEAD` is not served, on any path (#1143).** `HEAD /v1/health`,
`HEAD /v1/capabilities`, `HEAD /v1/wheel` and `HEAD /v1/connectors/drive`
answered `200` up to this change and answer `404 not_found` after it. They were
never declared: Starlette adds `HEAD` wherever `GET` is, so four operations
existed on the wire with no route spec, no schema, no documentation and no
test — and the set had already grown from two to four with nobody deciding to
add them. **Use `GET`.** Nothing is lost by it: authentication sits above the
router, so an anonymous `HEAD` probe already got `401` and was never usable as a
credential-free liveness check, and any caller holding a token can read the
whole of `GET /v1/health`, whose body is a single small object. `HEAD` was also
not the cheap existence check it looks like — the framework runs the full
handler and discards the body, so `HEAD /v1/capabilities` was taking the audit
log's file lock and `fsync` for a response nobody read.

**A trailing slash is `404`, not a redirect (#1369).** `GET /v1/health/` is not
a path this server serves, and it is answered as one. The router's default
`307` was wrong twice over: `307` is outside the published status set, so a
conforming client reads it as "unreachable" the same way it would a `405`; and
the redirect is issued *above* the contract-version gate, so a client speaking a
minor this server does not serve was handed a URL to retry instead of
`409 incompatible_version`. Send the canonical path.

**`404` is keyed on the routing statuses, not on the exception class.** The two
answers the router can reach — `404` for a path it does not serve and `405` for
a verb it does not serve on a path it does — are the only ones that become
`not_found`. Any *other* internal `HTTPException` is a bug in this server rather
than a routing outcome, because every published refusal is returned through the
error table and none is ever raised; it therefore takes the error boundary's
path and renders `500 internal_error`, with the traceback going to the operator
log and nothing but the constant envelope going to the caller. Folding those
into `404` instead would have made the promise directly above this paragraph
false, and would have hidden the fault from the operator as well as the client.

## Hardening

| Limit | Default | Behaviour when exceeded |
|---|---|---|
| Request body size | 1 MiB, except `POST /v1/uploads` at ~13.4 MiB | `422 invalid_request`. Enforced on `Content-Length` *and* on streamed bytes, so a chunked request cannot bypass it, and the body is never buffered past the cap. The upload route declares its own cap (`ROUTE_BODY_CAPS`, keyed on the literal path) because base64 of the tool's 10 MiB document limit does not fit in a limit sized for a journal entry — matching the 10 MiB cap Adepthood already enforces. Raising the *global* cap instead would let every route commit that memory; lowering `max_body_bytes` therefore does **not** lower the upload route, whose cap is published contract. |
| Per-request timeout | 30 s | `503 temporarily_unavailable`. |
| Concurrent requests | 32 | `503 temporarily_unavailable`. |

The concurrency limit is process-global, not per-consumer, so one consumer can
in principle starve the others; per-consumer rate limiting is a tracked
follow-up.

**The per-request timeout is a cancel scope evaluated on the event loop**,
so it can only fire while the loop is free to run it. That is why
`GET /v1/capabilities`'s readiness probe is dispatched to a worker thread
rather than awaited inline — otherwise a blocked loop would mean the
deadline could never be reached, let alone enforced, on the one endpoint
every client calls first. Be precise about what that buys: anyio cannot
cancel a worker thread, so on timeout the request is abandoned at the HTTP
layer while the filesystem work it kicked off keeps running to completion in
the background. That is strictly better than a deadline that cannot fire at
all — but it is not cancellation, and no code here should be read as
promising that it is.

### Request logging

One structured line per request carrying method, **route template**, consumer,
status and duration. It never records a request body, a token, or a fragment id
— and it logs the route *template* (`/v1/journal-entries/{external_id}`), never
the concrete path, so an identifier does not reach this log through the URL.
That sentence is about *this* log line; the two paragraphs below say what it
takes for the process as a whole to keep the promise, and where the promise
stops. Read all three, because the first on its own was once true of Creek's
own middleware while the process still published identifiers elsewhere.

**That guarantee is about the whole process, not just Creek's own
middleware.** `creek-tools-api` starts uvicorn with `access_log=False`,
because uvicorn ships its own access logger — on by default — that runs
*alongside* the middleware above, not instead of it, and writes the client
address and the concrete path with its query string on every request:
`INFO: 127.0.0.1:59272 - "PUT /v1/journal-entries/zz-sentinel-external-id-9x7q-zz HTTP/1.1" 200 OK`.
Left on, that second logger would republish the caller's IP and a
consumer-chosen identifier on every sync — making the promise above false
without a single line of Creek's own code being wrong.

**Scope: this is a promise about `creek-tools-api` itself, not about
whatever sits in front of it.** A reverse proxy, load balancer, or TLS
terminator placed in front of this server logs the full request line by
default, and that log lives outside this process — nothing here can redact
it. An operator who fronts `creek-tools-api` with one must suppress or
template the path at that layer too; the guarantee above does not, and
cannot, reach past the process boundary.

This is ordinary stdlib logging, deliberately **not** the vault audit log. An
audit entry per request would put a hash-chained write on an unauthenticated
path (a denial-of-service amplifier) and would pollute the tool-invocation
trail.

### Fault logging

A `500` also writes one `ERROR` record, with the traceback, to a **second**
logger: `creek_mcp.httpapi.error`. It carries the `request_id` and nothing
else about the request — route and consumer are already on the access line,
joined by that same id.

The response body does not change: the caller still gets exactly
`{code, message, request_id}` with the constant `internal_error` message, and
never the traceback, the exception class or a source path. The traceback is for
the operator; without it a `500` is a fault that can be counted but not
diagnosed.

**Route the two loggers separately.** The access line is five fields chosen to
be safe to ship. A traceback carries whatever the exception's own message
carried, which can include vault content — so it wants its own handler,
destination and retention rather than inheriting the access log's.

### ASGI scopes

The server answers `http` and relays `lifespan`. Any other scope type — a
`websocket`, or anything a future server introduces — is **refused at the
outermost middleware** rather than passed down. A passthrough written as
"anything that is not `http`" would hand the first `websocket` route anyone
mounted a path through the whole stack with no authentication, no ceiling gate
and no access line, and the router would answer it rather than error. There is
no `ws://` surface today; the allowlist is what keeps that true by
construction.

## OpenAPI

```bash
creek-tools-api --print-openapi > openapi.json
```

The document is generated from the Pydantic models in `creek_mcp/api/models.py`
plus the route table in `creek_mcp/api/routes.py` — **not** by introspecting a
live framework app. That is the point: a framework's generated OpenAPI is a
function of the framework's version, so a routine dependency upgrade would
silently rewrite a document a consumer has pinned. Generating from our own
models makes it a function of our models alone. It is also why `/v1` uses
Starlette rather than FastAPI (see the ADR's "HTTP framework" section for all
four reasons).

**The document declares the bearer requirement it has always enforced (#1371).**
`components.securitySchemes.bearerAuth` is an OpenAPI `http`/`bearer` scheme —
the `Authorization` header, by RFC 7235 — and a document-level `security`
requirement applies it to every operation, including `GET /v1/health`. No
operation opts out, because none of them is anonymous: the bearer check sits
above the router. A client generated from this document therefore ships with a
place to put the credential.

**Path parameters are published with their bounds (#1132).** `{external_id}`
carries `minLength`, `maxLength` (the same `MAX_EXTERNAL_ID_CHARS` both write
surfaces enforce) and a `pattern` excluding ASCII control characters, `DEL` and
`/`. The pattern is deliberately looser than the server's own
`admissible_external_id` — which also refuses whitespace-only ids and
non-printable characters outside ASCII — because a published constraint tighter
than the server would make a generated client refuse, client-side, an id the
vault already holds.

**The document is generated on demand and is not committed.** Its agreement with
the published bundle is pinned by tests rather than by a second stored artifact:
every `components/schemas/<Model>` in the generated document is checked against
the committed `docs/contracts/adepthood-v1/schemas/<Model>.schema.json` read
from disk, modulo one mechanical `$defs` → `components/schemas` reference
rewrite that the two formats require. The document's `(path, method)` set is
checked against the routes actually mounted on the app, so a route added without
a `RouteSpec` turns the test red.

Whether the OpenAPI document should itself join `build_bundle()` and the
published fixture bundle at the next contract minor is a tracked follow-up. It
does not today, so — stated plainly — the *file* an operator generates is not
hash-pinned the way the fixture bundle is; only its schema content is pinned, by
the tests above.

## Why the adapter is not in `creek_mcp/api/`

`creek_mcp/api/` holds the published contract artifacts — the wire models, the
route table, the fixture-bundle builder and the OpenAPI generator — and a test
AST-sweeps it to prove none of them imports a web framework. That invariant is
what guarantees the published schemas and the OpenAPI document are functions of
our Pydantic models alone.

The Starlette adapter therefore lives beside it, in `creek_mcp/httpapi/`, rather
than under it. The sweep stays whole.
