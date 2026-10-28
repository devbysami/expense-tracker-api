import asyncio

from sqlalchemy import func, select, text

from database import SessionLocal
from expenses.models import Expense
from tests.conftest import create_category


async def count_expenses() -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(Expense))


def body(category_id: int, amount: str = "12.50") -> dict:
    return {"category_id": category_id, "amount": amount, "currency": "PKR", "spent_on": "2026-10-01"}


async def test_parallel_posts_with_same_key_create_one_expense(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    headers = {**auth_headers, "Idempotency-Key": "abc-123"}

    results = await asyncio.gather(*[client.post("/expenses", json=body(category_id), headers=headers) for _ in range(5)])

    assert {r.status_code for r in results} == {201}
    assert len({r.json()["id"] for r in results}) == 1  # everyone got the SAME expense back
    assert await count_expenses() == 1


async def test_retry_with_same_key_returns_first_response(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    headers = {**auth_headers, "Idempotency-Key": "retry-1"}
    first = await client.post("/expenses", json=body(category_id), headers=headers)
    second = await client.post("/expenses", json=body(category_id), headers=headers)
    assert second.status_code == 201
    assert second.json() == first.json()
    assert await count_expenses() == 1


async def test_same_key_different_body_is_422(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    headers = {**auth_headers, "Idempotency-Key": "k1"}
    await client.post("/expenses", json=body(category_id, "1.00"), headers=headers)
    resp = await client.post("/expenses", json=body(category_id, "2.00"), headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "idempotency_key_reused"
    assert await count_expenses() == 1


async def test_without_key_each_post_creates_an_expense(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    await client.post("/expenses", json=body(category_id), headers=auth_headers)
    await client.post("/expenses", json=body(category_id), headers=auth_headers)
    assert await count_expenses() == 2


async def test_expired_key_can_be_used_again(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    headers = {**auth_headers, "Idempotency-Key": "old"}
    await client.post("/expenses", json=body(category_id), headers=headers)
    async with SessionLocal() as db:
        await db.execute(text("UPDATE idempotency_keys SET created_at = now() - interval '25 hours'"))
        await db.commit()
    resp = await client.post("/expenses", json=body(category_id), headers=headers)
    assert resp.status_code == 201
    assert await count_expenses() == 2


async def test_failed_request_does_not_burn_the_key(client, auth_headers):
    headers = {**auth_headers, "Idempotency-Key": "k2"}
    assert (await client.post("/expenses", json=body(999), headers=headers)).status_code == 404
    category_id = await create_category(client, auth_headers)
    # Different body (fixed category) with the same key: allowed, because the failed attempt stored nothing.
    assert (await client.post("/expenses", json=body(category_id), headers=headers)).status_code == 201


async def test_keys_are_per_user(client):
    from tests.conftest import register_and_login
    alice = await register_and_login(client, "alice@example.com")
    bob = await register_and_login(client, "bob@example.com")
    a_cat, b_cat = await create_category(client, alice), await create_category(client, bob)
    r1 = await client.post("/expenses", json=body(a_cat), headers={**alice, "Idempotency-Key": "same"})
    r2 = await client.post("/expenses", json=body(b_cat), headers={**bob, "Idempotency-Key": "same"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
