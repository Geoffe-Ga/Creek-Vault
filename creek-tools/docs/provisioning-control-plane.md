# Provisioning control plane

ADR-0013 separates a small authenticated control plane from every single-vault
runtime. The API commits idempotency and queue state to SQLite and returns a job
handle immediately; provider work never runs in the API process.

Provisioning contract 2.0 implements the honest ordinary-Fly custody boundary
ratified in
[ADR-0014](architecture/ADR/0014-provider-managed-custody-for-ordinary-fly.md).
Fly supplies storage encryption and restart-time unlock; Fly and a sufficiently
privileged Creek operator can read mounted bytes. The service requests no
passphrase or recovery key, makes no no-escrow claim, and does not admit
INTIMATE content. An ordinary Fly Machine does not advertise INTIMATE.

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
  --maximum-live-allocations 5 \
  --host 127.0.0.1 --port 8830
```

A routable bind requires `--tls-cert` and `--tls-key`. Back up the SQLite file
with its filesystem's atomic snapshot mechanism; it contains job and provider
allocation identifiers, but no plaintext provider token, consumer credential,
recovery material, key material, or corpus content.

The production CLI defaults to a five-live-allocation admission cap. The count
includes every non-deleted durable job, so pending or deleting work reserves
capacity before a provider call. The count and new job/alias insert share one
SQLite write transaction; concurrent API processes cannot cross the cap. Start
the API with `--disable-new-activations` to refuse every new activation id with
`503 activation_unavailable`. An exact existing id remains replayable, and
status, routing, retry, export, and deletion do not depend on create admission.
The refusal writes nothing and the provider-free API makes no Fly request.

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

## Run the production worker (#1805)

`creek-provisioning-worker` is the only production composition that creates or
deletes Fly allocations. It binds `ProvisioningStore`, `FlyProviderDriver`,
`EncryptedFileFlySecretManager`, and `HttpOneTimeCredentialHandoff`; neither
test fake is reachable from the entry point. The required public routing URL is
the same stable origin handed to every allocation; readiness uses its separate
bounded Fly wait. The loop claims one leased job at a time. `SIGTERM` or
`SIGINT` interrupts an idle poll immediately, while an in-flight provider,
callback, or store boundary is allowed to reach its bounded timeout and settle
its lease before the process exits. An unclean process exit leaves the claim
durable and the next worker resumes it when the lease expires.

Create an owner-only secret-state directory and mount five owner-only regular
files. The master key is exactly 32 raw bytes. The CA certificate and private
key sign a distinct wildcard certificate for each Machine's private hostname;
the same CA certificate belongs in the routing service's trust store. The Fly
token must be a short-lived organization deploy token. The callback bearer is
rotated separately from Adepthood's control-plane bearer.

```console
install -d -m 0700 /var/lib/creek/runtime-secrets /run/creek-secrets
chmod 0400 /run/creek-secrets/fly-token \
  /run/creek-secrets/runtime-master-key \
  /run/creek-secrets/runtime-ca.crt \
  /run/creek-secrets/runtime-ca.key \
  /run/creek-secrets/adepthood-handoff-token

creek-provisioning-worker \
  --database /var/lib/creek/jobs.sqlite3 \
  --fly-token-file /run/creek-secrets/fly-token \
  --fly-organization creek-vaults \
  --fly-image registry.example/creek@sha256:<digest> \
  --fly-token-expires-at 2026-09-14T12:00:00+00:00 \
  --routing-public-url https://vault-router.example.com \
  --routing-readiness-timeout-seconds 20 \
  --secret-state-directory /var/lib/creek/runtime-secrets \
  --secret-master-key-file /run/creek-secrets/runtime-master-key \
  --tls-ca-certificate-file /run/creek-secrets/runtime-ca.crt \
  --tls-ca-private-key-file /run/creek-secrets/runtime-ca.key \
  --handoff-url https://adepthood.internal/internal/vault-provisioning/completions \
  --handoff-token-file /run/creek-secrets/adepthood-handoff-token
```

The non-secret options are `--database`, `--fly-organization`, `--fly-image`,
`--fly-token-expires-at`, `--routing-public-url`,
`--routing-readiness-timeout-seconds`, `--fly-api-base-url`, `--fly-region`,
`--fly-app-prefix`, `--fly-cpu-kind`, `--fly-cpus`, `--fly-memory-mb`,
`--fly-rootfs-size-gb`, `--fly-volume-size-gb`, `--vault-port`,
`--secret-state-directory`, `--handoff-url`, `--poll-interval-seconds`,
`--lease-seconds`, `--provider-timeout-seconds`,
`--callback-timeout-seconds`, and the diagnostic one-shot `--max-jobs`. The
mounted-secret path options are `--fly-token-file`,
`--secret-master-key-file`, `--tls-ca-certificate-file`,
`--tls-ca-private-key-file`, and `--handoff-token-file`; their values are file
paths, never credentials. Every mounted file and the state directory is refused
if group- or world-accessible, and symlinked mounted files are refused.

Issuance writes an AES-256-GCM bundle durably before any plaintext leaves the
secret boundary. Its filename and associated data contain only an activation
digest; the authenticated ciphertext binds the activation plus its exact
requester/consumer ownership. A retry or process restart therefore returns
byte-identical consumer registry and TLS material. Revocation writes a
content-free tombstone before deleting the ciphertext and is safe to repeat.
The Machine receives the single-consumer registry, leaf certificate, and
private key only through Fly's `files` contract; no secret enters its
environment or metadata.

The routing deployment injects `EncryptedFileRoutingCredentialVerifier` into
#1807's `build_routing_app`. The verifier mounts only the encrypted state
directory and master-key file—never the Fly token or TLS CA private key—and
returns the requester/consumer pair authenticated inside exactly one active
bundle. It compares every active credential in constant time, returns the same
anonymous miss for unknown, revoked, corrupt, or conflicting ownership, and
persists and logs none of the presented value. A restarted verifier reads the
same durable state immediately; it never issues or revokes credentials.

`creek-provisioning-router` is the production composition around that verifier.
The process also mounts the short-lived org deploy token used by its separate
Fly routing provider and the CA certificate used to authenticate private vault
Machines; the verifier itself still receives neither. A non-loopback bind
requires `--tls-cert` and `--tls-key`. The private `httpx` client trusts only the
mounted CA. Before each cold start the routing provider requires enough token
lifetime for every bounded Fly boundary plus the proxied request, so a process
that reaches its rotation window sheds traffic rather than attempting a route
with a token that can expire mid-flight.

```console
creek-provisioning-router \
  --database /var/lib/creek/jobs.sqlite3 \
  --fly-token-file /run/creek-secrets/fly-token \
  --fly-organization creek-vaults \
  --fly-image registry.example/creek@sha256:<digest> \
  --fly-token-expires-at 2026-09-14T12:00:00+00:00 \
  --secret-state-directory /var/lib/creek/runtime-secrets \
  --secret-master-key-file /run/creek-secrets/runtime-master-key \
  --private-tls-ca-certificate-file /run/creek-secrets/runtime-ca.crt \
  --host 0.0.0.0 --port 8840 \
  --tls-cert /run/creek-secrets/router.crt \
  --tls-key /run/creek-secrets/router.key
```

The Adepthood callback is an authenticated HTTPS POST to
`/internal/vault-provisioning/completions`. A `204` includes an identical
replay; `409` and other non-timeout 4xx responses are terminal conflicts, while
transport failures, `408`, `425`, `429`, and 5xx responses are retryable. No
response body is logged or copied into an exception.

Treat the `provisioning worker ready` log after configuration validation and
process liveness as the health signal. `provisioning worker stopped
processed=<count>` confirms a graceful drain. Absence of a fresh ready signal
after restart, an exited process, or retryable jobs accumulating past one lease
duration is unhealthy. The worker admits a claim only while the mounted Fly
token has enough remaining lifetime for ten serialized provider timeout
boundaries, the callback timeout, and one lease duration. That deterministic
window covers the longest normal one-app/one-volume/one-Machine create or
delete reconciliation plus settlement headroom. The check runs before every
claim, not only at process startup. On entering the window the worker logs a
content-free rotation warning, leaves pending work unclaimed with its attempt
count unchanged, drains no new work, closes its transports, and exits.

A Fly `401` after an earlier request boundary is a retryable authentication
failure. The current claim settles as failed without hiding or deleting an app,
volume, or Machine whose prior outcome succeeded. Recovery is: stop the worker;
preserve the SQLite file, encrypted runtime-secret directory, and master key
together; correct the provider or callback dependency; mount a fresh unexpired
Fly token; update `--fly-token-expires-at`; have the owning Adepthood control
plane requeue each failed job whose `retryable` flag is true; then restart. The
reconcile-first create/delete path adopts or removes partial resources before
settling, so neither billable resources nor teardown are stranded. Never delete
encrypted bundle state to fix a retry: doing so can mint a conflicting
credential for an already-created Machine.

Rotate the Fly token by stopping the worker, replacing the owner-only file with
a new short-lived org deploy token, updating `--fly-token-expires-at`, and
restarting. A token already inside the claim-admission window is refused at
startup; a token that enters it while the worker is idle causes the process to
exit before another claim. Rotate the handoff bearer by installing the same new
file in Adepthood and Creek during a drain, then restart the worker. Rotate the
runtime master key or CA only by draining all work and re-encrypting/reissuing
every live bundle under an explicit migration procedure; replacing either file
alone makes durable retries fail closed. Back up the encrypted state directory
and SQLite database atomically; the mounted keys stay in the secret manager,
never in that backup.

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

Successful ordinary-Fly create work performs the authenticated one-time
credential handoff and moves directly to `ready` inside the same lease-valid
write fence. The job records `custody_mode=provider_managed` and
`attested_confidential=false`; it creates no ceremony row or wrapped artifact.
The provider transparently unlocks the attached encrypted volume after a
scale-to-zero restart. Provider backup/restore is the only MVP recovery
mechanism.

## Public managed-vault routing (#1807)

The credential handoff never exposes the Machine address. Configure
`FlyProviderPolicy.routing_public_url` with the HTTPS origin of the shared
routing service; every allocation then hands Adepthood the same
`<routing-public-url>/v1` endpoint. The policy refuses plaintext, private-IP,
`.internal`, credential-bearing, query-bearing, and fragment-bearing values,
and provisioning fails before creating provider resources when this setting is
absent.

`build_routing_app` composes the public data-plane process from four injected
boundaries: `ProvisioningStore`, `RoutingCredentialVerifier`, `RoutingProvider`,
and an `httpx.AsyncClient` whose network can resolve Fly private DNS. A routing
credential authenticates to a `RoutingPrincipal(requester_identity,
consumer_identity)`; those two verified identities are the only inputs to the
store lookup. The request cannot name a job, activation, allocation, app,
Machine, hostname, IP, volume, or provider resource. Only a
`custody_mode=provider_managed`, `ready`, undeleted allocation owned by that
exact pair can route.

For the managed Fly pilot, an admitted request derives the canonical app from
the stored activation, verifies the stored provider allocation id, and
revalidates the Machine's complete immutable policy plus encrypted-volume
custody before starting it idempotently. It uses Fly's bounded
`wait?state=started` operation, then re-discovers and revalidates the Machine
and volume (so replacement or policy drift cannot produce a stale or unowned
target). The public router re-reads ownership, then returns Fly Proxy's
empty-body `307` with a delimiter-safe app/Machine target and the exact
per-allocation replay state. It never dials or emits a `.internal` address.
Deletion or replacement races therefore either replay to the current owned
Machine or fail as a content-free refusal.
Concurrent requests for the same durable allocation generation share one
in-flight preparation. A caller cancellation does not release a second start
beside the provider operation already running; cancellation still propagates
to that caller, and the single flight is forgotten when the bounded preparation
ends. A deleted and recreated owner-consumer pair has a new generation key and
can never inherit the prior Machine target.

The pilot router mounts only the published `/v1` method/path table. It preserves
the consumer bearer, contract-version and tier-ceiling headers for the vault
runtime to enforce, applies the public timeout, and caps replay bodies at Fly's
documented 1 MiB limit. The vault's Fly-only runtime requires a single
closed-shape `Fly-Replay-Src` state plus the preserved bearer before handling
the request. The generic private-TLS forwarding router remains available for
non-Fly deployments but is not the cross-network pilot path. Routing access
logs contain the requester
service identity, route template, status, duration, and correlation id; they
never contain the per-user consumer identity, credential, internal address,
provider identifier, request/response body, or content-derived metric.

Ordinary provider-managed deployments still install and require a private TLS
leaf certificate and key. The Fly replay runtime deliberately installs neither:
Fly Proxy supplies the TLS boundary, and the target accepts plaintext only
after exact Fly app/region/private-network attestation, a closed-shape replay
state, and the original consumer bearer all succeed.

The retired version-1
[`key ceremony protocol`](contracts/provisioning-v1/key-ceremony.md) and its
language-neutral vector remain only for migrated-row and interoperability
tests. `custody_mode=wrapped_artifact_only` identifies those historical rows
without claiming that the artifact ever controlled runtime storage. It is not
a production activation mode. A future attested implementation must prove key
use at first initialization and every restart before it may expose a ceremony
or set `attested_confidential=true`.

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

The production fleet driver first calls Fly's documented org-scoped
`GET /v1/apps?org_slug=...`, validates the closed response shape, and keeps only
names matching the exact managed-vault prefix and activation-digest shape.
It then inspects apps derived from live/pending-deletion allocations plus those
discovered names, so a store-forgotten orphan remains visible. Confirmed-deleted
jobs are not enough to hide an app that still exists in the provider inventory.
`--inventory-file` remains an additive cross-check for the output of
`fly apps list --json`; names outside the strict prefix are never requested.

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

Every path that moves a job into `deleting` (a consumer delete or a migrated
expired ceremony) opens a content-free receipt in
`provisioning_deletion_receipts`.
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
- a successful ordinary-Fly provider result plus authenticated handoff moves it
  directly to `ready`, records `custody_mode=provider_managed`, and reports
  `attested_confidential=false`;
- `awaiting_key_ceremony` and `custody_mode=wrapped_artifact_only` remain
  readable only for historical rows and are not reachable through the public
  contract;
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
