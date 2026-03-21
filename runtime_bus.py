from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from shared.utils import now_iso


def redis_url() -> str:
    return os.getenv("SUPERVISOR_REDIS_URL", "redis://127.0.0.1:6379/0")


def create_redis() -> Redis:
    return Redis.from_url(redis_url(), decode_responses=True)


def create_async_redis() -> AsyncRedis:
    return AsyncRedis.from_url(redis_url(), decode_responses=True)


class RunEventStore:
    def __init__(self, redis_client: Redis | None = None):
        self.redis = redis_client or create_redis()

    def _job_key(self, run_id: str) -> str:
        return f"supervisor:run:{run_id}"

    def _events_key(self, run_id: str) -> str:
        return f"supervisor:run:{run_id}:events"

    def _channel(self, run_id: str) -> str:
        return f"supervisor:run:{run_id}:channel"

    def initialize_run(self, run_id: str, queued_at: str) -> None:
        self.redis.hset(
            self._job_key(run_id),
            mapping={
                "run_id": run_id,
                "status": "queued",
                "queued_at": queued_at,
                "started_at": "",
                "finished_at": "",
                "error": "",
                "result": "",
                "event_seq": 0,
            },
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        mapping = {key: self._serialize(value) for key, value in fields.items()}
        if mapping:
            self.redis.hset(self._job_key(run_id), mapping=mapping)

    def append_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        event_id = self.redis.hincrby(self._job_key(run_id), "event_seq", 1)
        payload = dict(event)
        payload["event_id"] = int(event_id)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.redis.rpush(self._events_key(run_id), encoded)
        self.redis.publish(self._channel(run_id), encoded)
        return payload

    def set_result(self, run_id: str, result: dict[str, Any], status: str) -> None:
        self.update_run(
            run_id,
            status=status,
            finished_at=now_iso(),
            result=json.dumps(result, ensure_ascii=False),
        )

    def set_error(self, run_id: str, error: str) -> None:
        self.update_run(run_id, status="failed", finished_at=now_iso(), error=error)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        data = self.redis.hgetall(self._job_key(run_id))
        if not data:
            return None
        result = dict(data)
        if result.get("result"):
            try:
                result["result"] = json.loads(result["result"])
            except json.JSONDecodeError:
                result["result"] = None
        else:
            result["result"] = None
        result["events"] = self.get_events(run_id)
        return result

    def get_events(self, run_id: str, after_event_id: int = 0) -> list[dict[str, Any]]:
        items = self.redis.lrange(self._events_key(run_id), after_event_id, -1)
        events: list[dict[str, Any]] = []
        for item in items:
            try:
                event = json.loads(item)
            except json.JSONDecodeError:
                continue
            if int(event.get("event_id", 0)) > after_event_id:
                events.append(event)
        return events

    def _serialize(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)


class AsyncRunEventStore:
    def __init__(self, redis_client: AsyncRedis | None = None):
        self.redis = redis_client or create_async_redis()

    def _job_key(self, run_id: str) -> str:
        return f"supervisor:run:{run_id}"

    def _channel(self, run_id: str) -> str:
        return f"supervisor:run:{run_id}:channel"

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        data = await self.redis.hgetall(self._job_key(run_id))
        if not data:
            return None
        return data

    async def replay_events(self, run_id: str, after_event_id: int = 0) -> list[dict[str, Any]]:
        items = await self.redis.lrange(f"supervisor:run:{run_id}:events", after_event_id, -1)
        events: list[dict[str, Any]] = []
        for item in items:
            try:
                event = json.loads(item)
            except json.JSONDecodeError:
                continue
            if int(event.get("event_id", 0)) > after_event_id:
                events.append(event)
        return events

    @asynccontextmanager
    async def subscribe(self, run_id: str):
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self._channel(run_id))
        try:
            yield pubsub
        finally:
            await pubsub.unsubscribe(self._channel(run_id))
            await pubsub.close()

    async def stream_events(self, run_id: str, after_event_id: int = 0) -> AsyncIterator[dict[str, Any]]:
        last_event_id = after_event_id
        replay = await self.replay_events(run_id, after_event_id=after_event_id)
        for event in replay:
            last_event_id = max(last_event_id, int(event.get("event_id", 0)))
            yield event

        async with self.subscribe(run_id) as pubsub:
            while True:
                run = await self.get_run(run_id)
                if run is None:
                    return

                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and isinstance(message.get("data"), str):
                    try:
                        event = json.loads(message["data"])
                    except json.JSONDecodeError:
                        event = None
                    if event is not None:
                        event_id = int(event.get("event_id", 0))
                        if event_id > last_event_id:
                            last_event_id = event_id
                            yield event

                status = run.get("status", "")
                if status in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.1)

