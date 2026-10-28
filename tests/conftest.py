import os
from pathlib import Path

from dotenv import dotenv_values

# Point the app at the test DB. Must happen BEFORE the app (and its engine) is imported.
# Real env vars win over .env (handy in CI).
_env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or _env["TEST_DATABASE_URL"]
# Fake credentials for the local moto S3 server: tests never talk to real AWS.
os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_SESSION_TOKEN="testing")

import socket  # noqa: E402
from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

import boto3  # noqa: E402
import fakeredis  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402
from sqlalchemy import text  # noqa: E402

from core.redis_ops import get_redis  # noqa: E402
from core.storage import Storage, get_storage  # noqa: E402
from database import engine  # noqa: E402
from main import app  # noqa: E402
from models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def create_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_db():
    """Every test starts from empty tables."""
    yield
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def fake_redis():
    """In-memory Redis, fresh for every test. Swapped in for the real one via dependency_overrides."""
    fake = fakeredis.FakeAsyncRedis(decode_responses=True)
    app.dependency_overrides[get_redis] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_redis, None)
    await fake.aclose()


@pytest.fixture(scope="session")
def s3_server():
    """moto's S3 running as a local HTTP server, so the real aioboto3 code path is exercised."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def storage(s3_server):
    """Fresh bucket per test, injected into the app in place of the real S3 storage."""
    s3 = boto3.client("s3", region_name="us-east-1", endpoint_url=s3_server)
    bucket = "test-receipts"
    s3.create_bucket(Bucket=bucket)
    test_storage = Storage(bucket=bucket, region="us-east-1", endpoint_url=s3_server)
    app.dependency_overrides[get_storage] = lambda: test_storage
    yield test_storage
    app.dependency_overrides.pop(get_storage, None)
    for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
        s3.delete_object(Bucket=bucket, Key=obj["Key"])
    s3.delete_bucket(Bucket=bucket)


@pytest.fixture
async def client(fake_redis):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1") as c:
        yield c


async def register_and_login(client: AsyncClient, email: str = "a@example.com") -> dict:
    """Returns auth headers for a fresh user."""
    await client.post("/auth/register", json={"email": email, "password": "password123", "base_currency": "PKR"})
    resp = await client.post("/auth/login", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def auth_headers(client) -> dict:
    return await register_and_login(client)


async def add_rates(rows: list[tuple[str, str, str]]) -> None:
    """rows = [(currency, "2026-10-01", "0.005"), ...]"""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO exchange_rates (currency, valid_on, rate_to_usd) VALUES (:c, :d, :r)"),
            [{"c": c, "d": date.fromisoformat(d), "r": Decimal(r)} for c, d, r in rows],
        )


async def create_category(client: AsyncClient, headers: dict, name: str = "Food", budget: str | None = None) -> int:
    body = {"name": name} if budget is None else {"name": name, "monthly_budget": budget}
    resp = await client.post("/categories", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def create_expense(client: AsyncClient, headers: dict, category_id: int, **overrides) -> dict:
    body = {"category_id": category_id, "amount": "100.00", "currency": "PKR", "spent_on": "2026-10-01",
            "description": "", **overrides}
    resp = await client.post("/expenses", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()
