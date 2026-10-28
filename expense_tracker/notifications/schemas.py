from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int
    month: date
    level: int
    message: str
    created_at: datetime
