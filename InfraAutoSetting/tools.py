from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, TypedDict

from Supervisor.models import TargetHost, UserRequest
from agent_logging import get_agent_logger, timed_step
from eventing import emit_event
from shared.base_tools import BaseTools, FileWriteResult

logger = get_agent_logger("infra_auto_setting.tools", "infra_auto_setting.log")


class ValidationIssue(TypedDict):
    code: str
    message: str
    severity: str


class ValidationResult(TypedDict):
    ok: bool
    issues: list[ValidationIssue]


class RemoteExecutionResult(TypedDict):
    ok: bool
    command_label: str
    error_code: str
    stderr: str
    exit_code: int
    stdout: str
    timed_out: bool


class InfraTools(BaseTools):
    def __init__(self, dry_run: bool = False):
        super().__init__(owner="infra_build", log_label="infra_auto_setting", logger=logger)
        self.dry_run = dry_run
        self._handlers.update({
            "execution_file_write": self.execution_file_write,
            "code_validator": self.code_validator,
            "ssh": self.ssh,
        })

    def code_validator(self, script_path: str, request: UserRequest) -> ValidationResult:
        with timed_step(logger, "infra_auto_setting.tools.code_validator", script_path=script_path):
            emit_event(owner="infra_build", phase="tool.code_validator", status="started", message="인프라 스크립트 검증을 시작합니다.", details={"script_path": script_path})
            path = Path(script_path)
            content = path.read_text(encoding="utf-8")
            issues: list[ValidationIssue] = []

            if "set -euo pipefail" not in content:
                issues.append(
                    {
                        "code": "V_SAFETY_FLAGS_MISSING",
                        "message": "Shell script must include set -euo pipefail",
                        "severity": "error",
                    }
                )
            if "rm -rf /" in content:
                issues.append(
                    {
                        "code": "V_DANGEROUS_CMD",
                        "message": "Dangerous command detected: rm -rf /",
                        "severity": "error",
                    }
                )
            if request.constraints.sudo_allowed == "no" and re.search(r"(^|\s)sudo\s", content, re.MULTILINE):
                issues.append(
                    {
                        "code": "V_SUDO_FORBIDDEN",
                        "message": "sudo is not allowed by current policy",
                        "severity": "error",
                    }
                )
            for match in re.finditer(r"cp\s+-[a-zA-Z]*r[a-zA-Z]*\s+(\S+)\s+(\S+)", content):
                src, dst = match.group(1).rstrip("/"), match.group(2).rstrip("/")
                if dst.startswith(src + "/"):
                    issues.append(
                        {
                            "code": "V_SELF_REF_COPY",
                            "message": f"cp copies '{src}' into itself ('{dst}'). Use an external backup path like /tmp.",
                            "severity": "error",
                        }
                    )

            try:
                syntax_check = subprocess.run(
                    ["bash", "-n", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if syntax_check.returncode != 0:
                    issues.append(
                        {
                            "code": "V_BASH_SYNTAX",
                            "message": f"Bash syntax error: {syntax_check.stderr.strip()}",
                            "severity": "error",
                        }
                    )
            except Exception:
                pass

            if request.logging.base_dir not in content:
                issues.append(
                    {
                        "code": "V_LOG_BASE_DIR_MISSING",
                        "message": "logging.base_dir not referenced in script",
                        "severity": "warning",
                    }
                )

            has_error = any(item["severity"] == "error" for item in issues)
            result = {"ok": not has_error, "issues": issues}
            emit_event(
                owner="infra_build",
                phase="tool.code_validator",
                status="completed" if result["ok"] else "failed",
                message="인프라 스크립트 검증이 완료되었습니다." if result["ok"] else "인프라 스크립트 검증에서 오류가 발견되었습니다.",
                details={"issue_count": len(issues)},
            )
            return result

    def ssh(self, request: UserRequest, local_script_path: str) -> RemoteExecutionResult:
        target = request.targets[0] if request.targets else None
        if target is None:
            return self._remote_result(
                ok=False,
                command_label="ssh --unavailable",
                error_code="E_SSH_CONNECT",
                stderr="No target host was provided.",
                exit_code=1,
            )

        command_label = f"ssh --host {target.host} --port {target.ssh_port} --script {local_script_path}"
        emit_event(owner="infra_build", phase="tool.ssh", status="started", message="원격 인프라 적용을 시작합니다.", details={"host": target.host, "script_path": local_script_path})
        if self.dry_run:
            emit_event(owner="infra_build", phase="tool.ssh", status="completed", message="드라이런 모드로 원격 인프라 적용을 건너뛰었습니다.", details={"host": target.host})
            return self._remote_result(
                ok=True,
                command_label=command_label + " --dry-run",
                stdout="Dry-run mode enabled. Remote execution skipped.",
            )

        ssh_cmd = self._build_ssh_command(target)
        try:
            with open(local_script_path, encoding="utf-8") as script_file:
                process = subprocess.run(
                    ssh_cmd,
                    stdin=script_file,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=900,
                )
        except subprocess.TimeoutExpired as exc:
            result = self._remote_result(ok=False, command_label=command_label, error_code="E_SSH_TIMEOUT", stderr=str(exc), exit_code=1, timed_out=True)
            emit_event(owner="infra_build", phase="tool.ssh", status="failed", message="원격 인프라 적용이 타임아웃되었습니다.", details={"host": target.host, "error_code": "E_SSH_TIMEOUT"})
            return result
        except Exception as exc:  # pragma: no cover
            result = self._remote_result(ok=False, command_label=command_label, error_code="E_SSH_CONNECT", stderr=str(exc), exit_code=1)
            emit_event(owner="infra_build", phase="tool.ssh", status="failed", message="SSH 연결에 실패했습니다.", details={"host": target.host, "error_code": "E_SSH_CONNECT"})
            return result

        if process.returncode != 0:
            result = self._remote_result(ok=False, command_label=command_label, error_code="E_REMOTE_EXEC", stderr=process.stderr, stdout=process.stdout, exit_code=process.returncode)
            emit_event(owner="infra_build", phase="tool.ssh", status="failed", message="원격 인프라 적용 명령이 실패했습니다.", details={"host": target.host, "error_code": "E_REMOTE_EXEC"})
            return result

        emit_event(owner="infra_build", phase="tool.ssh", status="completed", message="원격 인프라 적용이 완료되었습니다.", details={"host": target.host})
        return self._remote_result(ok=True, command_label=command_label, stdout=process.stdout, stderr=process.stderr)

    def _remote_result(
        self,
        *,
        ok: bool,
        command_label: str,
        error_code: str = "",
        stderr: str = "",
        stdout: str = "",
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> RemoteExecutionResult:
        return {
            "ok": ok,
            "command_label": command_label,
            "error_code": error_code,
            "stderr": stderr,
            "exit_code": exit_code,
            "stdout": stdout,
            "timed_out": timed_out,
        }
