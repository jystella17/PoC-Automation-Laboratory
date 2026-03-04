from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException

from agent_logging import get_agent_logger, log_event, timed_step
from Supervisor import MissingInfoError, SupervisorAgent
from Supervisor.models import BuildPlan, ChatRequest, ChatResponse, GraphView, SupervisorRunResult, UserRequest

app = FastAPI(title="Supervisor Agent API", version="0.1.0")
agent = SupervisorAgent()
logger = get_agent_logger("supervisor.api", "supervisor_api.log")


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
            reply, plan, run_result = agent.chat_reply(parsed)
            log_event(
                logger,
                "supervisor_api.chat.result",
                has_run_result=run_result is not None,
                missing_info=plan.missing_info,
            )
            return ChatResponse(
                reply=reply,
                metadata={
                    "missing_info": plan.missing_info,
                    "mermaid": plan.graph.mermaid,
                    "steps": [step.model_dump() for step in plan.steps],
                    "final_summary": run_result.final_summary if run_result else "",
                    "executed": [execution.model_dump() for execution in run_result.executed] if run_result else [],
                    "generated_outputs": run_result.generated_outputs if run_result else [],
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
