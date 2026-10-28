from fastapi import APIRouter

from expenses import views
from expenses.schemas import ExpenseOut, ExpensePage

router = APIRouter(prefix="/expenses", tags=["Expenses"])

router.add_api_route("", views.create_expense, methods=["POST"], status_code=201, response_model=ExpenseOut)
router.add_api_route("", views.list_expenses, methods=["GET"], response_model=ExpensePage)
router.add_api_route("/{expense_id}", views.get_expense, methods=["GET"], response_model=ExpenseOut)
router.add_api_route("/{expense_id}", views.update_expense, methods=["PATCH"], response_model=ExpenseOut)
router.add_api_route("/{expense_id}", views.delete_expense, methods=["DELETE"], status_code=204, response_model=None)
