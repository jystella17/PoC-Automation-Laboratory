# PoC-Automation-Laboratory

## Supervisor Agent + FastAPI + Streamlit

### 1) 의존성 설치
```bash
pip install -r requirements-supervisor.txt
```

### 2) Azure OpenAI 설정 파일 준비
```bash
cp config/settings.json config/supervisor_settings.json
```

`config/supervisor_settings.json`에 Azure OpenAI `endpoint`, `api_key`, `deployment_name`을 입력한다.
필요하면 아래 환경변수로 파일 값을 덮어쓸 수 있다.

```bash
export AZURE_OPENAI_ENABLED=true
export AZURE_OPENAI_ENDPOINT="https://your-resource-name.openai.azure.com/"
export AZURE_OPENAI_API_KEY="..."
export AZURE_OPENAI_DEPLOYMENT="gpt-4o-mini"
export AZURE_OPENAI_API_VERSION="2024-02-01"
```

### 3) FastAPI 실행
```bash
uvicorn apps.supervisor_api:app --host 0.0.0.0 --port 8000 --reload
```

### 4) Redis 실행
```bash
redis-server
```

기본 연결 주소는 `redis://127.0.0.1:6379/0` 이며, 필요하면 아래 환경변수로 변경한다.

```bash
export SUPERVISOR_REDIS_URL="redis://127.0.0.1:6379/0"
```

### 5) Celery Worker 실행
```bash
celery -A apps.celery_app.celery_app worker --loglevel=info
```

### 6) Streamlit UI 실행
```bash
streamlit run ui/chat_ui.py
```

### 주요 엔드포인트
- `GET /health`
- `GET /v1/supervisor/graph`
- `POST /v1/supervisor/plan`
- `POST /v1/supervisor/run`
- `POST /v1/supervisor/run-async`
- `GET /v1/supervisor/run-async/{run_id}`
- `GET /v1/supervisor/run-async/{run_id}/events`
- `POST /v1/chat`

### Supervisor 구현 메모
- `Supervisor/agent.py`는 `LangGraph` 상태 그래프로 `plan -> dispatch -> build_infra/generate_app -> finalize` 흐름을 정의한다.
- 그래프 시각화 정보는 `BuildPlan.graph`, `SupervisorRunResult.graph`, `GET /v1/supervisor/graph`에서 확인할 수 있다.
- 비동기 실행은 `Celery + Redis` 기반이며, 각 Agent 단계/Tool 호출 이벤트는 Redis에 저장되고 SSE로 UI에 실시간 전달된다.
