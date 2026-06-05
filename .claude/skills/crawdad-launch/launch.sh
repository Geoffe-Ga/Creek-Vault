#!/usr/bin/env bash
# Preflight + config generation for launching CrawDad against a chosen vault.
# Does everything EXCEPT the final `crawdad run` — prints the exact launch
# command on success so the caller can start the bot in the background.
#
# Usage: launch.sh <vault-dir>
set -euo pipefail

VAULT="${1:-}"
if [[ -z "$VAULT" ]]; then
  echo "ERROR: no vault directory given. Usage: launch.sh <vault-dir>" >&2
  exit 2
fi

# Resolve to an absolute, real path (CrawDad/MCP need absolute paths).
VAULT="$(cd "$VAULT" 2>/dev/null && pwd)" || {
  echo "ERROR: vault directory does not exist: $1" >&2
  exit 2
}

# Repo layout, derived from this script's location:
#   <repo>/.claude/skills/crawdad-launch/launch.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CREEK_PROJECT="$REPO/creek-tools"      # the Python subproject (uv project)
CRAWDAD_DIR="$REPO/crawdad"            # CrawDad lives beside creek-tools
META="$VAULT/00-Creek-Meta"
VAULT_CONFIG="$META/creek_config.yaml"
CONFIG_OUT="$CRAWDAD_DIR/crawdad.yaml"

fail() { echo "ERROR: $*" >&2; exit 1; }

echo "== CrawDad launch preflight =="
echo "vault:         $VAULT"
echo "creek project: $CREEK_PROJECT"
echo "crawdad dir:   $CRAWDAD_DIR"
echo

# 1. Vault must be a Creek vault.
[[ -d "$META" ]]            || fail "not a Creek vault — missing $META (run 'creek init --vault $VAULT')"
[[ -f "$VAULT_CONFIG" ]]   || fail "missing vault config $VAULT_CONFIG (run 'creek init --vault $VAULT')"
[[ -d "$CRAWDAD_DIR" ]]    || fail "cannot find CrawDad project at $CRAWDAD_DIR"

# 2. Secrets must be in the environment (CrawDad refuses to start without them).
[[ -n "${DISCORD_BOT_TOKEN:-}" ]] || fail "DISCORD_BOT_TOKEN is not set in the environment"
[[ -n "${ANTHROPIC_API_KEY:-}" ]] || fail "ANTHROPIC_API_KEY is not set in the environment"
echo "[ok] DISCORD_BOT_TOKEN and ANTHROPIC_API_KEY present"

# 3. vault_path in the vault config MUST be absolute, or every MCP tool reads
#    the wrong directory (resolved against the MCP process cwd, not the config).
VP="$(sed -n 's/^vault_path:[[:space:]]*//p' "$VAULT_CONFIG" | head -1 | tr -d '"'"'"' ')"
if [[ "$VP" != /* ]]; then
  echo "[fix] vault_path in $VAULT_CONFIG is '$VP' (relative) — rewriting to absolute"
  # Portable in-place edit: GNU sed needs no suffix, BSD/macOS sed requires one.
  # `-i.bak` works on both; the .bak also serves as a backup of the prior config.
  sed -i.bak "s|^vault_path:.*|vault_path: $VAULT|" "$VAULT_CONFIG" \
    && rm -f "${VAULT_CONFIG}.bak"
else
  echo "[ok] vault_path is absolute ($VP)"
fi

# 4. State/latest.md unblocks free-text replies and session-state load.
if [[ -f "$META/State/latest.md" ]]; then
  echo "[ok] State/latest.md present"
else
  echo "[fix] State/latest.md missing — generating with 'creek state'"
  ( cd "$CREEK_PROJECT" && \
    CREEK_CONFIG="$VAULT_CONFIG" uv run creek state --vault "$VAULT" >/dev/null ) \
    || echo "[warn] 'creek state' failed; the bot still runs but free-text may be limited"
fi

# 5. Voice-core skill: CrawDad's skill_loader reads creek-skills/voice-core/SKILL.md
#    (bug #538), but 'creek skills generate' writes meta/voice-core.SKILL.md.
#    Mirror it so replies sound like the vault owner, not a generic assistant.
VC_DIR="$VAULT/creek-skills/voice-core"
if [[ -f "$VC_DIR/SKILL.md" ]]; then
  echo "[ok] creek-skills/voice-core/SKILL.md present"
elif [[ -f "$VAULT/creek-skills/meta/voice-core.SKILL.md" ]]; then
  echo "[fix] mirroring meta/voice-core.SKILL.md -> voice-core/SKILL.md (#538 workaround)"
  mkdir -p "$VC_DIR"
  cp "$VAULT/creek-skills/meta/voice-core.SKILL.md" "$VC_DIR/SKILL.md"
else
  echo "[warn] no voice-core skill found — replies will use a generic voice."
  echo "       build one with: (cd $CREEK_PROJECT && uv run creek skills generate --vault $VAULT)"
fi

# 6. Preserve existing Discord allowlists if a config is already present.
#    Read ALL numeric ids under each key (not just the first), validate each is
#    a snowflake, and fall back to the known single-user defaults when empty.
#    NOTE: these defaults are the repo author's own Discord ids — replace them
#    with your own user/channel ids when running a different deployment.
DEFAULT_USER="579188864804192268"
DEFAULT_CHANNEL="860198841784205325"

# read_list <file> <key> -> one numeric id per line for that YAML sequence
read_list() {
  awk -v k="$2:" '
    $0 ~ "^"k"[[:space:]]*$" {f=1; next}   # enter the target sequence
    /^[^[:space:]-]/         {f=0}          # any new top-level key ends it
    f && /^[[:space:]]*-/    {gsub(/[^0-9]/, ""); if (length($0)) print}
  ' "$1"
}

USER_IDS=()
CHAN_IDS=()
if [[ -f "$CONFIG_OUT" ]]; then
  while IFS= read -r id; do [[ "$id" =~ ^[0-9]+$ ]] && USER_IDS+=("$id"); done \
    < <(read_list "$CONFIG_OUT" allowed_user_ids)
  while IFS= read -r id; do [[ "$id" =~ ^[0-9]+$ ]] && CHAN_IDS+=("$id"); done \
    < <(read_list "$CONFIG_OUT" allowed_channel_ids)
fi
[[ ${#USER_IDS[@]} -eq 0 ]] && USER_IDS=("$DEFAULT_USER")
[[ ${#CHAN_IDS[@]} -eq 0 ]] && CHAN_IDS=("$DEFAULT_CHANNEL")
echo "[ok] allowlist: ${#USER_IDS[@]} user(s), ${#CHAN_IDS[@]} channel(s)"

# 7. Write the launch config. mcp_server_command must point at the creek-tools
#    project (absolute) and the vault's own config. Path values are single-quoted
#    so a path containing YAML-reserved chars (: # & *) can't corrupt the config
#    or inject into the executed mcp_server_command argv.
{
  printf "vault_path: '%s'\n" "$VAULT"
  printf "mcp_server_command:\n"
  printf "  - uv\n  - run\n  - --project\n"
  printf "  - '%s'\n" "$CREEK_PROJECT"
  printf "  - creek-tools-mcp\n  - --config\n"
  printf "  - '%s'\n" "$VAULT_CONFIG"
  printf "allowed_user_ids:\n"
  for id in "${USER_IDS[@]}"; do printf "  - %s\n" "$id"; done
  printf "allowed_channel_ids:\n"
  for id in "${CHAN_IDS[@]}"; do printf "  - %s\n" "$id"; done
} > "$CONFIG_OUT"
echo "[ok] wrote $CONFIG_OUT"

echo
echo "READY"
echo "Launch with:"
echo "  cd $CRAWDAD_DIR && uv run crawdad run --config $CONFIG_OUT"
