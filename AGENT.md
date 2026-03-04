# Infra Test Lab
인프라 운영자를 위한 테스트 환경 구축/스펙 검증 플랫폼. 
Supervisor Agent가 사용자 요구사항을 구조화하고, Application/Infra Setting Agent를 orchestration 한다.

## 0) Objectives (System Goals)
사용자가 입력한 기술스택/요구사항/테스트 시나리오에 맞게
- 테스트 환경 인프라를 안전하게 구축하고
- 샘플 애플리케이션을 생성/배포한다.
---

## 1) Agents
* LangGraph 기반 구현, Supervisor Agent - Sample App / Infra Build Agent는 A2A 통신 필요
#### 1.1 Supervisor Agent
**Role**
* 사용자 요구를 구조화(스택/목적/부하/대상서버/제약)하고 작업 계획을 만든다.
* Sub-agent를 순차/병렬 호출하고 결과를 합쳐 사용자에게 작업 결과를 알려준다.
* 
**Tone**
- 꼼꼼한 PM. 누락 정보가 있으면 실행 전에 질문한다.

**Failure Policy**
- 실패 시: 원인(명령/로그/환경/권한)을 요약하고, 사용자가 취할 액션을 제시한다.

#### 1.2 Infra Build Agent
**Role**
- Apache/Tomcat/Kafka/Pinpoint 등 인프라 기술스택 설치 및 초기구성
- 부하 테스트 도구(JMeter 등) 설치/실행 보조.
- 로그/메트릭 수집 경로 설정 및 수집 수행.

**Safety / Guardrails**
- sudo는 최소 사용(필요 이유를 명시)
- 실행 전 “생성된 스크립트 전문”을 사용자에게 제공하고 승인(ACK) 후 실행한다.
- 민감정보(키/토큰/비번)는 출력에 마스킹한다.

#### 1.3 Sample App Agent
**Role**
- 요구사항 기반 샘플 앱 생성 (예: Spring/JSP/REST, OOM/Leak 재현)
- 빌드 산출물(WAR/JAR/Docker image) 생성
- 배포 스펙(포트/환경변수/리소스 제한) 정의
- 애플리케이션 생성을 위한 기본 포맷 (API 문서, DB 스키마, 코드 컨벤션 등 - CLAUDE.md, AGENT.md 등에 포함되는 내용들)은 사전에 작성 -> 기술 스택 / Memory Leak 발생 등 사용자 요구사항을 md 문서에 포함 -> md 문서 기반 애플리케이션 코드 생성
- DB 등 애플리케이션 동작을 위한 추가 소프트웨어 설치 필요 시, Infra Agent에게 설치 요청
- Docker 기반 배포하는 경우, 애플리케이션 Dockerize 및 S3/ECR 등 업로드 -> Infra Agent에게 대상 서버에 Docker Image 배포 요청

**Constraints**
- 앱 생성 전 필수 입력(DB 계정, Port 등)이 없으면 질문 후 진행 

**Mechanism**
사용자 입력 및 요구사항을 기반으로 APPLICATION_SPEC.md 완성 -> DB 설치 등 필요한 경우 Infra Agent에게 설치 요청 -> APPLICATION_SPEC.md 기반으로 애플리케이션 코드 생성 -> 코드 build (Spring/Spring Boot/JSP라면 .jar 파일 등) -> (Docker 기반 배포하는 경우) DockerFile 생성 및 Dockerize -> Agent 배포 서버에서 접근 가능한 S3/ECR 등에 Push -> 애플리케이션 배포 대상 서버에서 애플리케이션 & DB 등 필요 SW 이미지/실행 파일 Pull -> 애플리케이션 실행 (docker run / java -jar 등)

## 2) 현재 구현 범위
#### 2.1 Supervisor Agent
- LangGraph 기반 상태 그래프로 동작한다.
- 사용자 입력을 `UserRequest`로 정규화한다.
- 실행 전 필수 정보 누락 여부를 점검하고 `BuildPlan`을 생성한다.
- `plan` 모드에서는 검토 결과만 반환한다.
- `run` 모드에서는 `build_infra`, `generate_app`, `finalize` 흐름을 거쳐 `SupervisorRunResult`를 반환한다.

#### 2.2 UI
- Streamlit UI에서 인프라/앱/대상 서버/부하 정보를 입력받는다.
- 입력값을 정리하고 검증한 뒤 Supervisor API에 전달한다.
- `/v1/supervisor/plan` 으로 사전 점검 결과를 표시한다.
- `/v1/chat` 으로 사용자 요청과 직전 대화 이력을 전달해 사용자용 설명 응답을 표시한다.

#### 2.3 LLM
- Azure OpenAI 사용은 선택 사항이다.
- 설정이 활성화되고 필수 값이 존재하면 Azure OpenAI로 요약/응답을 생성한다.
- 그렇지 않으면 코드 내 fallback 응답 생성 로직을 사용한다.

---

## 3) 시스템 아키텍처
#### 3.1 LangGraph 노드
- `START`
- `plan`
- `dispatch`
- `build_infra`
- `generate_app`
- `finalize`
- `END`

#### 3.2 라우팅 규칙
- `START -> plan`
- `plan -> dispatch`: `mode=run` 이고 필수 정보가 모두 있을 때
- `plan -> finalize`: `mode=plan` 이거나 필수 정보가 부족할 때
- `dispatch -> build_infra`
- `dispatch -> generate_app`
- `build_infra -> finalize`
- `generate_app -> finalize`
- `finalize -> END`

주의:
- 현재 그래프 정의상 `dispatch` 이후 `build_infra` 와 `generate_app` 가 모두 실행 경로에 연결되어 있다.
- 두 노드는 실제 외부 에이전트 호출이 아니라 Supervisor에서 호출하는 Sub-Agent이다.

---

## 4) 입력 데이터 포맷
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
    "sudo_allowed": "limited"
  },
  "targets": [
    {
      "host": "10.0.0.10",
      "user": "ec2-user",
      "auth_ref": "/path/to/key.pem",
      "os_type": "Ubuntu22.04"
    }
  ],
  "logging": {
    "base_dir": "/var/log/infra-test-lab",
    "gc_log_dir": "/var/log/infra-test-lab/gc",
    "app_log_dir": "/var/log/infra-test-lab/app"
  },
  "app_tech_stack": {
    "framework": "Spring Boot",
    "minor_version": "Spring Boot 4.0",
    "language": ["Java21"],
    "databases": "MySQL",
    "db_user": "appuser",
    "db_pw": "secret"
  },
  "additional_request": "메모리 릭 재현용 엔드포인트 포함"
}
```

#### 4.1 주요 모델
- `TargetHost`: `host`, `user`, `auth_ref`, `os_type`
- `InfraTechStack`: `os`, `components`, `versions`, `instances`
- `LoadProfile`: `tps`, `payload_bytes`, `duration_sec`, `concurrency`
- `Topology`: `nodes`, `apps`
- `RequestConstraints`: `no_public_upload`, `security_policy_notes`, `sudo_allowed`
- `LoggingConfig`: `base_dir`, `gc_log_dir`, `app_log_dir`
- `AppTechStack`: `framework`, `minor_version`, `language`, `databases`, `db_user`, `db_pw`
- `UserRequest`: 위 모든 모델 + `additional_request`

---

## 5) 필수 정보 점검 규칙
`plan` 단계에서 아래 항목을 검사한다.

#### 5.1 대상 서버
- `targets` 가 비어 있으면 실행 차단
- 각 타겟에 대해 `host`, `user`, `auth_ref` 가 비어 있으면 실행 차단

#### 5.2 인프라 구성
- `infra_tech_stack.components` 가 비어 있으면 실행 차단
- `infra_tech_stack.versions` 자체가 비어 있으면 실행 차단
- `tomcat` 선택 시 `versions.tomcat` 필수
- `kafka` 선택 시 `versions.kafka` 필수
- `tomcat`, `kafka`, 또는 Java 기반 앱 언어 선택 시 `versions.java` 필수

#### 5.3 로그 정책
- `logging.base_dir` 가 비어 있으면 실행 차단

#### 5.4 차단 시 동작
- `BuildPlan.missing_info`
- `BuildPlan.missing_requirements`
- 각 누락 항목에 대한 사용자 질문과 사유
- `build_infra`, `generate_app` 단계 상태를 `failed` 로 표시

---

## 6) UI 입력 및 검증 규칙
#### 6.1 선택 가능한 기본 옵션
- 컴포넌트: `Apache`, `Tomcat`, `Kafka`, `Pinpoint`, `Others`
- OS: `Linux`, `Windows`
- Target OS: `Ubuntu22.04`, `Rhel9`, `Amazon Linux2023`, `Debian12`
- 언어: `Java21`, `Java17`, `Python3.12`, `Python3.11`, `프롬프트로 직접 입력`
- DB: `None`, `MySQL`, `PostgreSQL`, `MariaDB`, `Redis`, `MongoDB`
- 프레임워크: `None`, `Spring Boot`, `Spring`, `FastAPI`

#### 6.2 UI 정리 규칙
- 선택하지 않은 컴포넌트의 버전과 인스턴스 수는 `None`, `0` 으로 정리한다.
- 프레임워크가 `None` 이면 세부 버전은 `None`, 애플리케이션 인스턴스 수는 `0` 으로 정리한다.
- DB가 `None` 이면 `db_user`, `db_pw` 는 빈 값으로 정리한다.

#### 6.3 UI 검증 규칙
- 선택한 컴포넌트는 버전이 `None` 이면 안 된다.
- 선택한 컴포넌트는 인스턴스 수가 1 이상이어야 한다.
- 프레임워크를 선택했으면 세부 버전이 필요하다.
- 프레임워크를 선택했으면 애플리케이션 인스턴스 수가 1 이상이어야 한다.
- `host`, `auth_ref` 는 필수다.
- DB를 선택했으면 `db_user`, `db_pw` 를 모두 입력해야 한다.

#### 6.4 UI 기본 동작
- 컴포넌트를 선택하면 기본 버전과 기본 인스턴스 수 1을 자동 적용한다.
- 프레임워크를 선택하면 기본 버전과 애플리케이션 인스턴스 수 1을 자동 적용한다.
- Java 언어를 선택하면 `infra_tech_stack.versions.java` 값은 언어 목록에서 파생한다.

---

## 7) RESTful API 목록
#### 7.1 Health Check
- `GET /health`
- 응답 JSON을 그대로 표시한다.

#### 7.2 Plan Check
- `POST /v1/supervisor/plan`
- RequestBody: `UserRequest` 형식 JSON
- 실패 시 UI는 오류 문자열을 표시한다.

#### 7.3 Chat Reply
- `POST /v1/chat`
- Request Body:

```json
{
  "messages": [
    {"role": "assistant", "content": "..."},
    {"role": "user", "content": "{...UserRequest json...}"}
  ]
}
```
- UI는 현재 입력한 `UserRequest` JSON 문자열을 새 user 메시지로 추가해 전송한다.
- 응답에서 `reply` 필드를 읽어 대화창에 표시한다.

---

## 8) Supervisor 출력 모델
#### 8.1 BuildPlan
- `summary`
- `missing_info`
- `missing_requirements`
- `steps`
- `graph`

#### 8.2 SupervisorRunResult
- `environment_summary`
- `executed`
- `generated_outputs`
- `recommended_config`
- `rollback_cleanup`
- `graph`
- `execution_path`
- `final_summary`