# ADR-0006: Confidential-compute enclave — attestation trust model

- **Status**: Accepted
- **Date**: 2026-07-01
- **Driving issues**: #760 (attested GPU-CC enclave provider), #757 / #755 (confidential-hosting / remote-transport decisions), #642 (per-stage model routing)

## Context

The confidential-hosting epic needs the system's most private tier (INTIMATE) to
get GPU-quality voice output without egressing to a provider that can read it.
The plan is an operator-run **GPU confidential-compute (GPU-CC) enclave**: the
model runs in a hardware-encrypted memory enclave the operator cannot read, and
the client verifies **remote attestation** before sending anything.

`ModelRouter`'s INTIMATE chokepoint (#642/#647) routes INTIMATE work only to
providers whose class-level `is_cloud` is `False`. So the entire safety of
sending INTIMATE content to a *remote* enclave rests on one claim: that
`is_cloud=False` is justified because attestation is verified before any egress.
If that gate is weak, a mis-set flag — or an impersonator — turns
intimate-safe into a silent leak. The gate, not the flag, must be the boundary.

An initial implementation verified attestation with a plain HTTP GET plus a
string comparison of the returned measurement. That cannot distinguish a genuine
enclave from an impersonator (no signature / root of trust), cannot detect a
replayed quote (no freshness), and does not require TLS. It was rejected in
review as not backing its own security claim.

## Decision

The `enclave` provider (`creek/classify/llm/providers.py`) performs a
**cryptographic attestation handshake before every completion**, and fails
closed on any failure — there is no plaintext fallback. All of the following
must hold or the prompt is never sent:

1. **TLS transport.** `enclave_url` must be `https://` (defends the channel and
   the bearer of any per-request material).
2. **Freshness / anti-replay.** The client sends a fresh 32-byte random nonce as
   a challenge; the quote must echo that exact nonce. An old (previously valid)
   quote cannot be replayed.
3. **Authenticity / integrity — root of trust.** The quote carries an Ed25519
   signature over `domain-sep || measurement || nonce`; it must verify under the
   operator-configured **trust-root public key** (`enclave_attestation_pubkey`).
   An impersonator without the corresponding private key cannot forge a quote.
4. **Policy.** The attested `measurement` must equal the configured
   `enclave_expected_measurement` (the enclave is the expected image).

Configuration (`LLMConfig`): `enclave_url`, `enclave_expected_measurement`, and
`enclave_attestation_pubkey`. **None of these is a secret** — the public key is,
by definition, public; the measurement and URL are non-sensitive. No key or
consent value is ever written to config (consistent with ADR-0003 and the
cloud-provider rule).

## Consequences

### Positive

- `is_cloud=False` is now *defended in code*: authenticity (signature vs a trust
  root), freshness (nonce), integrity (signed measurement), and transport (TLS)
  are all checked before egress, so INTIMATE routing to the enclave is backed by
  the gate rather than a bare flag.
- The router needs no change: `is_cloud=False` makes the enclave INTIMATE-eligible
  and a valid local `default` rescue for an INTIMATE-bound cloud stage.
- Fail-closed everywhere: every missing prerequisite or failed check raises
  `EnclaveAttestationError` (a `RuntimeError`) and sends no prompt.

### Negative / boundaries

- **The trust root is operator-provisioned, not a full vendor certificate chain.**
  This verifies the enclave holds the private key matching the configured public
  key and reports the expected measurement over a fresh nonce. It does **not**
  yet validate a hardware attestation report up a vendor PKI (e.g. an NVIDIA
  GPU-CC / SEV-SNP / TDX quote chained to the vendor root). Provisioning the
  correct trust-root key and measurement is an operator responsibility; a
  compromised or mis-provisioned root key would undermine the gate. Extending to
  full vendor-chain verification is deliberate future work (a follow-up issue),
  and the boundary is documented so the guarantee is not overstated.
- **Attestation fetch validates TLS against the `certifi` CA bundle, not a
  pinned CA and not the OS trust store.** The client opens `httpx.Client` with
  no `verify=` override, so `httpx` builds its default SSL context from the
  `certifi` bundle shipped with the Python environment (`certifi` is a
  transitive dependency via `httpx`; nothing in `providers.py` or `pyproject.toml`
  sets a truststore, `SSL_CERT_FILE`, or `REQUESTS_CA_BUNDLE`). A self-signed
  certificate on an internal-only enclave endpoint is therefore rejected (fails
  closed, which is safe) rather than silently accepted. To trust an internal CA,
  an operator can add it to the `certifi` bundle, point `SSL_CERT_FILE` (or
  `SSL_CERT_DIR`) at a custom bundle — `httpx` honours both when `trust_env` is
  left at its default `True` — or pass an explicit `verify=`/`ssl.SSLContext`.
- Per-call attestation adds one round-trip of latency before each completion
  (accepted: the enclave path is for high-value voice generation, not bulk
  classification).

## Revisit when

- A vendor hardware-attestation flow (NVIDIA GPU-CC / SEV-SNP / TDX) is wired so
  the trust root becomes the vendor PKI rather than an operator-held key.
- A short-lived attested-session/token model replaces per-call attestation to
  cut the extra round-trip while preserving freshness.
