from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, TypedDict

from Supervisor.models import TargetHost, UserRequest
from agent_logging import get_agent_logger, timed_step
from eventing import emit_event

from .models import ValidationIssue

logger = get_agent_logger("sample_app_gen.tools", "sample_app_gen.log")

ToolName = Literal["execution_file_write", "code_validator", "build_code", "docker_build"]


class FileWriteResult(TypedDict):
    ok: bool
    written_path: str
    bytes_written: int


class ValidationResult(TypedDict):
    ok: bool
    issues: list[ValidationIssue]


class BuildCodeResult(TypedDict):
    ok: bool
    output_path: str


class DockerBuildResult(TypedDict):
    ok: bool
    image_name: str
    tag: str
    image_ref: str
    archive_path: str
    remote_archive_path: str
    command_label: str
    stderr: str
    error_code: str


class SampleAppTools:
    def __init__(self):
        self._handlers: dict[ToolName, Any] = {
            "execution_file_write": self.execution_file_write,
            "code_validator": self.code_validator,
            "build_code": self.build_code,
            "docker_build": self.docker_build,
        }

    def call(self, tool_name: ToolName, **kwargs: Any) -> Any:
        handler = self._handlers[tool_name]
        return handler(**kwargs)

    def execution_file_write(
        self,
        path: Path,
        content: str,
        overwrite: bool = True,
        create_parent: bool = True,
    ) -> FileWriteResult:
        with timed_step(logger, "sample_app_gen.tools.execution_file_write", path=str(path)):
            emit_event(owner="sample_app", phase="tool.execution_file_write", status="started", message="파일 쓰기 도구를 호출합니다.", details={"path": str(path)})
            if path.exists() and not overwrite:
                raise FileExistsError(f"File already exists: {path}")
            if create_parent:
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            emit_event(owner="sample_app", phase="tool.execution_file_write", status="completed", message="파일 쓰기가 완료되었습니다.", details={"path": str(path)})
            return {
                "ok": True,
                "written_path": str(path),
                "bytes_written": len(content.encode("utf-8")),
            }

    def code_validator(
        self,
        project_dir: Path,
        expected_files: list[str],
        existing_files: dict[str, str],
        framework: str,
    ) -> ValidationResult:
        with timed_step(logger, "sample_app_gen.tools.code_validator", project_dir=str(project_dir)):
            emit_event(owner="sample_app", phase="tool.code_validator", status="started", message="코드 검증 도구를 호출합니다.", details={"project_dir": str(project_dir)})
            issues: list[ValidationIssue] = []
            for relative_path in expected_files:
                path = project_dir / relative_path
                if not path.exists():
                    issues.append(ValidationIssue(path=relative_path, message="Expected file is missing."))

            for relative_path, content in existing_files.items():
                if not relative_path.endswith(".py"):
                    continue
                try:
                    ast.parse(content)
                except SyntaxError as exc:
                    issues.append(ValidationIssue(path=relative_path, message=f"Python syntax error: {exc.msg}"))

            normalized = framework.strip().lower()
            if normalized == "fastapi":
                if "app/main.py" not in existing_files:
                    issues.append(ValidationIssue(path="app/main.py", message="FastAPI entrypoint is required."))
            elif normalized in {"spring", "spring boot"}:
                has_pom = any(Path(path).name == "pom.xml" for path in existing_files)
                has_gradle = any(Path(path).name in {"build.gradle", "build.gradle.kts"} for path in existing_files)
                if not has_pom and not has_gradle:
                    issues.append(
                        ValidationIssue(
                            path="pom.xml|build.gradle",
                            message="Java build descriptor is required (Maven pom.xml or Gradle build.gradle).",
                        )
                    )

                # Validate by semantic markers instead of hard-coded package/file path.
                has_spring_entrypoint = any(
                    path.endswith(".java")
                    and "@SpringBootApplication" in content
                    and "SpringApplication.run(" in content
                    for path, content in existing_files.items()
                )
                if not has_spring_entrypoint:
                    issues.append(
                        ValidationIssue(
                            path="src/main/java/**/Application.java",
                            message="Spring boot entrypoint is required.",
                        )
                    )

            result = {
                "ok": len(issues) == 0,
                "issues": issues,
            }
            emit_event(
                owner="sample_app",
                phase="tool.code_validator",
                status="completed" if result["ok"] else "failed",
                message="코드 검증 도구 실행이 완료되었습니다." if result["ok"] else "코드 검증 도구에서 오류가 발견되었습니다.",
                details={"issue_count": len(issues)},
            )
            return result

    def build_code(self, project_dir: Path, output_base: Path) -> BuildCodeResult:
        with timed_step(logger, "sample_app_gen.tools.build_code", project_dir=str(project_dir), output_base=str(output_base)):
            emit_event(owner="sample_app", phase="tool.build_code", status="started", message="아티팩트 패키징 도구를 호출합니다.", details={"project_dir": str(project_dir)})
            output_base.parent.mkdir(parents=True, exist_ok=True)
            archive_path = shutil.make_archive(str(output_base), "zip", root_dir=project_dir)
            emit_event(owner="sample_app", phase="tool.build_code", status="completed", message="아티팩트 패키징이 완료되었습니다.", details={"output_path": archive_path})
            return {"ok": True, "output_path": archive_path}

    def docker_build(
        self,
        project_dir: Path,
        image_name: str,
        request: UserRequest,
        output_dir: Path,
        tag: str = "latest",
    ) -> DockerBuildResult:
        with timed_step(logger, "sample_app_gen.tools.docker_build", image_name=image_name, tag=tag):
            emit_event(owner="sample_app", phase="tool.docker_build", status="started", message="Docker 이미지 빌드/전송 도구를 호출합니다.", details={"image_name": image_name, "tag": tag})
            target = request.targets[0] if request.targets else None
            image_ref = self._image_ref(image_name=image_name, tag=tag)
            archive_path = output_dir / f"{self._safe_name(image_ref)}.tar"
            remote_archive_path = f"/tmp/{archive_path.name}"
            command_label = f"docker_build --target {image_ref}"

            if target is None:
                result = self._docker_error(
                    image_name=image_name,
                    tag=tag,
                    image_ref=image_ref,
                    archive_path=str(archive_path),
                    remote_archive_path=remote_archive_path,
                    command_label=command_label,
                    error_code="E_TARGET_MISSING",
                    stderr="No target host was provided for docker image upload.",
                )
                emit_event(owner="sample_app", phase="tool.docker_build", status="failed", message="대상 서버 정보가 없어 이미지 전송을 시작할 수 없습니다.", details={"error_code": result["error_code"]})
                return result

            if target.auth_method != "pem_path":
                result = self._docker_error(
                    image_name=image_name,
                    tag=tag,
                    image_ref=image_ref,
                    archive_path=str(archive_path),
                    remote_archive_path=remote_archive_path,
                    command_label=command_label,
                    error_code="E_AUTH_UNSUPPORTED",
                    stderr=f"Unsupported auth_method for docker image upload: {target.auth_method}",
                )
                emit_event(owner="sample_app", phase="tool.docker_build", status="failed", message="지원하지 않는 인증 방식으로 이미지 전송이 중단되었습니다.", details={"error_code": result["error_code"], "auth_method": target.auth_method})
                return result

            if os.getenv("SAMPLE_APP_AGENT_DRY_RUN", "false").lower() != "false":
                result = {
                    "ok": True,
                    "image_name": image_name,
                    "tag": tag,
                    "image_ref": image_ref,
                    "archive_path": str(archive_path),
                    "remote_archive_path": remote_archive_path,
                    "command_label": command_label + " --dry-run",
                    "stderr": "",
                    "error_code": "",
                }
                emit_event(owner="sample_app", phase="tool.docker_build", status="completed", message="드라이런 모드로 이미지 빌드/전송을 건너뛰었습니다.", details={"image_ref": image_ref})
                return result

            output_dir.mkdir(parents=True, exist_ok=True)

            build_result = subprocess.run(
                ["docker", "build", "-t", image_ref, str(project_dir)],
                check=False,
                text=True,
                capture_output=True,
                timeout=1800,
            )
            if build_result.returncode != 0:
                result = self._docker_error(
                    image_name=image_name,
                    tag=tag,
                    image_ref=image_ref,
                    archive_path=str(archive_path),
                    remote_archive_path=remote_archive_path,
                    command_label=command_label,
                    error_code="E_DOCKER_BUILD",
                    stderr=build_result.stderr or build_result.stdout,
                )
                emit_event(owner="sample_app", phase="tool.docker_build", status="failed", message="Docker build가 실패했습니다.", details={"error_code": result["error_code"]})
                return result

            save_result = subprocess.run(
                ["docker", "image", "save", "-o", str(archive_path), image_ref],
                check=False,
                text=True,
                capture_output=True,
                timeout=1800,
            )
            if save_result.returncode != 0:
                result = self._docker_error(
                    image_name=image_name,
                    tag=tag,
                    image_ref=image_ref,
                    archive_path=str(archive_path),
                    remote_archive_path=remote_archive_path,
                    command_label=command_label,
                    error_code="E_DOCKER_SAVE",
                    stderr=save_result.stderr or save_result.stdout,
                )
                emit_event(owner="sample_app", phase="tool.docker_build", status="failed", message="Docker image save가 실패했습니다.", details={"error_code": result["error_code"]})
                return result

            scp_result = subprocess.run(
                self._build_scp_command(target=target, local_path=archive_path, remote_path=remote_archive_path),
                check=False,
                text=True,
                capture_output=True,
                timeout=1800,
            )
            if scp_result.returncode != 0:
                result = self._docker_error(
                    image_name=image_name,
                    tag=tag,
                    image_ref=image_ref,
                    archive_path=str(archive_path),
                    remote_archive_path=remote_archive_path,
                    command_label=command_label,
                    error_code="E_IMAGE_UPLOAD",
                    stderr=scp_result.stderr or scp_result.stdout,
                )
                emit_event(owner="sample_app", phase="tool.docker_build", status="failed", message="Docker 이미지 업로드가 실패했습니다.", details={"error_code": result["error_code"]})
                return result

            remote_result = subprocess.run(
                self._build_ssh_command(
                    target=target,
                    remote_command=f"docker load -i {self._shell_quote(remote_archive_path)} && rm -f {self._shell_quote(remote_archive_path)}",
                ),
                check=False,
                text=True,
                capture_output=True,
                timeout=1800,
            )
            if remote_result.returncode != 0:
                result = self._docker_error(
                    image_name=image_name,
                    tag=tag,
                    image_ref=image_ref,
                    archive_path=str(archive_path),
                    remote_archive_path=remote_archive_path,
                    command_label=command_label,
                    error_code="E_DOCKER_REMOTE_LOAD",
                    stderr=remote_result.stderr or remote_result.stdout,
                )
                emit_event(owner="sample_app", phase="tool.docker_build", status="failed", message="대상 서버 docker load가 실패했습니다.", details={"error_code": result["error_code"]})
                return result

            result = {
                "ok": True,
                "image_name": image_name,
                "tag": tag,
                "image_ref": image_ref,
                "archive_path": str(archive_path),
                "remote_archive_path": remote_archive_path,
                "command_label": command_label,
                "stderr": "",
                "error_code": "",
            }
            emit_event(owner="sample_app", phase="tool.docker_build", status="completed", message="Docker 이미지 빌드와 대상 서버 적재가 완료되었습니다.", details={"image_ref": image_ref, "remote_archive_path": remote_archive_path})
            return result

    def _image_ref(self, image_name: str, tag: str) -> str:
        return image_name if ":" in image_name.rsplit("/", maxsplit=1)[-1] else f"{image_name}:{tag}"

    def _safe_name(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "-", value)

    def _common_ssh_options(self) -> list[str]:
        strict_host_key_checking = os.getenv("SSH_STRICT_HOST_KEY_CHECKING", "accept-new").strip() or "accept-new"
        user_known_hosts_file = os.getenv("SSH_USER_KNOWN_HOSTS_FILE", "").strip()
        if not user_known_hosts_file:
            user_known_hosts_file = str(Path.home() / ".ssh" / "known_hosts")
        return [
            "-o",
            f"StrictHostKeyChecking={strict_host_key_checking}",
            "-o",
            f"UserKnownHostsFile={user_known_hosts_file}",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
        ]

    def _build_ssh_command(self, target: TargetHost, remote_command: str) -> list[str]:
        base = ["ssh", *self._common_ssh_options()]
        if target.auth_method == "pem_path":
            base += ["-i", target.auth_ref]
        base += ["-p", str(target.ssh_port), f"{target.user}@{target.host}", remote_command]
        return base

    def _build_scp_command(self, target: TargetHost, local_path: Path, remote_path: str) -> list[str]:
        base = ["scp", *self._common_ssh_options()]
        if target.auth_method == "pem_path":
            base += ["-i", target.auth_ref]
        base += ["-P", str(target.ssh_port), str(local_path), f"{target.user}@{target.host}:{remote_path}"]
        return base

    def _shell_quote(self, value: str) -> str:
        return "'" + value.replace("'", "'\"'\"'") + "'"

    def _docker_error(
        self,
        *,
        image_name: str,
        tag: str,
        image_ref: str,
        archive_path: str,
        remote_archive_path: str,
        command_label: str,
        error_code: str,
        stderr: str,
    ) -> DockerBuildResult:
        return {
            "ok": False,
            "image_name": image_name,
            "tag": tag,
            "image_ref": image_ref,
            "archive_path": archive_path,
            "remote_archive_path": remote_archive_path,
            "command_label": command_label,
            "stderr": stderr,
            "error_code": error_code,
        }
