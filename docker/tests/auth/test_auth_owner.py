"""owner_auth: JWT detection, owner matching, fail-closed config, caching."""
import pytest
from fastapi import HTTPException

import owner_auth
from auth_helpers import OWNER_ID, OWNER_JWT, OTHER_JWT, UNCONFIRMED_JWT, SA_TOKEN


def test_is_jwt():
    assert owner_auth.is_jwt(OWNER_JWT)
    assert not owner_auth.is_jwt(SA_TOKEN)
    assert not owner_auth.is_jwt("")
    assert not owner_auth.is_jwt(None)
    assert not owner_auth.is_jwt("eyJabc.def")          # two segments
    assert not owner_auth.is_jwt("abc.def.ghi")         # header not eyJ
    assert not owner_auth.is_jwt("eyJa b.c.d")          # not base64url


def test_owner_accepted_by_email_case_insensitive(env, supabase):
    owner = owner_auth.verify_owner(OWNER_JWT)
    assert owner.id == OWNER_ID
    assert supabase.last_headers["apikey"] == "anon-key"
    assert supabase.last_headers["authorization"] == f"Bearer {OWNER_JWT}"


def test_owner_rejected_by_email(env, supabase):
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(OTHER_JWT)
    assert e.value.status_code == 401


def test_unconfirmed_email_is_not_owner(env, supabase):
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(UNCONFIRMED_JWT)
    assert e.value.status_code == 401


def test_owner_accepted_by_id(env, supabase, monkeypatch):
    monkeypatch.delenv("OWNER_EMAIL")
    monkeypatch.setenv("OWNER_USER_ID", OWNER_ID)
    assert owner_auth.verify_owner(OWNER_JWT).id == OWNER_ID
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(OTHER_JWT)
    assert e.value.status_code == 401


def test_owner_user_id_set_ignores_email_match(env, supabase, monkeypatch):
    # OWNER_EMAIL also matches OTHER's account here, but with OWNER_USER_ID set
    # only the id counts.
    monkeypatch.setenv("OWNER_USER_ID", OWNER_ID)
    monkeypatch.setenv("OWNER_EMAIL", "intruder@example.com")
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(OTHER_JWT)
    assert e.value.status_code == 401
    assert owner_auth.verify_owner(OWNER_JWT).id == OWNER_ID


def test_invalid_session_is_401(env, supabase):
    from auth_helpers import make_jwt
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(make_jwt("expired"))
    assert e.value.status_code == 401


@pytest.mark.parametrize("unset", [("OWNER_EMAIL",), ("SUPABASE_URL",), ("SUPABASE_ANON_KEY",)])
def test_fail_closed_without_config(env, supabase, monkeypatch, unset):
    for name in unset:
        monkeypatch.delenv(name)
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(OWNER_JWT)
    assert e.value.status_code == 503
    assert supabase.calls == 0
    assert not owner_auth.owner_configured()


def test_supabase_down_is_503_and_not_cached(env, supabase):
    supabase.down = True
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(OWNER_JWT)
    assert e.value.status_code == 503
    supabase.down = False
    assert owner_auth.verify_owner(OWNER_JWT).id == OWNER_ID


def test_supabase_5xx_is_503(env, supabase):
    supabase.status_override = 500
    with pytest.raises(HTTPException) as e:
        owner_auth.verify_owner(OWNER_JWT)
    assert e.value.status_code == 503


def test_cache_hit_avoids_second_call(env, supabase):
    owner_auth.verify_owner(OWNER_JWT)
    owner_auth.verify_owner(OWNER_JWT)
    assert supabase.calls == 1
    # The raw token is never a cache key.
    assert OWNER_JWT not in owner_auth._cache


def test_negative_result_cached_briefly(env, supabase, monkeypatch):
    for _ in range(2):
        with pytest.raises(HTTPException):
            owner_auth.verify_owner(OTHER_JWT)
    assert supabase.calls == 1
    # After the negative TTL it is checked again.
    real = owner_auth.time.monotonic
    monkeypatch.setattr(owner_auth.time, "monotonic", lambda: real() + owner_auth.NEGATIVE_TTL + 1)
    with pytest.raises(HTTPException):
        owner_auth.verify_owner(OTHER_JWT)
    assert supabase.calls == 2


def test_require_owner_dependency(env, supabase):
    assert owner_auth.require_owner(f"Bearer {OWNER_JWT}").id == OWNER_ID
    for header in (None, f"Bearer {SA_TOKEN}", "Basic abc"):
        with pytest.raises(HTTPException) as e:
            owner_auth.require_owner(header)
        assert e.value.status_code == 401
