from __future__ import annotations

import re
from pathlib import Path
from string import Template

from Supervisor.models import UserRequest

from .models import ApplicationFilePlan, ApplicationPlan

TEMPLATE_DIR = Path(__file__).resolve().parent
BOARD_TEMPLATE_DIR = TEMPLATE_DIR / "templates" / "board"
SPRING_TEMPLATE_DIR = TEMPLATE_DIR / "spring_template"
FASTAPI_TEMPLATE_DIR = TEMPLATE_DIR / "fastapi_template"
DEFAULT_GRADLE_VERSION = "8.14"


def _load_board_template(relative_path: str) -> str:
    path = BOARD_TEMPLATE_DIR / relative_path
    return path.read_text(encoding="utf-8")


def _load_template(path: Path) -> Template:
    return Template(path.read_text(encoding="utf-8"))


def runtime_version(language: str) -> str:
    values = re.findall(r"\d+(?:\.\d+)?", language)
    return values[0] if values else ""


def normalized_runtime_version(value: str, default: str) -> str:
    extracted = runtime_version(value or "")
    return extracted or default


def resolve_build_system(request: UserRequest, framework: str) -> str:
    if framework.strip().lower() not in {"spring", "spring boot"}:
        return "maven"
    requested = str(getattr(request.app_tech_stack, "build_system", "")).strip().lower()
    if requested in {"maven", "gradle"}:
        return requested
    if "gradle" in request.additional_request.lower():
        return "gradle"
    return "maven"


def detect_special_scenarios(additional_request: str) -> list[str]:
    value = additional_request.lower()
    scenarios: list[str] = []
    if any(token in value for token in ["memory leak", "메모리 릭", "threadlocal"]):
        scenarios.append("memory_leak")
    if any(token in value for token in ["oom", "out of memory", "outofmemory"]):
        scenarios.append("oom")
    return scenarios


def required_env(request: UserRequest) -> list[str]:
    envs: list[str] = []
    if request.app_tech_stack.databases and request.app_tech_stack.databases.lower() != "none":
        envs.extend(["APP_DB_HOST=", "APP_DB_PORT=", "APP_DB_NAME=", "APP_DB_USER=", "APP_DB_PASSWORD="])
    return envs


def deployment_command(port: str, request: UserRequest, image_name: str) -> str:
    args = [f"-e APP_LOG_DIR={request.logging.app_log_dir}"]
    if request.app_tech_stack.databases and request.app_tech_stack.databases.lower() != "none":
        args.append("-e APP_DB_PASSWORD=${APP_DB_PASSWORD}")
    return f"docker run -d -p {port}:{port} {' '.join(args)} {image_name}".strip()


def spring_main_class_name(app_id: str) -> str:
    words = [chunk for chunk in re.split(r"[^a-zA-Z0-9]+", app_id) if chunk]
    base = "".join(word[:1].upper() + word[1:] for word in words) or "SampleApp"
    return base if base.endswith("Application") else f"{base}Application"


def spring_main_class_path(app_id: str) -> str:
    return f"src/main/java/com/example/sampleapp/{spring_main_class_name(app_id)}.java"


def gradle_version_for_plan(plan: ApplicationPlan) -> str:
    if "4.0" in plan.framework_version:
        return "8.14"
    return DEFAULT_GRADLE_VERSION


def _spring_boot_version(plan: ApplicationPlan) -> str:
    if "4.0" in plan.framework_version:
        return "4.0.0"
    if "3.0" in plan.framework_version:
        return "3.0.0"
    return "3.5.0"


def fallback_plan(request: UserRequest, project_dir: Path, app_id: str, language: str) -> ApplicationPlan:
    framework = request.app_tech_stack.framework.strip() or "FastAPI"
    rv = runtime_version(language)
    is_java = language.lower().startswith("java")
    build_system = resolve_build_system(request=request, framework=framework)
    port = "8080" if is_java else "8000"
    file_plan = fallback_file_plan(framework, build_system=build_system)
    return ApplicationPlan(
        app_id=app_id,
        framework=framework,
        framework_version=request.app_tech_stack.minor_version or "latest",
        language=language,
        build_system=build_system,
        runtime_version=rv,
        artifact_type="jar" if is_java else "zip",
        artifact_name=f"{app_id}.{'jar' if is_java else 'zip'}",
        image_name=f"sample-app/{app_id}:latest",
        project_dir=str(project_dir),
        log_dir=request.logging.app_log_dir,
        gc_log_dir=request.logging.gc_log_dir,
        special_scenarios=detect_special_scenarios(request.additional_request),
        deployment_commands=[deployment_command(port, request, f"sample-app/{app_id}:latest")],
        required_env=required_env(request),
        file_plan=file_plan,
        spec_markdown=fallback_spec_markdown(request, framework, language, app_id, file_plan, build_system),
    )


def normalize_plan(plan: ApplicationPlan, request: UserRequest, project_dir: Path, app_id: str, resolve_language_fn) -> ApplicationPlan:
    language = resolve_language_fn(plan.framework, [plan.language] if plan.language else [])
    is_java = language.lower().startswith("java")
    build_system = resolve_build_system(request=request, framework=plan.framework)
    rv = normalized_runtime_version(
        plan.runtime_version or runtime_version(language) or ("17" if is_java else "3.12"),
        default="17" if is_java else "3.12",
    )
    file_plan_list = list(plan.file_plan) if plan.file_plan else fallback_file_plan(plan.framework, build_system=build_system)
    file_plan_list = sanitize_file_plan(file_plan_list, framework=plan.framework, build_system=build_system)
    file_plan_list = ensure_required_file_plan(file_plan_list, app_id=app_id, framework=plan.framework, build_system=build_system)

    normalized_artifact_name = f"{app_id}.jar" if is_java else f"{app_id}.zip"
    return plan.model_copy(
        update={
            "app_id": app_id,
            "language": language,
            "build_system": build_system,
            "runtime_version": rv,
            "artifact_type": "jar" if is_java else "zip",
            "artifact_name": normalized_artifact_name,
            "project_dir": str(project_dir),
            "file_plan": file_plan_list,
        }
    )


def sanitize_file_plan(
    file_plan: list[ApplicationFilePlan],
    framework: str,
    build_system: str,
) -> list[ApplicationFilePlan]:
    normalized_framework = framework.strip().lower()
    sanitized: list[ApplicationFilePlan] = []
    seen: set[str] = set()
    blocked_suffixes = (".kts",)
    blocked_exact = {"gradle/wrapper/gradle-wrapper.jar"}

    for item in file_plan:
        path = item.path.strip()
        if not path or path in seen:
            continue
        if path in blocked_exact:
            continue
        if path.endswith(blocked_suffixes):
            continue
        if normalized_framework in {"spring", "spring boot"} and path.endswith(".kt"):
            continue
        if normalized_framework in {"spring", "spring boot"} and build_system == "maven":
            if path in {"build.gradle", "settings.gradle", "gradlew", "gradlew.bat", "gradle/wrapper/gradle-wrapper.properties"}:
                continue
        sanitized.append(item.model_copy(update={"path": path}))
        seen.add(path)

    return sanitized


def ensure_required_file_plan(
    file_plan: list[ApplicationFilePlan],
    app_id: str,
    framework: str,
    build_system: str,
) -> list[ApplicationFilePlan]:
    existing_paths = {item.path for item in file_plan}
    normalized_framework = framework.strip().lower()
    required: list[ApplicationFilePlan] = []
    main_class_p = spring_main_class_path(app_id)

    if normalized_framework in {"spring", "spring boot"}:
        has_declared_entrypoint = any(
            item.path.endswith(".java")
            and (
                Path(item.path).name.endswith("Application.java")
                or "entrypoint" in item.purpose.lower()
            )
            for item in file_plan
        )
        if build_system == "gradle":
            required.extend(
                [
                    ApplicationFilePlan(path="settings.gradle", purpose="Gradle settings", language="gradle"),
                    ApplicationFilePlan(path="build.gradle", purpose="Gradle build descriptor", language="gradle"),
                    ApplicationFilePlan(path="gradlew", purpose="Gradle wrapper launcher", language="shell"),
                    ApplicationFilePlan(path="gradlew.bat", purpose="Gradle wrapper launcher for Windows", language="batch"),
                    ApplicationFilePlan(path="gradle/wrapper/gradle-wrapper.properties", purpose="Gradle wrapper properties", language="properties"),
                ]
            )
        else:
            required.append(ApplicationFilePlan(path="pom.xml", purpose="Maven build descriptor", language="xml"))

        required.extend(
            [
                ApplicationFilePlan(path="src/main/resources/application.yml", purpose="Application config", language="yaml"),
                ApplicationFilePlan(path="Dockerfile", purpose="Container build file", language="dockerfile"),
            ]
        )
        if any(path.startswith("src/main/java/com/example/board/") for path in existing_paths):
            required.extend(
                [
                    ApplicationFilePlan(path="src/main/java/com/example/board/model/Post.java", purpose="Board domain model", language="java"),
                    ApplicationFilePlan(path="src/main/java/com/example/board/dto/PostRequest.java", purpose="Board create/update request DTO", language="java"),
                    ApplicationFilePlan(path="src/main/java/com/example/board/dto/PostResponse.java", purpose="Board response DTO", language="java"),
                    ApplicationFilePlan(path="src/main/java/com/example/board/repository/InMemoryPostRepository.java", purpose="In-memory board repository", language="java"),
                    ApplicationFilePlan(path="src/main/java/com/example/board/service/PostService.java", purpose="Board service layer", language="java"),
                    ApplicationFilePlan(path="src/main/java/com/example/board/controller/BoardController.java", purpose="Board REST controller", language="java"),
                    ApplicationFilePlan(path="src/main/java/com/example/board/exception/NotFoundException.java", purpose="Board not found exception", language="java"),
                    ApplicationFilePlan(path="src/main/java/com/example/board/exception/GlobalExceptionHandler.java", purpose="Board exception handler", language="java"),
                    ApplicationFilePlan(path="src/test/java/com/example/board/BoardControllerTest.java", purpose="Board controller integration test", language="java"),
                ]
            )
        if not has_declared_entrypoint:
            required.append(
                ApplicationFilePlan(path=main_class_p, purpose="Spring Boot entrypoint", language="java")
            )
    elif normalized_framework == "fastapi":
        required.extend(
            [
                ApplicationFilePlan(path="requirements.txt", purpose="Python dependencies", language="text"),
                ApplicationFilePlan(path="app/main.py", purpose="FastAPI entrypoint", language="python"),
                ApplicationFilePlan(path="Dockerfile", purpose="Container build file", language="dockerfile"),
            ]
        )

    merged = list(file_plan)
    for item in required:
        if item.path not in existing_paths:
            merged.append(item)
            existing_paths.add(item.path)
    return merged


def fallback_file_plan(framework: str, build_system: str = "maven") -> list[ApplicationFilePlan]:
    if framework.strip().lower() == "fastapi":
        return [
            ApplicationFilePlan(path="requirements.txt", purpose="Python dependencies", language="text"),
            ApplicationFilePlan(path="app/main.py", purpose="FastAPI entrypoint", language="python"),
            ApplicationFilePlan(path=".env.example", purpose="Example environment variables", language="dotenv"),
            ApplicationFilePlan(path="Dockerfile", purpose="Container build file", language="dockerfile"),
            ApplicationFilePlan(path="README.md", purpose="Generated project guide", language="markdown"),
        ]
    java_files = [
        ApplicationFilePlan(path="src/main/java/com/example/sampleapp/SampleAppApplication.java", purpose="Spring Boot entrypoint", language="java"),
        ApplicationFilePlan(path="src/main/java/com/example/sampleapp/DemoController.java", purpose="REST controller", language="java"),
        ApplicationFilePlan(path="src/main/resources/application.yml", purpose="Application config", language="yaml"),
        ApplicationFilePlan(path=".env.example", purpose="Example environment variables", language="dotenv"),
        ApplicationFilePlan(path="Dockerfile", purpose="Container build file", language="dockerfile"),
        ApplicationFilePlan(path="README.md", purpose="Generated project guide", language="markdown"),
    ]
    if build_system == "gradle":
        return [
            ApplicationFilePlan(path="settings.gradle", purpose="Gradle settings", language="gradle"),
            ApplicationFilePlan(path="build.gradle", purpose="Gradle build descriptor", language="gradle"),
            *java_files,
        ]
    return [
        ApplicationFilePlan(path="pom.xml", purpose="Maven build descriptor", language="xml"),
        *java_files,
    ]


def fallback_spec_markdown(
    request: UserRequest,
    framework: str,
    language: str,
    app_id: str,
    file_plan: list[ApplicationFilePlan],
    build_system: str,
) -> str:
    files = "\n".join(f"- `{item.path}`: {item.purpose}" for item in file_plan)
    scenarios = "\n".join(f"- {item}" for item in detect_special_scenarios(request.additional_request)) or "- none"
    return "\n".join(
        [
            "# APPLICATION_SPEC",
            "",
            f"- app_id: {app_id}",
            f"- framework: {framework}",
            f"- framework_version: {request.app_tech_stack.minor_version or 'latest'}",
            f"- language: {language}",
            f"- build_system: {build_system}",
            f"- logging_dir: {request.logging.app_log_dir}",
            "",
            "## Requested Scenarios",
            scenarios,
            "",
            "## Planned Files",
            files,
            "",
            "## Additional Request",
            request.additional_request or "- none",
        ]
    )


def fallback_pom(plan: ApplicationPlan) -> str:
    return _load_template(SPRING_TEMPLATE_DIR / "pom.xml.tmpl").substitute(
        ARTIFACT_ID=plan.app_id,
        SPRING_BOOT_VERSION=_spring_boot_version(plan),
        JAVA_VERSION=normalized_runtime_version(plan.runtime_version, default="17"),
    )


def fallback_gradle_build(plan: ApplicationPlan) -> str:
    java_version = normalized_runtime_version(plan.runtime_version, default="17")
    return _load_template(SPRING_TEMPLATE_DIR / "build.gradle.tmpl").substitute(
        SPRING_BOOT_VERSION=_spring_boot_version(plan),
        JAVA_MAJOR_VERSION=java_version.split(".")[0],
    )


def fallback_fastapi_main(request: UserRequest, plan: ApplicationPlan) -> str:
    base = _load_template(FASTAPI_TEMPLATE_DIR / "main.py").substitute(
        APP_LOG_DIR=request.logging.app_log_dir,
        FRAMEWORK_NAME=plan.framework,
    )
    if "memory_leak" in plan.special_scenarios:
        return base + (FASTAPI_TEMPLATE_DIR / "leak_block.py").read_text(encoding="utf-8") + "\n"
    return base + "\n"


def render_dockerfile_template(plan: ApplicationPlan) -> str:
    framework = plan.framework.strip().lower()
    if framework == "fastapi":
        template_path = FASTAPI_TEMPLATE_DIR / "docker_template_python_fastapi.tmpl"
        return _load_template(template_path).substitute(
            PYTHON_VERSION=normalized_runtime_version(plan.runtime_version, default="3.12")
        ) + "\n"

    template_name = "docker_template_java_gradle.tmpl" if plan.build_system == "gradle" else "docker_template_java_maven.tmpl"
    template_path = SPRING_TEMPLATE_DIR / template_name
    artifact_name = plan.artifact_name if plan.artifact_name.endswith(".jar") else f"{plan.app_id}.jar"
    return _load_template(template_path).substitute(
        JAVA_VERSION=normalized_runtime_version(plan.runtime_version, default="17"),
        ARTIFACT_NAME=artifact_name,
        GRADLE_VERSION=gradle_version_for_plan(plan),
    ) + "\n"


_BOARD_FILE_MAP = {
    "src/main/java/com/example/board/model/Post.java": "model/Post.java",
    "src/main/java/com/example/board/dto/PostRequest.java": "dto/PostRequest.java",
    "src/main/java/com/example/board/dto/PostResponse.java": "dto/PostResponse.java",
    "src/main/java/com/example/board/repository/InMemoryPostRepository.java": "repository/InMemoryPostRepository.java",
    "src/main/java/com/example/board/service/PostService.java": "service/PostService.java",
    "src/main/java/com/example/board/controller/BoardController.java": "controller/BoardController.java",
    "src/main/java/com/example/board/exception/NotFoundException.java": "exception/NotFoundException.java",
    "src/main/java/com/example/board/exception/GlobalExceptionHandler.java": "exception/GlobalExceptionHandler.java",
    "src/test/java/com/example/board/BoardControllerTest.java": "test/BoardControllerTest.java",
}


def resolve_file_content(
    request: UserRequest,
    plan: ApplicationPlan,
    file_plan: ApplicationFilePlan,
) -> str | None:
    """Unified deterministic content resolver (merges D-9: _deterministic + _fallback)."""
    path = file_plan.path

    # Board templates from files
    board_template = _BOARD_FILE_MAP.get(path)
    if board_template is not None:
        return _load_board_template(board_template)

    # Board application entrypoint
    if path == "src/main/java/com/example/board/BoardApplication.java":
        return (SPRING_TEMPLATE_DIR / "BoardApplication.java").read_text(encoding="utf-8")

    # Dockerfile
    if path == "Dockerfile":
        return render_dockerfile_template(plan)

    # Build descriptors
    if path == "pom.xml":
        return fallback_pom(plan)
    if path == "build.gradle":
        return fallback_gradle_build(plan)
    if path == "settings.gradle":
        return _load_template(SPRING_TEMPLATE_DIR / "settings.gradle.tmpl").substitute(APP_ID=plan.app_id)

    # Gradle wrapper files
    if path == "gradlew":
        return _load_template(SPRING_TEMPLATE_DIR / "gradlew.tmpl").template + "\n"
    if path == "gradlew.bat":
        return _load_template(SPRING_TEMPLATE_DIR / "gradlew.bat.tmpl").template + "\n"
    if path == "gradle/wrapper/gradle-wrapper.properties":
        return _load_template(SPRING_TEMPLATE_DIR / "gradle-wrapper.properties.tmpl").substitute(
            GRADLE_VERSION=gradle_version_for_plan(plan)
        ) + "\n"

    # Spring main class
    if path == spring_main_class_path(plan.app_id):
        cls_name = spring_main_class_name(plan.app_id)
        content = (SPRING_TEMPLATE_DIR / "SpringApplication.java").read_text(encoding="utf-8")
        return content.replace("SampleAppApplication", cls_name)

    # application.yml
    if path == "src/main/resources/application.yml" or path.endswith("application.yml"):
        return _load_template(SPRING_TEMPLATE_DIR / "application.yml.tmpl").substitute(LOG_DIR=plan.log_dir)

    # Fallback-only paths
    if path == "requirements.txt":
        return (FASTAPI_TEMPLATE_DIR / "requirements.txt").read_text(encoding="utf-8")
    if path == ".env.example":
        return "\n".join(required_env(request) + [f"APP_LOG_DIR={plan.log_dir}"]) + "\n"
    if path == "README.md":
        return f"# Generated Sample App\n\n- framework: {plan.framework}\n- language: {plan.language}\n"
    if path == "app/main.py":
        return fallback_fastapi_main(request, plan)
    if path.endswith("SampleAppApplication.java"):
        return (SPRING_TEMPLATE_DIR / "SpringApplication.java").read_text(encoding="utf-8")
    if path.endswith("DemoController.java"):
        return (SPRING_TEMPLATE_DIR / "DemoController.java").read_text(encoding="utf-8")

    return None
