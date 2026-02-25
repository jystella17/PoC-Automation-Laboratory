# PoC-Automation-Laboratory

## Supervisor Agent + FastAPI + Streamlit

### 1) 의존성 설치
```bash
pip install -r requirements-supervisor.txt
```

### 2) FastAPI 실행
```bash
uvicorn apps.supervisor_api:app --host 0.0.0.0 --port 8000 --reload
```

### 3) Streamlit UI 실행
```bash
streamlit run ui/chat_ui.py
```

### 주요 엔드포인트
- `GET /health`
- `POST /v1/supervisor/plan`
- `POST /v1/supervisor/run`
- `POST /v1/chat`
