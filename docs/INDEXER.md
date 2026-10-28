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

The SDK's `DirEntry` carries a stable `file_id`, `size` and `mtime`, so a scan
lists metadata only and never downloads a file to find out whether it changed.

1. **Scan** breadth-first with `fs.read_dir`, skipping hidden and system folders
   (same list as the web app). Each scan has an id; every file seen is stamped
   with it.
2. **New or changed** — no ledger row for the `file_id`, or `(size, mtime)`
   differs → queued. Newest `mtime` first, so a fresh filespace is useful within
   minutes.
3. **Renamed** — same `file_id`, new path → path updated in place, vectors keep
   their embeddings (only metadata is re-upserted).
4. **Touched, not changed** — content sha256 equals the stored hash → only the
   ledger's `mtime` is updated.
5. **Deleted** — after a scan in which *every* directory listed successfully,
   files not stamped with this scan id are tombstoned and their chunks removed.
   A directory that fails to list marks its whole subtree as "unknown" for this
   scan: nothing under it is deleted. A transient error must never look like a
   deletion.
6. **Chunk-level reuse** — embeddings are cached by `(model, sha256(chunk text))`.
   An edit to one paragraph re-embeds one or two chunks, not the file.

Hints from the file service queue the hinted path immediately (and, for deletes
and moves, run a scan of the parent directory) so in-app changes show up within
seconds. A full delta scan runs every `SYNC_INTERVAL_SEC`, and on demand.

## Extraction and chunking

Plain text (`md, markdown, txt, log, csv, tsv, json, xml, yml, yaml, rtf`),
HTML (tags stripped), DOCX, PPTX and XLSX (stdlib `zipfile` + XML), PDF
(`pypdf`). Larger than `INDEX_MAX_FILE_BYTES`, binary, or unextractable →
`skipped` with a reason. Chunking matches the web app's `chunkText`: 1200
characters, 150 overlap, breaking on newline or sentence end.

## Storage and the vector index

`/data/index.db` (SQLite, WAL) is the durable source of truth:

- `filespaces(workspace_id, filespace, enabled, last_scan_at, last_sync_at, last_error, …)`
- `files(workspace_id, filespace, file_id, path, size, mtime, content_hash, status, reason, chunk_count, scan_id, indexed_at)`
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
