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
  # Atomic, portable rewrite: write to a temp file then mv into place. Avoids the
  # GNU-vs-BSD `sed -i` suffix split, and the original survives if sed fails.
  TMP_CFG="$(mktemp)"
  sed "s|^vault_path:.*|vault_path: $VAULT|" "$VAULT_CONFIG" > "$TMP_CFG" \
    && mv "$TMP_CFG" "$VAULT_CONFIG"
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

# 6. Determine the Discord allowlists. Preferred source is the existing config
#    (preserve ALL numeric ids, not just the first); otherwise fall back to the
#    CRAWDAD_DEFAULT_USER / CRAWDAD_DEFAULT_CHANNEL env vars. No personal ids are
#    baked into this script — a fresh vault with no prior config must supply them.
DEFAULT_USER="${CRAWDAD_DEFAULT_USER:-}"
DEFAULT_CHANNEL="${CRAWDAD_DEFAULT_CHANNEL:-}"

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
if [[ ${#USER_IDS[@]} -eq 0 ]]; then
  [[ "$DEFAULT_USER" =~ ^[0-9]+$ ]] \
    || fail "no allowed_user_ids in $CONFIG_OUT and CRAWDAD_DEFAULT_USER is unset/invalid — export it as your Discord user id"
  USER_IDS=("$DEFAULT_USER")
fi
if [[ ${#CHAN_IDS[@]} -eq 0 ]]; then
  [[ "$DEFAULT_CHANNEL" =~ ^[0-9]+$ ]] \
    || fail "no allowed_channel_ids in $CONFIG_OUT and CRAWDAD_DEFAULT_CHANNEL is unset/invalid — export it as your Discord channel id"
  CHAN_IDS=("$DEFAULT_CHANNEL")
fi
echo "[ok] allowlist: ${#USER_IDS[@]} user(s), ${#CHAN_IDS[@]} channel(s)"

# 7. Build the MCP server command.
#
#    CrawDad spawns this per turn via the MCP stdio SDK, which (a) scrubs the
#    environment down to a safe allowlist — dropping ANTHROPIC_API_KEY, so
#    in-MCP cloud tools (draft/author/mine/classify) silently degrade — and
#    (b) inherits whatever start-up cost the command has. Spawning `uv run`
#    every turn is slow and contends on uv's project lock when the agent loop
#    fires several rounds, which surfaces as "could not start ... ('uv')".
#
#    Fix both by wrapping the command in a LOGIN shell that re-establishes the
#    user's exported secrets (ANTHROPIC_API_KEY) from their profile and sets
#    CREEK_ANTHROPIC_CONSENT=1, then `exec`s the creek-tools venv entry point
#    DIRECTLY (no `uv run`: deterministic, fast, no lock contention, and `exec`
#    hands stdio straight to the server so the MCP JSON-RPC stream stays clean).
#    Falls back to `uv run` if the venv binary isn't present yet.
# /bin/bash is the universal fallback (always present, POSIX `export`/`;`/`exec`);
# fish can't run the `export VAR=val; exec …` wrapper, so coerce it to bash too.
LOGIN_SHELL="${SHELL:-/bin/bash}"
if [[ "$LOGIN_SHELL" == *fish ]]; then
  echo "[warn] \$SHELL is fish, which can't run the POSIX MCP wrapper — using /bin/bash"
  LOGIN_SHELL=/bin/bash
fi
MCP_BIN="$CREEK_PROJECT/.venv/bin/creek-tools-mcp"

# The wrapper recovers ANTHROPIC_API_KEY by sourcing the LOGIN profile
# (~/.zprofile, ~/.zshenv, ~/.bash_profile, ~/.profile) — NOT an interactive rc
# (~/.zshrc, ~/.bashrc). Verify that actually works by probing the login shell
# under a scrubbed env (mimicking the MCP SDK, which drops the key): if the key
# only lives in an rc file, the wrapper can't recover it and cloud tools degrade.
if env -i HOME="$HOME" PATH="$PATH" TERM="${TERM:-xterm}" \
     "$LOGIN_SHELL" -lc 'printenv ANTHROPIC_API_KEY' >/dev/null 2>&1; then
  echo "[ok] $LOGIN_SHELL login profile exposes ANTHROPIC_API_KEY to the MCP wrapper"
else
  echo "[warn] $LOGIN_SHELL login profile does NOT export ANTHROPIC_API_KEY —"
  echo "       in-MCP cloud tools (draft/author/mine/classify) will degrade."
  echo "       Add 'export ANTHROPIC_API_KEY=…' to a LOGIN file (~/.zprofile,"
  echo "       ~/.zshenv or ~/.bash_profile), not ~/.zshrc / ~/.bashrc, then relaunch."
fi

# shq: POSIX shell single-quote (embedded ' becomes '\'') — safe argv for the -lc string.
shq() { local q="'" s="$1"; s="${s//$q/$q\\$q$q}"; printf '%s%s%s' "$q" "$s" "$q"; }
if [[ -x "$MCP_BIN" ]]; then
  echo "[ok] using direct MCP entry point $MCP_BIN"
  MCP_EXEC="exec $(shq "$MCP_BIN") --config $(shq "$VAULT_CONFIG")"
else
  echo "[warn] $MCP_BIN missing — falling back to 'uv run' (slower; run 'uv sync' in creek-tools)"
  MCP_EXEC="exec uv run --project $(shq "$CREEK_PROJECT") creek-tools-mcp --config $(shq "$VAULT_CONFIG")"
fi
MCP_INNER="export CREEK_ANTHROPIC_CONSENT=1; $MCP_EXEC"
echo "[ok] CREEK_ANTHROPIC_CONSENT=1 in MCP wrapper — cloud tools (draft/author/mine/classify) egress vault content to Anthropic on your key"

# 8. Write the launch config. Every scalar is a YAML single-quoted string (with
#    embedded single quotes doubled per spec) so a path with YAML-reserved chars
#    (: # & *) or a quote can't corrupt the config or inject into the argv.
# sq: YAML single-quoted scalar (embedded ' becomes '') — safe scalar for crawdad.yaml.
sq() { local q="'" s="$1"; s="${s//$q/$q$q}"; printf '%s%s%s' "$q" "$s" "$q"; }
{
  printf "vault_path: %s\n" "$(sq "$VAULT")"
  printf "mcp_server_command:\n"
  printf "  - %s\n" "$(sq "$LOGIN_SHELL")"
  printf "  - %s\n" "$(sq "-lc")"
  printf "  - %s\n" "$(sq "$MCP_INNER")"
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
