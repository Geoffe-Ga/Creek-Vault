# ADR-0013: Demand-provisioned per-user vault lifecycle

- **Status**: Accepted (ratified 2026-09-06)
- **Date**: 2026-09-06
- **Driving issue**: #1724; consumer counterpart
  `Geoffe-Ga/adepthood#2575`
- **Amends**: [ADR-0007](0007-confidential-per-user-hosting.md) Decision 1 by
  defining when an allocation exists and how its disposable compute runs. The
  per-user isolation, encrypted-volume, no-escrow, and attestation decisions
  remain unchanged.
- **Consumer counterpart**: Adepthood ADR 0007, "Confidential vaults are
  provisioned on demand, not at signup"

## Context

ADR-0007 chose one single-user Creek execution boundary per person, with
ephemeral compute and a durable encrypted user-owned volume. It did not decide
whether every Adepthood registration immediately creates that allocation, who
owns cloud-provider credentials, how background provisioning reports progress,
or when compute stops.

Those omissions became blocking product questions in #1724. A VM created for
every registration would charge for root filesystems and volumes whether the
person ever activates the private corpus. Putting provider orchestration in
Adepthood would duplicate lifecycle knowledge across the seam. Running the
Machine permanently would turn a sporadic personal workload into a fixed bill.
Doing the user-held-key ceremony during signup would turn Adepthood's low-door
journal experience into an irrecoverable security ceremony before the person
has asked for the feature.

Asynchrony answers the latency problem only. Demand provisioning and
scale-to-zero answer the cost problem. The three must be recorded separately so
"asynchronous" is never mistaken for "free".

## Decision 1 — Creek owns a shared asynchronous provisioning control plane

Creek ships and owns a small control-plane service separate from the
single-vault `creek-tools-api` process. An authenticated consumer submits an
idempotent activation request and receives a durable job handle immediately.
The control plane owns:

- cloud-provider credentials and API calls;
- the approved Creek image and measured-image identity;
- provider application, Machine, volume, network, and credential lifecycle;
- reconciliation of requested and actual state;
- one-time delivery of the resulting vault URL and per-consumer credential;
- teardown and orphan cleanup; and
- fleet usage and cost telemetry.

Adepthood never receives a provider credential and never shells out to a
provider CLI. Its only roles are to request activation, show job progress, and
store the completed vault connection under its existing encrypted credential
rules.

The job API is asynchronous and idempotent. Repeating one activation id returns
the same allocation or terminal result. Concurrent activation requests for one
consumer identity cannot create multiple billable volumes. States include at
least `pending`, `provisioning`, `awaiting_key_ceremony`, `ready`, `failed`,
`deleting`, and `deleted`; failure carries a stable machine-readable reason and
no secret material.

Job logs and consumer-visible responses never contain provider tokens,
passphrases, recovery keys, volume master keys, unredacted per-consumer bearer
tokens, journal text, or other vault content.

## Decision 2 — Allocate only after explicit private-vault activation

Account creation is not a Creek provisioning event. The consumer calls the
control plane only after an authenticated person explicitly activates the
private-vault capability and begins the key ceremony. An account that never
activates creates no provider application, Machine, root filesystem, or volume.

The passphrase-derived wrapping key and one-time recovery key from ADR-0005 are
created during activation, not signup. The control plane may reserve an
idempotency record while the ceremony is incomplete, but it must not leave a
usable plaintext vault or an indefinitely billable volume behind. Abandoned
ceremonies expire and reconcile to zero provider resources.

Provisioning failure is retryable and does not imply that an Adepthood account
or journal write failed. That degrade-never-throw behavior is a consumer
contract, not merely a UI preference.

## Decision 3 — One provider application and one scale-to-zero Machine per activated user

The MVP preserves Creek's single-user design literally: one provider
application, one Creek Machine, and one encrypted volume per activated user.
The provider application is the dynamic routing and lifecycle boundary. There
is no tenant field inside the vault and no shared readable corpus.

The reference driver targets Fly.io in one North American region:

- `shared-cpu-1x`, 1 GB RAM;
- 1 GB root filesystem;
- 5 GB persistent volume;
- no dedicated IPv4;
- zero minimum running Machines; and
- shared internal routing through the authenticated control plane.

The Machine starts on authenticated demand. For a request that launches
background work, the worker—not proxy idleness—owns shutdown: it drains the
durable job, commits outputs, closes the vault, and exits. Proxy autostop may be
used as a backstop, never as the correctness mechanism for background work.

The image, provider, region, CPU, memory, and volume size are configurable
driver policy. One isolated execution boundary, one encrypted durable volume,
and scale-to-zero are the architecture.

## Decision 4 — The reference cost model is a guardrail, not code

Using Fly.io prices published on 2026-09-06, the reference allocation is
estimated by:

```text
$0.75                         5 GB persistent volume
+ $0.15 * stopped_fraction   1 GB stopped root filesystem
+ $0.0082 * running_hours    1 GB shared-cpu-1x compute
+ egress + paid snapshots
```

That gives an approximate per-user monthly cost of $0.90 while fully stopped,
$0.94 at ten running minutes per day, $1.14 at one running hour per day, $1.86
at four running hours per day, and $6.67 when continuously started. A shared
256 MB control plane is approximately $2.02 per month before traffic.

These values belong in operations documentation and billing tests as injected
assumptions, never as business logic. The control plane reports provisioned
volumes, stopped rootfs capacity, active Machine seconds, snapshot bytes, and
egress so actual invoices can be reconciled. It alerts on duplicate allocations,
orphan resources, and departure from an operator-set monthly budget.

## Decision 5 — Ordinary VMs cannot unlock INTIMATE processing

A provider VM supplies process isolation, not operator blindness. The control
plane must label an allocation's confidentiality capability honestly. A normal
Fly Machine can serve OPEN/PERSONAL work under the consumer's existing policy;
it may not cause a consumer to send INTIMATE plaintext or advertise the full
private-compute promise.

INTIMATE processing becomes available only when the deployment proves the
whole ADR-0005/0006/0007 chain: user-held key, no operator escrow, fresh remote
attestation of the measured image, key release only into that enclave, and
confidential inference for any model that sees the plaintext. Failure or expiry
of attestation fails closed.

## Decision 6 — Deletion is reconciled and auditable

An explicit consumer deletion request moves the allocation to `deleting`,
stops compute, destroys the Machine and volume, revokes its consumer credential,
and records a content-free completion receipt. Retries are idempotent. A failed
provider deletion remains visible to reconciliation and alerts until the
provider confirms that no billable resource survives.

The receipt may identify the consumer surrogate, provider, resource classes,
timestamps, and outcome. It contains no vault contents, keys, credentials, or
provider secret. A person's export opportunity and account-deletion UX are the
consumer's responsibility; Creek's responsibility is that an accepted delete
does not silently become an orphaned bill.

## Decision 7 — Pooling may replace physical per-user compute only after measurement

Review the one-app/one-Machine driver at 500 activated vaults, 1,000
provisioned volumes, a material change in confidential-compute availability, or
three rolling months above the approved fleet budget—whichever comes first.

A future scheduler may pool attested execution capacity while keeping distinct
encrypted user volumes and keys. It must prove no cross-user plaintext state,
attested key release, equivalent deletion, and the same externally observable
single-user contract. A multi-tenant readable corpus is not an optimization of
this decision; it is a different architecture requiring a new ADR.

## Consequences

- Creek gains an independently deployable control plane and provider-driver
  boundary.
- Per-user recurring storage costs begin at activation, while compute follows
  use.
- Cold-start and provisioning states become part of the product contract.
- The control plane becomes security-critical and needs narrow credentials,
  auditability, reconciliation, quotas, and adversarial tests.
- ADR-0007's privacy posture remains intact and is no longer conflated with an
  always-on VM or signup-time allocation.

