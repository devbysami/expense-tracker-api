from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ImportStartedOut(BaseModel):
    job_id: str
    status: str


class ImportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    status: str
    rows_done: int
    rows_imported: int
    rows_duplicate: int
    rows_failed: int
    errors: list[dict]
    created_at: datetime
    finished_at: datetime | None
