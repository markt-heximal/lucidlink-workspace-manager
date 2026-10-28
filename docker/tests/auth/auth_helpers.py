"""Constants and fakes shared by the auth tests (imported by conftest and tests).

A uniquely named module rather than `from conftest import ...`, so it cannot
collide with another test package's conftest.
"""
import base64
import json

import httpx

OWNER_EMAIL = "owner@example.com"
OWNER_ID = "11111111-2222-3333-4444-555555555555"
SA_TOKEN = "sa_live:abcdefgh:SECRETSECRETSECRET3f2a"
SA_TOKEN_2 = "sa_live:zzzzzzzz:OTHERSECRETVALUE9b9b"


def make_jwt(sub: str = "user") -> str:
    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'sub': sub})}.c2lnbmF0dXJl"


OWNER_JWT = make_jwt("owner")
OTHER_JWT = make_jwt("someone-else")
UNCONFIRMED_JWT = make_jwt("unconfirmed")


class FakeSupabase:
    """Maps bearer -> user JSON; counts calls; can be switched to 'down'."""

    def __init__(self):
        self.calls = 0
        self.down = False
        self.status_override = None
        self.users = {
            OWNER_JWT: {"id": OWNER_ID, "email": "Owner@Example.com",
                        "email_confirmed_at": "2026-01-01T00:00:00Z"},
            OTHER_JWT: {"id": "other-id", "email": "intruder@example.com",
                        "email_confirmed_at": "2026-01-01T00:00:00Z"},
            UNCONFIRMED_JWT: {"id": "unconfirmed-id", "email": OWNER_EMAIL},
        }
        self.last_headers = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.last_headers = request.headers
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if self.status_override:
            return httpx.Response(self.status_override, json={})
        assert request.url.path == "/auth/v1/user"
        bearer = request.headers.get("authorization", "")[len("Bearer "):]
        user = self.users.get(bearer)
        if user is None:
            return httpx.Response(401, json={"msg": "invalid JWT"})
        return httpx.Response(200, json=user)


def owner_headers(**extra) -> dict:
    return {"Authorization": f"Bearer {OWNER_JWT}", **extra}
