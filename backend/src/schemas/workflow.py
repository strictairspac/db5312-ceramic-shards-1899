from typing import Any

from pydantic import BaseModel, Field, field_validator


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class HandoverSyncForm(BaseModel):
    crew: str = Field(min_length=1, max_length=40)
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator('crew')
    @classmethod
    def crew_not_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('班组不能为空')
        return value
