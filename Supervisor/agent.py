from __future__ import annotations

from dataclasses import dataclass

from .models import AgentExecution, BuildPlan, PlanStep, SupervisorRunResult, UserRequest


@dataclass
class MissingInfoError(Exception):
    missing_fields: list[str]

    def __str__(self) -> str:
        return "Missing required input: " + ", ".join(self.missing_fields)


class SupervisorAgent:
    def _missing_info(self, req: UserRequest) -> list[str]:
        missing: list[str] = []

        if not req.targets:
            missing.append("targets")
        else:
            for idx, target in enumerate(req.targets):
                if not target.auth_ref:
                    missing.append(f"targets[{idx}].auth_ref")

        if not req.infra_tech_stack.components:
            missing.append("infra_tech_stack.components")
        if not req.infra_tech_stack.versions:
            missing.append("infra_tech_stack.versions")
        if not req.logging.base_dir:
            missing.append("logging.base_dir")

        return missing

    def plan(self, req: UserRequest) -> BuildPlan:
        missing = self._missing_info(req)
        return BuildPlan(
            summary="Parsed request and created infra/app execution plan.",
            missing_info=missing,
            steps=[
                PlanStep(
                    name="plan",
                    owner="supervisor",
                    status="completed",
                    detail="Validated required input and generated workflow skeleton.",
                ),
                PlanStep(
                    name="build_infra",
                    owner="infra_build",
                    status="pending",
                    detail="Install and configure required infra components.",
                ),
                PlanStep(
                    name="generate_app",
                    owner="sample_app",
                    status="pending",
                    detail="Generate app source and build deployable artifact.",
                ),
            ],
        )

    def _simulate_infra_build(self, req: UserRequest) -> AgentExecution:
        commands = [f"install_component --name {component}" for component in req.infra_tech_stack.components]
        commands.append(f"mkdir -p {req.logging.base_dir} {req.logging.gc_log_dir} {req.logging.app_log_dir}")
        return AgentExecution(
            agent="infra_build",
            success=True,
            executed_commands=commands,
            notes=[
                "sudo usage follows constraints.sudo_allowed.",
                "Production targets are blocked by default policy.",
            ],
        )

    def _simulate_app_generate(self, req: UserRequest) -> AgentExecution:
        framework = req.app_tech_stack.framework
        language = req.app_tech_stack.language
        return AgentExecution(
            agent="sample_app",
            success=True,
            executed_commands=[
                f"scaffold_app --framework {framework} --language {language}",
                "build_artifact --type service",
            ],
            notes=[
                "Include API endpoints and memory leak/OOM simulation options in generated app.",
                "Mask DB credentials in logs and reports.",
            ],
        )

    def run(self, req: UserRequest) -> SupervisorRunResult:
        missing = self._missing_info(req)
        if missing:
            raise MissingInfoError(missing)

        infra = self._simulate_infra_build(req)
        app = self._simulate_app_generate(req)

        return SupervisorRunResult(
            environment_summary={
                "os": req.infra_tech_stack.os,
                "components": ", ".join(req.infra_tech_stack.components),
                "targets": ", ".join(target.host for target in req.targets),
                "framework": req.app_tech_stack.framework,
            },
            executed=[infra, app],
            generated_outputs=[
                "infra bootstrap script",
                "sample app source",
                "runbook",
            ],
            recommended_config=[
                "JAVA_OPTS: -Xms2g -Xmx2g -XX:+UseG1GC",
                "Tune Kafka partitions and replication by target TPS.",
                "Store GC logs and app logs in separate directories.",
            ],
            rollback_cleanup=[
                "stop services: app/tomcat/kafka",
                "remove generated artifacts and temp files",
                "restore changed config backups",
            ],
        )
