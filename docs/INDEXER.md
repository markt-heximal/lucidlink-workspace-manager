# Delta indexer, owner auth and token vault

Design and interface contract for pre-crawled Discover, semantic search and Ask.
This is a single-owner deployment: one person, several devices, several
LucidLink workspaces and filespaces.

## Why it lives here

The filespace is only reachable from the tailnet, so the browser used to walk it,
download every text file, chunk it and post the chunks to a Lovable Cloud edge
function on every Discover run. That re-read and re-embedded everything each
time, capped at 2,000 files, never pruned deleted files, and needed the browser
open. The indexer runs next to the file service on AI-Factory-Mini instead,
talks to LucidLink through the same Python SDK, and only does work for files
that changed.

## Containers

| Service | Port | Role |
|---|---|---|
| `lucidlink-api` | 127.0.0.1:3003 | LucidLink Management API (unchanged) |
| `lucidlink-fileservice` | 127.0.0.1:8000 | Public file API (gateway catch-all). Adds owner auth, `/vault/*`, and proxies `/index/*` to the indexer |
| `lucidlink-indexer` | internal 8100 only | Delta crawler, extraction, embeddings, search, ask, survey |

Both Python services use the same image (`docker/`), different commands. They
share the `lucidlink-data` volume mounted at `/data`:

- `/data/vault.db` — encrypted settings (owned by the file service, read by the indexer)
- `/data/index.db` — ledger, chunks and embeddings (owned by the indexer)

The indexer has its own `lucidlink.Client`, so a crawl never waits on the file
service's global `_registry_lock` and UI requests never wait on a crawl.

## Environment

| Variable | Used by | Meaning |
|---|---|---|
| `VAULT_KEY` | both | Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). Required for the vault. |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | fileservice | Used to verify the owner's Supabase session (`GET /auth/v1/user`). |
| `OWNER_EMAIL` and/or `OWNER_USER_ID` | fileservice | The only account allowed. If neither is set, owner auth is refused (fail closed). |
| `INDEXER_URL` | fileservice | Default `http://lucidlink-indexer:8100`. |
| `INDEXER_INTERNAL_KEY` | both | Shared secret; the indexer rejects any request without `X-Internal-Key`. |
| `EMBED_BASE_URL`, `EMBED_MODEL`, `EMBED_API_KEY` | indexer | OpenAI-compatible `/embeddings` endpoint (e.g. Ollama on the Spark: `.../v1`, `nomic-embed-text`). |
| `RUVECTOR_URL`, `RUVECTOR_API_KEY` | indexer | Ruvector server base URL, e.g. `https://spark-47d8.tail333a1d.ts.net/ruvector`. Optional; local search is used when unset or unhealthy. |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT` | both | Existing chat endpoint, reused for Ask and the topic survey. |
| `SYNC_INTERVAL_SEC` | indexer | Background delta scan cadence per filespace. Default 300. |
| `INDEX_MAX_FILE_BYTES` | indexer | Skip larger files. Default 25 MB. |
| `INDEX_JOB_BUDGET_SEC` | indexer | Seconds one job may index before yielding its slot, so a huge filespace takes turns with the rest. Default 120. |

## Owner auth (Option A: existing Supabase sign-in)

`docker/owner_auth.py`

- `is_jwt(token) -> bool` — three base64url segments, first starts with `eyJ`.
- `verify_owner(bearer: str) -> Owner` — calls `{SUPABASE_URL}/auth/v1/user`
  with `apikey: SUPABASE_ANON_KEY` and `Authorization: Bearer <jwt>`. Accepts only
  when the returned user's email matches `OWNER_EMAIL` (case-insensitive) or id
  matches `OWNER_USER_ID`. Results are cached for 60 s keyed by sha256 of the
  token (never the raw token). Raises `HTTPException(401)` otherwise. This works
  regardless of how the Supabase project signs JWTs.
- `require_owner` — FastAPI dependency built on the above.

`ll_core.require_token` / `require_token_and_filespace` keep their current
behaviour for a LucidLink service-account token. When the bearer is a JWT they
verify the owner and substitute the LucidLink token from the vault for the
workspace named by `X-Workspace-Id` (falling back to the vault's
`activeWorkspaceId`). The Management API proxy (`/api/v1/*`) does the same
substitution. Existing token-in-browser clients keep working unchanged.

## Vault

`docker/vault.py` — SQLite `/data/vault.db`, one row, Fernet-encrypted JSON.

Payload shape (a subset of the web app's `ApiSettings`):

```json
{
  "workspaces": [{"id": "default", "label": "Default", "url": "https://…", "token": "sa_live:…",
                  "filespace": "my-app-bucket", "filespaces": ["my-app-bucket"]}],
  "activeWorkspaceId": "default",
  "minioProfiles": [{"id": "ugreen", "label": "…", "endpoint": "…", "region": "…",
                     "accessKeyId": "…", "secretAccessKey": "…", "forcePathStyle": true}],
  "activeMinioProfileId": "ugreen"
}
```

Python surface: `load() -> dict`, `save(payload) -> dict`, `masked(payload) -> dict`,
`token_for(workspace_id: str | None) -> str | None`, `workspaces() -> list[dict]`.

Masking: LucidLink `token` values are returned as `"<prefix>••••<last4>"`
(e.g. `sa_live:••••3f2a`), or `""` when empty. On `save`, a token that is
omitted, empty-string-but-previously-set-and-flagged, or contains `••••` keeps
the stored value; any other string replaces it. To clear a token the client
sends `"token": null`. MinIO credentials are returned unmasked to the owner,
because the in-browser S3 console needs them.

HTTP (file service, owner only):

| Method | Path | Body / result |
|---|---|---|
| GET | `/vault/status` | no auth: `{"configured": bool, "ownerConfigured": bool, "hasSettings": bool}` |
| GET | `/vault/settings` | masked payload + `"updatedAt"` (ISO) |
| PUT | `/vault/settings` | payload → saved masked payload + `"updatedAt"` |
| GET/PUT | `/vault/favorites` | see "Favorites, synced" (Simplification phase 2) |

Favorites live in the same encrypted row under `"favorites"` (Python:
`load_favorites()`, `save_favorites(body)`); a settings save carries them over
and the settings payload never includes them.

## Indexer HTTP API

Served by `lucidlink-indexer` and reached by the browser only through the file
service proxy `/index/{path}`, which requires the owner and adds
`X-Internal-Key`. All bodies are JSON. `workspaceId` defaults to the vault's
active workspace.

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/index/status` | — | `{"filespaces": [FilespaceStatus], "embedder": {"model", "dim", "ok", "error"}, "vectorStore": {"backend": "ruvector"\|"local", "ok", "points", "error"}}` |
| POST | `/index/sync` | `{"workspaceId"?, "filespace"?}` | 202 `{"queued": [{"workspaceId","filespace"}]}` |
| PUT | `/index/filespaces` | `{"workspaceId","filespace","enabled": bool}` | `FilespaceStatus` |
| POST | `/index/hint` | `{"workspaceId"?, "filespace", "op": "write"\|"delete"\|"move"\|"mkdir", "path", "src"?}` | 202 `{}` |
| POST | `/index/search` | `{"workspaceId"?, "filespace"?, "query", "k"?: 20, "pathPrefix"?}` | `{"results": [{"workspaceId","filespace","path","chunkIndex","score","snippet","mtime"}]}` |
| GET | `/index/recent` | `?workspaceId&filespace&limit=20&kind=&change=&since=&before=` (limit capped at 200; `filespace` omitted = every enabled filespace of the workspace; `kind` as `/index/bykind`, `change` a comma list of new\|modified\|renamed\|deleted, `since` ≤ `when` < `before` (ISO); unknown kind/change or a bad time → 400; see What's new) | `{"files": [RecentFile], "next": ISO\|null}`, newest `when` first; pass `next` as `before` for the next page (ties on `when` stay on one page) |
| POST | `/index/ask` | `{"workspaceId"?, "filespace"?, "question", "k"?: 12}` | `{"answer", "sources": [path], "citations": [{"workspaceId","filespace","kind","path","chunkIndex","score"}]}`; 503 when no LLM; `{"empty": true, …}` when nothing indexed |
| GET | `/index/survey` | `?workspaceId&filespace&scope=/` | `{"topics": [Topic], "raw", "sources": [path], "generatedAt", "stale": bool}`; 404 when never generated |
| POST | `/index/survey` | `{"workspaceId"?, "filespace", "scope"?: "/"}` | regenerate now, same shape |
| POST | `/index/assist` | `{"action": "summarize"\|"rewrite"\|"shorten"\|"lengthen"\|"continue"\|"generate"\|"custom", "text"?, "instruction"?}` | `{"html", "action"}`: the document editor's AI menu, on this engine's LLM; 400 when there is nothing to work on, 503 when no LLM |

`FilespaceStatus`:

```json
{"workspaceId": "default", "filespace": "my-app-bucket", "enabled": true,
 "state": "idle|scanning|indexing|error|disabled",
 "lastScanAt": "ISO|null", "lastSyncAt": "ISO|null", "nextScanAt": "ISO|null",
 "files": {"indexed": 982, "pending": 3, "failed": 1, "skipped": 40},
 "chunks": 5120, "lastError": null,
 "progress": {"done": 0, "total": 3, "current": "/path|null"}}
```

`RecentFile`:

```json
{"workspaceId": "default", "filespace": "my-app-bucket", "path": "/Board/minutes.docx",
 "size": 48211, "mtime": "ISO|null", "status": "indexed|pending|skipped|failed|deleted",
 "indexedAt": "ISO|null", "change": "new|modified|renamed|deleted|null",
 "changedAt": "ISO|null", "previousPath": "/old/path|null",
 "when": "ISO", "type": "document|pdf|image|…"}
```

Live files are ordered by their own `mtime`; deletions and renames by `changedAt`
(when the indexer saw them). `change` is recorded by the ledger as it updates a
row (`Store.update_file` / `insert_file`): a size/mtime change is `modified`, a
new path for the same `file_id` is `renamed` (with `previousPath`), a tombstone
is `deleted`, and a file first seen after the filespace's first completed sync
(or one that reappears after a deletion) is `new`. Files found by the first
crawl, and rows written before this field existed, have `change: null`.

`Topic` matches the web app's `DiscoveredTopic`:
`{"name","description","dateRange","documents":[{"path","title","date"}]}`.
Each document may also carry `"excerpt"`: up to 220 characters, on one line, of
the passage that brought it into the survey (absent for paths the model
invented, and in surveys stored before this field existed).

Score is a similarity in `[0, 1]`, higher is better, whatever the backend.

`/index/assist` replaced the web app's `docs-ai` edge function, which sent
document text to a hosted AI gateway. The text now goes only to `LLM_BASE_URL`.
Plain-text model output is escaped and wrapped in `<p>`; HTML output is passed
through for the editor, whose schema drops anything it doesn't know. Text is
capped at 200,000 characters and instructions at 4,000.

The file service also forwards hints: after a successful `/files/write`,
`/files/upload`, `/files/mkdir`, `DELETE /files`, `DELETE /files/dir` or
`/files/move`, it posts `/index/hint` in the background (best effort, 2 s
timeout, failures logged and ignored). `/agent/query` first tries `/index/ask`
for that filespace and falls back to its current behaviour when the indexer is
unreachable or has nothing indexed.

## Change detection

A scan lists metadata only and never downloads a file to find out whether it
changed. SDK 0.14's `fs.read_dir` does **not** expose `file_id` (it is `""` on
every entry) and its `mtime` may use different units from `fs.get_entry`'s, so
identity comes from `get_entry`, and only when needed.

1. **Scan** breadth-first with `fs.read_dir`, skipping hidden and system folders
   (same list as the web app). Each scan has an id; every file seen is stamped
   with it.
2. **Fast path by path** — the ledger keeps the `(size, mtime)` read_dir last
   reported for each path (`list_size`, `list_mtime`). A live row at the same
   path with equal listing values is unchanged: no further call, so an unchanged
   rescan is `read_dir` only. Otherwise `fs.get_entry(path)` supplies the stable
   `file_id` and authoritative `(size, mtime)`, and steps 3–5 apply. An entry
   without a `file_id`, or a failed `get_entry`, makes the scan incomplete
   (nothing is deleted) and is never written to the ledger.
3. **New or changed** — no ledger row for the `file_id`, or `(size, mtime)`
   differs → queued. Newest `mtime` first, so a fresh filespace is useful within
   minutes.
4. **Renamed** — same `file_id`, new path → path updated in place, vectors keep
   their embeddings (only metadata is re-upserted).
5. **Touched, not changed** — content sha256 equals the stored hash → only the
   ledger's `mtime` is updated.
6. **Deleted** — after a scan in which *every* directory listed successfully,
   files not stamped with this scan id are tombstoned and their chunks removed.
   A directory that fails to list marks its whole subtree as "unknown" for this
   scan: nothing under it is deleted. A transient error must never look like a
   deletion.
7. **Chunk-level reuse** — embeddings are cached by `(model, sha256(chunk text))`.
   An edit to one paragraph re-embeds one or two chunks, not the file.

Hints from the file service are handled within seconds with targeted calls
only: `write`/`mkdir` (and a move's destination) `get_entry` the path and
reconcile that file or list that folder; `delete` (and a move's source)
tombstones the path's subtree once `get_entry` confirms it is gone. Hints for
filespaces not being indexed, for paths that do not exist, or malformed paths
are ignored and never escalate to a full scan; more than 50 queued hints for one
filespace collapse into one full scan. A full delta scan runs every
`SYNC_INTERVAL_SEC`, and on demand.

## Extraction and chunking

Plain text (`md, markdown, txt, log, csv, tsv, json, xml, yml, yaml, rtf`),
HTML (tags stripped), DOCX, PPTX and XLSX (stdlib `zipfile` + XML), PDF
(`pypdf`), and Apple iWork `pages`, `numbers`, `key` (below). Larger than
`INDEX_MAX_FILE_BYTES`, binary, or unextractable → `skipped` with a reason.
Chunking matches the web app's `chunkText`: 1200 characters, 150 overlap,
breaking on newline or sentence end.

**iWork** (`docker/indexer/iwork.py`, pure Python, no extra dependencies).
A document is either a single-file zip or a *package*: a folder named
`X.pages` / `X.numbers` / `X.key`. Text is taken, in order of preference, from:

1. `Index/*.iwa` (also inside a nested `Index.zip`): Apple's snappy framing
   (`[0x00][3-byte LE length][raw snappy block]`, no CRC) decoded by a bounded
   pure-Python snappy decoder, then each archive's protobuf payloads walked
   generically. Length-delimited fields are read as nested messages or, when
   they look like human text, kept as text (`human_text` holds every
   heuristic: printable, has letters, not a UUID / hex / reverse-DNS id /
   MIME type / locale / file name / font or style name). `Document.iwa`
   comes first, then the other archives in natural name order; stylesheet,
   metadata, view-state, calculation-engine and master-slide archives are
   skipped. Consecutive repeats collapse; the text is capped like any other.
2. iWork '09 `index.xml(.gz)` / `index.apxl(.gz)`, tags stripped.
3. `QuickLook/Preview.pdf` through the PDF extractor.

Otherwise the file is `skipped` with "iWork document with no readable text".
Expected to be recovered (not yet checked on real files): body text, headers/footers, text boxes and shapes, comments,
Numbers sheet/table names and text cells, Keynote slide text and presenter
notes. Not recovered: Numbers numeric, date and formula cells (stored as
binary cell data), chart data, and text in images. Keynote slides follow
archive name order, which is not always presentation order.

A package is **one file**: the scan never recurses into it. Its size is the
sum of the contained file sizes and its mtime the newest contained mtime,
from a bounded listing-only walk (5000 entries, 8 levels; dot-names
ignored), and these walk values are what the fast path compares, so an
unchanged package costs a few listings and no reads. Its id is the folder's
own `get_entry` id (a path id on block sources); an empty id makes the scan
incomplete, as for files. A package that fails to list makes the scan
incomplete, never a deletion. To extract, only `Index/**`, `index.xml*`,
`index.apxl*`, `Index.zip` and (when it still fits) `QuickLook/Preview.pdf`
are read, at most `INDEX_MAX_FILE_BYTES` in all, into a deterministic
in-memory zip that goes through the same extractor; previews and `Data/` are
never read. Hints for a path inside a package apply to the package.

## Storage and the vector index

`/data/index.db` (SQLite, WAL) is the durable source of truth:

- `filespaces(workspace_id, filespace, enabled, last_scan_at, last_sync_at, last_error, …)`
- `files(workspace_id, filespace, file_id, path, size, mtime, content_hash, status, reason, chunk_count, scan_id, indexed_at, change_kind, changed_at, prev_path)`
- `chunks(point_id, workspace_id, filespace, file_id, chunk_index, text_hash, text)`
- `embeddings(model, text_hash, dim, vector BLOB)` — the embedding cache
- `surveys(workspace_id, filespace, scope, payload, generated_at, stale)`

Search always works from SQLite (numpy brute force; fine at personal scale).
When `RUVECTOR_URL` is set, Ruvector is a mirror used to accelerate search:

- Collection `ll_<model-slug>_<dim>`, cosine. `POST /collections`,
  `PUT /collections/{name}/points` with `{id, vector, metadata}`,
  `POST /collections/{name}/points/search` (`score` is a distance; similarity
  is `1 - score`).
- Stock `ruvector-server` keeps collections in memory and has no point delete.
  So: results are filtered against live `point_id`s in SQLite; when the
  collection is missing (Ruvector restarted) it is recreated and rehydrated
  from SQLite without re-embedding; when dead points exceed 20% of live ones
  the collection is rebuilt.
- Any Ruvector error falls back to local search for that request and is shown
  in `/index/status`.

## Survey (Discover topics)

After a sync that changed something, and at most every 10 minutes, the indexer
regenerates the root-scope survey for that filespace with the same probes and
prompt the edge function used, and stores it. Discover opens instantly from
the stored survey; "Refresh topics" forces a regeneration.

## Connected block storage (In Place, Phase A)

The bridge from the AI Lab's "LucidLink In Place" concept, first phase: a folder
on block storage (a NAS share, external drive or local disk) is indexed **where
it lives**, read-only, with 0 bytes moved, and becomes searchable and askable
next to the filespaces. Check-out to LucidLink and check-in are later phases.

**iCloud Drive.** `~/Library/Mobile Documents/com~apple~CloudDocs` works like
any folder, but with "Optimize Mac Storage" on, files evicted to iCloud must be
downloaded when read, which makes the first index slower and can fail for
files macOS will not fetch on demand; those are recorded as failed and retried.
If Desktop & Documents are synced to iCloud, ~/Desktop is already inside iCloud
Drive, so index one or the other, not both.

**Mounting.** The share is mounted on the Mini (e.g. `/Volumes/<share>`) and
bind-mounted into the indexer container **read-only** (`:ro`), so the container
cannot write to it whatever the code does.

| Variable | Meaning |
|---|---|
| `BLOCK_SOURCE_PATH`, `BLOCK_SOURCE_PATH_2` … `_6` | Host folders (a NAS share, drive, iCloud Drive, Desktop), each mounted read-only in its own slot, `/block/source1` … `/block/source6`, so the container sees only those folders. Unset slots get an empty placeholder. |
| `BLOCK_SOURCES` | `name=/container/path` entries separated by `;`, e.g. `NAS=/block/source1`. Empty: no block sources. |
| `BLOCK_SOURCE_WORKSPACE` | Workspace id the block sources belong to. Default: `local` (the engine computer; see below). |
| `ENGINE_NAME` | The engine computer's name, the label of its drives in the app. `start.sh` sets it once from the computer's name. |

**The helper.** `docker/add-folder.sh` does the steps above in one command on
the Mini: `./add-folder.sh smb://nas.local/Projects` (mounts the share first;
a sub-path after the share is allowed), `./add-folder.sh /Volumes/Projects
--name Studio`, `--list` and `--remove NAME`. It picks the first free slot,
refuses a folder that is already connected, inside or around another slot,
names a source after the folder (iCloud Drive becomes `iCloud`), backs up
`.env` (mode 600) before editing `BLOCK_SOURCE_PATH[_N]` and `BLOCK_SOURCES`,
recreates only `lucidlink-indexer`, then reports what the indexer says about
the new source (accepted, or why it was refused). `--yes` skips the question,
`--no-restart` only edits `.env`. Removing a source stops indexing it; chunks
already indexed from it stay in the index until it is cleared.

**Keys.** A block source is a target `(workspaceId, name)` like a filespace,
with `kind: "block"`. A name that collides with a vault filespace in that
workspace is refused and not indexed: the filespace keeps the key and keeps
working, and the refusal shows as that entry's `lastError` (when it has no
error of its own) and in `/index/status` → `blockSources`. A missing, empty or
placeholder folder records `lastError: "block source path not mounted"` and
neither indexes nor deletes anything. Enable/disable, sync, status, search,
ask, survey and recent work unchanged. Hints never apply (the file service
never writes there).

**Reading.** `LocalSource` lists with `os.scandir` and reads files directly.
It never follows symlinks, never leaves its root, and skips system folders and
files (`.DS_Store`, `._*` AppleDouble files, `.Spotlight-V100`, `.Trashes`,
`.fseventsd`, `#recycle`, `@eaDir`, `.TemporaryItems`, plus the existing skip
list). `mtime` is `st_mtime_ns`.

**File ids.** Inode numbers through SMB and OrbStack file sharing are not
guaranteed stable across restarts, so a block file's id is
`"p:" + sha1(relative path)`. A rename is then a delete plus a new file; the
embedding cache (by chunk text hash) means it is re-read but not re-embedded.

**API additions (all sources).**

- `FilespaceStatus` gains `"kind": "lucidlink" | "block"`.
- Search results, recent files and ask citations gain `"kind"`.
- `/index/status` gains `"blockSources": [{"name", "workspaceId", "path",
  "kind": "block", "accepted": bool, "error": "…"|null}]` (configuration
  problems and refusals).
- `GET /index/profile?workspaceId&filespace` — what the source holds, from the
  ledger only (no reads):
  ```json
  {"workspaceId": "default", "filespace": "NAS", "kind": "block",
   "totals": {"files": 1234, "bytes": 5678901},
   "byType": [{"type": "document", "files": 120, "bytes": 3456}],
   "ages": {"7d": 12, "30d": 40, "90d": 100, "older": 1082},
   "largestFolders": [{"path": "/Clients", "files": 300, "bytes": 123456}],
   "skipped": [{"reason": "unsupported type (.mov)", "files": 400}],
   "lastScanAt": "ISO|null"}
  ```
  Types follow the AI Lab's classes: document, spreadsheet, presentation, pdf,
  image, design, audio, video, code, archive, project, other; `byType` lists
  the non-empty ones, largest first. `ages` buckets live files by `mtime` and
  are exclusive (8–30 days counts under `30d`, not `7d`). `largestFolders`
  is the top 10 folders one level below the root by bytes (files directly in
  the root are in no folder).

### Folders from the app (no terminal)

The owner connects and removes block storage from the web app (Settings ›
Storage Links › Local storage or NAS). The indexer sees a few **roots** read-only, mounted once
by compose, and indexes only the folders chosen inside them; choices are kept in
the index database, so nothing is restarted.

| Root id | Label | Host folder (`.env`) | In the container |
|---|---|---|---|
| `volumes` | Drives and network shares | `BLOCK_ROOT_VOLUMES` (default `/Volumes`) | `/host/volumes` |
| `icloud` | iCloud Drive | `BLOCK_ROOT_ICLOUD` | `/host/icloud` |
| `desktop` | Desktop | `BLOCK_ROOT_DESKTOP` | `/host/desktop` |
| `documents` | Documents | `BLOCK_ROOT_DOCUMENTS` | `/host/documents` |

`start.sh` fills `BLOCK_ROOT_ICLOUD`, `BLOCK_ROOT_DESKTOP` and `BLOCK_ROOT_DOCUMENTS` on macOS when those
folders exist and the variables are unset. A root whose folder is unset, missing
or empty is `available: false`. Browsing lists folders only, never follows a
symlink (so `/Volumes/Macintosh HD`, a symlink to `/`, is not offered) and
skips dot-names and system folders.

Sources from `.env` (`BLOCK_SOURCES`, origin `engine`) and from the app (origin
`app`) are listed together; either can be removed from the app. Removing an
`engine` source records that in the database (`.env` is not edited, and the slot
stays mounted until `add-folder.sh --remove` or a manual edit). Removing any
source deletes its ledger rows, chunks and surveys, so its results leave search
at once. Names are unique across all block sources, may not contain `/`, `;` or
`=`, are at most 64 characters, and may not match a filespace in the block
workspace. Folders are compared by **host path** (`BLOCK_SOURCE_PATH*` and the
root host folders are passed to the container as plain strings), so the same
folder reached through a slot and a root, or a folder inside or around a
connected one, is refused.

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/index/folders` | — | `{"roots": [Root], "sources": [BlockSource]}` |
| GET | `/index/folders/browse` | `?root=volumes&path=/` | `{"root", "path", "parent": "/…"\|null, "entries": [{"name", "path", "hostPath", "connected": "<source name>"\|null}], "truncated": bool}` (folders only, sorted by name, at most 500); 404 unknown root, 400 bad path, 503 root unavailable |
| POST | `/index/folders` | `{"root", "path", "name"?}` | 201 `BlockSource`, first scan queued; 400 invalid (not a folder, bad name), 409 already connected, overlapping or name taken, 503 root unavailable |
| POST | `/index/folders/remove` | `{"name"}` | `{"removed", "files", "chunks"}`; 404 unknown name |
| POST | `/index/folders/restart` | — | 202 `{"restarting": true}`: the indexer exits and Docker restarts it (for a share mounted after the indexer started that the container does not see yet) |

`Root`: `{"id", "label", "hostPath": "/Volumes"|null, "available": bool}`. The web
app groups them as **Local storage** (`icloud`, `desktop`, `documents`) and
**NAS** (`volumes`; external drives also appear there, since macOS mounts them
under /Volumes too).
`BlockSource` (also in `/index/status` → `blockSources`): `{"name", "workspaceId",
"path", "kind": "block", "accepted", "error", "origin": "app"|"engine", "hostPath":
"/Volumes/Projects"|null, "root": "volumes"|null, "folder": "/Projects"|null}`
(`root`/`folder` only for `app` sources).

### Drive prompts

The app nudges, it never decides: a new drive is offered, a missing one is
offered for removal, and nothing is connected or removed without a tap.

- **New drive.** A folder at the top of the `volumes` root (a share or drive
  mounted on the Mini) that appeared after the indexer first looked, that no
  source is, is inside or contains, and that was not dismissed. The first look
  (per database) records what is already there as known, so only later arrivals
  count. A drive that is unmounted and comes back is not new again.
- **Missing source.** A connected block source whose folder is missing, empty
  or the placeholder, continuously for at least `FOLDER_MISSING_GRACE_SEC`
  (default 900 s, so a NAS waking from sleep is not reported). It is never
  removed automatically; while it is missing the indexer neither indexes nor
  deletes anything for it. "Keep it" hides the prompt until the folder comes back
  and goes missing again.

Both are computed when the app asks (no background polling of drives).

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/index/folders/prompts` | — | `{"newDrives": [{"root": "volumes", "path": "/Archive-3", "name": "Archive-3", "hostPath": "/Volumes/Archive-3", "suggestedName": "Archive-3", "firstSeenAt": "ISO"}], "missing": [{"name", "origin", "hostPath", "root", "missingSince": "ISO"}]}` |
| POST | `/index/folders/prompts/dismiss` | `{"kind": "new", "path": "/Archive-3"}` or `{"kind": "missing", "name"}` | `{"dismissed": true}`; 404 when there is no such prompt |

Connecting a new drive is the normal `POST /index/folders`; forgetting a missing
source is `POST /index/folders/remove`.

### Browsing and opening connected folders

The header's source chooser lists connected folders next to filespaces, and
Files browses them **read-only** through the indexer (the file service never
touches block storage). Listing and reading go through the same `LocalSource`
as indexing: no symlinks, never outside the folder, system files hidden.

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/index/folders/list` | `?name=<source>&path=/` | `{"name", "path", "parent": "/…"\|null, "entries": [{"name", "path", "isDir", "size", "mtime": "ISO"\|null, "status": "indexed"\|"pending"\|"skipped"\|"failed"\|null, "reason": "…"\|null}], "truncated": bool}`: folders first, then files, by name; at most 2000; `status` and `reason` (why it was skipped or failed, e.g. online only) from the ledger for files; 404 unknown source, 400 bad path, 503 not mounted |
| GET | `/index/folders/file` | `?name=<source>&path=/a/b.pdf` | the file's bytes, `Content-Type` from the extension, `Content-Disposition: inline; filename*=…`, `Accept-Ranges: bytes` (a `Range` header gives 206); 404 unknown source or file, 400 bad path or a folder, 503 not mounted |

The file service's `/index/{path}` proxy streams responses and passes `Range`,
`Content-Range`, `Accept-Ranges`, `Content-Length` and `Content-Disposition`
through, so large files are not held in memory.

### The engine computer and All drives

Drives the engine computer reaches itself (block sources: NAS shares, USB disks,
local folders) belong to the workspace id **`local`**, not to whichever
LucidLink workspace is active. `BLOCK_SOURCE_WORKSPACE` still overrides it. The
app labels them with the engine computer's name (`ENGINE_NAME`, e.g.
"AI-Factory-Mini · NAS"; "the engine computer" in generic copy), returned as
`"engine": {"name", "workspaceId"}` by `GET /index/folders` and `/index/status`
(`name` is null when unset). On the first scheduling pass after upgrading,
block sources indexed under another workspace are moved to `local` in place
(files, chunks, surveys and the indexing priority list keep their ids and
embeddings, so nothing is re-read; their Ruvector points are re-sent with the
new workspace).

`workspaceId: "*"` (body, query or `X-Workspace-Id`) means **every workspace**,
the Mini's included, for search, ask and recent: the app's "All drives". With a
`filespace` it is that name in whichever workspace has it. Status, survey and
the folder endpoints are unchanged.

### Cloud storage through the engine computer's sync apps

Google Drive, Dropbox, OneDrive, Box and ShareFile on a Mac keep their folders
under `~/Library/CloudStorage` (Finder lists them under Locations). The engine
shares that folder read-only as a fifth root, so a cloud service is connected
like any other folder: no service sign-in in this app, nothing new stored in the
vault, and the content is what the sync app already has.

| Root id | Label | Host folder (`.env`) | In the container |
|---|---|---|---|
| `cloud` | Cloud storage | `BLOCK_ROOT_CLOUD` (`start.sh` fills `~/Library/CloudStorage` on macOS) | `/host/cloud` |

**Which service.** The top folder names the service and account:
`GoogleDrive-<account>`, `Dropbox`/`Dropbox-<team>`/`Dropbox (<name>)`,
`OneDrive-<account>`, `Box-Box`, `ShareFile-<account>`. Browse entries and
`BlockSource` carry `"provider": {"id": "google-drive"|"dropbox"|"onedrive"|"box"|"sharefile"|"other", "label": "Google Drive", "account": "mark@heximal.com"|null}`
for anything in the `cloud` root (null elsewhere).

**Online-only files.** Sync apps keep many files in the cloud until opened; on
the engine computer they are placeholders that download when read. A source in
the `cloud` root therefore reads only files already downloaded there and records
the rest as skipped with reason `online only (not downloaded on the engine
computer)`; its names, sizes and dates are still in the ledger, so they show in
browsing, recent and the storage profile. A placeholder is a file with a size
but no allocated blocks (`st_blocks == 0`). Setting `downloadOnlineOnly` lets
the indexer read (and so download) them, using disk on the engine computer;
turning it on re-queues the skipped files. Google Docs, Sheets and Slides are
links (`.gdoc`, `.gsheet`, `.gslides`), not documents, and are skipped as
unsupported.

| Method | Path | Body / query | Result |
|---|---|---|---|
| POST | `/index/folders/options` | `{"name", "downloadOnlineOnly": bool}` | `BlockSource`; 404 unknown source, 400 for a source not connected from the app |

`BlockSource` gains `"downloadOnlineOnly": bool` (false unless set; only
meaningful in the `cloud` root). New-drive prompts cover the `cloud` root too:
a new service or account under `~/Library/CloudStorage` is offered like a new
share, with `"root": "cloud"`; dismissing takes `{"kind": "new", "root":
"cloud", "path"}` (`root` defaults to `volumes`).

### Engine activity feed

The web app's engine console (a right-side drawer, green on black) shows what
the indexer is doing as it happens. The indexer keeps its recent log lines in
memory, a ring of `ACTIVITY_LINES` (default 1000), from its own loggers
(`indexer.*`) plus warnings and errors from anything else; request logs
(`uvicorn.access`) and its own polling are left out. Nothing is written to disk
or sent anywhere; owner-only like every `/index` route.

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/index/activity` | `?after=<seq>&limit=200` | `{"events": [{"seq", "ts": "ISO", "level": "info"\|"warning"\|"error", "source": "worker"\|"vectors"\|…, "text"}], "last": seq, "dropped": bool}`: events after `seq` (all buffered ones when omitted), oldest first, at most `limit` (max 500); `dropped` is true when `after` is older than the buffer, so lines were missed |

Besides scan summaries, each file read, skipped or failed gets a line
(`source: "files"`, e.g. `[local/NAS] indexed /Clients/brief.pdf (12 chunks)`); those
lines go only to the console, not to the container log.

Poll it every 1-2 s while the console is open; `seq` only grows. Lines are the
log text (they include file paths, as the logs do); anything that looks like a
bearer token or key is masked.

### Plain answers for the app (simplification, phase 1)

**Find by name.** `GET /index/find?q=&workspaceId=&filespace=&limit=20` matches
file **names and folder paths** in the ledger (no AI, instant), across the same
scopes as search (`workspaceId: "*"` = every location). Case-insensitive; a
name that starts with the query ranks above one that contains it, then newer
first. Every word of a multi-word query must appear in the path. Files the
indexer could not read (video, online-only, too large) are still found, since
they are in the ledger. Result: `{"results": [{"workspaceId", "filespace",
"kind", "location": Location, "path", "name", "size", "mtime", "status",
"reason"}]}` (`limit` max 100; `q` at least 2 characters, else empty).

**Where each item lives.** Every search hit, find result, recent file, ask
citation and `FilespaceStatus` now carries `"location"`:
`{"kind": "lucidlink"|"block", "root": "volumes"|"icloud"|"desktop"|"documents"|"cloud"|null,
"provider": {"id","label","account"}|null}`, so the app can badge iCloud,
Desktop, Google Drive and NAS correctly everywhere (`root` is null for `.env`
slots and LucidLink).

**Why something is wrong.** `FilespaceStatus` gains `"problem"`: null, or
`{"kind": "sign_in"|"offline"|"unreachable"|"too_big"|"engine"|"other", "detail"}`,
classified from `lastError` (`detail` is the raw text, for Engine only). The
first matching row wins, so the SDK's "Authentication failed: … Host not found"
is `unreachable`, not `sign_in`:

| kind | from | the app says |
|---|---|---|
| `offline` | block source path not mounted | "<name> is offline" |
| `too_big` | 413 / payload too large | "Some files are too large to read" |
| `engine` | embedder/LLM/vector errors | "Search is temporarily unavailable" |
| `unreachable` | host not found, connection refused/reset, timed out | "Can't reach LucidLink right now" |
| `sign_in` | authentication failed, lease expired, invalid/expired token, 401/403 | "Sign in to LucidLink again" |
| `other` | anything else | "Something went wrong reading <name>" |

**Suggested names.** Browse entries carry `"suggestedName"`: the folder name,
except at the top of a cloud service, where it is "<service> (<account name>)",
e.g. "Google Drive (mark.personal)", and iCloud Drive's root, "iCloud". The
app pre-fills it instead of sanitising the folder name itself.

### Simplification phase 2: sizes, kinds, topics everywhere, synced Favorites

**Sizes.** `FilespaceStatus` gains `"totals": {"files": <live files>, "bytes": <their size>}`
from the ledger (no reads), so Settings › Locations can list every location
with its size.

**By kind, across locations.** `GET /index/bykind?kind=&workspaceId=&filespace=&limit=100&offset=0`
lists live files of one kind, newest first (by `mtime`, future-dated files last),
across the same scopes as search (`workspaceId: "*"` = every location). Kinds
map onto the profile's type classes:

| `kind` | type classes |
|---|---|
| `documents` | document, pdf, spreadsheet |
| `presentations` | presentation |
| `images` | image, design |
| `video` | video, audio |

Result: `{"files": [{"workspaceId", "filespace", "location", "path", "name",
"size", "mtime", "status", "type"}], "total": n, "kind"}`; `limit` max 500;
400 for an unknown kind. A name's class is the profile's (`block.type_of`), so a
`.psd` counts as design and an `.srt` as a document. It is what the Files "Kind" filter shows when no single
folder is being browsed.

**Topics everywhere.** `GET /index/surveys?workspaceId=*` returns the stored
root-scope (`/`) survey of every location in scope, without generating any:
`{"surveys": [{"workspaceId", "filespace", "location", "generatedAt", "stale",
"topics": [Topic]}]}`, newest first. Locations never surveyed are left out.
Generating stays per location (`POST /index/survey`).

**Favorites, synced.** The file service keeps the owner's Favorites in the vault,
so they follow the owner across devices. Owner-only, like `/vault/settings`:

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/vault/favorites` | — | `{"favorites": [Favorite]}` (empty list when none) |
| PUT | `/vault/favorites` | `{"favorites": [Favorite]}` | the stored list; replaces it; at most 200; 400 when malformed |

`Favorite`: `{"id", "workspaceId", "filespace", "kind": "lucidlink"|"block",
"path", "label", "addedAt": "ISO"}` (`label` optional; unknown fields dropped;
`path` must start with `/`). Older vaults without favorites return an empty list.
`id`, `workspaceId`, `filespace`, `path` and `addedAt` must be non-empty strings,
`kind` one of the two values and ids unique; anything else is a 400 and the
stored list is unchanged.

### Simplification phase 3: What's new, storage at a glance, first run

**What's new.** `GET /index/recent` gains filters and paging so the app can show
every change across every location, grouped by day:

- `kind=documents|presentations|images|video` — the same mapping as `/index/bykind`
  (deletions are classed by their last name); unknown kind → 400.
- `change=new|modified|renamed|deleted` (comma-separated for several); unknown → 400.
- `since=ISO` and `before=ISO` — only changes with `when` ≥ `since` / strictly < `before`.
- `limit` stays capped at 200.

Each `RecentFile` gains `"when": "ISO"` (the timestamp it is ordered by: the file's
`mtime` for live files, `changedAt` for deletions and renames) and `"type"` (the
profile's type class). The response gains `"next": "ISO"|null`: pass it as
`before` to read the following page (null when there is nothing older). Ties on
`when` are not split across pages (a page may run a little over `limit`).
Future-dated files stay excluded as before.

**Storage at a glance.** No new route: the app reads `totals` from `/index/status`
and, for the location being looked at, `GET /index/profile` (`byType`,
`largestFolders`, `ages`, `skipped`). `largestFolders` items gain `"files"` and
`"bytes"` already; nothing else changes.

**First run.** No new route: the app uses `/index/folders/prompts` (new cloud
accounts and drives) and `/index/folders` (roots and connected sources) to offer "Choose what to search".

## Indexing controls

The owner manages indexing from Engine › Indexer: what failed and why, what is
queued, retry, pause everything, and which source goes first. All routes are
under `/index/*` (internal key at the indexer, owner session at the proxy).

| Method | Path | Body / query | Result |
|---|---|---|---|
| GET | `/index/files` | `?workspaceId&filespace&status=failed\|skipped\|pending\|indexed&limit=50&offset=0&q=` | `{"files": [File], "total": n, "byReason": [{"reason", "files"}]}` |
| POST | `/index/retry` | `{"workspaceId"?, "filespace", "paths"?: [path], "all"?: bool}` | `{"requeued": n}` |
| GET | `/index/control` | — | `Control` |
| PUT | `/index/control` | `{"paused"?: bool, "priority"?: [{"workspaceId", "filespace"}]}` | `Control` |

`File`: `{"path", "size", "mtime" (ISO), "status", "reason", "indexedAt", "changedAt", "kind"}`.

- `/index/files`: `limit` is capped at 200. `pending` is in queue order (the
  order the indexer will work: newest `mtime` first); `failed` and `skipped`
  are newest `mtime` first; `indexed` newest `indexedAt` first (ties by
  path). `q` filters by a case-insensitive (ASCII case folding) substring of
  the path; `%` and `_` match literally. `total` counts the status after `q`.
  `byReason` groups the whole status (not just the page, and ignoring `q`) for
  `failed` and `skipped`, largest first, top 20, and is `[]` for the others.
  `filespace` and `status` are required (422 otherwise); an unknown source
  gives an empty result, not 404.
- `/index/retry`: moves the named `failed` files (or every failed file of the
  source with `"all": true`) back to `pending`, clearing the retry back-off,
  and asks for a sync of that source when at least one file was requeued.
  Requeueing also clears the failure `reason` and stored content hash, so the
  file is read and extracted afresh. Paths that are not failed are ignored.
  Skipped files are not retried (their reasons are about type or size; changing
  the file or the limits re-queues them on the next scan).
- `Control`: `{"paused": bool, "pausedAt": ISO|null, "priority": [{"workspaceId",
  "filespace"}]}`, persisted in the index database (table `control`). Fields
  omitted from a PUT are unchanged; `pausedAt` is set when pausing (kept by a
  repeated pause) and cleared on resume. While paused, no new job
  starts (a running one finishes its current time slice), hints are kept
  queued, and search, ask, survey and status keep working. `priority` lists
  sources that are offered a job slot before the round-robin order; unknown
  sources in it (not an indexing target) and repeats are dropped on PUT.
  `/index/status` gains top-level `"paused"` and each filespace entry gains
  `"priority": int|null` (its 0-based position in that list) and
  `"state": "paused"` while paused and no job is running for it (this takes
  precedence over `idle` and `error`; `disabled` still wins).
