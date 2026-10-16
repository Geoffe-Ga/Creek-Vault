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
  fail-closed refusal.

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
