from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent_logging import get_agent_logger, log_event, timed_step
from Supervisor import MissingInfoError, SupervisorAgent
from Supervisor.models import BuildPlan, ChatRequest, ChatResponse, GraphView, SupervisorRunResult, UserRequest

app = FastAPI(title="Supervisor Agent API", version="0.1.0")
agent = SupervisorAgent()
logger = get_agent_logger("supervisor.api", "supervisor_api.log")


class RunAsyncStartResponse(BaseModel):
    run_id: str
    status: str
    queued_at: str


class RunAsyncStatusResponse(BaseModel):
    run_id: str
    status: str
    queued_at: str
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    result: SupervisorRunResult | None = None
    events: list[dict[str, object]] = Field(default_factory=list)


_run_executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("SUPERVISOR_RUN_WORKERS", "2"))))
_run_jobs: dict[str, dict[str, object]] = {}
_run_jobs_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_run_job(run_id: str, request: UserRequest) -> None:
    def _emit(event: dict[str, object]) -> None:
        with _run_jobs_lock:
            job = _run_jobs.get(run_id)
            if not job:
                return
            events = job.get("events")
            if not isinstance(events, list):
                events = []
                job["events"] = events
            events.append(event)
        print(f"[run:{run_id}] {event['timestamp']} | {event['owner']} | {event['phase']} | {event['status']} | {event['message']}")

    with _run_jobs_lock:
        job = _run_jobs.get(run_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = _now_iso()

    try:
        result = agent.run(request, event_callback=_emit)
    except Exception as exc:  # pragma: no cover
        with _run_jobs_lock:
            job = _run_jobs.get(run_id)
            if not job:
                return
            job["status"] = "failed"
            job["error"] = str(exc)
            job["finished_at"] = _now_iso()
        log_event(logger, "supervisor_api.run_async.failed", run_id=run_id, error=str(exc))
        return

    with _run_jobs_lock:
        job = _run_jobs.get(run_id)
        if not job:
            return
        job["status"] = "succeeded"
        job["result"] = result
        job["finished_at"] = _now_iso()
    log_event(logger, "supervisor_api.run_async.succeeded", run_id=run_id, final_summary=result.final_summary)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/supervisor/plan", response_model=BuildPlan)
async def create_plan(request: UserRequest) -> BuildPlan:
    with timed_step(logger, "supervisor_api.create_plan", framework=request.app_tech_stack.framework):
        return agent.plan(request)


@app.get("/v1/supervisor/graph", response_model=GraphView)
async def get_supervisor_graph() -> GraphView:
    return agent.graph_view()


@app.post("/v1/supervisor/run", response_model=SupervisorRunResult)
async def run_supervisor(request: UserRequest) -> SupervisorRunResult:
    with timed_step(logger, "supervisor_api.run_supervisor", framework=request.app_tech_stack.framework):
        try:
            return agent.run(request)
        except MissingInfoError as exc:
            log_event(logger, "supervisor_api.run_supervisor.missing_info", missing_fields=exc.missing_fields)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Required fields are missing; unable to execute.",
                    "missing_fields": exc.missing_fields,
                    "missing_requirements": [item.model_dump() for item in exc.missing_requirements],
                },
            ) from exc


@app.post("/v1/supervisor/run-async", response_model=RunAsyncStartResponse)
async def run_supervisor_async(request: UserRequest) -> RunAsyncStartResponse:
    with timed_step(logger, "supervisor_api.run_async.enqueue", framework=request.app_tech_stack.framework):
        run_id = uuid4().hex
        queued_at = _now_iso()
        with _run_jobs_lock:
            _run_jobs[run_id] = {
                "run_id": run_id,
                "status": "queued",
                "queued_at": queued_at,
                "started_at": "",
                "finished_at": "",
                "error": "",
                "result": None,
                "events": [],
            }
        _run_executor.submit(_execute_run_job, run_id, request)
        log_event(logger, "supervisor_api.run_async.queued", run_id=run_id)
        return RunAsyncStartResponse(run_id=run_id, status="queued", queued_at=queued_at)


@app.get("/v1/supervisor/run-async/{run_id}", response_model=RunAsyncStatusResponse)
async def get_run_supervisor_async(run_id: str) -> RunAsyncStatusResponse:
    with _run_jobs_lock:
        job = _run_jobs.get(run_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"run_id not found: {run_id}")

        return RunAsyncStatusResponse(
            run_id=str(job["run_id"]),
            status=str(job["status"]),
            queued_at=str(job["queued_at"]),
            started_at=str(job.get("started_at", "")),
            finished_at=str(job.get("finished_at", "")),
            error=str(job.get("error", "")),
            result=job.get("result"),
            events=job.get("events", []),
        )


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    with timed_step(logger, "supervisor_api.chat", message_count=len(request.messages)):
        if not request.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty.")

        last_user = ""
        for msg in reversed(request.messages):
            if msg.role == "user":
                last_user = msg.content.strip()
                break

        if not last_user:
            return ChatResponse(reply="No user message found.")

        try:
            payload = json.loads(last_user)
            parsed = UserRequest.model_validate(payload)
            plan = agent.plan(parsed)
            reply = agent.llm.generate_supervisor_reply(parsed, plan, None)
            log_event(
                logger,
                "supervisor_api.chat.result",
                has_run_result=False,
                missing_info=plan.missing_info,
            )
            return ChatResponse(
                reply=reply,
                metadata={
                    "missing_info": plan.missing_info,
                    "mermaid": plan.graph.mermaid,
                    "steps": [step.model_dump() for step in plan.steps],
                    "final_summary": "",
                    "executed": [],
                    "generated_outputs": [],
                },
            )
        except Exception as exc:
            log_event(logger, "supervisor_api.chat.fallback", error=str(exc))
            return ChatResponse(
                reply=(
                    "I can help as a Supervisor agent. "
                    "Provide infra/app requirements in natural language or as UserRequest JSON."
                )
            )
