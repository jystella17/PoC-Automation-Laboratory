from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

from Supervisor.models import AgentExecution, GraphEdge, GraphNode, GraphView


class ApplicationFilePlan(BaseModel):
    path: str
    purpose: str
    language: str


class ValidationIssue(BaseModel):
    path: str
    message: str


class ApplicationPlan(BaseModel):
    app_id: str
    framework: str
    framework_version: str
    language: str
    build_system: Literal["maven", "gradle"] = "maven"
    runtime_version: str = ""
    artifact_type: Literal["jar", "zip"] = "zip"
    artifact_name: str
    image_name: str
    project_dir: str
    log_dir: str
    gc_log_dir: str = ""
    special_scenarios: List[str] = Field(default_factory=list)
    deployment_commands: List[str] = Field(default_factory=list)
    required_env: List[str] = Field(default_factory=list)
    file_plan: List[ApplicationFilePlan] = Field(default_factory=list)
    spec_markdown: str = ""


class GeneratedFile(BaseModel):
    path: str
    description: str


class SampleAppRunResult(BaseModel):
    execution: AgentExecution
    generated_outputs: List[str] = Field(default_factory=list)
    recommended_config: List[str] = Field(default_factory=list)
    rollback_cleanup: List[str] = Field(default_factory=list)
    spec_markdown: str = ""
    generated_files: List[GeneratedFile] = Field(default_factory=list)
    graph: GraphView = Field(default_factory=GraphView)


GRAPH_NODES = [
    GraphNode(node_id="START", label="Start", kind="start"),
    GraphNode(node_id="plan_spec", label="Plan Spec", kind="task"),
    GraphNode(node_id="generate_files", label="Generate Files", kind="task"),
    GraphNode(node_id="validate_files", label="Validate Files", kind="gate"),
    GraphNode(node_id="repair_files", label="Repair Files", kind="task"),
    GraphNode(node_id="package_artifacts", label="Package Artifacts", kind="task"),
    GraphNode(node_id="finalize", label="Finalize Result", kind="end"),
    GraphNode(node_id="END", label="End", kind="end"),
]

GRAPH_EDGES = [
    GraphEdge(source="START", target="plan_spec"),
    GraphEdge(source="plan_spec", target="generate_files"),
    GraphEdge(source="generate_files", target="validate_files"),
    GraphEdge(source="validate_files", target="repair_files", condition="validation failed and repair budget remains"),
    GraphEdge(source="validate_files", target="package_artifacts", condition="validation passed"),
    GraphEdge(source="validate_files", target="finalize", condition="validation failed and repair budget exhausted"),
    GraphEdge(source="repair_files", target="validate_files"),
    GraphEdge(source="package_artifacts", target="finalize"),
    GraphEdge(source="finalize", target="END"),
]

GRAPH_MERMAID = "\n".join(
    [
        "graph TD",
        "    START([Start]) --> plan_spec[Plan Spec]",
        "    plan_spec --> generate_files[Generate Files]",
        "    generate_files --> validate_files{Validate Files}",
        "    validate_files -->|valid| package_artifacts[Package Artifacts]",
        "    validate_files -->|repair| repair_files[Repair Files]",
        "    validate_files -->|failed| finalize[Finalize Result]",
        "    repair_files --> validate_files",
        "    package_artifacts --> finalize",
        "    finalize --> END([End])",
    ]
)
