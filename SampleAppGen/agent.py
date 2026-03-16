from __future__ import annotations

import operator
import re
import shutil
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, GraphView, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step

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
                llm_mode = "llm"
            log_event(logger, "sample_app_gen.plan_spec_node.result", app_id=plan.app_id, llm_mode=llm_mode)
            return {
                "plan": plan,
                "notes": [f"APPLICATION_SPEC prepared via {llm_mode} planning."],
            }

    def _generate_files_node(self, state: SampleAppState) -> SampleAppState:
        request = state["request"]
        plan = state["plan"]
        with timed_step(logger, "sample_app_gen.generate_files_node", app_id=plan.app_id, file_count=len(plan.file_plan)):
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
                    content = self.llm.generate_file(request, plan, file_plan, existing_files)
                    if content is None:
                        content = self._fallback_file_content(request, plan, file_plan)
                        log_event(logger, "sample_app_gen.generate_file_step.fallback", path=file_plan.path)
                    path = project_dir / file_plan.path
                    self.tools.call("execution_file_write", path=path, content=content, overwrite=True)
                    existing_files[file_plan.path] = content
                    generated_files.append(GeneratedFile(path=str(path), description=file_plan.purpose))

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
            project_dir = Path(plan.project_dir)
            dist_dir = self.workspace_root / "artifacts"
            archive_base = dist_dir / plan.app_id
            build = self.tools.call("build_code", project_dir=project_dir, output_base=archive_base)
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
                f"Artifact bundle prepared at {archive_path}.",
            ]
            generated_outputs = [
                "배포 가이드라인 및 API 문서",
                f"artifact bundle: {archive_path}",
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
                        f"IMAGE_ARCHIVE: {docker['archive_path']}",
                        f"REMOTE_IMAGE_ARCHIVE: {docker['remote_archive_path']}",
                        "Docker image was built locally and loaded on the target host.",
                    ]
                )
                generated_outputs.append(f"container image: {docker['image_ref']}")
                rollback_cleanup.extend([f"rm -f {docker['archive_path']}", f"docker image rm {docker['image_ref']}"])
            else:
                notes.extend(
                    [
                        f"DOCKER_UPLOAD_ERROR: {docker['error_code']}",
                        f"DOCKER_UPLOAD_STDERR: {docker['stderr'][:400]}" if docker["stderr"] else "DOCKER_UPLOAD_STDERR: none",
                    ]
                )

            if self.llm.is_available:
                notes.append("LLM generated the application plan and source files.")
            else:
                notes.append("Azure OpenAI is not configured, so fallback generation was used.")

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
            if plan.framework.lower() == "fastapi":
                return (
                    f"FROM python:{plan.runtime_version or '3.12'}-slim\n\n"
                    "WORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\n"
                    "COPY app ./app\nEXPOSE 8000\nCMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n"
                )
            return (
                f"FROM eclipse-temurin:{plan.runtime_version or '17'}-jre\n\n"
                "WORKDIR /app\nCOPY target/*.jar app.jar\nEXPOSE 8080\nENTRYPOINT [\"java\", \"-jar\", \"/app/app.jar\"]\n"
            )
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
            f"    <java.version>{plan.runtime_version or '17'}</java.version>\n"
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
        java_version = plan.runtime_version or "17"
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
            "    testImplementation 'org.springframework.boot:spring-boot-starter-test'\n"
            "}\n\n"
            "tasks.named('test') {\n"
            "    useJUnitPlatform()\n"
            "}\n"
        )
