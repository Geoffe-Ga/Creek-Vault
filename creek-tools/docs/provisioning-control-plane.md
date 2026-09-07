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
same job id. Provider adapters must therefore make create and delete idempotent
on that job id. Stable failures are recorded as `FailureReason` values, and raw
exception detail is discarded before persistence or logging.

The worker takes two injected boundaries:

- `ProviderDriver`, which creates/deletes provider resources;
- `OneTimeCredentialHandoff`, which delivers an identical result at most once
  to the authenticated consumer backend and retains no plaintext in the test
  implementation.

`FakeProviderDriver` and `FakeOneTimeHandoff` prove the contract under tests.
They are not deployment drivers. Fly.io resource calls, scale-to-zero policy,
and provider deletion reconciliation land in #1770. This issue intentionally
ships no provider credential or provider-specific API call.

Successful create work stops at `awaiting_key_ceremony`. The user-held
no-escrow and attestation protocol that can advance it to `ready` is #1771.
Until that work lands, an ordinary allocation must not advertise INTIMATE
capability.

## State and retry rules

- `pending` is claimable create work.
- a create lease moves it to `provisioning`;
- a successful fake/real driver result moves it to `awaiting_key_ceremony`;
- #1771 may mark a completed ceremony `ready`;
- stable failures become `failed` and are retryable only when explicitly
  recorded as safe;
- delete changes any live state to `deleting`, and only provider confirmation
  produces the durable `deleted` receipt.

Activation ids remain durable aliases. Repeating one returns the same job;
distinct concurrent ids for the same consumer resolve to its one live job, so
two API processes cannot create two billable allocations.
