# ADR-0007: Confidential per-user hosting — VM, TEE-attested GPU voice, user-held keys, BYOK

- **Status**: Accepted (ratified 2026-06-30)
- **Date**: 2026-07-01
- **Driving issues**: epic #757; decision #755 (and `geoffe-ga/adepthood#927`); builds on the routing epic #642 and the handshake epic #748
- **Implemented by**: #758 (volume key), #759 (network transport), #760 (enclave provider), #761 (BYOK)

## Context

The Adepthood app needs to reach a user's Creek vault from a remote backend and
generate voice-quality output, **including for the user's most private
(INTIMATE) content**, without the operator being able to read that content. The
vault holds a person's whole journal; the product promise is that intimate
material never leaves the user's private space in a form anyone else can read.

That forces a set of coupled decisions — where the vault runs, who can decrypt
it, how a remote client reaches it, and where the LLM work happens — that must be
made together and recorded outside the issue threads that produced them. This ADR
is the umbrella record; the two hardest sub-decisions have their own ADRs
([ADR-0005](0005-confidential-volume-key-no-escrow.md) for key custody,
[ADR-0006](0006-enclave-attestation-trust-model.md) for enclave attestation).

## Decision

Adopt the ratified hosting/custody/routing model (#755):

1. **Persistent per-user VM.** Ephemeral compute (the Creek MCP server + models,
   spun up on access) plus a **durable, encrypted, user-owned volume** (the
   vault). Compute is disposable; the encrypted volume is the durable asset.

2. **User-held keys, no operator escrow.** The volume is encrypted under a random
   volume master key wrapped twice: a user **passphrase** (KEK via Argon2id,
   memory-hard) and a one-time **recovery key** (KEK via HKDF). The operator
   stores ciphertext only; losing both factors is unrecoverable, by design. This
   is issue-#758 "option E" — see **[ADR-0005](0005-confidential-volume-key-no-escrow.md)**.

3. **Confidential compute (TEE) with remote attestation; GPU-CC in scope.** The
   volume key is released into an enclave only after remote attestation verifies
   the measured image on genuine confidential hardware. **GPU confidential
   computing (H100+) is in scope** for the voice/`generation` stage, because
   CPU-small models follow instructions inadequately and the enclave is what lets
   even INTIMATE content get GPU-quality voice while staying private.

4. **The attested enclave is a provider classified `is_cloud=False`
   ("attest, then mount").** It is valid as `is_cloud=False` **only** because it
   cryptographically verifies attestation (signature vs a trust root + fresh
   nonce + TLS) before any key or prompt flows — the gate, not the flag, is the
   boundary. See **[ADR-0006](0006-enclave-attestation-trust-model.md)**.

5. **Network MCP transport + per-consumer auth, INTIMATE never remotely
   reachable.** A remote client reaches the vault over an authenticated
   streamable-http transport (per-consumer bearer tokens, no anonymous access),
   with `TierCeiling` default-deny at the boundary so INTIMATE is unreachable
   over the network. Implemented in #759.

6. **BYOK for OPEN/PERSONAL; INTIMATE-never-cloud stays automatic.** A user may
   point cloud stages at their **own** provider key (BYOK, #761). INTIMATE is
   protected regardless by the single `ModelRouter` chokepoint
   (`_enforce_local_for_intimate`, #642): an INTIMATE fragment bound for any
   cloud provider is redirected to a local/enclave provider, or the run fails
   loudly rather than egressing.

## Alternatives considered (briefly)

- **Operator-escrowed / resettable keys** — rejected: makes the operator a single
  point of compromise, contradicting "the operator cannot read it" (see ADR-0005).
- **Co-locate the Adepthood backend with the vault** — the per-user VM is the
  chosen shape of this; a bare relay/bridge or syncing a scoped non-intimate
  subset were considered and rejected (INTIMATE must never be remotely reachable
  under any option; decided jointly with `geoffe-ga/adepthood#927`).
- **CPU-only local models for all tiers** — rejected for the voice stage:
  instruction-following quality is inadequate, which is the reason GPU-CC is in
  scope.
- **Bare `is_cloud=False` flag on the enclave without attestation** — rejected: a
  mis-set flag would silently leak intimate content, so the flag is defended by
  the attestation gate (ADR-0006).

## Consequences

- The operator genuinely cannot read a user's vault (no escrow; INTIMATE never
  egresses to a readable cloud or over the network), which is the whole point.
- Two independent recovery factors reduce accidental total loss, but double-loss
  is permanent and must be surfaced in setup UX.
- More moving parts (VM lifecycle, attestation, per-consumer tokens, BYOK key
  supply); each is isolated behind a tested seam and its own issue/ADR.
- Must stay in lock-step with `geoffe-ga/adepthood#927` and the handshake epic
  #748.

## Revisit when

- The enclave attestation moves from an operator-provisioned trust root to a full
  vendor certificate chain (tracked in #778; see ADR-0006 "Revisit when").
- A regulatory or product requirement forces a key-recovery path (re-litigate the
  escrow trade-off explicitly; see ADR-0005).
- The per-user-VM cost model or the GPU-CC availability changes materially enough
  to reconsider the hosting shape.
