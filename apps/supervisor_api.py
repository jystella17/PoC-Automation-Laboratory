from __future__ import annotations

import json
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_logging import get_agent_logger, log_event, timed_step
from apps.supervisor_tasks import run_supervisor_task
from Supervisor import MissingInfoError, SupervisorAgent
from Supervisor.models import BuildPlan, ChatRequest, ChatResponse, GraphView, SupervisorRunResult, UserRequest
from runtime_bus import AsyncRunEventStore, RunEventStore
from shared.utils import now_iso

app = FastAPI(title="Supervisor Agent API", version="0.1.0")
logger = get_agent_logger("supervisor.api", "supervisor_api.log")
store = RunEventStore()
async_store = AsyncRunEventStore()

_agent: SupervisorAgent | None = None


def _get_agent() -> SupervisorAgent:
    global _agent
    if _agent is None:
        _agent = SupervisorAgent()
    return _agent


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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/supervisor/plan", response_model=BuildPlan)
async def create_plan(request: UserRequest) -> BuildPlan:
    with timed_step(logger, "supervisor_api.create_plan", framework=request.app_tech_stack.framework):
        return _get_agent().plan(request)


@app.get("/v1/supervisor/graph", response_model=GraphView)
async def get_supervisor_graph() -> GraphView:
    return _get_agent().graph_view()


@app.post("/v1/supervisor/run", response_model=SupervisorRunResult)
async def run_supervisor(request: UserRequest) -> SupervisorRunResult:
    with timed_step(logger, "supervisor_api.run_supervisor", framework=request.app_tech_stack.framework):
        try:
            return _get_agent().run(request)
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
        queued_at = now_iso()
        store.initialize_run(run_id, queued_at)
        store.append_event(
            run_id,
            {
                "timestamp": queued_at,
                "owner": "supervisor",
                "phase": "queue",
                "status": "started",
                "message": "비동기 실행 요청이 큐에 등록되었습니다.",
                "details": {"framework": request.app_tech_stack.framework},
            },
        )
        run_supervisor_task.delay(run_id, request.model_dump(mode="json"))
        log_event(logger, "supervisor_api.run_async.queued", run_id=run_id)
        return RunAsyncStartResponse(run_id=run_id, status="queued", queued_at=queued_at)


@app.get("/v1/supervisor/run-async/{run_id}", response_model=RunAsyncStatusResponse)
async def get_run_supervisor_async(run_id: str) -> RunAsyncStatusResponse:
    job = store.get_run(run_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"run_id not found: {run_id}")

    error_value = job.get("error", "")
    if isinstance(error_value, str):
        error_text = error_value
    else:
        error_text = json.dumps(error_value, ensure_ascii=False)

    return RunAsyncStatusResponse(
        run_id=str(job["run_id"]),
        status=str(job.get("status", "")),
        queued_at=str(job.get("queued_at", "")),
        started_at=str(job.get("started_at", "")),
        finished_at=str(job.get("finished_at", "")),
        error=error_text,
        result=job.get("result"),
        events=job.get("events", []),
    )


@app.get("/v1/supervisor/run-async/{run_id}/events")
async def stream_run_supervisor_events(run_id: str, last_event_id: int = 0) -> StreamingResponse:
    job = await async_store.get_run(run_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"run_id not found: {run_id}")

    async def _event_stream():
        async for event in async_store.stream_events(run_id, after_event_id=last_event_id):
            yield _format_sse("event", event)
        final_job = store.get_run(run_id)
        if final_job:
            yield _format_sse(
                "done",
                {
                    "run_id": run_id,
                    "status": final_job.get("status", ""),
                    "finished_at": final_job.get("finished_at", ""),
                },
            )

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
            agent = _get_agent()
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


def _format_sse(event_name: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {encoded}\n\n"
