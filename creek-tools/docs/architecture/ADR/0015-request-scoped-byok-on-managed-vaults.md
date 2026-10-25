# ADR-0015: A writer's request-scoped key powers a managed vault

- **Status**: Accepted (owner decision 2026-10-16)
- **Date**: 2026-10-16
- **Driving issue**: #1873; sibling Geoffe-Ga/adepthood#3437 (which amends
  adepthood ADR 0009 to match)
- **Amends**: the b05-selfhost model pin (#1849, #1858) and the managed-vault
  consent latch (#1864), for one narrow case. Both stand for everything else.

## Context

A managed vault is a Creek vault Adepthood provisions on Fly for a writer
(`CREEK_MANAGED_VAULT` set). Two earlier decisions made it local-only:

- **#1858 (b05-selfhost).** With a model package configured, or in container
  mode, only the pinned local model at its pinned digest over loopback may see
  vault text. A cloud stage is refused from its config before it is built.
- **#1864.** A managed vault refuses to start with cloud consent set, and
  latches consent off while it serves, so the operator's own cloud key can
  never carry a writer's journal.

The local model those decisions assumed has not shipped (#1849, and the
selection work in #1874/#1875). On 2026-10-15 the pilot answered
`GET /v1/capabilities` and `GET /v1/wheel` but refused every
`POST /v1/reflections` with `temporarily_unavailable`: no local model, and the
cloud refused. A managed-vault writer had no Resonance at all.

Many of those writers already hold their own Anthropic or OpenAI key, which
Adepthood accepts as BYOK. Until now a vault-bound pass ignored it (#3258).

## Decision

**On a managed vault, a request that carries the writer's own Anthropic or
OpenAI key is answered by that vendor, through the vault's own pipeline, for
OPEN and PERSONAL content, for that request only.**

The owner decision (2026-10-16): with BYOK and a managed vault, the cloud
model's output is generated through the Creek vault's voice-draft pipeline;
the writer's key is how the draft is generated, and the same key powers
classification and onboarding of new entries and uploads.

### The wire

Two optional headers, published on the three `/v1` routes that call a model —
`POST /v1/reflections`, `POST /v1/letters/{external_id}` and
`POST /v1/classifications` (contract `0.18.0`):

- `X-Creek-Provider`: `anthropic` or `openai`.
- `X-Creek-Provider-Key`: the writer's key.

Headers, never a body field, so the key cannot appear in body logging, in a
request-model validation error, or in anything derived from the parsed body.
Both or neither: one alone, an unknown vendor, or a value that is not
key-shaped is `422 invalid_request`, whose body is one fixed message and echoes
neither header. `GET /v1/capabilities` reports `request_key_providers` — the
two vendors on a managed vault, empty on a self-connected one — so Adepthood
can detect support before forwarding anything.

Routes that make no model call (`/v1/voice-drafts`, `/v1/uploads`) do not take
the headers. Upload *onboarding* is the `llm` classification pass that follows
an upload, which does.

### The boundary

- **The vault is a relay to the writer's own vendor.** It grounds the entry in
  the writer's corpus and voice, assembles the prompt, and sends it to the
  vendor the writer named, authenticated as the writer. The output is the
  vault's grounded, verbatim-validated result, as for any other provider.
- **INTIMATE never reaches the vendor.** There is no second tier gate. The
  key's stage is resolved through the same
  `ModelRouter` chokepoint — the vault's routing with `generation` set to the
  key's provider — so an INTIMATE request is redirected to the vault's local
  `default` (then held to the model pin like any local stage) or refused with
  `IntimateRoutingError`. In an `llm` classification pass, INTIMATE fragments
  take the router's local route; if that route cannot serve, they are left
  unclassified (`llm_call_failed`) rather than refusing the writer's whole pass,
  and they are never handed to the writer's provider.
- **The operator's key is still refused.** A vault config routing a stage to
  a cloud provider is refused exactly as #1858 decided, and the consent latch
  of #1864 still governs every provider built from the environment. A provider
  holding a request key never reads the environment for a key or for consent,
  so the operator's key cannot stand in for the writer's.
- **Self-connected vaults are unchanged.** Without `CREEK_MANAGED_VAULT` a
  request key is parsed (and refused if malformed) but never used.
- **Without a key, nothing changes**: the pinned local model, or the existing
  fail-closed refusal. *Amended 2026-10-24:* for an `llm` **classification
  job** with no key and no pinned model, a managed vault now runs the
  model-free `rules` pass instead of failing, and reports `method: rules`.
  Nothing is sent to any vendor, so the fail-closed property this clause
  protects — no egress without the writer's key — is unchanged. Reflections
  and letters still refuse. The refusal had left a production managed vault
  with 64 failed jobs and every fragment unclassified.

### The key is held for one request and kept nowhere

- It travels in `RequestKey`, whose `repr`/`str` redact the value and which
  refuses to pickle. Only the vendor SDK client constructor reads it.
- It is never written to the vault, the audit log, the access or error log, an
  error body, telemetry, or the embeddings cache. An `llm` classification job
  carries it to its worker thread as an argument, never in the persisted job
  record; a job reclaimed after a restart has no key.
- A vendor error raised under a request key is reduced to its exception type
  and HTTP status, because a 401 message can quote part of the rejected key.
- `tests/test_request_scoped_byok.py` sweeps a canary key from the response,
  every log record (vendor SDKs and `httpx` at DEBUG included), and every file
  under the test's root, on the success path and on a vendor 401 that echoes
  the key.

## Consequences

- **Processing is not operator-blind while it runs.** This was already true of
  OPEN and PERSONAL on ordinary Fly (ADR-0014): the running vault reads them in
  plaintext. Now the vault process also holds the writer's key in memory for
  the duration of the request, and the prompt it assembles leaves for the
  writer's vendor. Adepthood relays the key device → adepthood → vault per
  request; that relay is recorded in adepthood ADR 0009's 2026-10-16
  amendment.
- **The writer's vendor sees OPEN and PERSONAL prompts** under the writer's own
  account and terms. Adepthood labels each such answer as written by the
  writer's own key, through their vault.
- **The model is the vendor's default** for the key's provider. A vault config
  cannot redirect a writer's key to another model, gateway or `api_base`.
- **Managed vaults can reflect, write letters and classify today**, for
  writers with a key, without waiting for the local-model work in
  #1849/#1874/#1875. Writers without a key are no worse off than before.
- **Narrow by construction.** The exception lives in two functions in
  `creek_mcp/model_pin.py` (`request_key_admitted`,
  `build_request_scoped_provider`) and is reached from three routes. A future
  route that wants a request key must opt in with
  `RouteSpec.accepts_request_key`, which also publishes the headers.

## Amendment (2026-10-24, #1967): the MCP transport takes the same headers

Since #1962 `/mcp` is served inside the `/v1` process, and the headers an agent
sends reach the MCP SDK's request unchanged. Three MCP tools —
`creek.draft`, `creek.author` and `creek.reflect` — now honour the same two
headers on the network transport. This widens where the key may arrive, not
what it may do:

- **One decision, still.** `creek_mcp/httpapi/mcp_request_key.py` only extracts: it
  parses the pair with the same `parse_request_key`, binds the result for the
  one tool call, and the tool hands it to the same generation chokepoint the
  `/v1` routes use. `request_key_admitted` and
  `build_request_scoped_provider` remain the whole exception; the router
  remains the one tier gate, so an intimate-derived draft is forced local or
  refused.
- **The same refusal.** A malformed pair is refused before dispatch with the
  MCP refusal envelope carrying `/v1`'s fixed `invalid_request` message, and
  audited as refused with no argument summary.
- **Stdio never reads headers**, and a self-connected vault still ignores a
  key. `creek.handshake` reports `request_key_providers` exactly as
  `GET /v1/capabilities` does (empty over stdio).
- **Held for one call, kept nowhere.** A composition that detaches past the
  MCP deadline (#1966) carries the key in its worker's closure for that run;
  the job record, its audit entries, its progress messages and the saved draft
  never hold it. `tests/test_mcp_request_scoped_byok.py` sweeps a canary key
  from every response, log record and file on the synchronous, detached and
  vendor-`401` paths.
- **The agent's credential is not a key.** Only `X-Creek-Provider-Key` is
  read; an OAuth access token or static consumer token is never forwarded to a
  vendor (#1963).
- **Provenance.** A successful answer names its route in `inference_source`,
  and a saved draft carries it in its frontmatter.

Clients that cannot set custom headers — hosted Claude connectors and ChatGPT
developer-mode connectors — run pool-or-local only; `docs/mcp.md` lists which
clients can send them.
