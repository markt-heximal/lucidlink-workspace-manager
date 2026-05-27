import { useState, useCallback, useEffect } from "react";
import * as api from "@/lib/lucidlink";

export function useToken() {
  const [token, setToken] = useState<string>(() =>
    localStorage.getItem("lucidlink_token") || ""
  );

  const saveToken = useCallback((t: string) => {
    setToken(t);
    localStorage.setItem("lucidlink_token", t);
  }, []);

  const clearToken = useCallback(() => {
    setToken("");
    localStorage.removeItem("lucidlink_token");
  }, []);

  return { token, setToken: saveToken, clearToken, isAuthenticated: !!token };
}

export type CreatePhase = "idle" | "creating" | "initializing" | "ready" | "error";

export function useFilespaces(token: string) {
  const [filespaces, setFilespaces] = useState<api.Filespace[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createPhase, setCreatePhase] = useState<CreatePhase>("idle");

  const refresh = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.listFilespaces(token);
      setFilespaces(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { refresh(); }, [refresh]);

  const create = useCallback(async (params: api.CreateFilespaceParams) => {
    setCreatePhase("creating");
    setError(null);
    try {
      const fs = await api.createFilespace(token, params);
      setCreatePhase("initializing");
      await api.waitForReady(token, fs.id);
      await api.initializeFilespace(token, fs.name);
      setCreatePhase("ready");
      await refresh();
      return fs;
    } catch (e: any) {
      setCreatePhase("error");
      setError(e.message);
      throw e;
    }
  }, [token, refresh]);

  const remove = useCallback(async (id: string) => {
    await api.deleteFilespace(token, id);
    await refresh();
  }, [token, refresh]);

  return { filespaces, loading, error, refresh, create, remove, createPhase, setCreatePhase };
}

export function useFileBrowser(token: string, filespace: string) {
  const [currentPath, setCurrentPath] = useState("/");
  const [entries, setEntries] = useState<api.FileEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const navigate = useCallback(async (path: string) => {
    if (!token || !filespace) return;
    setLoading(true);
    setError(null);
    try {
      const files = await api.listFiles(token, filespace, path);
      setEntries(files);
      setCurrentPath(path);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [token, filespace]);

  useEffect(() => {
    if (token && filespace) navigate("/");
  }, [token, filespace, navigate]);

  const writeFile = useCallback(async (path: string, content: string) => {
    await api.writeFile(token, filespace, path);
    await navigate(currentPath);
  }, [token, filespace, currentPath, navigate]);

  const upload = useCallback(async (dirPath: string, file: File) => {
    const fullPath = dirPath.replace(/\/$/, "") + "/" + file.name;
    await api.uploadFile(token, filespace, fullPath, file);
    await navigate(currentPath);
  }, [token, filespace, currentPath, navigate]);

  const remove = useCallback(async (entry: api.FileEntry) => {
    if (entry.is_dir) {
      await api.deleteDir(token, filespace, entry.path);
    } else {
      await api.deleteFile(token, filespace, entry.path);
    }
    await navigate(currentPath);
  }, [token, filespace, currentPath, navigate]);

  const mkdir = useCallback(async (path: string) => {
    await api.makeDir(token, filespace, path);
    await navigate(currentPath);
  }, [token, filespace, currentPath, navigate]);

  const download = useCallback(async (path: string) => {
    const blob = await api.downloadFile(token, filespace, path);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = path.split("/").pop() || "download";
    a.click();
    URL.revokeObjectURL(url);
  }, [token, filespace]);

  const readFile = useCallback(async (path: string) => {
    return api.readFile(token, filespace, path);
  }, [token, filespace]);

  const move = useCallback(async (src: string, dst: string) => {
    await api.moveFile(token, filespace, src, dst);
    await navigate(currentPath);
  }, [token, filespace, currentPath, navigate]);

  const parentPath = currentPath === "/"
    ? null
    : currentPath.split("/").slice(0, -1).join("/") || "/";

  return {
    currentPath, entries, loading, error,
    navigate, writeFile, upload, remove, mkdir, download, readFile, move,
    parentPath,
    refresh: () => navigate(currentPath),
  };
}
