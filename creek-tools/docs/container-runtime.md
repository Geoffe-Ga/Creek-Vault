# Single-vault container runtime

The repository root `Dockerfile` packages exactly one `creek-tools-api`
process for exactly one consumer identity and one durable vault. The image is
disposable. Every writable Creek artifact—configuration, staged input, audit
state, and vault content—lives below the explicitly mounted `/vault` tree.

This runtime is the storage and API primitive selected by ADR-0013. It does not
implement the user-held volume-key ceremony or recovery key flow tracked by
#1771. Until that ceremony lands, the mounted provider volume must supply
encryption at rest. Do not describe the current image as operator-blind or
no-escrow encryption: the container enforces placement and isolation, while
the later key ceremony supplies that stronger property.

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

## Backup, restore, and deletion

Stop the container before taking a provider volume snapshot. Restore into a new
encrypted volume, attach it at `/vault`, mount the same secret files, and start
the same recorded image digest. The existing config makes this an ordinary
restart, not a first boot. Verify `--check ready` before routing traffic.

Deletion is a lifecycle operation owned by the control plane: stop traffic,
destroy the container, destroy the user's volume and snapshots, and revoke the
consumer credential. Once #1771 supplies no-escrow keys, losing both the
passphrase and recovery key is intentionally unrecoverable.

## Executable contract

`scripts/container-contract.sh` builds on these instructions in CI. It boots
two containers with two volumes and two consumer secrets, writes through real
`/v1`, restarts one, verifies content and credential persistence, proves
cross-volume isolation, exercises missing-volume and missing-config refusals,
checks each health state, and scans image history, process arguments, logs, and
the vault corpus for the test credentials.
