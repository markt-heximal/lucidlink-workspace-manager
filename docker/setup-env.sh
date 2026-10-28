#!/usr/bin/env bash
# Fill in docker/.env for the vault, owner sign-in, indexer and update alerts.
#
# Run this ON THE MINI from the docker/ directory:   ./setup-env.sh
#   --check   only test that the configured endpoints answer
#
# Secrets are generated here, on the Mini, and written only to docker/.env
# (chmod 600). Nothing is printed except variable names, so they never end up
# in a terminal log, a chat or git. Existing non-empty values are never
# overwritten, so it is safe to re-run. Written for macOS's bash 3.2.
set -euo pipefail
cd "$(dirname "$0")"
# Every file this script writes (.env, its temp copy, backups) is owner-only.
umask 077

ENV_FILE=".env"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# Values that are not secret: the web app's Supabase project (publishable key,
# already shipped in the web app), the owner, and the Ruvector mirror.
SUPABASE_URL_DEFAULT="https://vqbnkxekmbyailcpxbjw.supabase.co"
SUPABASE_ANON_KEY_DEFAULT="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZxYm5reGVrbWJ5YWlsY3B4Ymp3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg2MTA0MDAsImV4cCI6MjA5NDE4NjQwMH0.FSRaE9PUZ1OaIKfjKwCf_nbTwqi0SZmZ8mKAj6ZMC-Q"
OWNER_EMAIL_DEFAULT="markt@heximal.com"
RUVECTOR_URL_DEFAULT="https://spark-47d8.tail333a1d.ts.net/ruvector"

get() {  # get NAME -> value in .env, or empty
  [ -f "$ENV_FILE" ] || { echo ""; return; }
  grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true
}

set_if_empty() {  # set_if_empty NAME VALUE
  local name="$1" value="$2" current
  current="$(get "$name")"
  if [ -n "$current" ]; then
    echo "  = $name (kept)"
    return
  fi
  if grep -qE "^$name=" "$ENV_FILE"; then
    # Replace the empty line in place without sed -i (differs on macOS).
    awk -v n="$name" -v v="$value" 'BEGIN{FS=OFS="="} $1==n{print n"="v; next} {print}' \
      "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
  fi
  echo "  + $name"
}

ask() {  # ask NAME PROMPT DEFAULT -> sets NAME if empty, using the answer
  local name="$1" prompt="$2" default="$3" answer
  if [ -n "$(get "$name")" ]; then
    echo "  = $name (kept)"
    return
  fi
  printf '  %s [%s]: ' "$prompt" "$default"
  read -r answer || true
  set_if_empty "$name" "${answer:-$default}"
}

rand_hex() { openssl rand -hex "$1"; }
# Fernet keys are 32 random bytes, url-safe base64 (44 chars with padding).
fernet_key() { openssl rand -base64 32 | tr '+/' '-_'; }

probe() {  # probe LABEL URL
  local code
  code="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "$2" || echo 000)"
  if [ "$code" = "000" ]; then
    echo "  ✗ $1 unreachable ($2)"
  else
    echo "  ✓ $1 answered HTTP $code"
  fi
}

check() {
  echo ""
  echo "Checking endpoints from this machine..."
  local embed ruv llm sup
  sup="$(get SUPABASE_URL)"; [ -n "$sup" ] && probe "Supabase" "$sup/auth/v1/health"
  embed="$(get EMBED_BASE_URL)"; [ -n "$embed" ] && probe "Embeddings" "$embed/models"
  llm="$(get LLM_BASE_URL)"; [ -n "$llm" ] && probe "LLM" "$llm/models"
  ruv="$(get RUVECTOR_URL)"; [ -n "$ruv" ] && probe "Ruvector" "$ruv/health"
  echo ""
  echo "Note: the containers reach these through OrbStack. If a check passes here"
  echo "but the indexer status shows it unreachable, use the host's LAN/tailnet IP."
}

if [ "$CHECK_ONLY" = 1 ]; then
  check
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl is required"; exit 1; }

if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
else
  cp .env.example "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

echo "=== Generated secrets ==="
set_if_empty VAULT_KEY "$(fernet_key)"
set_if_empty INDEXER_INTERNAL_KEY "$(rand_hex 32)"
# ntfy topics are public by name, so the name itself must be unguessable.
set_if_empty NTFY_TOPIC "lucidlink-$(rand_hex 12)"
set_if_empty NTFY_URL "https://ntfy.sh"

echo ""
echo "=== Owner sign-in and Ruvector ==="
set_if_empty SUPABASE_URL "$SUPABASE_URL_DEFAULT"
set_if_empty SUPABASE_ANON_KEY "$SUPABASE_ANON_KEY_DEFAULT"
set_if_empty OWNER_EMAIL "$OWNER_EMAIL_DEFAULT"
set_if_empty RUVECTOR_URL "$RUVECTOR_URL_DEFAULT"

echo ""
echo "=== Models (press Enter to accept the default) ==="
ask EMBED_BASE_URL "Embeddings endpoint (OpenAI-compatible, ends in /v1)" "http://spark-47d8.tail333a1d.ts.net:11434/v1"
ask EMBED_MODEL "Embedding model" "nomic-embed-text"
ask LLM_BASE_URL "Chat model endpoint for Ask and topics (ends in /v1)" "$(get EMBED_BASE_URL)"
ask LLM_MODEL "Chat model" "qwen2.5:32b"

# Optional: a copy of the vault key in the macOS keychain. Without VAULT_KEY
# the vault cannot be decrypted, and the tokens would have to be re-entered.
if command -v security >/dev/null 2>&1; then
  echo ""
  printf '  Save a backup of VAULT_KEY in your macOS keychain? [Y/n]: '
  read -r yn || true
  if [ "${yn:-Y}" != "n" ] && [ "${yn:-Y}" != "N" ]; then
    security add-generic-password -U -a "$USER" -s "lucidlink-vault-key" -w "$(get VAULT_KEY)" \
      && echo "  ✓ Saved as keychain item 'lucidlink-vault-key'"
  fi
fi

chmod 600 "$ENV_FILE"
echo ""
echo "=== Done: $ENV_FILE (chmod 600; previous copy saved as .env.bak-*) ==="
echo ""
echo "Your ntfy topic for update alerts (subscribe to it in the ntfy iPhone app):"
echo "  $(get NTFY_TOPIC)"
check
echo ""
echo "Next: ./start.sh"
