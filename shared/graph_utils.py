from __future__ import annotations

from Supervisor.models import GraphEdge, GraphNode


_KIND_SHAPE = {
    "start": ("([", "])"),
    "end": ("([", "])"),
    "gate": ("{", "}"),
    "task": ("[", "]"),
}


def generate_mermaid(nodes: list[GraphNode], edges: list[GraphEdge]) -> str:
    node_map = {node.node_id: node for node in nodes}
    lines = ["graph TD"]
    rendered_nodes: set[str] = set()

    for edge in edges:
        source = node_map.get(edge.source)
        target = node_map.get(edge.target)
        src_str = _node_str(source, edge.source)
        tgt_str = _node_str(target, edge.target)
        if edge.source not in rendered_nodes:
            rendered_nodes.add(edge.source)
        if edge.target not in rendered_nodes:
            rendered_nodes.add(edge.target)

        if edge.condition:
            lines.append(f"    {src_str} -->|{edge.condition}| {tgt_str}")
        else:
            lines.append(f"    {src_str} --> {tgt_str}")

    return "\n".join(lines)


def _node_str(node: GraphNode | None, node_id: str) -> str:
    if node is None:
        return node_id
    left, right = _KIND_SHAPE.get(node.kind, ("[", "]"))
    return f"{node.node_id}{left}{node.label}{right}"
