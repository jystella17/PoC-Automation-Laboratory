from __future__ import annotations

import json
from collections.abc import Iterator

import requests

DEFAULT_API_URL = "http://127.0.0.1:8000"


def health_check(api_url: str) -> dict[str, object]:
    response = requests.get(f"{api_url}/health", timeout=5)
    response.raise_for_status()
    return response.json()


def start_supervisor_run(api_url: str, payload: dict[str, object]) -> dict[str, object]:
    response = requests.post(
        f"{api_url}/v1/supervisor/run-async",
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def get_supervisor_run_status(api_url: str, run_id: str) -> dict[str, object]:
    response = requests.get(
        f"{api_url}/v1/supervisor/run-async/{run_id}",
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def stream_supervisor_run_events(api_url: str, run_id: str, last_event_id: int = 0) -> Iterator[tuple[str, dict[str, object]]]:
    with requests.get(
        f"{api_url}/v1/supervisor/run-async/{run_id}/events",
        params={"last_event_id": last_event_id},
        stream=True,
        timeout=600,
    ) as response:
        response.raise_for_status()
        event_name = "message"
        data_lines: list[str] = []

        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            line = raw_line.strip()
            if not line:
                if data_lines:
                    payload = json.loads("\n".join(data_lines))
                    yield event_name, payload
                event_name = "message"
                data_lines = []
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
