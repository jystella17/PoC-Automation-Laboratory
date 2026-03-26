from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from config.versions import VERSION_CATALOG
from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, GraphView, TargetHost, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step
from eventing import emit_event
from shared.utils import extract_prior_notes

from .cache import ScriptCache
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
PROTECTED_PATH_PREFIXES = ("/var", "/etc", "/usr", "/opt")


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
        self._cache = ScriptCache()

    def graph_view(self) -> GraphView:
        return GraphView(nodes=GRAPH_NODES, edges=GRAPH_EDGES, mermaid=GRAPH_MERMAID)

    def run(self, request: UserRequest, prior_executions: list[AgentExecution] | None = None) -> InfraBuildRunResult:
        prior_executions = prior_executions or []
        with timed_step(logger, "infra_auto_setting.run", component_count=len(request.infra_tech_stack.components)):
            try:
                emit_event(owner="infra_build", phase="plan_script", status="started", message="인프라 스크립트 계획을 시작합니다.", details={"components": request.infra_tech_stack.components})
                resolved_versions, version_notes = self._resolve_versions(request.infra_tech_stack.versions)
                normalized_request = self._request_with_resolved_versions(request, resolved_versions)
                script, cache_note = self._resolve_script(normalized_request, resolved_versions, prior_executions)
                is_cache_hit = cache_note.startswith("SCRIPT_CACHE_HIT")
                emit_event(
                    owner="infra_build",
                    phase="plan_script",
                    status="completed",
                    message="캐시에서 인프라 스크립트를 재사용합니다." if is_cache_hit else "인프라 스크립트 계획이 완료되었습니다.",
                    details={"resolved_versions": resolved_versions, "cache_status": cache_note},
                )
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
                notes.append(cache_note)

                validation = self.tools.call("code_validator", script_path=script_path, request=normalized_request)
                executed_commands.append(f"code_validator --path {script_path}")
                if not validation["ok"]:
                    return self._validation_failed_result(
                        script_path=script_path,
                        executed_commands=executed_commands,
                        issues=validation["issues"],
                    )

                emit_event(owner="infra_build", phase="remote_execute", status="started", message="대상 서버에서 스크립트를 실행합니다.")
                remote = self.tools.call("ssh", request=normalized_request, local_script_path=script_path)
                executed_commands.append(remote["command_label"])
                notes.extend(self._runtime_notes(normalized_request))
                if remote["ok"]:
                    emit_event(owner="infra_build", phase="remote_execute", status="completed", message="원격 스크립트 실행이 성공했습니다.")
                else:
                    notes.extend(self._remote_failure_notes(remote))
                    emit_event(owner="infra_build", phase="remote_execute", status="failed", message="원격 스크립트 실행에 실패했습니다.", details={"exit_code": remote["exit_code"], "error_code": remote["error_code"]})

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

    # ------------------------------------------------------------------ #
    # result builders                                                      #
    # ------------------------------------------------------------------ #

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

    def _remote_failure_notes(self, remote: dict) -> list[str]:
        return [
            f"REMOTE_EXIT_CODE: {remote['exit_code']}",
            f"REMOTE_ERROR_CODE: {remote['error_code']}",
            f"REMOTE_STDOUT: {remote['stdout'][:2000]}" if remote["stdout"] else "REMOTE_STDOUT: none",
            f"REMOTE_STDERR: {remote['stderr'][:2000]}" if remote["stderr"] else "REMOTE_STDERR: none",
        ]

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

    # ------------------------------------------------------------------ #
    # version resolution                                                   #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # script resolution & caching                                         #
    # ------------------------------------------------------------------ #

    def _script_path(self, request: UserRequest) -> Path:
        target = self._primary_target(request)
        host = (target.host if target else "unknown-host").replace("/", "-")
        safe_host = re.sub(r"[^a-zA-Z0-9_.-]", "-", host)
        target_dir = self.workspace_root / "scripts"
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"infra_bootstrap_{safe_host}.sh"

    def _resolve_script(
        self,
        request: UserRequest,
        resolved_versions: dict[str, str],
        prior_executions: list[AgentExecution],
    ) -> tuple[str, str]:
        """Return (script, cache_note). Serves from cache on hit; generates and stores on miss."""
        package_manager = self._resolve_package_manager(request)
        cache_key = ScriptCache.build_key(
            components=request.infra_tech_stack.components,
            resolved_versions=resolved_versions,
            package_manager=package_manager,
            sudo_allowed=request.constraints.sudo_allowed,
            log_base_dir=request.logging.base_dir,
            log_gc_dir=request.logging.gc_log_dir,
            log_app_dir=request.logging.app_log_dir,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached_issues = self._validate_script_content(cached, request, resolved_versions, package_manager)
            if not cached_issues:
                log_event(logger, "infra_auto_setting.script_cache.hit", key_prefix=cache_key[:12])
                return cached, f"SCRIPT_CACHE_HIT: {cache_key[:12]}"
            log_event(logger, "infra_auto_setting.script_cache.hit", key_prefix=cache_key[:12])
            log_event(
                logger,
                "infra_auto_setting.script_cache.invalidated",
                key_prefix=cache_key[:12],
                issues=cached_issues,
            )

        script = self._build_script(request, resolved_versions, prior_executions, package_manager)
        self._cache.put(
            cache_key,
            script,
            meta={
                "components": request.infra_tech_stack.components,
                "versions": resolved_versions,
                "package_manager": package_manager,
            },
        )
        log_event(logger, "infra_auto_setting.script_cache.miss", key_prefix=cache_key[:12])
        return script, f"SCRIPT_CACHE_MISS: {cache_key[:12]}"

    # ------------------------------------------------------------------ #
    # script building                                                      #
    # ------------------------------------------------------------------ #

    _MAX_REPAIR = 2

    def _build_script(
        self,
        request: UserRequest,
        versions: dict[str, str],
        prior_executions: list[AgentExecution],
        package_manager: str,
    ) -> str:
        fallback = self._build_script_fallback(request, versions, prior_executions, package_manager)

        emit_event(owner="infra_build", phase="generate_script", status="started", message="인프라 스크립트를 LLM으로 생성합니다.")
        script = self.llm.generate_install_script(
            request=request,
            resolved_versions=versions,
            package_manager=package_manager,
            prior_executions=prior_executions,
            fallback_script=fallback,
        )
        if script is None:
            log_event(logger, "infra_auto_setting.build_script.llm_unavailable")
            emit_event(owner="infra_build", phase="generate_script", status="completed", message="LLM 생성 실패로 fallback 스크립트를 사용합니다.", details={"mode": "fallback"})
            return fallback
        emit_event(owner="infra_build", phase="generate_script", status="completed", message="LLM 스크립트 생성이 완료되었습니다.", details={"mode": "llm"})

        for attempt in range(self._MAX_REPAIR):
            emit_event(owner="infra_build", phase="validate_script", status="started", message="스크립트 내용 검증을 시작합니다.", details={"attempt": attempt + 1})
            issues = self._validate_script_content(script, request, versions, package_manager)
            if not issues:
                emit_event(owner="infra_build", phase="validate_script", status="completed", message="스크립트 검증을 통과했습니다.")
                return script
            emit_event(owner="infra_build", phase="validate_script", status="failed", message="스크립트 검증에서 이슈가 발견되었습니다.", details={"issue_count": len(issues), "issues": issues})

            emit_event(owner="infra_build", phase="repair_script", status="started", message="LLM으로 스크립트를 보정합니다.", details={"repair_round": attempt + 1})
            log_event(logger, "infra_auto_setting.build_script.repair", attempt=attempt + 1, issue_count=len(issues))
            repaired = self.llm.repair_script(
                script=script,
                issues=issues,
                request=request,
                resolved_versions=versions,
                package_manager=package_manager,
                fallback_script=fallback,
            )
            if repaired is None:
                emit_event(owner="infra_build", phase="repair_script", status="failed", message="LLM 보정에 실패했습니다.", details={"repair_round": attempt + 1})
                break
            emit_event(owner="infra_build", phase="repair_script", status="completed", message="LLM 스크립트 보정이 완료되었습니다.", details={"repair_round": attempt + 1})
            script = repaired

        final_issues = self._validate_script_content(script, request, versions, package_manager)
        if final_issues:
            log_event(logger, "infra_auto_setting.build_script.fallback_after_repair", issues=final_issues)
            emit_event(owner="infra_build", phase="generate_script", status="completed", message="보정 실패로 fallback 스크립트를 사용합니다.", details={"mode": "fallback", "remaining_issues": len(final_issues)})
            return fallback
        return script

    # ------------------------------------------------------------------ #
    # script content validation                                            #
    # ------------------------------------------------------------------ #

    def _validate_script_content(
        self,
        script: str,
        request: UserRequest,
        versions: dict[str, str],
        package_manager: str,
    ) -> list[str]:
        issues: list[str] = []

        if not self._bash_syntax_ok(script):
            issues.append("Script has bash syntax errors (bash -n failed).")

        for component in request.infra_tech_stack.components:
            normalized = component.strip().lower()
            if not self._script_addresses_component(script, normalized, versions):
                issues.append(f"Component '{normalized}' is not addressed in the script.")

        log_dirs = [d for d in (request.logging.base_dir, request.logging.gc_log_dir, request.logging.app_log_dir) if d]
        for log_dir in log_dirs:
            if log_dir not in script:
                issues.append(f"Logging directory '{log_dir}' is not referenced in the script.")

        for match in re.finditer(r"cp\s+-[a-zA-Z]*r[a-zA-Z]*\s+(\S+)\s+(\S+)", script):
            src, dst = match.group(1).rstrip("/"), match.group(2).rstrip("/")
            if dst.startswith(src + "/"):
                issues.append(f"Self-referential copy: '{src}' into '{dst}'. Use an external path like /tmp.")

        issues.extend(self._protected_path_write_issues(script, request.constraints.sudo_allowed))
        return issues

    def _protected_path_write_issues(self, script: str, sudo_allowed: str) -> list[str]:
        if sudo_allowed == "no":
            return []

        issues: list[str] = []
        for line_no, raw_line in enumerate(script.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("sudo "):
                continue
            if not self._contains_protected_path(line):
                continue
            if self._is_protected_write_command(line):
                issues.append(f"Line {line_no} writes to a protected path without sudo: {line}")
        return issues

    @staticmethod
    def _contains_protected_path(line: str) -> bool:
        return any(prefix in line for prefix in PROTECTED_PATH_PREFIXES)

    @staticmethod
    def _is_protected_write_command(line: str) -> bool:
        write_prefixes = (
            "mkdir ",
            "chmod ",
            "chown ",
            "cp ",
            "mv ",
            "install ",
            "ln ",
            "touch ",
            "tee ",
            "echo ",
            "cat ",
            "printf ",
        )
        return line.startswith(write_prefixes) or " >" in line or ">>" in line

    def _script_addresses_component(self, script: str, component: str, versions: dict[str, str]) -> bool:
        lower = script.lower()
        if component == "java":
            java_major = self._version_major(versions.get("java", ""))
            if not java_major:
                return True
            return any(pkg in lower for pkg in [
                f"openjdk-{java_major}-jdk",
                f"java-{java_major}-openjdk",
                f"java-{java_major}-amazon-corretto",
            ])
        if component == "apache":
            return "httpd" in lower or "apache2" in lower
        if component == "tomcat":
            return "tomcat" in lower
        if component == "kafka":
            return "kafka" in lower
        if component == "pinpoint":
            return "pinpoint" in lower
        return True

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

        for component in request.infra_tech_stack.components:
            normalized = component.strip().lower()
            version = versions.get(component) or versions.get(normalized, "latest")
            lines.extend(self._component_install_lines(normalized, version, package_manager, request))
            lines.append("")

        lines.extend(self._logging_directory_block(request))

        sample_app_notes = self._sample_app_notes(prior_executions)
        if sample_app_notes:
            lines.extend(["# Prior sample_app notes", *[f"# {line}" for line in sample_app_notes]])

        return "\n".join(lines).strip() + "\n"

    def _resolve_package_manager(self, request: UserRequest) -> str:
        target = self._primary_target(request)
        os_type = target.os_type.strip().lower() if target else ""

        if "ubuntu" in os_type or "debian" in os_type:
            return "apt"
        if any(token in os_type for token in ("rhel", "amazon linux", "amzn", "centos", "rocky", "fedora", "almalinux")):
            return "dnf"

        infra_os = request.infra_tech_stack.os.strip().lower()
        return "apt" if infra_os == "linux" else "unknown"

    def _component_install_lines(self, component: str, version: str, package_manager: str, request: UserRequest) -> list[str]:
        if package_manager == "unknown":
            return [f"echo 'package manager unknown for component: {component}; manual install required'"]

        prefix = self._sudo_prefix(request.constraints.sudo_allowed)
        major = self._version_major(version)

        if component == "java":
            if not major:
                return ["echo 'java version not specified; skipping'"]
            java_pkg = self._java_package_name(request, package_manager, major)
            if not java_pkg:
                return [f"echo 'Unsupported package manager for Java install: {package_manager}'; exit 1"]
            return [
                f"{prefix}{package_manager} -y install {java_pkg}",
                "JAVA_MAJOR=\"$(java -version 2>&1 | awk -F'[\\\".]' '/version/ {print $2}')\"",
                f"if [ \"$JAVA_MAJOR\" != \"{major}\" ]; then",
                f"  echo \"Requested Java {major}, but detected ${{JAVA_MAJOR}}.\"",
                "  exit 1",
                "fi",
                f"echo \"Java {major} is installed and active.\"",
            ]

        if component == "apache":
            if package_manager == "apt":
                pkg, service = "apache2", "apache2"
                install_cmd = (
                    f"{prefix}apt -y install {shlex.quote(f'apache2={version}*')} 2>/dev/null "
                    f"|| {prefix}apt -y install {pkg}"
                )
            else:
                pkg, service = "httpd", "httpd"
                install_cmd = (
                    f"{prefix}dnf -y install {shlex.quote(f'httpd-{version}*')} 2>/dev/null "
                    f"|| {prefix}dnf -y install {pkg}"
                )
            return [install_cmd, f"{prefix}systemctl enable --now {service}"]

        if component == "tomcat":
            versioned_pkg = f"tomcat{major}" if major else "tomcat"
            if package_manager == "apt":
                install_cmd = f"{prefix}apt -y install {versioned_pkg}"
            else:
                install_cmd = (
                    f"{prefix}dnf -y install {versioned_pkg} 2>/dev/null "
                    f"|| {prefix}dnf -y install tomcat"
                )
            return [install_cmd, f"{prefix}systemctl enable --now {versioned_pkg}"]

        if component == "kafka":
            if package_manager == "apt":
                install_cmd = (
                    f"{prefix}apt -y install {shlex.quote(f'kafka={version}*')} 2>/dev/null "
                    f"|| {prefix}apt -y install kafka"
                )
            else:
                install_cmd = (
                    f"{prefix}dnf -y install {shlex.quote(f'kafka-{version}*')} 2>/dev/null "
                    f"|| {prefix}dnf -y install kafka"
                )
            return [install_cmd]

        if component == "pinpoint":
            return [
                f"echo 'pinpoint target: {shlex.quote(version)}'",
                "echo 'pinpoint install requires package/source policy - placeholder step'",
            ]
        return [f"echo 'unsupported component: {component} (skipped)'"]

    def _sample_app_notes(self, prior_executions: list[AgentExecution]) -> list[str]:
        merged = extract_prior_notes(prior_executions, agent_filter="sample_app")
        if merged == "none":
            return []
        return merged.splitlines()[:10]

    # ------------------------------------------------------------------ #
    # script helpers                                                       #
    # ------------------------------------------------------------------ #

    def _logging_directory_block(self, request: UserRequest) -> list[str]:
        prefix = self._sudo_prefix(request.constraints.sudo_allowed)
        log_dirs = " ".join(
            shlex.quote(d)
            for d in (request.logging.base_dir, request.logging.gc_log_dir, request.logging.app_log_dir)
        )
        return [
            "# Provision log directories",
            f"{prefix}mkdir -p {log_dirs}",
            f"{prefix}chmod 755 {log_dirs}",
            "",
        ]

    # ------------------------------------------------------------------ #
    # Java helpers                                                         #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # static utilities                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _bash_syntax_ok(script: str) -> bool:
        """Return True if ``bash -n`` accepts the script (syntax-only check)."""
        try:
            result = subprocess.run(
                ["bash", "-n"],
                input=script,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return True  # if bash unavailable, skip check

    @staticmethod
    def _sudo_prefix(sudo_allowed: str) -> str:
        return "sudo " if sudo_allowed in {"yes", "limited"} else ""

    @staticmethod
    def _version_major(source: str) -> str:
        """Extract the leading integer from a version string (e.g. '10.1.36' → '10')."""
        value = source.strip()
        if not value:
            return ""
        match = re.search(r"\d+", value)
        return match.group(0) if match else ""

    @staticmethod
    def _version_sort_key(value: str) -> tuple[int, int, int]:
        match = re.search(r"\d+(?:\.\d+){0,2}", value)
        if not match:
            return 0, 0, 0
        parts = [int(x) for x in match.group(0).split(".")]
        while len(parts) < 3:
            parts.append(0)
        return parts[0], parts[1], parts[2]
