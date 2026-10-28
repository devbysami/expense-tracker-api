from fastapi import APIRouter

from imports import views
from imports.schemas import ImportJobOut, ImportStartedOut

router = APIRouter(tags=["Imports"])

router.add_api_route("/expenses/import", views.start_import, methods=["POST"], status_code=202,
                     response_model=ImportStartedOut)
router.add_api_route("/imports/{job_id}", views.get_import, methods=["GET"], response_model=ImportJobOut)
