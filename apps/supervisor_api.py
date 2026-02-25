from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException

from Supervisor import MissingInfoError, SupervisorAgent
from Supervisor.models import BuildPlan, ChatRequest, ChatResponse, SupervisorRunResult, UserRequest

app = FastAPI(title="Supervisor Agent API", version="0.1.0")
agent = SupervisorAgent()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/supervisor/plan", response_model=BuildPlan)
async def create_plan(request: UserRequest) -> BuildPlan:
    return agent.plan(request)


@app.post("/v1/supervisor/run", response_model=SupervisorRunResult)
async def run_supervisor(request: UserRequest) -> SupervisorRunResult:
    try:
        return agent.run(request)
    except MissingInfoError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Required fields are missing; unable to execute.",
                "missing_fields": exc.missing_fields,
            },
        ) from exc


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
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
        if plan.missing_info:
            return ChatResponse(reply="Parsed request. Missing required fields: " + ", ".join(plan.missing_info))
        return ChatResponse(reply="Parsed request. Execution plan: " + " | ".join(f"{s.name}:{s.detail}" for s in plan.steps))
    except Exception:
        return ChatResponse(
            reply=(
                "I can help as a Supervisor agent. "
                "Provide infra/app requirements in natural language or as UserRequest JSON."
            )
        )
