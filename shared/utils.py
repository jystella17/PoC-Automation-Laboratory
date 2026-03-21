from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_prior_notes(
    executions: list,
    agent_filter: str | None = None,
    max_chars: int = 3000,
) -> str:
    notes: list[str] = []
    for execution in executions:
        if agent_filter is not None and getattr(execution, "agent", None) != agent_filter:
            continue
        notes.extend(getattr(execution, "notes", []))
    merged = "\n".join(note for note in notes if note.strip())
    if not merged:
        return "none"
    if len(merged) > max_chars:
        return merged[:max_chars] + "\n...truncated..."
    return merged
