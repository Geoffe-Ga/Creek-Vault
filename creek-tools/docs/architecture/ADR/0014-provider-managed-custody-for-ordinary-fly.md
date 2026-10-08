# ADR-0014: Provider-managed custody for the ordinary Fly MVP

- **Status**: Accepted (ratified 2026-09-13)
- **Date**: 2026-09-13
- **Driving issue**: #1808; parent Geoffe-Ga/adepthood#2870
- **Amends**: [ADR-0005](0005-confidential-volume-key-no-escrow.md),
  [ADR-0007](0007-confidential-per-user-hosting.md), and
  [ADR-0013](0013-demand-provisioned-vault-lifecycle.md) for ordinary Fly
  Machines only
- **Superseded for journal content, as a decided target, by**:
  [adepthood ADR 0009](https://github.com/Geoffe-Ga/adepthood/blob/main/docs/adr/0009-privacy-custody-and-inference-architecture.md)
  (2026-10-07). See the section at the end. Everything below stays as written
  and still governs until adepthood B13 phases (b) and (c) land.

## Context

Creek implemented a client-generated wrapped-key artifact and recovery-code
protocol before the deployed runtime could use that artifact to encrypt or
unlock its durable volume. Ordinary Fly Volumes are encrypted at rest by Fly,
but Fly—not the user's passphrase or recovery code—holds the effective storage
unlock capability. The Creek process also reads OPEN and PERSONAL vault bytes
in plaintext while the Machine runs. Persisting the wrapped artifact as a
ceremony receipt therefore did not make the runtime no-escrow.

An unattended scale-to-zero restart needs an unlock capability available
without the user. On ordinary Fly hardware that capability must be available to
the provider. A user-held-only capability would instead require the user to
unlock every cold start, or an attested confidential environment able to obtain
and protect a released key. Neither path exists in the ordinary Fly MVP. A
ceremony that claims otherwise is worse than omitting the ceremony: it asks a
person to accept unrecoverable-loss friction without delivering the promised
custody boundary.

## Decision

### Ordinary Fly uses one explicit provider-managed mode

The managed-vault MVP uses custody_mode=provider_managed. The passphrase and
recovery key protect **nothing** in this mode because Creek and Adepthood do not
create, request, transmit, persist, or display either one. Fly's storage
encryption protects retired media and physical disks, not against Fly or a
sufficiently privileged Creek operator. Those parties can obtain the mounted
volume bytes; the running Creek service necessarily handles admitted OPEN and
PERSONAL content in plaintext. They can also observe account-scoped resource
identifiers, storage size, start/stop timing, network metadata, and operational
telemetry. Adepthood receives the scoped vault URL and consumer credential, not
a Fly token or storage key.

Scale-to-zero restart relies on Fly reattaching and transparently unlocking the
same provider-encrypted volume: Fly supplies the unlock capability. No user
action is required, and there is no
user recovery/reset promise. Provider loss of its storage capability is outside
Creek's recovery model; provider backup/restore is the only MVP recovery
mechanism.

### The public state machine skips the ceremony

Provisioning contract 2.0 removes the public key-ceremony route and schemas.
An ordinary create moves pending -> provisioning -> ready only after the
provider allocation and authenticated one-time credential handoff succeed in
the lease-valid store fence. That settlement durably records
custody_mode=provider_managed and attested_confidential=false. No ceremony row
or wrapped artifact is created. A failed or abandoned create follows the normal
retry/deletion and provider-reconciliation paths; there is no incomplete
ceremony resource class to expire.

The v1 cryptographic primitive and language-neutral vector remain archived for
interoperability tests. A migrated historical row is named
wrapped_artifact_only, which states the truth: its ciphertext was not consumed
by runtime volume custody. It is not exposed as a production activation route
and must never be described as no-escrow storage.

### INTIMATE remains local

Ordinary Fly continues to report attested_confidential=false. It cannot
advertise INTIMATE, receive INTIMATE over /v1, or route INTIMATE to remote
inference. Enabling INTIMATE still requires the complete attested chain in
ADR-0005/0006/0007, including a runtime that materially consumes user-controlled
key release and cannot silently fall back to provider-held unlock material.

## Consequences

- Managed activation no longer asks the user for a passphrase or recovery code.
- Support must not claim end-to-end encryption, operator blindness, no escrow,
  user-held recovery, or permanent double-loss semantics for ordinary Fly.
- Provider-managed encryption remains meaningfully better than an unencrypted
  disk, but it is a different threat boundary from the target architecture.
- The API major version changes because the ceremony route is removed and the
  job schema adds the explicit custody mode. Consumers must adopt the direct
  ready transition and honest copy before enabling managed activation.
- ADR-0005's user-held no-escrow design remains the target for a future runtime
  that can prove it; this decision does not weaken that design by pretending
  the current provider satisfies it.

## Revisit when

Reintroduce a user-held ceremony only when production tests prove all of these
properties together: the submitted artifact controls first initialization and
every restart; no reusable unlock material is operator-accessible; conflicting
binding or attestation fails closed; double loss is actually unrecoverable; and
the deployed confidential-compute chain, not a fake sink, consumes the release.

## Superseded for journal content by adepthood ADR 0009 (2026-10-07)

On 2026-10-07 the owner decided that **the operator cannot read people's
journal entries**. [adepthood ADR 0009](https://github.com/Geoffe-Ga/adepthood/blob/main/docs/adr/0009-privacy-custody-and-inference-architecture.md) records the decision.
Provider-managed custody is not operator-blind, so under that premise this
record is decided to stop governing journal content. Every statement above
remains an accurate description of what an ordinary Fly vault does.

**Not implemented yet.** Until adepthood B13 phases (b) and (c) complete for an
account, this record keeps describing **and governing** that account's journal
content, which stays operator-readable.

**What is decided to replace it for journal content:**

- Journal content and its prose derivatives will reach a managed vault **only
  as ciphertext** under the person's user-held key. The vault would store and
  sync that ciphertext without being able to read it.
- `custody_mode=provider_managed` **remains** for the vault's operational state:
  consumer credentials, job state and configuration. It also remains for any
  non-journal material the owner explicitly scopes in later.
- A vault-local model that needs plaintext is governed by adepthood ADR 0009
  Decision item 4. Each use is labelled, and plaintext inference in an
  ordinary Fly vault is not operator-blind while it runs.
- INTIMATE remains local, as above.

The "Revisit when" conditions above still govern any return to a
user-held-unlock *volume* ceremony. They do not govern journal content, which
is decided to become user-held under adepthood ADR 0009.

Nothing here is a public claim. Each claim waits for adepthood B24
([adepthood#3076](https://github.com/Geoffe-Ga/adepthood/issues/3076)).
