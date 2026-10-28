#!/usr/bin/env bash
# =============================================================================
#  OWNER-INSTALLED. Nothing in the engine runs this. Install it on the Mini
#  (AI-Factory-Mini) with the launchd agent in docs/UPDATES.md.
# =============================================================================
#
# Pull-and-redeploy for the LucidLink engine. Every few minutes (launchd):
#
#   1. fetch origin/main; exit quietly if HEAD is already there
#   2. refuse to touch a dirty or diverged checkout (notify instead)
#   3. tag the images that are running now as lucidlink-rollback/<svc>:<old sha>
#   4. fast-forward, rebuild every built service, run docker/start.sh
#   5. health-check /health and /version (git_sha must be the new commit, sdk
#      must equal the lucidlink pin in docker/requirements.txt)
#   6. on failure: reset the checkout to the old commit, put the old images back
#      under their compose names, `up -d --no-build`, remember the bad commit so
#      it is not retried every 5 minutes, and notify
#
# Renovate + CI decide *what* lands on main (renovate.json); this only decides
# whether the Mini can run it.
#
# Env (all optional; also read from $STATE_DIR/env if present):
#   REPO_DIR        checkout to deploy        (default: the repo holding this script)
#   BRANCH, REMOTE  what to track             (default: main, origin)
#   HEALTH_URL      file service base URL     (default: http://127.0.0.1:8000)
#   HEALTH_TIMEOUT  seconds to wait healthy   (default: 180)
#   STATE_DIR       lock, bad-sha, logs       (default: ~/.lucidlink-redeploy)
#   NTFY_URL, NTFY_TOPIC, NTFY_TOKEN   push a note on deploy / failure
#   DRY_RUN=1       report what would happen, change nothing
#
# The body is one function, called on the last line: bash then parses the
# whole script before running it, so the `git merge` below can rewrite this
# file safely while it runs.

main() {
  set -euo pipefail

  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  STATE_DIR="${STATE_DIR:-$HOME/.lucidlink-redeploy}"
  mkdir -p "$STATE_DIR"
  # shellcheck disable=SC1091
  [ -f "$STATE_DIR/env" ] && . "$STATE_DIR/env"

  REPO_DIR="${REPO_DIR:-$(cd "$script_dir/../.." && pwd)}"
  BRANCH="${BRANCH:-main}"
  REMOTE="${REMOTE:-origin}"
  HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000}"
  HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
  DRY_RUN="${DRY_RUN:-0}"
  # launchd starts with a bare PATH; OrbStack's docker is not on it.
  export PATH="$HOME/.orbstack/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

  local project="lucidlink"              # `name:` in docker/docker-compose.yml
  local built_services=(lucidlink-fileservice lucidlink-indexer)

  # One run at a time (mkdir is atomic; a stale lock older than 1h is cleared).
  # Global, not local: the EXIT trap runs after main's locals are gone.
  LOCK_DIR="$STATE_DIR/lock"
  local lock="$LOCK_DIR"
  if ! mkdir "$lock" 2>/dev/null; then
    if [ -n "$(find "$lock" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
      rmdir "$lock" 2>/dev/null || true
      mkdir "$lock"
    else
      exit 0
    fi
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

  cd "$REPO_DIR"
  git fetch --quiet "$REMOTE" "$BRANCH"
  local prev target
  prev="$(git rev-parse HEAD)"
  target="$(git rev-parse "$REMOTE/$BRANCH")"
  [ "$prev" = "$target" ] && exit 0

  if [ -f "$STATE_DIR/bad-sha" ] && [ "$(cat "$STATE_DIR/bad-sha")" = "$target" ]; then
    log "skip: $target failed before; push a fix (or rm $STATE_DIR/bad-sha) to retry"
    exit 0
  fi
  if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
    fail_without_change "checkout is on $(git rev-parse --abbrev-ref HEAD), not $BRANCH"
  fi
  if ! git diff --quiet || ! git diff --cached --quiet; then
    fail_without_change "uncommitted changes in $REPO_DIR"
  fi
  if ! git merge-base --is-ancestor "$prev" "$target"; then
    fail_without_change "local $BRANCH has diverged from $REMOTE/$BRANCH"
  fi

  local prev_short new_short
  prev_short="$(git rev-parse --short "$prev")"
  new_short="$(git rev-parse --short "$target")"
  log "deploying $prev_short -> $new_short"
  git log --oneline "$prev..$target" | sed 's/^/  /' | tee -a "$STATE_DIR/redeploy.log"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN: stopping before any change"
    exit 0
  fi

  # Snapshot what is running now so it can be put back without a rebuild.
  local svc id
  for svc in "${built_services[@]}"; do
    id="$(cd docker && docker compose images -q "$svc" 2>/dev/null | head -1 || true)"
    if [ -n "$id" ]; then
      docker image tag "$id" "lucidlink-rollback/$svc:$prev_short"
    fi
  done

  git merge --ff-only --quiet "$target"
  export GIT_SHA="$new_short"
  local ok=1
  # start.sh rebuilds only the file service; the indexer shares the Dockerfile
  # and the SDK pin, so build every built service first.
  if ! (cd docker && docker compose build --quiet "${built_services[@]}" && ./start.sh >>"$STATE_DIR/redeploy.log" 2>&1); then
    log "build/start failed"
    ok=0
  fi
  if [ "$ok" = "1" ] && ! wait_healthy "$new_short"; then
    ok=0
  fi

  if [ "$ok" = "1" ]; then
    rm -f "$STATE_DIR/bad-sha"
    echo "$target" >"$STATE_DIR/last-good"
    prune_rollback_tags
    notify "LucidLink engine deployed $new_short" "Was $prev_short. $(git log -1 --format=%s "$target")" default
    log "deployed $new_short"
    exit 0
  fi

  # ---- rollback --------------------------------------------------------------
  log "rolling back to $prev_short"
  echo "$target" >"$STATE_DIR/bad-sha"
  git reset --hard --quiet "$prev"
  for svc in "${built_services[@]}"; do
    if docker image inspect "lucidlink-rollback/$svc:$prev_short" >/dev/null 2>&1; then
      docker image tag "lucidlink-rollback/$svc:$prev_short" "$project-$svc:latest"
    fi
  done
  export GIT_SHA="$prev_short"
  # The compose file is back at the old commit, so the lucidlink-api digest
  # rolls back too (the old image is still in the local cache).
  (cd docker && docker compose up -d --no-build >>"$STATE_DIR/redeploy.log" 2>&1) || true
  if wait_healthy "$prev_short"; then
    notify "LucidLink deploy FAILED; rolled back" "$new_short failed health checks; running $prev_short again. Log: $STATE_DIR/redeploy.log" high
  else
    notify "LucidLink deploy FAILED and rollback unhealthy" "$new_short failed, and $prev_short did not come back healthy. Check the Mini now." urgent
  fi
  exit 1
}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$STATE_DIR/redeploy.log" >&2; }

notify() {  # title body priority(default|high|urgent)
  [ -n "${NTFY_URL:-}" ] && [ -n "${NTFY_TOPIC:-}" ] || return 0
  local prio=3
  case "$3" in high) prio=4 ;; urgent) prio=5 ;; esac
  # No arrays: macOS /bin/bash 3.2 treats an empty "${a[@]}" as unbound under set -u.
  local auth_header="X-No-Auth: 1"
  [ -n "${NTFY_TOKEN:-}" ] && auth_header="Authorization: Bearer $NTFY_TOKEN"
  curl -fsS -m 15 -H "$auth_header" -H "Title: $1" -H "Priority: $prio" -H "Tags: package" \
    -d "$2" "${NTFY_URL%/}/$NTFY_TOPIC" >/dev/null 2>&1 || log "ntfy notify failed"
}

fail_without_change() {
  log "not deploying: $1"
  notify "LucidLink redeploy blocked" "$1" high
  exit 1
}

wait_healthy() {  # expected short sha
  local want_sha="$1" want_sdk deadline body
  want_sdk="$(sed -n 's/^lucidlink==\([^[:space:]#;]*\).*/\1/p' "$REPO_DIR/docker/requirements.txt" | head -1)"
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 5 "$HEALTH_URL/health" >/dev/null 2>&1 \
       && body="$(curl -fsS -m 5 "$HEALTH_URL/version" 2>/dev/null)" \
       && WANT_SHA="$want_sha" WANT_SDK="$want_sdk" python3 -c '
import json, os, sys
v = json.loads(sys.argv[1])
ok = v.get("git_sha") == os.environ["WANT_SHA"] and (not os.environ["WANT_SDK"] or v.get("sdk") == os.environ["WANT_SDK"])
sys.exit(0 if ok else 1)' "$body"; then
      log "healthy: git_sha=$want_sha sdk=${want_sdk:-?}"
      return 0
    fi
    sleep 5
  done
  log "unhealthy after ${HEALTH_TIMEOUT}s (want git_sha=$want_sha sdk=${want_sdk:-?}); last /version: ${body:-none}"
  return 1
}

prune_rollback_tags() {  # keep the 3 newest rollback tags per service
  local svc
  for svc in lucidlink-fileservice lucidlink-indexer; do
    docker image ls "lucidlink-rollback/$svc" --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}' 2>/dev/null \
      | sort -r | tail -n +4 | cut -f2 \
      | while read -r ref; do docker image rm "$ref" >/dev/null 2>&1 || true; done
  done
}

main "$@"
