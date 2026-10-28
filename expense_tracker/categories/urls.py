from fastapi import APIRouter

from categories import views
from categories.schemas import CategoryOut, CategoryWithSpendOut

router = APIRouter(prefix="/categories", tags=["Categories"])

router.add_api_route("", views.create_category, methods=["POST"], status_code=201, response_model=CategoryOut)
router.add_api_route("", views.list_categories, methods=["GET"], response_model=list[CategoryWithSpendOut])
