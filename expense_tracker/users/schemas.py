"""
Pydantic schemas ~ Django REST Framework serializers.
  - *In  schemas validate the request body
  - *Out schemas shape the response (FastAPI filters out any field not declared here,
    so password_hash can never leak)
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from core.types import Currency


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    base_currency: Currency


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    base_currency: str
    created_at: datetime


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
