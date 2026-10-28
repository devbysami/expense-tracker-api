"""
Django-style URL table. `router.add_api_route(path, view, ...)` is exactly what the
`@router.post(...)` decorator does under the hood — this just keeps views and URLs apart.
"""
from fastapi import APIRouter

from users import views
from users.schemas import TokenPairOut, UserOut

router = APIRouter(prefix="/auth", tags=["Auth"])

router.add_api_route("/register", views.register, methods=["POST"], status_code=201, response_model=UserOut)
router.add_api_route("/login", views.login, methods=["POST"], response_model=TokenPairOut)
router.add_api_route("/refresh", views.refresh, methods=["POST"], response_model=TokenPairOut)
router.add_api_route("/logout", views.logout, methods=["POST"], status_code=204)
router.add_api_route("/me", views.me, methods=["GET"], response_model=UserOut)
