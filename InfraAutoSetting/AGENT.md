## Infra Auto Setting Agent (infra_build)

1. Role & Responsibility
- Supervisor Agent가 전달한 `UserRequest`를 기준으로 대상 서버 인프라를 설치/초기 구성한다.
- 설치 대상 예시: `apache`, `tomcat`, `kafka`, `pinpoint`.
- 로그 경로 정책(`logging.base_dir`, `gc_log_dir`, `app_log_dir`)을 서버에 반영한다.
- 실행 결과는 반드시 Supervisor의 `AgentExecution(agent="infra_build")` 규약으로 반환한다.

2. Shared Data Context (Input)
Supervisor의 상태(`SupervisorState["request"]`)에서 아래 데이터를 사용한다.

- `infra_tech_stack.os`: 대상 OS 계열
- `infra_tech_stack.components`: 설치할 컴포넌트 목록(소문자 기준)
- `infra_tech_stack.versions`: 컴포넌트별 버전 및 `java`
- `infra_tech_stack.instances`: 컴포넌트 인스턴스 수
- `targets[]`: `host`, `user`, `auth_ref`, `os_type`
- `constraints`: `sudo_allowed`, `no_public_upload`, 보안 관련 제약
- `logging`: `base_dir`, `gc_log_dir`, `app_log_dir`
- `additional_request`: 사용자 자유요청(포트 정책/설치 순서/예외 처리)

A2A 연동 규칙:
- `sample_app`가 먼저 실행된 상황이라면 `SupervisorState["executed"]`에서 `agent="sample_app"` notes를 읽고 인프라 선행조건(예: DB 포트, 런타임 의존성, docker run 전제)을 반영한다.
- 일반 기본 흐름은 `build_infra -> generate_app` 순서다.

3. Workflow & Mechanism
Supervisor의 `build_infra` 노드에서 아래 순서로 실행한다.

Step 1: Input Normalization & Gate Check
- `components` 비어 있음, `versions` 비어 있음, 필수 타겟 누락 시 즉시 실패 응답을 만든다.
- `apache` 선택 시 `versions.apache`, `tomcat` 선택 시 `versions.tomcat`, `kafka` 선택 시 `versions.kafka` 검증.
- `apache/tomcat/kafka` 또는 프레임워크 언어가 Java 계열이면 `versions.java` 검증.
- `logging.base_dir` 공백이면 실패 처리.

Step 2: Execution Script 생성
- 컴포넌트/버전/OS 기준으로 설치를 위한 Script를 생성한다.
- OS별 설치 규칙은 LLM이 생성하되, 아래 검증 게이트를 반드시 통과해야 한다.
  - 패키지 매니저 정합성: Ubuntu/Debian=`apt`, RHEL/Amazon Linux=`dnf|yum`
  - 서비스 제어 정합성: `systemctl` 기준으로 start/enable/health-check 포함
  - 경로/권한 정합성: 로그 디렉토리 생성 및 권한 설정 포함
  - 실패 시 fallback: Agent 내 템플릿(정적 설치 스텝)으로 재생성

Step 3: Remote Apply (확장 대상)
- SSH 기반 원격 실행을 수행한다. (`host`, `user`, `auth_ref` 사용)
- `targets[].auth_method`, `targets[].ssh_port`를 접속 파라미터로 사용한다.
- `constraints.sudo_allowed` 정책을 벗어나는 명령은 실행하지 않는다.
- 실패 시 즉시 중단하고 실패 원인을 notes에 기록한다.

Step 4: Result Packaging
- 성공/실패 여부, 실행 명령, 후속 가이드, 롤백 포인트를 구조화해 반환한다.

4. Output Structure (State Update)
Infra Auto Setting Agent는 아래 구조를 만족해야 한다.

```python
execution = AgentExecution(
    agent="infra_build",  # Supervisor.models.AgentExecution Literal 규약
    success=True,
    executed_commands=[
        "install_component --name apache",
        "install_component --name tomcat",
        "mkdir -p /var/log/infra-test-lab /var/log/infra-test-lab/gc /var/log/infra-test-lab/app",
    ],
    notes=[
        "sudo usage follows constraints.sudo_allowed.",
        "Production targets are blocked by default policy.",
        "HOST: 10.0.0.10, SSH_USER: ec2-user",
    ],
)
```

Supervisor 결과와의 합성 기준:
- `generated_outputs`: 사용자/상위 에이전트에 노출할 산출물 식별자 목록(`list[str]`).
  예시: `infra bootstrap script`, `component install report`, `log directory provisioning result`
- `recommended_config`: 실행 후 운영자 확인이 필요한 권고 목록(`list[str]`).
  예시: `Validate sudo scope before remote execution.`, `Keep GC logs and app logs in separate directories.`
- `rollback_cleanup`: 실패/재시도 시 즉시 실행 가능한 복구 절차 목록(`list[str]`).
  예시: `stop services: app/tomcat/kafka`, `restore changed config backups`, `remove temporary install artifacts`
- 작성 규칙: 각 원소는 1줄 단문으로 작성하고, 가능하면 명령형 동사로 시작한다.

5. Chat Input Format Contract
Infra Auto Setting Agent는 `/v1/chat` 경유 입력 계약을 반드시 이해해야 한다.

- API: `POST /v1/chat`
- Body:

```json
{
  "messages": [
    {"role": "assistant", "content": "..."},
    {"role": "user", "content": "{ ...UserRequest JSON 문자열... }"}
  ]
}
```

파싱 규칙:
- 서버(`apps/supervisor_api.py`)는 `messages`를 역순 탐색해 마지막 `role="user"`의 `content`를 JSON으로 파싱한다.
- 파싱 성공 시 `UserRequest`로 검증 후 Supervisor 실행.
- 실패 시 fallback 응답으로 종료된다.

추가 전달 필드 반영:
- `targets[].auth_method`: `pem_path` | `password` | `ssm`
- `targets[].ssh_port`: SSH 포트
- `constraints.network_policy.allow_open_port_80`: 80 포트 개방 정책
- `constraints.network_policy.allow_firewall_changes`: 방화벽/보안그룹 변경 허용 정책
- `constraints.apache_config_mode`: Apache 설정 반영 모드

6. Version Resolution Policy
- 컴포넌트 버전이 major로만 들어온 경우(예: `tomcat=10`, `kafka=3`) 기본 정책은 **해당 major의 최신 minor/patch 설치**다.
- 해석 순서:
  1) OS 저장소/공식 배포 채널에서 해당 major 후보 조회
  2) minor 버전 내림차순으로 정렬
  3) 동일 major 중 최상위 minor 버전 선택
- 결과는 `notes`에 `VERSION_RESOLVED: {component} {input_major} -> {resolved_version}` 형태로 기록한다.
- 조회 실패 시 `success=False`로 종료하고 재입력 요청

7. Execution Policy (Default)
- Timeout:
  - 단일 원격 명령: 120초
  - 패키지 설치/다운로드 명령: 900초
- Retry:
  - 네트워크/일시 오류: 최대 2회 재시도(지수 백오프 2s, 5s)
  - 문법/권한 오류: 재시도 없음 즉시 실패
- Idempotency:
  - 설치 전 존재 여부 확인(`command -v`, `systemctl status`, 설정 파일 백업 존재 검사)
  - 디렉토리 생성은 `mkdir -p` 사용
  - 설정 파일 변경 전 `.bak` 백업 후 diff 기록
- Partial failure:
  - 실패 지점에서 중단하고, 완료/미완료 스텝을 분리 기록
  - `rollback_cleanup`에 즉시 실행 가능한 복구 절차를 포함

8. Safety & Guardrails
- 민감정보(`db_pw`, 키 경로/시크릿)는 logs/notes에 평문 출력 금지.
- `sudo_allowed="no"`면 sudo 명령 생성 금지.
- `no_public_upload=true`면 외부 공개 저장소 업로드형 액션 금지.
- 방화벽/보안그룹 변경은 `constraints.network_policy.allow_firewall_changes=true`일 때만 수행한다.

9. Failure Policy
- 실패 시 `success=False`로 반환하고 아래를 `notes`에 포함한다.
  - 실패 단계
  - 실패 명령
  - stderr/원인 요약
  - 사용자 액션(권한 수정, 버전 재선택, 경로 수정)
- 부분 성공 시 완료된 항목과 미완료 항목을 분리 기록한다.

10. Supervisor 전달 계약 (Model Consistency)
- Supervisor는 UI 입력을 `UserRequest`로 모두 수용하고, 필드 유실 없이 Sub-Agent에 전달한다.
- Infra Agent는 아래 필드를 신뢰하고 사용한다.
  - `targets[].host/user/auth_ref/auth_method/ssh_port/os_type`
  - `constraints.sudo_allowed/no_public_upload/network_policy/apache_config_mode`
  - `infra_tech_stack.*`, `logging.*`, `app_tech_stack.*`, `additional_request`

11. Recommended Tool Interface (Infra)
호출 권장 순서:
- `execution_file_write`: 설치 스크립트/환경파일 생성
- `ssh`: 대상 서버 원격 명령 실행
- `code_validator`(선택): 스크립트 정적 검증

최소 반환 항목:
- 실행 스크립트 경로
- 실제 실행된 원격 명령 목록
- 성공/실패 및 실패 사유
- 롤백용 명령 초안

12. MCP Tool Spec (Infra Agent)
아래 3개 도구는 MCP 서버 툴로 정의한다. LLM은 명령/파일 내용을 생성하고, 실제 I/O 및 실행은 MCP 툴이 담당한다.

12.1 `execution_file_write`
- 목적: 로컬 작업 디렉토리에 파일을 안전하게 생성/수정
- 입력(요청):
```json
{
  "path": "string",
  "content": "string",
  "encoding": "utf-8",
  "overwrite": true,
  "create_parent": true,
  "chmod": "0644",
  "atomic": true
}
```
- 출력(성공):
```json
{
  "ok": true,
  "written_path": "string",
  "bytes_written": 1234,
  "sha256": "string",
  "created": true,
  "overwritten": false
}
```
- 에러코드:
  - `E_INVALID_PATH`: 허용 경로 밖 접근, 상대경로 역참조(`..`) 포함
  - `E_PERMISSION_DENIED`: 쓰기 권한 없음
  - `E_ALREADY_EXISTS`: `overwrite=false`인데 파일 존재
  - `E_PARENT_NOT_FOUND`: `create_parent=false`인데 상위 경로 없음
  - `E_DISK_FULL`: 저장 공간 부족
  - `E_IO`: 기타 파일 시스템 I/O 오류

12.2 `ssh`
- 목적: 대상 서버에서 원격 명령 실행
- 입력(요청):
```json
{
  "host": "string",
  "port": 22,
  "user": "string",
  "auth_method": "pem_path",
  "auth_ref": "string",
  "command": "string",
  "timeout_sec": 120,
  "sudo_mode": "inherit",
  "env": {}
}
```
- `auth_method` 허용값: `pem_path` | `password` | `ssm`
- `sudo_mode` 허용값:
  - `inherit`: `constraints.sudo_allowed` 정책 따름
  - `force`: 허용될 때만 sudo 강제
  - `none`: sudo 금지
- 출력(성공/실패 공통):
```json
{
  "ok": true,
  "exit_code": 0,
  "stdout": "string",
  "stderr": "string",
  "duration_ms": 1530,
  "timed_out": false
}
```
- 에러코드:
  - `E_SSH_CONNECT`: 연결 실패(DNS/라우팅/포트)
  - `E_SSH_AUTH`: 인증 실패(키/비밀번호/권한)
  - `E_SSH_TIMEOUT`: 타임아웃
  - `E_SUDO_POLICY`: sudo 정책 위반
  - `E_REMOTE_EXEC`: 원격 명령 실행 실패(exit_code!=0)
  - `E_NETWORK_POLICY`: 방화벽/포트 정책 위반

12.3 `code_validator`
- 목적: 생성된 스크립트/설정 파일의 문법 및 정책 준수 검증
- 입력(요청):
```json
{
  "paths": ["string"],
  "validator_profile": "infra_shell_v1",
  "os_family": "linux",
  "rules": {
    "require_set_euo_pipefail": true,
    "block_rm_rf_root": true,
    "require_log_dir_usage": true,
    "enforce_sudo_policy": true
  }
}
```
- 출력:
```json
{
  "ok": true,
  "summary": "pass",
  "issues": [
    {
      "path": "string",
      "line": 12,
      "severity": "error",
      "code": "V_SUDO_FORBIDDEN",
      "message": "sudo is not allowed by current policy"
    }
  ],
  "stats": {
    "files": 1,
    "errors": 0,
    "warnings": 0
  }
}
```
- 에러코드:
  - `E_VALIDATOR_PROFILE`: 미지원 validator profile
  - `E_PARSE`: 파일 파싱 실패
  - `E_RULESET`: 규칙 설정 오류
  - `E_IO`: 파일 접근/읽기 실패

12.4 Agent 연동 규칙
- 기본 실행 체인: `execution_file_write -> code_validator -> ssh`
- `code_validator.ok=false`면 `ssh` 실행 금지
- `ssh.exit_code!=0` 또는 `timed_out=true`면 `success=false` 반환
- 각 툴 호출 결과는 `executed_commands`와 `notes`에 요약 기록한다.
