# Single-vault container runtime

The repository root `Dockerfile` packages exactly one `creek-tools-api`
process for exactly one consumer identity and one durable vault. The image is
disposable. Every writable Creek artifact—configuration, staged input, audit
state, and vault content—lives below the explicitly mounted `/vault` tree.

This runtime is the storage and API primitive selected by ADR-0013. Under the
ordinary-Fly MVP in
[ADR-0014](architecture/ADR/0014-provider-managed-custody-for-ordinary-fly.md),
the mounted provider volume supplies encryption at rest. There is no user-held
passphrase or recovery key: Fly and a sufficiently privileged Creek operator
can read mounted bytes, and Fly supplies the unlock capability across
scale-to-zero restarts. Do not describe this image as operator-blind,
end-to-end encrypted, no-escrow, or user-recoverable. INTIMATE content remains
local until a future runtime proves the complete attested custody design.

## Build and identify the image

Build from the repository root. The Python base uses an exact patch release and
immutable multi-platform digest, and Python dependencies come from `uv.lock`.

```console
docker build --tag creek-vault:local .
docker image inspect creek-vault:local --format '{{.Id}}'
```

Record the resulting image digest with deployment metadata. A tag is a human
label; the digest is the image identity suitable for the measured-image work
that follows this runtime.

## Create the mounted inputs

Create one persistent volume and one secret directory per user. Never reuse a
volume or consumer token between two users.

```console
docker volume create creek-user-001
mkdir -p ./run-secrets
openssl rand -base64 48 | sed 's/^/adepthood=/' > ./run-secrets/creek_consumer_tokens
openssl req -x509 -newkey rsa:3072 -nodes \
  -keyout ./run-secrets/tls.key -out ./run-secrets/tls.crt \
  -days 30 -subj '/CN=127.0.0.1' -addext 'subjectAltName=IP:127.0.0.1'
sudo chown -R 10001:10001 ./run-secrets
sudo chmod 0511 ./run-secrets
sudo chmod 0400 ./run-secrets/creek_consumer_tokens ./run-secrets/tls.key
sudo chmod 0444 ./run-secrets/tls.crt
```

The image runs as UID/GID `10001:10001`. Bind mounts retain host ownership, so
the `chown` step is required: owner-only mode `0400` is secure and readable by
the container only when UID 10001 owns the file. Directory mode `0511` lets an
operator pass the public certificate to `curl` by its known name without
allowing directory listing; the bearer registry and private key remain
owner-only. Apply equivalent ownership and permissions when a secret manager
materializes these files.

The token file uses Creek's existing registry format:
`consumer=current-token`. During rotation, two tokens for that same consumer
may temporarily be comma-separated. A semicolon would add a second consumer,
which this image refuses.

Credentials must arrive through `/run/secrets`; do not use environment values,
Docker build arguments, command arguments, or image layers. The only supported
environment settings are non-secret paths and the port:

| Setting | Default | Purpose |
|---|---|---|
| `CREEK_CONTAINER_VAULT_PATH` | `/vault` | Exact durable mount point |
| `CREEK_CONTAINER_CONFIG_FILE` | `/vault/00-Creek-Meta/creek_config.yaml` | Optional explicit read-only config file |
| `CREEK_CONTAINER_CONSUMER_TOKENS_FILE` | `/run/secrets/creek_consumer_tokens` | Consumer registry secret path |
| `CREEK_CONTAINER_TLS_CERT_FILE` | `/run/secrets/tls.crt` | TLS certificate path |
| `CREEK_CONTAINER_TLS_KEY_FILE` | `/run/secrets/tls.key` | TLS private-key path |
| `CREEK_CONTAINER_PORT` | `8823` | HTTPS listener port |

An explicit config file must exist and its `vault_path` must resolve to the
mounted vault. This prevents configuration from redirecting writes into the
image root.

## First boot and restart

Run with a read-only root filesystem, a small disposable `/tmp`, no added
privileges, and explicit mounts:

```console
docker run --detach --name creek-user-001 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --security-opt no-new-privileges \
  --publish 127.0.0.1:8823:8823 \
  --mount source=creek-user-001,target=/vault \
  --mount type=bind,src="$PWD/run-secrets",dst=/run/secrets,readonly \
  creek-vault:local
```

On first boot, and only when `/vault` is both mounted and completely empty,
the entry point deploys the canonical Creek scaffold and writes a config whose
`vault_path` is `/vault`. Every later start validates that config and makes no
bootstrap write. A nonempty volume missing its config is refused untouched;
an unmounted `/vault` is also refused. This is intentionally stricter than
trying to repair ambiguous storage automatically.

Restarting the same container or creating a replacement container over the
same volume preserves content and accepts the same mounted credential:

```console
docker restart creek-user-001
```

For a second user, create a second volume and second secret directory and run a
second container. Never mount one user's volume into another user's container.

## Health and readiness

The image healthcheck performs the deepest probe. Operators can inspect each
layer independently without putting a credential in process arguments:

```console
docker exec creek-user-001 python -m creek_mcp.container_health --check process
docker exec creek-user-001 python -m creek_mcp.container_health --check volume
docker exec creek-user-001 python -m creek_mcp.container_health --check ready
```

The states are deliberately distinct:

- `process-up` / `process-down`: whether the loopback API socket accepts a connection.
- `vault-mounted` / `vault-unmounted`: whether the configured vault is still an exact Linux mount point.
- `v1-ready` / `v1-unready`: whether authenticated TLS `GET /v1/health` returns the pinned healthy response after the first two gates pass.

Exit codes are `0` for a satisfied target, `20` for process-down, `21` for an
unmounted vault, and `22` for an unready `/v1` application.

### Storage-ready is not model-ready

`--check ready` is **storage** readiness only. It stays the image
`HEALTHCHECK` default, and it never consults a model. A separate target asks
whether the pinned local model can generate:

```console
docker exec creek-user-001 python -m creek_mcp.container_health --check model
```

| State | Exit | Meaning |
|---|---|---|
| `model-ready` | `0` | The loopback runtime serves the pinned model at its pinned digest **and** completed a fixed synthetic canary prompt |
| `runtime-down` | `23` | The loopback runtime did not answer, answered non-200, or returned an unreadable inventory |
| `model-missing` | `24` | No model package is configured, the manifest failed to load, or the runtime does not list the pinned tag |
| `model-digest-mismatch` | `25` | The pinned tag is listed, but at another (or no) digest |
| `generation-failed` | `26` | The inventory matched, but the canary generation errored or returned nothing |

The model probe does not read the vault or its config. It sends only the
constant canary prompt to the runtime at `http://localhost:11434`, and it
prints one state line with no logging. A broken container environment under
`--check model` reports `model-missing`, never a storage state.

**The image ships no model runtime yet.** Until a runtime and model are
selected and installed (Creek-Vault#1849, gated on the #1850 capacity
decision), `--check model` reports `model-missing` (no package configured) or
`runtime-down`. An existing storage-only vault therefore stays storage-ready
and model-unavailable. `/v1/health` stays the constant described in the
2026-07-31 HTTP application API ADR, and it carries no model state.

### Model package manifest

A model package is pinned by a JSON manifest that the operator mounts
**outside `/vault`** and names with `CREEK_MODEL_PACKAGE_FILE`. Keeping it
outside the vault means a vault-config edit can neither set nor loosen the
pin. Every field is required, and the loader refuses a missing, unpinned or
malformed field:

- runtime name, exact version and digest;
- a model tag that is explicit and not `latest`;
- the weights' SHA-256;
- the digest the runtime's inventory reports;
- quantization, parameter count and size in bytes;
- the SPDX licence id and an `https` licence URL.

The approved-licence allowlist (`APPROVED_MODEL_LICENSES` in
`creek_mcp/model_package.py`) is **empty** until the owner approves a licence.
Until then every manifest is refused and the model stays unavailable.

To verify a weights file against the configured manifest:

```console
python -m creek_mcp.model_package verify --weights /path/to/weights
```

The command streams SHA-256 over the file and prints one of `verified`,
`missing`, `size-mismatch` or `digest-mismatch`. It exits `0` only for
`verified`, and `27` otherwise. A truncated, partial, substituted or
mid-read-modified blob is never verified.

### Loopback-only Ollama in container mode

In a container, every LLM stage's `ollama_url` must be a literal loopback
address (`localhost`, `127.0.0.0/8` or `[::1]`), whatever the stage's
provider. The Ollama provider is labelled local, so its endpoint must actually
be local. Two layers enforce this:

- **At startup**, a config with a non-loopback stage is refused. The refusal
  names the stage, never the URL. **This deliberately breaks a self-hosted
  container that points at a LAN Ollama.** Run the runtime inside the
  container's loopback instead.
- **At dial time**, the serving process sets `CREEK_OLLAMA_LOOPBACK_ONLY=1`,
  so a vault-config edit after boot cannot reach a remote host. Reflection
  also refuses unless a model package is configured and the stage resolves to
  its pinned model at its pinned digest over loopback. Without one, reflection
  in a container is unavailable rather than served by an unpinned or cloud
  model.

## Backup, restore, and deletion

Stop the container before taking a provider volume snapshot. Restore into a new
encrypted volume, attach it at `/vault`, mount the same secret files, and start
the same recorded image digest. The existing config makes this an ordinary
restart, not a first boot. Verify `--check ready` before routing traffic.

Deletion is a lifecycle operation owned by the control plane: stop traffic,
destroy the container, destroy the user's volume and snapshots, and revoke the
consumer credential. Provider backup and restore is the only ordinary-Fly MVP
recovery mechanism; there is no user recovery ceremony or reset promise.

## Executable contract

`scripts/container-contract.sh` builds on these instructions in CI. It boots
two containers with two volumes and two consumer secrets, writes through real
`/v1`, restarts one, verifies content and credential persistence, proves
cross-volume isolation, exercises missing-volume and missing-config refusals,
checks each health state, and scans image history, process arguments, logs, and
the vault corpus for the test credentials.
