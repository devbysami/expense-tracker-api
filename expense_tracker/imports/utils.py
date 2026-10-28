"""CSV import: streaming the upload to disk, parsing/validating rows, and the background job."""
import asyncio
import hashlib
import logging
import os
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import get_args

import pandas as pd
from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from categories.models import Category
from core.types import Currency
from database import SessionLocal
from expenses.models import Expense
from imports.models import ImportJob
from outbox.utils import add_budget_check

log = logging.getLogger(__name__)


class CsvStructureError(Exception):
    """The file itself is unusable (empty, missing columns, broken quoting) — not just one bad row."""

MAX_ROWS = 100_000
MAX_FILE_BYTES = 20 * 1024 * 1024  # ~100k rows of normal width fit easily
BATCH_SIZE = 1_000
MAX_STORED_ERRORS = 1_000  # all bad rows are counted; only the first 1000 reasons are stored
CHUNK_BYTES = 1024 * 1024
REQUIRED_COLUMNS = {"date", "amount", "currency", "category"}
SUPPORTED_CURRENCIES = set(get_args(Currency))
MAX_AMOUNT = Decimal("9999999999.99")  # NUMERIC(12, 2)


async def save_upload(upload: UploadFile) -> tuple[str, str] | None:
    """Streams the upload to a temp file in 1 MB chunks (never the whole file in memory) and hashes it
    on the way. Returns (path, sha256), or None if the file is too large."""
    sha = hashlib.sha256()
    size = 0
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "wb") as out:
        while chunk := await upload.read(CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                out.close()
                os.remove(path)
                return None
            sha.update(chunk)
            await asyncio.to_thread(out.write, chunk)  # disk write off the event loop
    return path, sha.hexdigest()


def parse_row(row: dict, categories: dict[str, int]) -> tuple[dict | None, str | None]:
    """Validates one CSV row. Returns (expense values, None) or (None, reason)."""
    problems = []

    try:
        spent_on = date.fromisoformat((row.get("date") or "").strip())
    except ValueError:
        spent_on = None
        problems.append("date must be YYYY-MM-DD")

    try:
        amount = Decimal((row.get("amount") or "").strip())
        if not amount.is_finite() or amount <= 0:
            raise InvalidOperation
        if amount.as_tuple().exponent < -2:
            problems.append("amount has more than 2 decimal places")
        elif amount > MAX_AMOUNT:
            problems.append("amount is too large")
    except InvalidOperation:
        amount = None
        problems.append("amount must be a number greater than 0")

    currency = (row.get("currency") or "").strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        problems.append(f"currency must be one of {', '.join(sorted(SUPPORTED_CURRENCIES))}")

    category_name = (row.get("category") or "").strip()
    category_id = categories.get(category_name.lower())
    if category_id is None:
        problems.append(f"unknown category '{category_name}'")

    description = (row.get("description") or "").strip()
    if len(description) > 500:
        problems.append("description is longer than 500 characters")

    if problems:
        return None, "; ".join(problems)
    return {
        "category_id": category_id,
        "amount": amount,
        "currency": currency,
        "spent_on": spent_on,
        "description": description,
    }, None


def open_csv(path: str):
    """pandas reader that yields DataFrames of BATCH_SIZE rows, so only one batch is in memory.
    dtype=str + keep_default_na=False: every cell stays the exact text from the file (no float
    guessing for amounts, no NaN for empty cells); our own validation converts it."""
    return pd.read_csv(
        path, dtype=str, keep_default_na=False, chunksize=BATCH_SIZE, encoding="utf-8-sig",
    )


async def run_import(job_id: str, user_id: int, path: str) -> None:
    """Background job. Works batch by batch: parse 1000 rows (in a thread), INSERT them in one
    statement, commit, update the counters. Between batches the event loop serves other requests."""
    try:
        async with SessionLocal() as db:
            job = await db.get(ImportJob, job_id)
            job.status = "processing"
            await db.commit()

            categories = {
                name.lower(): cid
                for cid, name in (await db.execute(
                    select(Category.id, Category.name).where(Category.user_id == user_id)
                )).all()
            }

            try:
                reader = await asyncio.to_thread(open_csv, path)
            except pd.errors.EmptyDataError:
                raise CsvStructureError("file is empty")
            with reader as chunks:
                first = True
                while True:
                    try:
                        chunk = await asyncio.to_thread(next, chunks, None)
                    except pd.errors.ParserError as exc:
                        # Broken CSV structure (e.g. wrong number of columns). Earlier batches stay imported.
                        raise CsvStructureError(str(exc)) from exc
                    if chunk is None:
                        break
                    chunk.columns = [str(c).strip().lower() for c in chunk.columns]

                    if first:
                        missing = REQUIRED_COLUMNS - set(chunk.columns)
                        if missing:
                            raise CsvStructureError(f"missing columns: {', '.join(sorted(missing))}")
                        first = False

                    good, errors = [], []
                    # chunk.index continues across chunks (0 = first data row); +2 because the header is
                    # row 1, so numbers match what a spreadsheet shows.
                    for index, row in zip(chunk.index, chunk.to_dict("records")):
                        row_number = int(index) + 2
                        if row_number - 1 > MAX_ROWS:
                            errors.append({"row": row_number, "error": f"file has more than {MAX_ROWS} rows"})
                            continue
                        values, error = parse_row(row, categories)
                        if error:
                            errors.append({"row": row_number, "error": error})
                        else:
                            # Same file + same row -> same key, so a second upload inserts nothing new.
                            good.append({**values, "user_id": user_id, "import_key": f"{job.file_sha256}:{row_number}"})

                    inserted = []
                    if good:
                        inserted = (await db.execute(
                            pg_insert(Expense)
                            .values(good)
                            .on_conflict_do_nothing(
                                index_elements=["user_id", "import_key"],
                                index_where=Expense.import_key.is_not(None),
                            )
                            .returning(Expense.category_id, Expense.spent_on)
                        )).all()
                        for category_id, month in {(c, d.replace(day=1)) for c, d in inserted}:
                            add_budget_check(db, user_id, category_id, month)

                    room = MAX_STORED_ERRORS - len(job.errors)
                    await db.execute(update(ImportJob).where(ImportJob.id == job_id).values(
                        rows_done=ImportJob.rows_done + len(chunk),
                        rows_imported=ImportJob.rows_imported + len(inserted),
                        rows_duplicate=ImportJob.rows_duplicate + len(good) - len(inserted),
                        rows_failed=ImportJob.rows_failed + len(errors),
                        errors=job.errors + errors[:max(room, 0)],
                    ))
                    await db.commit()  # each batch is durable; progress is visible to GET /imports/{id}
                    await db.refresh(job)

            job.status, job.finished_at = "completed", datetime.now(UTC)
            await db.commit()
    except Exception as exc:
        if not isinstance(exc, CsvStructureError):
            log.exception("import %s failed", job_id)
        async with SessionLocal() as db:
            job = await db.get(ImportJob, job_id)
            job.status, job.finished_at = "failed", datetime.now(UTC)
            job.errors = [*job.errors, {"row": None, "error": str(exc)[:500]}]
            await db.commit()
    finally:
        os.remove(path)
