## Infra Auto Setting Agent

### 1. Role & Responsibility
- Supervisor가 전달한 `UserRequest`를 기준으로 인프라 bootstrap script를 생성하고 원격 서버에 적용한다.
- 현재 지원 컴포넌트 중심은 `apache`, `tomcat`, `kafka`, `pinpoint`다.
- 로그 디렉터리 생성 정책과 Java 런타임 정책을 스크립트에 강제한다.
- 스크립트 검증 후 SSH 실행 결과를 `InfraBuildRunResult`로 반환한다.

### 2. Input Context
이 Agent는 `SupervisorState["request"]`의 `UserRequest`를 사용한다.

주요 입력 필드:
- `infra_tech_stack.os`, `components`, `versions`, `instances`
- `targets[]`: `host`, `user`, `auth_ref`, `auth_method`, `ssh_port`, `os_type`
- `constraints.sudo_allowed`, `no_public_upload`, `network_policy`, `apache_config_mode`
- `logging.base_dir`, `logging.gc_log_dir`, `logging.app_log_dir`
- `app_tech_stack.framework`, `language`
- `additional_request`

추가 참고 데이터:
- `prior_executions`: 앞선 `sample_app` 실행 notes가 있으면 스크립트 주석 영역에 일부 반영할 수 있다.

### 3. Current Workflow
현재 구현의 처리 순서는 아래와 같다.
- 입력 기반 버전 보정
- 스크립트 계획/생성
- `execution_file_write`
- `code_validator`
- `ssh`
- 결과 패키징

그래프 기준 단계:
- `plan_script`
- `write_script`
- `validate_script`
- `run_remote`
- `finalize`

라우팅 규칙:
- 스크립트 검증 통과 시 `run_remote`
- 스크립트 검증 실패 시 원격 실행 없이 `finalize`

### 4. Version Resolution Policy
- `infra_tech_stack.versions`의 값은 `config.versions.VERSION_CATALOG`를 참고해 보정될 수 있다.
- major 또는 major.minor만 입력된 경우 동일 계열의 가장 높은 catalog 항목으로 치환한다.
- 보정이 발생하면 notes에 `VERSION_RESOLVED: {component} {source} -> {resolved}` 형식으로 남긴다.
- catalog가 없거나 숫자형 버전 패턴이 아니면 원본 값을 유지한다.

### 5. Script Generation Policy
스크립트는 기본적으로 bash 스크립트이며 아래 정책을 따른다.
- 항상 `#!/usr/bin/env bash`와 `set -euo pipefail`을 포함한다.
- 대상 OS 기준 패키지 관리자를 선택한다.
  - `Ubuntu`, `Debian` -> `apt`
  - `RHEL`, `Amazon Linux` 계열 -> `dnf`
- `constraints.sudo_allowed`가 `yes` 또는 `limited`면 설치/디렉터리 생성 명령에 `sudo`를 붙일 수 있다.
- 항상 로그 디렉터리 생성 블록을 추가한다.
- Java가 필요한 스택이면 요청된 Java major 버전을 강제로 설치/검증하는 블록을 추가한다.

컴포넌트별 기본 설치 흐름:
- `apache`
  - `apt`면 `apache2`, `dnf`면 `httpd`
  - 서비스 enable/start 수행
- `tomcat`
  - `tomcat` 패키지 설치 시도
  - 서비스 enable/start 시도
- `kafka`
  - `kafka` 패키지 설치 시도
- `pinpoint`
  - placeholder 성격의 설치 안내 echo를 남긴다.

현재 구현 특성:
- 생성 결과는 실제 서비스별 완전 자동구성보다 bootstrap 스크립트 중심이다.
- `apache_config_mode`, `network_policy`는 현재 입력 모델에 포함되지만 스크립트 생성에 깊게 반영되지는 않는다.

### 6. Java Runtime Policy
아래 조건 중 하나면 Java 설치 강제 로직을 넣는다.
- 컴포넌트에 `tomcat` 또는 `kafka` 포함
- 앱 프레임워크가 `spring` 또는 `spring boot`
- 앱 언어가 Java 계열

패키지명 규칙:
- `apt` -> `openjdk-{major}-jdk`
- `dnf` + Amazon Linux -> `java-{major}-amazon-corretto-devel`
- `dnf` + 기타 -> `java-{major}-openjdk-devel`

### 7. Validation Rules
`InfraTools.code_validator()`는 아래를 확인한다.
- `set -euo pipefail` 포함 여부
- 위험 명령 `rm -rf /` 존재 여부
- `sudo_allowed=no`인데 `sudo` 명령 포함 여부
- `logging.base_dir`가 스크립트에 참조되는지 여부

판정 규칙:
- `error` severity가 하나라도 있으면 `ok=false`
- `logging.base_dir` 누락은 현재 warning이다.

### 8. Remote Execution Policy
`InfraTools.ssh()` 동작 규칙:
- 대상 호스트는 항상 `targets[0]`을 사용한다.
- dry-run 환경변수 `INFRA_AGENT_DRY_RUN`이 활성화되면 원격 실행을 건너뛴다.
- 실제 실행은 로컬 스크립트를 SSH stdin으로 전달하는 방식이다.
- 기본 timeout은 900초다.

현재 구현의 오류 코드:
- `E_SSH_CONNECT`
- `E_SSH_TIMEOUT`
- `E_REMOTE_EXEC`

### 9. Output Model
이 Agent는 `InfraBuildRunResult`를 반환한다.

주요 필드:
- `execution: AgentExecution`
- `generated_outputs: list[str]`
- `recommended_config: list[str]`
- `rollback_cleanup: list[str]`
- `generated_files: list[InfraScriptArtifact]`
- `graph: GraphView`

`execution` 규칙:
- `agent`는 항상 `infra_build`
- `executed_commands`에는 `execution_file_write`, `code_validator`, `ssh` 요약 문자열이 들어간다.
- `notes`에는 버전 보정, SSH 모드, 대상 호스트, SSH 인증 방식, 실패 stderr/stdout 요약이 들어갈 수 있다.

### 10. Default Returned Lists
현재 코드의 기본 반환값:
- `generated_outputs`
  - `infra bootstrap script`
  - `component install report`
  - `log directory provisioning result`
- `recommended_config`
  - `Validate sudo scope before remote execution.`
  - `Keep GC logs and app logs in separate directories.`
  - `Pin component versions to avoid drift between reruns.`
- `rollback_cleanup`
  - `stop services: app/tomcat/kafka`
  - `restore changed config backups`
  - `remove temporary install artifacts`

검증 실패 시 축소 반환값:
- `generated_outputs`: `infra bootstrap script`
- `recommended_config`: `Fix validation errors and retry.`
- `rollback_cleanup`: `remove temporary install artifacts`

### 11. Failure Policy
- 스크립트 검증에 실패하면 원격 실행 없이 `success=False`로 종료한다.
- SSH 실행 실패 시 exit code, error code, stdout/stderr 일부를 notes에 남긴다.
- 예기치 않은 예외가 발생하면 `Unexpected infra build failure: ...` 형식으로 반환한다.
- 대상 호스트가 없거나 원격 실행이 불가하면 실패 결과를 반환한다.

### 12. Tool Chain
현재 구현의 툴 체인은 아래와 같다.
- `execution_file_write`: bootstrap script 기록
- `code_validator`: shell script 정적 검증
- `ssh`: 원격 서버 적용

권장 실행 순서:
- `execution_file_write -> code_validator -> ssh`
