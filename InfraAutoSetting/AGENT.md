## Infra Auto Setting Agent

### 1. Role & Responsibility
- Supervisor가 전달한 `UserRequest`를 기준으로 인프라 bootstrap script를 생성하고 원격 서버에 적용한다.
- 현재 지원 컴포넌트: `apache`, `tomcat`, `kafka`, `pinpoint`, `java`
- 로그 디렉터리 생성 정책을 fallback 스크립트에 강제하고, LLM 생성 스크립트도 검증 단계에서 로그 경로 누락 여부를 점검한다.
- 동일 요청에 대해 Redis 기반 스크립트 캐싱을 통해 LLM 재호출을 방지한다.
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
- `prior_executions`: 앞선 `sample_app` 실행 notes가 있으면 스크립트 주석 영역에 일부 반영한다.

### 3. Current Workflow
처리 순서:

1. **버전 보정** (`_resolve_versions`): `VERSION_CATALOG` 기준으로 major-only 입력을 full 버전으로 치환
2. **캐시 조회** (`_resolve_script`): Redis에서 동일 요청 캐시 확인
   - 캐시 히트 → 저장된 스크립트를 내용 검증 후 재사용 (`SCRIPT_CACHE_HIT`)
   - 캐시된 스크립트가 현재 검증 기준에 맞지 않으면 무효화하고 새로 생성
   - 캐시 미스 → 3번으로 진행
3. **스크립트 생성** (`_build_script`):
   - deterministic fallback 스크립트를 먼저 구성
   - LLM 호출 우선 (`InfraScriptGeneratorLLM.generate_install_script`)
   - LLM 실패(None 반환) 시 fallback 스크립트 사용
   - LLM 응답이 있으면 최대 2회까지 validate/repair loop 수행
   - 최종 검증 실패 시 fallback 스크립트 사용
4. **캐시 저장**: 생성 스크립트를 Redis에 TTL과 함께 저장 (`SCRIPT_CACHE_MISS`)
5. **`execution_file_write`**: 스크립트 파일 기록
6. **`code_validator`**: 정적 검증
7. **`ssh`**: 원격 적용
8. **결과 패키징** → `InfraBuildRunResult` 반환

그래프 기준 단계:
- `plan_script` → `write_script` → `validate_script` → `run_remote` → `finalize`

라우팅 규칙:
- 스크립트 검증 통과 시 `run_remote`
- 스크립트 검증 실패 시 원격 실행 없이 `finalize`

### 4. Version Resolution Policy
- `infra_tech_stack.versions`의 값은 `config.versions.VERSION_CATALOG`를 참고해 보정될 수 있다.
- major 또는 major.minor만 입력된 경우 동일 계열의 가장 높은 catalog 항목으로 치환한다.
- 보정이 발생하면 notes에 `VERSION_RESOLVED: {component} {source} -> {resolved}` 형식으로 남긴다.
- catalog가 없거나 숫자형 버전 패턴이 아니면 원본 값을 유지한다.

### 5. Script Generation Policy
스크립트는 bash 스크립트이며 아래 정책을 따른다.

**기본 구조:**
- 항상 `#!/usr/bin/env bash`와 `set -euo pipefail` 포함
- 대상 OS 기준 패키지 관리자 선택:
  - `Ubuntu`, `Debian` → `apt`
  - `RHEL`, `Amazon Linux`, `CentOS`, `Rocky Linux`, `Fedora`, `AlmaLinux` 계열 → `dnf`
  - target 기준 판별이 안 되면 `infra_tech_stack.os == linux`일 때 `apt`, 그 외는 `unknown`
- `constraints.sudo_allowed`가 `yes` 또는 `limited`면 설치/디렉터리 명령에 `sudo` 사용
- fallback 스크립트는 항상 로그 디렉터리 생성 블록 포함
- LLM 스크립트는 로그 디렉터리 및 각 컴포넌트 반영 여부를 별도 검증한다

**LLM vs Fallback:**
- LLM이 유효한 스크립트를 반환하면 LLM 결과를 사용
- LLM 미응답/실패 시 deterministic fallback 스크립트 사용
- LLM 응답은 bash syntax, 컴포넌트 반영, 로그 경로 반영, self-referential copy, 보호 경로 write-sudo 규칙으로 검증
- 검증 실패 시 LLM repair를 최대 2회 시도한 뒤, 계속 실패하면 fallback 스크립트로 전환

**컴포넌트별 설치 흐름:**

- `apache`
  - `apt`: `apache2={version}*` 버전 핀 시도 → 실패 시 `apache2` fallback
  - `dnf`: `httpd-{version}*` 버전 핀 시도 → 실패 시 `httpd` fallback
  - 설치 후 `systemctl enable --now {service}`
- `tomcat`
  - major 버전 기반 패키지명 사용: `tomcat10`, `tomcat9` 등
  - `apt`: `tomcat{major}` 직접 설치
  - `dnf`: `tomcat{major}` 시도 → 실패 시 `tomcat` fallback
  - 설치 후 `systemctl enable --now tomcat{major}`
- `kafka`
  - `apt`: `kafka={version}*` 버전 핀 시도 → 실패 시 `kafka` fallback
  - `dnf`: `kafka-{version}*` 버전 핀 시도 → 실패 시 `kafka` fallback
- `java`
  - `java` 컴포넌트가 명시적으로 요청된 경우에만 패키지 설치 및 `java -version` major 검증 블록 생성
  - `apt`: `openjdk-{major}-jdk`
  - `dnf` + Amazon Linux → `java-{major}-amazon-corretto-devel`
  - `dnf` + 기타 → `java-{major}-openjdk-devel`
- `pinpoint`
  - placeholder 성격의 echo 안내만 출력

### 6. Java Runtime Policy
현재 코드 기준으로 `_requires_java()` 헬퍼는 존재하지만 실행 경로에서 사용되지 않는다.

- fallback 스크립트에서 Java 설치/검증은 `java` 컴포넌트가 요청에 포함된 경우에만 수행된다.
- `tomcat`, `kafka`, `spring`, Java 언어 앱이라는 이유만으로 별도 Java 설치 블록을 자동 추가하지는 않는다.
- LLM은 프롬프트 상 Java 정책을 참고할 수 있지만, 강제 보장은 검증 로직으로 구현되어 있지 않다.

### 7. Script Caching Policy
Redis 기반 캐시를 사용해 동일 요청에 대한 LLM 재호출을 방지한다.

**캐시 키 구조 (human-readable concatenation):**
```
{package_manager}:{sudo_allowed}:{sorted_components}:{sorted_versions}:{log_dirs}
```
예시:
```
apt:yes:apache,tomcat:apache=2.4.66,tomcat=10.1.36:/var/log/base|/var/log/gc|/var/log/app
```

Redis 저장 키: `infra:script_cache:{위 캐시 키}`

**정책:**
- TTL: 7일 (Redis `SETEX` 네이티브 TTL로 관리)
- 캐시 히트: `SCRIPT_CACHE_HIT: {key_prefix}` notes 기록
- 캐시 미스: `SCRIPT_CACHE_MISS: {key_prefix}` notes 기록
- 캐시 히트 스크립트도 현재 검증 규칙으로 재검사하며, 검증 실패 시 재생성 후 다시 저장한다
- Redis 연결 실패 시 캐시 miss로 처리하고 에이전트는 정상 동작 유지
- Redis 연결은 `runtime_bus.create_redis()` 경유로 생성한다

**캐시 키 정규화:**
- components: lowercase, strip, 정렬
- versions: key 기준 정렬
- package_manager, sudo_allowed는 그대로 사용

### 8. Validation Rules
`InfraTools.code_validator()`는 아래를 확인한다:
- `set -euo pipefail` 포함 여부
- 위험 명령 `rm -rf /` 존재 여부
- `sudo_allowed=no`인데 `sudo` 명령 포함 여부 (multiline 전체 스캔)
- self-referential recursive copy 여부 (`cp -r src src/...`)
- `bash -n` 문법 검사
- `logging.base_dir`가 스크립트에 참조되는지 여부

판정 규칙:
- `error` severity가 하나라도 있으면 `ok=false`
- `logging.base_dir` 누락은 `warning` (실행은 계속)

추가로 스크립트 생성/캐시 검증 단계의 `_validate_script_content()`는 아래도 확인한다:
- 요청된 각 컴포넌트가 스크립트에서 실제로 다뤄지는지
- `logging.base_dir`, `gc_log_dir`, `app_log_dir` 모두가 참조되는지
- `sudo_allowed != no`인 상황에서 `/var`, `/etc`, `/usr`, `/opt` 하위 보호 경로 write 명령에 `sudo`가 빠지지 않았는지

### 9. Remote Execution Policy
`InfraTools.ssh()` 동작 규칙:
- 대상 호스트는 항상 `targets[0]`을 사용한다.
- `INFRA_AGENT_DRY_RUN` 환경변수가 활성화되면 원격 실행을 건너뛴다.
- 실제 실행은 로컬 스크립트를 SSH stdin으로 전달하는 방식이다.
- 기본 timeout: 900초

오류 코드:
- `E_SSH_CONNECT`: 호스트 없음 또는 연결 실패
- `E_SSH_TIMEOUT`: 실행 타임아웃
- `E_REMOTE_EXEC`: 원격 명령 비정상 종료

### 10. Output Model
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
- `executed_commands`에는 `execution_file_write`, `code_validator`, `ssh` 요약 문자열 포함
- `notes`에 포함 가능한 항목: 버전 보정, 캐시 상태, SSH 모드, 대상 호스트, SSH 인증 방식, SSH 포트, 실패 stderr/stdout 요약 (각 최대 2000자)

### 11. Default Returned Lists
정상 실행 시 기본 반환값:
- `generated_outputs`: `infra bootstrap script`, `component install report`, `log directory provisioning result`
- `recommended_config`: `Validate sudo scope before remote execution.`, `Keep GC logs and app logs in separate directories.`, `Pin component versions to avoid drift between reruns.`
- `rollback_cleanup`: `stop services: app/tomcat/kafka`, `restore changed config backups`, `remove temporary install artifacts`

검증 실패 시 축소 반환값:
- `generated_outputs`: `infra bootstrap script`
- `recommended_config`: `Fix validation errors and retry.`
- `rollback_cleanup`: `remove temporary install artifacts`

### 12. Failure Policy
- 스크립트 검증 실패 시 원격 실행 없이 `success=False` 종료
- SSH 실행 실패 시 exit code, error code, stdout/stderr 일부를 notes에 기록
- 예기치 않은 예외 발생 시 `Unexpected infra build failure: ...` 형식으로 반환
- 대상 호스트가 없으면 `E_SSH_CONNECT` 오류로 실패 결과 반환

### 13. Tool Chain
```
execution_file_write → code_validator → ssh
```

- `execution_file_write`: bootstrap script 파일 기록 (chmod 0755)
- `code_validator`: shell script 정적 검증
- `ssh`: 원격 서버 stdin 실행
