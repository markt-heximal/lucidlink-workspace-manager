# LucidLink Agent — Feature Expansion Brief (SDK 0.12 capabilities)

**Paste this whole document into Lovable.** It extends the existing LucidLink
Workspace Manager app with the capabilities unlocked by LucidLink Python SDK
0.12: **Connect / S3 external assets, large-file streaming & media preview,
"Ask your filespace" analytics, a filespace stats dashboard, direct-link
sharing, and a multi-filespace switcher.**

---

## 0. Context — what already exists (do NOT rebuild these)

- This is a React + TypeScript app that talks to a FastAPI backend.
- **Base URL:** `API_BASE = "https://ai-factory-mini.tail333a1d.ts.net"` (already set in `src/lib/lucidlink.ts`).
- **Auth on every request:** `Authorization: Bearer <token>`. **Data-plane (file) requests also send** `X-LucidLink-Filespace: <filespace name>`.
- **Existing helpers in `src/lib/lucidlink.ts`:** `mgmtFetch(path, token, options)` and `fileFetch(path, token, filespace, options)`. **Reuse these** — add new methods next to the existing ones; don't rewrite the file.
- **Existing hooks in `src/hooks/useLucidLink.ts`:** `useToken`, `useFilespaces`, `useFileBrowser`. Follow the same patterns (react-query style, token from context) for new hooks.
- Existing types: `Filespace`, `FileEntry`, `EntryInfo`. Extend, don't duplicate.

### Ground rules
1. **Never put cloud secrets in frontend code or state.** S3 access keys are entered in a form, sent once to the backend over HTTPS, and never read back (the API returns masked values only). Remove reliance on any hardcoded `secretAccessKey`.
2. Add features as **new routes/tabs**; keep the current file browser working.
3. Every async surface has explicit **loading, empty, and error** states. Show the backend's error `detail` message, don't swallow it.
4. All new endpoints live at the same `API_BASE` and use the same auth headers (Bearer, plus `X-LucidLink-Filespace` where a filespace is involved).

### Design system (this is a build requirement, not polish)
- Clean, professional, **media-studio** aesthetic (the users are creative/M&E teams). Dark mode default, light mode supported.
- Consistent component library (shadcn/ui + Tailwind). Rounded cards, subtle borders, one accent color for primary actions.
- Responsive (desktop-first, works on tablet). Tables scroll horizontally inside their own container; the page never scrolls sideways.
- Accessible: labelled inputs, keyboard-navigable dialogs, visible focus, aria-live on async results.
- Use skeleton loaders (not spinners) for lists/tables; toasts for action success/failure; optimistic UI only for safe, reversible actions.

---

## 1. LucidLink Connect — link external S3 / HTTP assets into a filespace

**Concept (one line):** "Connect" maps objects that live in an external S3 bucket (or any HTTP URL) to paths inside a LucidLink filespace, so they stream to every client without copying the bytes in.

### New page: **Connect** (top-level nav item, cloud/link icon)

Two sections:

**A) Data Stores** — manage S3 credentials for a filespace.
- Table of connected data stores: name, kind (S3), endpoint, bucket, region, availability badge (Available / Unavailable), external-file count.
- "Add data store" dialog — fields: `name`, `endpoint`, `region`, `bucket`, `accessKeyId`, `secretAccessKey` (password input, never echoed back). Submit → toast, refresh table.
- Row actions: **Rekey** (re-enter credentials), **Remove** (confirm dialog).

**B) Linked Assets** — the S3/HTTP objects mapped into the current filespace.
- Table: filespace path, source (S3 data-store name or HTTP host), object key / URL, size, status.
- "Link asset" dialog with two modes:
  - **From S3 object:** pick a data store, enter `objectKey`, target `filePath`, optional `size`.
  - **From URL:** enter `url`, target `filePath`, optional `size`.
- **Bulk link:** paste or upload CSV (`filePath,objectKey[,size]`) to link many objects at once; show a progress list with per-row success/failure.
- Unlink action per row (removes the mapping, not the source object).

### Backend contract (the app calls these; see Appendix for full shapes)
```
GET    /connect/datastores                       (Bearer + X-LucidLink-Filespace)
POST   /connect/datastores        {name,endpoint,region,bucket,accessKeyId,secretAccessKey}
DELETE /connect/datastores/{name}
POST   /connect/datastores/{name}/rekey  {accessKeyId,secretAccessKey}
GET    /connect/datastores/available             -> {available: boolean}
POST   /connect/link              {dataStore,objectKey,filePath,size?}
POST   /connect/link-url          {url,filePath,size?}
GET    /connect/external-files?dataStore=<name>  -> [{filePath,source,key,size}]
DELETE /connect/link              {filePath}
```

---

## 2. Large-file streaming & media preview

**Concept:** don't load whole files into memory — stream ranges. Preview media inline.

- In the **file browser**, when a file is an image/video/audio/pdf, show a **Preview** pane:
  - `<video controls>` / `<audio controls>` / `<img>` whose `src` points at `GET /files/download?path=…` with the auth headers (use a short-lived object URL or a fetch-with-Range wrapper). The backend supports HTTP **Range** so the browser can seek without downloading the whole file.
  - Text/code files (< 1 MB): show `GET /files/read` content in a read-only viewer with syntax highlighting.
- **Uploads:** replace any "load entire file then POST" logic with a **progress-tracked upload** (`POST /files/upload`, multipart) showing a percentage bar; support drag-and-drop; allow large files (multi-GB) without freezing the tab.
- **Downloads:** show a progress indicator for large downloads.

### Backend contract
```
GET  /files/download?path=<p>     (Range-aware; returns bytes + Content-Type)  [exists — make UI Range-aware]
POST /files/upload                (multipart: path, file)                       [exists — add progress UI]
```

---

## 3. "Ask your filespace" — analytics & RAG agent

**Concept (one line):** the backend uses the SDK's fsspec integration to read data
files directly from the filespace and answer questions / preview tabular data —
this is what makes it an *Agent* app, not just a file browser.

### New page: **Ask** (sparkles/chat icon)
- A chat panel scoped to the **current filespace**. User asks a question ("summarize the latest edit notes", "how many shots are in /project/reels?", "what's in this CSV?").
- Render the answer as markdown, plus a **"Sources"** list of the filespace paths the answer drew from (clickable → open in file browser / preview).
- Show a thinking/streaming state; keep a short per-filespace history.

### Inline **Data preview** (in the file browser)
- For `.csv` / `.parquet` / `.tsv`, a "Preview data" action opens a **table** (first ~100 rows) with column headers and row count — backed by `POST /data/preview`.

### Backend contract
```
POST /agent/query    {filespace, question}  -> {answer: string(markdown), sources: [path...]}
POST /data/preview   {filespace, path, limit?} -> {columns:[...], rows:[[...]], totalRows:int}
```
> Note: `/agent/query` needs an LLM on the backend. It can point at the local
> Ollama the rest of the stack uses, or an API key — the backend owns that; the
> frontend just calls the endpoint.

---

## 4. Filespace analytics dashboard

**Concept:** surface the SDK's `get_size()` / `get_statistics()` per filespace.

- New **Dashboard** landing view (or a panel on each filespace):
  - Stat cards: **Total size**, **File count**, **Directory count**, **External (Connect) file count**.
  - A small bar/donut of storage by type if available.
  - Refresh button; cache for ~60s.
- Add a compact stats strip to the top of the file browser for the selected filespace.

### Backend contract
```
GET /filespaces/{name}/stats  -> {sizeBytes,fileCount,dirCount,externalFileCount, dataSize?, storageSize?}
```

---

## 5. Direct-link sharing

**Concept:** generate a shareable direct link to a file/folder via the Management API (already proxied at `/api/v1/*`).

- Add a **Share** action (link icon) to every file/folder row and to the preview pane.
- Opens a dialog that requests a direct link, shows it with a **Copy** button and (if returned) an expiry, and a QR code for quick mobile hand-off.

### Backend contract
```
POST /api/v1/<direct-link endpoint>  {filespace, path, ...}  -> {url, expiresAt?}
```
> Use the existing `mgmtFetch` helper. Confirm the exact Management-API path/shape
> against the backend before wiring (the proxy passes it straight through).

---

## 6. Multi-filespace switcher (quality-of-life)

- A persistent **filespace switcher** in the header (searchable dropdown) listing all filespaces from `GET /filespaces`, remembering recents.
- Internally prefer the filespace **`id`** (stable) over `name` (mutable) when the backend accepts it, so renames don't break links. Keep `X-LucidLink-Filespace` by name for now if that's what the backend expects, but store id alongside.

---

## Client & hook work (tie it together)
- **`src/lib/lucidlink.ts`:** add typed methods for every endpoint above, reusing `mgmtFetch` / `fileFetch`. Add interfaces: `DataStore`, `ExternalFile`, `FilespaceStats`, `DataPreview`, `AgentAnswer`, `DirectLink`.
- **`src/hooks/useLucidLink.ts`:** add `useDataStores`, `useExternalFiles`, `useFilespaceStats`, `useAgentQuery`, `useDataPreview`, `useDirectLink`, following the existing hook style.
- Keep all filespace-scoped calls passing the current filespace; don't hardcode.

## Acceptance criteria (Lovable: verify each)
- [ ] Connect page: add/list/rekey/remove data stores; link an S3 object and a URL; bulk-link via CSV; unlink. Secrets never appear in responses or client state.
- [ ] Media files preview inline; video seeks without full download; large uploads show progress and don't freeze the tab.
- [ ] Ask page returns a markdown answer with clickable sources; CSV/Parquet preview renders a table.
- [ ] Dashboard shows size + counts per filespace and refreshes.
- [ ] Share produces a copyable direct link with a QR code.
- [ ] Filespace switcher works and persists recents.
- [ ] Every new surface has loading / empty / error states and is responsive + dark/light themed.

---

## Appendix — backend endpoint contract (single source of truth)

These are the endpoints the frontend targets. All require `Authorization: Bearer <token>`;
those operating on filespace contents also require `X-LucidLink-Filespace: <name>`.
(The backend maps them to SDK 0.12 `ConnectManager`, `Filesystem.open/get_size/get_statistics`,
the fsspec integration, and the Management-API proxy respectively.)

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | /connect/datastores | — | `[{name,kind,endpoint,bucket,region,available,externalFileCount}]` |
| POST | /connect/datastores | `{name,endpoint,region,bucket,accessKeyId,secretAccessKey}` | `{name,...}` (no secret) |
| POST | /connect/datastores/{name}/rekey | `{accessKeyId,secretAccessKey}` | `{status:"ok"}` |
| DELETE | /connect/datastores/{name} | — | `{status:"ok"}` |
| GET | /connect/datastores/available | — | `{available:boolean}` |
| POST | /connect/link | `{dataStore,objectKey,filePath,size?}` | `{status:"ok"}` |
| POST | /connect/link-url | `{url,filePath,size?}` | `{status:"ok"}` |
| GET | /connect/external-files | `?dataStore=<name>` | `[{filePath,source,key,size}]` |
| DELETE | /connect/link | `{filePath}` | `{status:"ok"}` |
| GET | /filespaces/{name}/stats | — | `{sizeBytes,fileCount,dirCount,externalFileCount,dataSize?,storageSize?}` |
| POST | /data/preview | `{filespace,path,limit?}` | `{columns,rows,totalRows}` |
| POST | /agent/query | `{filespace,question}` | `{answer,sources}` |
| GET | /files/download | `?path=<p>` (Range-aware) | file bytes |
| POST | /files/upload | multipart `path,file` | `{status,path,size}` |
| POST | /api/v1/<direct-link> | `{filespace,path,...}` | `{url,expiresAt?}` |

### Implementation notes — ACTUAL response shapes as built (backend deployed 2026-07-05)

These reflect what the backend returns today; build the client to these exact shapes:

- `GET /connect/datastores` → `{"available": bool, "dataStores": [{name,kind,endpoint,bucket,region,keyId,accessKey(masked),urlExpirationMinutes,useVirtualAddressing,rekeyState,externalFileCount}]}`. **secretAccessKey is never returned; accessKey is masked.**
- `POST /connect/datastores` → the same store object (no secret). `rekey`/`DELETE`/`link*`/`unlink` → `{"status":"ok",...}`.
- `GET /connect/external-files?dataStore=` → `{"files":[{filePath,fileId,source}], "hasMore":bool, "cursor":str}` (the SDK list returns paths+ids, not per-file key/size).
- **Connect requires the filespace to be V9+ and have Connect enabled.** If not, every /connect/* call returns **409** with `{"detail":"Connect (external files) is not available…"}`. Show an "enable Connect" empty-state, not an error.
- `GET /filespaces/{name}/stats` → `{sizeBytes,fileCount,dirCount,symlinkCount,externalFileCount,dataSize,storageSize,entriesSize,externalFilesSize}`.
- `POST /data/preview` → `{columns:[...], rows:[[...]], rowsReturned:int, truncated:bool}` (CSV/TSV only; parquet → 415).
- `POST /agent/query` → `{answer:str(markdown), sources:[path,...]}`. **Latency note:** with the current on-prem 32B model a query takes ~60–100s — show a patient "thinking…" state (streamed typing look) and a generous client timeout. Returns **503** if the LLM isn't configured/reachable.
- `GET /files/download` with a `Range` header → **206** + `Content-Range`/`Accept-Ranges` (use for video/audio seeking); without Range → full file. `POST /files/upload` is multipart `path,file`.
