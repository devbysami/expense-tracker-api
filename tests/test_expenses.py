import asyncio
from datetime import date, timedelta

import pytest

from tests.conftest import create_category, create_expense, register_and_login


@pytest.mark.parametrize("amount", ["0", "-5", "1.234"])
async def test_bad_amount_rejected(client, auth_headers, amount):
    category_id = await create_category(client, auth_headers)
    resp = await client.post(
        "/expenses",
        json={"category_id": category_id, "amount": amount, "currency": "PKR", "spent_on": "2026-10-01"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_amount_is_returned_as_exact_decimal(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    expense = await create_expense(client, auth_headers, category_id, amount="0.10")
    assert expense["amount"] == "0.10"


async def test_unsupported_currency_rejected(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    resp = await client.post(
        "/expenses",
        json={"category_id": category_id, "amount": "1", "currency": "XYZ", "spent_on": "2026-10-01"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_other_users_expense_is_404(client):
    alice = await register_and_login(client, "alice@example.com")
    bob = await register_and_login(client, "bob@example.com")
    expense = await create_expense(client, alice, await create_category(client, alice))

    assert (await client.get(f"/expenses/{expense['id']}", headers=bob)).status_code == 404
    patch = await client.patch(f"/expenses/{expense['id']}", json={"description": "x"},
                               headers={**bob, "If-Match": '"1"'})
    assert patch.status_code == 404
    assert (await client.delete(f"/expenses/{expense['id']}", headers=bob)).status_code == 404
    # ...and alice's expense is untouched
    assert (await client.get(f"/expenses/{expense['id']}", headers=alice)).status_code == 200


async def test_expense_with_other_users_category_is_404(client):
    alice = await register_and_login(client, "alice@example.com")
    bob = await register_and_login(client, "bob@example.com")
    alice_category = await create_category(client, alice)
    resp = await client.post(
        "/expenses",
        json={"category_id": alice_category, "amount": "1", "currency": "PKR", "spent_on": "2026-10-01"},
        headers=bob,
    )
    assert resp.status_code == 404


async def test_get_returns_etag_and_patch_updates(client, auth_headers):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    resp = await client.get(f"/expenses/{expense['id']}", headers=auth_headers)
    assert resp.headers["ETag"] == '"1"'

    patched = await client.patch(f"/expenses/{expense['id']}", json={"amount": "55.50"},
                                 headers={**auth_headers, "If-Match": resp.headers["ETag"]})
    assert patched.status_code == 200
    assert patched.json()["amount"] == "55.50"
    assert patched.headers["ETag"] == '"2"'


async def test_patch_without_if_match_is_428(client, auth_headers):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    resp = await client.patch(f"/expenses/{expense['id']}", json={"description": "x"}, headers=auth_headers)
    assert resp.status_code == 428


async def test_patch_with_stale_etag_is_412(client, auth_headers):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    url, headers = f"/expenses/{expense['id']}", {**auth_headers, "If-Match": '"1"'}
    assert (await client.patch(url, json={"description": "first"}, headers=headers)).status_code == 200
    resp = await client.patch(url, json={"description": "second"}, headers=headers)
    assert resp.status_code == 412
    assert resp.json()["error"]["code"] == "version_conflict"


async def test_patch_with_garbage_if_match_is_400(client, auth_headers):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    resp = await client.patch(f"/expenses/{expense['id']}", json={"description": "x"},
                              headers={**auth_headers, "If-Match": "nonsense"})
    assert resp.status_code == 400


async def test_parallel_patches_one_wins_one_412(client, auth_headers):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    etag = (await client.get(f"/expenses/{expense['id']}", headers=auth_headers)).headers["ETag"]

    async def patch(text):
        return await client.patch(
            f"/expenses/{expense['id']}", json={"description": text}, headers={**auth_headers, "If-Match": etag}
        )

    results = await asyncio.gather(patch("phone 1"), patch("phone 2"))
    assert sorted(r.status_code for r in results) == [200, 412]


async def test_delete_expense(client, auth_headers):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    assert (await client.delete(f"/expenses/{expense['id']}", headers=auth_headers)).status_code == 204
    assert (await client.get(f"/expenses/{expense['id']}", headers=auth_headers)).status_code == 404


async def test_list_filters(client, auth_headers):
    food = await create_category(client, auth_headers, "Food")
    travel = await create_category(client, auth_headers, "Travel")
    await create_expense(client, auth_headers, food, amount="10", currency="PKR", spent_on="2026-10-01",
                         description="Coffee 100% arabica")
    await create_expense(client, auth_headers, food, amount="50", currency="USD", spent_on="2026-10-05")
    await create_expense(client, auth_headers, travel, amount="500", currency="AED", spent_on="2026-09-20",
                         description="Taxi")

    async def ids(**params):
        resp = await client.get("/expenses", params=params, headers=auth_headers)
        assert resp.status_code == 200
        return [e["amount"] for e in resp.json()["items"]]

    assert await ids() == ["50.00", "10.00", "500.00"]  # spent_on desc
    assert await ids(category_id=travel) == ["500.00"]
    assert await ids(date_from="2026-10-01", date_to="2026-10-04") == ["10.00"]
    assert await ids(min_amount="20", max_amount="100") == ["50.00"]
    assert await ids(currency="AED") == ["500.00"]


async def test_cursor_pagination_no_duplicates_or_gaps_while_rows_are_added(client, auth_headers):
    category_id = await create_category(client, auth_headers)
    start = date(2026, 9, 1)
    original = set()
    for i in range(25):  # several rows share a date, so the id tie-breaker matters
        e = await create_expense(client, auth_headers, category_id, spent_on=(start + timedelta(days=i // 3)).isoformat())
        original.add(e["id"])

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 7} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/expenses", params=params, headers=auth_headers)).json()
        seen += [e["id"] for e in page["items"]]
        pages += 1
        # Someone adds expenses between page requests (newest date -> they'd sort first).
        await create_expense(client, auth_headers, category_id, spent_on="2026-10-28")
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert pages == 4
    assert len(seen) == len(set(seen))  # no duplicates
    assert set(seen) == original  # nothing missing, and rows added later don't leak into later pages


async def test_invalid_cursor_is_400(client, auth_headers):
    resp = await client.get("/expenses", params={"cursor": "not-a-cursor"}, headers=auth_headers)
    assert resp.status_code == 400
