from fastapi import APIRouter

from reports import views
from reports.schemas import MonthlyReportOut

router = APIRouter(prefix="/reports", tags=["Reports"])

router.add_api_route("/monthly", views.monthly_report, methods=["GET"], response_model=MonthlyReportOut)
