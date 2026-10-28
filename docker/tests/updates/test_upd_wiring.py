"""The update monitor is mounted on the public file service, so it must be owner-only."""
from fastapi.testclient import TestClient

import lucidlink_api


def test_engine_updates_requires_owner():
    client = TestClient(lucidlink_api.app)
    assert client.get("/engine/updates").status_code == 401
    assert client.post("/engine/updates/check").status_code == 401
    # A LucidLink service-account token is not the owner.
    lucid = {"Authorization": "Bearer sa_live:abc:def"}
    assert client.get("/engine/updates", headers=lucid).status_code == 401
