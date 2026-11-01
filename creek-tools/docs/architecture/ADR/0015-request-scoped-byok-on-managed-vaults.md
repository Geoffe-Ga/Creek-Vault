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

## Amendment (2026-10-28, #1877): the environment, a refused key, re-atomization

An adversarial review of the shipped boundary found four gaps; none leaked a
key or sent INTIMATE content, and each is now closed in code rather than
accepted here.

- **The operator's environment does not apply to the writer's key.** Both SDKs
  read routing and decoration from the environment: `ANTHROPIC_BASE_URL`,
  `ANTHROPIC_CUSTOM_HEADERS`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`,
  `OPENAI_PROJECT_ID` and `OPENAI_CUSTOM_HEADERS`. With an operator org id set,
  a writer's OpenAI key was sent with `OpenAI-Organization: org-<operator>`
  and refused. A request-key client now passes the vendor's own base URL
  explicitly and removes every environment-derived header from each request
  (`providers._operator_header_overrides`). An operator-keyed, self-hosted
  client still reads its environment as before.
- **A refused key is fatal for the run.** A `401`/`403` on a request-scoped
  call is `RequestKeyRejectedError` (a `RuntimeError`, still status-only and
  unchained). The classification retry loop never retries it, the dispatcher
  stops starting new fragments, `creek.classify` returns a refusal, and the
  `/v1` job ends `failed` with the published `failure_code:
  "request_key_rejected"` (contract `0.26.0`). Before, a revoked key cost three
  delayed vendor calls per fragment and ended `succeeded` with nothing
  classified. The operator's own key keeps the retried behaviour.
- **The job-level canary.** `tests/test_request_scoped_byok.py` now runs a
  `/v1/classifications` job against a vendor `401` that quotes the key and
  sweeps every log record, the response and every file. Reverting the
  status-only redaction in `providers._error_detail` fails it; the chain
  suppression is pinned by the provider-level chain sweep, since no log on the
  job path renders an exception chain.
- **Re-atomization follows the final tier.** A fragment the classification
  raised to INTIMATE is re-atomized through `TierClassifiers.for_tier` on its
  new tier, so its children take the local INTIMATE route, or the rules pass
  when no local route exists, never the writer's provider.

## Amendment (2026-11-01, adepthood#3578): the key may come from adepthood's encrypted store

Owner decision R1-1 (2026-10-27) chose to keep the writer's key encrypted
beyond the request, so that background classification can use it. On
2026-11-01 the owner confirmed the split: "Split: un-enrolled stored
server-side; enrolled stay user-held." Adepthood ADR 0009's amendment of the
same date is the paired record. This amendment states what that changes for
Creek. **Creek's custody does not change.**

- **Where the key now comes from.** For an account that has not enrolled in
  user-held keys, adepthood keeps the writer's key encrypted at rest. Its
  background classification submits (a retried admission, a queued successor,
  restart recovery, a save from a device that sent no key) send
  `X-Creek-Provider` / `X-Creek-Provider-Key` from that stored copy. Before,
  those submits went keyless. They use the same headers on the same route,
  `POST /v1/classifications`, and only when this vault advertises the vendor
  in `request_key_providers`. Creek cannot tell, and does not need to tell, a
  key relayed from the writer's device from one read out of adepthood's store.
  An enrolled account's key stays user-held, so its background classification
  stays keyless until the writer's next online session relays it.
- **Creek still holds a key for one request and keeps nothing.** "The key is
  held for one request and kept nowhere" stands as written. The job record,
  the vault, the audit log and every log line still never hold it. A keyed
  `llm` job still carries it to its worker thread as an argument only.
- **A reclaimed job still has no key.** A job whose worker is lost to a
  restart or autostop is not resumed. Its next status read lands it `failed`
  with `failure_reason: worker_lost` and no `failure_code`. Adepthood treats
  that as retryable and re-admits a fresh job. **That job has a key only
  because adepthood sends one again, from its stored copy.** Nothing in Creek
  carries a key across the restart.
  `tests/test_request_scoped_byok.py::test_a_keyed_job_lost_to_a_restart_keeps_no_key_and_is_readmitted_keyed`
  pins this: the reclaimed job ends `failed` with no code, no file under the
  vault holds the key, and the re-admission with the key is written by the
  writer's vendor. No Creek code assumed a reclaimed job could never be
  followed by a keyed one, so no code changed.
- **A refused key ends with the run, as before.** A `401`/`403` still ends the
  job `failed` with `failure_code: "request_key_rejected"` (the 2026-10-28
  amendment). Adepthood now also deletes its stored copy when it lands that
  code, so a refused key is not sent to this vault again.
- **The boundary is unchanged.** Self-connected vaults still ignore a key.
  INTIMATE fragments still take the local route or stay unclassified and never
  reach the writer's vendor. The operator's key still never stands in for the
  writer's.

The custody that does change is adepthood's. Its stored copy is encrypted
under the operator's key, and it is not operator-blind. Adepthood ADR 0009
records that limit; this record does not repeat it as Creek's.
