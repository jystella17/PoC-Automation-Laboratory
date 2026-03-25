from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Post:
    id: int | None
    title: str
    content: str
    author: str
    created_at: datetime | None
    updated_at: datetime | None

    @staticmethod
    def copy(source: Post) -> Post:
        return Post(
            id=source.id,
            title=source.title,
            content=source.content,
            author=source.author,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )
