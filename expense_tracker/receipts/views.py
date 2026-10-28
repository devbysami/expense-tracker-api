import uuid

from fastapi.responses import JSONResponse
from sqlalchemy import update

from core.dependencies import CurrentUser, DBSession
from core.storage import DOWNLOAD_URL_EXPIRES, MAX_RECEIPT_BYTES, UPLOAD_URL_EXPIRES, StorageDep
from core.utils import error_response
from expenses.models import Expense
from expenses.utils import get_owned_expense
from outbox.utils import add_receipt_delete
from receipts.schemas import ConfirmIn, DownloadUrlOut, UploadUrlIn, UploadUrlOut
from receipts.utils import EXTENSIONS, detect_type, key_prefix


async def create_upload_url(
    expense_id: int, payload: UploadUrlIn, user: CurrentUser, db: DBSession, storage: StorageDep
) -> UploadUrlOut | JSONResponse:
    if await get_owned_expense(db, user.id, expense_id) is None:
        return error_response(404, "Expense not found")
    key = f"{key_prefix(user.id, expense_id)}{uuid.uuid4().hex}.{EXTENSIONS[payload.content_type]}"
    post = await storage.presigned_upload(key, payload.content_type)
    return UploadUrlOut(url=post["url"], fields=post["fields"], key=key,
                        max_bytes=MAX_RECEIPT_BYTES, expires_in=UPLOAD_URL_EXPIRES)


async def confirm_upload(
    expense_id: int, payload: ConfirmIn, user: CurrentUser, db: DBSession, storage: StorageDep
) -> Expense | JSONResponse:
    user_id = user.id
    expense = await get_owned_expense(db, user_id, expense_id)
    if expense is None:
        return error_response(404, "Expense not found")

    key = payload.key
    if not key.startswith(key_prefix(user_id, expense_id)) or "/" in key.removeprefix(key_prefix(user_id, expense_id)):
        return error_response(422, "This key does not belong to this expense", code="invalid_receipt_key")
    claimed_ext = key.rsplit(".", 1)[-1]

    size = await storage.object_size(key)
    if size is None:
        return error_response(422, "File was not uploaded", code="receipt_missing")
    if size > MAX_RECEIPT_BYTES:
        await storage.delete(key)
        return error_response(422, "File is larger than 5 MB", code="receipt_too_large")

    real_ext = detect_type(await storage.first_bytes(key))
    if real_ext is None or real_ext != claimed_ext:
        await storage.delete(key)  # don't keep a renamed .exe around
        return error_response(422, "File content is not a valid JPG, PNG or PDF", code="receipt_invalid_type")

    old_key = expense.receipt_key
    updated = await db.scalar(
        update(Expense)
        .where(Expense.id == expense_id, Expense.user_id == user_id)
        .values(receipt_key=key, version=Expense.version + 1)  # new ETag: the resource changed
        .returning(Expense)
    )
    if old_key and old_key != key:
        add_receipt_delete(db, old_key)  # replaced receipt: clean up the old file via the worker
    await db.commit()
    return updated


async def get_download_url(
    expense_id: int, user: CurrentUser, db: DBSession, storage: StorageDep
) -> DownloadUrlOut | JSONResponse:
    expense = await get_owned_expense(db, user.id, expense_id)
    if expense is None or not expense.receipt_key:
        return error_response(404, "Receipt not found")
    return DownloadUrlOut(url=await storage.presigned_download(expense.receipt_key), expires_in=DOWNLOAD_URL_EXPIRES)
