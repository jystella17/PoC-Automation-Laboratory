# Infra Test Lab
사용자 요청 기반으로 테스트 환경을 구축하고 샘플 애플리케이션을 생성 및 배포하는 Multi-Agent 테스트 환경 구축 서비스
Supervisor Agent가 입력을 검증하고 실행 계획을 수립한 뒤, Infra Agent와 Application Agent를 호출해 인프라 구성과 애플리케이션 산출물을 만든다.

## 0) Objectives (System Goals)
사용자가 입력한 인프라 요구사항, 애플리케이션 스택, 추가 요청을 바탕으로 아래 작업을 수행한다.
- 테스트 대상 서버 정보와 필수 입력값을 검증한다.
- 실행 전 계획(BuildPlan)과 그래프 정보를 제공한다.
- 필요 시 인프라 설치 스크립트와 샘플 애플리케이션 프로젝트를 생성한다.
- 비동기 실행 상태와 이벤트 스트림을 API/UI에서 확인할 수 있게 한다.

---

## 1) Agents
현재 구현은 LangGraph 기반 Supervisor가 하위 Agent를 오케스트레이션하는 구조다.
사용자의 입력값을 기반으로 `build_infra/generate_app` 실행 순서를 결정하고 순차 실행한다.

### 1.1 Supervisor Agent
**Role**
- `UserRequest`를 입력으로 받아 필수 입력 누락 여부를 검증한다.
- `BuildPlan`을 생성하고, 실행 가능하면 하위 Agent 호출 순서를 결정한다.
- `run` 모드에서는 Infra Agent와 Application Agent를 순차 호출한 뒤 `SupervisorRunResult`를 반환한다.
- 실행 이벤트를 기록하고 SSE 구독용 이벤트 스트림을 제공한다.

**Current Behavior**
- 그래프 노드: `plan -> dispatch -> build_infra -> generate_app -> finalize`
- `mode=plan` 또는 필수 정보 누락 시 `finalize`로 바로 이동한다.
- 인프라 요청이 없으면 Infra 단계는 skip되고, 앱 요청이 없으면 Sample App 단계는 skip된다.

**Failure Policy**
- 필수 입력이 누락되면 `MissingRequirement` 목록과 질문 문구를 포함한 계획 결과를 반환한다.
- `run` 호출 시 필수 입력이 누락되면 HTTP 400과 함께 `missing_fields`, `missing_requirements`를 반환한다.

### 1.2 InfraAutoSetting Agent
**Role**
- 요청된 인프라 컴포넌트 기준으로 bootstrap shell script를 생성한다.
- 버전 카탈로그를 참고해 컴포넌트 버전을 보정한다.
- 로그 디렉터리 생성 정책과 Java 런타임 설치 정책을 스크립트에 강제한다.
- 스크립트 정적 검증 후 SSH 실행 또는 dry-run 실행을 수행한다.

**Current Scope**
- 지원 컴포넌트 중심: `apache`, `tomcat`, `kafka`, `pinpoint`
- 대상 호스트의 `os_type`에 따라 `apt` 또는 `dnf` 기반 설치 명령을 만든다.
- 결과로 실행 명령, 생성 산출물, 권장 설정, rollback/cleanup 가이드를 반환한다.

**Safety / Guardrails**
- `constraints.sudo_allowed`에 따라 `sudo` 사용 여부를 제한한다.
- 로그 경로 생성과 Java 설치 검증을 스크립트에 명시적으로 포함한다.
- 원격 실행 실패 시 exit code, stdout/stderr 일부를 notes에 기록한다.

### 1.3 SampleAppGen Agent
**Role**
- 요청된 프레임워크와 언어를 기준으로 `APPLICATION_SPEC.md`와 프로젝트 파일을 생성한다.
- 정적 검증 후 필요 시 repair 라운드를 최대 2회 수행한다.
- 빌드 산출물과 Docker 이미지를 생성하고 배포용 권장 설정을 반환한다.

**Current Scope**
- 현재 코드 기준 지원 프레임워크: `Spring Boot`, `Spring`, `FastAPI`
- 언어 해석: Spring 계열은 Java, FastAPI는 Python 중심
- 빌드 시스템 입력: `auto`, `maven`, `gradle`
- 생성 위치: 저장소 하위 `generated_apps/`

**Packaging / Delivery**
- 프로젝트 소스와 `APPLICATION_SPEC.md`를 생성한다.
- 빌드 성공 시 jar/zip 계열 산출물과 Docker 이미지 생성 결과를 반환한다.
- Docker 업로드가 실패해도 실패 사유를 notes에 남긴다.

---

## 2) 현재 구현 범위
### 2.1 Supervisor Agent
- LangGraph 상태 그래프로 동작한다.
- `UserRequest`를 입력으로 받아 `BuildPlan` 또는 `SupervisorRunResult`를 생성한다.
- `plan` 모드에서는 누락 정보, 질문, 실행 단계, 그래프 정보를 반환한다.
- `run` 모드에서는 순차 실행 결과와 이벤트 로그를 반환한다.
- 비동기 실행은 Celery task와 Redis 기반 event store를 사용한다.

### 2.2 UI
- Streamlit UI에서 인프라/애플리케이션/타깃 서버/부가 요청을 입력받는다.
- 폼 입력을 정리하고 검증한 뒤 `UserRequest` JSON으로 변환한다.
- 현재 UI의 실행 기본 경로는 `/v1/supervisor/run-async`다.
- 진행 상태는 `/v1/supervisor/run-async/{run_id}/events` SSE를 통해 실시간 표시한다.
- 사이드바에서 FastAPI health check를 수행할 수 있다.

### 2.3 LLM
- Azure OpenAI는 선택 사항이다.
- 설정이 유효하면 계획 요약, 애플리케이션 스펙/파일 생성, Supervisor 응답 생성에 활용한다.
- 설정이 없으면 각 Agent는 fallback 로직으로 계획/스크립트/소스 파일을 생성한다.

---

## 3) 실행 그래프 아키텍처
### 3.1 Supervisor LangGraph 노드
- `START`
- `plan`
- `dispatch`
- `build_infra`
- `generate_app`
- `finalize`
- `END`

### 3.2 Supervisor 플로우 규칙
- `START -> plan`
- `plan -> dispatch`: `mode=run` 이고 필수 입력이 모두 있을 때
- `plan -> finalize`: `mode=plan` 이거나 필수 입력이 누락됐을 때
- `dispatch -> build_infra`
- `build_infra -> generate_app`
- `generate_app -> finalize`
- `finalize -> END`

주의:
- 현재 구현은 Infra와 App을 병렬 실행하지 않는다.
- `dispatch`는 실제 외부 큐 분배가 아니라 실행 대상 단계를 결정하는 게이트다.
- `graph_view()`는 노드/엣지/mermaid 문자열을 함께 반환한다.

---

## 4) 입력 데이터 모델
Supervisor와 UI는 아래 `UserRequest` 스키마를 기준으로 동작한다.

```json
{
  "infra_tech_stack": {
    "os": "Linux",
    "components": ["apache", "tomcat", "kafka", "pinpoint"],
    "versions": {
      "apache": "2.4.66",
      "tomcat": "10",
      "kafka": "3.6",
      "pinpoint": "Pinpoint v3",
      "java": "21"
    },
    "instances": {
      "apache": 1,
      "tomcat": 1,
      "kafka_consumer": 1,
      "pinpoint_agent": 1
    }
  },
  "load_profile": {
    "tps": 5000,
    "payload_bytes": 1024,
    "duration_sec": 600,
    "concurrency": 200
  },
  "topology": {
    "nodes": 1,
    "apps": 1
  },
  "constraints": {
    "no_public_upload": true,
    "security_policy_notes": [],
    "sudo_allowed": "limited",
    "network_policy": {
      "allow_open_port_80": true,
      "allow_firewall_changes": false
    },
    "apache_config_mode": "system_prompt_default"
  },
  "targets": [
    {
      "host": "10.0.0.10",
      "user": "ec2-user",
      "auth_ref": "/path/to/key.pem",
      "auth_method": "pem_path",
      "ssh_port": 22,
      "os_type": "Ubuntu22.04"
    }
  ],
  "logging": {
    "base_dir": "/var/log/infra-test-lab",
    "gc_log_dir": "/var/log/infra-test-lab/gc",
    "app_log_dir": "/var/log/infra-test-lab/app"
  },
  "app_tech_stack": {
    "framework": "FastAPI",
    "minor_version": "FastAPI 0.135.1",
    "build_system": "auto",
    "language": ["Python3.12"],
    "databases": "MySQL",
    "db_user": "appuser",
    "db_pw": "secret"
  },
  "additional_request": "memory leak 재현 시나리오 포함"
}
```

### 4.1 주요 모델
- `TargetHost`: `host`, `user`, `auth_ref`, `auth_method`, `ssh_port`, `os_type`
- `InfraTechStack`: `os`, `components`, `versions`, `instances`
- `LoadProfile`: `tps`, `payload_bytes`, `duration_sec`, `concurrency`
- `Topology`: `nodes`, `apps`
- `RequestConstraints`: `no_public_upload`, `security_policy_notes`, `sudo_allowed`, `network_policy`, `apache_config_mode`
- `LoggingConfig`: `base_dir`, `gc_log_dir`, `app_log_dir`
- `AppTechStack`: `framework`, `minor_version`, `build_system`, `language`, `databases`, `db_user`, `db_pw`
- `UserRequest`: 위 모델 전체 + `additional_request`

---

## 5) 필수 정보 검증 규칙
Supervisor의 `plan` 단계에서 아래 항목을 검증한다.

### 5.1 대상 서버
- `targets`가 비어 있으면 실행 차단
- 각 대상에 대해 `host`, `user`, `auth_ref`가 비어 있으면 실행 차단

### 5.2 인프라 / 앱 선택
- 인프라 컴포넌트와 앱 프레임워크가 모두 없으면 실행 차단
- 인프라 컴포넌트가 선택됐는데 `infra_tech_stack.versions`가 비어 있으면 실행 차단
- `tomcat` 선택 시 `versions.tomcat` 필수
- `kafka` 선택 시 `versions.kafka` 필수
- `tomcat`, `kafka`, 또는 Java 계열 앱 언어 선택 시 `versions.java` 필수

### 5.3 로그 정책
- `logging.base_dir`가 비어 있으면 실행 차단

### 5.4 차단 시 반환 정보
- `BuildPlan.missing_info`
- `BuildPlan.missing_requirements`
- 각 누락 항목에 대한 `question`, `reason`
- 차단 시 `build_infra`, `generate_app` 단계 상태는 `failed`

---

## 6) UI 입력 및 검증 규칙
### 6.1 선택 가능한 주요 옵션
- 컴포넌트: `Apache`, `Tomcat`, `Kafka`, `Pinpoint`, `Others`
- OS: `Linux`, `Windows`
- Target OS: `Ubuntu22.04`, `Rhel9`, `Amazon Linux2023`, `Debian12`
- SSH 인증 방식: `pem_path`, `password`, `ssm`
- 언어: `Java21`, `Java17`, `Python3.12`, `Python3.11`, 직접 입력 옵션
- DB: `None`, `MySQL`, `PostgreSQL`, `MariaDB`, `Redis`, `MongoDB`
- 프레임워크: `None`, `Spring Boot`, `Spring`, `FastAPI`
- 빌드 시스템: `auto`, `maven`, `gradle`

### 6.2 UI 정리 규칙
- 선택하지 않은 컴포넌트의 버전/인스턴스는 `None`, `0`으로 정리한다.
- 프레임워크가 `None`이면 프레임워크 버전은 `None`, 앱 인스턴스 수는 `0`으로 정리한다.
- DB가 `None`이면 `db_user`, `db_pw`는 빈 문자열로 정리한다.
- 프레임워크 선택 시 기본 버전과 앱 인스턴스 `1`을 자동 적용한다.
- 컴포넌트 선택 시 기본 버전과 인스턴스 `1`을 자동 적용한다.

### 6.3 UI 검증 규칙
- 선택한 컴포넌트의 버전은 `None`일 수 없다.
- 선택한 컴포넌트의 인스턴스 수는 1 이상이어야 한다.
- 프레임워크를 선택했으면 프레임워크 버전이 필요하다.
- 프레임워크를 선택했으면 앱 인스턴스 수는 1 이상이어야 한다.
- `host`, `auth_ref`는 필수다.
- `ssh_port`는 1~65535 범위여야 한다.
- `ssh_auth_method=pem_path`이면 `auth_ref`는 파일명만이 아닌 경로 또는 시크릿 참조여야 한다.
- DB를 선택했으면 `db_user`, `db_pw`를 모두 입력해야 한다.
- 현재 UI 기준 패키지 설치나 보호 경로(`/var`, `/etc`, `/usr`, `/opt`) 사용이 필요하면 `sudo_allowed=yes`가 필요하다.

### 6.4 현재 UI 기본 정책
- `allow_open_port_80=true`
- `allow_firewall_changes=false` 기본값
- `apache_config_mode=system_prompt_default`
- 로그 경로 기본값:
  - `base_dir=/var/log/infra-test-lab`
  - `gc_log_dir=/var/log/infra-test-lab/gc`
  - `app_log_dir=/var/log/infra-test-lab/app`

---

## 7) REST API 목록
### 7.1 Health Check
- `GET /health`

### 7.2 Graph View
- `GET /v1/supervisor/graph`
- Supervisor 그래프 노드/엣지/mermaid 문자열 반환

### 7.3 Plan Check
- `POST /v1/supervisor/plan`
- Request Body: `UserRequest`
- Response: `BuildPlan`

### 7.4 Sync Run
- `POST /v1/supervisor/run`
- Request Body: `UserRequest`
- Response: `SupervisorRunResult`
- 필수 입력 누락 시 HTTP 400

### 7.5 Async Run
- `POST /v1/supervisor/run-async`
- Response: `run_id`, `status`, `queued_at`

### 7.6 Async Run Status
- `GET /v1/supervisor/run-async/{run_id}`
- Response: 상태, 타임스탬프, 결과, 이벤트 목록

### 7.7 Async Event Stream
- `GET /v1/supervisor/run-async/{run_id}/events`
- SSE(`text/event-stream`) 기반 진행 이벤트 스트림

### 7.8 Chat Reply
- `POST /v1/chat`
- 마지막 user 메시지를 `UserRequest` JSON으로 해석해 계획 기반 응답 생성
- Response: `reply`, `metadata`

---

## 8) 주요 출력 모델
### 8.1 BuildPlan
- `summary`
- `missing_info`
- `missing_requirements`
- `steps`
- `graph`

### 8.2 SupervisorRunResult
- `environment_summary`
- `executed`
- `generated_outputs`
- `recommended_config`
- `rollback_cleanup`
- `graph`
- `execution_path`
- `final_summary`

### 8.3 AgentExecution
- `agent`
- `success`
- `executed_commands`
- `notes`

---

## 9) 구현 메모
- `apps/supervisor_api.py`: FastAPI 엔드포인트 및 async run 진입점
- `Supervisor/agent.py`: Supervisor LangGraph 오케스트레이션
- `Supervisor/validation.py`: 필수 입력 검증 규칙
- `InfraAutoSetting/agent.py`: 인프라 스크립트 생성 및 SSH 실행
- `SampleAppGen/agent.py`: 애플리케이션 스펙/소스/패키징 처리
- `ui/chat_ui.py`: Streamlit UI
- `ui/form_logic.py`: UI 폼 정리 및 검증 규칙
