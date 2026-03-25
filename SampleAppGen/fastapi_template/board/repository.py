from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock

from .models import Post


class InMemoryPostRepository:
    def __init__(self) -> None:
        self._store: dict[int, Post] = {}
        self._sequence: int = 0
        self._lock = Lock()

    def find_all(self) -> list[Post]:
        with self._lock:
            posts = [Post.copy(p) for p in self._store.values()]
        return sorted(posts, key=lambda p: p.id)

    def find_by_id(self, post_id: int) -> Post | None:
        with self._lock:
            post = self._store.get(post_id)
        return Post.copy(post) if post else None

    def create(self, post: Post) -> Post:
        with self._lock:
            self._sequence += 1
            now = datetime.now(timezone.utc)
            stored = Post.copy(post)
            stored.id = self._sequence
            stored.created_at = now
            stored.updated_at = now
            self._store[stored.id] = stored
            return Post.copy(stored)

    def update(self, post_id: int, post: Post) -> Post | None:
        with self._lock:
            existing = self._store.get(post_id)
            if existing is None:
                return None
            updated = Post.copy(post)
            updated.id = post_id
            updated.created_at = existing.created_at
            updated.updated_at = datetime.now(timezone.utc)
            self._store[post_id] = updated
            return Post.copy(updated)

    def delete(self, post_id: int) -> bool:
        with self._lock:
            return self._store.pop(post_id, None) is not None
