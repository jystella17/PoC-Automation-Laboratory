from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypedDict

from Supervisor.models import TargetHost
from agent_logging import timed_step
from eventing import emit_event


class FileWriteResult(TypedDict):
    ok: bool
    written_path: str
    bytes_written: int


class BaseTools:
    def __init__(self, owner: str, log_label: str, logger: Any):
        self._owner = owner
        self._log_label = log_label
        self._logger = logger
        self._handlers: dict[str, Any] = {}

    def call(self, tool_name: str, **kwargs: Any) -> Any:
        handler = self._handlers[tool_name]
        return handler(**kwargs)

    def execution_file_write(
        self,
        path: Path,
        content: str,
        overwrite: bool = True,
        chmod: str | None = None,
        create_parent: bool = True,
    ) -> FileWriteResult:
        with timed_step(self._logger, f"{self._log_label}.tools.execution_file_write", path=str(path)):
            emit_event(
                owner=self._owner,
                phase="tool.execution_file_write",
                status="started",
                message="파일 쓰기 도구를 호출합니다.",
                details={"path": str(path)},
            )
            if path.exists() and not overwrite:
                raise FileExistsError(f"File already exists: {path}")
            if create_parent:
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if chmod:
                path.chmod(int(chmod, 8))
            emit_event(
                owner=self._owner,
                phase="tool.execution_file_write",
                status="completed",
                message="파일 쓰기가 완료되었습니다.",
                details={"path": str(path)},
            )
            return {
                "ok": True,
                "written_path": str(path),
                "bytes_written": len(content.encode("utf-8")),
            }

    @staticmethod
    def _common_ssh_options() -> list[str]:
        strict_host_key_checking = os.getenv("SSH_STRICT_HOST_KEY_CHECKING", "accept-new").strip() or "accept-new"
        user_known_hosts_file = os.getenv("SSH_USER_KNOWN_HOSTS_FILE", "").strip()
        if not user_known_hosts_file:
            user_known_hosts_file = str(Path.home() / ".ssh" / "known_hosts")
        return [
            "-o", f"StrictHostKeyChecking={strict_host_key_checking}",
            "-o", f"UserKnownHostsFile={user_known_hosts_file}",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
        ]

    @classmethod
    def _build_ssh_command(cls, target: TargetHost, remote_command: str = "bash -s") -> list[str]:
        base = ["ssh", *cls._common_ssh_options()]
        if target.auth_method == "pem_path":
            base += ["-i", target.auth_ref]
        base += ["-p", str(target.ssh_port), f"{target.user}@{target.host}", remote_command]
        return base
