# ADR-0013: Demand-provisioned per-user vault lifecycle

- **Status**: Accepted (ratified 2026-09-06)
- **Date**: 2026-09-06
- **Driving issue**: #1724; consumer counterpart
  `Geoffe-Ga/adepthood#2575`
- **Amends**: [ADR-0007](0007-confidential-per-user-hosting.md) Decision 1 by
  defining when an allocation exists and how its disposable compute runs. The
  per-user isolation and encrypted-volume decisions remain unchanged.
- **Custody amendment**:
  [ADR-0014](0014-provider-managed-custody-for-ordinary-fly.md) replaces the
  ordinary-Fly ceremony below with an explicit provider-managed direct-ready
  transition. Historical ceremony states remain readable for migration only.
- **Alert amendment**: [Amended by #1867](#amended-by-1867-2026-10-31) adds
  `provider_credential_expiring` to the closed operator alert list.
- **Vault-alert amendment**: [Amended by #2049](#amended-by-2049-2026-11-01)
  carries alerts raised inside a vault to the operator: the control plane
  pulls them.
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
consumer identity cannot create multiple billable volumes. The ordinary-Fly
path uses `pending`, `provisioning`, `ready`, `failed`, `deleting`, and
`deleted`. The legacy `awaiting_key_ceremony` value remains readable for
migrated rows only; failure carries a stable machine-readable reason and no
secret material.

Job logs and consumer-visible responses never contain provider tokens,
passphrases, recovery keys, volume master keys, unredacted per-consumer bearer
tokens, journal text, or other vault content.

## Decision 2 — Allocate only after explicit private-vault activation

Account creation is not a Creek provisioning event. The consumer calls the
control plane only after an authenticated person explicitly activates the
private-vault capability. An account that never
activates creates no provider application, Machine, root filesystem, or volume.

Ordinary Fly requests no passphrase or recovery key because neither one
controls its volume. After allocation and authenticated credential handoff, the
job becomes `ready` with `custody_mode=provider_managed` and
`attested_confidential=false`. Provider failures remain durably reconcilable so
an abandoned activation cannot silently leave an indefinitely billable volume.

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
- ADR-0007's target privacy posture remains explicit, while ADR-0014 prevents
  the ordinary Fly MVP from claiming it prematurely.

## Amended by #1867 (2026-10-31)

This record stays as written. Decision 4 closed the operator alert list
(duplicate allocations, orphan resources, departure from the monthly budget,
and the kinds later ADRs joined to it). #1867 adds one kind, approved by the
owner with release decision R2-2 (Fly org tokens live at most 90 days):

- **`provider_credential_expiring`.** The scheduled fleet report raises it on
  the fleet when the operator-recorded expiry of the Fly org token is within
  the warning window (72 hours by default, configurable), has already passed,
  or was never recorded. A missing expiry is a configuration gap, never
  "fine". It is re-raised on every report pass until the recorded expiry
  changes.
- **Recorded, not discovered.** The expiry and the token's non-secret ID are
  operator-recorded configuration beside the token. Creek never calls the
  provider's token API to learn them and never reads the token to compute the
  warning.
- **No token material.** The alert's private wording names only the token ID
  and the expiry; the delivered payload stays the counts-only
  `creek_fleet_alert_v1` document, so neither leaves the control plane.
- **Receiver first.** The consumer's alert receiver validates the same closed
  list, so it must admit the new kind before a Creek build that raises it is
  deployed.

## Amended by #2049 (2026-11-01)

This record stays as written. Decision 4's alerts were all raised by the
control plane's fleet pass. Since ADR-0016 (#1896) and #2007 a managed vault
raises some of its own: the attesting pool's `pool_attestation_failed`,
`pool_credential_rejected`, `pool_admission_refused`,
`pool_vendor_rate_limited` and `pool_quota_exhausted`, and the local spend
meter's EUR 5 alert (`monthly_budget_departure`). Each was written only to the
vault's own `creek.pool` log, and the fleet report cannot read a vault volume,
so none reached a person.

**Decision: the control plane pulls.** Each fleet report pass reads, from
every vault the same pass observed running, a closed, counts-only owed-alert
summary, and hands the new counts to the same `AlertRelay` and
`HttpFleetAlertSink` its own alerts use. The pull was chosen over a vault push
for three reasons:

- **No new egress from the vault.** A managed vault is local-only and its one
  outbound path is the pinned pool sidecar. A push would give every vault a
  route to the control plane; a pull adds none.
- **No new credential.** The control plane already holds each allocation's
  replay state (known only to it and that Machine) and consumer bearer. The
  vault already verifies both on every replayed request. A push would need a
  vault-to-control-plane credential, and the consumer bearer cannot serve,
  because Adepthood holds it too and could forge or suppress alerts with it.
- **It fits autostop.** A stopped vault cannot push. Under the pull, its owed
  alerts wait on its volume, and the first pass that sees it running reads
  them. The pass never wakes a stopped vault.

**The channel.**

- **Vault side** (`creek/vault_alerts.py`). An outbox on the volume records a
  cumulative raised count and a cumulative acknowledged count per kind, with
  a random epoch minted when the outbox is created. The only kinds are the
  closed `VAULT_ALERT_KINDS` subset of `AlertKind`. Writes are atomic and
  locked across processes, and a damaged document is reported, never reset.
- **Private listener** (`creek_mcp/httpapi/vault_alerts.py`). It has two
  routes, `GET /internal/vault-alerts` and `POST /internal/vault-alerts/ack`.
  It is served on port 8824 at the Machine's `FLY_PRIVATE_IP` (6PN) only,
  never in the Fly service config, and never on the published `/v1` app. The
  stack is the `/v1` layers in the same order: access log, error boundary,
  the replay-state guard, then the bearer guard. The edge replays only
  published paths, so no public request reaches it.
- **Control-plane side** (`creek_mcp/provisioning/vault_alert_pull.py`). It
  dials `http://<app>.internal:8824` with a proxy-free bounded client, and
  every answer is parsed against the closed shape before anything is trusted.
  Counts above a durable per-allocation, per-epoch mark are new. The pass
  hands them to the relay, flushes, records the marks, and only then
  acknowledges the vault.

**Guarantees.**

- **Counts only.** Nothing but closed kinds and counts crosses either hop or
  is stored: no subject, request id, vendor id, amount, prompt, answer or key.
  The receiver's `creek_fleet_alert_v1` document is unchanged, and Adepthood's
  receiver already admits every kind (adepthood#3646).
- **Delivered once, never lost.** A vault stops owing an alert only after the
  relay has delivered it or retained it durably. The relay now also persists
  counts it collects while backing off. Because the mark is written before the
  acknowledgement, a lost acknowledgement causes a re-acknowledgement, not a
  second delivery. The one duplicate window is a control-plane crash between
  the relay's flush and the mark. In that case the alert is delivered twice,
  never lost.
- **Visible while owed.** For each vault, the store keeps the outcome of the
  last read and the counts last seen owed. `creek-provisioning-fleet report`
  prints them as `vault_alerts_owed`. An unreachable vault keeps its
  last-known counts there.
- **The bound.** An alert raised in a vault that autostops before any pass
  sees it running is delivered at the first pass that does. The pilot's pass
  runs every five minutes.

**Admission (R2-4).** An alert that a vault still owes does not close the
activation gate. A stopped vault holding a spend alert is the ordinary case,
not a failed alert channel. R2-4 still governs the channel itself. If the
relay cannot deliver vault alerts, it refuses new activations exactly as it
does for fleet alerts.

**Spend alert.** The spend meter raises its alert with the one call
`owe_vault_alert("monthly_budget_departure")`. At the receiver this kind looks
the same as the fleet budget departure. In the control plane's log the two are
different lines: `vault alert kind=monthly_budget_departure count=1` for the
vault's alert, and `fleet alert kind=monthly_budget_departure` for the fleet
estimate, which is raised again on every pass while it holds.
