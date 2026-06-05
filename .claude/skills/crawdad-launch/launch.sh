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
  # macOS/BSD sed in-place
  sed -i '' "s|^vault_path:.*|vault_path: $VAULT|" "$VAULT_CONFIG"
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

# 6. Preserve existing Discord allowlists if a config is already present;
#    otherwise fall back to the known single-user defaults.
DEFAULT_USER="579188864804192268"
DEFAULT_CHANNEL="860198841784205325"
USER_ID="$DEFAULT_USER"
CHANNEL_ID="$DEFAULT_CHANNEL"
if [[ -f "$CONFIG_OUT" ]]; then
  EXIST_USER="$(awk '/^allowed_user_ids:/{f=1;next} /^[^[:space:]-]/{f=0} f && /-/{gsub(/[^0-9]/,"");print;exit}' "$CONFIG_OUT")"
  EXIST_CHAN="$(awk '/^allowed_channel_ids:/{f=1;next} /^[^[:space:]-]/{f=0} f && /-/{gsub(/[^0-9]/,"");print;exit}' "$CONFIG_OUT")"
  [[ -n "$EXIST_USER" ]] && USER_ID="$EXIST_USER"
  [[ -n "$EXIST_CHAN" ]] && CHANNEL_ID="$EXIST_CHAN"
fi
echo "[ok] allowlist: user=$USER_ID channel=$CHANNEL_ID"

# 7. Write the launch config. mcp_server_command must point at the creek-tools
#    project (absolute) and the vault's own config.
cat > "$CONFIG_OUT" <<YAML
vault_path: $VAULT
mcp_server_command:
  - uv
  - run
  - --project
  - $CREEK_PROJECT
  - creek-tools-mcp
  - --config
  - $VAULT_CONFIG
allowed_user_ids:
  - $USER_ID
allowed_channel_ids:
  - $CHANNEL_ID
YAML
echo "[ok] wrote $CONFIG_OUT"

echo
echo "READY"
echo "Launch with:"
echo "  cd $CRAWDAD_DIR && uv run crawdad run --config $CONFIG_OUT"
