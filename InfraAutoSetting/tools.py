from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Literal, TypedDict

from Supervisor.models import TargetHost, UserRequest
from agent_logging import get_agent_logger, timed_step

logger = get_agent_logger("infra_auto_setting.tools", "infra_auto_setting.log")

ToolName = Literal["execution_file_write", "code_validator", "ssh"]


class FileWriteResult(TypedDict):
    ok: bool
    written_path: str
    bytes_written: int


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


class InfraTools:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._handlers: dict[ToolName, Any] = {
            "execution_file_write": self.execution_file_write,
            "code_validator": self.code_validator,
            "ssh": self.ssh,
        }

    def call(self, tool_name: ToolName, **kwargs: Any) -> Any:
        handler = self._handlers[tool_name]
        return handler(**kwargs)

    def execution_file_write(
        self,
        path: Path,
        content: str,
        overwrite: bool = True,
        chmod: str | None = None,
    ) -> FileWriteResult:
        with timed_step(logger, "infra_auto_setting.tools.execution_file_write", path=str(path)):
            if path.exists() and not overwrite:
                raise FileExistsError(f"File already exists: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if chmod:
                path.chmod(int(chmod, 8))
            return {
                "ok": True,
                "written_path": str(path),
                "bytes_written": len(content.encode("utf-8")),
            }

    def code_validator(self, script_path: str, request: UserRequest) -> ValidationResult:
        with timed_step(logger, "infra_auto_setting.tools.code_validator", script_path=script_path):
            content = Path(script_path).read_text(encoding="utf-8")
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
            if request.constraints.sudo_allowed == "no" and re.search(r"(^|\s)sudo\s", content):
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
            return {"ok": not has_error, "issues": issues}

    def ssh(self, request: UserRequest, local_script_path: str) -> RemoteExecutionResult:
        target = request.targets[0] if request.targets else None
        if target is None:
            return self._remote_error(
                command_label="ssh --unavailable",
                error_code="E_SSH_CONNECT",
                stderr="No target host was provided.",
            )

        command_label = f"ssh --host {target.host} --port {target.ssh_port} --script {local_script_path}"
        if self.dry_run:
            return {
                "ok": True,
                "exit_code": 0,
                "stdout": "Dry-run mode enabled. Remote execution skipped.",
                "stderr": "",
                "timed_out": False,
                "command_label": command_label + " --dry-run",
                "error_code": "",
            }

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
            return self._remote_error(command_label, "E_SSH_TIMEOUT", str(exc), timed_out=True)
        except Exception as exc:  # pragma: no cover
            return self._remote_error(command_label, "E_SSH_CONNECT", str(exc))

        if process.returncode != 0:
            return {
                "ok": False,
                "error_code": "E_REMOTE_EXEC",
                "stderr": process.stderr,
                "command_label": command_label,
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "timed_out": False,
            }

        return {
            "ok": True,
            "exit_code": 0,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "timed_out": False,
            "command_label": command_label,
            "error_code": "",
        }

    def _build_ssh_command(self, target: TargetHost) -> list[str]:
        base = ["ssh"]
        if target.auth_method == "pem_path":
            base += ["-i", target.auth_ref]
        base += ["-p", str(target.ssh_port), f"{target.user}@{target.host}", "bash -s"]
        return base

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
