import boto3
import httpx

from tests.conftest import create_category, create_expense, register_and_login
from worker import process_batch

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
PDF = b"%PDF-1.7\n" + b"0" * 100
EXE = b"MZ\x90\x00" + b"\x00" * 100  # Windows executable header


async def upload_receipt(client, headers, expense_id: int, content_type: str, data: bytes) -> str:
    """Asks the API for a pre-signed POST, then uploads straight to (fake) S3 like a mobile app would."""
    resp = await client.post(f"/expenses/{expense_id}/receipt/upload-url", json={"content_type": content_type},
                             headers=headers)
    assert resp.status_code == 200, resp.text
    presigned = resp.json()
    async with httpx.AsyncClient() as s3:
        upload = await s3.post(presigned["url"], data=presigned["fields"], files={"file": ("receipt", data)})
    assert upload.status_code in (200, 204), upload.text
    return presigned["key"]


def object_exists(storage, key: str) -> bool:
    s3 = boto3.client("s3", region_name="us-east-1", endpoint_url=storage.endpoint_url)
    return s3.list_objects_v2(Bucket=storage.bucket, Prefix=key).get("KeyCount", 0) > 0


async def test_receipt_upload_confirm_and_download(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    user_id = (await client.get("/auth/me", headers=auth_headers)).json()["id"]

    key = await upload_receipt(client, auth_headers, expense["id"], "image/png", PNG)
    assert key.startswith(f"receipts/{user_id}/{expense['id']}/") and key.endswith(".png")

    confirmed = await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": key}, headers=auth_headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["receipt_key"] == key
    assert confirmed.json()["version"] == 2  # ETag changes when a receipt is attached

    download = (await client.get(f"/expenses/{expense['id']}/receipt", headers=auth_headers)).json()
    assert download["expires_in"] == 900
    async with httpx.AsyncClient() as http:
        assert (await http.get(download["url"])).content == PNG


async def test_exe_renamed_to_png_is_rejected_and_deleted(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    key = await upload_receipt(client, auth_headers, expense["id"], "image/png", EXE)

    resp = await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": key}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "receipt_invalid_type"
    assert not object_exists(storage, key)
    assert (await client.get(f"/expenses/{expense['id']}", headers=auth_headers)).json()["receipt_key"] is None


async def test_pdf_content_under_png_name_is_rejected(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    key = await upload_receipt(client, auth_headers, expense["id"], "image/png", PDF)
    resp = await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": key}, headers=auth_headers)
    assert resp.status_code == 422


async def test_confirm_without_upload_is_422(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    user_id = (await client.get("/auth/me", headers=auth_headers)).json()["id"]
    key = f"receipts/{user_id}/{expense['id']}/never-uploaded.png"
    resp = await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": key}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "receipt_missing"


async def test_confirm_with_key_of_another_expense_is_422(client, auth_headers, storage):
    category_id = await create_category(client, auth_headers)
    first = await create_expense(client, auth_headers, category_id)
    second = await create_expense(client, auth_headers, category_id)
    key = await upload_receipt(client, auth_headers, first["id"], "application/pdf", PDF)
    resp = await client.post(f"/expenses/{second['id']}/receipt/confirm", json={"key": key}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_receipt_key"


async def test_only_jpg_png_pdf_upload_urls(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    resp = await client.post(f"/expenses/{expense['id']}/receipt/upload-url", json={"content_type": "application/x-msdownload"},
                             headers=auth_headers)
    assert resp.status_code == 422


async def test_receipt_endpoints_are_404_for_other_users(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    bob = await register_and_login(client, "bob@example.com")
    assert (await client.post(f"/expenses/{expense['id']}/receipt/upload-url", json={"content_type": "image/png"},
                              headers=bob)).status_code == 404
    assert (await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": "x"},
                              headers=bob)).status_code == 404
    assert (await client.get(f"/expenses/{expense['id']}/receipt", headers=bob)).status_code == 404


async def test_no_receipt_yet_is_404(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    assert (await client.get(f"/expenses/{expense['id']}/receipt", headers=auth_headers)).status_code == 404


async def test_deleting_expense_deletes_file_via_outbox_even_after_s3_failure(client, auth_headers, storage, monkeypatch):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    key = await upload_receipt(client, auth_headers, expense["id"], "image/png", PNG)
    await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": key}, headers=auth_headers)
    await process_batch(storage)  # clear the budget-check event

    assert (await client.delete(f"/expenses/{expense['id']}", headers=auth_headers)).status_code == 204

    # S3 is down the first time: the event stays pending...
    async def s3_down(key):
        raise ConnectionError("S3 unavailable")
    monkeypatch.setattr(storage, "delete", s3_down)
    await process_batch(storage)
    assert object_exists(storage, key)

    # ...and the next run cleans it up.
    monkeypatch.undo()
    await process_batch(storage)
    assert not object_exists(storage, key)


async def test_replacing_a_receipt_deletes_the_old_file(client, auth_headers, storage):
    expense = await create_expense(client, auth_headers, await create_category(client, auth_headers))
    old = await upload_receipt(client, auth_headers, expense["id"], "image/png", PNG)
    await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": old}, headers=auth_headers)
    new = await upload_receipt(client, auth_headers, expense["id"], "application/pdf", PDF)
    await client.post(f"/expenses/{expense['id']}/receipt/confirm", json={"key": new}, headers=auth_headers)
    await process_batch(storage)
    assert not object_exists(storage, old)
    assert object_exists(storage, new)
