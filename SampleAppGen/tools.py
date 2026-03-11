from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Any, Literal, TypedDict

from agent_logging import get_agent_logger, timed_step

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
            if path.exists() and not overwrite:
                raise FileExistsError(f"File already exists: {path}")
            if create_parent:
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
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
                required = {
                    "pom.xml": "Maven build descriptor is required.",
                    "src/main/java/com/example/sampleapp/SampleAppApplication.java": "Spring boot entrypoint is required.",
                }
                for path, message in required.items():
                    if path not in existing_files:
                        issues.append(ValidationIssue(path=path, message=message))

            return {
                "ok": len(issues) == 0,
                "issues": issues,
            }

    def build_code(self, project_dir: Path, output_base: Path) -> BuildCodeResult:
        with timed_step(logger, "sample_app_gen.tools.build_code", project_dir=str(project_dir), output_base=str(output_base)):
            output_base.parent.mkdir(parents=True, exist_ok=True)
            archive_path = shutil.make_archive(str(output_base), "zip", root_dir=project_dir)
            return {"ok": True, "output_path": archive_path}

    def docker_build(self, image_name: str, tag: str = "latest") -> DockerBuildResult:
        with timed_step(logger, "sample_app_gen.tools.docker_build", image_name=image_name, tag=tag):
            # Current implementation records docker build intent for downstream deployment.
            return {"ok": True, "image_name": image_name, "tag": tag}
