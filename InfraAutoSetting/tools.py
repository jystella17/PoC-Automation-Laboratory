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
            return self._remote_error(
                command_label="ssh --unavailable",
                error_code="E_SSH_CONNECT",
                stderr="No target host was provided.",
            )

        command_label = f"ssh --host {target.host} --port {target.ssh_port} --script {local_script_path}"
        emit_event(owner="infra_build", phase="tool.ssh", status="started", message="원격 인프라 적용을 시작합니다.", details={"host": target.host, "script_path": local_script_path})
        if self.dry_run:
            result = {
                "ok": True,
                "exit_code": 0,
                "stdout": "Dry-run mode enabled. Remote execution skipped.",
                "stderr": "",
                "timed_out": False,
                "command_label": command_label + " --dry-run",
                "error_code": "",
            }
            emit_event(owner="infra_build", phase="tool.ssh", status="completed", message="드라이런 모드로 원격 인프라 적용을 건너뛰었습니다.", details={"host": target.host})
            return result

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
            result = self._remote_error(command_label, "E_SSH_TIMEOUT", str(exc), timed_out=True)
            emit_event(owner="infra_build", phase="tool.ssh", status="failed", message="원격 인프라 적용이 타임아웃되었습니다.", details={"host": target.host, "error_code": result["error_code"]})
            return result
        except Exception as exc:  # pragma: no cover
            result = self._remote_error(command_label, "E_SSH_CONNECT", str(exc))
            emit_event(owner="infra_build", phase="tool.ssh", status="failed", message="SSH 연결에 실패했습니다.", details={"host": target.host, "error_code": result["error_code"]})
            return result

        if process.returncode != 0:
            result = {
                "ok": False,
                "error_code": "E_REMOTE_EXEC",
                "stderr": process.stderr,
                "command_label": command_label,
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "timed_out": False,
            }
            emit_event(owner="infra_build", phase="tool.ssh", status="failed", message="원격 인프라 적용 명령이 실패했습니다.", details={"host": target.host, "error_code": result["error_code"]})
            return result

        result = {
            "ok": True,
            "exit_code": 0,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "timed_out": False,
            "command_label": command_label,
            "error_code": "",
        }
        emit_event(owner="infra_build", phase="tool.ssh", status="completed", message="원격 인프라 적용이 완료되었습니다.", details={"host": target.host})
        return result

    def _remote_error(
        self,
        command_label: str,
        error_code: str,
        stderr: str,
        timed_out: bool = False,
    ) -> RemoteExecutionResult:
        return {
            "ok": False,
            "error_code": error_code,
            "stderr": stderr,
            "command_label": command_label,
            "exit_code": 1,
            "stdout": "",
            "timed_out": timed_out,
        }
