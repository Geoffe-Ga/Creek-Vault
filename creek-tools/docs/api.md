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

## What is implemented today (#1074)

This is a **tracer**: the thinnest end-to-end path that is honest about what it
does and does not yet do.

| Route | Status |
|---|---|
| `GET /v1/capabilities` | **Implemented.** Real handshake — versions, vault readiness, tier model, capability list. |
| `GET /v1/health` | **Implemented.** Readiness only. Not part of the published contract. |
| `PUT /v1/journal-entries/{external_id}` | `501 unsupported_capability` (#1075). |
| `POST /v1/reflections` | `501 unsupported_capability` (#1076). |
| `GET /v1/wheel` | `501 unsupported_capability` (#1077). |

**The three unbuilt routes return an error, never a plausible empty success.**
No fabricated `fragment_id`, no `action: "unchanged"`, no zeroed wheel that
could be misread as an empty corpus. A stub that looks like success is worse
than one that admits it is unbuilt: the whole reason epic #1071 exists is that
every failure used to collapse into "vault unavailable", indistinguishable from
"no vault configured", and two repositories shipped a contract neither
implemented.

`GET /v1/capabilities` advertises only the capabilities actually implemented —
today, `["capabilities"]`. #1075–#1077 each add one. The advertised list and the
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
| `CREEK_MCP_CONSUMER_TOKENS` | `consumer=token` pairs, `;`-separated. Required — there is no anonymous access. |
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

**Rotation is the only logout.** `CREEK_MCP_TOKEN_TTL_SECONDS` bounds how long
an individually captured `AccessToken` object stays valid; it does **not** revoke
the underlying shared secret, because the bearer is re-verified and re-issued on
every request. To revoke a consumer, remove or replace its entry in
`CREEK_MCP_CONSUMER_TOKENS` and restart.

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

## Privacy tiers

The ceiling arrives on `X-Creek-Tier-Ceiling` and defaults to `open` when absent
— fail closed. Every response carries `Vary: X-Creek-Tier-Ceiling`, so an
intermediary cache cannot serve one caller's ceiling-filtered response to
another.

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

The three capability routes — `PUT /v1/journal-entries/{external_id}`,
`POST /v1/reflections` and `GET /v1/wheel` — require
`X-Creek-Contract-Version: <major.minor>`, for example `0.2`. The comparison is
strict membership against the server's `supported_contract_minors`: a missing
header, a full patch version like `0.2.0`, or anything unrecognised is `409
incompatible_version`, refused before any vault read.

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
| `unavailable` | 503 | retry after operator action |
| `temporarily_unavailable` | 503 | retry with backoff |
| `internal_error` | 500 | retry with backoff |

Retry policy is the static published table (`retry-policy.json`), a pure
function of `code`. There is no `retryable` field on the wire, ever.

**`not_found` is a routing code, never a content code.** It means "no such
endpoint on this server". It is never emitted for a vault object: a caller who
could distinguish "no such fragment" from "you may not see this fragment" could
enumerate the corpus one id at a time without reading a byte of it. Every
vault-object non-answer collapses to `403 privacy_refused` with one fixed
reason. A method mismatch (`PUT /v1/wheel`) also renders `404`, because `405` is
not in the contract's published status set.

## Hardening

| Limit | Default | Behaviour when exceeded |
|---|---|---|
| Request body size | 1 MiB | `422 invalid_request`. Enforced on `Content-Length` *and* on streamed bytes, so a chunked request cannot bypass it, and the body is never buffered past the cap. |
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
`INFO: 127.0.0.1:59272 - "PUT /v1/journal-entries/zz-sentinel-external-id-9x7q-zz HTTP/1.1" 501 Not Implemented`.
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
