from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

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


class SupervisorAgent:
    def __init__(self, settings: SupervisorSettings | None = None):
        self.settings = settings or load_settings()
        self.llm = SupervisorLLM(self.settings.azure_openai)
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
        workflow.add_edge("dispatch", "generate_app")
        workflow.add_edge("build_infra", "finalize")
        workflow.add_edge("generate_app", "finalize")
        workflow.add_edge("finalize", END)
        return workflow.compile()

    def graph_view(self) -> GraphView:
        nodes = [
            GraphNode(node_id="START", label="Start", kind="start"),
            GraphNode(node_id="plan", label="Plan Request", kind="task"),
            GraphNode(node_id="dispatch", label="Dispatch Agents", kind="gate"),
            GraphNode(node_id="build_infra", label="Build Infra", kind="task"),
            GraphNode(node_id="generate_app", label="Generate App", kind="task"),
            GraphNode(node_id="finalize", label="Finalize Result", kind="end"),
            GraphNode(node_id="END", label="End", kind="end"),
        ]
        edges = [
            GraphEdge(source="START", target="plan"),
            GraphEdge(source="plan", target="dispatch", condition="mode=run and no missing requirements"),
            GraphEdge(source="plan", target="finalize", condition="mode=plan or missing requirements"),
            GraphEdge(source="dispatch", target="build_infra"),
            GraphEdge(source="dispatch", target="generate_app"),
            GraphEdge(source="build_infra", target="finalize"),
            GraphEdge(source="generate_app", target="finalize"),
            GraphEdge(source="finalize", target="END"),
        ]
        mermaid = "\n".join(
            [
                "graph TD",
                "    START([Start]) --> plan[Plan Request]",
                "    plan -->|mode=run and complete| dispatch{Dispatch Agents}",
                "    plan -->|mode=plan or blocked| finalize[Finalize Result]",
                "    dispatch --> build_infra[Build Infra]",
                "    dispatch --> generate_app[Generate App]",
                "    build_infra --> finalize",
                "    generate_app --> finalize",
                "    finalize --> END([End])",
            ]
        )
        return GraphView(nodes=nodes, edges=edges, mermaid=mermaid)

    def _missing_info(self, req: UserRequest) -> list[MissingRequirement]:
        missing: list[MissingRequirement] = []

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

        if not req.infra_tech_stack.components:
            missing.append(
                MissingRequirement(
                    field="infra_tech_stack.components",
                    question="Which infra components should be installed?",
                    reason="The build flow needs at least one component such as Tomcat, Apache, Kafka, or Pinpoint.",
                )
            )

        versions = req.infra_tech_stack.versions
        if not versions:
            missing.append(
                MissingRequirement(
                    field="infra_tech_stack.versions",
                    question="Which component versions should be applied?",
                    reason="Version selection is required to generate deterministic install steps.",
                )
            )
        else:
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
            requires_java = any(component in {"tomcat", "kafka"} for component in req.infra_tech_stack.components) or (
                req.app_tech_stack.language.strip().lower().startswith("java")
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
            "Blocked by missing required fields."
            if blocked
            else "Ready to install and configure required infra components."
        )
        app_detail = (
            "Blocked by missing required fields."
            if blocked
            else "Ready to generate app source and build deployable artifact."
        )
        return [
            PlanStep(
                name="plan",
                owner="supervisor",
                status="completed",
                detail="Validated required input and assembled a LangGraph workflow.",
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

    def _plan_node(self, state: SupervisorState) -> SupervisorState:
        request = state["request"]
        missing_requirements = self._missing_info(request)
        blocked = bool(missing_requirements)
        summary = self.llm.summarize_plan(request, missing_requirements)
        return {
            "blocked": blocked,
            "missing_requirements": missing_requirements,
            "summary": summary,
            "environment_summary": self._environment_summary(request),
            "steps": self._build_plan_steps(blocked),
            "execution_path": ["plan"],
        }

    def _route_after_plan(self, state: SupervisorState) -> str:
        if state["mode"] == "plan" or state["blocked"]:
            return "finalize"
        return "dispatch"

    def _dispatch_node(self, _state: SupervisorState) -> SupervisorState:
        return {"execution_path": ["dispatch"]}

    def _build_infra_node(self, state: SupervisorState) -> SupervisorState:
        request = state["request"]
        commands = [f"install_component --name {component}" for component in request.infra_tech_stack.components]
        commands.append(f"mkdir -p {request.logging.base_dir} {request.logging.gc_log_dir} {request.logging.app_log_dir}")
        execution = AgentExecution(
            agent="infra_build",
            success=True,
            executed_commands=commands,
            notes=[
                "sudo usage follows constraints.sudo_allowed.",
                "Production targets are blocked by default policy.",
            ],
        )
        return {
            "executed": [execution],
            "generated_outputs": ["infra bootstrap script"],
            "recommended_config": [
                "Store GC logs and app logs in separate directories.",
                "Validate sudo scope before remote execution.",
            ],
            "rollback_cleanup": [
                "stop services: app/tomcat/kafka",
                "restore changed config backups",
            ],
            "execution_path": ["build_infra"],
        }

    def _generate_app_node(self, state: SupervisorState) -> SupervisorState:
        request = state["request"]
        execution = AgentExecution(
            agent="sample_app",
            success=True,
            executed_commands=[
                f"scaffold_app --framework {request.app_tech_stack.framework} --language {request.app_tech_stack.language}",
                "build_artifact --type service",
            ],
            notes=[
                "Include API endpoints and memory leak/OOM simulation options in generated app.",
                "Mask DB credentials in logs and reports.",
            ],
        )
        return {
            "executed": [execution],
            "generated_outputs": ["sample app source", "runbook"],
            "recommended_config": [
                "JAVA_OPTS: -Xms2g -Xmx2g -XX:+UseG1GC",
                "Tune Kafka partitions and replication by target TPS.",
            ],
            "rollback_cleanup": ["remove generated artifacts and temp files"],
            "execution_path": ["generate_app"],
        }

    def _finalize_node(self, state: SupervisorState) -> SupervisorState:
        if state["blocked"]:
            final_summary = "Workflow stopped at the planning gate because required inputs are still missing."
        elif state["mode"] == "plan":
            final_summary = "Workflow was planned successfully and is ready for execution."
        else:
            final_summary = "LangGraph workflow completed simulated infra and app execution."
        return {
            "final_summary": final_summary,
            "execution_path": ["finalize"],
        }

    def plan(self, req: UserRequest) -> BuildPlan:
        state = self.graph.invoke(self._initial_state(req, mode="plan"))
        missing_requirements = state.get("missing_requirements", [])
        return BuildPlan(
            summary=state.get("summary", ""),
            missing_info=[item.field for item in missing_requirements],
            missing_requirements=missing_requirements,
            steps=state.get("steps", []),
            graph=self.graph_view(),
        )

    def chat_reply(self, req: UserRequest) -> tuple[str, BuildPlan]:
        plan = self.plan(req)
        reply = self.llm.generate_supervisor_reply(req, plan)
        print(plan, reply)
        return reply, plan

    def run(self, req: UserRequest) -> SupervisorRunResult:
        state = self.graph.invoke(self._initial_state(req, mode="run"))
        missing_requirements = state.get("missing_requirements", [])
        if missing_requirements:
            raise MissingInfoError(
                missing_fields=[item.field for item in missing_requirements],
                missing_requirements=missing_requirements,
            )

        print(self.graph_view())
        print(state.get("final_summary", ""))

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
