# ADR-0005: Confidential-volume key — user-held, no operator escrow

- **Status**: Accepted
- **Date**: 2026-07-01
- **Driving issues**: #758 (per-user encrypted volume + recovery key), #757 / #755 (confidential-hosting / remote-transport decisions)

## Context

The confidential-hosting epic encrypts each user's durable vault volume so the
operator cannot read it. That requires a key model with a deliberate, hard-to-
reverse property: **who can recover the data, and who cannot** — including the
operator.

Two broad options exist for the at-rest key:

1. **Operator-escrowed / resettable** — the operator holds (or can reconstruct)
   a key, so a user who forgets their passphrase can be helped. This makes the
   operator a single point of compromise: a breach, subpoena, or insider can
   decrypt every user's vault. It contradicts "the operator cannot read it."
2. **User-held, no escrow** — only key material the user holds can decrypt the
   volume. The operator stores ciphertext only. A user who loses their factors
   loses the data; there is no reset.

The vault holds a person's whole journal, including intimate-tier content that
the rest of the system is explicitly built never to egress. The product framing
(per the #758 issue) is Obsidian Sync's optional end-to-end encryption: powerful
privacy, with the well-understood cost that the provider genuinely cannot help
you recover.

## Decision

Adopt **user-held keys with no operator escrow and no reset** (issue-#758
"option E").

- A random 256-bit **volume master key (VMK)** encrypts the volume.
- The VMK is wrapped twice, giving two independent unwrap paths:
  - a **passphrase** the user chooses (KEK via Argon2id, memory-hard);
  - a one-time **recovery key** generated at setup and shown to the user to
    store (KEK via HKDF, since it is already full-entropy).
- The persisted artifact (`creek/confidential/keyvault.py` `KeyVault`) holds
  **ciphertext + public parameters only** (salt, Argon2id cost, AEAD nonces).
  The VMK, passphrase, and recovery key are never persisted operator-side.
- **Losing both the passphrase and the recovery key is permanent, unrecoverable
  data loss.** There is no operator reset path, by design.

## Consequences

### Positive

- The operator genuinely cannot decrypt a user's vault — no escrow to breach,
  subpoena, or misuse. This is the whole point of confidential hosting.
- Two independent recovery factors (passphrase + recovery key) reduce the chance
  of accidental total loss versus a single factor.
- AES-GCM + domain-separated AAD make wrong keys / tampering fail closed.

### Negative

- **Irreversible on double-loss.** A user who loses both factors loses the data
  with no recourse. This must be surfaced clearly in the setup UX (show and
  insist the user store the recovery key) and documented for users.
- Support cannot "reset a password"; the recovery key is the only backstop.
- The single persisted key artifact is precious — hence it is written atomically
  and `0600` (`save_key_vault`), so a torn write cannot itself cause lockout.

## Revisit when

- A user-research signal shows the no-reset model causes unacceptable data-loss
  incidents in practice (revisit the recovery UX before revisiting escrow).
- A regulatory or product requirement forces a recovery path — at which point the
  escrow trade-off must be re-litigated explicitly, not added silently.
- The KDF choice needs strengthening (raise Argon2id cost; params are persisted
  per-vault so old vaults keep unlocking).
