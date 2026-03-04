from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _resolve_log_dir() -> Path:
    if os.getenv("AGENT_LOG_DIR"):
        return Path(os.getenv("AGENT_LOG_DIR", "")).expanduser().resolve()
    return Path("/tmp/poc-automation-laboratory/logs")


LOG_DIR = _resolve_log_dir()


def get_agent_logger(name: str, filename: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = logging.FileHandler(LOG_DIR / filename, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


@contextmanager
def timed_step(logger: logging.Logger, step: str, **fields) -> Iterator[None]:
    started_at = time.perf_counter()
    log_event(logger, f"{step}.start", **fields)
    try:
        yield
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        log_event(
            logger,
            f"{step}.error",
            duration_ms=duration_ms,
            error_type=type(exc).__name__,
            error=str(exc),
            **fields,
        )
        raise
    else:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        log_event(logger, f"{step}.end", duration_ms=duration_ms, **fields)
