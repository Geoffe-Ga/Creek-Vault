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

## Fleet reconciliation, telemetry, and budget alarms (#1769)

ADR-0013 Decisions 4, 6, and 7 are operated from a third process,
`creek-provisioning-fleet`. It shares the SQLite file with the API and worker,
holds a Fly driver whose secret manager refuses to issue or revoke, and can
therefore inventory and stop Machines but never provision or delete. Nothing
here changes the `/control/v1` job contract: stuck, orphan, and budget
conditions are operator-side classifications, not job states.

```console
creek-provisioning-fleet report \
  --database ./provisioning-state/jobs.sqlite3 \
  --policy-file ./provisioning-state/fleet-policy.toml \
  --fly-token-file ./run-secrets/fly_token \
  --fly-organization creek-vaults \
  --fly-image registry.example/creek@sha256:<digest> \
  --fly-token-expires-at 2026-12-31T00:00:00+00:00 \
  --inventory-file ./provisioning-state/apps.json
```

`report` observes and exits `0` when clean, `3` when any alert is present, and
`1` when the provider inventory could not be read (it never prints a "clean"
report in that case). `reconcile` takes the same arguments and additionally
performs the only two repairs the tool knows: stopping a live allocation's
Machine that has run continuously past `max_continuous_running_seconds`, and
requeueing a retryable failed delete. Stopping an overrunning Machine
interrupts background work, so run `reconcile` on a schedule you accept for
that, and `report` everywhere else. Both commands print one JSON document with
the keys `observed_at`, `telemetry`, `divergences`, `alerts`, `estimate`,
`review_triggers`, and `inventory_mode`, and log each alert as
`fleet alert kind=<kind> subject=<id>` with nothing else on the line.

The driver inspects only apps it can derive from live and pending-deletion
allocations plus the injected inventory file; no org-wide listing endpoint is
used. Confirmed-deleted jobs are never re-inspected, so provider traffic is
bounded by the live fleet, not by history. To catch apps the store has
forgotten, pass the output of `fly apps list --json` as `--inventory-file`
(a JSON list of names or of objects with `name`/`Name`); names outside the
configured app prefix are ignored by the driver. An org-wide discovery call
can be added once its endpoint is verified against a fake route.

### Policy file

Every rate, budget, duration, and review threshold is operator configuration
read from `--policy-file`. The file is parsed with `parse_float=Decimal`, so
`0.15` is exactly `0.15`. The values below are reference assumptions from
ADR-0013 Decision 4, not defaults: the tool has no defaults and refuses to
start without every key. They are injected assumptions for billing tests and
operations, never as business logic.

```toml
[budget]            # reference assumptions from ADR-0013 Decision 4, not defaults
currency = "USD"
monthly_budget = 500.00
volume_gb_month_rate = 0.15
stopped_rootfs_gb_month_rate = 0.15
running_hour_rate = 0.0082
# snapshot_gb_month_rate = 0.02   omit -> the component is reported as unpriced
# egress_gb_rate = 0.02

[policy]
max_continuous_running_seconds = 14400
stuck_deletion_seconds = 3600

[review]            # ADR-0013 Decision 7 thresholds, supplied by the operator
activated_vaults = 500
provisioned_volumes = 1000
months_over_budget = 3

[usage]             # optional, copied from the provider invoice each month
# snapshot_bytes = 0
# egress_bytes = 0
# running_seconds = 0
```

### Alerts

| kind | meaning | response |
|------|---------|----------|
| `duplicate_resource` | a live allocation owns more than one Machine or live volume | inspect the app; the worker's create path refuses duplicates, so this is a provider-side leftover |
| `orphan_resource` | a resource exists under an app the store does not want (no job, or the job is confirmed deleted) | confirm on the provider console, then delete it there; the tool never deletes |
| `stuck_deletion` | a deletion has been unconfirmed for at least `stuck_deletion_seconds` | `reconcile` requeues a retryable failure; a non-retryable one needs the provider console |
| `continuous_running` | a Machine has been observed running for at least `max_continuous_running_seconds` | `reconcile` stops it only when the allocation is live (this interrupts background work); an orphan or deleting Machine is reported only - stop it on the provider console; `report` only reports |
| `monthly_budget_departure` | the month estimate is greater than or equal to `monthly_budget` (equal fires) | reconcile the invoice below and revisit the budget or the fleet |

`missing_resource` and `unconfirmed_deletion` appear under `divergences` but
raise no alert: the first is expected during a partial create and the second
is the normal window between a delete request and provider confirmation.

### Invoice reconciliation

Each telemetry field maps to one invoice line. `sources` in the report says
where every value came from: `store` (sampled or counted by the control
plane), `provider` (read from the provider API this pass), `injected` (copied
from the invoice into `[usage]`), or `unavailable` (`null`, never `0`).

| telemetry field | invoice line | source |
|-----------------|--------------|--------|
| `provisioned_volumes`, `volume_bytes` | persistent volume GB-months | provider |
| `stopped_rootfs_gb` | stopped root filesystem GB-months | provider |
| `running_machine_seconds_fleet` (and per allocation) | Machine compute hours | store (sampled between consecutive running observations) |
| `running_seconds_injected` | Machine compute hours | injected; reported beside the sampled figure, never instead of it |
| `snapshot_bytes` | volume snapshot storage | injected or unavailable |
| `egress_bytes` | outbound data transfer | injected or unavailable |
| `machines_without_rootfs_size` | Machines whose root filesystem size the provider did not report; when non-zero, `stopped_rootfs` is also listed under `unpriced` | provider |
| `activated_allocations`, `allocations_by_state` | number of billable allocations | store |
| `duplicate_allocation_attempts` | (none; duplicate attempts the store absorbed) | store |
| `orphan_resources`, `unconfirmed_deletions`, `oldest_unconfirmed_deletion_seconds` | resources that may still be billed | provider / store |

The estimate uses injected running seconds when present; otherwise it uses the
sampled month-to-date figure, projected to the calendar month only once at
least one day of the month has elapsed (`running_basis` says which). Storage
components are full-month rates. Unpriced or unknown inputs are listed under
`unpriced` rather than silently counted as zero; a Machine whose root
filesystem size the provider omitted adds `stopped_rootfs` to that list.

Monthly walkthrough: when the invoice for a closed month arrives, copy its
snapshot, egress, and running figures into `[usage]`, then run
`report --record-month YYYY-MM` naming that closed month. The tool refuses a
month that has not ended, computes the month's compute from its own stored
running buckets (or the injected `[usage]` running seconds) without any
projection, and writes the result to `provisioning_budget_months`; compare
`estimate.estimated_month` and its `components` with the invoice lines.
Record every month: the three-rolling-months review trigger reads only
calendar-consecutive recorded months ending at the latest one, so a gap ends
the window rather than being skipped.

### Deletion receipts

Every path that moves a job into `deleting` (a consumer delete, an expired key
ceremony) opens a content-free receipt in `provisioning_deletion_receipts`.
The receipt is confirmed - with provider, provider allocation id, and the
resource classes removed (credential, machine, volume, app) - inside the same
write fence that marks the job `deleted`; a retryable failed delete bumps its
`attempts`, and only a non-retryable failure marks it `failed`. The receipt
carries the requester and consumer subjects the store already holds and never
a vault URL, credential, provider token, or ceremony material. Databases
upgraded from schema v3 receive a receipt flagged `backfilled` for any
deletion already in flight.

### Emergency fleet stop

```console
creek-provisioning-fleet emergency-stop --database ... --policy-file ... \
  --fly-token-file ... --fly-organization ... --fly-image ... --fly-token-expires-at ...
```

The emergency fleet stop calls `FlyProviderDriver.stop` for every allocation
the store still owns, which stops the Machine without detaching its volume. It
destroys no volume, Machine, or app, issues no `DELETE`, continues past a
per-allocation provider failure, prints a per-allocation outcome list, and
exits `1` if any stop failed. Stopping interrupts background work in progress;
the durable worker resumes it on the next claim.

### Review checkpoint (ADR-0013 Decision 7)

Decision 7 requires reviewing the one-app/one-Machine driver at 500 activated
vaults, 1,000 provisioned volumes, a material change in confidential-compute
availability, or three rolling months above the approved fleet budget,
whichever comes first. The report surfaces these as `review_triggers`:
`activated_vaults` and `provisioned_volumes` compare telemetry with the
`[review]` thresholds, `months_over_budget` reads the durable
`--record-month` history, and `confidential_compute_change` is raised by
passing `--confidential-compute-changed` when the operator judges that a
material change has happened. The thresholds are policy values; the ADR
numbers above are the example, not defaults.

## State and retry rules

- `pending` is claimable create work.
- a create lease moves it to `provisioning`;
- a successful fake/real driver result moves it to `awaiting_key_ceremony`;
- a valid ciphertext-only ceremony marks the job `ready` and reports whether
  attested confidential processing was actually verified;
- stable failures become `failed` and are retryable only when explicitly
  recorded as safe;
- delete changes any live state to `deleting` and opens a pending deletion
  receipt; only provider confirmation moves the job to `deleted` and confirms
  the receipt.

Activation ids remain durable aliases. Repeating one returns the same job;
distinct concurrent ids for the same requester/consumer-subject pair resolve to
its one live job, so two API processes cannot create two billable allocations.
Each requester/subject pair may retain at most 256 activation aliases. Existing
aliases remain idempotent after the limit is reached; an additional distinct
alias is rejected with `409`.
