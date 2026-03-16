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
    build_system: Literal["auto", "maven", "gradle"] = "auto"
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

    def describe(self) -> str:
        """Return a user-facing description of this plan step."""
        if self.name == "plan":
            return "입력된 요구사항과 대상 환경 정보를 검토하고, 바로 실행 가능한 상태인지 확인합니다."
        if self.name == "build_infra":
            if self.status == "failed":
                return "필수 정보가 아직 부족해 인프라 설치 및 환경 구성 작업은 시작할 수 없습니다."
            return "대상 서버에 필요한 인프라 구성요소를 설치하고, 로그 경로와 기본 실행 환경을 준비합니다."
        if self.name == "generate_app":
            if self.status == "failed":
                return "필수 정보가 아직 부족해 샘플 애플리케이션 생성 및 배포 준비 작업은 시작할 수 없습니다."
            return "요청한 프레임워크와 언어 기준으로 샘플 애플리케이션을 생성하고, 배포 가능한 산출물을 준비합니다."
        return self.detail


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
