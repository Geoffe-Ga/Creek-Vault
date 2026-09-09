# Provisioning control plane

ADR-0013 separates a small authenticated control plane from every single-vault
runtime. The API commits idempotency and queue state to SQLite and returns a job
handle immediately; provider work never runs in the API process.

## Serve the API

Create a durable database location and a mounted consumer registry. Bearer
values below are placeholders, never production credentials.

```console
mkdir -p ./provisioning-state ./run-secrets
printf 'adepthood=<strong-mounted-token>\n' > ./run-secrets/consumer_tokens
chmod 0400 ./run-secrets/consumer_tokens
creek-provisioning-api \
  --database ./provisioning-state/jobs.sqlite3 \
  --consumer-tokens-file ./run-secrets/consumer_tokens \
  --host 127.0.0.1 --port 8830
```

A routable bind requires `--tls-cert` and `--tls-key`. Back up the SQLite file
with its filesystem's atomic snapshot mechanism; it contains job and provider
allocation identifiers, but no plaintext provider token, consumer credential,
recovery material, key material, or corpus content.

The checked-in contract is
[`contracts/provisioning-v1/openapi.json`](contracts/provisioning-v1/openapi.json).
Clients submit `{activation_id, consumer_identity}`, where `consumer_identity`
is a stable opaque account subject in the bearer-authenticated requester's
namespace. The single mounted `adepthood` service bearer can therefore own one
isolated allocation per activated Adepthood user. Job ownership always comes
from the bearer, never from this body field. Clients poll `status_url`, retry
only a `failed` job whose `retryable` flag is true, and request deletion through
the same job URL.

## Worker composition

`ProvisioningWorker` claims SQLite rows under expiring leases. A process crash
leaves the row durable; after the lease expires, another process reclaims the
same job id. Provider adapters receive the full durable job so provider
resources can be reconciled by the canonical activation id even if a process
crashed before persisting its provider result. Stable failures are recorded as
`FailureReason` values, and raw exception detail is discarded before
persistence or logging.

The worker takes two injected boundaries:

- `ProviderDriver`, which creates/deletes provider resources;
- `OneTimeCredentialHandoff`, which delivers an identical result at most once
  to the authenticated consumer backend and retains no plaintext in the test
  implementation.

`FakeProviderDriver` and `FakeOneTimeHandoff` prove the contract under tests.
They are not deployment drivers. `FlyProviderDriver` is the first deployment
adapter and implements ADR-0013 Decision 3.

## Fly Machines driver (#1770)

Compose `FlyProviderDriver` only in the separate worker process. The API
process remains provider-free. Its `FlyCredential` must be a short-lived,
org-scoped deploy token for a dedicated Creek organization, never a personal
access token. A controller needs organization scope because it creates one app
per activation; after app creation, narrower app-scoped operations may be added
without broadening this boundary. Supply the token through an owner-only regular
file and construct it with `FlyCredential.from_file`; do not put it in an
environment value, argument, application log, exception, database, or Machine
configuration.

`FlyProviderPolicy` requires an immutable image digest. Its reference defaults
are `iad`, one shared CPU, 1 GB RAM, a 1 GB root filesystem, and a 5 GB encrypted
volume. They are ordinary configuration values and can be changed by the
deployment. The driver creates a custom private network and deliberately calls
no IP-allocation endpoint, so there is no dedicated IPv4. The resulting
Machine starts stopped, has `min_machines_running=0`, uses proxy autostop only
as a backstop, and mounts the same encrypted volume at `/vault` after every
stop/start cycle.

The injected `FlySecretManager` owns idempotent issuance and revocation of the
single-consumer registry plus per-Machine TLS material. Its values are installed
through the Fly Machines file contract, never environment variables, and every
secret-bearing dataclass excludes its contents from `repr`. The secret manager
must return the identical bundle when a create is replayed and make repeated
revocation a no-op.

Every provider name is derived from a SHA-256 activation digest. Machine
metadata also carries the allocation and activation ids. Provisioning lists and
adopts those resources before creating anything, so a partial create that left
an app or volume is resumed rather than duplicated. Deletion revokes the
consumer credential first, then stops and destroys the Machine, destroys the
volume, removes the app, and verifies absence. A partial delete stays retryable
and visible to the durable queue until reconciliation proves that no Machine or
volume remains.

Authenticated request handling may call `start()` and return without scheduling
a stop. Durable background execution calls `run_background_job()`, whose fixed
sequence is drain, commit, close, and stop. Both success and failure close the
vault and explicitly stop the Machine; HTTP-request lifetime is never the
shutdown signal.

Successful create work stops at `awaiting_key_ceremony` and atomically creates
a 24-hour challenge. The versioned
[`key ceremony protocol`](contracts/provisioning-v1/key-ceremony.md) (issue
#1771) accepts only an activation-bound ciphertext artifact. Passphrase,
recovery value/code, and unwrapped volume key never reach this service. An
identical completion retry is idempotent; a conflicting replay is refused.
Expired incomplete ceremonies move to `deleting`, and every worker pass sweeps
them before claiming work, so an abandoned activation reconciles provider
resources to zero.

The completed job advertises `attested_confidential`. An ordinary Fly Machine
does not advertise INTIMATE: it completes as `false`. `true` requires a fresh,
correctly measured, trust-root-signed recipient statement and delivery of an
opaque key envelope to the injected idempotent release sink. Attestation expiry,
signature failure, measurement mismatch, challenge mismatch, or recipient
mismatch fails before release.

## State and retry rules

- `pending` is claimable create work.
- a create lease moves it to `provisioning`;
- a successful fake/real driver result moves it to `awaiting_key_ceremony`;
- a valid ciphertext-only ceremony marks the job `ready` and reports whether
  attested confidential processing was actually verified;
- stable failures become `failed` and are retryable only when explicitly
  recorded as safe;
- delete changes any live state to `deleting`, and only provider confirmation
  produces the durable `deleted` receipt.

Activation ids remain durable aliases. Repeating one returns the same job;
distinct concurrent ids for the same requester/consumer-subject pair resolve to
its one live job, so two API processes cannot create two billable allocations.
Each requester/subject pair may retain at most 256 activation aliases. Existing
aliases remain idempotent after the limit is reached; an additional distinct
alias is rejected with `409`.

## Fleet telemetry and the reference cost model

`creek_mcp.provisioning.telemetry.FleetTelemetry` is a library surface, not an
endpoint. `/control/v1` carries exactly four consumer paths and one bearer, so a
fleet-wide aggregate served there would be cross-consumer data; telemetry is for
the operator who pays the provider invoice and is composed in operator tooling
instead. One `observe()` call reads the provider once, writes nothing durable,
mutates nothing, and returns a `FleetTelemetrySnapshot` in which every figure is
a `Meter(value, quality)`.

`Meter` cannot be constructed with a value and a quality that disagree: a figure
is present if and only if its quality is not `unavailable`. That makes the one
failure mode worth naming impossible rather than merely discouraged — a meter
nobody could read must never look like a meter read and found compliant. The
combinator behind every provider-sourced figure applies five arms:

| Situation | Reported |
|---|---|
| contributors exist, none readable | `unavailable` (no value) |
| nothing observed and enumeration incomplete | `unavailable` (no value) |
| some readable, or enumeration incomplete | `estimated` **lower bound** |
| all readable and enumeration complete | `exact` |
| zero contributors and enumeration complete | `exact` 0 — the only legitimate zero |

Four caveats before anyone reconciles these against an invoice.

*`exact` is scoped, not absolute.* The enumeration lists apps named
`<app_prefix>-*` inside one organization, so `exact` means "exact over that
scope", never "exact over what Fly bills the account". A Creek-owned resource in
another organization, or under an app whose name lost the prefix, is outside
every meter and no quality says so.

*Stopped rootfs capacity is never better than `estimated`.* `config.rootfs.size_gb`
is Creek's own provisioning request echoed back by the Machines listing — not a
documented Fly response field and not Fly's statement of what it is billing.
Reporting it as `exact` would claim a confirmation the control plane never got.

*Capacity and duration are both fleet-wide; `unmetered_running` is not.* Every
meter counts orphaned resources, because the operator is billed for them — an
orphan burning CPU is the most expensive thing fleet reconciliation exists to
find. `unmetered_running` comes verbatim from the reconciler and names live
allocations only; `unclassified_machines` names Machines whose state is in
neither the known-running nor the known-stopped set, which contribute an
unreadable value to both meters rather than being assumed to be either.

*A pass is not one transaction.* It issues three read-only store queries on
three connections alongside one provider enumeration, so fields fed by
different queries can describe instants milliseconds apart. Each query runs
exactly once, which is what stops two fields derived from the *same* query
disagreeing inside one snapshot.

Two figures deserve a further caveat.

*Machine seconds are instantaneous and a lower bound.* The only clock the Fly
Machines API offers is `updated_at`, which any provider-side write resets, so a
Machine that has run for a week but was touched an hour ago reads as an hour.
The figure is therefore always `estimated`, including when it is 0, and it
describes this instant — it is **not** a billing-period total. An accumulated
figure would need persisted samples and a sampler process, which is deliberately
not in this slice. A running Machine whose clock could not be read contributes
an unreadable value rather than a zero, so the meter degrades; if it belongs to
a live allocation the reconciler also names it in `unmetered_running`.

*Two different things are called "duplicate".* `duplicate_allocation_attempts`
counts activation aliases the durable store folded onto an existing job — a
lifetime, monotonic count of attempts that were *caught* before they could
create a second billable allocation, and a pure replay contributes nothing to
it. `duplicate_provider_resources` is the one that costs money: a second
Machine or volume actually billing under one allocation, counted off the
reconciler's own `DUPLICATE_ALLOCATION` divergences. Attempts the store
*refused* outright leave no durable trace at all and are reported as a
permanently unavailable meter rather than as a zero.

*Egress is unavailable.* Fly meters bytes out on its billing surface, not the
Machines API. `EgressMeter` is a separate injected boundary so an operator can
supply the figure from an invoice export; the shipped `UnavailableEgressMeter`
answers honestly rather than reporting zero.

### The reference cost model is a guardrail, not code

ADR-0013 Decision 4 names "operations documentation and billing tests" as the
only two homes for its reference prices, so they are recorded here and in
`tests/test_fleet_telemetry.py` — and nowhere in `creek/` or `creek_mcp/`.
`FleetPriceTable` has **no default on any field**: a cost cannot be produced
without an operator supplying every price, the monthly budget, the hours-per-month
basis and the GB-to-bytes conversion base.

Decision 4's published assumptions, from Fly.io prices dated 2026-09-06:

| Input | Value |
|---|---|
| `volume_gb_month` | `$0.15` (the ADR's `$0.75` for a 5 GB volume) |
| `stopped_rootfs_gb_month` | `$0.15` per GB, times the stopped fraction |
| `running_machine_hour` | `$0.00822` for shared-cpu-1x — see below |
| `hours_per_month` | `720` |
| `egress_gb`, `snapshot_gb_month` | not published by the ADR; operator-supplied |

Those inputs reproduce **all five** of the ADR's published per-user figures
exactly: `$0.90` fully stopped, `$0.94` at ten running minutes a day, `$1.14` at
one running hour a day, `$1.86` at four running hours a day and `$6.67`
continuously started (`0.75 + 720 × 0.00822 = 6.6684`).

Two things about those inputs are assumptions rather than derivations, and are
worth stating so nobody mistakes them for facts the ADR asserted.

*The hourly rate is supplied at more precision than the ADR displays.* The ADR
prints `$0.0082`; `0.00822` is a rate whose four-decimal display value is
exactly that, and it is what reproduces the published totals. If Fly's true
rate is some other value rounding to `$0.0082`, the continuous figure moves.

*The 720-hour month is an assumption the first four figures cannot test.* They
reproduce at 720, 730 and 744 alike. Only the continuous case distinguishes
them — `$6.67` at 720, `$6.75` at 730, `$6.87` at 744 — which is why 720 is the
value supplied.

Nothing in the telemetry module compares a meter to `monthly_budget`. The budget
is configuration that slice records so that alarms evaluate an operator-set
threshold rather than a number baked into the control plane. Alarms land in
`creek_mcp/provisioning/alerts.py`, and the rest of this section is what they do
with it.

### Alarms notify; they never remediate

`FleetAlarms.raise_alerts()` runs **one** telemetry pass and compares it to the
operator's thresholds. It is typed on `FleetTelemetry`, holds neither a
`ProvisioningStore` nor a `ProviderDriver`, and `AlertSink` declares exactly one
method — so a sink is never handed a repair capability and the whole pass puts
nothing but GETs on the wire. That is the same ruling `ReconcileMode` carries:
Decision 6 binds resource removal to revoking the consumer credential, and an
automatic repair path would destroy billable resources while leaving a live
credential issued.

Six codes, and every `DivergenceKind` maps to one of them:

| Code | What it observes |
|---|---|
| `duplicate_allocation` | a second billable Machine or volume under one allocation |
| `orphan_resource` | a provider resource no live allocation accounts for |
| `missing_resource` | a live allocation whose app the provider no longer lists |
| `stuck_deletion` | a deletion the provider has not confirmed within the window |
| `continuous_running` | a Machine billing for CPU past `max_continuous_running` |
| `budget_departure` | an injected billing period costing more than `monthly_budget` |

`missing_resource` exists rather than being ruled unalarmed because it is a
customer's vault gone while the store still bills for it. Totality is asserted
(`set(_DIVERGENCE_ALERTS) == set(DivergenceKind)`) and the lookup raises, so a
sixth divergence kind fails loudly instead of quietly escaping the alarm surface.

*An alarm that could not be evaluated never looks like one that found nothing.*
Every alert carries a `Meter`. `UNAVAILABLE` means **this threshold could not be
evaluated**; a threshold that *was* evaluated and found compliant raises no alert
at all, so the two are never confusable. Three cases are structural rather than
hypothetical: a running Machine whose provider clock is unreadable yields no
divergence and appears only in `unmetered_running`; a live Machine in a state
that is neither known-running nor known-stopped is in neither meter's set; and an
enumeration that did not complete makes `FleetReconciler._missing` suppress
itself wholesale. The first two raise a per-subject unevaluable
`continuous_running`; the third raises one **fleet-scoped** unevaluable alert
(`subject is None`) for each of the four inventory-derived codes.
`stuck_deletion` and `budget_departure` are outside that partition — their
evidence is the durable store and the injected billing boundary, and a failed
provider read says nothing about either.

*Dedupe is per-run and stateless.* One adopted orphan produces one divergence per
resource and exactly one `orphan_resource` alert carrying the contributor count,
taken from telemetry's own combinator so the alert and the meter cannot disagree.
Nothing durable is written — no table, no schema version, no suppression ledger —
because a durable ledger would break the equal-passes purity property the
reconciler and the telemetry pass both hold: the second pass over unchanged state
would emit nothing. **Cross-run suppression is the injected sink's contract**,
exactly as `FakeKeyReleaseSink` owns key-release idempotency; `observed_at` is
carried on every alert (excluded from equality) so a sink can implement a
time-boxed policy.

*The budget is injected, and it is fleet-scoped.* A billing period cannot be
derived from an instant, so it arrives through a third read-only Protocol,
`BillingPeriodSource`, shaped exactly like `EgressMeter`. The shipped
`UnavailableBillingPeriodSource` answers `UNAVAILABLE` honestly — the Fly
Machines API has no billing surface — and that produces an explicit
`budget_departure` alert rather than silence. Its subject is `None`: Decision 4
says "an operator-set monthly budget" and Decision 7 "the approved fleet budget",
and a typed `None` cannot collide with a `fly-<24 hex>` surrogate by construction.

Three limits, stated so nobody reads them as closed:

* A departure raised here is a **single-period** departure. It does **not**
  satisfy Decision 7's "three rolling months above the approved fleet budget"
  trigger, which needs persisted samples and therefore a schema change this
  slice deliberately does not take.
* A **running orphan past the policy window is a named non-alert**. It is
  covered by `orphan_resource`; `continuous_running` is out of scope for
  non-live subjects because `FleetReconciler._running` is live-fenced, and
  re-deriving the threshold in the alarm would duplicate one the reconciler
  owns. If that is ever to be closed the fix belongs in `FleetReconciler`.
* `missing_resource` inherits reconcile.py's **app granularity**: a volume that
  vanishes under a still-listed app is invisible to this alarm too.
