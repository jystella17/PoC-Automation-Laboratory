import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from ..exceptions import NotFoundError, not_found_handler, unexpected_handler, validation_handler
from ..repository import InMemoryPostRepository
from ..router import get_service, router
from ..service import PostService


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(NotFoundError, not_found_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(Exception, unexpected_handler)
    fresh_service = PostService(InMemoryPostRepository())
    app.dependency_overrides[get_service] = lambda: fresh_service
    return TestClient(app)


def test_create_and_fetch_post(client):
    payload = {"title": "Hello", "content": "World", "author": "tester"}
    response = client.post("/api/posts", json=payload)
    assert response.status_code == 201
    assert response.json()["id"] == 1

    response = client.get("/api/posts/1")
    assert response.status_code == 200
    assert response.json()["title"] == "Hello"
