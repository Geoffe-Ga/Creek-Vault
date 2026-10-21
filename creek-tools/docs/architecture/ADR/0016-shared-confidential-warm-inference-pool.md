# ADR-0016: A shared, confidential, always-warm inference pool for managed vaults

- **Status**: Proposed (drafted 2026-10-17 by Fable, claude-fable-5-1, for
  #1874; awaiting the owner's answers to Q1 and Q2 in Decision 0). The
  answers are recorded as a comment on #1849 and folded in by a follow-up
  docs PR that sets `Accepted (owner decision YYYY-MM-DD)`, as ADR-0015 did.
  Under a Yes to Q1, Accepted takes effect only once the adepthood ADR 0009
  amendment merges; until then O3 is the fallback design. Nothing here is a
  public claim; every claim waits for adepthood B24
  ([adepthood#3076](https://github.com/Geoffe-Ga/adepthood/issues/3076)).
- **Date**: 2026-10-17
- **Driving issue**: #1874; parent #1849 (B05); measurement #1850 (B09); model
  choice #1875; siblings [ADR-0015](0015-request-scoped-byok-on-managed-vaults.md)
  and Geoffe-Ga/adepthood#3437.
- **Amends**: the loopback-only rule of #1855
  (`creek/classify/llm/local_boundary.py`) and the model pin
  (`creek_mcp/model_pin.py`) for exactly one new destination, the pool; and
  ADR-0015's "without a key, nothing changes" clause only (the pinned
  destination may now be the pool; a writer's own key still wins, Decision 1
  item 10). It does not amend [ADR-0013](0013-demand-provisioned-vault-lifecycle.md)
  Decision 3: the pool holds no corpus and no vault state. Under No it
  amends ADR-0013 Decision 7's "attested execution capacity" clause, for
  OPEN and PERSONAL only (next bullet). [ADR-0004](0004-embeddings-stay-local.md)
  is amended only on a Yes to Q2, for managed vaults only.
- **Engages ADR-0013 Decision 7** (pooled execution capacity). The per-user
  Machine and volume are unchanged; the pool replaces no per-user compute.
  The owner's 2026-10-16 decision and Fly's GPU withdrawal are the "material
  change in confidential-compute availability" D7 names as a review trigger.
  D7's four proofs map here: no cross-user plaintext state (Decision 1 items
  6 and 7; T5, T21); attested key release (not applicable: the pool holds no
  volume key and no vault state; under O5 Decision 3.3's attestation gate
  covers the inference leg); equivalent deletion (Decision 1 item 3; T11);
  the same externally observable single-user contract (`/v1` gains only
  additive fields under a minor bump; T10, T15). D7's "attested execution capacity" is not met by O3: under No,
  this record amends D7 to admit an operator-run, unattested, no-retention
  pool for OPEN and PERSONAL only, and says so.
- **Governing owner decisions this record may not contradict**:
  [adepthood ADR 0009](https://github.com/Geoffe-Ga/adepthood/blob/main/docs/adr/0009-privacy-custody-and-inference-architecture.md)
  Decision items 3 and 4, its D05 section and reopen triggers, and its
  2026-10-14 budget answer ("no paid runtime and no paid reviews, for now");
  [ADR-0014](0014-provider-managed-custody-for-ordinary-fly.md) ("INTIMATE
  remains local"; superseded for journal content as a decided target by ADR
  0009, but governing every account until B13 phases (b) and (c) land for
  it); the owner's 2026-10-16 decision quoted in #1874. Where this record and
  the owner's text disagree, the owner's text governs.

## Context

### What exists at `a134bf4` (2026-10-16)

- **One scale-to-zero Machine per vault.** `creek_mcp/provisioning/fly.py`
  allocates `shared` CPU, `_DEFAULT_CPUS = 1`, `_DEFAULT_MEMORY_MB = 1024`, a
  1 GB rootfs with `persist: never` and a 5 GB volume, with
  `"min_machines_running": 0` and `"autostop": "stop"` (`_runtime_service`).
  Every vault app is created on its own custom private network named by the
  allocation id (`"network": reference.allocation_id`, `fly.py:611`;
  `_verify_app`, `fly.py:631`, rejects a differing network at `fly.py:642`). Fly forwards no
  packets between private networks unless told to. Every Machine's secret
  files travel in its config as base64 (`files[].raw_value`), readable by any
  holder of the organization's deploy token: the pilot's existing trust
  boundary, which nothing below widens.
- **Model package, loopback-only, readiness probe** (#1855).
  `creek_mcp/model_package.py` pins runtime name, version and digest, a
  non-`latest` model tag, the weights' SHA-256 (one file), the runtime
  inventory digest, quantization, parameter count, size and licence;
  `APPROVED_MODEL_LICENSES` is empty on purpose.
  `creek_mcp/container_health.py --check model` is fail-closed, separate from
  storage readiness, and never reads the vault (`CANARY_PROMPT` is a
  constant). In container mode (`CREEK_OLLAMA_LOOPBACK_ONLY=1`, set by
  `container_runtime.py` and `fly_vault_runtime.py`) every Ollama dial must
  be a literal loopback URL, and `creek_mcp/model_pin.py` admits only
  `PINNED_PROVIDER = "ollama"` on loopback at the pinned digest; a cloud stage
  is refused from its config. **No managed Machine carries a manifest
  today**: `_runtime_config` sets `CREEK_MANAGED_VAULT` and the replay fields
  only, and the image sets no `CREEK_MODEL_PACKAGE_FILE`.
- **Grounding is absent on a managed vault today.** The image installs
  `uv sync --locked --no-dev --no-install-project` (`Dockerfile`) with no
  extras; `sentence-transformers` lives only in the `embeddings` extra
  (`pyproject.toml`), and `_load_sentence_transformer`
  (`creek/link/embeddings.py`) imports it at runtime and downloads the model.
  So the grounding session cannot be built and `creek_mcp/tools/reflect.py`
  degrades to "an ungrounded-but-successful reflection". With the extra, the
  measured load is "~2 s on the first load in a process, ~20 s on a
  genuinely cold one" (`reflect.py`), paid on every wake of a
  `persist: never` rootfs: the rejected cold start in miniature.
  [ADR-0004](0004-embeddings-stay-local.md) keeps embeddings local-only by
  design.
- **BYOK** ([ADR-0015](0015-request-scoped-byok-on-managed-vaults.md)): a
  request carrying `X-Creek-Provider` / `X-Creek-Provider-Key` on a managed
  vault is answered by that vendor for OPEN and PERSONAL, that request only;
  contract `0.18.0` (`creek_mcp/contract.py`). adepthood still charges a
  credit for every vault-bound pass "whatever key header it carries" (#3258,
  `backend/src/routers/journal.py`); Geoffe-Ga/adepthood#3437 (open) stops
  that for request-key answers.
- **The deadline that binds.** The vault sheds a request after
  `DEFAULT_TIMEOUT_SECONDS = 30.0` (`creek_mcp/httpapi/middleware/limits.py`),
  `OllamaProvider.REQUEST_TIMEOUT = 30.0`, and adepthood bounds the call at
  `_VAULT_TOTAL_DEADLINE_SECONDS = 10.0 × 3 = 30 s`
  (`backend/src/services/creek_vault_client.py`), inside the HTTP request
  (`run_resonance`, `backend/src/routers/journal.py`). But `_post_reflection`
  runs on the pooled client whose `httpx.Timeout` `read` phase is
  `_VAULT_TIMEOUT_SECONDS = 10.0`: a vault that sends no response byte for
  10 s is a `VaultCallTimedOutError` whatever the 30 s says,
  `/v1/reflections` is not streamed, and the replay router's
  `_wait_until_started` may hold a cold Machine's request up to
  `_DEFAULT_READINESS_TIMEOUT_SECONDS = 20` before the `307`, inside that
  window. Only the cold embedding call is widened
  (`_COLD_EMBEDDING_DEADLINE_SECONDS = 45.0`, via `_widened_phase_budgets`).
  So the effective reflection budget is about 10 s from request to first
  byte, wake included, until adepthood widens that call. A shed request keeps
  its off-loop thread running (`read_off_loop`, `creek_mcp/httpapi/deadline.py`,
  used by `httpapi/reflect.py`, abandons the thread and cannot cancel it), so
  without a cancel path it would keep a pool slot. Vault admission caps:
  `DEFAULT_MAX_CONCURRENCY = 32`, `DEFAULT_MAX_PER_CONSUMER = 8`.
- **A reflection is short.** `creek_mcp/tools/reflect.py` caps output at
  `_REFLECTION_MAX_OUTPUT_TOKENS = 128` (64 base + 32 per note). The prompt
  is the entry, grounding snippets when any exist, and a template the #1850
  harness budgets at `PROMPT_OVERHEAD_TOKENS = 1536` (`creek_mcp/bench/runner.py`).
- **A job protocol already exists on `/v1`.** Contract 0.10.0's `pipeline`
  capability: `POST /v1/classifications` and `POST /v1/links` answer
  `JobAcceptedResponse`, polled at `GET /v1/jobs/{job_id}`
  (`JobStatusResponse`, `creek_mcp/api/models.py`); adepthood already polls
  it (`backend/src/services/creek_vault_pipeline.py`).
- **Volume is small and capped.** `DEFAULT_MONTHLY_CAP = 20` charged
  generations per account-month, shared across transcription, resonance
  passes and letters (`backend/src/services/usage.py`), so at most 20
  reflections, with two concurrent generations per account
  (`MAX_CONCURRENT_GENERATIONS_PER_USER`). The pilot admits at most five live
  allocations under a USD 25/month alert
  ([managed-vault-fly-pilot.md](../../managed-vault-fly-pilot.md)): at most
  100 reflections a month.
- **Attested inference has a provider shape.** `EnclaveProvider`
  (`creek/classify/llm/providers.py`) is `is_cloud = False` only because it
  verifies attestation before every completion
  ([ADR-0006](0006-enclave-attestation-trust-model.md), "the gate, not the
  flag"). adepthood ADR 0009 amended it: its operator-provisioned trust root
  is not evidence, and journal content is not routed to a remote enclave
  under the current decision.

### Why the per-vault model fails

The owner (2026-10-16): "The cold start scale to zero problem with our own
local model is a nonstarter. We can't wait to install a model every time a
user presses get resonance. We need an architecture that allows for shared
yet confidential compute with a local model that can service multiple vaults
simultaneously, scaling appropriately to avoid terribly latency and also
terrible pricing." A model inside every vault means N copies of the weights,
a model load on every cold start, and GPU-class memory per vault.

### A platform fact #1874 did not account for

**Fly.io no longer sells GPU Machines.** A Fly blog post is bannered "Fly
GPUs are deprecated and will be unavailable after August 1", the community
thread of a customer notice reads "Fly.io GPUs will be deprecated as of July
31, 2026", and Fly's GPU documentation URL now serves the February 2025 post
"We Were Wrong About GPUs", which the 2026 banner supersedes (sources below;
read 2026-10-17). A Fly-only pool is CPU-only; a GPU pool lives on another
provider and joins Fly's private network from outside.

### The policy frame, restated

1. ADR 0009 item 3: cloud models are reached only with the person's own key;
   "the app's own cloud key will never carry a person's journal content".
2. ADR 0009 item 4: credits pay for non-cloud inference only, "the
   vault-local model (B05) or a managed no-retention runtime we run";
   processing there "is not operator-blind while it runs", stores nothing,
   and is labelled on each use. Its escalated gap (a vault-answered request
   not proven non-cloud) closes by "either a vault contract field that
   reports a local route per answer, or credit admission limited to vaults
   the person connected themselves; the owner decides which".
3. ADR 0009 D05: attested confidential compute "is not pursued ... It reopens
   only if the owner sets an operator-blind target for the managed runtime's
   processing", with conditions "real hardware, a vendor attestation chain,
   nonce freshness, user-authorized key release, and a hostile-host lab".
   The listed reopen trigger, in full: "Confidential compute becomes
   affordable enough to set an operator-blind target for the
   runtime-we-control path, once D05 is proven (C)". Option C's own trigger
   names "the runtime we control".
4. ADR 0009 budget (2026-10-14): "no paid runtime and no paid reviews, for
   now. That is a constraint, not a figure." The pilot checklist: "A free
   allowance is not a cap."
5. ADR-0014: an ordinary Fly vault reads OPEN and PERSONAL in plaintext while
   it runs and cannot receive INTIMATE over `/v1`. The pool cannot change the
   vault's custody.

## Requirements

The owner's baseline from #1874, sharpened where this record may sharpen and
never dropped. Each maps to acceptance tests below.

- **R1 Warm.** Weights stay resident in workers shared by many vaults; no
  reflection waits for a model download or load; the pool never scales to
  zero.
- **R2 Latency.** Prerequisite: adepthood widens `_post_reflection`'s phase
  budgets to its 30 s deadline with `_widened_phase_budgets`, as the cold
  embedding call already is; until then the budget is the 10 s first-byte
  window in Context, wake included. Then, measured by #1850 before any
  cohort, from adepthood's socket through the replay router, cold and warm:
  vault `POST /v1/reflections` p95 ≤ 25 s from the vault's receipt of the
  request (5 s under `DEFAULT_TIMEOUT_SECONDS`); Machine wake, measured by
  the cold and idle sweeps, must fit beside that 25 s inside adepthood's
  30 s; pool generate p95 ≤ 10 s for a 2,500-token prompt and 128 output
  tokens at concurrency 4; pool time-to-first-token p95 ≤ 2 s, attestation
  round trip included. The `PoolProvider` dials with a 1 s connect timeout
  and a 12 s request timeout, never beyond the vault request's remaining
  budget, no retries; a cancelled or shed vault request cancels its pool
  call. If a model cannot meet the budgets at p95: a faster model or
  quantization first, otherwise reflection joins the existing `pipeline`
  job protocol, a contract minor this record does not make.
- **R3 Confidentiality.** A vault's plaintext is processed for that request
  only and stored nowhere: no prompt or response logging anywhere in the
  path, including captured process output, error bodies, crash dumps and
  swap; no KV-cache or prefix-cache reuse across vaults; no request-scoped
  state after the response. The record says plainly who can read plaintext
  while it runs.
- **R4 Tier rules.** INTIMATE routing is unchanged: no INTIMATE plaintext
  leaves the vault process for any pool, and the `ModelRouter` chokepoint,
  not a pool-side check, enforces it.
- **R5 Tenancy.** A per-vault credential bound to its allocation, minted at
  activation and revoked at deletion; one vault cannot observe another's
  requests, queue or usage; per-vault concurrency and rate limits apply.
- **R6 Scaling and cost.** Scale on queue depth to a warm minimum of one,
  never zero; scale-out is automatic, and a host that can only be added by
  hand does not meet R6 and appears below for price comparison only. Cost per
  1,000 reflections at pilot and production volumes, from a dated price
  sheet or #1850 measurement, never arithmetic alone.
- **R7 Network path.** Owner baseline: vault to pool over the platform's
  private network, never the public internet. O2 and O3 meet it (a Flycast
  address, mutual TLS terminated by the gateway so Fly Proxy carries
  ciphertext, a WireGuard tunnel). O5 does not: its egress crosses the public
  internet as ciphertext inside an attestation-bound channel. This record
  does not sharpen R7 to admit O5; it records the deviation for the owner in
  Q1. The replay router is untouched.
- **R8 Model package.** The #1855 manifest, digest verification and readiness
  probe move to the pool; the manifest schema is unchanged.
- **R9 Fallback.** A writer's own key is answered by that vendor (ADR-0015);
  without a key, the pool; pool unavailable means refuse, fail closed. Never
  the operator's cloud key, never a different model, never the pool after a
  vendor error under a writer's key.
- **R10 Provenance.** Every model-backed answer names its route, so adepthood
  can label each use. One of the two closures ADR 0009 item 4 lists; which
  the owner adopts stays an escalation (Decision 1 item 11).

## Options

**Every price below was read on 2026-10-17 through web-search summaries of
the vendor pages, not from the sheet itself, because this session could not
open the pages.** Each figure is attributed below and must be re-read from
the dated sheet, by the owner or by #1850's live mode, and recorded in the
fleet policy file before it is relied on; until then no figure satisfies R6
or #1874's cost constraint. The ranking argument is the decision; the
numbers are placeholders of the right order.

"Per 1,000 reflections" assumes 2,500 prompt tokens and 128 output tokens per
reflection (grounded or not; #1850 replaces it with measured counts). A fixed
warm minimum is divided by monthly volume: the pilot ceiling (100) and 10,000
a month (500 accounts at the default allowance). Monthly figures are hourly
× 730 h, the basis `creek_mcp/bench/cost.py` uses
(`HOURS_PER_BILLING_MONTH = 730`), except Fly's two rows, Fly's own listed
monthly prices (720 h basis; USD 0.3667/h and 0.7334/h, so USD 268 and 535
at 730 h). "× the pilot cap" divides by USD 25 with EUR at par. Host prices
are detailed in Decision 2.3.

| Option | Warm, no cold start | p95 fit | Who reads plaintext while it runs | INTIMATE | Cost per 1,000 reflections: pilot / 10k a month | Network path | Status under ADR 0009 |
|---|---|---|---|---|---|---|---|
| **O1** Per-vault model on each Machine (#1855 completed as filed) | No: a load on every wake, N copies of weights | Unmeasured; 1 GB RAM holds none of #1875's 24–32 B candidates | Operator, Fly | Refused (ADR-0014) | Scales with vault count, not use | Loopback | Allowed; rejected by the owner on 2026-10-16 |
| **O2** Creek-run CPU pool on Fly (`performance-8x` 16 GB USD 264.01/mo or `performance-16x` 32 GB USD 528.02/mo, iad/ewr) | Yes | Unmeasured; ADR-0007 rejected CPU-small models for the voice stage; #1850 can measure it | Operator, Fly | Refused | USD 2,640–5,280 / USD 26–53 | Fly private network (Flycast) | Allowed ("a runtime we run"); a paid runtime, 11–21× the pilot cap |
| **O3** Creek-run GPU pool off Fly, bridged by WireGuard (RunPod L40S 48 GB ≈ USD 796/mo; A100 80 GB ≈ USD 1,307/mo; Hetzner GEX63 96 GB EUR 999/mo; GEX131-1 96 GB EUR 1,197.30/mo; plus setup fees) | Yes | Unmeasured; GPU-class, plausible for #1875's candidates | Operator, the GPU host provider, Fly (as host of the gateway Machine) | Refused | USD 7,960–13,070 or EUR 9,990–11,973 / USD 80–131 or EUR 100–120 (one host; capacity unmeasured) | Flycast on each vault network to a gateway under mutual TLS, one WireGuard tunnel to the host | Allowed ("a runtime we run"); a paid runtime, 32–52× the pilot cap |
| **O4** Managed inference provider, plain no-retention (DeepInfra Qwen3-32B USD 0.08 / 0.28 per M, "zero retention") | Yes | Vendor-grade | Operator, Fly, **the vendor** | Refused | USD 0.24 / USD 0.24 | Public internet, TLS | **Not selectable**: the app's key would carry journal content to a cloud model (item 3) |
| **O5** Vendor-attested confidential inference (Privatemode gpt-oss-120b EUR 0.43 / 1.70 per M; Tinfoil gpt-oss-120b USD 0.15 / 0.60, GLM-5.3-Flash USD 0.40 / 1.25) | Yes | Vendor-grade; measured by #1850 before a cohort | Operator and Fly at the vault; at inference only the vendor's attested image. **Vendor-asserted**: vendor and host are excluded only to the extent the measured image is open-source, reproducibly built and audited, which condition 3.3 (f) covers and this record has not verified for either candidate ([runbook](../../pool-measurement-pin.md)) | Refused by this record; the only pool whose inference leg could qualify later | EUR 1.29 / EUR 1.29 (Privatemode gpt-oss-120b); USD 0.45 / USD 0.45 (Tinfoil gpt-oss-120b) | Public internet, inside an attestation-bound encrypted channel (R7 deviation) | **Not selectable as written**: a cloud model on the app's account (item 3), not "we run" (item 4), D05 not pursued. No listed reopen trigger covers it: both name the runtime we control, and one is conditioned on D05 being proven first. Selecting O5 is a new owner decision outside the triggers |
| **O6** Creek-run attested GPU pool (Azure `NCC40ads_H100_v5` USD 6.98/h ≈ USD 5,095/mo; Phala H100 TEE USD 3.08/h ≈ USD 2,248/mo) | Yes | GPU-class | Nobody, once D05 is proven on that hardware | Refused until the whole chain is attested | USD 22,480–50,950 / USD 225–510 | WireGuard to the host | Allowed only through D05 in full; a paid runtime, 90–204× the pilot cap |

Reading the table: O1 is rejected by the owner. O4 is excluded by a ratified
decision. O6 costs two orders of magnitude more than the pilot cap and still
needs D05. O2 is not selected because ADR-0007 rejected CPU-small models for
the voice stage and no #1875 candidate has been measured on CPU; it is not
excluded by policy, and under No it is measured first. That leaves a
Creek-run pool (O3, with O2 as its measured-first variant) and a
vendor-attested pool (O5). Both meet R1, R3, R5, R8, R9 and R10 under the
contract in Decision 1. They differ on three things the owner decides:

1. **Cost shape.** O3's warm minimum is fixed at USD 796–1,307 (RunPod L40S,
   A100) or EUR 999–1,197 (Hetzner GEX63, GEX131-1) a month, plus one-time
   setup of EUR 599–999 on Hetzner, before a single reflection: at the
   pilot's volume, USD 7,960–13,070 or EUR 9,990–11,973 per 1,000. O5 costs
   about EUR 1.29 or USD 0.45 per 1,000 at any volume. Break-even, same
   currency and single-host arithmetic (measured capacity, not price, bounds
   the real break-even): Hetzner GEX63 against Privatemode gpt-oss-120b at
   EUR 999 / EUR 1.29 ≈ 774,000 reflections a month (about 38,700 accounts at
   the default allowance); RunPod L40S against Tinfoil gpt-oss-120b at
   USD 796 / USD 0.45 ≈ 1.77 million a month (about 88,000 accounts).
2. **Who can read.** O3 adds a party that sees plaintext, the GPU host
   provider, and keeps the operator able to read while it runs. O5 is
   designed to remove host and operator from the inference leg, leaving the
   vendor's measured image, provided the vault verifies the attestation chain
   before it sends anything.
3. **Policy.** O3 is allowed by ADR 0009 as written but is a paid runtime,
   which the 2026-10-14 answer forbids for now. O5 is a metered, vendor-run
   runtime whose bill at the pilot's ceiling is zero only while a vendor free
   allowance covers it, and "a free allowance is not a cap"; whether that
   satisfies the 2026-10-14 constraint is the owner's reading. O5 also needs
   the owner to amend ADR 0009 items 3 and 4, reopen D05 for the inference
   leg, and accept the R7 deviation.

Both keep #1875's model choice open, within different catalogues: O3 runs any
approved open-weight model that fits the host; O5 runs the vendor's
catalogue (on 2026-10-17 Privatemode lists gpt-oss-120b, Kimi K2.6, GLM-5.3
and GLM-5.3-Flash; Tinfoil lists gpt-oss-120b, GLM-5.3, GLM-5.3-Flash,
Llama 3.3 70B, Kimi K3 and others, Kimi K2.6 having been sunset in July 2026).
gpt-oss-120b is Apache-2.0 with OpenAI's gpt-oss usage policy attached; the
other licences are verified in #1875, whose voice-fidelity eval runs on
whichever catalogue applies, with the sampling profile Decision 1 item 4
pins.

## Decision

### Decision 0. The owner questions, and the architect's recommendations

**Q1. May BotMason credits fund OPEN and PERSONAL inference in a
vendor-operated attested runtime that the vault verifies, per connection, to
be running the vendor's published measured image, with the host and the
operator excluded by hardware?** Answering Yes also sets, for the inference
leg only, the operator-blind target D05 names as its reopen condition, and
accepts the R7 deviation.

- **Yes** selects **O5** (Decision 3). It needs the owner to amend adepthood
  ADR 0009 in adepthood, as #3437 does for BYOK: item 3 keeps its definition
  of a cloud model and gains an explicit exception, "a vendor-attested
  runtime verified under Creek ADR-0016 Decision 3 may be reached with the
  app's own credential, because the vendor cannot read what it carries";
  item 4's "a managed no-retention runtime we run" becomes "a managed
  no-retention runtime we run, or a vendor-attested runtime verified under
  Creek ADR-0016 Decision 3"; item 4's honest-limit paragraph and label
  example, the Threat-actors and Per-platform rows on managed-runtime
  inference, the scorecard twin `0009-architecture-scorecard.json` and
  `backend/tests/test_custody_decision_record.py` change to match; D05 is
  reopened for the inference leg only, under Decision 3.3's substitute
  conditions, with an explicit waiver of the two it cannot meet. O3 stays
  designed and priced as the successor once volume nears the break-even.
- **No** selects **O3** (Decision 2), with O2 as its measured-first variant:
  #1850 measures a `performance-16x` CPU pool against R2 (itself a metered
  Fly Machine for the sweep's duration, authorized by the owner as a one-off
  line before the run) before any GPU spend is authorized; if it fits, the
  engine runs as Fly Machines inside the
  gateway app (no WireGuard, no third processor); if not, the GPU design
  stands. The pool is then a paid runtime, designed now and deployed only
  when the owner records a budget line for the warm minimum (Decision 4).
  Until then managed vaults serve OPEN and PERSONAL by BYOK alone
  (ADR-0015), and credit-funded reflection stays refused, the feature loss
  ADR 0009 item 12 already accepts.

**Recommendation on Q1 (the architect's, conditional): Yes, O5**, if the
owner is willing to amend adepthood ADR 0009 items 3 and 4 and reopen D05
for the inference leg. No trigger in ADR 0009 authorizes that today; it is a
fresh owner decision. O5 is a metered, vendor-run, paid runtime whose bill at
the pilot's ceiling is zero only while a vendor free allowance covers it (to
verify: that the allowance applies to the API and how output tokens count);
a free allowance is not a cap, so whether O5 satisfies the 2026-10-14
constraint is the owner's reading, not this record's. O5 meets every
requirement here except the R7 baseline, which it replaces with
ciphertext-only egress inside an attested channel; answering Yes accepts
that substitution explicitly. It is the only option whose cost scales with
use at the pilot's volume, and the only one that moves the inference leg
toward the operator-blind target instead of away from it. Its risks are in
Decision 3.5. An agent cannot ratify a privacy posture; this is the owner's
call.

**Q2. May grounding embeddings for a managed vault be computed in the pool?**
Decision 1 item 13 states the options and the recommendation.

Decision 1 holds under either answer to Q1; Decisions 2 and 3 are its two
implementations; Decisions 4 to 7 hold under either answer.

### Decision 1. The pool contract (implementation-independent)

Every pool implementation, and every vault that uses one, satisfies all of
the following; they are the acceptance tests below.

1. **Pinned identity, per request.** The vault carries a model package
   manifest (#1855 schema, fields unchanged) naming the pool's runtime and
   model. It is carried in the activation's stored runtime bundle
   (`FlyRuntimeSecrets` gains it, versioned per activation) and written into
   the Machine as a file, so `_verify_machine` compares each Machine against
   its own activation's stored version, never a fleet constant (Decision 6).
   The pool serves a *set* of pinned identities; every request names the
   identity it expects, the pool routes to it or refuses, and every response
   echoes it. The echo is a consistency check against misrouting, not
   integrity evidence: under O3 the only integrity evidence is the pool's own
   `model_package.verify_weights` run before it serves, which is
   operator-asserted (`model_blob_sha256` covers one file, so a sharded model
   ships as one packed archive verified before unpacking); under O5 the vault
   trusts its own attestation result, never the response body. The sampling
   profile is part of the served artefact set that `runtime_inventory_digest`
   covers; how a non-Ollama runtime fills that digest is the build issue's to
   define without changing the schema. An upgrade is rolled per activation
   (write the new bundle version, update the Machine config, which restarts
   a running Machine after a drain and leaves a stopped one untouched, then
   verify), and the old identity is retired only when inventory shows no
   activation on the old version, so no fleet-wide refusal follows a model
   change; under O3 the dual-identity window needs a second engine host or
   the O2 variant's Machines, since one 24–48 GB card holds one 24–32 B
   model.
2. **Exactly one destination beyond loopback.** In container mode a vault
   has exactly one inference destination beyond loopback, reached directly
   or through a pinned loopback sidecar: `CREEK_POOL_URL`, carried in the
   activation's stored bundle and written into the Machine's `env`, which
   `_verify_machine` compares against that activation's stored version. Under
   O3 its host is the IPv6 Flycast literal inside `fdaa::/16` allocated for
   that vault's network, and the vault verifies the gateway's certificate
   against the control-plane CA under a fixed SNI name carried in the
   manifest (`creek-pool-gateway`), the Flycast address being only the
   connect target; under O5 it is the exact vendor hostname pinned in the
   manifest. No vault config value can set or change it. The dial lives in
   `creek/classify/llm/pool_boundary.py` (proxy-free like `ollama_client`,
   R2's timeouts, no retries, and a cancel handle that closes the socket from
   the event loop), and `tests/test_ollama_endpoint_invariant.py` gains a
   parallel rule: any module that reads `CREEK_POOL_URL` or names a pool path
   constructs no HTTP client outside that boundary, and a vendor SDK call is
   allowlisted by function or moved there. Every other model dial stays
   loopback-only as #1855 left it.
3. **Per-vault credential, bound to the allocation.** The control plane mints
   per activation a random bearer token and a client certificate. The
   pilot's CA today issues only server certificates (`CN=hostname`, DNS SAN,
   `SERVER_AUTH`) for the private-TLS vault listener, so it gains a second
   issuance path: EKU `CLIENT_AUTH`, subject `CN=<allocation_id>`, no SAN.
   Both are stored in the activation's bundle and written to the Machine as
   `/run/secrets/creek_pool_token`, `/run/secrets/creek_pool.crt` and
   `/run/secrets/creek_pool.key` beside the runtime's other secret files (the
   replay path installs no TLS material today). The pool keeps only the
   token's hash and the certificate's identity, takes the allocation from the
   verified subject, compares it with the token's registry row, and accepts
   the token only over that mutual-TLS session. The binding is credential to
   allocation, not to Machine: it stops one vault's token being used with
   another vault's certificate; it does not protect against a deploy-token
   holder, who reads all three files from the Machine config, a compromised
   vault process, the operator or Fly, and a copied triple works from any
   address that reaches the gateway (Context). Revocation happens at deletion
   in `FlySecretManager.revoke`, which `delete()` already calls before
   destroying the Machine; it pushes an invalidation to the pool over the
   control channel (Decision 2.1) and is confirmed before the content-free
   receipt is issued; the pool's registry cache TTL is 60 s and a refresh
   failure fails closed. The fleet report reconciles the credential as a
   `ResourceClass.CREDENTIAL`. Under a vendor with a key API (Decision 3) the
   credential is a per-vault vendor key, kept in full in the encrypted bundle
   because the vendor's update and delete calls require it, with the expiry
   and caps of Decision 3.4; caps bound total spend, not rate, so item 8's
   per-hour limit is enforced in the vault's own limiter under every vendor.
4. **A narrow pool API.** One closed request shape: identity, a random
   request id (the only correlation key, a header), `deadline_ms` (at most
   12,000; the gateway's engine read timeout is that less elapsed time),
   prompt, optional system text, `max_tokens` (capped per route; 128 for
   reflections) and the declared tier, `open` or `personal`. The pool refuses
   `intimate`, unknown tiers, any client-chosen model, sampling, cache or
   adapter field, unknown fields, a prompt over 4,096 tokens and a body over
   the vault's 1 MiB limit, each a content-free 4xx. The vault enforces the
   prompt cap before dialling: grounding snippets are dropped first, and an
   entry that still exceeds the cap is refused as `invalid_request` with a
   content-free `prompt_too_long` reason rather than truncated, so a
   reflection never silently reads part of an entry; the pool's cap is
   defence in depth. Sampling is pinned pool-side as a profile inside the
   served artefact set (item 1), so #1875's eval and production generate
   alike. Admission refusals use a new `rate_limited` code, HTTP 429, with
   disposition `retry_with_backoff` in the published `retry-policy.json` and
   an advisory `Retry-After` header outside that table; it is served only to
   callers declaring the new contract minor, older minors keep
   `temporarily_unavailable`, 429 joins the closed status set, and adepthood
   maps it onto its existing 429 availability path and shows the writer a
   wait rather than an outage. Under O5 the attesting client translates the
   shape to the vendor's OpenAI-compatible API.
5. **No retention.** No component in the path logs or keeps a prompt, a
   response, or a request body: not the vault, a gateway, the engine or the
   vendor. Upstream error bodies are never relayed or logged, only mapped to
   outcome codes, because a vLLM 422 echoes the submitted field; no
   error-reporting SDK and no traceback-with-locals formatter runs on the
   gateway or the engine; process stdout and stderr, which Fly's log pipeline
   captures and retains outside our control, carry only the content-free
   record; gateway and engine set `prctl(PR_SET_DUMPABLE, 0)`, which also
   blocks `ptrace` and `/proc/<pid>/mem`; the engine runs with
   `--swap-space 0` and no `--cpu-offload-gb`; `/dev/shm` is sized for the
   engine's own use and cleared on restart, and no scratch path survives a
   restart; a shutdown hook zeroes the engine's pre-allocated KV pool before
   exit. Host swap and host core capture remain residual risks on any
   container host (a tenant cannot set `swapoff` or `kernel.core_pattern`,
   and a piped core pattern ignores `RLIMIT_CORE`), closed only on a VM or
   bare metal, which feeds the host-class choice in Decision 2.3; RunPod
   Secure Cloud is single-tenant while the pod runs, not dedicated hardware.
   For a vLLM engine the image is pinned by digest
   to a release where `--enable-log-requests` defaults to off;
   `--enable-log-outputs` is never set; `VLLM_LOGGING_LEVEL` is pinned at
   `INFO` or above as defence in depth, because with request logging on,
   prompt inputs are logged at `DEBUG`; `VLLM_NO_USAGE_STATS=1`. T4 asserts
   all of this against the pinned release; a release bump re-runs T4.
   Operational records carry only an allocation hash, the request id, token
   counts, latency, queue wait and an outcome code.
6. **No cross-vault cache.** Prefix caching is off (`--no-enable-prefix-caching`
   on vLLM, which enables it by default on V1 for supported models); no
   `cache_salt` design and no salt secret exist, because a shared block pool
   with salts still leaks occupancy between vaults, and the only shareable
   prefix, the template, is worth nothing at 20 reflections a vault-month.
   Speculative decoding is off, no LoRA adapters are loaded, no KV cache is
   offloaded to disk. After a response is sent the pool holds no state for
   that request. Under O5 the vault sends no cache-control field, and the
   vendor's documented cache isolation is reviewed under B11 (Tinfoil
   documents "secure prompt caching"; Privatemode prices cached input
   separately, so it caches); a vendor that cannot isolate caching per key
   fails T5.
7. **Per-request isolation and cancellation.** Continuous batching across
   vaults is permitted: compute sharing, not state sharing. No request-scoped
   memory, file or cache entry outlives its response. When the vault sheds
   the request at its deadline, the route catches the cancellation at the
   `read_off_loop` boundary and calls the pool boundary's cancel handle,
   which closes the pool socket from the event loop so the worker's blocked
   read fails and the thread exits (`read_off_loop` alone abandons the
   thread and cannot cancel it); the gateway aborts the engine request on
   client disconnect, so a shed call frees its pool slot. Caller disconnect
   is not observable today: the body-cap middleware's `_replay` answers
   `http.disconnect` as soon as the buffered body is consumed, so a
   disconnect watcher needs `_replay` to delegate to the original `receive`
   once its buffer is exhausted; until that lands only the shed path
   cancels.
8. **Admission.** Per vault: at most 2 generations in flight and a
   configurable rate (default 60 an hour, three times adepthood's monthly
   allowance in a single hour), enforced in the vault's own limiter and
   again at the pool under O3. Pool-wide: a bounded queue; a request whose
   queue wait would break R2 is refused within 1 s rather than queued to
   time out. Refusals are content-free and carry a retry hint. Under O5 the
   external tenancy control is the vendor key's caps (Decision 3.4).
9. **Readiness, separate from storage.** The pool's vault-facing `/ready`
   reports the identity set and a cached result of its own synthetic canary
   (`CANARY_PROMPT` moves from `container_health.py`); no queue depth, no
   counts, nothing about other vaults. Metrics and operator endpoints answer
   only the control plane's certificate role (Decision 2.1). The vault gains
   `container_health.py --check pool` (reachable, identity present, pool
   ready; it generates nothing). `READY` and the image `HEALTHCHECK` are
   unchanged; storage-ready never implies pool-ready. Under O5, with no
   gateway, there is no vault-facing `/ready`: `--check pool` reports the
   attesting client's cached verification of the pinned measurement set and
   the vendor model listing, and the canary is dropped, since a generation
   would be a billed vendor call.
10. **Fallback order.** A request with a writer's key is answered by that
    vendor (ADR-0015, unchanged); a vendor error under a writer's key is a
    refusal, never a pool call. Without a key, the pool. If the pool is
    unreachable, reports a different identity, refuses admission, or the
    attestation fails, the vault refuses with its existing shapes
    (`temporarily_unavailable`, which is also how admission over-cap sheds
    today in `limits.py`; `rate_limited` for admission once item 4's contract
    bump lands; a vendor `insufficient_quota` under O5 is
    `temporarily_unavailable`, because retrying cannot clear it) and dials
    nothing else: not an operator cloud key, not
    another model, not a stale cache. adepthood treats an answer whose
    `inference_source` differs from the route it disclosed as its existing
    `409` stale-route refusal.
11. **Provenance.** Every model-backed `/v1` answer carries
    `inference_source`, one of `pool:creek`, `pool:attested:<vendor>`,
    `request_key:<vendor>` or `local`, on `ReflectionResponse`, the letter
    response and `ClassificationResponse` (inside `JobStatusResponse.result`),
    with a contract minor bump. It is the "vault contract field that reports
    a local route per answer" ADR 0009 item 4 lists as one of two closures;
    the owner has not chosen between that field and limiting credit
    admission to self-connected vaults. The field ships because the pool
    needs it for labelling either way; whether it closes the item 4 gap is
    an owner escalation. "Non-cloud" in ADR 0009 item 4 is a payer and
    custody term carried by this field, not by the provider's `is_cloud`
    flag.
12. **Telemetry and alerts.** Content-free, as the pilot already requires:
    counts, latency, queue depth, token totals per allocation hash, outcome
    codes, and a pool cost line in the fleet report, which alerts on identity
    mismatches, attestation failures, 401/403 and admission refusals per
    vault, engine or tunnel down, vendor 429s, per-key cap or free-allowance
    exhaustion, gateway restarts, and a warm minimum below one
    (`pool_min_below_one`).
13. **Grounding (Q2).** Managed-vault reflections are ungrounded today
    (Context). Embedding runs at link time (`POST /v1/links`, the bulk pass
    adepthood already runs under its widened deadline) and at reflection time
    (`creek/generate/grounding.py`: the query plus up to 256 live embeds of
    cache misses), never at ingest; `/v1/classifications` does not embed.
    Options: (i) the pool serves the embedding model too, for link-time and
    reflection-time embeddings, under every item above: batches of at most
    256 under the 1 MiB cap, each fragment's tier handed to the chokepoint,
    the vault's parquet cache staying in the vault, and the embedding model,
    its dimensionality and the similarity threshold pinned in the manifest
    identity and recorded in the parquet. Under O3 the pool serves
    `all-MiniLM-L6-v2` itself, so the vector space is unchanged and no index
    migration follows; under O5 the vendor's model differs (Tinfoil
    `nomic-embed-text`, 768 dimensions; Privatemode `qwen3-embedding-4b`), it
    must be inside the attested measurement set, and a managed vault's
    parquet is then vendor-specific, so an export to a self-hosted vault
    re-embeds, which the amendment must state. (ii) Bake the local model into
    the vault image with `HF_HUB_OFFLINE=1`: fails R1 and R2 (a per-wake load
    this record calls the rejected cold start) and is unsized against a 1 GB
    Machine; reference only. (iii) Stay ungrounded. **Recommendation: (i)
    under O3; under O5, (i) only with an attested embedding model, else
    (iii).** (i) amends ADR-0004 for managed vaults only and is recorded as Q2
    because none of ADR-0004's three reopening criteria (a measured quality
    gap, a feature local models cannot serve, a re-architecture where quality
    binds) is met by this cost argument, and its successor clause requires
    the completions consent gate, which a managed vault pins off
    (`managed_local_only`, #1864): the amendment must waive that clause
    explicitly for the pool and keep local sentence-transformers the default
    on self-hosted vaults. Until the owner answers, (iii) is the state, the
    prompt planning above holds, and #1850 measures the real prompt.

### Decision 2. Implementation O3: a Creek-run GPU pool off Fly

Selected if Q1 is answered No. Designed now; deployed only under Decision 4.

1. **Topology.** Three parts.
   - A **pool gateway** app on Fly in the dedicated organization, on its own
     custom private network (`creek-pool`); one Machine in the pilot, two on
     different hosts in production, `min_machines_running = 1`. It declares
     one service, a plain TCP port with no handlers (`ports: [{"port": N}]`,
     `concurrency.type = "connections"`; Fly's `tls` handler is unavailable
     on private addresses, and raw TCP over Flycast is how Fly's own Postgres
     is reached), and terminates mutual TLS itself, so Fly Proxy forwards
     ciphertext. Endpoints are authorized by client-certificate role: vault
     certificates reach generate and `/ready` only; the control plane's
     certificate, from the same CA, reaches the registry push, the
     invalidation call and the metrics endpoint; nothing else is declared, so
     a Flycast address exposes nothing more on any network. Each vault
     reaches the gateway over a **Flycast address allocated on that vault's
     own network** (`fly ips allocate-v6 --private --network <allocation_id>`
     or the Machines API `ip_assignments` call shown in Fly's warm-pool
     blueprint, `{"type": "private_v6", "network": "<allocation_id>"}`, which
     the build issue verifies with a live test; flyctl may allocate through
     Fly's GraphQL API, which would add a second API client and token scope
     to `fly.py`, and per-app Flycast address limits at 500 vaults are
     checked the same way), allocated after `_ensure_app` and before
     `_ensure_machine` (reconcile re-reads it idempotently), released at
     deletion, reconciled by the fleet report. The control plane reaches the
     gateway the same way, over one Flycast address of the gateway allocated
     on the control plane's network, and **pushes** the credential registry,
     invalidations and manifest identities; no Flycast address of the
     control plane exists on `creek-pool`, so the control listener's
     public-edge hardening is untouched and the engine hosts can reach
     nothing of it. The gateway caches the registry with a 60 s TTL, fails
     closed when it cannot refresh, holds no secret on a volume, and keeps
     nothing a Fly snapshot would capture. Its direct 6PN address inside
     `creek-pool` serves no listener: engine hosts are dialled and never
     dial.
   - One or more **engine hosts** off Fly, each a WireGuard peer created on
     the `creek-pool` network (`fly wireguard create ... --network
     creek-pool`, a GraphQL operation that needs its own scoped token, which
     the driver's deploy token is not shown to permit), with persistent
     keepalive, the peer's private key held on the host at mode 0400 and
     rotated on a schedule and on any host change (it reaches the host
     through the GPU provider's API, so the provider can read it, consistent
     with the provider reading plaintext anyway), and a host firewall that
     admits only the gateway Machines' private addresses. Each runs vLLM from
     an image pinned by digest, serving the manifest's identities, bound to
     the tunnel address only. The gateway dials `<peer>._peer.internal` with a
     1 s connect timeout, a read timeout of the request's `deadline_ms` less
     elapsed time, and no retries. In the O2 variant the engine runs as Fly
     Machines inside the gateway app, and no tunnel exists.
   - The **vault side** of Decision 1 (Decision 6).
2. **Engine.** vLLM, chosen for continuous batching across vaults, request
   logging off by default in the pinned release, and Prometheus metrics for
   queue depth (`vllm:num_requests_waiting`), with prefix caching,
   speculative decoding and adapters off. The manifest's `runtime_name` is
   `vllm`, `runtime_digest` is the image digest, and `model_blob_sha256` is
   verified by `model_package.verify_weights` at engine start; the gateway
   reports the manifest's digests on `/ready`.
3. **Host class.** #1875 sizes its candidates at one 24–48 GB GPU at 4- to
   8-bit. At 4-bit with KV cache for concurrency 4 at about 2,600 tokens, a
   24 GB card is marginal for the 27–32 B candidates and fits the 24 B one;
   #1850 decides, so both classes are priced. The cheapest priced row that
   fits the 48 GB class **and** R6's automatic scale-out is RunPod L40S
   Secure Cloud (USD 1.09/h ≈ USD 796/mo; A100 80 GB USD 1.79/h ≈
   USD 1,307/mo); a 24 GB RunPod row (RTX 4090 or A5000 class, Secure Cloud)
   is priced by the build issue from the sheet. Hetzner GEX63 (EUR 999/mo plus
   EUR 999 setup, 96 GB) and GEX131-1 (EUR 1,197.30/mo plus EUR 599 setup,
   96 GB) fit the memory class but scale out by hand; selecting either needs
   the owner to waive R6's autoscale clause explicitly, and their break-even
   above is shown on that assumption. Hetzner is a German processor for
   writers wherever they are, and goes on the recipient list like any host.
   Hetzner GEX44-1 (20 GB, EUR 232.30/mo plus EUR 114 setup) fits the 24 B
   candidate's weights (about 12–13 GB at 4-bit) with about 7 GB left for KV
   cache and runtime overhead, less than the 24 GB class; whether that serves
   concurrency 4 is for #1850, so it is reference only. Hetzner's bare metal
   also closes the host-level swap and core-dump residue of Decision 1
   item 5 that no container host can. RunPod Community Cloud is excluded: its
   hosts are third-party machines, which widens who can read plaintext.
4. **Scaling.** Warm minimum one engine host, never zero. The gateway
   exposes queue depth and in-flight counts on its metrics endpoint, which
   the control plane polls over the channel of item 1; the fleet report
   alerts when p95 queue wait exceeds 2 s over an hour, and scale-out
   adds an engine host automatically through the host provider's API (R6).
   Fly's metrics autoscaler applies to Fly Machines only, so it scales the
   gateway and the O2 variant's engine Machines, not off-Fly hosts. Scale-in
   never goes below one.
5. **Honest limit.** While a request runs, the Creek operator can read it on
   the gateway and on the engine host, the host's provider can read it on
   their hardware, and Fly can read it on the gateway Machine it hosts.
   Nothing is stored. adepthood labels each use, per ADR 0009 item 4. The
   host provider is a new processor and goes on adepthood's recipient list
   (B11, `backend/src/privacy/recipients.py`) before any cohort.
6. **Cost line.** Fixed: the engine host (plus any one-time setup fee), the
   gateway Machines (about USD 2/month each at ADR-0013 Decision 4's figure,
   re-priced on deployment; Fly's table prices are for iad and ewr and vary
   by region), and egress at USD 0.02/GB in North America, negligible at
   reflection sizes. Marginal: zero until a host's measured capacity is
   reached, then one more host.

### Decision 3. Implementation O5: a vendor-attested confidential pool

Selected if Q1 is answered Yes, after the ADR 0009 amendment is merged.

1. **Shape.** No gateway in the default design. The vault's `PoolProvider`
   uses the vendor's attesting client in-process (Tinfoil publishes a Python
   SDK that verifies attestation client-side and refuses to send on failure;
   Privatemode publishes a JavaScript/TypeScript SDK and, for Python, a proxy
   container that attests the backend before the first request and encrypts
   end to end, which would run as a loopback sidecar in the vault image
   whose single egress destination is pinned by the same env key and checked
   by `_verify_machine`). The attesting client is pinned by hash in
   `uv.lock`, reviewed on every bump, runs with no access to `RequestKey`,
   and its verification round trip is inside R2's TTFT budget. The vault
   dials the vendor's endpoint over the public internet inside that
   attestation-bound encrypted channel. Plaintext exists in the vault process
   and inside the vendor's enclave; it crosses no network in the clear,
   Fly's included. This is the R7 deviation.
2. **Pin.** The manifest names the vendor model id and the pinned measurement
   set; `runtime_digest` is a SHA-256 over the pinned measurement bundle.
   Because vendors rebuild often, the pin is the vendor's build signing
   identity (the Sigstore identity its measurements are published under)
   plus a minimum version; a new measurement under that identity is admitted
   only after a review-gated window during which both are served, and
   anything else fails closed until the operator re-pins, as a digest
   mismatch does today.
3. **Substitute conditions, for the owner to accept or reject in place of
   D05's.** D05's five conditions were written for a runtime we control.
   Mapped to a vendor: (a) *real hardware*: kept. (b) *a vendor attestation
   chain*: kept, and not an operator-held key, so ADR-0006's trust-root
   caveat does not apply. With Tinfoil the client verifies the enclave's CPU
   attestation report back to AMD's SEV-SNP root certificate and compares
   its measurements with the Sigstore-published build measurements; the
   GPU's confidential-compute mode is verified inside the enclave at boot
   with NVIDIA's local-gpu-verifier and is covered by the attested
   measurement, not verified by the client against an NVIDIA root. With
   Privatemode the proxy performs the remote attestation of the backend's
   measured components before sending. (c) *nonce freshness*: unmet as
   stated; replaced by the vendor's binding of the attested measurement to
   the live session's key, which the build issue verifies on the vendor's
   design and T18 exercises; needs an explicit owner waiver. (d)
   *user-authorized key release*: not applicable, because no key is
   released: the vault sends plaintext it already holds under ADR-0014. (e)
   *a hostile-host lab*: cannot be run on vendor hardware; unmet, replaced
   by vendor negative tests (T18); needs an explicit owner waiver. Added for
   a vendor: (f) the attested image is open-source and reproducibly built,
   and the pinned measurement is rebuilt from source by Creek or an
   independent party before each re-pin; without (f) the Options table's
   cell reads "vendor-asserted", and this record has not verified (f) for
   either vendor. (g) The vendor's written retention terms and rate limits
   are reviewed under B11 (Privatemode's documentation reports keeping
   request metadata such as IP address, timestamp, API key and request path,
   method, status and latency for up to 90 days, and per-key token usage
   permanently for billing; that is metadata, and it is disclosed). (h)
   Jurisdiction is chosen explicitly (Edgeless Systems GmbH operates
   Privatemode from Bochum, Germany; Tinfoil, Inc. from San Francisco,
   California). No "operator-blind" claim is made until B24 certifies it;
   the per-use label reads, for example, "processed in an attested private
   enclave run by <vendor>; not stored".
4. **Tenancy.** Tinfoil's Admin API (an organization admin key) creates,
   updates (name, expiry, caps) and deletes API keys with optional expiry and
   independent lifetime dollar, input-token and output-token caps; update and
   delete require the full key value, key creation requires active token
   billing, and an exhausted cap answers `insufficient_quota` (429, not
   transient). Each activation gets its own vendor key: cap = tokens consumed
   so far (from the key listing) + `DEFAULT_MONTHLY_CAP` (20) × the measured
   tokens per reflection × 2, reset by `update` on adepthood's calendar
   month; expiry extended in place every 15 days for 30, so one missed cycle
   is survivable; re-issue (create-then-delete, a per-activation Machine
   update) only on compromise; deletion revokes it. The control plane keeps
   the full key in the encrypted bundle and holds the organization admin key
   as a declared secret file with its own alert kind. The key is an unbound
   bearer: copied from a Machine config it works from anywhere until its cap
   is spent, which is why the cap is sized to the allowance. A control plane
   that is down when a key expires leaves that vault `temporarily_unavailable`
   until it returns: fail closed, accepted. Under O5 the vault's own limiter
   enforces item 8's concurrency and rate, and the key's caps are the only
   external control; a Creek relay holding one vendor key is not designed in
   unless T18 proves with a canary that the relay sees ciphertext only. A
   vendor without per-key caps fails this item.
5. **Risks, stated.** Vendor dependency, mitigated by the contract: two
   vendors already fit it and the vault-side code does not change between
   them. Catalogue constraint on #1875. Attestation client maintenance and
   re-pinning when the vendor rotates. Vendor metadata retention and rate
   limits. A vendor outage is a refusal, never a fallback. Public-internet
   egress from the vault to the vendor, encrypted end to end, which the owner
   accepts explicitly by answering Yes. The exclusion of the vendor rests on
   condition (f), which this record has not verified.
6. **Cost line.** Per token, from the vendor's price sheet. Privatemode's
   marketing pricing page lists a free plan of 5,000,000 chat tokens at
   sign-up plus 1,000,000 tokens a month with Inference API access (the docs
   pricing page lists only the sign-up grant; how output tokens are counted
   is not stated; prices are plus VAT where applicable); at face value it
   covers the pilot's ceiling of about 263,000 tokens a month. On Tinfoil
   gpt-oss-120b the pilot's ceiling would cost about USD 0.05 a month on a
   funded account, and Tinfoil has no zero-bill path, since key creation
   requires active token billing. Either way O5 is a per-token paid service,
   cents or not, for the owner to rule on against "no paid runtime".

### Decision 4. Economics, and what the owner must authorize

- Under **No**, the warm minimum is a paid runtime. This record does not
  authorize spend. Deployment waits for an owner note recording the host,
  the monthly figure, the alert threshold and the cleanup owner, in the form
  the pilot checklist already requires. The pilot's USD 25/month alert is a
  storage-era number; a warm GPU minimum is a separate line of USD 796–1,307
  or EUR 999–1,197 a month at today's sheets, or USD 264–528 for the O2
  variant if it measures fit.
- Under **Yes**, the pool is a metered vendor line in the fleet report; the
  owner sets the monthly alert, and the free allowance is not treated as a
  cap.
- Under either, #1850 runs its live mode against the chosen pool before any
  cohort, and the measured token counts replace the planning assumption in
  every cost figure above.

### Decision 5. INTIMATE

The pool is **not** an acceptable INTIMATE destination, under either
implementation. ADR-0014 keeps INTIMATE off managed vaults entirely: the
vault reads plaintext, and `/v1` refuses INTIMATE at the tier ceiling
(`REMOTE_ADMITTED_CEILINGS` in `creek_mcp/policy.py`). That ceiling does not
cover MCP or CLI callers on a self-hosted vault, so the `ModelRouter`
chokepoint (`_enforce_local_for_intimate`, `creek/classify/llm/router.py`)
gains the pool as a redirect class, per Decision 6: an INTIMATE fragment is
refused inside the vault before any `PoolProvider` is built and before any
byte leaves the process, on every transport, and a `default` stage routed to
the pool can never serve as the router's rescue target for INTIMATE. The
pool adds a refusal of any request whose declared tier is `intimate`, as
defence in depth only; it is never the first line, because by then the
plaintext would have left the vault. O5 is the only pool whose inference leg
could later serve INTIMATE, and only once the whole chain of ADR-0005, 0006
and 0007 and ADR 0009 D05 is proven for the vault itself; that is a future
record, not this one.

### Decision 6. Vault-side, control-plane and adepthood changes

Vault side (`creek-tools/`):

- `creek/classify/llm/providers.py`: a `PoolProvider` registered as `pool`.
  It is **not** INTIMATE-eligible. The registry gains a class-level
  `intimate_eligible` flag, `True` for `ollama` and `enclave` (their status
  today; ADR 0009 separately suspends the enclave's use for journal content,
  unchanged here), `False` for `pool` and for every cloud provider.
  `ModelRouter._enforce_local_for_intimate` redirects or refuses on
  `not intimate_eligible` exactly as it does on `is_cloud` today, for the
  stage and for the `default` rescue target alike, and `is_cloud` keeps only
  its consent meaning (`False` for `pool`: no operator cloud key is involved,
  no consent variable is read, and `creek_mcp/bench/local_only.py`'s
  factory admits it; its `require_local_target` then judges the dial
  target, so the O3 Flycast literal in `fdaa::/16` passes as `private` while
  the O5 vendor hostname is `remote` and needs `--allow-remote-host`, which
  Decision 7's pool mode passes and records). The provider dials only
  `CREEK_POOL_URL`, through `creek/classify/llm/pool_boundary.py`.
- `creek/classify/llm/local_boundary.py` and `pool_boundary.py`: the one
  pinned pool destination beyond loopback, and nothing else; the AST
  invariant tests gain the parallel rule of Decision 1 item 2.
- `creek_mcp/model_pin.py`: `PINNED_PROVIDER` becomes the pair `ollama` and
  `pool`; `pinned_model_serves` checks the pool's identity against the
  manifest; `pin_refuses_stage` keeps refusing every cloud stage; the
  `request_key_admitted` then `pin_refuses_stage` ordering in
  `creek_mcp/server.py` is unchanged.
- `creek_mcp/container_health.py`: `--check pool` per Decision 1 item 9.
- `creek_mcp/api/models.py`, `creek_mcp/contract.py`: `inference_source` per
  Decision 1 item 11; `CapabilitiesResponse` reports `inference_pool` so
  adepthood can detect the route before dialling; the `rate_limited` error
  code with `Retry-After`; contract minor bump with the usual fixtures and
  schema bundle.
- `creek_mcp/httpapi/reflect.py` and the letter and classification routes:
  fallback order per Decision 1 item 10, the prompt cap and `prompt_too_long`
  refusal of item 4, and the cancel path of item 7; refusal shapes otherwise
  unchanged.

Control plane (`creek_mcp/provisioning/`):

- `fly.py`: the activation's stored bundle (`FlyRuntimeSecrets`) gains the
  manifest, `CREEK_POOL_URL`, the pool token, client certificate and key,
  versioned per activation; `_runtime_config` and `_runtime_secret_files`
  read them from the bundle and write them into the Machine's `env` and
  `files` on both runtimes, and `_verify_machine` compares each Machine
  against its own activation's stored version (today both functions are
  pure functions of the fleet policy, so a policy flip would reject every
  not-yet-updated Machine); `FlySecretManager` issues and revokes them;
  under O3 the per-network Flycast address is allocated after `_ensure_app`
  and before `_ensure_machine`; revoke, invalidate and release on deletion.
- `fly_vault_bootstrap.py`: `_SECRET_FILES` lists the three pool files so the
  replay bootstrap normalizes them like its other entries (it validates
  listed files; it does not enumerate `/run/secrets`).
- `inventory.py`, `reconcile.py`, `fleet_schema.py`, `budget.py`: the pool
  credential, the Flycast assignment and the gateway app join the inventory,
  so they are neither reported as orphans nor left behind; the fleet policy
  TOML gains the pool's rate, an `observed_on` date and the same staleness
  refusal `creek_mcp/bench/cost.py` applies (new behaviour in `budget.py`);
  the alert kinds of Decision 1 item 12 join the closed set.

adepthood (its own issue, under #3437's parent), in this order: land #3437,
so a request-key answer is not charged a credit; widen `_post_reflection`'s
phase budgets to the 30 s deadline with `_widened_phase_budgets` (the R2
prerequisite) and treat an `inference_source` that differs from the disclosed
route as the existing `409` stale-route refusal; read `inference_source`,
label each use, add the host provider or vendor to the recipient list; under
Yes, the ADR 0009 amendment of Decision 0.

### Decision 7. Measurement before any cohort

#1850's harness gains a live mode for the pool API (today it is
Ollama-specific: `--ollama-url`, `/api/tags` digest). It runs the cold/warm,
context, concurrency and idle sweeps against the chosen pool from a
vault-shaped allocation and from adepthood's side through the replay router,
reports p50/p95 per sweep against R2's budgets, Machine wake, the real prompt
size, and the `capacity` block against `limits.py`'s caps, and attaches the
content-free report to the ratification comment on #1849 together with
#1875's eval. No cohort is admitted on a budget that was not measured.

## Acceptance tests

Each build sub-issue cites the tests it owns. All are hermetic unless marked
live; live checks are evidence, not substitutes.

| Test | Requirement | Asserts |
|---|---|---|
| T1 identity | R8, D1.1 | A pool reporting any identity other than the manifest's is refused before a prompt is sent; a response echoing a different identity is discarded; a loadable manifest plus a matching `/ready` is required for `model-ready`. |
| T2 single destination | R7, D1.2 | AST and runtime. O3: the only non-loopback inference URL a container-mode vault can dial is `CREEK_POOL_URL` from the environment; a vault-config URL, a public address, a hostname and an IPv6 outside `fdaa::/16` are each refused with the fixed URL-free message. O5: the only admitted host is the exact vendor hostname pinned in the manifest; any other hostname, IP literal or vault-config URL is refused; under a loopback sidecar the vault dials loopback and the sidecar's single egress destination is pinned by the same env key. Any module reading `CREEK_POOL_URL` or naming a pool path builds no HTTP client outside the boundary. |
| T3 credential canary | R5, D1.3 | A canary pool token and client key appear in no log record, error body, telemetry, job record or file under the test root, on success and on a pool 401. |
| T4 content canary (O3; engine parts live) | R3, D1.5 | A unique string in a prompt and in a response appears in no gateway log, engine log, metrics label, captured process output, `/dev/shm` entry or file on the pool host, on the success path, on a malformed request (the 422 path) and on a forced exception in the gateway handler; vLLM is started with D1.5's flags against the pinned release and the test fails if any logging flag is present; `PR_SET_DUMPABLE` is 0 in both processes and `--swap-space 0` is set. |
| T5 cache isolation (engine check live) | R3, D1.6 | The engine launch config disables prefix caching, speculative decoding and adapters (hermetic); across a two-vault, identical-prompt run the engine's prefix-cache hit counter stays zero (live). Under O5 this is a vendor-side check recorded with the vendor's documentation. |
| T6 INTIMATE | R4, D5, D6 | An `intimate` ceiling is refused at `/v1` before any dial (existing test, kept); on a stage routed to `pool`, an INTIMATE fragment is redirected to the local `default` or raises `IntimateRoutingError` at the chokepoint with a socket spy showing zero dials; a `default` stage routed to `pool` is never the rescue target; the pool refuses `tier=intimate` and an unknown tier. |
| T7 admission (pool part O3) | R5, D1.8 | A third concurrent generation from one vault, and the 61st in an hour, are refused within 1 s with a content-free `rate_limited` body, at the vault and (O3) at the pool; another vault is unaffected. |
| T8 cross-vault blindness (live) | R5 | Vault A's credential cannot read, cancel or enumerate vault B's requests; the pool API has no listing endpoint; the vault-facing `/ready` reports no queue depth or counts. |
| T9 fail closed | R9, D1.10 | Pool down, identity mismatch, admission refusal and attestation failure each yield the route's existing refusal shape within adepthood's 10 s first-byte window (30 s once widened), with zero dials to any other host (socket spy) and no model substitution. |
| T10 BYOK precedence | R9 | With a request key the vendor answers and the pool is never dialled, on success and on a vendor 5xx (a refusal, not a pool call); without one the pool answers; ADR-0015's canary sweep still passes. |
| T11 lifecycle | R5, D6 | Activation mints and installs the credential and certificate (and under O3 allocates the Flycast address after the app and before the Machine); deletion revokes them in `FlySecretManager.revoke`, the pool refuses the credential before the receipt is issued, the Machine is destroyed after, and the address is released; the fleet report flags a survivor as an orphan. |
| T12 exact config | D6 | `_verify_machine` rejects a Machine whose `env` or `files` differ from its own activation's stored bundle version (`CREEK_POOL_URL`, `CREEK_MODEL_PACKAGE_FILE`, the manifest and the three pool files, on either runtime) and accepts a Machine on an older bundle version until that activation is rolled; `fly_vault_bootstrap` normalizes the pool files with its other listed files. |
| T13 readiness | D1.9 | `--check pool` reports reachable/identity/ready states with stable exit codes and makes no generation (under O5, ready is the attesting client's cached verification of the pinned measurement set); `--check ready` is unchanged and never consults the pool. |
| T14 budgets (live) | R2, D7 | With adepthood's reflection call widened, #1850's pool mode, from adepthood's socket through the replay router, cold and warm, reports vault p95 ≤ 25 s from receipt, pool p95 ≤ 10 s, TTFT p95 ≤ 2 s at concurrency 4, Machine wake, the real prompt size and the capacity block, on the chosen pool. |
| T15 provenance | R10, D1.11 | `inference_source` is `pool:creek`, `pool:attested:<vendor>`, `request_key:<vendor>` or `local` and matches a socket spy's observed destination on every route, including a classification inside `JobStatusResponse.result`. |
| T16 cost line | R6, D4 | The fleet report prices the pool from the dated policy file and refuses an undated or stale sheet, as `bench.sh cost` does (new in `budget.py`). |
| T17 egress (live, O3) | R7 | On a test vault with no request key, with public egress blocked inside the Machine (nftables installed before the privilege drop) or under a socket spy, a reflection still completes through the Flycast address; with the tunnel down, it refuses. |
| T18 attestation (O5) | D3.3 | A tampered measurement, an unpinned measurement, a measurement under another signing identity, a missing transparency-log entry and a plain-TLS endpoint are each refused with nothing sent; a matching one proceeds; the vendor's session binding behaves as documented. |
| T19 warm minimum | R1, D2.4, D3 | The scaling policy refuses a minimum of zero; after #1850's idle sweep the first reflection shows no load or download event and its p95 equals the warm p95 within measurement noise; the fleet report raises `pool_min_below_one`. |
| T20 narrow API | D1.4 | A client-chosen model, sampling, cache or adapter field, an unknown field, a prompt over 4,096 tokens and a body over 1 MiB are each refused 4xx with a content-free body; the vault drops snippets and then refuses an over-cap entry as `invalid_request` / `prompt_too_long` before any dial; `rate_limited` is served only to callers declaring the new minor and 429 is in the closed status set; the sampling profile is inside the served artefact set. |
| T21 no residue | D1.7, D1.12 | After a response the pool holds no request-scoped memory, file or cache entry keyed to that request; telemetry rows carry only the fields D1.12 allows. |
| T22 scale-out (live, O3) | R6, D2.4 | With queue wait held above the alert threshold for the configured window, one engine host is added; scale-in never drops below one. |
| T23 cancellation | D1.7 | Hermetic: a fake pool holds the connection open; after the vault's deadline fires, the cancel handle closes the pool socket within 1 s and the worker thread exits; live: the gateway aborts the engine request on client disconnect. |
| T24 binding and reachability (live, O3) | R5, D1.3, D2.1 | A valid token without a client certificate is refused from every network, and with another allocation's certificate is refused; the control plane's certificate cannot call generate and a vault certificate cannot call the registry or metrics endpoints; no Flycast address of the control plane exists on `creek-pool`; the engine port is unreachable from any address but the gateway's. |
| T25 KV scrub (O3) | D1.5 | Hermetic: the engine's shutdown hook zeroes the pre-allocated KV pool and the tensors read back as zero; host-level residue on a container host is recorded as a residual risk in the host choice. |
| T26 rolling upgrade | D1.1 | Two activations on different bundle versions are both verified and answered by their own identity; a policy flip without a per-activation roll rejects nothing; retiring the old identity after inventory shows no activation on it produces zero refusals. |
| T27 payer (adepthood) | R9, D6 | A request-key answer is charged no credit (#3437); a pool answer is charged once; a `pool` answer against a disclosed `request_key` route is the existing `409`. |

## Consequences

- **Managed vaults can reflect on credits** once the pool is live and
  adepthood's reflection call is widened, with a route adepthood can name on
  every answer. Writers with a key are unchanged.
- **Under No**, nothing deploys until the owner funds a warm minimum that is
  32–52 times the pilot cap (11–21 times for the O2 variant), and plaintext
  gains one more reader under O3 (the GPU host provider), labelled on each
  use. Under Yes, the owner amends ADR 0009 and the inference leg is designed
  to be unreadable by the vendor and host, a target rather than a claim until
  B24 certifies it, while the vault itself stays as readable as ADR-0014
  says.
- **The loopback-only invariant gains one pinned exception**, set by the
  provisioner and not by any config, and the AST tests that guard it grow
  rather than shrink. **The INTIMATE chokepoint gains one predicate**
  (`intimate_eligible`) and no new gate.
- **The control plane learns three more secrets, one more address and a
  bundle version per activation under O3** (token, certificate, key, Flycast
  assignment) plus one shared gateway app per fleet, or one vendor key per
  activation plus one organization admin key under O5, all reconciled.
- **Grounding on managed vaults is decided by Q2**, not assumed; until then
  reflections there stay ungrounded, as today.
- **INTIMATE is exactly where ADR-0014 left it.** Nothing here is evidence
  for an operator-blind claim; B24 still gates every word of public copy.
- **#1875 proceeds** against the catalogue the answer to Q1 implies, and its
  licence approval lands in `APPROVED_MODEL_LICENSES` with the manifest.

## Build sub-issues to cut under #1849 after ratification

1. **Vault-side pool contract** (Decision 6, vault side; T1, T2, T6, T9, T10,
   T13, T15, T23), including the "amended by ADR-0016" paragraphs in
   `docs/managed-vault-fly-pilot.md` ("Managed vaults are local-only for the
   operator, with one exception") and `docs/container-runtime.md`
   ("Loopback-only Ollama in container mode").
2. **Pool gateway service** under O3 (`creek_pool`: narrow API, mutual TLS,
   auth, limits, no-log, `/ready`, operator port; T3, T4, T5, T7, T8, T20,
   T21, T24, T26). Under O5, the attesting `PoolProvider` and per-vault vendor
   keys instead (T3, T5 as a vendor-side check, T7 at the vault, T8, T18, T20,
   T21, T26; T4 and T24 not applicable: no Creek-run engine).
3. **Control-plane integration** (Decision 6, control plane; T11, T12, T16).
4. **Engine host runbook and image pin** under O3 (vLLM digest, weights
   verify at start, WireGuard peer on `creek-pool` and the token scope that
   creates it, firewall, key custody and rotation, process-level hardening,
   scale-out automation; T4, T17, T19, T22, T25). Under O5, T19 against the
   vendor.
5. **Bench pool mode and the measurement report** (Decision 7; T14),
   measuring the O2 variant first under No.
6. **adepthood**: #3437, widen the reflection call's phase budgets, the
   stale-route `409` on provenance mismatch, provenance label, recipient
   list, and the ADR 0009 amendment under Yes (T27).
7. **Grounding** per the answer to Q2: the pool's embedding endpoint under
   (i), or the baked-in model under (ii), with ADR-0004's amendment.

Owner escalations, in order: Q1 (which also settles the R7 deviation); under
Yes the vendor choice, condition (f)'s evidence, the two D05 waivers and the
ADR 0009 amendment, under No a budget line, the host choice and an R6 waiver
if a hand-scaled host is chosen; Q2; the ADR 0009 item 4 gap-closure
mechanism; then #1875's model and licence.

## Sources (read 2026-10-17 through search summaries; re-read before ratification)

- Fly GPU deprecation: https://fly.io/blog/transcribing-on-fly-gpu-machines/
  (banner); https://community.fly.io/t/gpu-migration-fly-io-gpus-will-be-deprecated-as-of-july-31-2026/27110;
  https://fly.io/docs/gpus/ (serves https://fly.io/blog/wrong-about-gpu/,
  February 2025, superseded by the 2026 banner).
- Fly prices (iad/ewr base, vary by region): https://fly.io/pricing/,
  https://fly.io/docs/about/pricing/ (`performance-8x` USD 0.3667/h,
  `performance-16x` USD 0.7334/h; RAM USD 6/GB-month; egress USD 0.02/GB).
- Flycast and private networking: https://fly.io/docs/networking/flycast/
  (`--network`); https://fly.io/docs/networking/private-networking/;
  https://docs.fly.io/flyctl/cmd/fly_ips_allocate-v6;
  https://fly.io/docs/blueprints/warm-pool-user-machines/ (`ip_assignments`,
  not in https://docs.fly.io/machines/api/).
- WireGuard: https://fly.io/docs/blueprints/bridge-deployments-wireguard/;
  https://docs.fly.io/flyctl/cmd/fly_wireguard_create (`--network`).
- Fly autoscaling: https://fly.io/docs/launch/autoscale-by-metric/;
  https://fly.io/docs/launch/autostop-autostart/.
- vLLM: https://docs.vllm.ai/en/latest/cli/serve/ (logging flags);
  https://docs.vllm.ai/en/stable/api/vllm/config/cache/ (prefix caching
  default); https://docs.vllm.ai/en/stable/usage/usage_stats/;
  https://docs.vllm.ai/en/stable/usage/metrics/.
- Privatemode: https://docs.privatemode.ai/pricing/;
  https://www.privatemode.ai/pricing (free plan);
  https://docs.privatemode.ai/models/overview/;
  https://docs.privatemode.ai/architecture/overview/;
  https://docs.privatemode.ai/api/proxy-configuration/ (`--port`; the
  camelCase `--apiKey` is being replaced by `--api-key`);
  https://docs.privatemode.ai/reference/sdk/;
  https://docs.privatemode.ai/getting-started/faq/ and
  https://docs.privatemode.ai/security/trust-and-compliance/ (retention);
  https://www.edgeless.systems/imprint.
- Tinfoil: https://tinfoil.sh/inference (catalogue and prices);
  https://docs.tinfoil.sh/resources/changelog (Kimi K2.6 sunset);
  https://docs.tinfoil.sh/verification/attestation-architecture;
  https://docs.tinfoil.sh/verification/verification-in-tinfoil;
  https://github.com/tinfoilsh/tinfoil-python;
  https://docs.tinfoil.sh/admin/admin-api (keys, expiry, lifetime caps);
  https://docs.tinfoil.sh/sdk/error-handling (`insufficient_quota`);
  https://docs.tinfoil.sh/guides/proxy-server;
  https://tinfoil.sh/blog/2026-07-14-secure-prompt-caching;
  https://tinfoil.sh/privacy.
- DeepInfra (O4 only): https://deepinfra.com/Qwen/Qwen3-32B.
- GPU hosts (O3): https://www.runpod.io/pricing (page dated 2026-10-08);
  https://docs.runpod.io/pods/overview (Secure versus Community Cloud);
  Hetzner price list effective 2026-06-15,
  https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/
  (GEX44-1, GEX131-1, excl. IPv4);
  https://www.hetzner.com/pressroom/hetzner-expands-gpu-portfolio-with-new-gex63-featuring-nvidia-rtx-pro-6000-blackwell-max-q/
  (GEX63, 2026-10-06, incl. IPv4);
  https://docs.hetzner.com/robot/dedicated-server/server-lines/gpu-server/
  (current line-up; GEX130 no longer listed).
- Attested GPU hosts (O6): Azure `Standard_NCC40ads_H100_v5` USD 6.98/h
  pay-as-you-go in East US 2, from a third-party aggregator of the Azure
  Retail Prices API
  (https://costcalc.cloudoptimo.com/azure-pricing-calculator/vm/Standard-NCC40ads-H100-v5;
  up to USD 9.73/h elsewhere; not confirmed on a Microsoft page);
  https://phala.com/gpu-tee (H100 TEE on demand, 24-hour minimum; reserved
  slot term not published).
- gpt-oss-120b licence: https://openai.com/index/gpt-oss-model-card/
  ("available under the Apache 2.0 license and our gpt-oss usage policy").
