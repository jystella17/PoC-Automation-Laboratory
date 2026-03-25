from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from .board.exceptions import NotFoundError, not_found_handler, unexpected_handler, validation_handler
from .board.router import router as board_router

LOG_DIR = os.getenv("APP_LOG_DIR", "/var/log/app")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup")
    yield
    logger.info("Application shutdown")


app = FastAPI(title="Sample App", lifespan=lifespan)

app.include_router(board_router)
app.add_exception_handler(NotFoundError, not_found_handler)
app.add_exception_handler(RequestValidationError, validation_handler)
app.add_exception_handler(Exception, unexpected_handler)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
