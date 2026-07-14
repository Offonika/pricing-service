from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BitrixMatchingSessionRequest(BaseModel):
    access_token: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class BitrixMatchingUser(BaseModel):
    user_id: str
    name: str | None = None


class BitrixMatchingSessionResponse(BaseModel):
    session_token: str
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int
    user: BitrixMatchingUser
