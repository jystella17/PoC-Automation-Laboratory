from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from .exceptions import NotFoundError
from .repository import InMemoryPostRepository
from .schemas import PostRequest, PostResponse
from .service import PostService

router = APIRouter(prefix="/api/posts", tags=["posts"])


@lru_cache(maxsize=1)
def _get_repository() -> InMemoryPostRepository:
    return InMemoryPostRepository()


def get_service() -> PostService:
    return PostService(_get_repository())


ServiceDep = Annotated[PostService, Depends(get_service)]


@router.get("", response_model=list[PostResponse])
def list_all(service: ServiceDep) -> list[PostResponse]:
    return service.find_all()


@router.get("/{post_id}", response_model=PostResponse)
def get_by_id(post_id: int, service: ServiceDep) -> PostResponse:
    result = service.find_by_id(post_id)
    if result is None:
        raise NotFoundError(f"Post not found: {post_id}")
    return result


@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def create(request: PostRequest, service: ServiceDep) -> PostResponse:
    return service.create(request)


@router.put("/{post_id}", response_model=PostResponse)
def update(post_id: int, request: PostRequest, service: ServiceDep) -> PostResponse:
    result = service.update(post_id, request)
    if result is None:
        raise NotFoundError(f"Post not found: {post_id}")
    return result


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(post_id: int, service: ServiceDep) -> Response:
    if not service.delete(post_id):
        raise NotFoundError(f"Post not found: {post_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
