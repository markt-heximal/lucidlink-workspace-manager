"""Fixtures: a fake Supabase, a temp vault and the env both need.

Nothing here talks to the network or to LucidLink: Supabase and the indexer are
httpx.MockTransport handlers, and the SDK is bypassed by patching `_with_fs`.
"""
import httpx
import pytest
from cryptography.fernet import Fernet

import owner_auth
from auth_helpers import FakeSupabase, OWNER_EMAIL, SA_TOKEN, SA_TOKEN_2


@pytest.fixture
def supabase(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(owner_auth, "_transport", httpx.MockTransport(fake.handler))
    owner_auth.clear_cache()
    yield fake
    owner_auth.clear_cache()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.test")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("OWNER_EMAIL", OWNER_EMAIL)
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "vault.db"))
    monkeypatch.setenv("INDEXER_INTERNAL_KEY", "internal-secret")
    return tmp_path


@pytest.fixture
def seeded_vault(env):
    import vault
    # An older app build still sends MinIO fields; the vault drops them.
    vault.save({
        "workspaces": [
            {"id": "default", "label": "Default", "url": "https://a", "token": SA_TOKEN,
             "filespace": "fs1", "filespaces": ["fs1"]},
            {"id": "ws2", "label": "Second", "url": "https://b", "token": SA_TOKEN_2,
             "filespace": "fs2", "filespaces": ["fs2"]},
        ],
        "activeWorkspaceId": "default",
        "minioProfiles": [{"id": "ugreen", "label": "NAS", "endpoint": "https://nas",
                           "region": "us-east-1", "accessKeyId": "AKIA123",
                           "secretAccessKey": "minio-secret", "forcePathStyle": True}],
        "activeMinioProfileId": "ugreen",
    })
    return vault


LEGACY_FAVORITE = {"id": "f1", "workspaceId": "default", "filespace": "fs1", "kind": "lucidlink",
                   "path": "/Clients", "addedAt": "2026-10-01T10:00:00Z"}


@pytest.fixture
def legacy_vault(env):
    """A row written before Simplification phase 4: MinIO profiles stored encrypted."""
    import json
    import vault
    payload = {
        "workspaces": [{"id": "default", "label": "Default", "url": "https://a", "token": SA_TOKEN,
                        "filespace": "fs1", "filespaces": ["fs1"]}],
        "activeWorkspaceId": "default",
        "minioProfiles": [
            {"id": "ugreen", "accessKeyId": "AKIA123", "secretAccessKey": "minio-secret"},
            {"id": "other", "accessKeyId": "AKIA456", "secretAccessKey": "minio-secret-2"},
        ],
        "activeMinioProfileId": "ugreen",
        "favorites": [LEGACY_FAVORITE],
    }
    blob = vault._fernet().encrypt(json.dumps(payload).encode())
    with vault._connect() as conn:
        conn.execute("INSERT INTO vault (id, ciphertext, updated_at) VALUES (1, ?, ?)",
                     (blob, "2026-01-01T00:00:00Z"))
    vault._migrated.discard(vault._path())
    return payload
