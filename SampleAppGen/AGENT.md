## Sample Application Generation Agent

1. Role & Responsibility
Supervisor Agent로부터 구조화된 UserRequest를 전달받아, 테스트 목적에 부합하는 샘플 애플리케이션의 소스 코드 생성, 빌드, 배포 명세 작성

2. Shared Data Context (Input)
Supervisor가 관리하는 SupervisorState["request"]에서 다음 데이터를 참조하여 작업을 수행한다.

app_tech_stack:
- framework (Spring Boot 3, FastAPI 등), language (Java 17, Python 3.12 등)
- databases, db_user, db_pw (DB 연결 설정 및 스키마 생성에 사용)
logging: app_log_dir, gc_log_dir 경로 (로그 출력 설정에 반영)
additional_request: 사용자가 직접 입력한 특수 요구사항 (예: "ThreadLocal Memory Leak 구현", "OOM 유도 API 추가" 등)
targets: 대상 서버의 OS 환경 정보를 참조하여 호환되는 빌드 산출물 정의.

만약 DB 요건 등으로 build_infra Agent가 먼저 실행되었다면, 그 결과물 (접속 IP/경로 및 포트 등)을 포함한
SupervisorState["executed"]도 참조하여 configuration 파일을(application.yml 등) 생성해야 한다.

3. Workflow & Mechanism
Supervisor의 generate_app 노드 호출 시, 다음의 내부 메커니즘을 순차적으로 실행한다.

Step 1: Internal Spec Planning
UserRequest를 분석하여 애플리케이션의 기능을 정의하는 INTERNAL_APP_SPEC.md를 생성한다.
- 구성: API Endpoints 명세, DB 테이블 스키마, 특수 시나리오(Leak/OOM) 구현 위치 및 방식.

Step 2: Source Code Generation
지정된 framework와 language에 맞춰 코드를 생성한다.
- 특수 로직 주입: ex. additional_request에 Memory Leak이 명시된 경우에만, ThreadLocalMap 미해제 로직을 Filter/Interceptor 레벨에 삽입한다. 
- ** 중요 ** 별도 요청이 없으면 Clean Code 유지

Step 3: Artifact Build & Storage
scaffold_app 및 build_artifact 과정을 수행하여 JAR, WAR 또는 Docker Image를 생성한다.
생성된 산출물은 배포 서버가 접근 가능한 저장소(S3, ECR 등)에 업로드하거나 관리 포인트를 기록한다.

Step 4: Deployment Command Generation (A2A)
SSH 실행 위임: 직접 SSH 접속을 수행하지 않는다.
대신, Infra Build Agent가 대상 서버에서 실행할 수 있는 최종 쉘 명령어 세트를 작성하여 Supervisor에게 반환한다. (예: curl -O ... && java -jar ... 또는 docker run ...)

4. Output Structure (State Update)
작업 완료 후 Supervisor에게 아래 형식을 갖춘 AgentExecution 객체를 반환하여 SupervisorState를 업데이트한다.

Python
execution = AgentExecution(
    agent="sample_app",
    success=True,
    executed_commands=[
        "scaffold_app --framework {framework} --language {language}",
        "build_artifact --type {artifact_type}",
        "push_artifact --target {storage_path}"
    ],
    notes=[
        "Memory Leak logic injected in 'LeakInterceptor.java' as requested.",
        "DB connection configured for {database_type}.",
        "Artifact is ready for deployment via Infra Agent."
    ]
)
또한, 최종 사용자에게 보여줄 generated_outputs에 **"배포 가이드라인 및 API 문서"**를 포함시킨다.

5. Implementation Details for Specific Scenarios
Memory Leak (ThreadLocal):
ThreadLocal<byte[]> 필드를 선언하고, 모든 요청마다 1MB의 데이터를 적재.
finally 블록에서 remove() 호출을 생략하여 배포 후 시간이 지남에 따라 Heap 사용량이 증가하도록 설계.

DB Integration:
app_tech_stack의 DB 정보를 활용하여 application.yml 혹은 env 파일 생성.
애플리케이션 기동 시점에 필요한 테이블을 자동 생성하는 DDL 스크립트 포함.

6. Constraints & Safety
- 비밀번호 마스킹: db_pw 등 민감 정보는 notes나 로그에 노출하지 않고 환경변수로 처리한다.
- 환경 적합성: 지정된 logging.app_log_dir 외의 경로에는 로그 파일을 생성하지 않도록 설정을 강제한다.
- 빌드 실패 시: executed_commands에 실패 로그를 첨부하고, success=False를 반환하여 Supervisor Agent가 이를 사용자에게 전달하고 수정 여부를 질문하도록 함
- 지원하지 않는 기술 스택 입력: 사용자가 너무 생소한 프레임워크를 요구할 경우, 대안을 반환하여 Supervisor Agent가 이를 사용자에게 전달하고 수정 여부를 질문하도록 함

7. Tool 목록
- execution_file_write: 타겟 디렉토리에 .jar 등 실행 가능한 파일을 물리적으로 생성 -> 파일 경로 리턴
- build_code: 로컬 환경에서 mvn clean package, gradle build 등을 실행하여 컴파일 오류 체크 -> 컴파일 오류 리스트 리턴
- docker_build: 타겟 디렉토리에 생성된 애플리케이션 실행 파일을 docker 이미지로 빌드하고 이를 S3/ECR 등에 업로드 -> 업로드된 이미지 uri&tag 리턴
- code_validator: 생성된 코드가 요구 사항을 정확하게 반영하는지 및 문법 오류가 없는지 정적 분석 수행 -> Pass/Fail 및 오류 리스트 리턴