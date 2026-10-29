#!/usr/bin/env bash
# Connect a folder (NAS share, external drive, local or iCloud folder) to the
# indexer as read-only block storage, in one command. See docs/INDEXER.md,
# "Connected block storage".
#
#   ./add-folder.sh smb://nas.local/Projects        mount the share, then add it
#   ./add-folder.sh /Volumes/Projects --name Studio  add a mounted folder as "Studio"
#   ./add-folder.sh --list                           show the six slots
#   ./add-folder.sh --remove Studio                  free that slot
#
# Options: --name NAME (shown in the app; default: the folder's name),
#          --yes (don't ask), --no-restart (only edit .env).
# It edits BLOCK_SOURCE_PATH[_N] and BLOCK_SOURCES in .env (backing it up first)
# and recreates only the indexer container. Runs on macOS's bash 3.2.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${ENV_FILE:-.env}"
DOCKER="${DOCKER:-docker}"
OPEN_CMD="${OPEN_CMD:-open}"
VOLUMES_DIR="${VOLUMES_DIR:-/Volumes}"
MOUNT_WAIT_SEC="${MOUNT_WAIT_SEC:-60}"
SLOTS="1 2 3 4 5 6"

die() { echo "Error: $*" >&2; exit 1; }
say() { echo "$*"; }

# ---------------------------------------------------------------- .env access
slot_var() { if [ "$1" = 1 ]; then echo BLOCK_SOURCE_PATH; else echo "BLOCK_SOURCE_PATH_$1"; fi; }

env_get() {   # last KEY=value line wins, as with docker compose; surrounding quotes removed
  [ -f "$ENV_FILE" ] || return 0
  awk -v k="$1" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2) } END {
      if (v ~ /^".*"$/ || v ~ /^'"'"'.*'"'"'$/) v = substr(v, 2, length(v) - 2); printf "%s", v }' "$ENV_FILE"
}

env_set() {   # replace every KEY= line with one, or append; keeps the file's permissions
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ]; then
    awk -v k="$1" -v line="$key=$value" 'index($0, k "=") == 1 { if (!done) print line; done = 1; next }
      { print } END { if (!done) print line }' "$ENV_FILE" > "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" > "$tmp"
    : > "$ENV_FILE"; chmod 600 "$ENV_FILE"
  fi
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

backup_env() {
  [ -f "$ENV_FILE" ] || return 0
  local b; b="$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
  cp -p "$ENV_FILE" "$b"; chmod 600 "$b"
  say "Backed up $ENV_FILE to $b"
}

# BLOCK_SOURCES entries, one per line: name<TAB>container path
entries() { env_get BLOCK_SOURCES | tr ';' '\n' | awk -F= 'NF >= 2 && $1 != "" {
    n = $1; sub(/^[ \t]+/, "", n); sub(/[ \t]+$/, "", n); p = substr($0, length($1) + 2); gsub(/[ \t]/, "", p);
    print n "\t" p }'; }

slot_of_entry() { case "$1" in /block/source[1-6]) echo "${1#/block/source}" ;; *) echo "" ;; esac; }

phys() { (cd "$1" 2>/dev/null && pwd -P); }   # physical path, symlinks resolved

# ---------------------------------------------------------------- commands
list() {
  local n var path name
  say "Block storage slots ($ENV_FILE):"
  for n in $SLOTS; do
    var="$(slot_var "$n")"; path="$(env_get "$var")"
    name="$(entries | awk -F'\t' -v p="/block/source$n" '$2 == p { print $1; exit }')"
    if [ -n "$path" ]; then say "  $n  ${name:-(no name in BLOCK_SOURCES)}  ->  $path"
    else say "  $n  free"; fi
  done
}

mount_url() {   # smb://host/share[/sub/path] -> mounted folder path on stdout
  local url="$1" rest share sub before name i
  rest="${url#*://}"; rest="${rest#*@}"; rest="${rest#*/}"
  share="${rest%%/*}"; sub=""; [ "$rest" != "$share" ] && sub="${rest#*/}"
  [ -n "$share" ] || die "no share name in $url (expected smb://host/share)"
  share="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' "$share")"
  sub="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' "$sub")"
  if [ -d "$VOLUMES_DIR/$share" ] && [ -n "$(ls -A "$VOLUMES_DIR/$share" 2>/dev/null)" ]; then
    echo >&2 "$share is already mounted at $VOLUMES_DIR/$share"
    name="$share"
  else
    before="$(ls -1 "$VOLUMES_DIR" 2>/dev/null || true)"
    echo >&2 "Mounting $url (macOS may ask you to sign in to the server)..."
    "$OPEN_CMD" "$url" || die "could not open $url"
    name=""
    i=0
    while [ "$i" -lt "$MOUNT_WAIT_SEC" ]; do
      if [ -d "$VOLUMES_DIR/$share" ] && [ -n "$(ls -A "$VOLUMES_DIR/$share" 2>/dev/null)" ]; then name="$share"; break; fi
      # A second mount of the same share name appears as "share-1".
      name="$(ls -1 "$VOLUMES_DIR" 2>/dev/null | grep -Fxv -f <(printf '%s\n' "$before") | grep -F "$share" | head -n 1 || true)"
      [ -n "$name" ] && break
      sleep 1; i=$((i + 1))
    done
    [ -n "$name" ] || die "$share didn't appear in $VOLUMES_DIR within ${MOUNT_WAIT_SEC}s. Mount it in Finder (Go > Connect to Server), then run this again with the /Volumes path."
  fi
  if [ -n "$sub" ]; then echo "$VOLUMES_DIR/$name/$sub"; else echo "$VOLUMES_DIR/$name"; fi
}

default_name() {
  local base
  case "$1" in *"com~apple~CloudDocs"*) [ "$(basename "$1")" = "com~apple~CloudDocs" ] && { echo iCloud; return; } ;; esac
  base="$(basename "$1")"
  printf '%s' "$base" | tr -c 'A-Za-z0-9._-' '-' | sed -e 's/--*/-/g' -e 's/^-//' -e 's/-$//'
}

confirm() {
  [ "$YES" = 1 ] && return 0
  [ -t 0 ] || die "not a terminal; add --yes to go ahead without asking"
  printf '%s [y/N] ' "$1"; read -r reply
  case "$reply" in y|Y|yes|YES) return 0 ;; *) say "Nothing changed."; exit 0 ;; esac
}

restart_indexer() {
  if [ "$RESTART" = 0 ]; then say "Skipped the restart (--no-restart). Run ./start.sh to apply."; return; fi
  say "Recreating the indexer with the new folders..."
  "$DOCKER" compose up -d lucidlink-indexer >/dev/null
  local i=0 health=""
  while [ "$i" -lt 45 ]; do
    health="$("$DOCKER" inspect -f '{{.State.Health.Status}}' lucidlink-indexer 2>/dev/null || true)"
    [ "$health" = healthy ] && break
    sleep 2; i=$((i + 1))
  done
  [ "$health" = healthy ] || say "The indexer isn't healthy yet (status: ${health:-unknown}). Check: docker logs lucidlink-indexer"
}

verify() {   # what the indexer itself says about this source
  local name="$1" slot="$2" seen
  [ "$RESTART" = 1 ] || return 0
  seen="$("$DOCKER" exec lucidlink-indexer sh -c "ls -A /block/source$slot 2>/dev/null | head -n 3" || true)"
  if [ -z "$seen" ]; then
    say "Warning: the indexer sees an empty folder in slot $slot. Is the share still mounted? Does OrbStack have Full Disk Access?"
    return 0
  fi
  "$DOCKER" exec -e NAME="$name" lucidlink-indexer python -c '
import json, os, urllib.request
req = urllib.request.Request("http://localhost:8100/index/status",
                             headers={"X-Internal-Key": os.environ.get("INDEXER_INTERNAL_KEY", "")})
try:
    status = json.load(urllib.request.urlopen(req, timeout=10))
except Exception as e:
    print("Could not read the indexer status yet (%s); check Engine > Indexing in the app." % e)
    raise SystemExit(0)
for b in status.get("blockSources", []):
    if b.get("name") == os.environ["NAME"]:
        if b.get("accepted"):
            print("The indexer accepted %s; indexing starts within a minute (Engine > Indexing)." % b["name"])
        else:
            print("The indexer refused %s: %s" % (b["name"], b.get("error")))
        break
else:
    print("The indexer does not list %s yet; check Engine > Indexing in the app." % os.environ["NAME"])
' || true
}

add() {
  local target="$1" folder n var path free="" name real other_real ename epath
  case "$target" in
    smb://*|afp://*|nfs://*) folder="$(mount_url "$target")" ;;
    *) folder="${target/#\~/$HOME}" ;;
  esac
  [ -d "$folder" ] || die "$folder is not a folder (or isn't mounted)"
  [ -r "$folder" ] || die "$folder isn't readable by $(whoami)"
  real="$(phys "$folder")"; [ -n "$real" ] || die "cannot open $folder"

  name="${NAME:-$(default_name "$real")}"
  [ -n "$name" ] || die "couldn't make a name from $folder; pass --name"
  case "$name" in *[\;=/]*) die "the name can't contain ; = or /" ;; esac
  while IFS="$(printf '\t')" read -r ename epath; do
    [ "$ename" = "$name" ] && die "a source called '$name' already exists ($epath); pick another with --name"
  done < <(entries)

  for n in $SLOTS; do
    var="$(slot_var "$n")"; path="$(env_get "$var")"
    if [ -z "$path" ]; then [ -z "$free" ] && free="$n"; continue; fi
    other_real="$(phys "$path" || true)"; [ -n "$other_real" ] || other_real="$path"
    [ "$other_real" = "$real" ] && die "$folder is already connected in slot $n"
    case "$real/" in "$other_real"/*) die "$folder is inside slot $n ($path), which is already indexed" ;; esac
    case "$other_real/" in "$real"/*) die "slot $n ($path) is inside $folder; remove it first or pick a narrower folder" ;; esac
  done
  [ -n "$free" ] || die "all six slots are in use (see --list); free one with --remove NAME"

  say ""
  say "Connect $folder as \"$name\""
  say "  slot $free, read-only: the indexer can read it but never write, move or delete"
  say "  it appears in Engine > Indexing and in search and Ask across all filespaces"
  say "  (a name that matches a LucidLink filespace is refused by the indexer)"
  confirm "Go ahead?"

  backup_env
  env_set "$(slot_var "$free")" "$real"
  local sources; sources="$(env_get BLOCK_SOURCES)"
  sources="${sources%;}"; [ -n "$sources" ] && sources="$sources;"
  env_set BLOCK_SOURCES "$sources$name=/block/source$free"
  say "Saved to $ENV_FILE."
  restart_indexer
  verify "$name" "$free"
}

remove() {
  local name="$1" epath slot kept=""
  epath="$(entries | awk -F'\t' -v n="$name" '$1 == n { print $2; exit }')"
  [ -n "$epath" ] || die "no source called '$name' (see --list)"
  slot="$(slot_of_entry "$epath")"
  say "Disconnect \"$name\" ($epath${slot:+, slot $slot}). Nothing on the folder itself is touched."
  confirm "Go ahead?"
  backup_env
  while IFS="$(printf '\t')" read -r n p; do
    [ "$n" = "$name" ] && continue
    kept="${kept:+$kept;}$n=$p"
  done < <(entries)
  env_set BLOCK_SOURCES "$kept"
  [ -n "$slot" ] && env_set "$(slot_var "$slot")" ""
  say "Saved to $ENV_FILE."
  restart_indexer
  say "\"$name\" is no longer indexed. Results already indexed from it may still show in search until the index is cleared."
}

# ---------------------------------------------------------------- arguments
NAME=""; YES=0; RESTART=1; MODE=""; ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) MODE=list ;;
    --remove) MODE=remove; shift; [ $# -gt 0 ] || die "--remove needs a name"; ARG="$1" ;;
    --name) shift; [ $# -gt 0 ] || die "--name needs a value"; NAME="$1" ;;
    --yes|-y) YES=1 ;;
    --no-restart) RESTART=0 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) [ -z "$ARG" ] || die "one folder at a time"; ARG="$1"; MODE="${MODE:-add}" ;;
  esac
  shift
done

case "$MODE" in
  list) list ;;
  remove) remove "$ARG" ;;
  add) add "$ARG" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
