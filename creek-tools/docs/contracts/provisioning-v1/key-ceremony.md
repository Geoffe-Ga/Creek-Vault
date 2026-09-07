# Key ceremony protocol 1.0.0

This protocol completes an explicitly requested private-vault activation without
giving the Creek operator a recovery path. It is language-neutral; the checked-in
[`key-ceremony-test-vectors.json`](key-ceremony-test-vectors.json) is the
interoperability authority for client and server implementations.

## Security boundary

The client generates the passphrase, 256-bit volume master key (VMK), 256-bit
recovery value, Argon2id salt, client nonce, and both AES-GCM nonces locally.
Only the ciphertext-only wrapped artifact is submitted. The passphrase,
printable recovery code, raw recovery value, and unwrapped VMK MUST NOT enter an
HTTP request, log, analytic event, crash report, or operator-controlled durable
store.

The recovery code is shown or downloaded by the client exactly once before it
sets `recovery_saved: true`. Creek has no request field or retrieval route for
it. Losing both the passphrase and recovery code is permanent, unrecoverable
data loss; clients MUST state that before confirmation. Cancelling before
completion submits no artifact and leaves the challenge to expire.

## Sequence and expiry

1. The consumer explicitly submits an activation and polls until the job is
   `awaiting_key_ceremony`.
2. `GET /control/v1/jobs/{job_id}/key-ceremony` returns the version, activation
   id, ceremony id, 32-byte unpadded base64url server nonce, and expiry.
3. The client creates the bound wrapped artifact below and retains the recovery
   code only long enough to show/download it once.
4. `PUT` to the same route submits the ciphertext artifact. An identical retry
   is idempotent; a different payload for a completed ceremony is
   `ceremony_conflict`.
5. A successful job becomes `ready` and reports `attested_confidential` as
   `true` or `false`. An ordinary Fly Machine always reports `false`.

Challenges expire 24 hours after provider allocation. Expiry is inclusive: a
submission at or after `expires_at` returns `ceremony_expired`, changes the job
to `deleting`, and the durable worker reconciles provider resources to zero. The
worker performs the same sweep idempotently if no request arrives.

## Wrapped artifact

Version 2 extends the pre-existing `KeyVault` JSON with a `binding` object. For
protocol 1.0.0, the algorithms and parameters are fixed:

- VMK and recovery value: 32 random bytes each;
- recovery display: uppercase unpadded base32, grouped in five-character chunks;
- passphrase KEK: Argon2id, 16-byte salt, 32-byte output, `time_cost=3`,
  `memory_kib=65536`, `lanes=4`;
- recovery KEK: HKDF-SHA256, 32-byte output, no salt, info
  `creek.confidential.recovery-kek.v1`;
- wrapping: AES-256-GCM with independent 12-byte nonces; and
- ciphertext: the 32-byte VMK plus the 16-byte GCM authentication tag.

The binding keys, in canonical JSON order after lexicographic sorting, are
`activation_id`, `ceremony_id`, `client_nonce`, `protocol_version`, and
`server_nonce`. Canonical JSON is ASCII, UTF-8 encoded, has no insignificant
whitespace, and uses `,` and `:` separators.

The passphrase-wrap associated data is:

```text
UTF8("creek.confidential.vmk.passphrase.v1") || 0x00 || canonical_binding_json
```

The recovery-wrap associated data substitutes
`creek.confidential.vmk.recovery.v1`. Thus copying an artifact to another
activation, challenge, or client nonce invalidates both authentication tags.

## Attested key release

No `key_release` means the allocation is not attested and
`attested_confidential=false`. It MUST NOT advertise INTIMATE capability.

An attested submission carries both `attestation` and `key_release`. The
statement names the expected measured image, echoes the server nonce, binds an
X25519 recipient public key, and has timezone-aware issuance/expiry timestamps.
Its Ed25519 signature covers:

```text
UTF8("creek.key-ceremony.attestation.v1") || 0x00 || canonical_statement_json
```

where `canonical_statement_json` excludes `signature` and follows the same JSON
rules above. Both client and control plane verify the configured trust root,
exact measurement, challenge nonce, and `issued_at <= now < expires_at <=
challenge.expires_at`. The opaque release envelope repeats the recipient key and
uses `x25519-hkdf-sha256-aes256gcm`. Missing, mismatched, invalid, or expired
attestation fails with `ceremony_conflict` before the idempotent release sink is
called. Only a verified release may set `attested_confidential=true`.

## Stable errors

- `invalid_request` — malformed JSON, extra fields, wrong version/algorithm,
  non-canonical sizes, or `recovery_saved` is not exactly `true`;
- `job_unavailable` — missing or cross-consumer job (intentionally identical);
- `invalid_transition` — the job is not awaiting a ceremony;
- `ceremony_conflict` — challenge/replay mismatch or failed attestation; and
- `ceremony_expired` — the challenge timed out and teardown is queued.

Every response is `Cache-Control: no-store`. Error text, persistence, and
telemetry contain identifiers and stable codes only, never request secrets or
raw cryptographic failure detail.
