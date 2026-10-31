"""vault: Collections (saved searches with pins) — same encrypted row as favorites."""
import json

import pytest
from fastapi.testclient import TestClient

import vault
from auth_helpers import SA_TOKEN, OTHER_JWT, owner_headers

REF = {"workspaceId": "default", "filespace": "fs1", "path": "/Clients/brief.pdf"}
COL = {
    "id": "c1", "name": "Northwind briefs", "query": "northwind brief", "mode": "all",
    "filters": {"kind": "documents", "pathPrefix": "/Clients", "modifiedAfter": "2026-01-01T00:00:00Z",
                "include": ["budget"], "exclude": [], "inName": False},
    "scope": {"workspaceId": "default", "filespace": "fs1"},
    "pinned": [REF], "excluded": [{**REF, "path": "/Clients/old.pdf"}],
    "createdAt": "2026-10-01T10:00:00Z", "updatedAt": "2026-10-02T10:00:00.123Z", "lastViewedAt": None,
}
PINS_ONLY = {"id": "c2", "name": "Pins", "query": "", "mode": "keyword", "filters": {}, "scope": None,
             "pinned": [REF, {**REF, "workspaceId": "local", "filespace": "NAS", "path": "/x.mov"}],
             "excluded": [], "createdAt": "2026-10-01T10:00:00Z", "updatedAt": "2026-10-01T10:00:00Z",
             "lastViewedAt": "2026-10-03T09:00:00Z"}


@pytest.fixture
def client():
    import lucidlink_api
    return TestClient(lucidlink_api.app)


def _raw():
    with vault._connect() as conn:
        row = conn.execute("SELECT ciphertext FROM vault WHERE id = 1").fetchone()
    return json.loads(vault._fernet().decrypt(bytes(row[0])))


def test_older_vault_is_empty_round_trip_and_unknown_fields_dropped(seeded_vault, env):
    assert vault.load_collections() == []
    stored = vault.save_collections({"collections": [
        {**COL, "color": "red", "filters": {**COL["filters"], "bogus": 1},
         "scope": {**COL["scope"], "extra": True}, "pinned": [{**REF, "note": "x"}, REF]},
        PINS_ONLY]})
    assert stored == [COL, PINS_ONLY]                 # unknown fields dropped, repeated pin kept once
    assert vault.load_collections() == stored
    # Encrypted at rest with the rest of the row.
    raw = b"".join((env / n).read_bytes() for n in ("vault.db", "vault.db-wal") if (env / n).exists())
    assert b"Northwind" not in raw


def test_defaults_for_optional_fields(seeded_vault):
    minimal = {"id": "m", "name": "M", "createdAt": "2026-10-01T10:00:00Z", "updatedAt": "2026-10-01T10:00:00Z"}
    assert vault.save_collections({"collections": [minimal]}) == [{
        **minimal, "query": "", "mode": "all", "filters": {}, "scope": None, "pinned": [], "excluded": [],
        "lastViewedAt": None}]


def test_validation_leaves_the_list_unchanged(seeded_vault):
    vault.save_collections({"collections": [COL]})
    bad = [
        [], {"collections": "x"}, {"collections": [1]},
        {"collections": [{**COL, "id": ""}]},
        {"collections": [COL, COL]},
        {"collections": [{**COL, "name": ""}]},
        {"collections": [{**COL, "name": "   "}]},
        {"collections": [{**COL, "name": "x" * 81}]},
        {"collections": [{**COL, "query": 3}]},
        {"collections": [{**COL, "mode": "fuzzy"}]},
        {"collections": [{**COL, "filters": "x"}]},
        {"collections": [{**COL, "filters": {"kind": "spreadsheets"}}]},
        {"collections": [{**COL, "filters": {"modifiedAfter": "yesterday"}}]},
        {"collections": [{**COL, "filters": {"include": "budget"}}]},
        {"collections": [{**COL, "filters": {"include": ["t"] * 21}}]},
        {"collections": [{**COL, "filters": {"inName": "yes"}}]},
        {"collections": [{**COL, "scope": {"filespace": "fs1"}}]},
        {"collections": [{**COL, "scope": "default"}]},
        {"collections": [{**COL, "pinned": [{**REF, "path": "Clients/a"}]}]},
        {"collections": [{**COL, "pinned": [{"workspaceId": "default", "path": "/a"}]}]},
        {"collections": [{**COL, "excluded": "x"}]},
        {"collections": [{**COL, "pinned": [{**REF, "path": f"/p{i}"} for i in range(501)]}]},
        {"collections": [{**COL, "excluded": [{**REF, "path": f"/p{i}"} for i in range(501)]}]},
        {"collections": [{k: v for k, v in COL.items() if k != "createdAt"}]},
        {"collections": [{**COL, "updatedAt": "nope"}]},
        {"collections": [{**COL, "lastViewedAt": 5}]},
        {"collections": [{**COL, "id": f"c{i}"} for i in range(101)]},
    ]
    for body in bad:
        with pytest.raises(ValueError):
            vault.save_collections(body)
        assert vault.load_collections() == [COL]
    many = {"collections": [{**COL, "id": f"c{i}", "pinned": [{**REF, "path": f"/p{j}"} for j in range(500)]}
                            for i in range(100)]}
    assert len(vault.save_collections(many)) == 100
    assert vault.save_collections({"collections": []}) == []


def test_settings_and_favorites_saves_keep_collections(seeded_vault):
    stored = vault.save_collections({"collections": [COL, PINS_ONLY]})
    out = vault.save({"activeWorkspaceId": "ws2"})
    assert "collections" not in out
    vault.save_favorites({"favorites": [{"id": "f1", "workspaceId": "default", "filespace": "fs1",
                                         "kind": "lucidlink", "path": "/", "addedAt": "2026-10-01T10:00:00Z"}]})
    assert vault.load_collections() == stored
    assert len(vault.load_favorites()) == 1
    # And a collections save keeps settings and favorites.
    vault.save_collections({"collections": []})
    assert len(vault.load_favorites()) == 1
    assert vault.load()["workspaces"][0]["token"] == SA_TOKEN
    assert vault.load()["activeWorkspaceId"] == "ws2"


def test_purge_keeps_collections(legacy_vault):
    vault.save_collections({"collections": [COL]})
    raw = _raw()
    assert "minioProfiles" not in raw and raw["collections"] == [COL]
    assert raw["favorites"] == legacy_vault["favorites"]
    assert vault.purge_retired() == 0
    assert _raw()["collections"] == [COL]


def test_purge_retired_leaves_stored_collections(env):
    """A legacy row that already has collections: the startup purge keeps them."""
    payload = {"workspaces": [], "activeWorkspaceId": None,
               "minioProfiles": [{"id": "m", "secretAccessKey": "s"}], "collections": [COL]}
    blob = vault._fernet().encrypt(json.dumps(payload).encode())
    with vault._connect() as conn:
        conn.execute("INSERT INTO vault (id, ciphertext, updated_at) VALUES (1, ?, ?)", (blob, "2026-01-01T00:00:00Z"))
    assert vault.purge_retired() == 1
    raw = _raw()
    assert "minioProfiles" not in raw and raw["collections"] == [COL]
    assert vault.load_collections() == [COL]


def test_routes_owner_only(client, seeded_vault, supabase):
    assert client.get("/vault/collections").status_code == 401
    assert client.get("/vault/collections", headers={"Authorization": f"Bearer {OTHER_JWT}"}).status_code == 401
    assert client.put("/vault/collections", json={"collections": []},
                      headers={"Authorization": f"Bearer {OTHER_JWT}"}).status_code == 401

    assert client.get("/vault/collections", headers=owner_headers()).json() == {"collections": []}
    r = client.put("/vault/collections", headers=owner_headers(), json={"collections": [{**COL, "extra": 1}]})
    assert r.status_code == 200 and r.json() == {"collections": [COL]}
    assert client.get("/vault/collections", headers=owner_headers()).json() == {"collections": [COL]}
    assert client.put("/vault/collections", headers=owner_headers(),
                      json={"collections": [{**COL, "mode": "x"}]}).status_code == 400
    assert client.get("/vault/collections", headers=owner_headers()).json() == {"collections": [COL]}
    settings = client.get("/vault/settings", headers=owner_headers()).json()
    assert "collections" not in settings
    r = client.put("/vault/settings", headers=owner_headers(), json={"activeWorkspaceId": "ws2"})
    assert r.status_code == 200 and "collections" not in r.json()
    assert client.get("/vault/collections", headers=owner_headers()).json() == {"collections": [COL]}


def test_503_when_vault_not_configured(client, env, supabase, monkeypatch):
    monkeypatch.delenv("VAULT_KEY")
    assert client.get("/vault/collections", headers=owner_headers()).status_code == 503
    assert client.put("/vault/collections", headers=owner_headers(),
                      json={"collections": []}).status_code == 503
