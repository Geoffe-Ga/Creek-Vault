# Pool measurement pin and warm minimum

How an operator pins the attested inference pool's measurement set, re-pins it
when the vendor rebuilds, and gathers the evidence that the pool is actually
warm — ADR-0016 acceptance test **T19**.

## Status: what is decided, and what is not

Read this before you act on any step below. Every procedure here is written
against a design that is **selected but not in force**.

- **ADR-0016 is ratified as O5**, the vendor-attested confidential pool (owner
  decision 2026-10-20, recorded on
  [#1849](https://github.com/Geoffe-Ga/Creek-Vault/issues/1849)). By the
  record's own Status clause, `Accepted` takes effect only once the **adepthood
  ADR 0009 amendment** to items 3 and 4 merges. Until it does, **O3 remains the
  standing fallback design**, which is why the dormant O3 runbook below is kept
  rather than deleted.
- **The vendor is not chosen.** It is an open owner escalation, together with
  condition (f)'s evidence and the two D05 waivers
  ([ADR-0016](architecture/ADR/0016-shared-confidential-warm-inference-pool.md),
  Decision 3.3 and the escalation order at the end of that record). So this
  page is vendor-neutral: it says how to pin and verify a measurement set and
  what evidence to record, and it asserts nothing about any candidate's
  properties. Where a value is the vendor's to supply, the table cell reads
  `<to be recorded>`.
- **No spend is authorized** (Decision 4, owner comment 2026-10-20: design and
  measure only). Nothing on this page may be executed against a vendor until
  the owner records a budget line. The procedures are written so they can be
  reviewed now and run later.
- **Nothing here is a public claim.** Every custody or privacy word on a
  user-facing surface waits for adepthood B24
  ([adepthood#3076](https://github.com/Geoffe-Ga/adepthood/issues/3076)).
- **INTIMATE never reaches the pool**, under either implementation
  (ADR-0016 Decision 5). The `ModelRouter` chokepoint refuses or redirects an
  INTIMATE fragment before any `PoolProvider` is built; the pool's own tier
  refusal is defence in depth only.

### What this page is not

It is not a provisioning script, not a vendor comparison, and not evidence
that the pool is private. It records what was pinned, how to re-pin it, and
which of T19's clauses are satisfied — including the ones that are not.

## 1. What is pinned, and what actually verifies it

A managed vault carries one `00-Creek-Meta`-external model package manifest
(#1855 schema, version 1, **fields unchanged** by ADR-0016), read from the path
in `CREEK_MODEL_PACKAGE_FILE` and settable from no vault config
(`creek_mcp/model_package.py`). Under O5 the operator fills it for a vendor
runtime as follows.

| Manifest field | Under O5 it holds | Verified by |
|---|---|---|
| `runtime_name` | The vendor runtime's name, exactly as its identity listing reports it | The identity digest below |
| `runtime_version` | The pinned minimum version, exactly as reported | The identity digest below |
| `runtime_digest` | The SHA-256 over the pinned measurement bundle (Decision 3.2) | **Nothing in the vault today.** The field's shape is validated; no code reads it. Its verification is the attesting client's, which is not built (#1896) |
| `model_name` | The vendor's catalogue model id | The identity listing must offer it |
| `runtime_inventory_digest` | The pool identity digest, defined below | `pool_identity_matches`, before every prompt |
| `model_blob_sha256` | Required by the schema, which ADR-0016 leaves unchanged, so the operator still fills it. No vault-side check consumes it for a pool runtime: `verify_weights` runs where the weights are, which under O5 is inside the vendor's enclave and not ours to run | — |

### The pool identity digest

A pool runtime has no Ollama `/api/tags` manifest digest to report, so
`runtime_inventory_digest` is **defined** for it, without adding a field, as

```text
SHA-256( "creek-pool-identity/1" \n <runtime_name> \n <runtime_version> \n <model_name> )
```

The scheme tag is `POOL_IDENTITY_SCHEME` in
`creek/classify/llm/providers.py`; compute a candidate with
`pool_identity_digest(runtime_name, runtime_version, model_name)` and paste the
lowercase hex result into the manifest. A pool whose listing implies any other
digest is refused **before a prompt is sent**, and a response echoing a
different model is discarded rather than returned.

### What the health check tells you, and what it costs

`creek-container-health --check pool` reads the pinned destination
(`CREEK_POOL_URL`, Machine environment only) and the identity listing, and
**makes no generation**: a canary on a metered vendor would be a billed call on
every healthcheck, which the no-spend decision rules out. `--check ready` never
consults the pool, and storage-ready implies neither model-ready nor
pool-ready.

| State | Exit code | Means |
|---|---|---|
| `pool-ready` | 0 | The pool offers the pinned model at the pinned identity |
| `pool-unconfigured` | 30 | No manifest, or no `CREEK_POOL_URL` — no network call was made |
| `pool-unreachable` | 31 | The listing did not answer usably |
| `pool-model-missing` | 32 | The listing offers no such model |
| `pool-identity-mismatch` | 33 | The listing implies a different identity digest |

Dial budgets, from `creek/classify/llm/pool_boundary.py`: connect 1 s, request
12 s, no retries, no second destination, and a cancel handle that closes the
socket at the vault's deadline. Exits 31 and 33 are refusals, never a fallback
to another model.

## 2. The pin identity, and the re-pin window

Vendors rebuild their attested images often, so a bare measurement digest would
fail closed every week. Decision 3.2 pins the **publisher** instead and admits
new measurements under a review gate.

Record, per pin:

| Pin field | Value |
|---|---|
| Build signing identity (the Sigstore identity the measurements are published under) | `<to be recorded>` |
| Minimum runtime version admitted | `<to be recorded>` |
| Measurement bundle digest, currently pinned (`runtime_digest`) | `<to be recorded>` |
| Transparency-log entry for that bundle | `<to be recorded>` |
| Who rebuilt that bundle from source, and when (condition (f)) | **Nobody. Unverified — see below** |
| Date pinned, and the reviewer | `<to be recorded>` |

### Re-pin procedure

1. **Observe the new measurement.** The vendor publishes a measurement under
   the pinned signing identity at or above the minimum version.
2. **Review it before admitting it.** Record the new bundle digest, its
   transparency-log entry, and — when condition (f) is ever satisfied — who
   rebuilt it from source. A measurement under a different signing identity is
   not a re-pin; it is a new pin and a new review.
3. **Open the dual-serve window.** During a review-gated window both the old
   and the new measurement are served. Anything outside that pair **fails
   closed** until the operator re-pins, exactly as a digest mismatch does
   today (exit 33 above).
4. **Roll per activation, never fleet-wide.** The identity lives in the
   activation's stored `PoolRuntimeBundle` (`bundle_version`, `pool_url`,
   `manifest_json`, credential — `creek_mcp/provisioning/pool_bundle.py`), and
   `_verify_machine` compares each Machine against **its own activation's**
   stored version. Write the next bundle version, update that Machine's config,
   then verify. A Machine on an older bundle version stays accepted until its
   activation is deliberately rolled, so a re-pin rejects nothing it did not
   roll (T12, T26).
5. **Retire the old identity last.** Only once inventory shows no activation on
   it. Then zero refusals follow.
6. **Record the change** in this page's pin table, with the reviewer and date.

The Machine-side files a roll touches are
`/run/secrets/creek_model_package.json` (the manifest) and
`/run/secrets/creek_pool_key` (the per-activation vendor key). Never paste a
real key value into this page, a commit message, or an issue: record the
vendor's own reference for it instead.

## 3. Decision 3.3: every condition, with a verdict

D05 was written for a runtime we control. Decision 3.3 maps its five
conditions onto a vendor and adds three. **Every row below is the record's
verdict, not evidence.** Five of the eight carry an open owner gate: two
waivers, (f)'s evidence, and the two that wait on the vendor choice.

| Condition | Verdict | Owner gate, if open |
|---|---|---|
| (a) Real hardware | kept | — |
| (b) A vendor attestation chain, not an operator-held key | kept — so ADR-0006's trust-root caveat does not apply | — |
| (c) Nonce freshness | unmet-and-replaced by the vendor's binding of the attested measurement to the live session key; **waiver required and not granted** | Owner must waive D05 (c) explicitly; the substitute behaviour is T18's to exercise |
| (d) User-authorized key release | not applicable — no key is released; the vault sends plaintext it already holds under ADR-0014 | — |
| (e) A hostile-host lab | unmet-and-replaced by vendor negative tests (T18); **waiver required and not granted** | Owner must waive D05 (e) explicitly |
| (f) The attested image is open-source and reproducibly built, and the pinned measurement is rebuilt from source before each re-pin | **unverified — vendor-asserted** | Owner escalation: (f)'s evidence. ADR-0016 states it has **not** verified (f) for either candidate |
| (g) Written retention terms and rate limits reviewed | unverified here — to be recorded at vendor selection, under adepthood B11 | Owner escalation: the vendor choice |
| (h) Jurisdiction chosen explicitly | unverified here — to be recorded at vendor selection | Owner escalation: the vendor choice |

### Condition (f), stated plainly

The exclusion of the vendor and its host from the set of parties that can read
a prompt **rests entirely on condition (f)**, and (f) is unverified. ADR-0016
Decision 3.3 and Decision 3.5 both say so: this record has not verified, for
either candidate, that the attested image is open-source and reproducibly
built, nor that anyone rebuilds the pinned measurement from source before a
re-pin.

Until an independent rebuild is recorded in the pin table above:

- the Options-table cell for who can read under O5 reads **"vendor-asserted"**,
  and so does this runbook;
- no "operator-blind" claim is made anywhere, on any surface, and none would be
  supportable;
- the per-use label stays at the wording in section 5.

### (g) retention and rate limits, and (h) jurisdiction

These are recorded **at vendor selection**, not before. ADR-0016 Decision 3.3
records, for its two unchosen candidates, the shape of what must be captured:
documented retention of request *metadata* (addresses, timestamps, API key,
request path, method, status, latency) for a bounded period, per-key token
usage kept permanently for billing, and a named operating company in a named
jurisdiction. That is metadata and it is disclosed; it is not journal content,
and it is not nothing. Capture, for whichever vendor is chosen:

| Item | Value |
|---|---|
| Retention of request metadata, and for how long | `<to be recorded>` |
| Usage records kept for billing, and for how long | `<to be recorded>` |
| Documented cache isolation per key (T5's vendor-side check) | `<to be recorded>` |
| Rate limits, and the error shape when one is hit | `<to be recorded>` |
| Operating company and jurisdiction | `<to be recorded>` |
| Date the terms were read, and by whom | `<to be recorded>` |
| Added to adepthood's recipient list (B11) | `<to be recorded>` |

A vendor **without per-key caps fails Decision 3.4** and is not selectable; the
owner's 2026-10-20 comment makes that binding at selection.

## 4. Key custody and rotation

Two secrets exist under O5. Neither may ever appear in a log, an error body, a
job record, a commit or this page.

**The organization admin key** creates, updates and deletes per-activation
vendor keys. It is held by the control plane as a declared secret file with its
own alert kind, and re-issued **only on compromise** (create-then-delete, a
per-activation Machine update). *Not built:* the declared-secret entry and that
alert kind belong to the control-plane issue
([#1900](https://github.com/Geoffe-Ga/Creek-Vault/issues/1900)); the only pool
alert kind registered today is `pool_credential_orphan`.

**One vendor key per activation**, kept in full in the encrypted bundle because
the vendor's update and delete calls require the value
(`PoolCredentialKind.VENDOR_KEY`, installed at `/run/secrets/creek_pool_key`).
It is an unbound bearer: copied out of a Machine config it works from anywhere
until its cap is spent, which is why the cap — not the network — is the brake.

| Lifecycle step | Rule | Where it lives |
|---|---|---|
| Size the caps | `tokens spent so far + monthly cap × measured tokens per reflection × 2`, independently per direction | `size_pool_caps` / `pool_cap_tokens` |
| Which measurement may price a cap | Only provider-reported counts. A `byte_upper_bound` is a bound on bytes; `unreported` and `not_measured` are absences. Each refuses with `ValueError` | `measured_reflection_tokens` |
| Reset | By the vendor's update call, on adepthood's calendar month | #1900 |
| Expiry | Extended **in place** every 15 days for 30, so one missed cycle is survivable | #1900 |
| Re-issue | Create-then-delete, only on compromise | #1900 |
| Revoke | At deletion, confirmed **before** the content-free receipt is issued; the issuer raises rather than returns on an unconfirmed revocation | `PoolBundleIssuer.revoke` |
| Reconcile | A surviving credential is an orphan in the fleet report (`pool_credential_orphan`) | `AlertKind` |

An exhausted cap answers `insufficient_quota` (429, non-transient). The
contract maps it to `temporarily_unavailable` so the vault refuses rather than
spending on, because retrying cannot clear it — *designed, not built:* the
mapping lives in the attesting provider (#1896), and no code in this
repository reads that vendor status today. A control plane that is down when a key
expires leaves that vault `temporarily_unavailable` until it returns — fail
closed, accepted.

## 5. The per-use label, and the standing bar

The per-use label wording, per Decision 3.3:

```text
processed in an attested private enclave run by <vendor>; not stored
```

No stronger word than that, and no "operator-blind" claim of any kind, until
adepthood B24 certifies the copy — and, for the exclusion the label implies,
not before condition (f) has evidence. "Not stored" is a statement about
prompt and response content, not about the vendor's disclosed metadata
retention in section 3 (g).

## 6. T19: the warm minimum, and the evidence it needs

> **T19** (R1, D2.4, D3): the scaling policy refuses a minimum of zero; after
> #1850's idle sweep the first reflection shows no load or download event and
> its p95 equals the warm p95 within measurement noise; the fleet report raises
> `pool_min_below_one`.

Status, clause by clause, at this commit:

| Clause | Under O5 it means | Status |
|---|---|---|
| The scaling policy refuses a minimum of zero | There is no Creek-run engine host to scale. Residency is the vendor's property, asserted in its documentation and bounded by the contract's "a vendor outage is a refusal, never a fallback". The O3 policy that refuses a minimum of zero is dormant (section 7) | **Not met, and not implementable vault-side.** Owner gate: the vendor choice. What replaces it is the documented-residency row below plus the measured equivalence |
| The first reflection after idle shows no load or download event | The attested channel gives the vault no engine-level telemetry, and no catalogue in ADR-0016's sources documents a load or download event, so unless the chosen vendor publishes one this clause has no observable signal under O5. The substitute is weaker and is recorded as such: latency equivalence below, plus the vendor's written residency terms | **Not met.** Owner gates: the vendor choice and a budget line (Decision 4) |
| Its p95 equals the warm p95 within measurement noise | The harness's `idle` sweep (`--idle-cycles`, `--idle-seconds`) reports a `resume` phase beside `warm` in `per_sweep.idle.per_phase`; the two p95s are compared | **Not met.** The harness is Ollama-specific today (`--ollama-url`, `/api/tags`); pool mode is [#1892](https://github.com/Geoffe-Ga/Creek-Vault/issues/1892), and the live run needs the owner's budget note ([#1899](https://github.com/Geoffe-Ga/Creek-Vault/issues/1899)) |
| The fleet report raises `pool_min_below_one` | Hermetic: the alert kind is in the closed `AlertKind` vocabulary and the report raises it when a warm minimum reads below one | **Not met:** `pool_min_below_one` is **not registered** in `AlertKind` — it belongs to [#1900](https://github.com/Geoffe-Ga/Creek-Vault/issues/1900). `tests/test_pool_runbook_docs.py` fails this page when that lands, so this row cannot go stale |

### The procedure, for when those gates clear

Do not run this until the owner records a budget line. Then, from a
vault-shaped allocation:

1. Confirm the pin: `creek-container-health --check pool` exits 0.
2. Run the idle sweep in pool mode with at least one cycle whose
   `--idle-seconds` exceeds the vendor's documented idle behaviour, alongside a
   warm sweep in the same run so both phases share one harness and one
   endpoint.
3. Read `per_sweep.idle.per_phase.resume.latency.p95` against
   `per_sweep.warm.latency.p95`. Equal within measurement noise is the
   evidence; a `resume` p95 materially above `warm` is a cold start by another
   name, and T19 fails.
4. Attach the content-free report, the measured
   `tokens_per_reflection` block, and the vendor's written residency and rate
   terms to the ratification comment. **No cohort is admitted on a budget that
   was not measured.**
5. Record the outcome in the pin table in section 2, with the date and the
   reviewer.

Report fields and flags are documented in [bench.md](bench.md).

## 7. The O3 engine-host runbook, dormant

Kept, not deleted: ADR-0016's Status clause keeps **O3 as the standing fallback
design** until the adepthood ADR 0009 amendment merges, and Decision 3.5
mitigates vendor dependency on the contract being portable between the two.
Nothing in this section is live. If O3 is built, these become requirements and
the acceptance tests below become live again.

### Dormant acceptance tests

| Test | Why it is dormant under O5 |
|---|---|
| **T4** content canary | There is no Creek-run gateway, engine log, captured process output, `/dev/shm` entry or pool host to sweep |
| **T17** Flycast egress | There is no Flycast address: the vault dials the vendor over the public internet inside the attestation-bound channel (the R7 deviation the owner accepted) |
| **T22** scale-out | There is no engine host to add |
| **T25** KV-pool scrub | There is no engine process to hook a shutdown handler onto |

T24 (binding and reachability) is likewise not applicable under O5, per the
sub-issue cut table on #1849. T5 survives as a **vendor-side** check recorded
with the vendor's documentation.

### What O3 would require

- **Engine image, pinned by digest** to a vLLM release where
  `--enable-log-requests` defaults to **off**. `--enable-log-outputs` is never
  set. `VLLM_LOGGING_LEVEL` is pinned at `INFO` or above as defence in depth,
  because with request logging on, prompt inputs are logged at `DEBUG`.
  `VLLM_NO_USAGE_STATS=1`.
- **No offload, no cross-tenant cache**: `--swap-space 0`, no
  `--cpu-offload-gb`, `--no-enable-prefix-caching` (vLLM enables prefix caching
  by default on V1 for supported models), speculative decoding off, no LoRA
  adapters, no KV cache offloaded to disk.
- **Process hardening**: `prctl(PR_SET_DUMPABLE, 0)` in both the gateway and
  the engine, which also blocks `ptrace` and `/proc/<pid>/mem`. No
  error-reporting SDK and no traceback-with-locals formatter. Upstream error
  bodies are mapped to outcome codes, never relayed or logged, because a vLLM
  422 echoes the submitted field.
- **Weights verified at start** via `model_package.verify_weights`, against the
  manifest's `model_blob_sha256`. That field covers exactly one file, so a
  sharded model ships as **one packed archive** verified before unpacking.
- **Network**: a WireGuard peer created on the `creek-pool` network — a
  GraphQL operation needing its own scoped token, which the driver's deploy
  token is not shown to permit — with persistent keepalive, the peer key at
  mode 0400 on the host, rotated on a schedule and on any host change, and a
  host firewall admitting only the gateway Machines' private addresses. Vaults
  reach the gateway over a Flycast address allocated on each vault's own
  network.
- **Scaling**: warm minimum one engine host, **never zero**; scale-out adds a
  host automatically through the provider's API when p95 queue wait exceeds the
  alert threshold over the window; scale-in never drops below one. Fly's
  metrics autoscaler covers Fly Machines only, so it does not scale an off-Fly
  host.
- **Residual risks that no container host closes**: host swap and host core
  capture. A tenant cannot set `swapoff` or `kernel.core_pattern`, and a piped
  core pattern ignores `RLIMIT_CORE`. These close only on a VM or bare metal.
  Single-tenancy while a pod runs is not dedicated hardware.
- **Honest limit, if O3 ships**: while a request runs the Creek operator can
  read it on the gateway and on the engine host, the host's provider can read
  it on their hardware, and Fly can read it on the gateway Machine. Nothing is
  stored. The host provider is a new processor and goes on adepthood's
  recipient list before any cohort.

## Sources

- [ADR-0016](architecture/ADR/0016-shared-confidential-warm-inference-pool.md)
  — Decision 1 (the pool contract), Decision 2 (O3), Decision 3 (O5),
  Decision 4 (spend), Decision 5 (INTIMATE), Decision 7 (measurement), and the
  acceptance-test table.
- Owner ratification and the sub-issue cut table: comments on
  [#1849](https://github.com/Geoffe-Ga/Creek-Vault/issues/1849).
- Code this page quotes: `creek/classify/llm/pool_boundary.py`,
  `creek/classify/llm/providers.py` (`POOL_IDENTITY_SCHEME`,
  `pool_identity_digest`, `pool_identity_matches`, `PoolProvider`),
  `creek_mcp/model_pin.py`, `creek_mcp/model_package.py`,
  `creek_mcp/container_health.py`, `creek_mcp/provisioning/pool_bundle.py`,
  `creek_mcp/provisioning/budget.py`.
- Operator context: [managed-vault-fly-pilot.md](managed-vault-fly-pilot.md),
  [container-runtime.md](container-runtime.md), [bench.md](bench.md).
