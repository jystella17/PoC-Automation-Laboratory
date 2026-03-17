from __future__ import annotations

from apps.celery_app import celery_app
from Supervisor import MissingInfoError, SupervisorAgent
from Supervisor.models import UserRequest
from runtime_bus import RunEventStore

agent = SupervisorAgent()
store = RunEventStore()


@celery_app.task(name="supervisor.run")
def run_supervisor_task(run_id: str, payload: dict[str, object]) -> None:
    request = UserRequest.model_validate(payload)
    store.update_run(run_id, status="running", started_at=_now_iso())

    def _emit(event: dict[str, object]) -> None:
        store.append_event(run_id, event)

    try:
        result = agent.run(request, event_callback=_emit)
        store.set_result(run_id, result.model_dump(mode="json"), status="succeeded")
    except MissingInfoError as exc:
        error = {
            "message": "Required fields are missing; unable to execute.",
            "missing_fields": exc.missing_fields,
            "missing_requirements": [item.model_dump(mode="json") for item in exc.missing_requirements],
        }
        store.append_event(
            run_id,
            {
                "timestamp": _now_iso(),
                "owner": "supervisor",
                "phase": "run",
                "status": "failed",
                "message": "필수 입력값이 부족하여 실행을 중단합니다.",
                "details": error,
            },
        )
        store.update_run(run_id, status="failed", finished_at=_now_iso(), error=error)
    except Exception as exc:
        store.append_event(
            run_id,
            {
                "timestamp": _now_iso(),
                "owner": "supervisor",
                "phase": "run",
                "status": "failed",
                "message": "백그라운드 실행이 예외로 종료되었습니다.",
                "details": {"error": str(exc)},
            },
        )
        store.set_error(run_id, str(exc))


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
