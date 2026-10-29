"""vault: encryption at rest, masking, merge-on-save, token lookup, routes."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import vault
from auth_helpers import SA_TOKEN, SA_TOKEN_2, OTHER_JWT, owner_headers


def test_round_trip_and_ciphertext_on_disk(seeded_vault, env):
    data = vault.load()
    assert data["workspaces"][0]["token"] == SA_TOKEN
    assert "minioProfiles" not in data and "activeMinioProfileId" not in data
    raw = b""
    for name in ("vault.db", "vault.db-wal"):
        p = env / name
        if p.exists():
            raw += p.read_bytes()
    assert raw
    for secret in (SA_TOKEN, "SECRETSECRETSECRET", "minio-secret"):
        assert secret.encode() not in raw
    mode = sqlite3.connect(env / "vault.db").execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_wrong_key_cannot_decrypt(seeded_vault, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("VAULT_KEY", Fernet.generate_key().decode())
    with pytest.raises(vault.VaultError):
        vault.load()


def test_masking():
    assert vault.mask_token(SA_TOKEN) == "sa_live:••••3f2a"
    assert vault.mask_token("plainlongtokenvalue1234") == "••••1234"
    assert vault.mask_token("") == ""
    assert vault.mask_token(None) == ""
    assert vault.mask_token("short") == "••••"
    m = vault.masked({
        "workspaces": [{"id": "a", "token": SA_TOKEN}, {"id": "b", "token": ""}],
        "minioProfiles": [{"id": "m", "secretAccessKey": "s3cret"}],
    })
    assert m["workspaces"][0] == {"id": "a", "token": "sa_live:••••3f2a", "tokenSet": True}
    assert m["workspaces"][1] == {"id": "b", "token": "", "tokenSet": False}
    assert "minioProfiles" not in m and "s3cret" not in str(m)


def test_merge_rules(seeded_vault):
    current = vault.masked(vault.load())
    ws_default, ws2 = current["workspaces"]
    new_ws = {"id": "ws3", "label": "Third", "token": "sa_live:new:NEWTOKENVALUE1234"}
    out = vault.save({
        "workspaces": [
            {**ws_default, "label": "Renamed"},          # masked token -> keep
            {**ws2, "token": None},                      # null -> clear
            new_ws,                                      # new workspace added
        ],
    })
    stored = vault.load()
    by_id = {w["id"]: w for w in stored["workspaces"]}
    assert by_id["default"]["token"] == SA_TOKEN
    assert by_id["default"]["label"] == "Renamed"
    assert by_id["ws2"]["token"] == ""
    assert by_id["ws3"]["token"] == "sa_live:new:NEWTOKENVALUE1234"
    assert "tokenSet" not in by_id["default"]
    # Omitted top-level keys keep their stored values.
    assert stored["activeWorkspaceId"] == "default"
    assert "minioProfiles" not in stored
    assert out["workspaces"][2]["token"] == "sa_live:••••1234"
    assert out["updatedAt"]

    # Omitted token keeps; "" flagged as set keeps; a new string replaces.
    vault.save({"workspaces": [{"id": "default"}, {"id": "ws2", "token": "replacement-token-xyz9"},
                               {"id": "ws3", "token": "", "tokenSet": True}]})
    by_id = {w["id"]: w for w in vault.load()["workspaces"]}
    assert by_id["default"]["token"] == SA_TOKEN
    assert by_id["ws2"]["token"] == "replacement-token-xyz9"
    assert by_id["ws3"]["token"] == "sa_live:new:NEWTOKENVALUE1234"


def test_save_rejects_bad_payload(seeded_vault):
    with pytest.raises(ValueError):
        vault.save({"workspaces": [{"label": "no id"}]})
    with pytest.raises(ValueError):
        vault.save({"workspaces": [{"id": "a"}, {"id": "a"}]})
    assert vault.load()["workspaces"][0]["token"] == SA_TOKEN  # unchanged


def test_token_for(seeded_vault):
    assert vault.token_for("ws2") == SA_TOKEN_2
    assert vault.token_for(None) == SA_TOKEN            # falls back to active
    assert vault.token_for("missing") is None           # explicit id never falls back
    vault.save({"activeWorkspaceId": "ws2"})
    assert vault.token_for(None) == SA_TOKEN_2
    assert [w["id"] for w in vault.workspaces()] == ["default", "ws2"]


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("VAULT_KEY", raising=False)
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "v.db"))
    assert not vault.is_configured()
    with pytest.raises(vault.VaultNotConfigured):
        vault.token_for(None)


# ---------------------------------------------------------------- routes
@pytest.fixture
def client():
    import lucidlink_api
    return TestClient(lucidlink_api.app)


def test_status_needs_no_auth(client, env):
    r = client.get("/vault/status")
    assert r.status_code == 200
    assert r.json() == {"configured": True, "ownerConfigured": True, "hasSettings": False}


def test_settings_require_owner(client, seeded_vault, supabase):
    assert client.get("/vault/settings").status_code == 401
    assert client.get("/vault/settings", headers={"Authorization": f"Bearer {SA_TOKEN}"}).status_code == 401
    assert client.get("/vault/settings", headers={"Authorization": f"Bearer {OTHER_JWT}"}).status_code == 401
    assert client.put("/vault/settings", json={}, headers={"Authorization": f"Bearer {OTHER_JWT}"}).status_code == 401

    r = client.get("/vault/settings", headers=owner_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["workspaces"][0]["token"] == "sa_live:••••3f2a"
    assert SA_TOKEN not in r.text and SA_TOKEN_2 not in r.text
    assert "minioProfiles" not in body and "activeMinioProfileId" not in body
    assert "minio-secret" not in r.text
    assert body["updatedAt"]

    r = client.put("/vault/settings", headers=owner_headers(), json={**body, "activeWorkspaceId": "ws2"})
    assert r.status_code == 200
    assert SA_TOKEN not in r.text
    assert vault.load()["workspaces"][0]["token"] == SA_TOKEN
    assert vault.token_for(None) == SA_TOKEN_2

    r = client.put("/vault/settings", headers=owner_headers(), json={"workspaces": "nope"})
    assert r.status_code == 400


def test_settings_503_when_vault_not_configured(client, env, supabase, monkeypatch):
    monkeypatch.delenv("VAULT_KEY")
    r = client.get("/vault/settings", headers=owner_headers())
    assert r.status_code == 503
    assert "VAULT_KEY" in r.json()["detail"]
    assert client.get("/vault/status").json()["configured"] is False


# ---------------------------------------------------------------- favorites
FAV = {"id": "f1", "workspaceId": "default", "filespace": "fs1", "kind": "lucidlink",
       "path": "/Clients/Northwind", "label": "Northwind", "addedAt": "2026-10-01T10:00:00Z"}


def test_favorites_older_vault_is_empty_and_settings_keep_them(seeded_vault, env):
    assert vault.load_favorites() == []
    stored = vault.save_favorites({"favorites": [{**FAV, "color": "red"},
                                                 {**FAV, "id": "f2", "kind": "block", "label": None,
                                                  "workspaceId": "local", "filespace": "NAS", "path": "/"}]})
    assert stored[0] == FAV                                   # unknown field dropped
    assert "label" not in stored[1]                           # optional
    # A settings save carries favorites over, and settings never show them.
    out = vault.save({"activeWorkspaceId": "ws2"})
    assert "favorites" not in out
    assert vault.load_favorites() == stored
    assert vault.load()["workspaces"][0]["token"] == SA_TOKEN
    # Encrypted at rest with the rest of the row.
    raw = b"".join((env / n).read_bytes() for n in ("vault.db", "vault.db-wal") if (env / n).exists())
    assert b"Northwind" not in raw


def test_favorites_validation(seeded_vault):
    bad = [
        [], {"favorites": "x"}, {"favorites": [1]},
        {"favorites": [{**FAV, "path": "Clients"}]},
        {"favorites": [{**FAV, "kind": "s3"}]},
        {"favorites": [{k: v for k, v in FAV.items() if k != "addedAt"}]},
        {"favorites": [{**FAV, "label": 3}]},
        {"favorites": [FAV, FAV]},
        {"favorites": [{**FAV, "id": f"f{i}"} for i in range(201)]},
    ]
    for body in bad:
        with pytest.raises(ValueError):
            vault.save_favorites(body)
    assert len(vault.save_favorites({"favorites": [{**FAV, "id": f"f{i}"} for i in range(200)]})) == 200
    assert vault.save_favorites({"favorites": []}) == []
    assert vault.load_favorites() == []


def test_favorites_routes_owner_only(client, seeded_vault, supabase):
    assert client.get("/vault/favorites").status_code == 401
    assert client.get("/vault/favorites", headers={"Authorization": f"Bearer {OTHER_JWT}"}).status_code == 401
    assert client.put("/vault/favorites", json={"favorites": []},
                      headers={"Authorization": f"Bearer {OTHER_JWT}"}).status_code == 401

    assert client.get("/vault/favorites", headers=owner_headers()).json() == {"favorites": []}
    r = client.put("/vault/favorites", headers=owner_headers(), json={"favorites": [{**FAV, "extra": 1}]})
    assert r.status_code == 200 and r.json() == {"favorites": [FAV]}
    assert client.get("/vault/favorites", headers=owner_headers()).json() == {"favorites": [FAV]}
    assert client.put("/vault/favorites", headers=owner_headers(),
                      json={"favorites": [{**FAV, "path": "x"}]}).status_code == 400
    assert client.get("/vault/favorites", headers=owner_headers()).json() == {"favorites": [FAV]}
    assert "favorites" not in client.get("/vault/settings", headers=owner_headers()).json()


def test_favorites_503_when_vault_not_configured(client, env, supabase, monkeypatch):
    monkeypatch.delenv("VAULT_KEY")
    assert client.get("/vault/favorites", headers=owner_headers()).status_code == 503


# ---------------------------------------------------------------- MinIO retired (phase 4)
def _raw_payload():
    import json
    with vault._connect() as conn:
        row = conn.execute("SELECT ciphertext, updated_at FROM vault WHERE id = 1").fetchone()
    return json.loads(vault._fernet().decrypt(bytes(row[0]))), row[1]


def test_purge_retired_migration(legacy_vault, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="vault"):
        assert vault.purge_retired() == 2
    raw, updated_at = _raw_payload()
    assert "minioProfiles" not in raw and "activeMinioProfileId" not in raw
    assert raw["workspaces"] == legacy_vault["workspaces"]
    assert raw["activeWorkspaceId"] == "default"
    assert raw["favorites"] == legacy_vault["favorites"]
    assert updated_at == "2026-01-01T00:00:00Z"            # settings unchanged
    assert "purged 2 retired MinIO profile(s)" in caplog.text
    for secret in ("minio-secret", "AKIA123", "ugreen", SA_TOKEN):
        assert secret not in caplog.text                   # count only, never values
    assert vault.purge_retired() == 0                      # idempotent
    assert vault.token_for(None) == SA_TOKEN


def test_purge_on_write(legacy_vault):
    vault.save({"activeWorkspaceId": "default"})
    raw, _ = _raw_payload()
    assert "minioProfiles" not in raw and "activeMinioProfileId" not in raw
    assert raw["favorites"] == legacy_vault["favorites"]
    assert raw["workspaces"][0]["token"] == SA_TOKEN


def test_purge_on_favorites_write(legacy_vault):
    vault.save_favorites({"favorites": legacy_vault["favorites"]})
    raw, _ = _raw_payload()
    assert "minioProfiles" not in raw
    assert raw["workspaces"] == legacy_vault["workspaces"]


def test_first_read_migrates(client, legacy_vault, supabase):
    r = client.get("/vault/settings", headers=owner_headers())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"workspaces", "activeWorkspaceId", "updatedAt"}
    assert body["workspaces"][0]["token"] == "sa_live:••••3f2a"
    assert "minio" not in r.text.lower()
    raw, _ = _raw_payload()
    assert "minioProfiles" not in raw and raw["favorites"] == legacy_vault["favorites"]
    assert client.get("/vault/favorites", headers=owner_headers()).json() == {
        "favorites": legacy_vault["favorites"]}


def test_startup_migrates(legacy_vault):
    import lucidlink_api
    with TestClient(lucidlink_api.app):
        pass
    raw, _ = _raw_payload()
    assert "minioProfiles" not in raw and "activeMinioProfileId" not in raw
    assert raw["favorites"] == legacy_vault["favorites"]


def test_ensure_migrated_without_key_is_quiet(monkeypatch, tmp_path):
    monkeypatch.delenv("VAULT_KEY", raising=False)
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "v.db"))
    vault.ensure_migrated()                                # no raise


def test_older_app_put_with_minio_fields_is_accepted_and_dropped(client, seeded_vault, supabase):
    vault.save_favorites({"favorites": [FAV]})
    r = client.put("/vault/settings", headers=owner_headers(), json={
        "workspaces": [{"id": "default", "token": "sa_live:••••3f2a", "tokenSet": True},
                       {"id": "ws2"}],
        "activeWorkspaceId": "ws2",
        "minioProfiles": [{"id": "x", "secretAccessKey": "new-minio-secret"}],
        "activeMinioProfileId": "x",
    })
    assert r.status_code == 200
    assert "minio" not in r.text.lower()
    # Even malformed retired fields are ignored, not rejected.
    r = client.put("/vault/settings", headers=owner_headers(),
                   json={"minioProfiles": "nope", "activeMinioProfileId": 5})
    assert r.status_code == 200
    raw, _ = _raw_payload()
    assert "minioProfiles" not in raw and "activeMinioProfileId" not in raw
    assert raw["activeWorkspaceId"] == "ws2"
    assert [w["token"] for w in raw["workspaces"]] == [SA_TOKEN, SA_TOKEN_2]
    assert raw["favorites"] == [FAV]
