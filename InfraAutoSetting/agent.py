from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

from config.versions import VERSION_CATALOG
from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, GraphView, TargetHost, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step
from eventing import emit_event
from shared.utils import extract_prior_notes

from .llm import InfraScriptGeneratorLLM
from .models import GRAPH_EDGES, GRAPH_MERMAID, GRAPH_NODES, InfraBuildRunResult, InfraScriptArtifact
from .tools import InfraTools, ValidationIssue

logger = get_agent_logger("infra_auto_setting.agent", "infra_auto_setting.log")

DEFAULT_GENERATED_OUTPUTS = [
    "infra bootstrap script",
    "component install report",
    "log directory provisioning result",
]
DEFAULT_RECOMMENDED_CONFIG = [
    "Validate sudo scope before remote execution.",
    "Keep GC logs and app logs in separate directories.",
    "Pin component versions to avoid drift between reruns.",
]
DEFAULT_ROLLBACK_CLEANUP = [
    "stop services: app/tomcat/kafka",
    "restore changed config backups",
    "remove temporary install artifacts",
]


class InfraAutoSettingAgent:
    def __init__(
        self,
        settings: AzureOpenAISettings | None = None,
        workspace_root: str | Path | None = None,
        dry_run: bool | None = None,
    ):
        base_dir = Path(workspace_root) if workspace_root else Path(tempfile.gettempdir()) / "infraautosetting-workspace"
        self.workspace_root = base_dir
        self.llm = InfraScriptGeneratorLLM(settings or AzureOpenAISettings())
        self.dry_run = (os.getenv("INFRA_AGENT_DRY_RUN", "false").lower() != "false") if dry_run is None else dry_run
        self.tools = InfraTools(dry_run=self.dry_run)

    def graph_view(self) -> GraphView:
        return GraphView(nodes=GRAPH_NODES, edges=GRAPH_EDGES, mermaid=GRAPH_MERMAID)

    def run(self, request: UserRequest, prior_executions: list[AgentExecution] | None = None) -> InfraBuildRunResult:
        prior_executions = prior_executions or []
        with timed_step(logger, "infra_auto_setting.run", component_count=len(request.infra_tech_stack.components)):
            try:
                emit_event(owner="infra_build", phase="plan_script", status="started", message="인프라 스크립트 계획을 시작합니다.", details={"components": request.infra_tech_stack.components})
                resolved_versions, version_notes = self._resolve_versions(request.infra_tech_stack.versions)
                normalized_request = self._request_with_resolved_versions(request, resolved_versions)
                script = self._build_script(normalized_request, resolved_versions, prior_executions)
                emit_event(owner="infra_build", phase="plan_script", status="completed", message="인프라 스크립트 계획이 완료되었습니다.", details={"resolved_versions": resolved_versions})
                artifact = self.tools.call(
                    "execution_file_write",
                    path=self._script_path(normalized_request),
                    content=script,
                    overwrite=True,
                    chmod="0755",
                )
                script_path = artifact["written_path"]

                executed_commands = [f"execution_file_write --path {script_path} --type script"]
                notes = list(version_notes)

                validation = self.tools.call("code_validator", script_path=script_path, request=normalized_request)
                executed_commands.append(f"code_validator --path {script_path}")
                if not validation["ok"]:
                    return self._validation_failed_result(
                        script_path=script_path,
                        executed_commands=executed_commands,
                        issues=validation["issues"],
                    )

                remote = self.tools.call("ssh", request=normalized_request, local_script_path=script_path)
                executed_commands.append(remote["command_label"])
                notes.extend(self._runtime_notes(normalized_request))
                if not remote["ok"]:
                    notes.extend(
                        [
                            f"REMOTE_EXIT_CODE: {remote['exit_code']}",
                            f"REMOTE_ERROR_CODE: {remote['error_code']}",
                            f"REMOTE_STDOUT: {remote['stdout'][:400]}" if remote["stdout"] else "REMOTE_STDOUT: none",
                            f"REMOTE_STDERR: {remote['stderr'][:400]}" if remote["stderr"] else "REMOTE_STDERR: none",
                        ]
                    )

                execution = AgentExecution(
                    agent="infra_build",
                    success=remote["ok"],
                    executed_commands=executed_commands,
                    notes=notes,
                )
                return self._result(
                    execution=execution,
                    generated_files=[InfraScriptArtifact(path=script_path, description="infra bootstrap script")],
                    generated_outputs=DEFAULT_GENERATED_OUTPUTS,
                    recommended_config=DEFAULT_RECOMMENDED_CONFIG,
                    rollback_cleanup=DEFAULT_ROLLBACK_CLEANUP,
                )
            except Exception as exc:
                log_event(logger, "infra_auto_setting.run.exception", error=str(exc))
                emit_event(owner="infra_build", phase="run", status="failed", message="인프라 Agent 실행 중 예외가 발생했습니다.", details={"error": str(exc)})
                return self._unexpected_failure_result(str(exc))

    def _request_with_resolved_versions(self, request: UserRequest, resolved_versions: dict[str, str]) -> UserRequest:
        merged_versions = dict(request.infra_tech_stack.versions)
        merged_versions.update(resolved_versions)
        return request.model_copy(
            update={
                "infra_tech_stack": request.infra_tech_stack.model_copy(
                    update={"versions": merged_versions}
                )
            }
        )

    def _validation_failed_result(
        self,
        script_path: str,
        executed_commands: list[str],
        issues: list[ValidationIssue],
    ) -> InfraBuildRunResult:
        issue_lines = [f"{item['code']}: {item['message']}" for item in issues]
        execution = AgentExecution(
            agent="infra_build",
            success=False,
            executed_commands=executed_commands,
            notes=["Script validation failed; remote execution skipped.", *issue_lines],
        )
        return self._result(
            execution=execution,
            generated_files=[InfraScriptArtifact(path=script_path, description="infra bootstrap script")],
            generated_outputs=["infra bootstrap script"],
            recommended_config=["Fix validation errors and retry."],
            rollback_cleanup=["remove temporary install artifacts"],
        )

    def _unexpected_failure_result(self, message: str) -> InfraBuildRunResult:
        execution = AgentExecution(
            agent="infra_build",
            success=False,
            executed_commands=[],
            notes=[f"Unexpected infra build failure: {message}"],
        )
        return self._result(
            execution=execution,
            generated_outputs=[],
            recommended_config=["Inspect infra_auto_setting.log and retry with corrected inputs."],
            rollback_cleanup=["remove temporary install artifacts"],
        )

    def _result(
        self,
        execution: AgentExecution,
        generated_outputs: list[str],
        recommended_config: list[str],
        rollback_cleanup: list[str],
        generated_files: list[InfraScriptArtifact] | None = None,
    ) -> InfraBuildRunResult:
        return InfraBuildRunResult(
            execution=execution,
            generated_outputs=generated_outputs,
            recommended_config=recommended_config,
            rollback_cleanup=rollback_cleanup,
            generated_files=generated_files or [],
            graph=self.graph_view(),
        )

    def _runtime_notes(self, request: UserRequest) -> list[str]:
        target = self._primary_target(request)
        return [
            f"SSH_MODE: {'dry_run' if self.dry_run else 'execute'}",
            f"TARGET: {target.host if target else 'none'}",
            f"SSH_AUTH_METHOD: {target.auth_method if target else 'none'}",
            f"SSH_PORT: {target.ssh_port if target else 22}",
            "sudo usage follows constraints.sudo_allowed.",
        ]

    def _primary_target(self, request: UserRequest) -> TargetHost | None:
        return request.targets[0] if request.targets else None

    def _resolve_versions(self, versions: dict[str, str]) -> tuple[dict[str, str], list[str]]:
        resolved = dict(versions)
        notes: list[str] = []
        for component, value in versions.items():
            normalized_component = component.strip().lower()
            source = value.strip()
            if not source or source.lower() == "none":
                continue
            chosen = self._resolve_with_catalog(normalized_component, source)
            if chosen != source:
                notes.append(f"VERSION_RESOLVED: {normalized_component} {source} -> {chosen}")
            resolved[component] = chosen
        return resolved, notes

    def _resolve_with_catalog(self, component: str, requested: str) -> str:
        catalog = VERSION_CATALOG.get(component, [])
        if not catalog:
            return requested

        requested_token = requested.strip().lower().replace("v", "")
        if not re.fullmatch(r"\d+(?:\.\d+)?", requested_token):
            return requested

        requested_parts = tuple(int(x) for x in requested_token.split("."))
        candidates: list[str] = []
        for candidate in catalog:
            match = re.search(r"\d+(?:\.\d+){0,2}", candidate)
            if not match:
                continue
            candidate_parts = tuple(int(x) for x in match.group(0).split("."))
            if len(requested_parts) == 1 and candidate_parts[0] == requested_parts[0]:
                candidates.append(candidate)
            elif len(requested_parts) >= 2 and candidate_parts[:2] == requested_parts[:2]:
                candidates.append(candidate)

        if not candidates:
            return requested
        return max(candidates, key=self._version_sort_key)

    def _version_sort_key(self, value: str) -> tuple[int, int, int]:
        match = re.search(r"\d+(?:\.\d+){0,2}", value)
        if not match:
            return 0, 0, 0
        parts = [int(x) for x in match.group(0).split(".")]
        while len(parts) < 3:
            parts.append(0)
        return parts[0], parts[1], parts[2]

    def _script_path(self, request: UserRequest) -> Path:
        target = self._primary_target(request)
        host = (target.host if target else "unknown-host").replace("/", "-")
        safe_host = re.sub(r"[^a-zA-Z0-9_.-]", "-", host)
        target_dir = self.workspace_root / "scripts"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"infra_bootstrap_{safe_host}.sh"

    def _build_script(self, request: UserRequest, versions: dict[str, str], prior_executions: list[AgentExecution]) -> str:
        package_manager = self._resolve_package_manager(request)
        fallback_script = self._build_script_fallback(request, versions, prior_executions, package_manager)
        script = fallback_script
        script = self._sanitize_generated_script(
            script=script,
            request=request,
            resolved_versions=versions,
            package_manager=package_manager,
        )
        script = self._enforce_logging_directory_policy(script=script, request=request)
        return self._enforce_java_runtime_policy(
            script=script,
            request=request,
            resolved_versions=versions,
            package_manager=package_manager,
        )

    def _build_script_fallback(
        self,
        request: UserRequest,
        versions: dict[str, str],
        prior_executions: list[AgentExecution],
        package_manager: str,
    ) -> str:
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"# package_manager: {package_manager}",
            f"# sudo_allowed: {request.constraints.sudo_allowed}",
            "",
        ]
        log_dir_block = self._logging_directory_block(request)

        for component in request.infra_tech_stack.components:
            normalized = component.strip().lower()
            version = versions.get(component) or versions.get(normalized, "latest")
            lines.extend(self._component_install_lines(normalized, version, package_manager, request.constraints.sudo_allowed))
            lines.append("")

        lines.extend(log_dir_block)

        sample_app_notes = self._sample_app_notes(prior_executions)
        if sample_app_notes:
            lines.extend(["# Prior sample_app notes", *[f"# {line}" for line in sample_app_notes]])

        return "\n".join(lines).strip() + "\n"

    def _resolve_package_manager(self, request: UserRequest) -> str:
        target = self._primary_target(request)
        os_type = target.os_type.strip().lower() if target else ""

        if "ubuntu" in os_type or "debian" in os_type:
            return "apt"
        if "rhel" in os_type or "amazon linux" in os_type or "amzn" in os_type:
            return "dnf"

        infra_os = request.infra_tech_stack.os.strip().lower()
        return "apt" if infra_os == "linux" else "unknown"

    def _component_install_lines(self, component: str, version: str, package_manager: str, sudo_allowed: str) -> list[str]:
        if package_manager == "unknown":
            return [f"echo 'package manager unknown for component: {component}; manual install required'"]

        prefix = "sudo " if sudo_allowed in {"yes", "limited"} else ""
        quoted_version = shlex.quote(version)

        if component == "apache":
            package_name = "apache2" if package_manager == "apt" else "httpd"
            service_name = "apache2" if package_manager == "apt" else "httpd"
            return [
                f"{prefix}{package_manager} -y install {package_name}",
                f"echo 'apache version target: {quoted_version}'",
                f"{prefix}systemctl enable --now {service_name}",
            ]
        if component == "tomcat":
            return [
                f"echo 'tomcat major/minor target: {quoted_version}'",
                f"{prefix}{package_manager} -y install tomcat || true",
                f"{prefix}systemctl enable --now tomcat || true",
            ]
        if component == "kafka":
            return [
                f"echo 'kafka target: {quoted_version}'",
                f"{prefix}{package_manager} -y install kafka || true",
            ]
        if component == "pinpoint":
            return [
                f"echo 'pinpoint target: {quoted_version}'",
                "echo 'pinpoint install requires package/source policy - placeholder step'",
            ]
        return [f"echo 'unsupported component: {component} (skipped)'"]

    def _sample_app_notes(self, prior_executions: list[AgentExecution]) -> list[str]:
        merged = extract_prior_notes(prior_executions, agent_filter="sample_app")
        if merged == "none":
            return []
        return merged.splitlines()[:10]

    def _enforce_java_runtime_policy(
        self,
        script: str,
        request: UserRequest,
        resolved_versions: dict[str, str],
        package_manager: str,
    ) -> str:
        if not self._requires_java(request):
            return script
        java_major = self._java_major(resolved_versions.get("java", ""))
        if not java_major:
            return script

        marker = "# Enforce requested Java runtime"
        if marker in script:
            return script

        prefix = "sudo " if request.constraints.sudo_allowed in {"yes", "limited"} else ""
        java_package = self._java_package_name(request, package_manager, java_major)
        if java_package:
            install_cmd = f"{prefix}{package_manager} -y install {java_package}"
        else:
            install_cmd = f"echo 'Unsupported package manager for Java install: {package_manager}'; exit 1"

        java_block = [
            "",
            marker,
            install_cmd,
            "JAVA_MAJOR=\"$(java -version 2>&1 | awk -F'[\\\".]' '/version/ {print $2}')\"",
            f"if [ \"$JAVA_MAJOR\" != \"{java_major}\" ]; then",
            f"  echo \"Requested Java {java_major}, but detected ${{JAVA_MAJOR}}.\"",
            "  exit 1",
            "fi",
            f"echo \"Java {java_major} is installed and active.\"",
            "",
        ]
        base = script if script.endswith("\n") else script + "\n"
        return base + "\n".join(java_block)

    def _sanitize_generated_script(
        self,
        script: str,
        request: UserRequest,
        resolved_versions: dict[str, str],
        package_manager: str,
    ) -> str:
        java_major = self._java_major(resolved_versions.get("java", ""))
        sanitized_lines: list[str] = []
        skip_install_continuation = False

        for raw_line in script.splitlines():
            line = raw_line.strip()
            if skip_install_continuation:
                if self._continues_shell_command(line):
                    continue
                skip_install_continuation = False

            if self._is_logging_directory_line(line, request):
                continue

            if self._is_package_install_line(line):
                if self._line_mentions_java_or_gradle(line, java_major):
                    skip_install_continuation = self._continues_shell_command(line)
                    continue

            if self._line_mentions_java_or_gradle(line, java_major):
                continue

            sanitized_lines.append(raw_line)

        return "\n".join(sanitized_lines).strip() + "\n"

    def _is_package_install_line(self, line: str) -> bool:
        return bool(re.search(r"\b(?:sudo\s+)?(?:dnf|yum|apt|apt-get)\b.*\binstall\b", line))

    def _line_mentions_java_or_gradle(self, line: str, java_major: str) -> bool:
        java_pattern = rf"\b(openjdk-{java_major}-jdk|java-{java_major}-openjdk(?:-devel)?|java-{java_major}-amazon-corretto(?:-devel)?)\b" if java_major else r"$^"
        return bool(re.search(java_pattern, line) or re.search(r"\bgradle\b", line))

    def _continues_shell_command(self, line: str) -> bool:
        stripped = line.rstrip()
        return stripped.endswith("\\") or stripped.endswith("&&") or stripped.endswith("||")

    def _enforce_logging_directory_policy(self, script: str, request: UserRequest) -> str:
        marker = "# Provision log directories"
        if marker in script:
            return script

        base = script if script.endswith("\n") else script + "\n"
        return base + "\n".join(self._logging_directory_block(request))

    def _logging_directory_block(self, request: UserRequest) -> list[str]:
        prefix = "sudo " if request.constraints.sudo_allowed in {"yes", "limited"} else ""
        return [
            "# Provision log directories",
            f"{prefix}mkdir -p {shlex.quote(request.logging.base_dir)} {shlex.quote(request.logging.gc_log_dir)} {shlex.quote(request.logging.app_log_dir)}",
            f"{prefix}chmod 755 {shlex.quote(request.logging.base_dir)} {shlex.quote(request.logging.gc_log_dir)} {shlex.quote(request.logging.app_log_dir)}",
            "",
        ]

    def _is_logging_directory_line(self, line: str, request: UserRequest) -> bool:
        logging_paths = (
            request.logging.base_dir,
            request.logging.gc_log_dir,
            request.logging.app_log_dir,
        )
        if not any(path in line for path in logging_paths):
            return False
        return "mkdir -p" in line or re.search(r"\bchmod\s+755\b", line) is not None

    def _java_package_name(self, request: UserRequest, package_manager: str, java_major: str) -> str:
        target = self._primary_target(request)
        os_type = target.os_type.strip().lower() if target else ""

        if package_manager == "apt":
            return f"openjdk-{java_major}-jdk"

        if package_manager == "dnf":
            if "amazon linux" in os_type or "amzn" in os_type:
                return f"java-{java_major}-amazon-corretto-devel"
            return f"java-{java_major}-openjdk-devel"

        return ""

    def _requires_java(self, request: UserRequest) -> bool:
        components = {component.strip().lower() for component in request.infra_tech_stack.components}
        languages = [language.strip().lower() for language in request.app_tech_stack.language]
        framework = request.app_tech_stack.framework.strip().lower()
        if components.intersection({"tomcat", "kafka"}):
            return True
        if framework in {"spring", "spring boot"}:
            return True
        return any(language.startswith("java") for language in languages)

    def _java_major(self, source: str) -> str:
        value = source.strip()
        if not value:
            return ""
        match = re.search(r"\d+", value)
        return match.group(0) if match else ""
