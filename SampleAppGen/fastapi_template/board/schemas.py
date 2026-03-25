from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PostRequest(BaseModel):
    title: str = Field(..., max_length=200)
    content: str = Field(..., max_length=10000)
    author: str = Field(..., max_length=100)


class PostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    author: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
