import asyncio

from tests.conftest import register_and_login


async def test_register_returns_201_without_password(client):
    resp = await client.post(
        "/auth/register", json={"email": "x@example.com", "password": "password123", "base_currency": "AED"}
    )
    assert resp.status_code == 201
    assert "password_hash" not in resp.json()


async def test_duplicate_email_returns_409_in_error_format(client):
    body = {"email": "x@example.com", "password": "password123", "base_currency": "AED"}
    await client.post("/auth/register", json=body)
    resp = await client.post("/auth/register", json=body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


async def test_me_requires_token(client):
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_me_with_token(client):
    headers = await register_and_login(client)
    resp = await client.get("/auth/me", headers=headers)
    assert resp.json()["email"] == "a@example.com"


async def test_sixth_failed_login_returns_429(client):
    await register_and_login(client, "victim@example.com")
    bad = {"email": "victim@example.com", "password": "wrong-password"}

    for _ in range(5):
        assert (await client.post("/auth/login", json=bad)).status_code == 401

    resp = await client.post("/auth/login", json=bad)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0
    assert resp.json()["error"]["code"] == "too_many_requests"


async def test_unknown_email_is_401_not_500(client):
    resp = await client.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert resp.status_code == 401


async def login_tokens(client, email="r@example.com") -> dict:
    await client.post("/auth/register", json={"email": email, "password": "password123", "base_currency": "PKR"})
    return (await client.post("/auth/login", json={"email": email, "password": "password123"})).json()


async def test_refresh_rotates_tokens(client):
    first = await login_tokens(client)
    resp = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert resp.status_code == 200
    assert resp.json()["refresh_token"] != first["refresh_token"]


async def test_refresh_token_used_twice_revokes_all_tokens(client):
    token_a = (await login_tokens(client))["refresh_token"]
    token_b = (await client.post("/auth/refresh", json={"refresh_token": token_a})).json()["refresh_token"]

    reuse = await client.post("/auth/refresh", json={"refresh_token": token_a})  # stolen token comes back
    assert reuse.status_code == 401

    # B was never used, but must be dead now too
    assert (await client.post("/auth/refresh", json={"refresh_token": token_b})).status_code == 401


async def test_refresh_with_garbage_token_is_401(client):
    resp = await client.post("/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert resp.status_code == 401


async def test_parallel_refresh_with_same_token_only_one_wins(client):
    token = (await login_tokens(client))["refresh_token"]
    results = await asyncio.gather(
        client.post("/auth/refresh", json={"refresh_token": token}),
        client.post("/auth/refresh", json={"refresh_token": token}),
    )
    assert sorted(r.status_code for r in results) == [200, 401]


async def test_register_rejects_unsupported_base_currency(client):
    resp = await client.post(
        "/auth/register", json={"email": "x@example.com", "password": "password123", "base_currency": "XYZ"}
    )
    assert resp.status_code == 422


async def test_logout_revokes_refresh_token(client):
    tokens = await login_tokens(client)
    assert (await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})).status_code == 204
    resp = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 401


async def test_logout_with_garbage_token_is_401(client):
    assert (await client.post("/auth/logout", json={"refresh_token": "junk"})).status_code == 401


async def test_access_token_cannot_be_used_as_refresh_token(client):
    tokens = await login_tokens(client)
    resp = await client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert resp.status_code == 401


async def test_unknown_url_uses_error_format(client):
    resp = await client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_health(client):
    resp = await client.get("http://test/health")
    assert resp.json() == {"status": "ok"}
