Build a "LucidLink Workspace Manager" app with these features:

## Core Features
1. **Token Authentication** — A settings/login screen where users paste their LucidLink service account token (format: `sa_live:...`). Store in localStorage. Show authenticated state in header.

2. **Filespace Manager** — Dashboard showing all filespaces as cards. Each card shows: name, status (ready/notReady), storage size, provider, created date. Actions: create new filespace, delete filespace, click to browse files.

3. **Create Filespace Dialog** — Form with: filespace name (required, lowercase+hyphens only), and optional fields for MinIO endpoint, bucket name, access key, secret key (all pre-filled with UGREEN defaults). On create, shows a progress indicator through these phases:
   - **"creating"** — spinner + "Creating filespace..." (POST to Management API)
   - **"initializing"** — spinner + "Initializing storage..." (polls `GET /api/v1/filespaces/{id}` every 2s until status is `"ready"`, then calls File Service `GET /files?path=/` to bootstrap encryption)
   - **"ready"** — green checkmark + "Filespace ready!" — auto-closes dialog after 1.5 seconds
   - **"error"** — red error message with a retry button
   
   The `useFilespaces` hook exposes `createPhase` (type: `"idle" | "creating" | "initializing" | "ready" | "error"`) and `setCreatePhase`. After the dialog closes, reset to `setCreatePhase("idle")`. The `create` function already handles the full flow: create → poll for ready → initialize SDK → refresh list.

4. **File Browser** — Full file/folder browser for a selected filespace. Features:
   - Breadcrumb navigation showing current path
   - Table/list of files and folders with name, size, type, modified date
   - Click folder to navigate into it
   - "Up" button to go to parent directory
   - Upload file button (drag-and-drop or file picker)
   - Create new folder button
   - Download file button
   - Delete file/folder button with confirmation
   - Read/preview text files inline
   - Move/rename files

## Architecture

### Two backend APIs (already running, no setup needed):

**Management API** — `https://spark-47d8.tail333a1d.ts.net`
- Auth: `Authorization: Bearer <service_account_token>`
- `GET /api/v1/filespaces` — list filespaces
- `POST /api/v1/filespaces` — create filespace
- `DELETE /api/v1/filespaces/{id}` — delete filespace
- `GET /api/v1/filespaces/{id}/entries/resolve?path=/` — resolve path to entry
- `GET /api/v1/filespaces/{id}/entries/{entryId}/children` — list children

**File Service (Python SDK)** — `https://ai-factory-mini.tail333a1d.ts.net`
- Auth: `Authorization: Bearer <token>` + `X-LucidLink-Filespace: <name>` headers
- `GET /filespaces` — list filespaces
- `GET /files?path=/` — list directory
- `GET /files/read?path=/file.txt` — read file content
- `POST /files/write` — write file `{"path":"/file.txt","content":"..."}`
- `POST /files/mkdir?path=/dir` — create directory
- `DELETE /files?path=/file.txt` — delete file
- `DELETE /files/dir?path=/dir&recursive=true` — delete directory
- `POST /files/move` — move/rename `{"src":"/old","dst":"/new"}`
- `GET /files/download?path=/file` — download file
- `POST /files/upload` — upload file (multipart form: path + file)
- `GET /files/stat?path=/file` — file info
- `GET /files/exists?path=/file` — check existence

### UGREEN MinIO defaults (pre-fill in create filespace form):
- Endpoint: `https://ugreen-nas.tail333a1d.ts.net`
- Bucket: `lucidlink`
- Access Key: `59985424344315eb7d715b9e`
- Secret Key: `4zulHWOCAg2o4JqKC3T5WUaIrOeygAaSReVc8nv2`
- Region: `us-east-1`

### Create filespace request body:
```json
{
  "name": "my-filespace",
  "region": "us-east-1",
  "storageProvider": "Other",
  "storageOwner": "customer",
  "customerStorageParams": {
    "accessKeyId": "59985424344315eb7d715b9e",
    "secretAccessKey": "4zulHWOCAg2o4JqKC3T5WUaIrOeygAaSReVc8nv2",
    "endpoint": "https://ugreen-nas.tail333a1d.ts.net",
    "bucketName": "lucidlink"
  }
}
```

### Filespace creation lifecycle:
1. `POST /api/v1/filespaces` — creates the filespace (returns immediately with `status: "notReady"`)
2. Poll `GET /api/v1/filespaces/{id}` every 2 seconds until `status` becomes `"ready"` (typically 3-10 seconds, timeout at 30s)
3. `GET /files?path=/` with `X-LucidLink-Filespace: <name>` header — triggers the Python SDK to bootstrap encryption keys
4. Filespace is now fully operational

The `waitForReady` function in `src/lib/lucidlink.ts` and the `createPhase` state in the `useFilespaces` hook handle this entire flow. The UI just needs to read `createPhase` to show the right indicator.

## UI/UX
- Use shadcn/ui components
- Dark mode support
- Responsive layout
- Toast notifications for success/error
- Loading states on all async operations
- Confirmation dialogs before destructive actions (delete)
- File size formatting (bytes → KB/MB/GB)
- Empty state illustrations when no filespaces or files exist
