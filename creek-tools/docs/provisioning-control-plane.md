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
Clients submit `{activation_id, consumer_identity}`, poll `status_url`, retry
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
distinct concurrent ids for the same consumer resolve to its one live job, so
two API processes cannot create two billable allocations. Each consumer may
retain at most 256 activation aliases. Existing aliases remain idempotent after
the limit is reached; an additional distinct alias is rejected with `409`.
