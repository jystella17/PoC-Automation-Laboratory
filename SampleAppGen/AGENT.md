## Sample Application Generation Agent

### 1. Role & Responsibility
- Supervisor가 전달한 `UserRequest`를 기반으로 샘플 애플리케이션 스펙과 프로젝트 파일을 생성한다.
- 현재 지원 프레임워크는 `Spring Boot`, `Spring`, `FastAPI`다.
- 생성된 코드를 정적 검증하고, 필요 시 최대 2회까지 repair를 수행한다.
- 빌드 산출물과 Docker 이미지 생성/전송 결과를 `SampleAppRunResult`로 반환한다.

### 2. Input Context
이 Agent는 `SupervisorState["request"]`의 `UserRequest`를 그대로 사용한다.

주요 입력 필드:
- `app_tech_stack.framework`, `minor_version`, `build_system`, `language`
- `app_tech_stack.databases`, `db_user`, `db_pw`
- `logging.app_log_dir`, `logging.gc_log_dir`
- `topology.apps`
- `targets[]`: Docker 이미지 전송 대상 서버 정보
- `additional_request`: memory leak, OOM, board CRUD 등 추가 시나리오 힌트

추가 참고 데이터:
- `prior_executions`: 앞 단계 Agent notes를 참고할 수 있으나, 현재 구현의 핵심 흐름은 자체 계획과 fallback 템플릿 중심이다.

### 3. Current Workflow
현재 LangGraph는 아래 순서로 동작한다.
- `plan_spec`
- `generate_files`
- `validate_files`
- `repair_files`(필요 시, 최대 2회)
- `package_artifacts`
- `finalize`

라우팅 규칙:
- 검증 성공 시 `package_artifacts`로 이동
- 검증 실패 + repair budget 남음: `repair_files`로 이동
- 검증 실패 + repair budget 소진: `finalize`로 종료

### 4. Planning Rules
- 프레임워크가 `FastAPI`면 Python 계열 언어를 우선 선택하고 기본값은 `Python3.12`다.
- 프레임워크가 `Spring` 또는 `Spring Boot`면 Java 계열 언어를 우선 선택하고 기본값은 `Java17`이다.
- Java 계열의 빌드 시스템은 `app_tech_stack.build_system`을 우선 사용하고, `auto`면 기본값은 Maven이다.
- `additional_request`에서 아래 특수 시나리오를 감지한다.
  - `memory leak`, `threadlocal` -> `memory_leak`
  - `oom`, `out of memory` -> `oom`
- 산출 프로젝트는 저장소 하위 `generated_apps/{app_id}`에 생성된다.

### 5. File Generation Policy
- 항상 `APPLICATION_SPEC.md`를 먼저 생성한다.
- 프레임워크/빌드 시스템에 맞는 필수 파일을 보강한다.
- LLM 계획이 없거나 불완전하면 `plan_builder.py`의 fallback 계획과 템플릿을 사용한다.

현재 기본 파일 정책:
- FastAPI
  - `requirements.txt`
  - `app/main.py`
  - `Dockerfile`
  - `.env.example`
  - `README.md`
- Spring / Spring Boot
  - Maven: `pom.xml`
  - Gradle: `settings.gradle`, `build.gradle`, `gradlew`, `gradlew.bat`, `gradle/wrapper/gradle-wrapper.properties`
  - `src/main/resources/application.yml`
  - Spring Boot entrypoint Java file
  - `Dockerfile`
  - `.env.example`
  - `README.md`

추가 규칙:
- Board CRUD 관련 경로가 포함되면 board 템플릿 세트를 강제로 보강한다.
- FastAPI fallback 엔트리포인트는 `/health`를 생성한다.
- `memory_leak` 시나리오가 감지되면 FastAPI fallback 코드에 leak 엔드포인트를 주입한다.

### 6. Validation Rules
`SampleAppTools.code_validator()`는 아래를 검증한다.
- 계획된 파일이 실제로 생성됐는지 확인
- Python 파일 AST 파싱 가능 여부 확인
- FastAPI
  - `app/main.py` 존재 여부
  - `Dockerfile`에 `uvicorn` 포함 여부
- Spring / Spring Boot
  - `pom.xml` 또는 `build.gradle` 존재 여부
  - `@SpringBootApplication` + `main(String[] args)` + `SpringApplication.run(...)` 진입점 존재 여부
  - Spring entrypoint가 여러 개면 실패
  - `jakarta.validation` 사용 시 validation starter 의존성 존재 여부
  - board CRUD 파일 세트가 불완전하면 실패
  - `Dockerfile`은 `ENTRYPOINT ["java", "-jar", ...]` 형태여야 하며 `sh -c`를 쓰면 실패

### 7. Build & Packaging Policy
`package_artifacts` 단계에서는 아래 순서로 진행한다.
- `build_code`
- `docker_build`
- 결과 요약 후 `SampleAppRunResult` 반환

`build_code` 동작:
- Gradle wrapper가 있으면 `./gradlew clean bootJar --no-daemon -x test`
- `build.gradle`만 있고 로컬 gradle이 있으면 `gradle clean bootJar --no-daemon -x test`
- `build.gradle`만 있고 docker가 있으면 `gradle:8.14-jdk21` 컨테이너로 dockerized gradle build
- Maven wrapper가 있으면 `./mvnw -q -DskipTests package`
- `pom.xml`과 로컬 Maven이 있으면 `mvn -q -DskipTests package`
- 빌드 후 `build/libs/*.jar` 또는 `target/*.jar`에서 패키징 결과를 찾는다.

`docker_build` 동작:
- 대상 서버가 없으면 실패
- 현재 구현상 Docker 전송은 `auth_method=pem_path`만 지원한다.
- dry-run 환경변수 `SAMPLE_APP_AGENT_DRY_RUN`이 설정되면 실제 빌드/전송을 건너뛴다.
- 실제 전송은 `docker build` 후 `docker image save | ssh docker load` 방식으로 수행한다.

### 8. Output Model
이 Agent는 `SampleAppRunResult`를 반환한다.

주요 필드:
- `execution: AgentExecution`
- `generated_outputs: list[str]`
- `recommended_config: list[str]`
- `rollback_cleanup: list[str]`
- `spec_markdown: str`
- `generated_files: list[GeneratedFile]`
- `graph: GraphView`

`execution` 규칙:
- `agent`는 항상 `sample_app`
- `success`는 최종 패키징 단계 성공 여부를 따른다.
- `executed_commands`에는 `execution_file_write`, `code_validator`, `build_code`, `docker_build` 요약 문자열이 들어간다.
- `notes`에는 build/docker 오류, 배포 명령, 이미지 참조, fallback 여부가 기록될 수 있다.

### 9. Generated Outputs Conventions
현재 코드 기준 대표 산출물:
- `application spec: {APPLICATION_SPEC.md path}`
- `sample app source: {project_dir}`
- `application jar: {artifact_path}`
- `container image: {image_ref}`
- `배포 가이드라인 및 API 문서`

권장 설정 예시:
- `APP_LOG_DIR=...`
- DB 사용 시 `APP_DB_HOST`, `APP_DB_PORT`, `APP_DB_NAME`, `APP_DB_USER`, `APP_DB_PASSWORD`
- Java 앱이면 `JAVA_TOOL_OPTIONS=-Xlog:gc*:file=...`

### 10. Failure Policy
- 프레임워크가 `none`이거나 `topology.apps <= 0`이면 성공으로 처리하되 앱 생성을 skip한다.
- 예기치 않은 예외가 발생하면 `success=False`와 함께 오류 요약을 notes에 기록한다.
- 정적 검증 실패가 repair budget 내에서 복구되지 않으면 `success=False`로 종료할 수 있다.
- Docker 전송 실패 시 `DOCKER_UPLOAD_ERROR`, `DOCKER_UPLOAD_STDERR`를 notes에 남긴다.

### 11. Tool Chain
현재 구현의 툴 체인은 아래와 같다.
- `execution_file_write`: 프로젝트 파일 생성
- `code_validator`: 생성 코드 정적 검증
- `build_code`: jar 산출물 패키징
- `docker_build`: Docker 이미지 생성 및 대상 서버 적재

권장 실행 순서:
- `execution_file_write -> code_validator -> build_code -> docker_build`
