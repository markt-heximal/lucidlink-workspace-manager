const API_BASE = "https://ai-factory-mini.tail333a1d.ts.net";
const MANAGEMENT_API = API_BASE;
const FILE_SERVICE = API_BASE;

export interface Filespace {
  id: string;
  name: string;
  status?: string;
  storage?: {
    owner: string;
    provider: string;
    region: string;
    endpoint: string;
    bucketName: string;
  };
  currentStorageSize?: number;
  createdAt?: string;
  updatedAt?: string;
}

export interface FileEntry {
  name: string;
  is_dir: boolean;
  size: number;
  path: string;
}

export interface EntryInfo {
  id: string;
  parentId?: string;
  name: string;
  type: "file" | "dir";
  size?: number;
  createdAt: string;
  modifiedAt: string;
}

export interface CreateFilespaceParams {
  name: string;
  region?: string;
  endpoint?: string;
  bucketName?: string;
  accessKeyId?: string;
  secretAccessKey?: string;
}

const UGREEN_DEFAULTS = {
  endpoint: "https://ugreen-nas.tail333a1d.ts.net",
  bucketName: "lucidlink",
  accessKeyId: "59985424344315eb7d715b9e",
  secretAccessKey: "4zulHWOCAg2o4JqKC3T5WUaIrOeygAaSReVc8nv2",
  region: "us-east-1",
};

async function mgmtFetch(path: string, token: string, options: RequestInit = {}) {
  const res = await fetch(`${MANAGEMENT_API}${path}`, {
    ...options,
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: { message: res.statusText } }));
    throw new Error(body.error?.message || `Management API error: ${res.status}`);
  }
  return res.json();
}

async function fileFetch(path: string, token: string, filespace: string, options: RequestInit = {}) {
  const res = await fetch(`${FILE_SERVICE}${path}`, {
    ...options,
    headers: {
      "Authorization": `Bearer ${token}`,
      "X-LucidLink-Filespace": filespace,
      ...options.headers,
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || `File service error: ${res.status}`);
  }
  return res;
}

// --- Management API (filespace CRUD) ---

export async function listFilespaces(token: string): Promise<Filespace[]> {
  const data = await mgmtFetch("/api/v1/filespaces", token);
  return data.data;
}

export async function getFilespace(token: string, id: string): Promise<Filespace> {
  const data = await mgmtFetch(`/api/v1/filespaces/${id}`, token);
  return data.data;
}

export async function createFilespace(token: string, params: CreateFilespaceParams): Promise<Filespace> {
  const data = await mgmtFetch("/api/v1/filespaces", token, {
    method: "POST",
    body: JSON.stringify({
      name: params.name,
      region: params.region || UGREEN_DEFAULTS.region,
      storageProvider: "Other",
      storageOwner: "customer",
      customerStorageParams: {
        accessKeyId: params.accessKeyId || UGREEN_DEFAULTS.accessKeyId,
        secretAccessKey: params.secretAccessKey || UGREEN_DEFAULTS.secretAccessKey,
        endpoint: params.endpoint || UGREEN_DEFAULTS.endpoint,
        bucketName: params.bucketName || UGREEN_DEFAULTS.bucketName,
      },
    }),
  });
  return data.data;
}

export async function deleteFilespace(token: string, id: string): Promise<void> {
  await mgmtFetch(`/api/v1/filespaces/${id}`, token, { method: "DELETE" });
}

export async function updateFilespace(token: string, id: string, name: string): Promise<Filespace> {
  const data = await mgmtFetch(`/api/v1/filespaces/${id}`, token, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
  return data.data;
}

export async function resolveEntry(token: string, filespaceId: string, path: string): Promise<EntryInfo> {
  const data = await mgmtFetch(
    `/api/v1/filespaces/${filespaceId}/entries/resolve?path=${encodeURIComponent(path)}`,
    token,
  );
  return data.data;
}

export async function listChildren(token: string, filespaceId: string, entryId: string): Promise<EntryInfo[]> {
  const data = await mgmtFetch(
    `/api/v1/filespaces/${filespaceId}/entries/${encodeURIComponent(entryId)}/children`,
    token,
  );
  return data.data.entries;
}

// --- File Service / Python SDK (file content operations) ---

export async function listFiles(token: string, filespace: string, path = "/"): Promise<FileEntry[]> {
  const res = await fileFetch(`/files?path=${encodeURIComponent(path)}`, token, filespace);
  return res.json();
}

export async function readFile(token: string, filespace: string, path: string): Promise<string> {
  const res = await fileFetch(`/files/read?path=${encodeURIComponent(path)}`, token, filespace);
  const data = await res.json();
  return data.content;
}

export async function writeFile(token: string, filespace: string, path: string, content: string): Promise<void> {
  await fileFetch("/files/write", token, filespace, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
}

export async function makeDir(token: string, filespace: string, path: string): Promise<void> {
  await fileFetch(`/files/mkdir?path=${encodeURIComponent(path)}`, token, filespace, { method: "POST" });
}

export async function deleteFile(token: string, filespace: string, path: string): Promise<void> {
  await fileFetch(`/files?path=${encodeURIComponent(path)}`, token, filespace, { method: "DELETE" });
}

export async function deleteDir(token: string, filespace: string, path: string, recursive = true): Promise<void> {
  await fileFetch(
    `/files/dir?path=${encodeURIComponent(path)}&recursive=${recursive}`,
    token, filespace,
    { method: "DELETE" },
  );
}

export async function moveFile(token: string, filespace: string, src: string, dst: string): Promise<void> {
  await fileFetch("/files/move", token, filespace, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ src, dst }),
  });
}

export async function statFile(token: string, filespace: string, path: string) {
  const res = await fileFetch(`/files/stat?path=${encodeURIComponent(path)}`, token, filespace);
  return res.json();
}

export async function fileExists(token: string, filespace: string, path: string): Promise<boolean> {
  const res = await fileFetch(`/files/exists?path=${encodeURIComponent(path)}`, token, filespace);
  const data = await res.json();
  return data.exists;
}

export async function downloadFile(token: string, filespace: string, path: string): Promise<Blob> {
  const res = await fileFetch(`/files/download?path=${encodeURIComponent(path)}`, token, filespace);
  return res.blob();
}

export async function uploadFile(token: string, filespace: string, path: string, file: File): Promise<void> {
  const formData = new FormData();
  formData.append("path", path);
  formData.append("file", file);
  const res = await fetch(`${FILE_SERVICE}/files/upload`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "X-LucidLink-Filespace": filespace,
    },
    body: formData,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || `Upload failed: ${res.status}`);
  }
}

// --- File Service (filespace listing via Python SDK) ---

export async function listFilespacesSDK(token: string): Promise<{ id: string; name: string; created: string }[]> {
  const res = await fetch(`${FILE_SERVICE}/filespaces`, {
    headers: { "Authorization": `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(`Failed to list filespaces: ${res.status}`);
  const data = await res.json();
  return data.filespaces;
}

// --- Initialize filespace (link via Python SDK to bootstrap encryption) ---

export async function initializeFilespace(token: string, filespace: string): Promise<FileEntry[]> {
  return listFiles(token, filespace, "/");
}

export async function waitForReady(
  token: string,
  id: string,
  onPoll?: (status: string) => void,
  timeoutMs = 30000,
  intervalMs = 2000,
): Promise<Filespace> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const fs = await getFilespace(token, id);
    onPoll?.(fs.status || "unknown");
    if (fs.status === "ready") return fs;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error("Filespace did not become ready in time");
}
