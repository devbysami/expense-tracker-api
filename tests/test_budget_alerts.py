from sqlalchemy import func, select, update

from database import SessionLocal
from notifications.models import Notification
from outbox.models import OutboxEvent
from tests.conftest import create_category, create_expense
from worker import delete_expired_idempotency_keys, process_batch


async def notification_levels() -> list[int]:
    async with SessionLocal() as db:
        return sorted((await db.scalars(select(Notification.level))).all())


async def pending_events() -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.processed_at.is_(None)))


async def test_expense_writes_outbox_event_and_no_alert_inside_the_request(client, auth_headers, storage):
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="90")
    assert await pending_events() == 1
    assert await notification_levels() == []  # nothing sent until the worker runs
    assert await process_batch(storage) == 1
    assert await notification_levels() == [80]


async def test_event_processed_twice_creates_one_notification(client, auth_headers, storage):
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="85")
    await process_batch(storage)

    # Simulate a worker crash after the work but before marking the event done -> it runs again.
    async with SessionLocal() as db:
        await db.execute(update(OutboxEvent).values(processed_at=None))
        await db.commit()
    assert await process_batch(storage) == 1

    assert await notification_levels() == [80]


async def test_crossing_100_percent_gives_both_levels_once(client, auth_headers, storage):
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="50")
    await create_expense(client, auth_headers, food, amount="40")   # 90% -> 80 alert
    await create_expense(client, auth_headers, food, amount="20")   # 110% -> 100 alert
    await create_expense(client, auth_headers, food, amount="5")    # still over: nothing new
    await process_batch(storage)
    assert await notification_levels() == [80, 100]

    notes = (await client.get("/notifications", headers=auth_headers)).json()
    assert [n["level"] for n in notes] == [100, 80] or [n["level"] for n in notes] == [80, 100]
    assert all(n["category_id"] == food for n in notes)


async def test_no_alert_under_80_percent_or_without_budget(client, auth_headers, storage):
    food = await create_category(client, auth_headers, "Food", "100")
    gifts = await create_category(client, auth_headers, "Gifts")
    await create_expense(client, auth_headers, food, amount="79.99")
    await create_expense(client, auth_headers, gifts, amount="5000")
    await process_batch(storage)
    assert await notification_levels() == []
    assert await pending_events() == 0


async def test_editing_an_expense_rechecks_the_budget(client, auth_headers, storage):
    food = await create_category(client, auth_headers, "Food", "100")
    expense = await create_expense(client, auth_headers, food, amount="10")
    await process_batch(storage)
    await client.patch(f"/expenses/{expense['id']}", json={"amount": "95"}, headers={**auth_headers, "If-Match": '"1"'})
    await process_batch(storage)
    assert await notification_levels() == [80]


async def test_failing_event_is_retried_and_does_not_block_others(client, auth_headers, storage):
    food = await create_category(client, auth_headers, "Food", "100")
    async with SessionLocal() as db:
        db.add(OutboxEvent(event_type="no.such.type", payload={}))
        await db.commit()
    await create_expense(client, auth_headers, food, amount="90")

    assert await process_batch(storage) == 2
    assert await notification_levels() == [80]  # the good event still went through
    async with SessionLocal() as db:
        bad = await db.scalar(select(OutboxEvent).where(OutboxEvent.event_type == "no.such.type"))
    assert bad.processed_at is None and bad.attempts == 1 and "unknown event type" in bad.last_error


async def test_notifications_are_per_user(client, auth_headers, storage):
    from tests.conftest import register_and_login
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="100")
    await process_batch(storage)
    bob = await register_and_login(client, "bob@example.com")
    assert (await client.get("/notifications", headers=bob)).json() == []


async def test_cleanup_of_expired_idempotency_keys(client, auth_headers):
    from sqlalchemy import text
    food = await create_category(client, auth_headers)
    await client.post("/expenses", json={"category_id": food, "amount": "1", "currency": "PKR", "spent_on": "2026-10-01"},
                      headers={**auth_headers, "Idempotency-Key": "x"})
    async with SessionLocal() as db:
        await db.execute(text("UPDATE idempotency_keys SET created_at = now() - interval '2 days'"))
        await db.commit()
    await delete_expired_idempotency_keys()
    async with SessionLocal() as db:
        assert await db.scalar(text("SELECT count(*) FROM idempotency_keys")) == 0
