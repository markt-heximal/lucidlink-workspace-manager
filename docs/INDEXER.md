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
| `LUCIDLINK_CACHE_DATA_GB`, `LUCIDLINK_CACHE_METADATA_GB` | both | SDK cache maximums per linked filespace, in GiB. Default 5 and 1 (see "SDK notes"). |
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
| GET | `/index/recent` | `?workspaceId&filespace&limit=20` (limit capped at 200; `filespace` omitted = every enabled filespace of the workspace) | `{"files": [RecentFile]}`, newest first |
| POST | `/index/ask` | `{"workspaceId"?, "filespace"?, "question", "k"?: 12}` | `{"answer", "sources": [path], "citations": [{"path","chunkIndex","score"}]}`; 503 when no LLM; `{"empty": true, …}` when nothing indexed |
| GET | `/index/survey` | `?workspaceId&filespace&scope=/` | `{"topics": [Topic], "raw", "sources": [path], "generatedAt", "stale": bool}`; 404 when never generated |
| POST | `/index/survey` | `{"workspaceId"?, "filespace", "scope"?: "/"}` | regenerate now, same shape |

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
 "changedAt": "ISO|null", "previousPath": "/old/path|null"}
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

Score is a similarity in `[0, 1]`, higher is better, whatever the backend.

The file service also forwards hints: after a successful `/files/write`,
`/files/upload`, `/files/mkdir`, `DELETE /files`, `DELETE /files/dir` or
`/files/move`, it posts `/index/hint` in the background (best effort, 2 s
timeout, failures logged and ignored). `/agent/query` first tries `/index/ask`
for that filespace and falls back to its current behaviour when the indexer is
unreachable or has nothing indexed.

## Change detection

A scan lists metadata only and never downloads a file to find out whether it
changed. SDK 1.0's `file_system.read_dir` returns each entry's stable id
(`FsEntry.id`, the ledger's `file_id`) and `modified_at` (ns), the same values
`get_entry` reports, so a scan normally needs one call per directory and none
per file. (SDK 0.14's `read_dir` returned `file_id=""` for every entry, so
identity had to come from a `get_entry` per new or changed file; that path is
kept as a fallback for any entry listed without an id.)

1. **Scan** breadth-first with `file_system.read_dir`, skipping hidden and
   system folders (same list as the web app; `.lucid_audit` is hidden). Each
   scan has an id; every file seen is stamped with it.
2. **Fast path by path** — the ledger keeps the `(size, mtime)` read_dir last
   reported for each path (`list_size`, `list_mtime`). A live row at the same
   path with equal listing values, and the same `file_id` as the listing, is
   unchanged: no further call, so an unchanged rescan is `read_dir` only.
   Otherwise the listed id and `(size, mtime)` are used directly and steps 3–5
   apply. Only an entry listed without an id costs a `get_entry(path)` for the
   id and authoritative `(size, mtime)`. An entry with no `file_id` from either,
   or a failed `get_entry`, makes the scan incomplete (nothing is deleted) and
   is never written to the ledger.
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
(`pypdf`). Larger than `INDEX_MAX_FILE_BYTES`, binary, or unextractable →
`skipped` with a reason. Chunking matches the web app's `chunkText`: 1200
characters, 150 overlap, breaking on newline or sentence end.

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

## SDK notes

Both services pin `lucidlink==1.0.1` (docker/requirements.txt; Python >= 3.11,
x86_64 wheel only, hence `--platform=linux/amd64`). What the 0.14 -> 1.0
migration changed for this code:

- **Client and links.** `client.workspace` (one per service-account token)
  replaces `list_workspaces()`/`get_workspace()`. Filespaces are linked by id
  with `workspace.link_filespace(id, cache=CacheConfig(data_size=...,
  metadata_size=...))`; `filespace.file_system` replaces `.fs`. A Client is
  not thread safe ("do not use it, or the objects obtained from it, from
  several threads at the same time"), hence the file service's
  `_registry_lock` and the indexer's per-Client lock: indexer jobs on two
  filespaces of one workspace take turns. `unlink()` and `close()` now raise on failure; both
  registries log and carry on (`_release`).
- **Caches.** Each linked filespace has its own data and metadata cache, grown
  on demand up to a maximum. 0.14 defaulted to 25 GiB + 5 GiB per link, 1.0 to
  5 GiB + 1 GiB; both services now set it explicitly from
  `LUCIDLINK_CACHE_DATA_GB` / `LUCIDLINK_CACHE_METADATA_GB` (ll_cache.py,
  default 5 and 1). Worst case per service: clients x links x (data +
  metadata), 2 x 2 x 6 GiB for the indexer with the defaults. `/version`
  reports the values under `limits.cache_bytes_per_link`.
- **Entries.** `FsEntry` has `id`, `modified_at`, `created_at` (ns) and
  `inode_id`; `is_dir`/`is_file`/`is_symlink` are properties; symlinks are
  `type == "symlink"` (was `"link"`). `id` is per entry (hard links differ);
  the ledger keys on it.
- **Reads.** The indexer reads through `file_system.open(path, "rb")` (what
  `FsUtils.read_bytes` does). `statistics()` replaces `get_statistics()` and no
  longer reports file/directory/symlink counts or data size: the file
  service's `/filespaces/{name}/stats` returns null for those keys and adds
  `entriesCount`.
- **Errors.** Typed errors in `lucidlink.exceptions`; the file service maps
  authentication (401), access (403), filespace not found (404), rate limiting
  (429) and service unavailable (503) by type, and still redacts the token
  from every message it echoes.
- **No change feed.** 1.0 has no public watch API. The filespace audit trail
  (`/.lucid_audit/...`, admin-enabled, admin-readable) is the pull-based
  alternative if scans ever become too slow.

What the web app sees (file service HTTP API; routes unchanged):

- `GET /filespaces/{name}/stats`: `fileCount`, `dirCount`, `symlinkCount`,
  `dataSize`, `entriesSize` are null; new `entriesCount`.
- `GET /filespaces`: `created` is still an ISO 8601 string (now always
  `...Z`).
- `/connect/datastores`: `kind` is `"s3"` / `"http_link"` (was
  `"S3DataStore"`), `keyId` is null, HTTP-link stores appear with null S3
  fields; when Connect is unavailable the list answers
  `{"available": false, "dataStores": []}` instead of an error.
- `POST /connect/link-url` takes an optional `dataStore` (format 3.8+; default
  `LUCIDLINK_HTTP_LINK_STORE`, "http-links", created on first use).
- `GET /connect/external-files`: `cursor` is an integer offset as a string;
  pass back what the previous page returned.
- `DELETE /connect/link` refuses (400) a path that is not an external file of
  a data store; 3.6 HTTP links have no store and are deleted with
  `DELETE /files`.
- File mutations answer 404 / 409 / 400 / 403 for a missing path, an existing
  destination or non-empty directory, a file/directory mix-up, or a denied
  path, instead of 500. Read routes still answer 404 for any failure.
- `GET /version` adds `limits.cache_bytes_per_link`; the OpenAPI version is the
  SDK's (`1.0.1`).

## Connected block storage (In Place, Phase A)

The bridge from the AI Lab's "LucidLink In Place" concept, first phase: a folder
on block storage (a NAS share, external drive or local disk) is indexed **where
it lives**, read-only, with 0 bytes moved, and becomes searchable and askable
next to the filespaces. Check-out to LucidLink and check-in are later phases.

**Mounting.** The share is mounted on the Mini (e.g. `/Volumes/<share>`) and
bind-mounted into the indexer container **read-only** (`:ro`), so the container
cannot write to it whatever the code does.

| Variable | Meaning |
|---|---|
| `BLOCK_SOURCE_PATH` | Host path of the share, e.g. `/Volumes/Projects`. Mounted at `/block/source1:ro`. Unset: an empty placeholder folder is mounted and nothing is indexed. |
| `BLOCK_SOURCES` | `name=/container/path` entries separated by `;`, e.g. `NAS=/block/source1`. Empty: no block sources. |
| `BLOCK_SOURCE_WORKSPACE` | Workspace id the block sources belong to (they appear in that workspace's "all filespaces" search). Default: the vault's active workspace. |

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
