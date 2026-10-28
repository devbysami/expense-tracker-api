from tests.conftest import add_rates, create_category, create_expense
from core.dates import today_utc


async def test_create_category_and_duplicate_is_409(client, auth_headers):
    await create_category(client, auth_headers, "Food", "1000")
    resp = await client.post("/categories", json={"name": "Food"}, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


async def test_categories_require_auth(client):
    assert (await client.get("/categories")).status_code == 401


async def test_list_categories_with_this_months_spend_vs_budget(client, auth_headers):
    today = today_utc().isoformat()
    await add_rates([("USD", today, "1"), ("PKR", today, "0.004")])  # 1 USD = 250 PKR
    food = await create_category(client, auth_headers, "Food", "1000")
    await create_category(client, auth_headers, "Gifts")  # no budget, no spend
    await create_expense(client, auth_headers, food, amount="250", currency="PKR", spent_on=today)
    await create_expense(client, auth_headers, food, amount="1", currency="USD", spent_on=today)
    await create_expense(client, auth_headers, food, amount="999", currency="PKR", spent_on="2020-01-01")  # old month

    rows = (await client.get("/categories", headers=auth_headers)).json()
    assert rows == [
        {"id": food, "name": "Food", "monthly_budget": "1000.00", "spent_this_month": "500.00", "percent_used": "50.00"},
        {"id": rows[1]["id"], "name": "Gifts", "monthly_budget": None, "spent_this_month": "0.00", "percent_used": None},
    ]
