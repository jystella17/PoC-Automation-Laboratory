# Infra Test Lab
인프라 운영자를 위한 테스트 환경 구축/스펙 검증 플랫폼.
Supervisor Agent가 사용자 요구를 구조화하고, Infra/App 에이전트를 오케스트레이션한다.

---

## 0) Objectives (System Goals)
- 입력된 기술스택/테스트 시나리오에 맞춰:
  1) 테스트 환경을 안전하게 구축하고
  2) 샘플 애플리케이션을 배포/실행하며
  3) 로그/메트릭을 수집한다.
  
**Non-Goals**
- 운영(Production) 환경에 직접 적용/변경하지 않는다.
- DB/타 부서 소관 스택은 Out of Scope.

---

## 1) Agents
* LangGraph 기반 구현, Supervisor Agent - Sample App / Infra Build Agent는 A2A 통신 필요

### 1.1 Supervisor Agent
**Role**
- 사용자 요구를 구조화(스택/목적/부하/대상서버/제약)하고 작업 계획을 만든다.
- Sub-agent를 순차/병렬 호출하고 결과를 합쳐 사용자에게 작업 결과를 알려준다.

**Tone**
- 꼼꼼한 PM. 누락 정보가 있으면 실행 전에 질문한다.

**Failure Policy**
- 실패 시: 원인(명령/로그/환경/권한)을 요약하고, 사용자가 취할 액션을 제시한다.

---

### 1.2 Infra Build Agent
**Role**
- Apache/Tomcat/Kafka/Pinpoint 등 설치 및 초기구성.
- 부하 테스트 도구(JMeter 등) 설치/실행 보조.
- 로그/메트릭 수집 경로 설정 및 수집 수행.

**Safety / Guardrails**
- sudo는 최소 사용(필요 이유를 명시).
- 실행 전 “생성된 스크립트 전문”을 사용자에게 제공하고 승인(ACK) 후 실행한다.
- 민감정보(키/토큰/비번)는 출력에 마스킹한다.

---

### 1.3 Sample App Agent
**Role**
- 요구사항 기반 샘플 앱 생성 (예: Spring/JSP/REST, OOM/Leak 재현).
- 빌드 산출물(WAR/JAR/Docker image) 생성.
- 배포 스펙(포트/환경변수/리소스 제한) 정의.
- 애플리케이션 생성을 위한 기본 포맷 (API 문서, DB 스키마, 코드 컨벤션 등 - CLAUDE.md, AGENT.md 등에 포함되는 내용들)은 사전에 작성 -> 기술 스택 / Memory Leak 발생 등 사용자 요구사항을 md 문서에 포함 -> md 문서 기반 애플리케이션 코드 생성
- DB 등 애플리케이션 동작을 위한 추가 소프트웨어 설치/Docker Image 등이 필요한 경우, 

**Constraints**
- 앱 생성 전 필수 입력(DB 계정, Port 등)이 없으면 질문 후 진행.

**Mechanism**
사전 준비된 AGENT.md (샘플 애플리케이션의 API 문서, DB Schema, 간단한 기능 설명 등) -> 사용자 입력 및 요구사항을 기반으로 AGENT.md 문서 완성 ->
AGENT.md 문서로 코드 생성 -> 코드 build (Spring/Spring Boot/JSP라면 .jar 파일 등) -> (Docker 기반 배포하는 경우) DockerFile 생성 및 Dockerize -> Agent 배포 서버에서 접근 가능한 S3/ECR 등에 Push
-> 애플리케이션 배포 대상 서버에서 애플리케이션 & DB 등 필요 SW 이미지/실행 파일 Pull -> 애플리케이션 실행 (docker run / java -jar 등)

```
고민 사항
=> 애플리케이션 배포 대상 서버에서 이미지/실행 파일을 다운로드 받는 작업에 SSH 명령어 필요 
=> SSH MCP Server를 Infra/App Agent에서 모두 사용할 것인지, SSH 명령어 수행은 Infra Agent에서만 수행하도록 하고 다른 Agent에서 SSH 명령어 수행이 필요한 경우 A2A로 Infra Agent를 호출해 MCP Server에 접근할 것인지?
```
---

## 2) Shared Data Contract (Pydantic-friendly JSON)
모든 에이전트는 아래 스키마를 입력/출력 기준으로 사용한다.

### 2.1 UserRequest (Supervisor input parsing result)
```json
{
  "infra_tech_stack": {
    "os": "linux",
    "components": ["tomcat", "apache", "kafka", "pinpoint"],
    "versions": {
      "tomcat": "10.x",
      "kafka": "3.5.x",
      "java": "17|21",
      "spring": "6.x"
    }
  },
  "load_profile": {
    "tps": 5000,
    "payload_bytes": 1024,
    "duration_sec": 600,
    "concurrency": 200
  },
  "topology": {
    "nodes": 2,
    "apps": 4
  },
  "constraints": {
    "no_public_upload": true,
    "security_policy_notes": ["..."],
    "sudo_allowed": "yes|no|limited"
  },
  "targets": [
    {
      "host": "x.x.x.x",
      "user": "ec2-user",
      "auth_ref": "ssh_key_id_or_secret_ref",
      "os_type": "rhel9|ubuntu22.0.4|..."
    }
  ],
  "logging": {
    "base_dir": "/var/log/infra-test-lab",
    "gc_log_dir": "/var/log/infra-test-lab/gc",
    "app_log_dir": "/var/log/infra-test-lab/app"
  },
  "app_tech_stack": {
    "framework": "Spring6|Spring Boot3|FastAPI|...",
    "minor_version": "latest(default)|3.0.5|...",
    "language": "Java17|Java21|Python3.12|...",
    "databases": "MySQL|PostgreSQL|MariaDB|Redis|MongoDB|...",
    "db_user": "databases[0] : aaa, databases[1] : bbb, ...",
    "db_pw": "databases[0] : abcde, databases[1] : hijklm, ..."
  }
}
```
---
## 3) Workflow (LangGraph Nodes)
### Nodes
* plan: 요구사항 파싱/누락 체크/BuildPlan 생성
* build_infra: 설치/환경 구성
* generate_app: 샘플 앱 생성/빌드/배포

### Edges
* plan -> build_infra
* plan -> generate_app (병렬 가능, 단 infra 준비 의존 시 대기)
* any *_failed -> plan (사용자 질문/수정 루프)
* generate_app -> build_infra? ()

### Missing-info Gate
plan 노드는 아래 필수 정보가 없으면 절대 실행 단계로 넘어가지 않는다:
* 대상 호스트/접속 방식(SSH)
* 설치 대상 스택/버전
* 로그 디렉토리/권한 정책

## 4) Tooling (Function Calling Spec)
* 실제 구현 시 이름/스키마는 그대로 유지. (Supervisor는 결과를 표준 포맷으로 저장)

#### ssh_execute_command
* input: { "host": str, "user": str, "auth_ref": str, "cmd": str, "os_type": str } (auth_ref : 계정 비밀번호, pem key 경로 등)
* output: { "stdout": str, "stderr": str, "exit_code": int }

#### generate_docker_compose
* input: { "services": [...], "networks": [...], "volumes": [...] }
* output: { "yaml": str }

#### fetch_system_logs
* input: { "host": str, "user": str, "auth_ref": str, "path": str, "conditions": str } (conditions : 상위 n줄 - head, 마지막 n줄 - tail, 특정 시간대/ip/Status Code/지연 시간 n초 이상 등 - awk, grep 등)
* output: { "log_content": str }

#### upload_artifact (optional, internal only)
* input: { "type": "script|report|compose|war|jar", "name": str, "content_ref": str }
* output: { "artifact_id": str }

## 5) Output Requirements (User-visible)
최종 응답은 아래 섹션을 포함해야 한다.

1. Environment Summary (구성/버전/접속정보 마스킹)
2. What was executed (스크립트/명령 목록)
3. 요청에 따라 생성한 코드(API 목록 + Memory Leak 등 특이사항) 및 인프라 구축 결과(스펙)
4. Recommended Config (적용 가능한 옵션 세트)
5. Rollback / Cleanup (리소스 정리 명령)

## 6) Security & Compliance
* 비밀번호/키/토큰 등은 절대 로그/리포트에 평문으로 남기지 않는다(마스킹).
* 운영 서버/운영 계정에 대한 명령 실행은 기본 차단(사용자가 “테스트 서버”임을 명시해야 진행).
* 외부 퍼블릭 업로드 금지(도커 이미지/리포트 포함).
* 실행 전 승인(ACK) 절차 필수(Infra Build Agent).

## 7) Scenario Templates (Minimal)
### SC-001 Kafka Benchmark Template
* inputs: kafka_version, nodes, partitions 후보, replication_factor 후보, payload_bytes, tps, duration
* outputs: throughput/latency + cpu/mem + recommended partitions/replication

### SC-002 Tomcat JVM/GC Compare Template
* inputs: tomcat_version, java_version, heap_range, gc_algorithms, leak/oom mode, load profile
* outputs: avg/max pause, full gc count, oom point, recommended JAVA_OPTS
