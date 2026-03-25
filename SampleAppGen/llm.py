from __future__ import annotations

import json
import re
from pathlib import Path

from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step
from base_llm import BaseLLM
from shared.utils import extract_prior_notes

from .models import ApplicationFilePlan, ApplicationPlan, ValidationIssue


SYSTEM_PROMPT = """You are a senior software engineer generating production-like applications.
You write complete, coherent project files based on the user's requested framework, language, database, logging policy, and test scenarios.
Prefer practical, runnable code over placeholders. Respect requested failure scenarios such as memory leak or OOM only when explicitly requested.
Never expose secrets in generated code. Use environment variables for passwords and secrets.
Return JSON only when asked for JSON. Return raw file contents only when asked for a file body.

Deployment context:
  All generated applications are containerized with Docker and deployed via docker run.
  Do not generate bare-metal operation scripts (e.g. start.sh, stop.sh, run.sh, deploy.sh).
  A Dockerfile is the only deployment artifact needed alongside the application build.

Cross-file consistency rule:
  When generating a file that calls methods from another already-generated file,
  you MUST use the exact method/function names as they appear in the provided context.

  Java example:
    Context shows: public Post findById(Long id) { ... }
    CORRECT:       postService.findById(id)
    WRONG:         postService.getById(id)   <- invented name, causes compile error

  Python example:
    Context shows: def get_item(item_id: int) -> Item:
    CORRECT:       service.get_item(item_id)
    WRONG:         service.fetch_item(item_id)  <- invented name, causes AttributeError

  Always look up the Public API Registry and the full file content before writing any cross-file call.
"""

logger = get_agent_logger("sample_app_gen.llm", "sample_app_gen.log")


class SampleAppGeneratorLLM(BaseLLM):
    def __init__(self, settings: AzureOpenAISettings):
        super().__init__(settings)

    def plan_application(
        self,
        request: UserRequest,
        prior_executions: list[AgentExecution],
        project_dir: Path,
        app_id: str,
    ) -> ApplicationPlan | None:
        with timed_step(logger, "sample_app_gen.llm.plan_application", app_id=app_id):
            infra_notes = extract_prior_notes(prior_executions, agent_filter="infra_build")
            human_prompt = (
                "Return JSON only.\n"
                "Create an application generation plan for a sample app.\n"
                "The JSON schema is:\n"
                "{"
                "\"framework\": str, "
                "\"framework_version\": str, "
                "\"language\": str, "
                "\"build_system\": \"maven\"|\"gradle\", "
                "\"runtime_version\": str, "
                "\"artifact_type\": \"jar\"|\"zip\", "
                "\"artifact_name\": str, "
                "\"image_name\": str, "
                "\"log_dir\": str, "
                "\"gc_log_dir\": str, "
                "\"special_scenarios\": [str], "
                "\"deployment_commands\": [str], "
                "\"required_env\": [str], "
                "\"file_plan\": [{\"path\": str, \"purpose\": str, \"language\": str}], "
                "\"spec_markdown\": str"
                "}\n"
                f"app_id: {app_id}\n"
                f"project_dir: {project_dir}\n"
                f"user_request_json:\n{request.model_dump_json(indent=2)}\n"
                f"prior_infra_execution_notes:\n{infra_notes}\n"
                f"requested_build_system: {request.app_tech_stack.build_system or 'auto'}\n"
                "The file plan must include all files required to build or run the app locally. "
                "Use Java for Spring/Spring Boot and Python for FastAPI. "
                "For Spring/Spring Boot, choose build_system as maven or gradle based on user request/additional_request. "
                "Use environment variables for DB passwords. "
                "The spec_markdown must include endpoints, data/config notes, and any requested failure scenarios. "
                "Do NOT include bare-metal operation scripts (start.sh, stop.sh, run.sh, deploy.sh) in the file plan — the app is deployed via Docker. "
                "deployment_commands must contain only 'docker run' commands (e.g. 'docker run -d -p 8080:8080 ...'), never 'docker build' commands."
            )
            content = self._invoke_llm(SYSTEM_PROMPT, human_prompt)
            if content is None:
                log_event(logger, "sample_app_gen.llm.plan_application.skipped", reason="llm_not_available")
                return None
            payload = self._extract_json(content)
            if not payload:
                log_event(logger, "sample_app_gen.llm.plan_application.empty_payload", app_id=app_id)
                return None
            data = json.loads(payload)
            try:
                plan = ApplicationPlan(
                    app_id=app_id,
                    framework=data["framework"],
                    framework_version=data["framework_version"],
                    language=data["language"],
                    build_system=data.get("build_system", "maven"),
                    runtime_version=data.get("runtime_version", ""),
                    artifact_type=data.get("artifact_type", "zip"),
                    artifact_name=data["artifact_name"],
                    image_name=data["image_name"],
                    project_dir=str(project_dir),
                    log_dir=data.get("log_dir", request.logging.app_log_dir),
                    gc_log_dir=data.get("gc_log_dir", request.logging.gc_log_dir),
                    special_scenarios=data.get("special_scenarios", []),
                    deployment_commands=data.get("deployment_commands", []),
                    required_env=data.get("required_env", []),
                    file_plan=[ApplicationFilePlan.model_validate(item) for item in data.get("file_plan", [])],
                    spec_markdown=data.get("spec_markdown", ""),
                )
            except (KeyError, TypeError, ValueError) as exc:
                log_event(logger, "sample_app_gen.llm.plan_application.validation_error", app_id=app_id, error=str(exc))
                return None
            log_event(logger, "sample_app_gen.llm.plan_application.result", app_id=app_id, file_count=len(plan.file_plan))
            return plan

    def generate_file(
        self,
        request: UserRequest,
        plan: ApplicationPlan,
        file_plan: ApplicationFilePlan,
        existing_files: dict[str, str],
    ) -> str | None:
        with timed_step(logger, "sample_app_gen.llm.generate_file", path=file_plan.path):
            context = self._project_context(existing_files)
            api_registry = self._public_api_registry(existing_files)
            human_prompt = (
                "Return only the raw file content. No markdown fences.\n"
                f"Generate the file `{file_plan.path}` for a {plan.framework} sample application.\n"
                f"File purpose: {file_plan.purpose}\n"
                f"Expected language/format: {file_plan.language}\n"
                f"Application plan:\n{plan.model_dump_json(indent=2)}\n"
                f"Original user request:\n{request.model_dump_json(indent=2)}\n"
                f"Public API of already-generated files (use EXACT names — do not guess or invent):\n{api_registry}\n"
                f"Files generated so far (full source):\n{context}\n"
                "The result must be coherent with the rest of the project, use environment variables for secrets, "
                "and implement explicitly requested failure scenarios only when present."
            )
            result = self._invoke_llm(SYSTEM_PROMPT, human_prompt, strip_fences=True)
            if result is None:
                log_event(logger, "sample_app_gen.llm.generate_file.empty", path=file_plan.path)
                return None
            log_event(logger, "sample_app_gen.llm.generate_file.result", path=file_plan.path, size=len(result))
            return result

    def repair_file(
        self,
        request: UserRequest,
        plan: ApplicationPlan,
        file_plan: ApplicationFilePlan,
        current_content: str,
        issues: list[ValidationIssue],
        existing_files: dict[str, str],
    ) -> str | None:
        with timed_step(logger, "sample_app_gen.llm.repair_file", path=file_plan.path, issue_count=len(issues)):
            issue_text = "\n".join(f"- {item.path}: {item.message}" for item in issues)
            context = self._project_context(existing_files)
            api_registry = self._public_api_registry(existing_files)
            human_prompt = (
                "Return only the corrected raw file content. No markdown fences.\n"
                f"Repair the file `{file_plan.path}`.\n"
                f"Application plan:\n{plan.model_dump_json(indent=2)}\n"
                f"Validation issues:\n{issue_text}\n"
                f"Original user request:\n{request.model_dump_json(indent=2)}\n"
                f"Public API of other files (use EXACT names to fix cross-file call errors):\n{api_registry}\n"
                f"Current file content:\n{current_content}\n"
                f"Other project files (full source):\n{context}\n"
                "Fix only what is needed to satisfy the validation issues while preserving requested behavior."
            )
            result = self._invoke_llm(SYSTEM_PROMPT, human_prompt, strip_fences=True)
            if result is None:
                log_event(logger, "sample_app_gen.llm.repair_file.empty", path=file_plan.path)
                return None
            log_event(logger, "sample_app_gen.llm.repair_file.result", path=file_plan.path, size=len(result))
            return result

    def _extract_json(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
        match = re.search(r"```json\s*(\{.*\})\s*```", stripped, re.DOTALL)
        if match:
            return match.group(1)
        match = re.search(r"(\{.*\})", stripped, re.DOTALL)
        return match.group(1) if match else ""

    def _project_context(self, existing_files: dict[str, str]) -> str:
        if not existing_files:
            return "none"
        chunks = []
        for path, content in existing_files.items():
            limit = 8000 if path.endswith((".java", ".py", ".kt")) else 3000
            preview = content if len(content) <= limit else content[:limit] + "\n...truncated..."
            chunks.append(f"FILE: {path}\n{preview}")
        return "\n\n".join(chunks)

    def _public_api_registry(self, existing_files: dict[str, str]) -> str:
        """생성된 소스 파일에서 public 메서드 시그니처를 추출해 간결한 API 레지스트리를 반환합니다."""
        entries: list[str] = []
        for path, content in existing_files.items():
            if path.endswith(".java"):
                sigs = self._extract_java_public_methods(content)
            elif path.endswith(".py"):
                sigs = self._extract_python_public_functions(content)
            else:
                continue
            if sigs:
                class_name = Path(path).stem
                entries.append(f"{class_name}: {', '.join(sigs)}")
        return "\n".join(entries) if entries else "none"

    def _extract_java_public_methods(self, content: str) -> list[str]:
        """Java 소스에서 public 메서드 시그니처를 추출합니다 (클래스/어노테이션 선언 제외)."""
        sigs: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if (
                re.match(r"public\s+(?!class|interface|enum|static\s+class|@)\S", stripped)
                and "(" in stripped
                and not stripped.startswith("@")
            ):
                sig = re.sub(r"\s*\{.*", "", stripped).strip()
                sig = re.sub(r"\s+throws\s+\S+", "", sig).strip()
                if sig:
                    sigs.append(sig)
        return sigs[:12]

    def _extract_python_public_functions(self, content: str) -> list[str]:
        """Python 소스에서 public 함수/메서드 시그니처를 추출합니다 (언더스코어 시작 제외)."""
        sigs: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if re.match(r"def [^_]\w*\s*\(", stripped):
                sig = re.sub(r"\s*:$", "", stripped)
                sigs.append(sig)
        return sigs[:12]
