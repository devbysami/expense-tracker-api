from typing import Literal

from pydantic import BaseModel

ReceiptContentType = Literal["image/jpeg", "image/png", "application/pdf"]


class UploadUrlIn(BaseModel):
    content_type: ReceiptContentType


class UploadUrlOut(BaseModel):
    url: str
    fields: dict[str, str]  # send these as form fields, then the file as "file" (multipart POST)
    key: str
    max_bytes: int
    expires_in: int


class ConfirmIn(BaseModel):
    key: str


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int
