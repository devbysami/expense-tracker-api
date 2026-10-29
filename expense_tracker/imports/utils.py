"""CSV import: streaming the upload to disk, parsing/validating rows, and the background job."""
import asyncio
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
from outbox.handlers import process_event
from outbox.utils import add_budget_check

log = logging.getLogger(__name__)


class CsvStructureError(Exception):
    """The file itself is unusable (empty, missing columns, broken quoting) — not just one bad row."""

MAX_ROWS = 100_000
MAX_FILE_BYTES = 20 * 1024 * 1024  # ~100k rows of normal width fit easily
BATCH_SIZE = 1000
MAX_STORED_ERRORS = 1000  # all bad rows are counted; only the first 1000 reasons are stored
CHUNK_BYTES = 1024 * 1024
REQUIRED_COLUMNS = {"date", "amount", "currency", "category"}
SUPPORTED_CURRENCIES = set(get_args(Currency))
MAX_AMOUNT = Decimal("9999999999.99")  # NUMERIC(12, 2)


async def save_upload(upload: UploadFile) -> str | None:
    
    """Streams the upload to a temp file in 1 MB chunks (never the whole file in memory).
    Returns the path, or None if the file is too large."""
    
    size = 0
    fd, path = tempfile.mkstemp(suffix=".csv")
    
    with os.fdopen(fd, "wb") as out:
        
        while chunk := await upload.read(CHUNK_BYTES):
            
            size += len(chunk)
            
            if size > MAX_FILE_BYTES:
                out.close()
                os.remove(path)
                return None
            
            await asyncio.to_thread(out.write, chunk)  # disk write off the event loop
            
    return path


def make_import_key(values: dict, row_number: int) -> str:
    """The same row of the same file for the same user always gives the same key.
    Parts are joined with ":" so e.g. category 1 + row 23 can't look like category 12 + row 3."""
    version = 1  # a new expense always starts at version 1
    return ":".join([
        str(values["category_id"]),
        str(row_number),
        f"{values['amount']:.2f}",  # "10.5" and "10.50" give the same key
        str(version),
        values["spent_on"].isoformat(),
        str(values["user_id"]),
    ])


def parse_row(
    row: dict, row_number: int, categories: dict[str, int], user_id: int
) -> tuple[dict | None, str | None]:
    
    """Validates one CSV row. Returns (expense values incl. user_id and import_key, None) or (None, reason)."""
    
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
    
    values = {
        "user_id": user_id,
        "category_id": category_id,
        "amount": amount,
        "currency": currency,
        "spent_on": spent_on,
        "description": description,
    }
    
    import_key = make_import_key(values, row_number)
    
    values["import_key"] = import_key
    return values, None


def check_file(path: str) -> None:
    """Rejects a file that can't be imported at all, before a single row is inserted."""

    try:
        header = pd.read_csv(path, nrows=0, encoding="utf-8-sig")  # reads only the header line
    except pd.errors.EmptyDataError:
        raise CsvStructureError("file is empty")

    missing = REQUIRED_COLUMNS - {str(c).strip().lower() for c in header.columns}
    if missing:
        raise CsvStructureError(f"missing columns: {', '.join(sorted(missing))}")

    with open(path, encoding="utf-8-sig") as f:
        line_count = sum(1 for _ in f)
        
    if line_count - 1 > MAX_ROWS:
        raise CsvStructureError(f"file has more than {MAX_ROWS} rows")


async def insert_expenses(db, rows: list[dict]) -> list:
    """One INSERT for the whole batch. ON CONFLICT on the unique constraint is the safety net for two
    imports of the same file running at the same time; only the newly inserted rows come back."""

    if not rows:
        return []

    result = await db.execute(
        pg_insert(Expense)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_expenses_user_import_key")
        .returning(Expense.import_key, Expense.category_id, Expense.spent_on)
    )
    return result.all()


async def run_import(job_id: str, user_id: int, path: str) -> None:
    
    """Background job: check the file, then import it BATCH_SIZE rows at a time
    (one INSERT + one commit per batch, so GET /imports/{id} shows progress)."""

    async with SessionLocal() as db:

        job = await db.get(ImportJob, job_id)
        job.status = "processing"
        await db.commit()

        try:
            
            check_file(path)

            result = await db.execute(select(Category.id, Category.name).where(Category.user_id == user_id))
            categories = {name.lower(): category_id for category_id, name in result}

            # dtype=str + keep_default_na=False: every cell stays the exact text from the file
            # (no float guessing for amounts, no NaN for empty cells); parse_row converts it.
            chunks = pd.read_csv(
                path, dtype=str, keep_default_na=False, chunksize=BATCH_SIZE, encoding="utf-8-sig",
            )
            
            row_number = 1  # the header is row 1, so numbers match what a spreadsheet shows

            for chunk in chunks:

                chunk.columns = [str(c).strip().lower() for c in chunk.columns]
                parsed, errors = [], []

                for row in chunk.to_dict("records"):
                    
                    row_number += 1
                    values, error = parse_row(row, row_number, categories, user_id)

                    if error:
                        errors.append({"row": row_number, "error": error})
                    else:
                        parsed.append((row_number, values))

                # Skip rows whose key this user already has: they were imported before.
                good = [values for _, values in parsed]

                inserted = await insert_expenses(db, good)
                
                inserted_keys = {key for key, _, _ in inserted}
                duplicates = [
                    {"row": n, "error": "duplicate expense: this row was already imported"}
                    for n, values in parsed if values["import_key"] not in inserted_keys
                ]

                months = {(category_id, spent_on.replace(day=1)) for _, category_id, spent_on in inserted}
                events = [add_budget_check(db, user_id, category_id, month) for category_id, month in months]

                job.rows_done += len(chunk)
                job.rows_imported += len(inserted)
                job.rows_duplicate += len(duplicates)
                job.rows_failed += len(errors)
                errors = sorted(errors + duplicates, key=lambda e: e["row"])
                job.errors = (job.errors + errors)[:MAX_STORED_ERRORS]
                await db.commit()  # this batch is saved; earlier batches stay saved even if a later one fails

                for event in events:
                    await process_event(event.id)

            job.status = "completed"

        except Exception as exc:
            if not isinstance(exc, (CsvStructureError, pd.errors.ParserError)):
                log.exception("import %s failed", job_id)
                
            await db.rollback()  # drop the half-done batch
            await db.refresh(job)  # rollback expires `job`; async code can't lazy-load, so reload it
            
            job.status = "failed"
            job.errors = job.errors + [{"row": None, "error": str(exc)[:500]}]

        finally:
            os.remove(path)


        job.finished_at = datetime.now(UTC)
        await db.commit()
