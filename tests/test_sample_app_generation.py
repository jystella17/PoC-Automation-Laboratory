import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from SampleAppGen.agent import SampleAppAgent
from SampleAppGen.models import ApplicationFilePlan
from SampleAppGen.tools import SampleAppTools
from Supervisor.models import AppTechStack, LoggingConfig, Topology, UserRequest


class SampleAppAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = SampleAppAgent(workspace_root="/tmp/poc-automation-lab-tests")

    def test_existing_spring_entrypoint_is_not_duplicated(self) -> None:
        file_plan = [
            ApplicationFilePlan(
                path="src/main/java/com/example/board/BoardApplication.java",
                purpose="Spring Boot entrypoint",
                language="java",
            )
        ]

        merged = self.agent._ensure_required_file_plan(
            file_plan=file_plan,
            app_id="spring-boot-spring-boot-4-0-1-crud",
            framework="Spring Boot",
            build_system="gradle",
        )

        java_entrypoints = [item.path for item in merged if item.path.endswith("Application.java")]
        self.assertEqual(["src/main/java/com/example/board/BoardApplication.java"], java_entrypoints)

    def test_board_controller_is_generated_deterministically(self) -> None:
        request = UserRequest(
            topology=Topology(apps=1),
            logging=LoggingConfig(),
            app_tech_stack=AppTechStack(framework="Spring Boot", minor_version="4.0.1", build_system="gradle", language=["Java 21"]),
            additional_request="CRUD board",
        )
        plan = self.agent._fallback_plan(
            request=request,
            project_dir=self.agent.workspace_root / "sample",
            app_id="spring-boot-spring-boot-4-0-1-crud",
            language="Java21",
        )
        file_plan = ApplicationFilePlan(
            path="src/main/java/com/example/board/controller/BoardController.java",
            purpose="REST controller",
            language="java",
        )

        content = self.agent._deterministic_file_content(request, plan, file_plan)

        self.assertIsNotNone(content)
        self.assertIn("public class BoardController", content)
        self.assertIn("postService.update(id, request).orElseThrow", content)

    def test_runtime_version_is_normalized_from_label(self) -> None:
        request = UserRequest(
            topology=Topology(apps=1),
            logging=LoggingConfig(),
            app_tech_stack=AppTechStack(framework="Spring Boot", minor_version="4.0.1", build_system="gradle", language=["Java 21"]),
            additional_request="CRUD board",
        )
        plan = self.agent._fallback_plan(
            request=request,
            project_dir=self.agent.workspace_root / "sample",
            app_id="spring-boot-spring-boot-4-0-1-crud",
            language="Java21",
        ).model_copy(update={"runtime_version": "Java 21"})

        normalized = self.agent._normalize_plan(
            plan=plan,
            request=request,
            project_dir=self.agent.workspace_root / "sample",
            app_id="spring-boot-spring-boot-4-0-1-crud",
        )
        dockerfile = self.agent._render_dockerfile_template(normalized)

        self.assertEqual("21", normalized.runtime_version)
        self.assertIn("FROM gradle:8.14-jdk21 AS builder", dockerfile)
        self.assertIn("FROM eclipse-temurin:21-jre", dockerfile)

    def test_board_supporting_files_are_added_when_board_sources_exist(self) -> None:
        merged = self.agent._ensure_required_file_plan(
            file_plan=[
                ApplicationFilePlan(
                    path="src/main/java/com/example/board/controller/BoardController.java",
                    purpose="Board REST controller",
                    language="java",
                ),
                ApplicationFilePlan(
                    path="src/main/java/com/example/board/BoardApplication.java",
                    purpose="Spring Boot entrypoint",
                    language="java",
                ),
            ],
            app_id="spring-boot-spring-boot-4-0-1-crud",
            framework="Spring Boot",
            build_system="gradle",
        )

        merged_paths = {item.path for item in merged}
        self.assertIn("src/main/java/com/example/board/dto/PostRequest.java", merged_paths)
        self.assertIn("src/main/java/com/example/board/dto/PostResponse.java", merged_paths)
        self.assertIn("src/main/java/com/example/board/repository/InMemoryPostRepository.java", merged_paths)


class SampleAppToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = SampleAppTools()

    def test_validator_rejects_multiple_spring_entrypoints(self) -> None:
        result = self.tools.code_validator(
            project_dir=self._dummy_project_dir(),
            expected_files=[],
            existing_files={
                "src/main/java/com/example/a/AppA.java": (
                    "@SpringBootApplication\n"
                    "class AppA { public static void main(String[] args) { SpringApplication.run(AppA.class, args); } }"
                ),
                "src/main/java/com/example/b/AppB.java": (
                    "@SpringBootApplication\n"
                    "class AppB { public static void main(String[] args) { SpringApplication.run(AppB.class, args); } }"
                ),
                "build.gradle": "plugins {}\ndependencies { implementation 'org.springframework.boot:spring-boot-starter-web' }",
            },
            framework="Spring Boot",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("Multiple Spring Boot entrypoints" in issue.message for issue in result["issues"]))

    def test_validator_requires_validation_dependency_when_annotations_are_used(self) -> None:
        result = self.tools.code_validator(
            project_dir=self._dummy_project_dir(),
            expected_files=[],
            existing_files={
                "src/main/java/com/example/board/dto/PostRequest.java": "import jakarta.validation.constraints.NotBlank;",
                "src/main/java/com/example/board/BoardApplication.java": (
                    "@SpringBootApplication\n"
                    "class BoardApplication { void x() { SpringApplication.run(BoardApplication.class); } }"
                ),
                "build.gradle": "plugins {}\ndependencies { implementation 'org.springframework.boot:spring-boot-starter-web' }",
            },
            framework="Spring Boot",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("spring-boot-starter-validation" in issue.message for issue in result["issues"]))

    def test_validator_accepts_spring_application_builder_style_entrypoint(self) -> None:
        result = self.tools.code_validator(
            project_dir=self._dummy_project_dir(),
            expected_files=[],
            existing_files={
                "src/main/java/com/example/crud/CrudApplication.java": (
                    "@SpringBootApplication\n"
                    "public class CrudApplication {\n"
                    "  public static void main(String[] args) {\n"
                    "    SpringApplication app = new SpringApplication(CrudApplication.class);\n"
                    "    app.run(args);\n"
                    "  }\n"
                    "}"
                ),
                "build.gradle": "plugins {}\ndependencies { implementation 'org.springframework.boot:spring-boot-starter-web' }",
            },
            framework="Spring Boot",
        )

        self.assertTrue(result["ok"])

    def test_validator_rejects_incomplete_board_crud_source_set(self) -> None:
        result = self.tools.code_validator(
            project_dir=self._dummy_project_dir(),
            expected_files=[],
            existing_files={
                "src/main/java/com/example/board/BoardApplication.java": (
                    "@SpringBootApplication\n"
                    "class BoardApplication { void x() { SpringApplication.run(BoardApplication.class); } }"
                ),
                "src/main/java/com/example/board/controller/BoardController.java": "package com.example.board.controller;",
                "build.gradle": "plugins {}\ndependencies { implementation 'org.springframework.boot:spring-boot-starter-web'; implementation 'org.springframework.boot:spring-boot-starter-validation' }",
            },
            framework="Spring Boot",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(any("Board CRUD source set is incomplete" in issue.message for issue in result["issues"]))

    def _dummy_project_dir(self):
        return Path("/tmp")

    def test_build_code_returns_jar_artifact_not_zip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "build.gradle").write_text("plugins {}", encoding="utf-8")
            libs_dir = project_dir / "build" / "libs"
            libs_dir.mkdir(parents=True, exist_ok=True)
            built_jar = libs_dir / "demo-0.0.1.jar"
            built_jar.write_text("jar-bytes", encoding="utf-8")

            completed = type("CompletedProcess", (), {"returncode": 0, "stderr": "", "stdout": ""})()
            with patch("SampleAppGen.tools.subprocess.run", return_value=completed):
                result = self.tools.build_code(project_dir=project_dir, output_base=project_dir / "artifacts" / "demo")

            self.assertTrue(result["ok"])
            self.assertTrue(result["output_path"].endswith(".jar"))
            self.assertFalse(result["output_path"].endswith(".zip"))

    def test_build_code_uses_dockerized_gradle_when_local_gradle_is_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "build.gradle").write_text("plugins {}", encoding="utf-8")
            libs_dir = project_dir / "build" / "libs"
            libs_dir.mkdir(parents=True, exist_ok=True)
            built_jar = libs_dir / "demo-0.0.1.jar"
            built_jar.write_text("jar-bytes", encoding="utf-8")

            completed = type("CompletedProcess", (), {"returncode": 0, "stderr": "", "stdout": ""})()
            with patch("SampleAppGen.tools.shutil.which", side_effect=lambda name: None if name == "gradle" else "/usr/bin/docker"), \
                 patch("SampleAppGen.tools.subprocess.run", return_value=completed) as mock_run:
                result = self.tools.build_code(project_dir=project_dir, output_base=project_dir / "artifacts" / "demo")

            self.assertTrue(result["ok"])
            invoked_command = mock_run.call_args.args[0]
            self.assertEqual("docker", invoked_command[0])
            self.assertIn("gradle:8.14-jdk21", invoked_command)
            self.assertIn("GRADLE_USER_HOME=/tmp/gradle-home", invoked_command)
            self.assertIn("-Dorg.gradle.native=false", invoked_command)


if __name__ == "__main__":
    unittest.main()
