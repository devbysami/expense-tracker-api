from sqlalchemy import func, select

from database import SessionLocal
from expenses.models import Expense
from tests.conftest import create_category, register_and_login

CSV = (
    "date,amount,currency,category,description\n"
    "2026-10-01,10.50,PKR,Food,lunch\n"          # row 2 ok
    "2026-10-02,0,PKR,Food,zero\n"               # row 3 bad amount
    "2026-13-01,5,PKR,Food,bad date\n"           # row 4 bad date
    "2026-10-03,5,XYZ,Food,bad currency\n"       # row 5 bad currency
    "2026-10-04,5,PKR,Nope,bad category\n"       # row 6 unknown category
    "2026-10-05,1.999,USD,food,3 decimals\n"     # row 7 too many decimals
    "2026-10-06,20,usd,food,case-insensitive\n"  # row 8 ok (currency + category case don't matter)
    "2026-10-07,30,AED,Travel,\n"                # row 9 ok, empty description
)


async def count_expenses() -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(Expense))


async def upload(client, headers, content: str, name: str = "expenses.csv"):
    return await client.post("/expenses/import", files={"file": (name, content.encode(), "text/csv")}, headers=headers)


async def test_import_good_and_bad_rows_gives_right_counts(client, auth_headers):
    await create_category(client, auth_headers, "Food")
    await create_category(client, auth_headers, "Travel")

    resp = await upload(client, auth_headers, CSV)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    job = (await client.get(f"/imports/{job_id}", headers=auth_headers)).json()
    assert job["status"] == "completed"
    assert (job["rows_done"], job["rows_imported"], job["rows_failed"], job["rows_duplicate"]) == (8, 3, 5, 0)
    assert [e["row"] for e in job["errors"]] == [3, 4, 5, 6, 7]
    assert "unknown category 'Nope'" in job["errors"][3]["error"]
    assert await count_expenses() == 3


async def test_same_file_twice_creates_no_duplicates(client, auth_headers):
    await create_category(client, auth_headers, "Food")
    await create_category(client, auth_headers, "Travel")
    await upload(client, auth_headers, CSV)
    second = (await upload(client, auth_headers, CSV, name="renamed.csv")).json()

    job = (await client.get(f"/imports/{second['job_id']}", headers=auth_headers)).json()
    assert (job["rows_imported"], job["rows_duplicate"]) == (0, 3)
    assert await count_expenses() == 3


async def test_import_in_multiple_batches(client, auth_headers, monkeypatch):
    import imports.utils
    monkeypatch.setattr(imports.utils, "BATCH_SIZE", 2)  # force several batches with a small file
    await create_category(client, auth_headers, "Food")
    rows = "".join(f"2026-10-{d:02d},{d},PKR,Food,row {d}\n" for d in range(1, 8))
    job_id = (await upload(client, auth_headers, "date,amount,currency,category,description\n" + rows)).json()["job_id"]
    job = (await client.get(f"/imports/{job_id}", headers=auth_headers)).json()
    assert (job["status"], job["rows_imported"]) == ("completed", 7)


async def test_import_missing_columns_fails_the_job(client, auth_headers):
    job_id = (await upload(client, auth_headers, "when,how much\n2026-10-01,5\n")).json()["job_id"]
    job = (await client.get(f"/imports/{job_id}", headers=auth_headers)).json()
    assert job["status"] == "failed"
    assert "missing columns" in job["errors"][0]["error"]


async def test_import_empty_file_fails_the_job(client, auth_headers):
    job_id = (await upload(client, auth_headers, "")).json()["job_id"]
    job = (await client.get(f"/imports/{job_id}", headers=auth_headers)).json()
    assert job["status"] == "failed"


async def test_other_users_import_job_is_404(client, auth_headers):
    job_id = (await upload(client, auth_headers, CSV)).json()["job_id"]
    bob = await register_and_login(client, "bob@example.com")
    assert (await client.get(f"/imports/{job_id}", headers=bob)).status_code == 404
