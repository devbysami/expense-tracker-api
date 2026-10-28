"""
Outbox worker — a separate process:  python expense_tracker/worker.py

Loop: claim a batch of unprocessed outbox rows with FOR UPDATE SKIP LOCKED (so several workers
never take the same row), handle each in its own savepoint, mark it processed, commit.
A failing event is retried later (attempts + last_error) without blocking the others.
"""
import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

import models  # noqa: F401  (registers all tables)
from core.storage import Storage, storage as default_storage
from database import SessionLocal
from expenses.models import IdempotencyKey
from outbox.handlers import handle_event
from outbox.models import OutboxEvent

log = logging.getLogger("worker")

BATCH_SIZE = 100
MAX_ATTEMPTS = 10
IDLE_SLEEP_SECONDS = 1.0


async def process_batch(storage: Storage, batch_size: int = BATCH_SIZE) -> int:
    """Processes up to batch_size events. Returns how many were claimed."""
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(OutboxEvent.id, OutboxEvent.event_type, OutboxEvent.payload)
            .where(OutboxEvent.processed_at.is_(None), OutboxEvent.attempts < MAX_ATTEMPTS)
            .order_by(OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )).all()

        for event_id, event_type, payload in rows:
            try:
                async with db.begin_nested():  # savepoint: one bad event can't undo the others
                    await handle_event(db, event_type, payload, storage)
                values = {"processed_at": datetime.now(UTC)}
            except Exception as exc:
                log.exception("outbox event %s failed", event_id)
                values = {"attempts": OutboxEvent.attempts + 1, "last_error": str(exc)[:1000]}
            await db.execute(update(OutboxEvent).where(OutboxEvent.id == event_id).values(**values))

        await db.commit()
        return len(rows)


async def delete_expired_idempotency_keys() -> None:
    async with SessionLocal() as db:
        await db.execute(
            delete(IdempotencyKey).where(IdempotencyKey.created_at < datetime.now(UTC) - timedelta(hours=24))
        )
        await db.commit()


async def run(stop: asyncio.Event) -> None:  # pragma: no cover - long-running loop, parts are tested
    last_cleanup = datetime.min.replace(tzinfo=UTC)
    while not stop.is_set():
        try:
            claimed = await process_batch(default_storage)
            if datetime.now(UTC) - last_cleanup > timedelta(minutes=5):
                await delete_expired_idempotency_keys()
                last_cleanup = datetime.now(UTC)
        except Exception:
            log.exception("worker loop error")
            claimed = 0
        if claimed < BATCH_SIZE:  # caught up -> wait a bit (a full batch means more is waiting)
            try:
                await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP_SECONDS)
            except TimeoutError:
                pass


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    stop = asyncio.Event()

    async def _main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):  # finish the current batch, then exit
            loop.add_signal_handler(sig, stop.set)
        await run(stop)

    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
