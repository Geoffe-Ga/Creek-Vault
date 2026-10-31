# Pool measurement pin and warm minimum

How an operator pins the attested inference pool's measurement set, re-pins it
when the vendor rebuilds, and gathers the evidence that the pool is actually
warm — ADR-0016 acceptance test **T19**.

## Status: what is decided, and what is not

Read this before you act on any step below. Every procedure here is written
against a design that is **selected but not yet built**.

- **ADR-0016 is ratified as O5**, the vendor-attested confidential pool (owner
  decision 2026-10-20, recorded on
  [#1849](https://github.com/Geoffe-Ga/Creek-Vault/issues/1849)). By the
  record's own Status clause, `Accepted` takes effect only once the **adepthood
  ADR 0009 amendment** to items 3 and 4 merges. That amendment merged on
  2026-10-21 ([adepthood#3485](https://github.com/Geoffe-Ga/adepthood/pull/3485));
  ADR-0016's own Status line has not yet been updated to say so. The attesting
  client that O5 needs is not built
  ([#1896](https://github.com/Geoffe-Ga/Creek-Vault/issues/1896), open), so
  **O3 remains documented as the fallback design**, which is why the dormant O3
  runbook below is kept rather than deleted.
- **The vendor is chosen: Privatemode, operated by Edgeless Systems GmbH from
  Bochum, Germany** (owner decision 2026-10-21,
  `owner:2026-10-21#attested-vendor-and-waivers`, on
  [#1896](https://github.com/Geoffe-Ga/Creek-Vault/issues/1896); recorded in
  the 2026-10-21 amendment to [adepthood ADR 0009](https://github.com/Geoffe-Ga/adepthood/blob/main/docs/adr/0009-privacy-custody-and-inference-architecture.md)). The same decision
  waived Decision 3.3 conditions (c) and (e), each with a named substitute,
  and waived (f) on vendor-asserted evidence (section 3). Where a value is
  still the vendor's to supply, the table cell reads `<to be recorded>`.
- **A bounded pilot budget is authorized** (owner decision 2026-10-21 on
  [#1899](https://github.com/Geoffe-Ga/Creek-Vault/issues/1899), for that
  issue's live measurement run only). It replaces the 2026-10-20 "design and
  measure only" answer for that run. Agent work provisions nothing and spends
  nothing; the first vendor key is a human action (adepthood ADR 0009, "Noted
  2026-10-21").
- **The owner created the pilot key on 2026-10-27** (section 4). It is capped at
  €10/month and expires after one year.
- **Still missing from this page:** procedures for model withdrawal, a vendor
  outage, cap exhaustion and rollback. They are tracked under
  [#1901](https://github.com/Geoffe-Ga/Creek-Vault/issues/1901) and are not
  written yet.
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
(#1855 schema, version 1, **fields unchanged** by ADR-0016 except for the one
optional `embedding` pin ADR-0018 adds), read from the path
in `CREEK_MODEL_PACKAGE_FILE` and settable from no vault config
(`creek_mcp/model_package.py`). Under O5 the operator fills it for a vendor
runtime as follows.

The schema is `extra="forbid"`: the twelve fields below other than
`embedding` are required with **no defaults**, and a manifest missing any one
of them does not load at all. `embedding` is the one optional field (#1904,
ADR-0018); leaving it out is how a manifest says the vendor offers no
attested embedding model, and a managed vault then stays ungrounded. A
manifest that fails to load is not a warning — `configured_manifest` raises
`ModelPackageError`, `pinned_runtime_serves` returns `False`, and
`--check pool` reports `pool-unconfigured` (**exit 30**, section 3), which
looks identical to having configured no pool. Check the manifest first when
that exit appears.

| Manifest field | Under O5 it holds | Verified by |
|---|---|---|
| `schema_version` | Literal `1`. ADR-0016 leaves the #1855 schema unchanged | The schema |
| `runtime_name` | The vendor runtime's name, exactly as its identity listing reports it | The identity digest below |
| `runtime_version` | The exact version the listing reports, which is what the identity digest binds. **Not** the minimum admitted version — that is a separate review-gate record in section 2 ("Minimum runtime version admitted"). `_pinned_version` refuses a range expression or a floating tag, and `pool_identity` hashes this string verbatim into the digest that `pool_identity_matches` compares with `hmac.compare_digest`, so writing the minimum here mismatches and exits 33 for a pool correctly inside the pinned window | The identity digest below |
| `runtime_digest` | The SHA-256 over the pinned measurement bundle (Decision 3.2) | **Nothing in the vault today.** The field's shape is validated; no code reads it. Its verification is the attesting client's, which is not built (#1896) |
| `model_name` | The vendor's catalogue model id | The identity listing must offer it |
| `runtime_inventory_digest` | The pool identity digest, defined below | `pool_identity_matches`, before every prompt |
| `model_blob_sha256` | Required by the schema, which ADR-0016 leaves unchanged, so the operator still fills it. No vault-side check consumes it for a pool runtime: `verify_weights` runs where the weights are, which under O5 is inside the vendor's enclave and not ours to run | — |
| `quantization` | Non-empty. **Hard to source for a vendor runtime:** a vendor publishes a catalogue model id, not always its quantization. Take it from the measurement bundle the pin covers, and record the pin's own wording rather than inferring one | The schema (non-empty only) |
| `parameter_count` | Positive integer. **Hard to source:** derive it from the model's published family size, and record where the figure came from in the pin table below | The schema (positive only) |
| `size_bytes` | Positive integer. **Hard to source:** a vendor-hosted model publishes no blob size. Use the upstream open-weights artefact the vendor names; it is not verified vault-side | The schema (positive only) |
| `license_spdx` | An SPDX identifier, pattern-checked. **Hard to source:** the enclave image's licence and the model's licence may differ; this field is the model's | The schema (pattern only) |
| `license_url` | Where that licence is published | — |
| `embedding` | **Optional.** The attested embedding model, as an object of four fields: `model_name` (the vendor's catalogue id, e.g. `qwen3-embedding-4b`), `dimensions` (the vector length the vendor serves it at — take it from the vendor, not from the upstream model card, which may describe a different truncation), `similarity_threshold` (the resonance threshold calibrated for this model, strictly between 0 and 1; MiniLM's `0.75` does not transfer), and `runtime_inventory_digest` (`pool_identity_digest(runtime_name, runtime_version, <embedding model_name>)`). Omit it when the vendor has no attested embedding model | The identity listing must offer the model at that digest before any text is sent; every answer must name the model and have exactly `dimensions` floats per vector |

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

`python -m creek_mcp.container_health --check pool` reads the pinned destination
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

*Steps 1-3 are the vendor's side plus this page's review gate, and have **no
code in this repository today**: the minimum-version pin is a record in the
table above, not an admission rule, and no dual-serve window exists — a vault
admits exactly one `runtime_inventory_digest`, and there is no two-digest
admission path anywhere in `creek/` or `creek_mcp/`. Only step 4 has code.*

1. **Observe the new measurement.** The vendor publishes a measurement under
   the pinned signing identity at or above the minimum version.
2. **Review it before admitting it.** Record the new bundle digest, its
   transparency-log entry, and — when condition (f) is ever satisfied — who
   rebuilt it from source. A measurement under a different signing identity is
   not a re-pin; it is a new pin and a new review.
3. **Open the dual-serve window.** *Designed, not built.* The design is that
   during a review-gated window both the old and the new measurement are
   served, and anything outside that pair fails closed until the operator
   re-pins. No code admits two digests: today a single digest either matches
   or the vault refuses (exit 33 above), so in practice a re-pin is a
   cutover, not a window, and the old measurement stops being served the
   moment the manifest changes.
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
verdict, not evidence.** The owner settled the vendor and the waivers on
2026-10-21 (`owner:2026-10-21#attested-vendor-and-waivers`); the wording below
follows the "Confidential compute (D05)" section of
[adepthood ADR 0009](https://github.com/Geoffe-Ga/adepthood/blob/main/docs/adr/0009-privacy-custody-and-inference-architecture.md). Three conditions are waived and no others: (c) and
(e), which O5 cannot meet by construction, and (f), whose evidence is the
vendor's own. Each waiver is cited with its substitute, because the substitute
is the reason the waiver is defensible.

| Condition | Verdict | Owner gate, if open |
|---|---|---|
| (a) Real hardware | kept | — |
| (b) A vendor attestation chain, not an operator-held key | kept — so ADR-0006's trust-root caveat does not apply | — |
| (c) Nonce freshness | unmet-and-replaced by the vendor's binding of the attested measurement to the live session key; **waived** by the owner on 2026-10-21 | None open. The substitute is load-bearing: the Creek build issue verifies it against the vendor's design, and T18 exercises it |
| (d) User-authorized key release | not applicable — no key is released; the vault sends plaintext it already holds under ADR-0014 | — |
| (e) A hostile-host lab | unmet-and-replaced by vendor negative tests (T18), each of which must refuse with nothing sent; **waived** by the owner on 2026-10-21 | None open. The substitute is load-bearing on the same terms as (c)'s |
| (f) The attested image is open-source and reproducibly built, and the pinned measurement is rebuilt from source before each re-pin | **waived** by the owner on 2026-10-21, accepting vendor-asserted evidence in place of independent verification; still **unverified — vendor-asserted** | None open. The waiver is granted for Privatemode and travels to no other vendor. ADR-0016 states it has **not** verified (f) |
| (g) Written retention terms and rate limits reviewed | unverified here — reviewed under adepthood B11, and not yet captured in the table below | Capture Privatemode's terms in the table below |
| (h) Jurisdiction chosen explicitly | kept — chosen on 2026-10-21: Edgeless Systems GmbH, Bochum, Germany, so the enclave and the party running it sit in the EU | — |

### Condition (f), stated plainly

The exclusion of the vendor and its host from the set of parties that can read
a prompt **rests entirely on condition (f)**, and (f) is unverified. ADR-0016
Decision 3.3 and Decision 3.5 both say so: this record has not verified, for
either candidate, that the attested image is open-source and reproducibly
built, nor that anyone rebuilds the pinned measurement from source before a
re-pin.

On 2026-10-21 the owner **waived** (f) rather than hold the decision for
evidence about another party's build pipeline: vendor-asserted evidence is
accepted in place of independent verification, for Privatemode only. A waiver
of independent verification is not a claim that verification happened, so the
rules below stand unchanged.

Until an independent rebuild is recorded in the pin table above:

- the Options-table cell for who can read under O5 reads **"vendor-asserted"**,
  and so does this runbook;
- no "operator-blind" claim is made anywhere, on any surface, and none would be
  supportable;
- the per-use label stays at the wording in section 5.

### (g) retention and rate limits, and (h) jurisdiction

These are recorded **at vendor selection**, which happened on 2026-10-21.
ADR-0016 Decision 3.3 records, for its two candidates, the shape of what must
be captured:
documented retention of request *metadata* (addresses, timestamps, API key,
request path, method, status, latency) for a bounded period, per-key token
usage kept permanently for billing, and a named operating company in a named
jurisdiction. That is metadata and it is disclosed; it is not journal content,
and it is not nothing. Capture, for Privatemode:

| Item | Value |
|---|---|
| Retention of request metadata, and for how long | `<to be recorded>` |
| Usage records kept for billing, and for how long | `<to be recorded>` |
| Documented cache isolation per key (T5's vendor-side check) | `<to be recorded>` |
| Rate limits, and the error shape when one is hit | `<to be recorded>` |
| Operating company and jurisdiction | Edgeless Systems GmbH, Bochum, Germany (owner decision 2026-10-21) |
| Date the terms were read, and by whom | `<to be recorded>` |
| Added to adepthood's recipient list (B11) | `<to be recorded>` |

A vendor **without per-key caps fails Decision 3.4** and is not selectable; the
owner's 2026-10-20 comment makes that binding at selection.

## 4. Key custody and rotation

Two secrets exist under O5. Neither may ever appear in a log, an error body, a
job record, a commit or this page.

**The organization admin key** creates, updates and deletes per-activation
vendor keys. It is held by the control plane as a declared secret file with its
own alert kinds, and re-issued **only on compromise** (create-then-delete, a
per-activation Machine update). *Built* ([#1930](https://github.com/Geoffe-Ga/Creek-Vault/issues/1930)):
`creek_mcp.provisioning.pool_admin_key` owns its custody. `load_pool_admin_key`
is the one way to the value and raises rather than returning a default, so a
missing, malformed, undated or expired key refuses instead of falling through
to an unauthenticated path. Three alert kinds are registered, each distinct
from `pool_credential_orphan` (which is about a per-activation key, not this
one): `pool_admin_key_unusable`, `pool_admin_key_rejected` and
`pool_admin_key_near_expiry`.

**Declared now, enforced on use.** The rendered Fly pilot config declares
which regime it is in and defaults to **not in use**: `CREEK_POOL_ADMIN_KEY_IN_USE = "0"`, no
`CREEK_POOL_ADMIN_KEY_B64` secret, nothing for the bootstrap to normalize and
nothing that can refuse. Rendering with `--pool-admin-key-expires-at <instant>`
switches the config into the in-use regime, which appends the
`CREEK_POOL_ADMIN_KEY_B64` `[[files]]` entry at
`/run/secrets/creek_pool_admin_key` — the root bootstrap then normalizes it to
mode 0400 — and makes every refusal below binding. The environment-variable
form of the secret is refused in **both** regimes.

The default matters. A `[[files]]` entry makes its Fly secret mandatory at
deploy time, so declaring the admin key unconditionally would mean an operator
had to invent a placeholder credential to boot a pilot that consumes no admin
key — and a placeholder that satisfied the format check would make
`pool-admin-key-ready` meaningless. The custody path is therefore in place
*before* the first key exists, which is what was asked for, without making
today's pilot undeployable.

**The pilot key, created 2026-10-27.** The owner created the Privatemode
account and one pilot key, capped at €10/month and expiring after one year.
Privatemode keys are created and managed only in its web portal, so every human
step of the key's life is the owner's: Geoff holds the key and does its
creation, rotation, revocation and cleanup. Agent work never holds the value.

**Why the control plane keeps the value in full, not a hash or a handle:** the
vendor's Admin API identifies a key *by its own value*, so the `update` that
resets a cap, the `update` that extends an expiry and the `delete` that revokes
on deletion each present the full key value. A digest cannot be presented, and
the API offers no key id or prefix a caller may substitute. That is also why
this credential is more dangerous than a per-activation one: it carries no cap
and no activation binding, so it can re-cap, mint or delete every vault's key.

**Custody verdicts** (`pool_admin_key.PoolAdminKeyStatus`, content-free codes
only — never the value, its length or a digest). The "Raised by" column is not
decoration: only some of these are reachable at startup, and a table that
implied otherwise would be the same kind of false claim #1930 was filed over.

| Verdict | Exit code | Raised by | Means |
|---|---|---|---|
| `pool-admin-key-ready` | 0 | Startup, `load_pool_admin_key` | In use, present, owner-only, well-formed, in date |
| `pool-admin-key-not-in-use` | 0 | Startup, `load_pool_admin_key` | The config declares no admin key, or declares nothing at all. Today's default. Not a refusal at startup; the loader still refuses to hand over a value |
| `pool-admin-key-near-expiry` | 0 | Startup, `load_pool_admin_key` | Usable, inside 15 days of the declared expiry. Not a refusal: the key still works |
| `pool-admin-key-missing` | 40 | Startup (narrow), `load_pool_admin_key` | In use and no secret file. At startup `prepare_runtime_paths` normally refuses first with the generic exit `2`, because it lists the file before custody is verified; this code is what remains when the file goes missing after that |
| `pool-admin-key-malformed` | 41 | Startup, `load_pool_admin_key` | Not a bearer credential, or not owner-only |
| `pool-admin-key-rejected` | 42 | **Runtime only** | The vendor answered 401/403; the body is never read. Never raised at startup, which makes no vendor call |
| `pool-admin-key-undeclared` | 43 | Startup, `load_pool_admin_key` | A **garbled** `CREEK_POOL_ADMIN_KEY_IN_USE` (anything but `0` or `1`), or a missing, unparseable or timezone-naive `CREEK_POOL_ADMIN_KEY_EXPIRES_AT` while in use. An **absent** in-use flag is *not* this — it reads as `not-in-use` and proceeds |
| `pool-admin-key-expired` | 44 | Startup, `load_pool_admin_key` | The declared expiry has passed |

Codes start at 40 so none can be confused with `--check pool`'s `30`
(`pool-unconfigured`), which means "no pool at all" rather than "a pool with no
usable admin key". Every verdict that lets the control plane run is `0`: a
working key must never stop it, so the signal for those is the printed status
string, not the exit code. The expiry is **declared** by the operator, not
discovered: no vendor call is authorized, so the key's own expiry is recorded
in the Machine's non-secret `[env]` beside the secret. An undeclared expiry
refuses, because a key nobody dated expires unnoticed.

**Alerting is registered, not yet emitted.** The three alert kinds exist in the
closed `AlertKind` vocabulary, and each verdict maps onto one, but **no
production path emits them**: a near-expiry verdict today is *recorded* on
stderr at startup and **not delivered** to anyone. Two gates stand in the way,
and neither is this page's to clear. The periodic re-check belongs to the
attesting provider's fleet-report pass
([#1896](https://github.com/Geoffe-Ga/Creek-Vault/issues/1896)) — a control
plane boots once and runs for months, so a startup-only check cannot warn
about an expiry that arrives later. And adepthood's alert receiver enforces its
*own* closed kind list, which today admits six kinds and not even
`pool_credential_orphan`; an unknown kind is answered `400`, which
`HttpFleetAlertSink` escalates to `AlertDeliveryError`, which fails the
scheduler component and takes the Machine unhealthy. Wiring an emitter before
that list is widened would convert a warning into an outage, so near-expiry is
**not** an early warning yet, and this page does not pretend it is.

*Also not built here:* the vendor calls themselves — the `update` that resets
a cap, the in-place expiry extension and the create-then-delete re-issue — which
are #1896's and cannot be written before the vendor adapter exists. This page
records custody, not lifecycle.

**One vendor key per activation**, kept in full in the encrypted bundle because
the vendor's update and delete calls require the value
(`PoolCredentialKind.VENDOR_KEY`, installed at `/run/secrets/creek_pool_key`).
It is an unbound bearer: copied out of a Machine config it works from anywhere
until its cap is spent, which is why the cap — not the network — is the brake.

| Lifecycle step | Rule | Where it lives |
|---|---|---|
| Size the caps | `tokens spent so far + monthly cap × measured tokens per reflection × 2`, independently per direction | `size_pool_caps` / `pool_cap_tokens` |
| Which measurement may price a cap | Only provider-reported counts. A `byte_upper_bound` is a bound on bytes; `unreported` and `not_measured` are absences. Each refuses with `ValueError` | `measured_reflection_tokens` |
| Reset | By the vendor's update call, on adepthood's calendar month | Not built. #1900 merged carrying `expires_at` in the bundle, not the vendor calls that reset or extend it |
| Expiry | Extended **in place** every 15 days for 30, so one missed cycle is survivable | `PoolCredential.expires_at` holds the date; nothing extends it yet (see Reset) |
| Re-issue | Create-then-delete, only on compromise | Not built (see Reset) |
| Revoke | At deletion, confirmed **before** the content-free receipt is issued; the issuer raises rather than returns on an unconfirmed revocation | `PoolBundleIssuer.revoke` |
| Reconcile | A surviving credential is an orphan in the fleet report (`pool_credential_orphan`) | `AlertKind` |

An exhausted cap answers `insufficient_quota` (429, non-transient). The
contract maps it to `temporarily_unavailable` so the vault refuses rather than
spending on, because retrying cannot clear it — *designed, not built:* the
mapping lives in the attesting provider
([#1896](https://github.com/Geoffe-Ga/Creek-Vault/issues/1896), still open).
**The vault's own pool path does not read the 429 body at all:**
`_pool_status_error` is documented "no body is read" and collapses every 429 to
`ProviderRateLimitError`, so it cannot tell a spent cap from admission
pressure and the `temporarily_unavailable` mapping genuinely does not exist in
production. The **bench** client classifies the two codes apart
([#1892](https://github.com/Geoffe-Ga/Creek-Vault/issues/1892)):
`BenchPoolClient._refuse_admission` raises `PoolQuotaExhaustedError` on
`insufficient_quota`, which is a member of the bench's closed `Outcome`
vocabulary. That is measurement, not the refusal contract. A control plane that is down when a key
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
| The scaling policy refuses a minimum of zero | There is no Creek-run engine host to scale. Residency is the vendor's property, asserted in its documentation and bounded by the contract's "a vendor outage is a refusal, never a fallback". The O3 policy that refuses a minimum of zero is dormant (section 7) | **Not met, and not implementable vault-side.** The vendor is chosen; what replaces it is the documented-residency row below plus the measured equivalence |
| The first reflection after idle shows no load or download event | The attested channel gives the vault no engine-level telemetry, and no catalogue in ADR-0016's sources documents a load or download event, so unless the chosen vendor publishes one this clause has no observable signal under O5. The substitute is weaker and is recorded as such: latency equivalence below, plus the vendor's written residency terms | **Not met.** Needs Privatemode's written residency terms and the live run ([#1899](https://github.com/Geoffe-Ga/Creek-Vault/issues/1899)) |
| Its p95 equals the warm p95 within measurement noise | The harness's `idle` sweep (`--idle-cycles`, `--idle-seconds`) reports a `resume` phase in `per_sweep.idle.per_phase` — only `resume`, since `_idle` runs no warm trial — and the warm p95 comes from the `cold_warm` sweep, at `per_sweep.cold_warm.per_phase.warm.latency.p95_s`; the two are compared | **Not met, but no longer blocked on the harness.** Pool mode has landed ([#1892](https://github.com/Geoffe-Ga/Creek-Vault/issues/1892), closed): `--pool-url`, `--pool-identity` and `--pool-vendor` drive the real pool client through the same sweeps, hermetic against an in-process fake unless the run is live. What remains is the live run itself ([#1899](https://github.com/Geoffe-Ga/Creek-Vault/issues/1899)); its budget is authorized and the owner created the pilot key on 2026-10-27 |
| The fleet report raises `pool_min_below_one` | Hermetic: the alert kind is in the closed `AlertKind` vocabulary and the report raises it when a warm minimum reads below one | **Not met:** `pool_min_below_one` is **not registered** in `AlertKind`, which registers the pool credential orphan and the admin-key kinds of [#1930](https://github.com/Geoffe-Ga/Creek-Vault/issues/1930) but no warm-minimum kind. [#1900](https://github.com/Geoffe-Ga/Creek-Vault/issues/1900) was expected to register it and merged without doing so, so no open issue owns it today. `tests/test_pool_runbook_docs.py` fails this page the moment it lands, so this row cannot go stale |

### The procedure, for when those gates clear

Run this only under the bounded pilot budget the owner authorized on
[#1899](https://github.com/Geoffe-Ga/Creek-Vault/issues/1899), with the key the
owner holds. Then, from a vault-shaped allocation:

1. Confirm the pin: `python -m creek_mcp.container_health --check pool`
   exits 0. (There is no `creek-container-health` console script; the name is
   only the parser's `prog`.)
2. Run the harness in pool mode with both the `cold_warm` and `idle` sweeps in
   the same run, so the two phases share one harness and one endpoint, and
   give at least one idle cycle an `--idle-seconds` exceeding the vendor's
   documented idle behaviour. There is no separate "warm sweep" flag: the warm
   phase is produced by `cold_warm`.
3. Read `per_sweep.idle.per_phase.resume.latency.p95_s` against
   `per_sweep.cold_warm.per_phase.warm.latency.p95_s`. The field is `p95_s`,
   not `p95`. Equal within measurement noise is the evidence; a `resume` p95
   materially above `warm` is a cold start by another name, and T19 fails.
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
