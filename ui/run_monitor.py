from __future__ import annotations

import json

import streamlit as st

from api_client import get_supervisor_run_status, stream_supervisor_run_events


def _format_run_result(run_status: dict[str, object]) -> str:
    result = run_status.get("result", {}) if isinstance(run_status.get("result"), dict) else {}
    executed = result.get("executed", []) if isinstance(result, dict) else []
    final_summary = str(result.get("final_summary", "")).strip() if isinstance(result, dict) else ""
    lines = ["실행 결과"]
    if final_summary:
        lines.append(f"- 최종 요약: {final_summary}")
    for item in executed:
        if not isinstance(item, dict):
            continue
        agent_name = str(item.get("agent", "unknown")).strip()
        success = bool(item.get("success", False))
        lines.append(f"- {agent_name}: {'success' if success else 'failed'}")
        notes = item.get("notes", [])
        if isinstance(notes, list) and notes:
            first_note = str(notes[0]).strip()
            if first_note:
                lines.append(f"  - note: {first_note}")
    return "\n".join(lines)


def render_active_run_progress() -> None:
    run_id = str(st.session_state.get("active_run_id", "")).strip()
    events = st.session_state.get("active_run_events", [])
    status = str(st.session_state.get("active_run_status", "")).strip()
    if not run_id and not events:
        return

    st.subheader("실행 진행 상황")
    if run_id:
        st.caption(f"run_id={run_id}, status={status or 'unknown'}")
    for event in events:
        if not isinstance(event, dict):
            continue
        owner = str(event.get("owner", "unknown")).strip()
        phase = str(event.get("phase", "")).strip()
        event_status = str(event.get("status", "")).strip()
        message = str(event.get("message", "")).strip()
        timestamp = str(event.get("timestamp", "")).strip()
        st.markdown(f"- `{timestamp}` [{owner}/{phase}/{event_status}] {message}")
        details = event.get("details", {})
        if isinstance(details, dict) and details:
            st.code(json.dumps(details, ensure_ascii=False, indent=2), language="json")


def consume_active_run_stream(api_url: str) -> None:
    run_id = str(st.session_state.get("active_run_id", "")).strip()
    if not run_id:
        return

    event_placeholder = st.empty()
    try:
        for event_name, payload in stream_supervisor_run_events(
            api_url,
            run_id,
            last_event_id=int(st.session_state.get("active_run_last_event_id", 0)),
        ):
            if event_name == "event":
                event_id = int(payload.get("event_id", 0))
                if event_id <= int(st.session_state.get("active_run_last_event_id", 0)):
                    continue
                st.session_state.active_run_last_event_id = event_id
                st.session_state.active_run_events.append(payload)
                st.session_state.active_run_status = str(payload.get("status", st.session_state.get("active_run_status", "")))
                with event_placeholder.container():
                    render_active_run_progress()
            elif event_name == "done":
                break
    except Exception as exc:
        st.warning(f"이벤트 스트림 연결 실패(run_id={run_id}): {exc}")
        return

    try:
        run_status = get_supervisor_run_status(api_url, run_id)
    except Exception as exc:
        st.warning(f"최종 실행 결과 조회 실패(run_id={run_id}): {exc}")
        return

    status = str(run_status.get("status", "")).strip()
    st.session_state.active_run_status = status
    events = run_status.get("events", [])
    if isinstance(events, list):
        st.session_state.active_run_events = events
        if events:
            st.session_state.active_run_last_event_id = max(int(event.get("event_id", 0)) for event in events if isinstance(event, dict))

    if st.session_state.active_run_notified:
        return

    if status == "succeeded":
        st.session_state.messages.append({"role": "assistant", "content": _format_run_result(run_status)})
    else:
        error = str(run_status.get("error", "unknown error")).strip()
        st.session_state.messages.append({"role": "assistant", "content": f"실행 실패(run_id={run_id}): {error}"})

    st.session_state.active_run_notified = True
    st.session_state.active_run_id = ""
