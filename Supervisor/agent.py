from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from InfraAutoSetting import InfraAutoSettingAgent
from SampleAppGen import SampleAppAgent
from agent_logging import get_agent_logger, log_event, timed_step
from eventing import emit_event, reset_event_callback, set_event_callback

from .config import SupervisorSettings, load_settings
from .llm import SupervisorLLM
from .models import (
    AgentExecution,
    BuildPlan,
    GraphEdge,
    GraphNode,
    GraphView,
    MissingRequirement,
    PlanStep,
    SupervisorRunResult,
    UserRequest,
)

logger = get_agent_logger("supervisor.agent", "supervisor.log")


class SupervisorState(TypedDict, total=False):
    mode: Literal["plan", "run"]
    request: UserRequest
    blocked: bool
    missing_requirements: list[MissingRequirement]
    summary: str
    environment_summary: dict[str, str]
    final_summary: str
    steps: Annotated[list[PlanStep], operator.add]
    executed: Annotated[list[AgentExecution], operator.add]
    generated_outputs: Annotated[list[str], operator.add]
    recommended_config: Annotated[list[str], operator.add]
    rollback_cleanup: Annotated[list[str], operator.add]
    execution_path: Annotated[list[str], operator.add]


@dataclass
class MissingInfoError(Exception):
    missing_fields: list[str]
    missing_requirements: list[MissingRequirement] = field(default_factory=list)

    def __str__(self) -> str:
        return "Missing required input: " + ", ".join(self.missing_fields)


GRAPH_NODES = [
    GraphNode(node_id="START", label="Start", kind="start"),
    GraphNode(node_id="plan", label="Plan Request", kind="task"),
    GraphNode(node_id="dispatch", label="Dispatch Agents", kind="gate"),
    GraphNode(node_id="build_infra", label="Build Infra", kind="task"),
    GraphNode(node_id="generate_app", label="Generate App", kind="task"),
    GraphNode(node_id="finalize", label="Finalize Result", kind="end"),
    GraphNode(node_id="END", label="End", kind="end"),
]

GRAPH_EDGES = [
    GraphEdge(source="START", target="plan"),
    GraphEdge(source="plan", target="dispatch", condition="mode=run and no missing requirements"),
    GraphEdge(source="plan", target="finalize", condition="mode=plan or missing requirements"),
    GraphEdge(source="dispatch", target="build_infra"),
    GraphEdge(source="build_infra", target="generate_app"),
    GraphEdge(source="generate_app", target="finalize"),
    GraphEdge(source="finalize", target="END"),
]

GRAPH_MERMAID = "\n".join(
    [
        "graph TD",
        "    START([Start]) --> plan[Plan Request]",
        "    plan -->|mode=run and complete| dispatch{Dispatch Agents}",
        "    plan -->|mode=plan or blocked| finalize[Finalize Result]",
        "    dispatch --> build_infra[Build Infra]",
        "    build_infra --> generate_app[Generate App]",
        "    generate_app --> finalize",
        "    finalize --> END([End])",
    ]
)


class SupervisorAgent:
    def __init__(self, settings: SupervisorSettings | None = None):
        self.settings = settings or load_settings()
        self.llm = SupervisorLLM(self.settings.azure_openai)
        self.infra_agent = InfraAutoSettingAgent(settings=self.settings.azure_openai)
        self.sample_app_agent = SampleAppAgent(settings=self.settings.azure_openai)
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(SupervisorState)
        workflow.add_node("plan", self._plan_node)
        workflow.add_node("dispatch", self._dispatch_node)
        workflow.add_node("build_infra", self._build_infra_node)
        workflow.add_node("generate_app", self._generate_app_node)
        workflow.add_node("finalize", self._finalize_node)

        workflow.add_edge(START, "plan")
        workflow.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {
                "dispatch": "dispatch",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("dispatch", "build_infra")
        workflow.add_edge("build_infra", "generate_app")
        workflow.add_edge("generate_app", "finalize")
        workflow.add_edge("finalize", END)
        return workflow.compile()

    def graph_view(self) -> GraphView:
        return GraphView(nodes=GRAPH_NODES, edges=GRAPH_EDGES, mermaid=GRAPH_MERMAID)

    def _emit_event(
        self,
        *,
        owner: Literal["supervisor", "infra_build", "sample_app"],
        phase: str,
        status: Literal["started", "planned", "progress", "completed", "failed"],
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        emit_event(owner=owner, phase=phase, status=status, message=message, details=details)

    def _missing_info(self, req: UserRequest) -> list[MissingRequirement]:
        missing: list[MissingRequirement] = []
        app_languages = [language.strip().lower() for language in req.app_tech_stack.language]
        has_infra_request = bool(req.infra_tech_stack.components)
        has_app_request = req.app_tech_stack.framework.strip().lower() not in {"", "none"} and req.topology.apps > 0

        if not req.targets:
            missing.append(
                MissingRequirement(
                    field="targets",
                    question="Which test server should be used for this run?",
                    reason="A target host and SSH access path are required before execution.",
                )
            )
        else:
            for idx, target in enumerate(req.targets):
                prefix = f"targets[{idx}]"
                if not target.host.strip():
                    missing.append(
                        MissingRequirement(
                            field=f"{prefix}.host",
                            question="What is the target host IP or hostname?",
                            reason="The supervisor cannot build or deploy without a destination host.",
                        )
                    )
                if not target.user.strip():
                    missing.append(
                        MissingRequirement(
                            field=f"{prefix}.user",
                            question="Which SSH user should be used on the target host?",
                            reason="Remote execution requires an explicit login account.",
                        )
                    )
                if not target.auth_ref.strip():
                    missing.append(
                        MissingRequirement(
                            field=f"{prefix}.auth_ref",
                            question="What secret or key reference should be used for SSH authentication?",
                            reason="The target host exists, but no SSH credential reference was provided.",
                        )
                    )

        if not has_infra_request and not has_app_request:
            missing.append(
                MissingRequirement(
                    field="infra_tech_stack.components",
                    question="Which infra components should be installed, or which application framework should be generated?",
                    reason="At least one infra component or an application framework is required before execution.",
                )
            )

        versions = req.infra_tech_stack.versions
        if has_infra_request and not versions:
            missing.append(
                MissingRequirement(
                    field="infra_tech_stack.versions",
                    question="Which component versions should be applied?",
                    reason="Version selection is required to generate deterministic install steps.",
                )
            )
        elif has_infra_request:
            if "tomcat" in req.infra_tech_stack.components and versions.get("tomcat", "").strip() in {"", "none"}:
                missing.append(
                    MissingRequirement(
                        field="infra_tech_stack.versions.tomcat",
                        question="Which Tomcat version should be installed?",
                        reason="Tomcat is selected as a component, but its version is missing.",
                    )
                )
            if "kafka" in req.infra_tech_stack.components and versions.get("kafka", "").strip() in {"", "none"}:
                missing.append(
                    MissingRequirement(
                        field="infra_tech_stack.versions.kafka",
                        question="Which Kafka version should be installed?",
                        reason="Kafka is selected as a component, but its version is missing.",
                    )
                )
            requires_java = (
                    any(component in {"tomcat", "kafka"} for component in req.infra_tech_stack.components)
                    or any(language.startswith("java") for language in app_languages)
            )
            if requires_java and versions.get("java", "").strip() == "":
                missing.append(
                    MissingRequirement(
                        field="infra_tech_stack.versions.java",
                        question="Which Java version should be used for the infra and app stack?",
                        reason="Java is required for Tomcat, Kafka, and most sample app flows.",
                    )
                )

        if not req.logging.base_dir.strip():
            missing.append(
                MissingRequirement(
                    field="logging.base_dir",
                    question="Where should the base log directory be created?",
                    reason="The AGENT.md gate requires a log directory policy before execution.",
                )
            )

        return missing

    def _build_plan_steps(self, blocked: bool) -> list[PlanStep]:
        blocked_status = "failed" if blocked else "pending"
        blocked_detail = (
            "필수 정보가 부족해 인프라 설치 및 환경 구성 작업을 시작할 수 없습니다."
            if blocked
            else "대상 서버에 필요한 인프라 구성요소를 설치하고 실행 환경을 준비할 예정입니다."
        )
        app_detail = (
            "필수 정보가 부족해 샘플 애플리케이션 생성 및 배포 준비 작업을 시작할 수 없습니다."
            if blocked
            else "샘플 애플리케이션을 생성하고 배포 가능한 산출물을 준비할 예정입니다."
        )
        return [
            PlanStep(
                name="plan",
                owner="supervisor",
                status="completed",
                detail="입력된 요구사항과 대상 환경 정보를 검토하고, 실행 가능 여부를 먼저 판단합니다.",
            ),
            PlanStep(
                name="build_infra",
                owner="infra_build",
                status=blocked_status,
                detail=blocked_detail,
            ),
            PlanStep(
                name="generate_app",
                owner="sample_app",
                status=blocked_status,
                detail=app_detail,
            ),
        ]

    def _build_plan_steps_for_request(self, req: UserRequest, blocked: bool) -> list[PlanStep]:
        steps = self._build_plan_steps(blocked)
        has_infra_request = bool(req.infra_tech_stack.components)
        has_app_request = req.app_tech_stack.framework.strip().lower() not in {"", "none"} and req.topology.apps > 0

        updated_steps: list[PlanStep] = []
        for step in steps:
            if step.name == "build_infra" and not has_infra_request and not blocked:
                updated_steps.append(PlanStep(name=step.name, owner=step.owner, status="completed", detail="선택된 인프라 구성요소가 없어 인프라 설치 단계는 건너뜁니다."))
                continue
            if step.name == "generate_app" and not has_app_request and not blocked:
                updated_steps.append(PlanStep(name=step.name, owner=step.owner, status="completed", detail="선택된 애플리케이션 프레임워크가 없어 애플리케이션 생성 단계는 건너뜁니다."))
                continue
            updated_steps.append(step)
        return updated_steps

    def _environment_summary(self, req: UserRequest) -> dict[str, str]:
        return {
            "os": req.infra_tech_stack.os,
            "components": ", ".join(req.infra_tech_stack.components) or "none",
            "targets": ", ".join(target.host for target in req.targets) or "none",
            "framework": req.app_tech_stack.framework,
            "additional_request": req.additional_request or "none",
        }

    def _initial_state(self, req: UserRequest, mode: Literal["plan", "run"]) -> SupervisorState:
        return {
            "mode": mode,
            "request": req,
            "blocked": False,
            "missing_requirements": [],
            "summary": "",
            "environment_summary": {},
            "final_summary": "",
            "steps": [],
            "executed": [],
            "generated_outputs": [],
            "recommended_config": [],
            "rollback_cleanup": [],
            "execution_path": [],
        }

    def _invoke(self, req: UserRequest, mode: Literal["plan", "run"]) -> SupervisorState:
        return self.graph.invoke(self._initial_state(req, mode=mode))

    def _build_plan_from_state(self, state: SupervisorState) -> BuildPlan:
        missing_requirements = state.get("missing_requirements", [])
        return BuildPlan(
            summary=state.get("summary", ""),
            missing_info=[item.field for item in missing_requirements],
            missing_requirements=missing_requirements,
            steps=state.get("steps", []),
            graph=self.graph_view(),
        )

    def _build_run_result_from_state(self, state: SupervisorState) -> SupervisorRunResult:
        return SupervisorRunResult(
            environment_summary=state.get("environment_summary", {}),
            executed=state.get("executed", []),
            generated_outputs=state.get("generated_outputs", []),
            recommended_config=state.get("recommended_config", []),
            rollback_cleanup=state.get("rollback_cleanup", []),
            graph=self.graph_view(),
            execution_path=state.get("execution_path", []),
            final_summary=state.get("final_summary", ""),
        )

    def _plan_node(self, state: SupervisorState) -> SupervisorState:
        request = state["request"]
        with timed_step(logger, "supervisor.plan_node", mode=state.get("mode", "unknown")):
            self._emit_event(
                owner="supervisor",
                phase="plan",
                status="started",
                message="입력값 검증 및 실행 계획을 생성합니다.",
            )
            missing_requirements = self._missing_info(request)
            blocked = bool(missing_requirements)
            summary = self.llm.summarize_plan(request, missing_requirements)
            if blocked:
                self._emit_event(
                    owner="supervisor",
                    phase="plan",
                    status="failed",
                    message="필수 입력값이 부족해 실행이 차단되었습니다.",
                    details={"missing_fields": [item.field for item in missing_requirements]},
                )
            else:
                self._emit_event(
                    owner="supervisor",
                    phase="plan",
                    status="completed",
                    message="입력값 검증을 통과했습니다. 하위 Agent 실행을 시작합니다.",
                )
            return {
                "blocked": blocked,
                "missing_requirements": missing_requirements,
                "summary": summary,
                "environment_summary": self._environment_summary(request),
                "steps": self._build_plan_steps_for_request(request, blocked),
                "execution_path": ["plan"],
            }

    def _route_after_plan(self, state: SupervisorState) -> str:
        if state["mode"] == "plan" or state["blocked"]:
            return "finalize"
        return "dispatch"

    def _dispatch_node(self, _state: SupervisorState) -> SupervisorState:
        with timed_step(logger, "supervisor.dispatch_node"):
            request = _state["request"]
            dispatch_plan = []
            if request.infra_tech_stack.components:
                dispatch_plan.append("build_infra")
            if request.app_tech_stack.framework.strip().lower() not in {"", "none"} and request.topology.apps > 0:
                dispatch_plan.append("generate_app")
            log_event(
                logger,
                "supervisor.dispatch_node.plan",
                dispatch_targets=dispatch_plan,
                components=request.infra_tech_stack.components,
                framework=request.app_tech_stack.framework,
            )
            self._emit_event(
                owner="supervisor",
                phase="dispatch",
                status="planned",
                message="하위 Agent 호출 순서를 확정했습니다.",
                details={"dispatch_targets": dispatch_plan},
            )
            return {"execution_path": ["dispatch"]}

    def _build_infra_node(self, state: SupervisorState) -> SupervisorState:
        request = state["request"]
        with timed_step(logger, "supervisor.build_infra_node", components=request.infra_tech_stack.components):
            if not request.infra_tech_stack.components:
                self._emit_event(
                    owner="infra_build",
                    phase="run",
                    status="completed",
                    message="선택된 인프라 구성요소가 없어 Infra Agent 실행을 건너뜁니다.",
                    details={"components": []},
                )
                return {
                    "execution_path": ["build_infra"],
                }
            self._emit_event(
                owner="infra_build",
                phase="run",
                status="started",
                message="Infra Agent를 호출합니다.",
                details={
                    "components": request.infra_tech_stack.components,
                    "target_hosts": [target.host for target in request.targets],
                },
            )
            self._emit_event(
                owner="infra_build",
                phase="run",
                status="planned",
                message="인프라 설치 스크립트 생성/검증 후 원격 적용을 수행할 예정입니다.",
            )
            result = self.infra_agent.run(request, prior_executions=state.get("executed", []))
            execution = result.execution
            log_event(
                logger,
                "supervisor.build_infra_node.result",
                success=execution.success,
                executed_commands=execution.executed_commands,
                notes=execution.notes,
                generated_outputs=result.generated_outputs,
            )
            self._emit_event(
                owner="infra_build",
                phase="run",
                status="completed" if execution.success else "failed",
                message="Infra Agent 작업이 완료되었습니다." if execution.success else "Infra Agent 작업이 실패했습니다.",
                details={
                    "executed_commands": execution.executed_commands,
                    "notes": execution.notes,
                    "generated_outputs": result.generated_outputs,
                },
            )
            return {
                "executed": [execution],
                "generated_outputs": result.generated_outputs,
                "recommended_config": result.recommended_config,
                "rollback_cleanup": result.rollback_cleanup,
                "execution_path": ["build_infra"],
            }

    def _generate_app_node(self, state: SupervisorState) -> SupervisorState:
        request = state["request"]
        with timed_step(logger, "supervisor.generate_app_node", framework=request.app_tech_stack.framework):
            self._emit_event(
                owner="sample_app",
                phase="run",
                status="started",
                message="Application Agent를 호출합니다.",
                details={
                    "framework": request.app_tech_stack.framework,
                    "language": request.app_tech_stack.language,
                },
            )
            self._emit_event(
                owner="sample_app",
                phase="run",
                status="planned",
                message="애플리케이션 스펙/소스 생성, 정적 검증, 아티팩트 패키징을 수행할 예정입니다.",
            )
            result = self.sample_app_agent.run(request, prior_executions=state.get("executed", []))
            log_event(
                logger,
                "supervisor.generate_app_node.result",
                success=result.execution.success,
                executed_commands=result.execution.executed_commands,
                notes=result.execution.notes,
                generated_outputs=result.generated_outputs,
            )
            self._emit_event(
                owner="sample_app",
                phase="run",
                status="completed" if result.execution.success else "failed",
                message="Application Agent 작업이 완료되었습니다." if result.execution.success else "Application Agent 작업이 실패했습니다.",
                details={
                    "executed_commands": result.execution.executed_commands,
                    "notes": result.execution.notes,
                    "generated_outputs": result.generated_outputs,
                },
            )
            return {
                "executed": [result.execution],
                "generated_outputs": result.generated_outputs,
                "recommended_config": result.recommended_config,
                "rollback_cleanup": result.rollback_cleanup,
                "execution_path": ["generate_app"],
            }

    def _finalize_node(self, state: SupervisorState) -> SupervisorState:
        with timed_step(logger, "supervisor.finalize_node", mode=state.get("mode", "unknown")):
            self._emit_event(owner="supervisor", phase="finalize", status="started", message="최종 응답을 정리합니다.")
            if state["blocked"]:
                final_summary = "필수 입력값이 아직 부족해 계획 검토 단계에서 작업이 보류되었습니다."
            elif state["mode"] == "plan":
                final_summary = "실행 전 검토가 완료되었으며, 필요한 작업 순서를 기준으로 바로 진행할 수 있습니다."
            else:
                failed_agents = [execution.agent for execution in state.get("executed", []) if not execution.success]
                if failed_agents:
                    final_summary = (
                        "일부 하위 작업이 실패했습니다. "
                        f"실패한 작업: {', '.join(failed_agents)}."
                    )
                else:
                    final_summary = "인프라 준비와 애플리케이션 생성 작업까지 전체 실행 흐름을 완료했습니다."
            self._emit_event(owner="supervisor", phase="finalize", status="completed", message="최종 응답 정리가 완료되었습니다.", details={"final_summary": final_summary})
            return {
                "final_summary": final_summary,
                "execution_path": ["finalize"],
            }

    def plan(self, req: UserRequest) -> BuildPlan:
        with timed_step(logger, "supervisor.plan", framework=req.app_tech_stack.framework):
            state = self._invoke(req, mode="plan")
            result = self._build_plan_from_state(state)
            log_event(
                logger,
                "supervisor.plan.result",
                blocked=bool(result.missing_requirements),
                missing_fields=result.missing_info,
            )
            return result

    def chat_reply(self, req: UserRequest) -> tuple[str, BuildPlan, SupervisorRunResult | None]:
        with timed_step(logger, "supervisor.chat_reply", framework=req.app_tech_stack.framework):
            run_result: SupervisorRunResult | None = None
            if self._missing_info(req):
                state = self._invoke(req, mode="plan")
                plan = self._build_plan_from_state(state)
            else:
                state = self._invoke(req, mode="run")
                plan = self._build_plan_from_state(state)
                run_result = self._build_run_result_from_state(state)
            reply = self.llm.generate_supervisor_reply(req, plan, run_result)
            log_event(
                logger,
                "supervisor.chat_reply.result",
                has_run_result=run_result is not None,
                missing_requirements=len(plan.missing_requirements),
            )
            return reply, plan, run_result

    def run(self, req: UserRequest, event_callback=None) -> SupervisorRunResult:
        token = set_event_callback(event_callback) if event_callback else None
        try:
            with timed_step(logger, "supervisor.run", framework=req.app_tech_stack.framework):
                self._emit_event(
                    owner="supervisor",
                    phase="run",
                    status="started",
                    message="Supervisor 실행을 시작합니다.",
                )
                state = self._invoke(req, mode="run")
                missing_requirements = state.get("missing_requirements", [])
                if missing_requirements:
                    self._emit_event(
                        owner="supervisor",
                        phase="run",
                        status="failed",
                        message="필수 입력값이 부족하여 실행을 중단합니다.",
                        details={"missing_fields": [item.field for item in missing_requirements]},
                    )
                    raise MissingInfoError(
                        missing_fields=[item.field for item in missing_requirements],
                        missing_requirements=missing_requirements,
                    )

                result = self._build_run_result_from_state(state)
                self._emit_event(
                    owner="supervisor",
                    phase="run",
                    status="completed",
                    message="Supervisor 실행이 완료되었습니다.",
                    details={"final_summary": result.final_summary},
                )
                log_event(
                    logger,
                    "supervisor.run.result",
                    final_summary=result.final_summary,
                    execution_path=result.execution_path,
                    generated_outputs=result.generated_outputs,
                )
                return result
        finally:
            if token is not None:
                reset_event_callback(token)
