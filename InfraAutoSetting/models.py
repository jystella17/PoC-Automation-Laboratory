from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

from Supervisor.models import AgentExecution, GraphEdge, GraphNode, GraphView


class InfraScriptArtifact(BaseModel):
    path: str
    description: str


class InfraBuildRunResult(BaseModel):
    execution: AgentExecution
    generated_outputs: List[str] = Field(default_factory=list)
    recommended_config: List[str] = Field(default_factory=list)
    rollback_cleanup: List[str] = Field(default_factory=list)
    generated_files: List[InfraScriptArtifact] = Field(default_factory=list)
    graph: GraphView = Field(default_factory=GraphView)


GRAPH_NODES = [
    GraphNode(node_id="START", label="Start", kind="start"),
    GraphNode(node_id="plan_script", label="Plan Script", kind="task"),
    GraphNode(node_id="write_script", label="Write Script", kind="task"),
    GraphNode(node_id="validate_script", label="Validate Script", kind="gate"),
    GraphNode(node_id="run_remote", label="Run Remote", kind="task"),
    GraphNode(node_id="finalize", label="Finalize Result", kind="end"),
    GraphNode(node_id="END", label="End", kind="end"),
]

GRAPH_EDGES = [
    GraphEdge(source="START", target="plan_script"),
    GraphEdge(source="plan_script", target="write_script"),
    GraphEdge(source="write_script", target="validate_script"),
    GraphEdge(source="validate_script", target="run_remote", condition="validation passed"),
    GraphEdge(source="validate_script", target="finalize", condition="validation failed"),
    GraphEdge(source="run_remote", target="finalize"),
    GraphEdge(source="finalize", target="END"),
]

GRAPH_MERMAID = "\n".join(
    [
        "graph TD",
        "    START([Start]) --> plan_script[Plan Script]",
        "    plan_script --> write_script[Write Script]",
        "    write_script --> validate_script{Validate Script}",
        "    validate_script -->|valid| run_remote[Run Remote]",
        "    validate_script -->|invalid| finalize[Finalize Result]",
        "    run_remote --> finalize",
        "    finalize --> END([End])",
    ]
)
