## Sample Application Generation Agent

### 1. Role & Responsibility
- Supervisor가 전달한 `UserRequest`를 기반으로 샘플 애플리케이션 스펙과 프로젝트 파일을 생성한다.
- 현재 지원 프레임워크: `Spring Boot`, `Spring`, `FastAPI`
- 각 파일은 **Template → LLM → fallback template** 3단계 우선순위로 생성된다.
- 생성된 코드를 정적 검증하고, 필요 시 최대 2회까지 repair를 수행한다.
- 빌드 실패 시에도 repair budget이 남아 있으면 repair 단계로 재진입한다.
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
- `prior_executions`: 앞 단계 Agent notes를 참고할 수 있으나, 핵심 흐름은 자체 계획과 fallback 템플릿 중심이다.

### 3. Current Workflow
LangGraph 기반이며 아래 순서로 동작한다:

```
plan_spec → generate_files → validate_files
                                  ├─ (검증 통과) → package_artifacts
                                  │                    ├─ (빌드 오류 + repair budget) → repair_files → validate_files
                                  │                    └─ (정상 / budget 소진) → finalize
                                  ├─ (검증 실패 + repair budget) → repair_files → validate_files
                                  └─ (검증 실패 + budget 소진) → finalize
```

라우팅 규칙:
- 검증 통과 → `package_artifacts`
- 검증 실패 + repair budget 남음 → `repair_files`
- 검증 실패 + repair budget 소진 → `finalize`
- 빌드 실패 + repair budget 남음 → `repair_files` (build stderr를 ValidationIssue로 파싱하여 재진입)
- 빌드 실패 + repair budget 소진 → `finalize`

### 4. Planning Rules
- 프레임워크가 `FastAPI`면 Python 계열 언어를 우선 선택하고 기본값은 `Python3.12`다.
- 프레임워크가 `Spring` 또는 `Spring Boot`면 Java 계열 언어를 우선 선택하고 기본값은 `Java17`이다.
- Java 계열의 빌드 시스템은 `app_tech_stack.build_system`을 우선 사용하고, `auto`면 기본값은 Maven이다.
- `additional_request`에 `gradle`이 포함되어 있으면 Java 계열의 `auto` 요청도 Gradle로 해석한다.
- `additional_request`에서 아래 특수 시나리오를 감지한다 (영문/한글 모두 인식):
  - `memory leak`, `메모리 릭`, `threadlocal` → `memory_leak`
  - `oom`, `out of memory`, `outofmemory` → `oom`

**`app_id` 생성 규칙:**
- `{framework}-{minor_version}-{topology.apps}-{additional_request[:24]}`를 slugify (소문자, 영숫자+하이픈, 최대 80자)
- 예: `spring-boot-3-5-0-1-board-crud` → `spring-boot-3-5-0-1-board-crud`

**계획 흐름:**
1. LLM `plan_application()` 성공 → `normalize_plan()` 적용
2. `normalize_plan()`은 language/build_system/runtime_version 정규화, `sanitize_file_plan()` 적용, `ensure_required_file_plan()` 보강을 수행한다.
3. LLM 실패(None) → `fallback_plan()` 직접 사용

산출 프로젝트는 `generated_apps/{app_id}`에 생성된다.

### 5. File Generation Policy

**생성 우선순위 (파일별 독립 적용):**

| 우선순위 | 방법 | 설명 |
|---------|------|------|
| 1 | `resolve_file_content()` | 결정론적 템플릿/파일 직접 로딩 |
| 2 | `llm.generate_file()` | LLM 생성 |
| 3 | `resolve_file_content()` fallback | LLM 실패 시 다시 템플릿 |

`APPLICATION_SPEC.md`는 항상 가장 먼저 생성된다.

**`resolve_file_content()` 처리 경로:**
- Spring Board 템플릿 파일 → `spring_template/board/` 디렉터리에서 직접 읽기 (`_BOARD_FILE_MAP` 매핑)
- FastAPI Board 템플릿 파일 → `fastapi_template/board/` 디렉터리에서 직접 읽기 (`_FASTAPI_BOARD_FILE_MAP` 매핑)
- `Dockerfile` → `render_dockerfile_template()` (framework/build_system별 tmpl 파일)
- `pom.xml` → `spring_template/pom.xml.tmpl` + substitution
- `build.gradle` → `spring_template/build.gradle.tmpl` + substitution
- `settings.gradle` → `spring_template/settings.gradle.tmpl` + substitution
- `gradlew`, `gradlew.bat` → `spring_template/gradlew.tmpl`, `gradlew.bat.tmpl` (raw 내용)
- `gradle/wrapper/gradle-wrapper.properties` → `spring_template/gradle-wrapper.properties.tmpl` + GRADLE_VERSION substitution
- `src/main/resources/application.yml` 및 `*application.yml` → `spring_template/application.yml.tmpl` + LOG_DIR substitution
- `app/main.py` → `fastapi_template/main.py` 직접 읽기
- `.env.example` → `required_env()` 목록 + `APP_LOG_DIR` 조합
- `README.md` → 간단한 framework/language 안내 markdown

**템플릿 디렉터리 구조:**

`spring_template/`:
- `.tmpl` (substitution 필요): `pom.xml.tmpl`, `build.gradle.tmpl`, `settings.gradle.tmpl`, `application.yml.tmpl`, `gradlew.tmpl`, `gradlew.bat.tmpl`, `gradle-wrapper.properties.tmpl`, `docker_template_java_gradle.tmpl`, `docker_template_java_maven.tmpl`
- `board/` (소스 파일 직접 읽기 — 10개): `BoardApplication.java`, `model/Post.java`, `dto/PostRequest.java`, `dto/PostResponse.java`, `repository/InMemoryPostRepository.java`, `service/PostService.java`, `controller/PostController.java`, `exception/NotFoundException.java`, `exception/GlobalExceptionHandler.java`, `test/PostControllerTest.java`

`fastapi_template/`:
- `.tmpl` (substitution 필요): `docker_template_python_fastapi.tmpl`
- 소스 파일 (직접 읽기): `main.py`
- `board/` (소스 파일 직접 읽기 — 9개): `__init__.py`, `models.py`, `schemas.py`, `repository.py`, `service.py`, `router.py`, `exceptions.py`, `tests/__init__.py`, `tests/test_board_router.py`

**`sanitize_file_plan()` 차단 규칙:**
- `.kts` 확장자 파일 (Kotlin build script)
- `gradle/wrapper/gradle-wrapper.jar`
- Spring 프레임워크에서 `.kt` 파일
- Maven build_system인데 Gradle 관련 파일 포함 시 제거

**필수 파일 보강 (`ensure_required_file_plan()`):**
- FastAPI: `requirements.txt`, `app/main.py`, `Dockerfile`
- FastAPI + Board CRUD (`app/board/` 경로 존재 시): 9개 파일 세트 전체 보강
- Spring/Spring Boot (Maven): `pom.xml`, `application.yml`, `Dockerfile`, 진입점 Java 파일
- Spring/Spring Boot (Gradle): `settings.gradle`, `build.gradle`, `gradlew`, `gradlew.bat`, `gradle/wrapper/gradle-wrapper.properties`, `application.yml`, `Dockerfile`, 진입점 Java 파일
- Spring + Board CRUD (`src/main/java/com/example/board/` 경로 존재 시): 10개 파일 세트 전체 보강

주의:
- Gradle wrapper `jar` (`gradle/wrapper/gradle-wrapper.jar`)는 생성하지 않는다.
- `.env.example`, `README.md`는 fallback file plan에서 기본 제공되지만, `ensure_required_file_plan()`의 강제 보강 대상은 아니다.

**Spring Board CRUD 파일 세트 (10개):**
- `BoardApplication.java` (Spring Boot entrypoint)
- `model/Post.java`, `dto/PostRequest.java`, `dto/PostResponse.java`
- `repository/InMemoryPostRepository.java`, `service/PostService.java`
- `controller/PostController.java`
- `exception/NotFoundException.java`, `exception/GlobalExceptionHandler.java`
- `src/test/java/com/example/board/PostControllerTest.java`

**FastAPI Board CRUD 파일 세트 (9개):**
- `app/board/__init__.py`, `app/board/models.py`, `app/board/schemas.py`
- `app/board/repository.py`, `app/board/service.py`
- `app/board/router.py`, `app/board/exceptions.py`
- `app/board/tests/__init__.py`, `app/board/tests/test_board_router.py`

### 6. Validation Rules
`SampleAppTools.code_validator()`는 아래를 검증한다:

**공통:**
- 계획된 파일이 실제로 생성됐는지 확인 (파일 존재 여부)
- Python 파일 AST 파싱 가능 여부 (SyntaxError 감지)

**FastAPI:**
- `app/main.py` 존재 여부
- `Dockerfile`에 `uvicorn` 포함 여부
- `app/board/` 경로가 존재하는 경우, 핵심 Board 지원 파일 7종(`__init__.py`, `models.py`, `schemas.py`, `repository.py`, `service.py`, `router.py`, `exceptions.py`) 누락 여부를 검증한다.
- FastAPI board 테스트 파일(`app/board/tests/...`)은 생성 대상일 수 있지만 현재 validator의 필수 검증 대상은 아니다.

**Spring / Spring Boot:**
- `pom.xml` 또는 `build.gradle` 존재 여부
- `@SpringBootApplication` + `main(String[] args)` + (`SpringApplication.run(...)` 또는 `new SpringApplication(...).run(...)`) 진입점 존재 여부
- Spring entrypoint가 여러 개면 실패
- `jakarta.validation` 사용 시 `spring-boot-starter-validation` 의존성 존재 여부
- `src/main/java/com/example/board/` 경로가 존재하는 경우, 핵심 Board 지원 파일 9종(`BoardApplication.java`, `Post.java`, `PostRequest.java`, `PostResponse.java`, `InMemoryPostRepository.java`, `PostService.java`, `PostController.java`, `NotFoundException.java`, `GlobalExceptionHandler.java`) 누락 여부를 검증한다.
- Spring board 테스트 파일(`src/test/java/com/example/board/PostControllerTest.java`)은 생성 대상일 수 있지만 현재 validator의 필수 검증 대상은 아니다.
- `Dockerfile`은 `ENTRYPOINT ["java", "-jar", ...]` 형태여야 함
- `sh -c` 형태의 entrypoint 사용 시 실패

### 7. Build & Packaging Policy
`package_artifacts` 단계에서는 `build_code` → `docker_build` 순서로 진행한다.

**`build_code` 빌드 명령 선택 (우선순위):**

| 조건 | 명령 |
|------|------|
| `gradlew` 존재 + `gradle/wrapper/gradle-wrapper.jar` 존재 | `sh ./gradlew clean bootJar --no-daemon -x test` |
| `build.gradle` 존재 + 로컬 `gradle` 설치 | `gradle clean bootJar --no-daemon -x test` |
| `build.gradle` 존재 + 로컬 `docker` 설치 | Docker 컨테이너 (`gradle:8.14-jdk21`) 사용 |
| `mvnw` 존재 | `sh ./mvnw -q -DskipTests package` |
| `pom.xml` 존재 + 로컬 `mvn` 설치 | `mvn -q -DskipTests package` |

빌드 timeout: 1800초

추가 동작:
- 위 빌드 명령을 선택하지 못하더라도, `build/libs/*.jar` 또는 `target/*.jar`에 이미 패키징된 jar가 있으면 그 산출물을 재사용한다.

아티팩트 탐색:
- Gradle: `build/libs/*.jar` (`-plain.jar` 제외)
- Maven: `target/*.jar` (`-sources.jar`, `-javadoc.jar`, `-original.jar` 제외)

**빌드 실패 처리:**
- 빌드 실패 시 stderr를 `_parse_build_issues()`로 파싱해 `ValidationIssue` 리스트 생성
- Java/Gradle 컴파일 오류: `*.java:{line}: error: {message}` 패턴으로 추출
- 파싱 결과가 없으면 stderr 전체를 단일 BUILD 이슈로 생성
- repair budget이 남아 있으면 `repair_files`로 재진입

**`docker_build` 동작:**
- 대상 서버가 없으면 `E_TARGET_MISSING` 오류
- `auth_method=pem_path`만 지원, 그 외 `E_AUTH_UNSUPPORTED`
- `SAMPLE_APP_AGENT_DRY_RUN` 환경변수 활성화 시 실제 빌드/전송 건너뜀
- Docker 빌드 timeout: 1800초

**Docker 플랫폼 감지:**
- 대상 서버 `os_type`에 `arm`, `aarch64`, `graviton` 포함 시 → `linux/arm64`
- 그 외 기본값 → `linux/amd64`

**Docker 이미지 전송 방식:**
- `docker build` 로컬 빌드 → `docker image save | ssh docker load` 스트리밍 방식으로 대상 서버에 적재
- plan의 `deployment_commands` 중 첫 번째 `docker run ...` 명령이 있으면, image ref를 새 timestamp tag로 치환한 뒤 원격 실행한다.
- `docker run` 명령이 없으면 이미지 적재까지만 수행한다.

**Docker run 포트 충돌 failover:**
- `docker run` 실패 시 포트 충돌 여부 판단 (`port is already allocated`, `address already in use`, `bind:`)
- 충돌이면 10000~65535 범위 랜덤 포트로 교체 후 최대 3회 재시도
- 재시도 전 동일 이름 컨테이너를 `docker stop` + `docker rm`으로 정리

**이미지 태그 규칙:**
- `{image_name_base}:{YYYYmmdd-HHMMSS}` 형식으로 타임스탬프 태그 사용

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
- `success`는 `docker_build` 성공 여부를 따른다
- `executed_commands`에는 `execution_file_write`, `code_validator`, `build_code`, `docker_build` 요약 문자열 포함
- `notes`에는 빌드/Docker 오류, 배포 명령, 이미지 참조, LLM 사용 가능 여부, `VALIDATION_ERROR` 요약이 기록된다

### 9. Generated Outputs Conventions
주요 산출물:
- `application spec: {APPLICATION_SPEC.md path}`
- `sample app source: {project_dir}`
- `application jar: {artifact_path}`
- `container image: {image_ref}`
- `배포 가이드라인 및 API 문서`

권장 설정 (`recommended_config`):
- `APP_LOG_DIR={log_dir}`
- DB 사용 시: `APP_DB_HOST=`, `APP_DB_PORT=`, `APP_DB_NAME=`, `APP_DB_USER=`, `APP_DB_PASSWORD=`
- Java 앱이고 `gc_log_dir`이 있으면: `JAVA_TOOL_OPTIONS=-Xlog:gc*:file={gc_log_dir}/gc.log`

### 10. Failure Policy
- `framework == "none"` 또는 `topology.apps <= 0`이면 앱 생성 skip 후 `success=True` 반환
- 예기치 않은 예외 발생 시 `success=False` + 오류 요약을 notes에 기록
- 정적 검증 실패가 repair budget 내에서 복구되지 않으면 `success=False`로 종료
- 빌드 실패 시 `BUILD_ERROR`, `BUILD_STDERR`를 notes에 기록
- Docker 전송 실패 시 `DOCKER_UPLOAD_ERROR`, `DOCKER_UPLOAD_STDERR`를 notes에 기록

오류 코드:
- `E_BUILD_TOOL_MISSING`: 빌드 도구를 찾지 못함
- `E_BUILD_FAILED`: 빌드 명령 비정상 종료
- `E_ARTIFACT_MISSING`: 빌드 후 jar 파일 없음
- `E_TARGET_MISSING`: 대상 서버 없음
- `E_AUTH_UNSUPPORTED`: pem_path 이외의 인증 방식
- `E_DOCKER_BUILD`: docker build 실패
- `E_DOCKER_SAVE`: docker image save 실패
- `E_DOCKER_REMOTE_LOAD`: 대상 서버 docker load 실패
- `E_DOCKER_REMOTE_RUN`: 대상 서버 docker run 실패 (포트 failover 포함)

### 11. Tool Chain
```
execution_file_write → code_validator → build_code → docker_build
```

- `execution_file_write`: 프로젝트 파일 생성
- `code_validator`: 생성 코드 정적 검증
- `build_code`: jar 산출물 패키징
- `docker_build`: Docker 이미지 빌드 및 대상 서버 적재/기동
