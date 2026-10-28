from fastapi import APIRouter

from expenses.schemas import ExpenseOut
from receipts import views
from receipts.schemas import DownloadUrlOut, UploadUrlOut

router = APIRouter(prefix="/expenses/{expense_id}/receipt", tags=["Receipts"])

router.add_api_route("/upload-url", views.create_upload_url, methods=["POST"], response_model=UploadUrlOut)
router.add_api_route("/confirm", views.confirm_upload, methods=["POST"], response_model=ExpenseOut)
router.add_api_route("", views.get_download_url, methods=["GET"], response_model=DownloadUrlOut)
