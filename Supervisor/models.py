from __future__ import annotations

from typing import Any, List, Literal

from pydantic import BaseModel, Field


class TargetHost(BaseModel):
    host: str
    user: str
    auth_ref: str
    auth_method: Literal["pem_path", "password", "ssm"] = "pem_path"
    ssh_port: int = 22
    os_type: str


class InfraTechStack(BaseModel):
    os: str = "linux"
    components: List[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    instances: dict[str, int] = Field(default_factory=dict)


class LoadProfile(BaseModel):
    tps: int = 0
    payload_bytes: int = 0
    duration_sec: int = 0
    concurrency: int = 0


class Topology(BaseModel):
    nodes: int = 1
    apps: int = 1


class RequestConstraints(BaseModel):
    no_public_upload: bool = True
    security_policy_notes: List[str] = Field(default_factory=list)
    sudo_allowed: Literal["yes", "no", "limited"] = "limited"
    network_policy: dict[str, bool] = Field(
        default_factory=lambda: {
            "allow_open_port_80": True,
            "allow_firewall_changes": False,
        }
    )
    apache_config_mode: str = "system_prompt_default"


class LoggingConfig(BaseModel):
    base_dir: str = "/var/log/infra-test-lab"
    gc_log_dir: str = "/var/log/infra-test-lab/gc"
    app_log_dir: str = "/var/log/infra-test-lab/app"


class AppTechStack(BaseModel):
    framework: str = ""
    minor_version: str = ""
    language: List[str] = Field(default_factory=lambda: [""])
    databases: str = ""
    db_user: str = ""
    db_pw: str = ""


class UserRequest(BaseModel):
    infra_tech_stack: InfraTechStack = Field(default_factory=InfraTechStack)
    load_profile: LoadProfile = Field(default_factory=LoadProfile)
    topology: Topology = Field(default_factory=Topology)
    constraints: RequestConstraints = Field(default_factory=RequestConstraints)
    targets: List[TargetHost] = Field(default_factory=list)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    app_tech_stack: AppTechStack = Field(default_factory=AppTechStack)
    additional_request: str = ""


class MissingRequirement(BaseModel):
    field: str
    question: str
    reason: str


class PlanStep(BaseModel):
    name: str
    owner: Literal["supervisor", "infra_build", "sample_app"]
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"
    detail: str


class GraphNode(BaseModel):
    node_id: str
    label: str
    kind: Literal["start", "task", "gate", "end"] = "task"


class GraphEdge(BaseModel):
    source: str
    target: str
    condition: str = ""


class GraphView(BaseModel):
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)
    mermaid: str = ""


class BuildPlan(BaseModel):
    summary: str
    missing_info: List[str] = Field(default_factory=list)
    missing_requirements: List[MissingRequirement] = Field(default_factory=list)
    steps: List[PlanStep] = Field(default_factory=list)
    graph: GraphView = Field(default_factory=GraphView)


class AgentExecution(BaseModel):
    agent: Literal["infra_build", "sample_app"]
    success: bool
    executed_commands: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class SupervisorRunResult(BaseModel):
    environment_summary: dict[str, str]
    executed: List[AgentExecution]
    generated_outputs: List[str]
    recommended_config: List[str]
    rollback_cleanup: List[str]
    graph: GraphView = Field(default_factory=GraphView)
    execution_path: List[str] = Field(default_factory=list)
    final_summary: str = ""


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]


class ChatResponse(BaseModel):
    reply: str
    metadata: dict[str, Any] = Field(default_factory=dict)
