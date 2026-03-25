from __future__ import annotations

from .models import Post
from .repository import InMemoryPostRepository
from .schemas import PostRequest, PostResponse


class PostService:
    def __init__(self, repository: InMemoryPostRepository) -> None:
        self.repository = repository

    def find_all(self) -> list[PostResponse]:
        return [PostResponse.model_validate(p) for p in self.repository.find_all()]

    def find_by_id(self, post_id: int) -> PostResponse | None:
        post = self.repository.find_by_id(post_id)
        return PostResponse.model_validate(post) if post else None

    def create(self, request: PostRequest) -> PostResponse:
        post = Post(
            id=None,
            title=request.title,
            content=request.content,
            author=request.author,
            created_at=None,
            updated_at=None,
        )
        return PostResponse.model_validate(self.repository.create(post))

    def update(self, post_id: int, request: PostRequest) -> PostResponse | None:
        existing = self.repository.find_by_id(post_id)
        if existing is None:
            return None
        post = Post(
            id=post_id,
            title=request.title,
            content=request.content,
            author=request.author,
            created_at=existing.created_at,
            updated_at=existing.updated_at,
        )
        updated = self.repository.update(post_id, post)
        return PostResponse.model_validate(updated) if updated else None

    def delete(self, post_id: int) -> bool:
        return self.repository.delete(post_id)
