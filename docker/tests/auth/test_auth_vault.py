"""vault: encryption at rest, masking, merge-on-save, token lookup, routes."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import vault
from auth_helpers import SA_TOKEN, SA_TOKEN_2, OTHER_JWT, owner_headers


def test_round_trip_and_ciphertext_on_disk(seeded_vault, env):
    data = vault.load()
    assert data["workspaces"][0]["token"] == SA_TOKEN
    assert data["minioProfiles"][0]["secretAccessKey"] == "minio-secret"
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
    assert m["minioProfiles"][0]["secretAccessKey"] == "s3cret"


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
    assert stored["minioProfiles"][0]["id"] == "ugreen"
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
    assert body["minioProfiles"][0]["secretAccessKey"] == "minio-secret"
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
