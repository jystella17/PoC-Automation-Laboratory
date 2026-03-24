from __future__ import annotations

import operator
import re
import shutil
from datetime import datetime
from collections import defaultdict
from pathlib import Path
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
from .plan_builder import (
    detect_special_scenarios,
    fallback_plan,
    normalize_plan,
    required_env,
    resolve_file_content,
)
from .tools import SampleAppTools

logger = get_agent_logger("sample_app_gen.agent", "sample_app_gen.log")


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
        workflow.add_conditional_edges(
            "package_artifacts",
            self._route_after_packaging,
            {
                "repair_files": "repair_files",
                "finalize": "finalize",
            },
        )
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
                plan = fallback_plan(request=request, project_dir=project_dir, app_id=app_id, language=primary_language)
                llm_mode = "fallback"
            else:
                plan = normalize_plan(plan, request=request, project_dir=project_dir, app_id=app_id, resolve_language_fn=self._resolve_language)
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
                    content = resolve_file_content(request, plan, file_plan)
                    if content is not None:
                        log_event(logger, "sample_app_gen.generate_file_step.template", path=file_plan.path)
                    else:
                        content = self.llm.generate_file(request, plan, file_plan, existing_files)
                        if content is None:
                            content = resolve_file_content(request, plan, file_plan) or ""
                            log_event(logger, "sample_app_gen.generate_file_step.fallback", path=file_plan.path)
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
            by_path: dict[str, list[ValidationIssue]] = defaultdict(list)
            for issue in issues:
                by_path[issue.path].append(issue)

            for file_plan in plan.file_plan:
                if file_plan.path not in by_path:
                    continue
                with timed_step(logger, "sample_app_gen.repair_file_step", path=file_plan.path):
                    emit_event(owner="sample_app", phase="repair_file", status="progress", message="파일 보정을 수행합니다.", details={"path": file_plan.path})
                    repaired = resolve_file_content(request, plan, file_plan)
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
                            repaired = resolve_file_content(request, plan, file_plan) or ""
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
                build_issues = self._parse_build_issues(build["stderr"], project_dir)
                return {
                    "executed_commands": [f"build_code --path {project_dir} --output {archive_base}.jar"],
                    "notes": [
                        f"BUILD_ERROR: {build['error_code']}",
                        f"BUILD_STDERR: {build['stderr'][:4000]}" if build["stderr"] else "BUILD_STDERR: none",
                    ],
                    "validation_issues": build_issues,
                    "success": False,
                }
            archive_path = build["output_path"]
            build_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
            image_name_base = plan.image_name.rsplit(":", 1)[0]
            new_image_ref = f"{image_name_base}:{build_tag}"

            run_cmd = next(
                (cmd.strip() for cmd in plan.deployment_commands if cmd.strip().startswith("docker run")),
                None,
            )
            if run_cmd:
                run_cmd = run_cmd.replace(plan.image_name, new_image_ref)
            docker = self.tools.call(
                "docker_build",
                project_dir=project_dir,
                image_name=image_name_base,
                request=request,
                output_dir=dist_dir,
                tag=build_tag,
                run_cmd=run_cmd,
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

    def _route_after_packaging(self, state: SampleAppState) -> str:
        issues = state.get("validation_issues", [])
        if issues and state.get("repair_round", 0) < self.max_repairs:
            return "repair_files"
        return "finalize"

    def _parse_build_issues(self, stderr: str, project_dir: Path) -> list[ValidationIssue]:
        """Java/Gradle 컴파일 에러 stderr를 ValidationIssue 리스트로 파싱합니다."""
        issues: list[ValidationIssue] = []
        seen: set[str] = set()
        pattern = re.compile(r"^(/[^\n:]+\.java):(\d+): error: (.+)$", re.MULTILINE)
        for match in pattern.finditer(stderr):
            raw_path, line_num, message = match.group(1), match.group(2), match.group(3).strip()
            path = raw_path
            for base in (project_dir, Path("/workspace")):
                try:
                    path = str(Path(raw_path).relative_to(base))
                    break
                except ValueError:
                    continue
            key = f"{path}:{line_num}"
            if key not in seen:
                seen.add(key)
                issues.append(ValidationIssue(path=path, message=f"Compilation error at line {line_num}: {message}"))
        if not issues and stderr.strip():
            issues.append(ValidationIssue(path="BUILD", message=f"Build failed: {stderr.strip()[:500]}"))
        return issues

    def _finalize_node(self, state: SampleAppState) -> SampleAppState:
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
