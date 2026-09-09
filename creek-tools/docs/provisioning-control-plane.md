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

Two figures deserve a caveat before anyone reconciles them against an invoice.

*Machine seconds are instantaneous and a lower bound.* The only clock the Fly
Machines API offers is `updated_at`, which any provider-side write resets, so a
Machine that has run for a week but was touched an hour ago reads as an hour.
The figure is therefore always `estimated`, including when it is 0, and it
describes this instant — it is **not** a billing-period total. An accumulated
figure would need persisted samples and a sampler process, which is deliberately
not in this slice. Running Machines whose clock could not be read at all are
named in `unmetered_running` rather than folded into the number.

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
| `running_machine_hour` | `$0.0082` for shared-cpu-1x |
| `hours_per_month` | `720` |
| `egress_gb`, `snapshot_gb_month` | not published by the ADR; operator-supplied |

At a 720-hour month those inputs reproduce four of the ADR's five published
per-user figures exactly: `$0.90` fully stopped, `$0.94` at ten running minutes
a day, `$1.14` at one running hour a day and `$1.86` at four running hours a day.

**The ADR's fifth figure is arithmetically unreachable.** Continuously started
comes to `$6.6540` at 720 hours and `$6.7360` at 730; the published `$6.67`
implies 721.95 hours. The billing test pins `$6.65` and does **not** tune
`hours_per_month` to make `$6.67` pass — doing so would smuggle a hard-coded
business assumption into the one number nobody checks, which is precisely what
Decision 4 forbids. Treat `$6.67` in ADR-0013 as an error pending correction.

Nothing in the telemetry module compares a meter to `monthly_budget`. The budget
is configuration this slice records so that alarms, when they land, evaluate an
operator-set threshold rather than a number baked into the control plane.
