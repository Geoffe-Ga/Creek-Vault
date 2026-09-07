#!/usr/bin/env bash
# Exercise the built single-vault image against real Docker (#1772).

set -euo pipefail

IMAGE="${1:-creek-vault:contract}"
TOKEN_A="contract-a-token-000000000000000000000000000000000001"
TOKEN_B="contract-b-token-000000000000000000000000000000000002"
BAD_TOKEN="contract-bad-token-0000000000000000000000000000000003"
NAME_A="creek-contract-a-$$"
NAME_B="creek-contract-b-$$"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/creek-container-contract.XXXXXX")"

cleanup() {
    docker rm --force "$NAME_A" "$NAME_B" >/dev/null 2>&1 || true
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

fail() {
    echo "container contract failed: $1" >&2
    exit 1
}

make_inputs() {
    local identity="$1"
    local token="$2"
    local root="$TMP_ROOT/$identity"
    mkdir -p "$root/vault" "$root/secrets"
    chmod 0777 "$root/vault"
    printf '%s=%s\n' "$identity" "$token" > "$root/secrets/creek_consumer_tokens"
    printf 'adepthood=%s\n' "$BAD_TOKEN" > "$root/secrets/bad_tokens"
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$root/secrets/tls.key" \
        -out "$root/secrets/tls.crt" \
        -days 1 -subj '/CN=127.0.0.1' \
        -addext 'subjectAltName=IP:127.0.0.1' >/dev/null 2>&1
    chmod 0444 "$root/secrets"/*
}

start_container() {
    local name="$1"
    local identity="$2"
    local port="$3"
    docker run --detach --name "$name" \
        --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m \
        --security-opt no-new-privileges \
        --publish "127.0.0.1:${port}:8823" \
        --mount "type=bind,src=$TMP_ROOT/$identity/vault,dst=/vault" \
        --mount "type=bind,src=$TMP_ROOT/$identity/secrets,dst=/run/secrets,readonly" \
        "$IMAGE" >/dev/null
}

wait_healthy() {
    local name="$1"
    local status
    for _ in $(seq 1 60); do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name")"
        if [[ "$status" == "healthy" ]]; then
            return
        fi
        if [[ "$(docker inspect --format '{{.State.Status}}' "$name")" == "exited" ]]; then
            docker logs "$name" >&2
            fail "$name exited before becoming healthy"
        fi
        sleep 1
    done
    docker logs "$name" >&2
    fail "$name did not become healthy"
}

curl_config() {
    local identity="$1"
    local token="$2"
    local path="$TMP_ROOT/$identity/curl.conf"
    printf 'silent\nshow-error\nfail-with-body\nheader = "Authorization: Bearer %s"\n' \
        "$token" > "$path"
    printf '%s' "$path"
}

get_health() {
    local identity="$1"
    local token="$2"
    local port="$3"
    local config
    config="$(curl_config "$identity" "$token")"
    curl --config "$config" \
        --cacert "$TMP_ROOT/$identity/secrets/tls.crt" \
        "https://127.0.0.1:${port}/v1/health"
}

put_journal() {
    local identity="$1"
    local token="$2"
    local port="$3"
    local external_id="$4"
    local content="$5"
    local config
    config="$(curl_config "$identity" "$token")"
    curl --config "$config" \
        --cacert "$TMP_ROOT/$identity/secrets/tls.crt" \
        --request PUT \
        --header 'Content-Type: application/json' \
        --header 'X-Creek-Contract-Version: 0.15' \
        --header 'X-Creek-Tier-Ceiling: personal' \
        --data "{\"content\":\"$content\",\"tier\":\"personal\"}" \
        "https://127.0.0.1:${port}/v1/journal-entries/${external_id}"
}

assert_no_secret() {
    local where="$1"
    local body="$2"
    [[ "$body" != *"$TOKEN_A"* ]] || fail "$where contains consumer A credential"
    [[ "$body" != *"$TOKEN_B"* ]] || fail "$where contains consumer B credential"
}

make_inputs "adepthood-a" "$TOKEN_A"
make_inputs "adepthood-b" "$TOKEN_B"
start_container "$NAME_A" "adepthood-a" 18823
wait_healthy "$NAME_A"

[[ "$(get_health "adepthood-a" "$TOKEN_A" 18823)" == '{"status":"ok"}' ]] \
    || fail "first boot did not serve authenticated /v1 health"

response_a="$(put_journal "adepthood-a" "$TOKEN_A" 18823 \
    'container-contract-a' 'isolated journal sentinel alpha')"
[[ "$response_a" == *'"status":"ok"'* ]] || fail "consumer A journal write failed"
journal_a="$(find "$TMP_ROOT/adepthood-a/vault/01-Fragments/Journal" \
    -type f ! -name .gitkeep -print -quit)"
[[ -n "$journal_a" ]] || fail "consumer A journal did not reach the mounted vault"
hash_before="$(shasum -a 256 "$journal_a" | cut -d ' ' -f 1)"

docker restart "$NAME_A" >/dev/null
wait_healthy "$NAME_A"
hash_after="$(shasum -a 256 "$journal_a" | cut -d ' ' -f 1)"
[[ "$hash_after" == "$hash_before" ]] || fail "restart rewrote journal content"
[[ "$(get_health "adepthood-a" "$TOKEN_A" 18823)" == '{"status":"ok"}' ]] \
    || fail "restart did not preserve the mounted consumer credential"

start_container "$NAME_B" "adepthood-b" 18824
wait_healthy "$NAME_B"
response_b="$(put_journal "adepthood-b" "$TOKEN_B" 18824 \
    'container-contract-b' 'isolated journal sentinel beta')"
[[ "$response_b" == *'"status":"ok"'* ]] || fail "consumer B journal write failed"
grep -R -F -q 'isolated journal sentinel alpha' "$TMP_ROOT/adepthood-a/vault" \
    || fail "consumer A content is absent from volume A"
grep -R -F -q 'isolated journal sentinel beta' "$TMP_ROOT/adepthood-b/vault" \
    || fail "consumer B content is absent from volume B"
if grep -R -F -q 'isolated journal sentinel beta' "$TMP_ROOT/adepthood-a/vault"; then
    fail "consumer B content crossed into volume A"
fi
if grep -R -F -q 'isolated journal sentinel alpha' "$TMP_ROOT/adepthood-b/vault"; then
    fail "consumer A content crossed into volume B"
fi

[[ "$(docker exec "$NAME_A" python -m creek_mcp.container_health --check process)" == "process-up" ]] \
    || fail "process health state is not distinguishable"
[[ "$(docker exec "$NAME_A" python -m creek_mcp.container_health --check volume)" == "vault-mounted" ]] \
    || fail "volume health state is not distinguishable"
[[ "$(docker exec "$NAME_A" python -m creek_mcp.container_health --check ready)" == "v1-ready" ]] \
    || fail "v1 readiness state is not distinguishable"
set +e
bad_health="$(docker exec \
    --env CREEK_CONTAINER_CONSUMER_TOKENS_FILE=/run/secrets/bad_tokens \
    "$NAME_A" python -m creek_mcp.container_health --check ready)"
bad_health_code=$?
set -e
[[ "$bad_health_code" -eq 22 && "$bad_health" == "v1-unready" ]] \
    || fail "bad authenticated readiness did not report v1-unready"

set +e
missing_mount_log="$(docker run --rm \
    --mount "type=bind,src=$TMP_ROOT/adepthood-a/secrets,dst=/run/secrets,readonly" \
    "$IMAGE" 2>&1)"
missing_mount_code=$?
set -e
[[ "$missing_mount_code" -ne 0 && "$missing_mount_log" == *"explicitly mounted volume"* ]] \
    || fail "missing volume was not refused"
assert_no_secret "missing-volume log" "$missing_mount_log"

mkdir -p "$TMP_ROOT/missing-config/vault"
chmod 0777 "$TMP_ROOT/missing-config/vault"
printf 'operator bytes\n' > "$TMP_ROOT/missing-config/vault/keep.txt"
set +e
missing_config_log="$(docker run --rm \
    --mount "type=bind,src=$TMP_ROOT/missing-config/vault,dst=/vault" \
    --mount "type=bind,src=$TMP_ROOT/adepthood-a/secrets,dst=/run/secrets,readonly" \
    "$IMAGE" 2>&1)"
missing_config_code=$?
set -e
[[ "$missing_config_code" -ne 0 && "$missing_config_log" == *"missing config"* ]] \
    || fail "nonempty volume missing config was not refused"
[[ "$(cat "$TMP_ROOT/missing-config/vault/keep.txt")" == "operator bytes" ]] \
    || fail "missing-config refusal altered existing bytes"
[[ ! -e "$TMP_ROOT/missing-config/vault/00-Creek-Meta/creek_config.yaml" ]] \
    || fail "missing-config refusal generated a config"
assert_no_secret "missing-config log" "$missing_config_log"

metadata="$(docker inspect --format '{{json .Config.Env}} {{json .Config.Cmd}} {{json .Config.Entrypoint}}' "$IMAGE")"
history="$(docker history --no-trunc "$IMAGE")"
processes="$(docker top "$NAME_A" -eo pid,args)"
logs="$(docker logs "$NAME_A" 2>&1)"
assert_no_secret "image metadata" "$metadata"
assert_no_secret "image history" "$history"
assert_no_secret "container process arguments" "$processes"
assert_no_secret "container logs" "$logs"
if grep -R -F -q "$TOKEN_A" "$TMP_ROOT/adepthood-a/vault"; then
    fail "consumer A credential was written into its vault corpus"
fi
if grep -R -F -q "$TOKEN_B" "$TMP_ROOT/adepthood-b/vault"; then
    fail "consumer B credential was written into its vault corpus"
fi

digest="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
[[ "$digest" == sha256:* ]] || fail "image has no inspectable digest"
echo "container contract passed: $digest"
