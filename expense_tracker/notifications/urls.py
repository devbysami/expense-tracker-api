from fastapi import APIRouter

from notifications import views
from notifications.schemas import NotificationOut

router = APIRouter(prefix="/notifications", tags=["Notifications"])

router.add_api_route("", views.list_notifications, methods=["GET"], response_model=list[NotificationOut])
