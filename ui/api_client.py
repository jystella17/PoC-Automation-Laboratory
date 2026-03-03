from __future__ import annotations

import json

import requests

DEFAULT_API_URL = "http://127.0.0.1:8000"


def health_check(api_url: str) -> dict[str, object]:
    response = requests.get(f"{api_url}/health", timeout=5)
    response.raise_for_status()
    return response.json()


def fetch_plan(api_url: str, payload: dict[str, object]) -> tuple[dict[str, object] | None, str | None]:
    try:
        response = requests.post(f"{api_url}/v1/supervisor/plan", json=payload, timeout=30)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, str(exc)


def request_chat_reply(
    api_url: str,
    payload: dict[str, object],
    messages: list[dict[str, str]],
) -> str:
    message = json.dumps(payload, indent=2)
    request_messages = [*messages, {"role": "user", "content": message}]
    response = requests.post(
        f"{api_url}/v1/chat",
        json={"messages": request_messages},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("reply", "Empty response")
