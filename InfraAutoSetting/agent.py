from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, GraphView, TargetHost, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step

from .llm import InfraScriptGeneratorLLM
from .models import GRAPH_EDGES, GRAPH_MERMAID, GRAPH_NODES, InfraBuildRunResult, InfraScriptArtifact
from .tools import InfraTools, ValidationIssue

logger = get_agent_logger("infra_auto_setting.agent", "infra_auto_setting.log")

VERSION_CATALOG: dict[str, list[str]] = {
    "apache": ["2.4.66", "2.4.65"],
    "tomcat": ["10.1.36", "10.1.35", "9.0.95"],
    "kafka": ["3.6.2", "3.6.1", "3.5.2"],
    "java": ["21.0.4", "17.0.12"],
    "pinpoint": ["Pinpoint v3", "Pinpoint v2"],
}

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
                resolved_versions, version_notes = self._resolve_versions(request.infra_tech_stack.versions)
                script = self._build_script(request, resolved_versions, prior_executions)
                artifact = self.tools.call(
                    "execution_file_write",
                    path=self._script_path(request),
                    content=script,
                    overwrite=True,
                    chmod="0755",
                )
                script_path = artifact["written_path"]

                executed_commands = [f"execution_file_write --path {script_path} --type script"]
                notes = list(version_notes)

                validation = self.tools.call("code_validator", script_path=script_path, request=request)
                executed_commands.append(f"code_validator --path {script_path}")
                if not validation["ok"]:
                    return self._validation_failed_result(
                        script_path=script_path,
                        executed_commands=executed_commands,
                        issues=validation["issues"],
                    )

                remote = self.tools.call("ssh", request=request, local_script_path=script_path)
                executed_commands.append(remote["command_label"])
                notes.extend(self._runtime_notes(request))
                if not remote["ok"]:
                    notes.extend(
                        [
                            f"REMOTE_ERROR_CODE: {remote['error_code']}",
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
                return self._unexpected_failure_result(str(exc))

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
        return sorted(candidates, key=self._version_sort_key, reverse=True)[0]

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
        fallback_script = self._build_script_fallback(request, versions, prior_executions)
        llm_script = self.llm.generate_install_script(
            request=request,
            resolved_versions=versions,
            package_manager=self._resolve_package_manager(request),
            prior_executions=prior_executions,
            fallback_script=fallback_script,
        )
        script = llm_script if llm_script else fallback_script
        return self._enforce_java_runtime_policy(
            script=script,
            request=request,
            resolved_versions=versions,
            package_manager=self._resolve_package_manager(request),
        )

    def _build_script_fallback(
        self,
        request: UserRequest,
        versions: dict[str, str],
        prior_executions: list[AgentExecution],
    ) -> str:
        package_manager = self._resolve_package_manager(request)
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"# package_manager: {package_manager}",
            f"# sudo_allowed: {request.constraints.sudo_allowed}",
            "",
        ]

        for component in request.infra_tech_stack.components:
            normalized = component.strip().lower()
            version = versions.get(component) or versions.get(normalized, "latest")
            lines.extend(self._component_install_lines(normalized, version, package_manager, request.constraints.sudo_allowed))
            lines.append("")

        lines.extend(
            [
                "# Provision log directories",
                f"mkdir -p {shlex.quote(request.logging.base_dir)} {shlex.quote(request.logging.gc_log_dir)} {shlex.quote(request.logging.app_log_dir)}",
                f"chmod 755 {shlex.quote(request.logging.base_dir)} {shlex.quote(request.logging.gc_log_dir)} {shlex.quote(request.logging.app_log_dir)}",
                "",
            ]
        )

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
        output: list[str] = []
        for execution in prior_executions:
            if execution.agent != "sample_app":
                continue
            output.extend(note.strip() for note in execution.notes if note.strip())
        return output[:10]

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
        if package_manager == "apt":
            install_cmd = f"{prefix}apt -y install openjdk-{java_major}-jdk"
        elif package_manager == "dnf":
            install_cmd = f"{prefix}dnf -y install java-{java_major}-openjdk-devel"
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
