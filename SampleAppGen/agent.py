from __future__ import annotations

import operator
import re
import shutil
from pathlib import Path
from string import Template
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, GraphView, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step
from eventing import emit_event

from .llm import SampleAppGeneratorLLM
from .models import (
    GRAPH_EDGES,
    GRAPH_MERMAID,
    GRAPH_NODES,
    ApplicationFilePlan,
    ApplicationPlan,
    GeneratedFile,
    SampleAppRunResult,
    ValidationIssue,
)
from .tools import SampleAppTools

logger = get_agent_logger("sample_app_gen.agent", "sample_app_gen.log")
TEMPLATE_DIR = Path(__file__).resolve().parent
DEFAULT_GRADLE_VERSION = "8.14"


class UnsupportedStackError(Exception):
    pass


class SampleAppState(TypedDict, total=False):
    request: UserRequest
    prior_executions: list[AgentExecution]
    plan: ApplicationPlan
    existing_files: dict[str, str]
    generated_files: Annotated[list[GeneratedFile], operator.add]
    executed_commands: Annotated[list[str], operator.add]
    notes: Annotated[list[str], operator.add]
    generated_outputs: Annotated[list[str], operator.add]
    recommended_config: Annotated[list[str], operator.add]
    rollback_cleanup: Annotated[list[str], operator.add]
    validation_issues: list[ValidationIssue]
    repair_round: int
    success: bool


class SampleAppAgent:
    def __init__(
        self,
        settings: AzureOpenAISettings | None = None,
        workspace_root: str | Path | None = None,
        max_repairs: int = 2,
    ):
        # Store generated source/artifacts under the project root by default.
        base_dir = Path(workspace_root) if workspace_root else Path(__file__).resolve().parents[1]
        self.workspace_root = base_dir / "generated_apps"
        self.max_repairs = max_repairs
        self.llm = SampleAppGeneratorLLM(settings or AzureOpenAISettings())
        self.tools = SampleAppTools()
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(SampleAppState)
        workflow.add_node("plan_spec", self._plan_spec_node)
        workflow.add_node("generate_files", self._generate_files_node)
        workflow.add_node("validate_files", self._validate_files_node)
        workflow.add_node("repair_files", self._repair_files_node)
        workflow.add_node("package_artifacts", self._package_artifacts_node)
        workflow.add_node("finalize", self._finalize_node)

        workflow.add_edge(START, "plan_spec")
        workflow.add_edge("plan_spec", "generate_files")
        workflow.add_edge("generate_files", "validate_files")
        workflow.add_conditional_edges(
            "validate_files",
            self._route_after_validation,
            {
                "repair_files": "repair_files",
                "package_artifacts": "package_artifacts",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("repair_files", "validate_files")
        workflow.add_edge("package_artifacts", "finalize")
        workflow.add_edge("finalize", END)
        return workflow.compile()

    def graph_view(self) -> GraphView:
        return GraphView(nodes=GRAPH_NODES, edges=GRAPH_EDGES, mermaid=GRAPH_MERMAID)

    def run(
        self,
        request: UserRequest,
        prior_executions: list[AgentExecution] | None = None,
    ) -> SampleAppRunResult:
        framework = request.app_tech_stack.framework.strip().lower()
        with timed_step(logger, "sample_app_gen.run", framework=framework):
            if framework == "none" or request.topology.apps <= 0:
                execution = AgentExecution(
                    agent="sample_app",
                    success=True,
                    executed_commands=[],
                    notes=["No application framework was requested, so sample app generation was skipped."],
                )
                result = SampleAppRunResult(
                    execution=execution,
                    generated_outputs=["sample app generation skipped"],
                    graph=self.graph_view(),
                )
                log_event(logger, "sample_app_gen.run.skipped", framework=framework)
                return result

            try:
                state = self.graph.invoke(
                    {
                        "request": request,
                        "prior_executions": prior_executions or [],
                        "existing_files": {},
                        "generated_files": [],
                        "executed_commands": [],
                        "notes": [],
                        "generated_outputs": [],
                        "recommended_config": [],
                        "rollback_cleanup": [],
                        "validation_issues": [],
                        "repair_round": 0,
                        "success": False,
                    }
                )
            except Exception as exc:
                log_event(logger, "sample_app_gen.run.exception", framework=framework, error=str(exc))
                execution = AgentExecution(
                    agent="sample_app",
                    success=False,
                    executed_commands=[],
                    notes=[f"Unexpected sample app generation failure: {exc}"],
                )
                return SampleAppRunResult(execution=execution, graph=self.graph_view())

            issue_lines = [f"{item.path}: {item.message}" for item in state.get("validation_issues", [])]
            notes = [*state.get("notes", [])]
            if issue_lines:
                notes.extend(f"VALIDATION_ERROR: {item}" for item in issue_lines)

            execution = AgentExecution(
                agent="sample_app",
                success=state.get("success", False),
                executed_commands=state.get("executed_commands", []),
                notes=notes,
            )
            result = SampleAppRunResult(
                execution=execution,
                generated_outputs=state.get("generated_outputs", []),
                recommended_config=state.get("recommended_config", []),
                rollback_cleanup=state.get("rollback_cleanup", []),
                spec_markdown=state.get("plan", ApplicationPlan(
                    app_id="sample-app",
                    framework=request.app_tech_stack.framework or "unknown",
                    framework_version=request.app_tech_stack.minor_version or "latest",
                    language="unknown",
                    build_system="maven",
                    artifact_name="sample-app.zip",
                    image_name="sample-app/sample-app:latest",
                    project_dir="",
                    log_dir=request.logging.app_log_dir,
                )).spec_markdown,
                generated_files=state.get("generated_files", []),
                graph=self.graph_view(),
            )
            log_event(
                logger,
                "sample_app_gen.run.result",
                success=result.execution.success,
                generated_file_count=len(result.generated_files),
                generated_outputs=result.generated_outputs,
            )
            return result

    def _plan_spec_node(self, state: SampleAppState) -> SampleAppState:
        request = state["request"]
        with timed_step(logger, "sample_app_gen.plan_spec_node", framework=request.app_tech_stack.framework):
            emit_event(owner="sample_app", phase="plan_spec", status="started", message="애플리케이션 스펙 계획을 생성합니다.")
            framework = request.app_tech_stack.framework.strip()
            languages = [value.strip() for value in request.app_tech_stack.language if value.strip()]
            primary_language = self._resolve_language(framework, languages)
            app_id = self._slugify(
                f"{framework}-{request.app_tech_stack.minor_version}-{request.topology.apps or 1}-{request.additional_request[:24]}"
            )
            project_dir = self.workspace_root / app_id

            plan = self.llm.plan_application(
                request=request,
                prior_executions=state.get("prior_executions", []),
                project_dir=project_dir,
                app_id=app_id,
            )
            if plan is None:
                plan = self._fallback_plan(request=request, project_dir=project_dir, app_id=app_id, language=primary_language)
                llm_mode = "fallback"
            else:
                plan = self._normalize_plan(plan, request=request, project_dir=project_dir, app_id=app_id)
                llm_mode = "llm"
            log_event(logger, "sample_app_gen.plan_spec_node.result", app_id=plan.app_id, llm_mode=llm_mode)
            emit_event(
                owner="sample_app",
                phase="plan_spec",
                status="completed",
                message="애플리케이션 스펙 계획 생성이 완료되었습니다.",
                details={"app_id": plan.app_id, "llm_mode": llm_mode},
            )
            return {
                "plan": plan,
                "notes": [f"APPLICATION_SPEC prepared via {llm_mode} planning."],
            }

    def _generate_files_node(self, state: SampleAppState) -> SampleAppState:
        request = state["request"]
        plan = state["plan"]
        with timed_step(logger, "sample_app_gen.generate_files_node", app_id=plan.app_id, file_count=len(plan.file_plan)):
            emit_event(owner="sample_app", phase="generate_files", status="started", message="프로젝트 파일 생성을 시작합니다.", details={"app_id": plan.app_id})
            project_dir = Path(plan.project_dir)
            if project_dir.exists():
                shutil.rmtree(project_dir)
            project_dir.mkdir(parents=True, exist_ok=True)

            existing_files: dict[str, str] = {}
            generated_files: list[GeneratedFile] = []

            spec_path = project_dir / "APPLICATION_SPEC.md"
            self.tools.call("execution_file_write", path=spec_path, content=plan.spec_markdown, overwrite=True)
            existing_files["APPLICATION_SPEC.md"] = plan.spec_markdown
            generated_files.append(GeneratedFile(path=str(spec_path), description="Generated application specification"))

            for file_plan in plan.file_plan:
                with timed_step(logger, "sample_app_gen.generate_file_step", path=file_plan.path):
                    emit_event(owner="sample_app", phase="generate_file", status="progress", message="파일을 생성합니다.", details={"path": file_plan.path})
                    content = self._deterministic_file_content(request, plan, file_plan)
                    if content is None:
                        content = self.llm.generate_file(request, plan, file_plan, existing_files)
                        if content is None:
                            content = self._fallback_file_content(request, plan, file_plan)
                            log_event(logger, "sample_app_gen.generate_file_step.fallback", path=file_plan.path)
                    else:
                        log_event(logger, "sample_app_gen.generate_file_step.template", path=file_plan.path)
                    path = project_dir / file_plan.path
                    self.tools.call("execution_file_write", path=path, content=content, overwrite=True)
                    existing_files[file_plan.path] = content
                    generated_files.append(GeneratedFile(path=str(path), description=file_plan.purpose))
            emit_event(owner="sample_app", phase="generate_files", status="completed", message="프로젝트 파일 생성이 완료되었습니다.", details={"file_count": len(generated_files)})

            return {
                "existing_files": existing_files,
                "generated_files": generated_files,
                "executed_commands": [f"execution_file_write --path {project_dir} --type source"],
                "generated_outputs": [f"application spec: {spec_path}", f"sample app source: {project_dir}"],
                "rollback_cleanup": [f"rm -rf {project_dir}"],
                "notes": [f"Generated {len(plan.file_plan)} project files under {project_dir}."],
            }

    def _validate_files_node(self, state: SampleAppState) -> SampleAppState:
        plan = state["plan"]
        with timed_step(logger, "sample_app_gen.validate_files_node", app_id=plan.app_id):
            emit_event(owner="sample_app", phase="validate_files", status="started", message="생성된 소스 정적 검증을 시작합니다.", details={"app_id": plan.app_id})
            project_dir = Path(plan.project_dir)
            validation = self.tools.call(
                "code_validator",
                project_dir=project_dir,
                expected_files=[item.path for item in plan.file_plan],
                existing_files=state.get("existing_files", {}),
                framework=plan.framework,
            )
            issues: list[ValidationIssue] = validation["issues"]

            log_event(logger, "sample_app_gen.validate_files_node.result", issue_count=len(issues))
            emit_event(
                owner="sample_app",
                phase="validate_files",
                status="completed" if not issues else "failed",
                message="소스 정적 검증이 완료되었습니다." if not issues else "소스 정적 검증에서 이슈가 발견되었습니다.",
                details={"issue_count": len(issues), "issues": [item.model_dump(mode="json") for item in issues]},
            )
            return {
                "validation_issues": issues,
                "executed_commands": [f"code_validator --path {project_dir}"],
                "notes": ["Static validation completed." if not issues else "Static validation found issues."],
            }

    def _route_after_validation(self, state: SampleAppState) -> str:
        issues = state.get("validation_issues", [])
        if not issues:
            return "package_artifacts"
        if state.get("repair_round", 0) < self.max_repairs:
            return "repair_files"
        return "finalize"

    def _repair_files_node(self, state: SampleAppState) -> SampleAppState:
        request = state["request"]
        plan = state["plan"]
        with timed_step(logger, "sample_app_gen.repair_files_node", app_id=plan.app_id, repair_round=state.get("repair_round", 0) + 1):
            emit_event(owner="sample_app", phase="repair_files", status="started", message="검증 이슈 보정을 시작합니다.", details={"repair_round": state.get("repair_round", 0) + 1})
            project_dir = Path(plan.project_dir)
            existing_files = dict(state.get("existing_files", {}))
            issues = state.get("validation_issues", [])
            by_path = {item.path: [] for item in issues}
            for issue in issues:
                by_path.setdefault(issue.path, []).append(issue)

            for file_plan in plan.file_plan:
                if file_plan.path not in by_path:
                    continue
                with timed_step(logger, "sample_app_gen.repair_file_step", path=file_plan.path):
                    emit_event(owner="sample_app", phase="repair_file", status="progress", message="파일 보정을 수행합니다.", details={"path": file_plan.path})
                    repaired = self._deterministic_file_content(request, plan, file_plan)
                    if repaired is None:
                        current_content = existing_files.get(file_plan.path, "")
                        repaired = self.llm.repair_file(
                            request=request,
                            plan=plan,
                            file_plan=file_plan,
                            current_content=current_content,
                            issues=by_path[file_plan.path],
                            existing_files=existing_files,
                        )
                        if repaired is None:
                            repaired = self._fallback_file_content(request, plan, file_plan)
                            log_event(logger, "sample_app_gen.repair_file_step.fallback", path=file_plan.path)
                    else:
                        log_event(logger, "sample_app_gen.repair_file_step.template", path=file_plan.path)
                    path = project_dir / file_plan.path
                    self.tools.call("execution_file_write", path=path, content=repaired, overwrite=True)
                    existing_files[file_plan.path] = repaired

            return {
                "existing_files": existing_files,
                "repair_round": state.get("repair_round", 0) + 1,
                "notes": [f"Repair round {state.get('repair_round', 0) + 1} completed."],
            }

    def _package_artifacts_node(self, state: SampleAppState) -> SampleAppState:
        plan = state["plan"]
        with timed_step(logger, "sample_app_gen.package_artifacts_node", app_id=plan.app_id):
            request = state["request"]
            emit_event(owner="sample_app", phase="package_artifacts", status="started", message="아티팩트 패키징과 이미지 배포를 시작합니다.", details={"app_id": plan.app_id})
            project_dir = Path(plan.project_dir)
            dist_dir = self.workspace_root / "artifacts"
            archive_base = dist_dir / plan.app_id
            build = self.tools.call("build_code", project_dir=project_dir, output_base=archive_base)
            if not build["ok"]:
                emit_event(
                    owner="sample_app",
                    phase="package_artifacts",
                    status="failed",
                    message="애플리케이션 빌드 단계에서 실패했습니다.",
                    details={"error_code": build["error_code"]},
                )
                return {
                    "executed_commands": [f"build_code --path {project_dir} --output {archive_base}.jar"],
                    "notes": [
                        f"BUILD_ERROR: {build['error_code']}",
                        f"BUILD_STDERR: {build['stderr'][:4000]}" if build["stderr"] else "BUILD_STDERR: none",
                    ],
                    "success": False,
                }
            archive_path = build["output_path"]
            docker = self.tools.call(
                "docker_build",
                project_dir=project_dir,
                image_name=plan.image_name,
                request=request,
                output_dir=dist_dir,
                tag="latest",
            )

            recommended = [f"APP_LOG_DIR={plan.log_dir}", *plan.required_env]
            if plan.language.lower().startswith("java") and plan.gc_log_dir:
                recommended.append(f"JAVA_TOOL_OPTIONS=-Xlog:gc*:file={plan.gc_log_dir}/gc.log")

            notes = [
                f"DEPLOY_CMD: {plan.deployment_commands[0] if plan.deployment_commands else 'docker run ...'}",
                f"Application jar prepared at {archive_path}.",
            ]
            generated_outputs = [
                "배포 가이드라인 및 API 문서",
                f"application jar: {archive_path}",
            ]
            rollback_cleanup = [*state.get("rollback_cleanup", []), f"rm -f {archive_path}"]
            executed_commands = [
                f"build_code --path {project_dir} --output {archive_path}",
                docker["command_label"],
            ]

            if docker["ok"]:
                notes.extend(
                    [
                        f"IMAGE_REF: {docker['image_ref']}",
                        "Docker image was built locally and streamed to the target host via docker save | ssh docker load.",
                    ]
                )
                generated_outputs.append(f"container image: {docker['image_ref']}")
                rollback_cleanup.append(f"docker image rm {docker['image_ref']}")
            else:
                notes.extend(
                    [
                        f"DOCKER_UPLOAD_ERROR: {docker['error_code']}",
                        f"DOCKER_UPLOAD_STDERR: {docker['stderr'][:4000]}" if docker["stderr"] else "DOCKER_UPLOAD_STDERR: none",
                    ]
                )

            if self.llm.is_available:
                notes.append("LLM generated the application plan and source files.")
            else:
                notes.append("Azure OpenAI is not configured, so fallback generation was used.")

            emit_event(
                owner="sample_app",
                phase="package_artifacts",
                status="completed" if docker["ok"] else "failed",
                message="아티팩트 패키징과 이미지 배포가 완료되었습니다." if docker["ok"] else "이미지 배포 단계에서 실패했습니다.",
                details={"archive_path": archive_path, "image_ref": docker.get("image_ref", ""), "error_code": docker.get("error_code", "")},
            )

            return {
                "executed_commands": executed_commands,
                "generated_outputs": generated_outputs,
                "recommended_config": recommended,
                "rollback_cleanup": rollback_cleanup,
                "notes": notes,
                "success": docker["ok"],
            }

    def _finalize_node(self, state: SampleAppState) -> SampleAppState:
        if state.get("validation_issues"):
            return {"success": state.get("success", False)}
        return {"success": state.get("success", False)}

    def _resolve_language(self, framework: str, languages: list[str]) -> str:
        normalized_framework = framework.strip().lower()
        if normalized_framework == "fastapi":
            return next((item for item in languages if item.lower().startswith("python")), "Python3.12")
        if normalized_framework in {"spring", "spring boot"}:
            return next((item for item in languages if item.lower().startswith("java")), "Java17")
        raise UnsupportedStackError(f"Unsupported framework: {framework}")

    def _slugify(self, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return normalized[:80] or "sample-app"

    def _fallback_plan(self, request: UserRequest, project_dir: Path, app_id: str, language: str) -> ApplicationPlan:
        framework = request.app_tech_stack.framework.strip() or "FastAPI"
        runtime_version = self._runtime_version(language)
        is_java = language.lower().startswith("java")
        build_system = self._resolve_build_system(request=request, framework=framework)
        port = "8080" if is_java else "8000"
        file_plan = self._fallback_file_plan(framework, build_system=build_system)
        return ApplicationPlan(
            app_id=app_id,
            framework=framework,
            framework_version=request.app_tech_stack.minor_version or "latest",
            language=language,
            build_system=build_system,
            runtime_version=runtime_version,
            artifact_type="jar" if is_java else "zip",
            artifact_name=f"{app_id}.{'jar' if is_java else 'zip'}",
            image_name=f"sample-app/{app_id}:latest",
            project_dir=str(project_dir),
            log_dir=request.logging.app_log_dir,
            gc_log_dir=request.logging.gc_log_dir,
            special_scenarios=self._detect_special_scenarios(request.additional_request),
            deployment_commands=[self._deployment_command(port, request, f"sample-app/{app_id}:latest")],
            required_env=self._required_env(request),
            file_plan=file_plan,
            spec_markdown=self._fallback_spec_markdown(request, framework, language, app_id, file_plan, build_system),
        )

    def _normalize_plan(self, plan: ApplicationPlan, request: UserRequest, project_dir: Path, app_id: str) -> ApplicationPlan:
        language = self._resolve_language(plan.framework, [plan.language] if plan.language else [])
        is_java = language.lower().startswith("java")
        build_system = self._resolve_build_system(request=request, framework=plan.framework)
        runtime_version = self._normalized_runtime_version(
            plan.runtime_version or self._runtime_version(language) or ("17" if is_java else "3.12"),
            default="17" if is_java else "3.12",
        )
        file_plan = list(plan.file_plan) if plan.file_plan else self._fallback_file_plan(plan.framework, build_system=build_system)
        file_plan = self._sanitize_file_plan(file_plan, framework=plan.framework, build_system=build_system)
        file_plan = self._ensure_required_file_plan(file_plan, app_id=app_id, framework=plan.framework, build_system=build_system)

        normalized_artifact_name = f"{app_id}.jar" if is_java else f"{app_id}.zip"
        return plan.model_copy(
            update={
                "app_id": app_id,
                "language": language,
                "build_system": build_system,
                "runtime_version": runtime_version,
                "artifact_type": "jar" if is_java else "zip",
                "artifact_name": normalized_artifact_name,
                "project_dir": str(project_dir),
                "file_plan": file_plan,
            }
        )

    def _sanitize_file_plan(
        self,
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

    def _ensure_required_file_plan(
        self,
        file_plan: list[ApplicationFilePlan],
        app_id: str,
        framework: str,
        build_system: str,
    ) -> list[ApplicationFilePlan]:
        existing_paths = {item.path for item in file_plan}
        normalized_framework = framework.strip().lower()
        required: list[ApplicationFilePlan] = []
        main_class_path = self._spring_main_class_path(app_id)

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
                    ApplicationFilePlan(
                        path="src/main/resources/application.yml",
                        purpose="Application config",
                        language="yaml",
                    ),
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
                    ApplicationFilePlan(
                        path=main_class_path,
                        purpose="Spring Boot entrypoint",
                        language="java",
                    )
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

    def _fallback_file_plan(self, framework: str, build_system: str = "maven") -> list[ApplicationFilePlan]:
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

    def _fallback_file_content(self, request: UserRequest, plan: ApplicationPlan, file_plan: ApplicationFilePlan) -> str:
        path = file_plan.path
        if path == "requirements.txt":
            return "fastapi\nuvicorn[standard]\n"
        if path == ".env.example":
            return "\n".join(self._required_env(request) + [f"APP_LOG_DIR={plan.log_dir}"]) + "\n"
        if path == "Dockerfile":
            deterministic = self._deterministic_file_content(request, plan, file_plan)
            if deterministic is not None:
                return deterministic
        if path == "README.md":
            return f"# Generated Sample App\n\n- framework: {plan.framework}\n- language: {plan.language}\n"
        if path == "app/main.py":
            return self._fallback_fastapi_main(request, plan)
        if path == "pom.xml":
            return self._fallback_pom(plan)
        if path == "build.gradle":
            return self._fallback_gradle_build(plan)
        if path == "settings.gradle":
            return f"rootProject.name = '{plan.app_id}'\n"
        if path.endswith("SampleAppApplication.java"):
            return (
                "package com.example.sampleapp;\n\n"
                "import org.springframework.boot.SpringApplication;\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
                "@SpringBootApplication\n"
                "public class SampleAppApplication {\n"
                "    public static void main(String[] args) {\n"
                "        SpringApplication.run(SampleAppApplication.class, args);\n"
                "    }\n"
                "}\n"
            )
        if path.endswith("DemoController.java"):
            return (
                "package com.example.sampleapp;\n\n"
                "import java.util.Map;\n"
                "import org.springframework.web.bind.annotation.GetMapping;\n"
                "import org.springframework.web.bind.annotation.RestController;\n\n"
                "@RestController\n"
                "public class DemoController {\n"
                "    @GetMapping(\"/health\")\n"
                "    public Map<String, Object> health() {\n"
                f"        return Map.of(\"status\", \"ok\", \"framework\", \"{plan.framework}\");\n"
                "    }\n"
                "}\n"
            )
        if path.endswith("application.yml"):
            return f"server:\n  port: 8080\nlogging:\n  file:\n    name: {plan.log_dir}/application.log\n"
        return ""

    def _deterministic_file_content(
        self,
        request: UserRequest,
        plan: ApplicationPlan,
        file_plan: ApplicationFilePlan,
    ) -> str | None:
        path = file_plan.path
        if path == "Dockerfile":
            return self._render_dockerfile_template(plan)
        if path == "pom.xml":
            return self._fallback_pom(plan)
        if path == "build.gradle":
            return self._fallback_gradle_build(plan)
        if path == "settings.gradle":
            return f"rootProject.name = '{plan.app_id}'\n"
        if path == "gradlew":
            return self._load_template(TEMPLATE_DIR / "gradlew.tmpl").template + "\n"
        if path == "gradlew.bat":
            return self._load_template(TEMPLATE_DIR / "gradlew.bat.tmpl").template + "\n"
        if path == "gradle/wrapper/gradle-wrapper.properties":
            return self._load_template(TEMPLATE_DIR / "gradle-wrapper.properties.tmpl").substitute(
                GRADLE_VERSION=self._gradle_version_for_plan(plan)
            ) + "\n"
        if path == self._spring_main_class_path(plan.app_id):
            return (
                "package com.example.sampleapp;\n\n"
                "import org.springframework.boot.SpringApplication;\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
                "@SpringBootApplication\n"
                f"public class {self._spring_main_class_name(plan.app_id)} {{\n"
                "    public static void main(String[] args) {\n"
                f"        SpringApplication.run({self._spring_main_class_name(plan.app_id)}.class, args);\n"
                "    }\n"
                "}\n"
            )
        if path == "src/main/resources/application.yml":
            return f"server:\n  port: 8080\nlogging:\n  file:\n    name: {plan.log_dir}/application.log\n"
        if path == "src/main/java/com/example/board/BoardApplication.java":
            return self._spring_board_application(plan)
        if path == "src/main/java/com/example/board/controller/BoardController.java":
            return self._spring_board_controller()
        if path == "src/main/java/com/example/board/service/PostService.java":
            return self._spring_post_service()
        if path == "src/main/java/com/example/board/repository/InMemoryPostRepository.java":
            return self._spring_post_repository()
        if path == "src/main/java/com/example/board/model/Post.java":
            return self._spring_post_model()
        if path == "src/main/java/com/example/board/dto/PostRequest.java":
            return self._spring_post_request()
        if path == "src/main/java/com/example/board/dto/PostResponse.java":
            return self._spring_post_response()
        if path == "src/main/java/com/example/board/exception/NotFoundException.java":
            return self._spring_not_found_exception()
        if path == "src/main/java/com/example/board/exception/GlobalExceptionHandler.java":
            return self._spring_global_exception_handler()
        if path == "src/test/java/com/example/board/BoardControllerTest.java":
            return self._spring_board_controller_test()
        return None

    def _spring_main_class_name(self, app_id: str) -> str:
        words = [chunk for chunk in re.split(r"[^a-zA-Z0-9]+", app_id) if chunk]
        base = "".join(word[:1].upper() + word[1:] for word in words) or "SampleApp"
        return base if base.endswith("Application") else f"{base}Application"

    def _spring_main_class_path(self, app_id: str) -> str:
        return f"src/main/java/com/example/sampleapp/{self._spring_main_class_name(app_id)}.java"

    def _render_dockerfile_template(self, plan: ApplicationPlan) -> str:
        framework = plan.framework.strip().lower()
        if framework == "fastapi":
            template_path = TEMPLATE_DIR / "docker_template_python_fastapi.tmpl"
            return self._load_template(template_path).substitute(
                PYTHON_VERSION=self._normalized_runtime_version(plan.runtime_version, default="3.12")
            ) + "\n"

        template_name = "docker_template_java_gradle.tmpl" if plan.build_system == "gradle" else "docker_template_java_maven.tmpl"
        template_path = TEMPLATE_DIR / template_name
        artifact_name = plan.artifact_name if plan.artifact_name.endswith(".jar") else f"{plan.app_id}.jar"
        return self._load_template(template_path).substitute(
            JAVA_VERSION=self._normalized_runtime_version(plan.runtime_version, default="17"),
            ARTIFACT_NAME=artifact_name,
            GRADLE_VERSION=self._gradle_version_for_plan(plan),
        ) + "\n"

    def _gradle_version_for_plan(self, plan: ApplicationPlan) -> str:
        if "4.0" in plan.framework_version:
            return "8.14"
        return DEFAULT_GRADLE_VERSION

    def _load_template(self, path: Path) -> Template:
        return Template(path.read_text(encoding="utf-8"))

    def _fallback_fastapi_main(self, request: UserRequest, plan: ApplicationPlan) -> str:
        leak_block = ""
        if "memory_leak" in plan.special_scenarios:
            leak_block = (
                "\nLEAK_BUCKET: list[bytes] = []\n\n"
                "@app.post('/api/v1/scenario/leak')\n"
                "def leak():\n"
                "    LEAK_BUCKET.append(b'x' * 1024 * 1024)\n"
                "    return {'chunks': len(LEAK_BUCKET)}\n"
            )
        return (
            "from __future__ import annotations\n\n"
            "import os\nfrom pathlib import Path\n\n"
            "from fastapi import FastAPI\n\n"
            "app = FastAPI(title='Generated Sample App', version='0.1.0')\n"
            f"APP_LOG_DIR = Path(os.getenv('APP_LOG_DIR', '{request.logging.app_log_dir}'))\n"
            "try:\n    APP_LOG_DIR.mkdir(parents=True, exist_ok=True)\nexcept OSError:\n    pass\n\n"
            "@app.get('/health')\n"
            "def health():\n"
            f"    return {{'status': 'ok', 'framework': '{plan.framework}'}}\n"
            f"{leak_block}\n"
        )

    def _fallback_pom(self, plan: ApplicationPlan) -> str:
        version = "3.5.0"
        if "4.0" in plan.framework_version:
            version = "4.0.0"
        elif "3.0" in plan.framework_version:
            version = "3.0.0"
        return (
            "<project xmlns=\"http://maven.apache.org/POM/4.0.0\" "
            "xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" "
            "xsi:schemaLocation=\"http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd\">\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>com.example</groupId>\n"
            f"  <artifactId>{plan.app_id}</artifactId>\n"
            "  <version>0.0.1-SNAPSHOT</version>\n"
            "  <parent>\n"
            "    <groupId>org.springframework.boot</groupId>\n"
            "    <artifactId>spring-boot-starter-parent</artifactId>\n"
            f"    <version>{version}</version>\n"
            "    <relativePath/>\n"
            "  </parent>\n"
            "  <properties>\n"
            f"    <java.version>{self._normalized_runtime_version(plan.runtime_version, default='17')}</java.version>\n"
            "  </properties>\n"
            "  <dependencies>\n"
            "    <dependency>\n"
            "      <groupId>org.springframework.boot</groupId>\n"
            "      <artifactId>spring-boot-starter-web</artifactId>\n"
            "    </dependency>\n"
            "  </dependencies>\n"
            "</project>\n"
        )

    def _fallback_spec_markdown(
        self,
        request: UserRequest,
        framework: str,
        language: str,
        app_id: str,
        file_plan: list[ApplicationFilePlan],
        build_system: str,
    ) -> str:
        files = "\n".join(f"- `{item.path}`: {item.purpose}" for item in file_plan)
        scenarios = "\n".join(f"- {item}" for item in self._detect_special_scenarios(request.additional_request)) or "- none"
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

    def _detect_special_scenarios(self, additional_request: str) -> list[str]:
        value = additional_request.lower()
        scenarios: list[str] = []
        if any(token in value for token in ["memory leak", "메모리 릭", "threadlocal"]):
            scenarios.append("memory_leak")
        if any(token in value for token in ["oom", "out of memory", "outofmemory"]):
            scenarios.append("oom")
        return scenarios

    def _required_env(self, request: UserRequest) -> list[str]:
        envs: list[str] = []
        if request.app_tech_stack.databases and request.app_tech_stack.databases.lower() != "none":
            envs.extend(["APP_DB_HOST=", "APP_DB_PORT=", "APP_DB_NAME=", "APP_DB_USER=", "APP_DB_PASSWORD="])
        return envs

    def _deployment_command(self, port: str, request: UserRequest, image_name: str) -> str:
        args = [f"-e APP_LOG_DIR={request.logging.app_log_dir}"]
        if request.app_tech_stack.databases and request.app_tech_stack.databases.lower() != "none":
            args.append("-e APP_DB_PASSWORD=${APP_DB_PASSWORD}")
        return f"docker run -d -p {port}:{port} {' '.join(args)} {image_name}".strip()

    def _runtime_version(self, language: str) -> str:
        values = re.findall(r"\d+(?:\.\d+)?", language)
        return values[0] if values else ""

    def _normalized_runtime_version(self, value: str, default: str) -> str:
        extracted = self._runtime_version(value or "")
        return extracted or default

    def _resolve_build_system(self, request: UserRequest, framework: str) -> str:
        if framework.strip().lower() not in {"spring", "spring boot"}:
            return "maven"
        requested = str(getattr(request.app_tech_stack, "build_system", "")).strip().lower()
        if requested in {"maven", "gradle"}:
            return requested
        if "gradle" in request.additional_request.lower():
            return "gradle"
        return "maven"

    def _fallback_gradle_build(self, plan: ApplicationPlan) -> str:
        spring_boot_version = "3.5.0"
        if "4.0" in plan.framework_version:
            spring_boot_version = "4.0.0"
        elif "3.0" in plan.framework_version:
            spring_boot_version = "3.0.0"
        java_version = self._normalized_runtime_version(plan.runtime_version, default="17")
        return (
            "plugins {\n"
            "    id 'java'\n"
            "    id 'org.springframework.boot' version '" + spring_boot_version + "'\n"
            "    id 'io.spring.dependency-management' version '1.1.7'\n"
            "}\n\n"
            "group = 'com.example'\n"
            "version = '0.0.1-SNAPSHOT'\n\n"
            "java {\n"
            "    toolchain {\n"
            "        languageVersion = JavaLanguageVersion.of(" + java_version + ")\n"
            "    }\n"
            "}\n\n"
            "repositories {\n"
            "    mavenCentral()\n"
            "}\n\n"
            "dependencies {\n"
            "    implementation 'org.springframework.boot:spring-boot-starter-web'\n"
            "    implementation 'org.springframework.boot:spring-boot-starter-validation'\n"
            "    testImplementation 'org.springframework.boot:spring-boot-starter-test'\n"
            "}\n\n"
            "tasks.named('test') {\n"
            "    useJUnitPlatform()\n"
            "}\n"
        )

    def _spring_board_application(self, plan: ApplicationPlan) -> str:
        return (
            "package com.example.board;\n\n"
            "import org.springframework.boot.SpringApplication;\n"
            "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
            "@SpringBootApplication\n"
            "public class BoardApplication {\n"
            "    public static void main(String[] args) {\n"
            "        SpringApplication.run(BoardApplication.class, args);\n"
            "    }\n"
            "}\n"
        )

    def _spring_post_model(self) -> str:
        return (
            "package com.example.board.model;\n\n"
            "import java.time.Instant;\n\n"
            "public class Post {\n"
            "    private Long id;\n"
            "    private String title;\n"
            "    private String content;\n"
            "    private String author;\n"
            "    private Instant createdAt;\n"
            "    private Instant updatedAt;\n\n"
            "    public Post() {\n"
            "    }\n\n"
            "    public Post(String title, String content, String author) {\n"
            "        this(null, title, content, author, null, null);\n"
            "    }\n\n"
            "    public Post(Long id, String title, String content, String author, Instant createdAt, Instant updatedAt) {\n"
            "        this.id = id;\n"
            "        this.title = title;\n"
            "        this.content = content;\n"
            "        this.author = author;\n"
            "        this.createdAt = createdAt;\n"
            "        this.updatedAt = updatedAt;\n"
            "    }\n\n"
            "    public Long getId() {\n"
            "        return id;\n"
            "    }\n\n"
            "    public void setId(Long id) {\n"
            "        this.id = id;\n"
            "    }\n\n"
            "    public String getTitle() {\n"
            "        return title;\n"
            "    }\n\n"
            "    public void setTitle(String title) {\n"
            "        this.title = title;\n"
            "    }\n\n"
            "    public String getContent() {\n"
            "        return content;\n"
            "    }\n\n"
            "    public void setContent(String content) {\n"
            "        this.content = content;\n"
            "    }\n\n"
            "    public String getAuthor() {\n"
            "        return author;\n"
            "    }\n\n"
            "    public void setAuthor(String author) {\n"
            "        this.author = author;\n"
            "    }\n\n"
            "    public Instant getCreatedAt() {\n"
            "        return createdAt;\n"
            "    }\n\n"
            "    public void setCreatedAt(Instant createdAt) {\n"
            "        this.createdAt = createdAt;\n"
            "    }\n\n"
            "    public Instant getUpdatedAt() {\n"
            "        return updatedAt;\n"
            "    }\n\n"
            "    public void setUpdatedAt(Instant updatedAt) {\n"
            "        this.updatedAt = updatedAt;\n"
            "    }\n"
            "}\n"
        )

    def _spring_post_request(self) -> str:
        return (
            "package com.example.board.dto;\n\n"
            "import com.example.board.model.Post;\n"
            "import jakarta.validation.constraints.NotBlank;\n"
            "import jakarta.validation.constraints.Size;\n\n"
            "public class PostRequest {\n"
            "    @NotBlank\n"
            "    @Size(max = 200)\n"
            "    private String title;\n\n"
            "    @NotBlank\n"
            "    @Size(max = 10000)\n"
            "    private String content;\n\n"
            "    @NotBlank\n"
            "    @Size(max = 100)\n"
            "    private String author;\n\n"
            "    public String getTitle() {\n"
            "        return title;\n"
            "    }\n\n"
            "    public void setTitle(String title) {\n"
            "        this.title = title;\n"
            "    }\n\n"
            "    public String getContent() {\n"
            "        return content;\n"
            "    }\n\n"
            "    public void setContent(String content) {\n"
            "        this.content = content;\n"
            "    }\n\n"
            "    public String getAuthor() {\n"
            "        return author;\n"
            "    }\n\n"
            "    public void setAuthor(String author) {\n"
            "        this.author = author;\n"
            "    }\n\n"
            "    public Post toModel() {\n"
            "        return new Post(title, content, author);\n"
            "    }\n"
            "}\n"
        )

    def _spring_post_response(self) -> str:
        return (
            "package com.example.board.dto;\n\n"
            "import com.example.board.model.Post;\n\n"
            "import java.time.Instant;\n\n"
            "public class PostResponse {\n"
            "    private Long id;\n"
            "    private String title;\n"
            "    private String content;\n"
            "    private String author;\n"
            "    private Instant createdAt;\n"
            "    private Instant updatedAt;\n\n"
            "    public static PostResponse fromModel(Post post) {\n"
            "        PostResponse response = new PostResponse();\n"
            "        response.setId(post.getId());\n"
            "        response.setTitle(post.getTitle());\n"
            "        response.setContent(post.getContent());\n"
            "        response.setAuthor(post.getAuthor());\n"
            "        response.setCreatedAt(post.getCreatedAt());\n"
            "        response.setUpdatedAt(post.getUpdatedAt());\n"
            "        return response;\n"
            "    }\n\n"
            "    public Long getId() {\n"
            "        return id;\n"
            "    }\n\n"
            "    public void setId(Long id) {\n"
            "        this.id = id;\n"
            "    }\n\n"
            "    public String getTitle() {\n"
            "        return title;\n"
            "    }\n\n"
            "    public void setTitle(String title) {\n"
            "        this.title = title;\n"
            "    }\n\n"
            "    public String getContent() {\n"
            "        return content;\n"
            "    }\n\n"
            "    public void setContent(String content) {\n"
            "        this.content = content;\n"
            "    }\n\n"
            "    public String getAuthor() {\n"
            "        return author;\n"
            "    }\n\n"
            "    public void setAuthor(String author) {\n"
            "        this.author = author;\n"
            "    }\n\n"
            "    public Instant getCreatedAt() {\n"
            "        return createdAt;\n"
            "    }\n\n"
            "    public void setCreatedAt(Instant createdAt) {\n"
            "        this.createdAt = createdAt;\n"
            "    }\n\n"
            "    public Instant getUpdatedAt() {\n"
            "        return updatedAt;\n"
            "    }\n\n"
            "    public void setUpdatedAt(Instant updatedAt) {\n"
            "        this.updatedAt = updatedAt;\n"
            "    }\n"
            "}\n"
        )

    def _spring_post_repository(self) -> str:
        return (
            "package com.example.board.repository;\n\n"
            "import com.example.board.model.Post;\n"
            "import org.springframework.stereotype.Repository;\n\n"
            "import java.time.Instant;\n"
            "import java.util.ArrayList;\n"
            "import java.util.Comparator;\n"
            "import java.util.List;\n"
            "import java.util.Optional;\n"
            "import java.util.concurrent.ConcurrentHashMap;\n"
            "import java.util.concurrent.atomic.AtomicLong;\n\n"
            "@Repository\n"
            "public class InMemoryPostRepository {\n"
            "    private final ConcurrentHashMap<Long, Post> store = new ConcurrentHashMap<>();\n"
            "    private final AtomicLong sequence = new AtomicLong();\n\n"
            "    public List<Post> findAll() {\n"
            "        List<Post> posts = new ArrayList<>();\n"
            "        for (Post value : store.values()) {\n"
            "            posts.add(copy(value));\n"
            "        }\n"
            "        posts.sort(Comparator.comparing(Post::getId));\n"
            "        return posts;\n"
            "    }\n\n"
            "    public Optional<Post> findById(Long id) {\n"
            "        Post post = store.get(id);\n"
            "        return post == null ? Optional.empty() : Optional.of(copy(post));\n"
            "    }\n\n"
            "    public Post create(Post post) {\n"
            "        long id = sequence.incrementAndGet();\n"
            "        Instant now = Instant.now();\n"
            "        Post stored = copy(post);\n"
            "        stored.setId(id);\n"
            "        stored.setCreatedAt(now);\n"
            "        stored.setUpdatedAt(now);\n"
            "        store.put(id, stored);\n"
            "        return copy(stored);\n"
            "    }\n\n"
            "    public Optional<Post> update(Long id, Post post) {\n"
            "        Post updated = store.computeIfPresent(id, (key, existing) -> {\n"
            "            Post next = copy(post);\n"
            "            next.setId(id);\n"
            "            next.setCreatedAt(existing.getCreatedAt());\n"
            "            next.setUpdatedAt(Instant.now());\n"
            "            return next;\n"
            "        });\n"
            "        return updated == null ? Optional.empty() : Optional.of(copy(updated));\n"
            "    }\n\n"
            "    public boolean delete(Long id) {\n"
            "        return store.remove(id) != null;\n"
            "    }\n\n"
            "    private Post copy(Post source) {\n"
            "        return new Post(\n"
            "            source.getId(),\n"
            "            source.getTitle(),\n"
            "            source.getContent(),\n"
            "            source.getAuthor(),\n"
            "            source.getCreatedAt(),\n"
            "            source.getUpdatedAt()\n"
            "        );\n"
            "    }\n"
            "}\n"
        )

    def _spring_post_service(self) -> str:
        return (
            "package com.example.board.service;\n\n"
            "import com.example.board.dto.PostRequest;\n"
            "import com.example.board.dto.PostResponse;\n"
            "import com.example.board.model.Post;\n"
            "import com.example.board.repository.InMemoryPostRepository;\n"
            "import org.springframework.stereotype.Service;\n\n"
            "import java.util.List;\n"
            "import java.util.Optional;\n"
            "import java.util.stream.Collectors;\n\n"
            "@Service\n"
            "public class PostService {\n"
            "    private final InMemoryPostRepository repository;\n\n"
            "    public PostService(InMemoryPostRepository repository) {\n"
            "        this.repository = repository;\n"
            "    }\n\n"
            "    public List<PostResponse> findAll() {\n"
            "        return repository.findAll().stream().map(PostResponse::fromModel).collect(Collectors.toList());\n"
            "    }\n\n"
            "    public Optional<PostResponse> findById(Long id) {\n"
            "        return repository.findById(id).map(PostResponse::fromModel);\n"
            "    }\n\n"
            "    public PostResponse create(PostRequest request) {\n"
            "        return PostResponse.fromModel(repository.create(request.toModel()));\n"
            "    }\n\n"
            "    public Optional<PostResponse> update(Long id, PostRequest request) {\n"
            "        return repository.findById(id)\n"
            "            .map(existing -> {\n"
            "                Post updated = new Post(\n"
            "                    id,\n"
            "                    request.getTitle(),\n"
            "                    request.getContent(),\n"
            "                    request.getAuthor(),\n"
            "                    existing.getCreatedAt(),\n"
            "                    existing.getUpdatedAt()\n"
            "                );\n"
            "                return repository.update(id, updated);\n"
            "            })\n"
            "            .flatMap(optional -> optional)\n"
            "            .map(PostResponse::fromModel);\n"
            "    }\n\n"
            "    public boolean delete(Long id) {\n"
            "        return repository.delete(id);\n"
            "    }\n"
            "}\n"
        )

    def _spring_board_controller(self) -> str:
        return (
            "package com.example.board.controller;\n\n"
            "import com.example.board.dto.PostRequest;\n"
            "import com.example.board.dto.PostResponse;\n"
            "import com.example.board.exception.NotFoundException;\n"
            "import com.example.board.service.PostService;\n"
            "import jakarta.validation.Valid;\n"
            "import org.springframework.http.ResponseEntity;\n"
            "import org.springframework.web.bind.annotation.DeleteMapping;\n"
            "import org.springframework.web.bind.annotation.GetMapping;\n"
            "import org.springframework.web.bind.annotation.PathVariable;\n"
            "import org.springframework.web.bind.annotation.PostMapping;\n"
            "import org.springframework.web.bind.annotation.PutMapping;\n"
            "import org.springframework.web.bind.annotation.RequestBody;\n"
            "import org.springframework.web.bind.annotation.RequestMapping;\n"
            "import org.springframework.web.bind.annotation.RestController;\n"
            "import org.springframework.web.servlet.support.ServletUriComponentsBuilder;\n\n"
            "import java.net.URI;\n"
            "import java.util.List;\n\n"
            "@RestController\n"
            "@RequestMapping(\"/api/posts\")\n"
            "public class BoardController {\n"
            "    private final PostService postService;\n\n"
            "    public BoardController(PostService postService) {\n"
            "        this.postService = postService;\n"
            "    }\n\n"
            "    @GetMapping\n"
            "    public List<PostResponse> listAll() {\n"
            "        return postService.findAll();\n"
            "    }\n\n"
            "    @GetMapping(\"/{id}\")\n"
            "    public PostResponse getById(@PathVariable Long id) {\n"
            "        return postService.findById(id).orElseThrow(() -> new NotFoundException(\"Post not found: \" + id));\n"
            "    }\n\n"
            "    @PostMapping\n"
            "    public ResponseEntity<PostResponse> create(@Valid @RequestBody PostRequest request) {\n"
            "        PostResponse created = postService.create(request);\n"
            "        URI location = ServletUriComponentsBuilder.fromCurrentRequest()\n"
            "            .path(\"/{id}\")\n"
            "            .buildAndExpand(created.getId())\n"
            "            .toUri();\n"
            "        return ResponseEntity.created(location).body(created);\n"
            "    }\n\n"
            "    @PutMapping(\"/{id}\")\n"
            "    public PostResponse update(@PathVariable Long id, @Valid @RequestBody PostRequest request) {\n"
            "        return postService.update(id, request).orElseThrow(() -> new NotFoundException(\"Post not found: \" + id));\n"
            "    }\n\n"
            "    @DeleteMapping(\"/{id}\")\n"
            "    public ResponseEntity<Void> delete(@PathVariable Long id) {\n"
            "        if (!postService.delete(id)) {\n"
            "            throw new NotFoundException(\"Post not found: \" + id);\n"
            "        }\n"
            "        return ResponseEntity.noContent().build();\n"
            "    }\n"
            "}\n"
        )

    def _spring_not_found_exception(self) -> str:
        return (
            "package com.example.board.exception;\n\n"
            "public class NotFoundException extends RuntimeException {\n"
            "    public NotFoundException(String message) {\n"
            "        super(message);\n"
            "    }\n"
            "}\n"
        )

    def _spring_global_exception_handler(self) -> str:
        return (
            "package com.example.board.exception;\n\n"
            "import jakarta.servlet.http.HttpServletRequest;\n"
            "import org.springframework.http.HttpStatus;\n"
            "import org.springframework.http.ResponseEntity;\n"
            "import org.springframework.validation.FieldError;\n"
            "import org.springframework.web.bind.MethodArgumentNotValidException;\n"
            "import org.springframework.web.bind.annotation.ExceptionHandler;\n"
            "import org.springframework.web.bind.annotation.RestControllerAdvice;\n\n"
            "import java.time.Instant;\n"
            "import java.util.LinkedHashMap;\n"
            "import java.util.List;\n"
            "import java.util.Map;\n\n"
            "@RestControllerAdvice(basePackages = \"com.example.board\")\n"
            "public class GlobalExceptionHandler {\n"
            "    public record ErrorResponse(\n"
            "        Instant timestamp,\n"
            "        int status,\n"
            "        String error,\n"
            "        String message,\n"
            "        String path,\n"
            "        Object details\n"
            "    ) {\n"
            "    }\n\n"
            "    @ExceptionHandler(NotFoundException.class)\n"
            "    public ResponseEntity<ErrorResponse> handleNotFound(NotFoundException ex, HttpServletRequest request) {\n"
            "        return build(HttpStatus.NOT_FOUND, ex.getMessage(), request, null);\n"
            "    }\n\n"
            "    @ExceptionHandler(MethodArgumentNotValidException.class)\n"
            "    public ResponseEntity<ErrorResponse> handleValidation(MethodArgumentNotValidException ex, HttpServletRequest request) {\n"
            "        List<Map<String, Object>> fieldErrors = ex.getBindingResult()\n"
            "            .getFieldErrors()\n"
            "            .stream()\n"
            "            .map(this::toFieldError)\n"
            "            .toList();\n"
            "        return build(HttpStatus.BAD_REQUEST, \"Validation failed\", request, Map.of(\"fieldErrors\", fieldErrors));\n"
            "    }\n\n"
            "    @ExceptionHandler(Exception.class)\n"
            "    public ResponseEntity<ErrorResponse> handleUnexpected(Exception ex, HttpServletRequest request) {\n"
            "        return build(HttpStatus.INTERNAL_SERVER_ERROR, \"Internal server error\", request, null);\n"
            "    }\n\n"
            "    private ResponseEntity<ErrorResponse> build(HttpStatus status, String message, HttpServletRequest request, Object details) {\n"
            "        ErrorResponse body = new ErrorResponse(\n"
            "            Instant.now(),\n"
            "            status.value(),\n"
            "            status.getReasonPhrase(),\n"
            "            message,\n"
            "            request == null ? null : request.getRequestURI(),\n"
            "            details\n"
            "        );\n"
            "        return ResponseEntity.status(status).body(body);\n"
            "    }\n\n"
            "    private Map<String, Object> toFieldError(FieldError fieldError) {\n"
            "        Map<String, Object> details = new LinkedHashMap<>();\n"
            "        details.put(\"field\", fieldError.getField());\n"
            "        details.put(\"message\", fieldError.getDefaultMessage());\n"
            "        details.put(\"rejectedValue\", fieldError.getRejectedValue());\n"
            "        return details;\n"
            "    }\n"
            "}\n"
        )

    def _spring_board_controller_test(self) -> str:
        return (
            "package com.example.board;\n\n"
            "import org.junit.jupiter.api.Test;\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n"
            "import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;\n"
            "import org.springframework.boot.test.context.SpringBootTest;\n"
            "import org.springframework.http.MediaType;\n"
            "import org.springframework.test.web.servlet.MockMvc;\n\n"
            "import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;\n"
            "import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;\n"
            "import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;\n"
            "import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;\n\n"
            "@SpringBootTest\n"
            "@AutoConfigureMockMvc\n"
            "class BoardControllerTest {\n"
            "    @Autowired\n"
            "    private MockMvc mockMvc;\n\n"
            "    @Test\n"
            "    void createAndFetchPost() throws Exception {\n"
            "        String payload = \"\"\"\n"
            "            {\n"
            "              \\\"title\\\": \\\"Hello\\\",\n"
            "              \\\"content\\\": \\\"World\\\",\n"
            "              \\\"author\\\": \\\"tester\\\"\n"
            "            }\n"
            "            \"\"\";\n\n"
            "        mockMvc.perform(post(\"/api/posts\")\n"
            "                .contentType(MediaType.APPLICATION_JSON)\n"
            "                .content(payload))\n"
            "            .andExpect(status().isCreated())\n"
            "            .andExpect(jsonPath(\"$.id\").value(1));\n\n"
            "        mockMvc.perform(get(\"/api/posts/1\"))\n"
            "            .andExpect(status().isOk())\n"
            "            .andExpect(jsonPath(\"$.title\").value(\"Hello\"));\n"
            "    }\n"
            "}\n"
        )
