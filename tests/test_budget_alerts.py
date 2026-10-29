from sqlalchemy import func, select, update

from database import SessionLocal
from notifications.models import Notification
from outbox.handlers import process_event
from outbox.models import OutboxEvent
from tests.conftest import add_rates, create_category, create_expense


async def notification_levels() -> list[int]:
    async with SessionLocal() as db:
        return sorted((await db.scalars(select(Notification.level))).all())


async def pending_events() -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.processed_at.is_(None)))


async def all_event_ids() -> list[int]:
    async with SessionLocal() as db:
        return sorted((await db.scalars(select(OutboxEvent.id))).all())


async def test_expense_writes_outbox_event_processed_by_background_task(client, auth_headers):
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="90")
    # The background task runs after the response (httpx's ASGITransport waits for it).
    assert len(await all_event_ids()) == 1
    assert await pending_events() == 0
    assert await notification_levels() == [80]


async def test_event_processed_twice_creates_one_notification(client, auth_headers):
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="85")

    # Simulate a crash after the work but before marking the event done -> it runs again.
    async with SessionLocal() as db:
        await db.execute(update(OutboxEvent).values(processed_at=None))
        await db.commit()
    [event_id] = await all_event_ids()
    await process_event(event_id)

    assert await notification_levels() == [80]
    assert await pending_events() == 0


async def test_already_processed_event_is_skipped(client, auth_headers):
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="85")
    [event_id] = await all_event_ids()
    await process_event(event_id)  # already done by the background task -> no-op
    assert await notification_levels() == [80]


async def test_crossing_100_percent_gives_both_levels_once(client, auth_headers):
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="50")
    await create_expense(client, auth_headers, food, amount="40")   # 90% -> 80 alert
    await create_expense(client, auth_headers, food, amount="20")   # 110% -> 100 alert
    await create_expense(client, auth_headers, food, amount="5")    # still over: nothing new
    assert await notification_levels() == [80, 100]

    notes = (await client.get("/notifications", headers=auth_headers)).json()
    assert sorted(n["level"] for n in notes) == [80, 100]
    assert all(n["category_id"] == food for n in notes)


async def test_budget_total_converts_other_currencies(client, auth_headers):
    # 1 PKR = 0.004 USD -> 1 USD = 250 PKR. No rate on 10-05, so the 10-01 rate is used.
    await add_rates([("PKR", "2026-10-01", "0.004"), ("USD", "2026-10-01", "1")])
    food = await create_category(client, auth_headers, "Food", "1000")  # budget in PKR (user's base)
    await create_expense(client, auth_headers, food, amount="500", currency="PKR", spent_on="2026-10-05")
    await create_expense(client, auth_headers, food, amount="2", currency="USD", spent_on="2026-10-05")
    # 500 PKR + 2 USD * 250 = 1000 PKR = 100% (a plain SUM of amounts would be 502 -> no alert)
    assert await notification_levels() == [80, 100]


async def test_no_alert_under_80_percent_or_without_budget(client, auth_headers):
    food = await create_category(client, auth_headers, "Food", "100")
    gifts = await create_category(client, auth_headers, "Gifts")
    await create_expense(client, auth_headers, food, amount="79.99")
    await create_expense(client, auth_headers, gifts, amount="5000")
    assert await notification_levels() == []
    assert await pending_events() == 0


async def test_editing_an_expense_rechecks_the_budget(client, auth_headers):
    food = await create_category(client, auth_headers, "Food", "100")
    expense = await create_expense(client, auth_headers, food, amount="10")
    await client.patch(f"/expenses/{expense['id']}", json={"amount": "95"}, headers={**auth_headers, "If-Match": '"1"'})
    assert await notification_levels() == [80]


async def test_failing_event_stays_pending_with_error(client, auth_headers):
    async with SessionLocal() as db:
        bad = OutboxEvent(event_type="no.such.type", payload={})
        db.add(bad)
        await db.commit()
    await process_event(bad.id)

    async with SessionLocal() as db:
        bad = await db.get(OutboxEvent, bad.id)
    assert bad.processed_at is None and bad.attempts == 1 and "unknown event type" in bad.last_error


async def test_notifications_are_per_user(client, auth_headers):
    from tests.conftest import register_and_login
    food = await create_category(client, auth_headers, "Food", "100")
    await create_expense(client, auth_headers, food, amount="100")
    bob = await register_and_login(client, "bob@example.com")
    assert (await client.get("/notifications", headers=bob)).json() == []
