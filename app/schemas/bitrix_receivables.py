from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BitrixReceivablesSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class BitrixReceivablesUser(BaseModel):
    user_id: str
    name: str | None = None


class BitrixReceivablesSessionResponse(BaseModel):
    session_token: str
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int
    user: BitrixReceivablesUser
    access_level: Literal["full", "department"]
    department_refs: list[str]
