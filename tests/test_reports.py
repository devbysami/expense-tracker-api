from tests.conftest import add_rates, create_category, create_expense


async def test_monthly_report_mixed_currencies_and_missing_rate_day(client, auth_headers):
    # User's base currency is PKR (see register_and_login).
    await add_rates([
        ("USD", "2026-09-01", "1"), ("PKR", "2026-09-01", "0.004"),
        ("USD", "2026-10-01", "1"), ("PKR", "2026-10-01", "0.005"),
        ("AED", "2026-10-01", "0.25"), ("AED", "2026-10-10", "0.27"),
        # no AED rate on 2026-10-15 -> the 2026-10-10 rate (0.27) must be used
        ("AED", "2026-10-20", "0.30"),
    ])
    food = await create_category(client, auth_headers, "Food", "10000")
    travel = await create_category(client, auth_headers, "Travel")

    await create_expense(client, auth_headers, food, amount="10", currency="USD", spent_on="2026-10-05")    # 2000
    await create_expense(client, auth_headers, food, amount="100", currency="AED", spent_on="2026-10-15")  # 5400
    await create_expense(client, auth_headers, food, amount="500", currency="PKR", spent_on="2026-10-20")  # 500
    await create_expense(client, auth_headers, food, amount="5", currency="USD", spent_on="2026-09-10")    # 1250
    await create_expense(client, auth_headers, travel, amount="1000", currency="PKR", spent_on="2026-09-03")
    await create_expense(client, auth_headers, travel, amount="1", currency="USD", spent_on="2026-11-01")  # next month

    resp = await client.get("/reports/monthly", params={"year": 2026, "month": 10}, headers=auth_headers)
    assert resp.status_code == 200
    report = resp.json()
    assert report["base_currency"] == "PKR"
    assert report["categories"] == [
        {"category_id": food, "category_name": "Food", "total": "7900.00", "budget": "10000.00",
         "percent_used": "79.00", "previous_month_total": "1250.00", "change_from_previous": "6650.00",
         "change_percent": "532.00"},
        {"category_id": travel, "category_name": "Travel", "total": "0.00", "budget": None,
         "percent_used": None, "previous_month_total": "1000.00", "change_from_previous": "-1000.00",
         "change_percent": "-100.00"},
    ]


async def test_report_january_compares_with_previous_december(client, auth_headers):
    food = await create_category(client, auth_headers, "Food")
    await create_expense(client, auth_headers, food, amount="300", currency="PKR", spent_on="2025-12-31")
    await create_expense(client, auth_headers, food, amount="100", currency="PKR", spent_on="2026-01-01")
    row = (await client.get("/reports/monthly", params={"year": 2026, "month": 1}, headers=auth_headers)).json()
    assert row["categories"][0]["total"] == "100.00"
    assert row["categories"][0]["previous_month_total"] == "300.00"


async def test_report_rejects_bad_month(client, auth_headers):
    resp = await client.get("/reports/monthly", params={"year": 2026, "month": 13}, headers=auth_headers)
    assert resp.status_code == 422
