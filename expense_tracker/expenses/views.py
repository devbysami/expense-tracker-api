from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Header, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from categories.utils import get_owned_category
from core.dependencies import CurrentUser, DBSession
from core.utils import error_response
from expenses.models import Expense, IdempotencyKey
from expenses.schemas import ExpenseFilters, ExpenseIn, ExpensePage, ExpenseOut, ExpenseUpdate
from expenses.utils import (
    decode_cursor, encode_cursor, get_owned_expense, make_etag, parse_if_match, request_hash,
)
from outbox.utils import add_budget_check, add_receipt_delete

IDEMPOTENCY_TTL = timedelta(hours=24)


async def create_expense(
    payload: ExpenseIn,
    user: CurrentUser,
    db: DBSession,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,  # "Idempotency-Key"
) -> Expense | JSONResponse:
    user_id = user.id  # read once: a rollback below would expire `user`
    body_hash = request_hash(payload.model_dump(mode="json"))

    if idempotency_key:
        # Keys live 24h: drop an expired one so the key can be used again.
        await db.execute(delete(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == idempotency_key,
            IdempotencyKey.created_at < datetime.now(UTC) - IDEMPOTENCY_TTL,
        ))
        # Claim the key. If another request holds it in an open transaction, Postgres makes this
        # INSERT WAIT until that transaction ends; then it conflicts and we replay its saved response.
        claimed = await db.scalar(
            pg_insert(IdempotencyKey)
            .values(user_id=user_id, key=idempotency_key, request_hash=body_hash)
            .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_key")
            .returning(IdempotencyKey.id)
        )
        if claimed is None:
            saved = await db.scalar(select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.key == idempotency_key
            ))
            await db.commit()
            if saved.request_hash != body_hash:
                return error_response(
                    422, "Idempotency-Key was already used with a different body", code="idempotency_key_reused"
                )
            return JSONResponse(saved.response_body, status_code=saved.response_status)

    if await get_owned_category(db, user_id, payload.category_id) is None:
        await db.rollback()  # also releases the claimed key, so a corrected retry can use it
        return error_response(404, "Category not found")

    expense = Expense(user_id=user_id, **payload.model_dump())
    db.add(expense)
    await db.flush()  # INSERT now -> id, version and timestamps are known
    body = ExpenseOut.model_validate(expense).model_dump(mode="json")

    if idempotency_key:
        await db.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.id == claimed)
            .values(response_status=201, response_body=body)
        )
    add_budget_check(db, user_id, expense.category_id, expense.spent_on)
    await db.commit()  # expense + idempotency record + outbox event: all or nothing
    return JSONResponse(body, status_code=201)


async def list_expenses(
    filters: Annotated[ExpenseFilters, Query()], user: CurrentUser, db: DBSession
) -> ExpensePage | JSONResponse:
    stmt = select(Expense).where(Expense.user_id == user.id)
    if filters.category_id is not None:
        stmt = stmt.where(Expense.category_id == filters.category_id)
    if filters.date_from:
        stmt = stmt.where(Expense.spent_on >= filters.date_from)
    if filters.date_to:
        stmt = stmt.where(Expense.spent_on <= filters.date_to)
    if filters.min_amount is not None:
        stmt = stmt.where(Expense.amount >= filters.min_amount)
    if filters.max_amount is not None:
        stmt = stmt.where(Expense.amount <= filters.max_amount)
    if filters.currency:
        stmt = stmt.where(Expense.currency == filters.currency)
    if filters.q:
        escaped = filters.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Expense.description.ilike(f"%{escaped}%", escape="\\"))

    if filters.cursor:
        position = decode_cursor(filters.cursor)
        if position is None:
            return error_response(400, "Invalid cursor")
        # Keyset: "rows that sort after the last one I saw". Unlike OFFSET, rows inserted
        # meanwhile can't shift the pages, so nothing is skipped or repeated.
        stmt = stmt.where(tuple_(Expense.spent_on, Expense.id) < tuple_(*position))

    stmt = stmt.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(filters.limit + 1)
    rows = (await db.scalars(stmt)).all()

    items = rows[: filters.limit]
    next_cursor = encode_cursor(items[-1].spent_on, items[-1].id) if len(rows) > filters.limit else None
    return ExpensePage(items=[ExpenseOut.model_validate(e) for e in items], next_cursor=next_cursor)


async def get_expense(
    expense_id: int, response: Response, user: CurrentUser, db: DBSession
) -> Expense | JSONResponse:
    expense = await get_owned_expense(db, user.id, expense_id)
    if expense is None:
        return error_response(404, "Expense not found")
    response.headers["ETag"] = make_etag(expense.version)
    return expense


async def update_expense(
    expense_id: int,
    payload: ExpenseUpdate,
    response: Response,
    user: CurrentUser,
    db: DBSession,
    if_match: Annotated[str | None, Header()] = None,  # reads "If-Match"
) -> Expense | JSONResponse:
    if if_match is None:
        return error_response(428, "If-Match header is required")

    expected_version = parse_if_match(if_match)
    if expected_version is None:
        return error_response(400, "If-Match must be an ETag returned by GET /expenses/{id}")

    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if changes.get("category_id") is not None:
        if await get_owned_category(db, user.id, changes["category_id"]) is None:
            return error_response(404, "Category not found")

    # Version check + update in ONE statement: the WHERE on version makes it atomic,
    # so of two parallel PATCHes with the same ETag only one can match the row.
    stmt = (
        update(Expense)
        .where(Expense.id == expense_id, Expense.user_id == user.id, Expense.version == expected_version)
        .values(**changes, version=Expense.version + 1)
        .returning(Expense)
    )
    expense = await db.scalar(stmt)
    if expense is None:
        # Note: no db.rollback() here — rollback expires loaded objects (incl. `user`), and touching
        # an expired attribute in async code raises MissingGreenlet. The UPDATE changed nothing anyway.
        if await get_owned_expense(db, user.id, expense_id) is None:  # doesn't exist / isn't ours
            return error_response(404, "Expense not found")
        return error_response(412, "Expense was changed by someone else; GET it again", code="version_conflict")

    # A new amount/currency/category/date can push the (new) category-month over a budget level.
    if changes.keys() & {"amount", "currency", "category_id", "spent_on"}:
        add_budget_check(db, user.id, expense.category_id, expense.spent_on)
    await db.commit()
    response.headers["ETag"] = make_etag(expense.version)
    return expense


async def delete_expense(expense_id: int, user: CurrentUser, db: DBSession) -> JSONResponse | None:
    deleted = (await db.execute(
        delete(Expense)
        .where(Expense.id == expense_id, Expense.user_id == user.id)
        .returning(Expense.id, Expense.receipt_key)
    )).first()
    if deleted is None:  # nothing deleted: doesn't exist or isn't ours
        return error_response(404, "Expense not found")
    if deleted.receipt_key:
        # The S3 delete happens in the worker, from an outbox row committed WITH the delete:
        # if S3 is down the event stays pending and is retried, so the file is never forgotten.
        add_receipt_delete(db, deleted.receipt_key)
    await db.commit()
