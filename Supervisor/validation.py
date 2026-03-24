from __future__ import annotations

from .models import MissingRequirement, UserRequest


def has_infra_request(req: UserRequest) -> bool:
    return bool(req.infra_tech_stack.components)


def has_app_request(req: UserRequest) -> bool:
    return req.app_tech_stack.framework.strip().lower() not in {"", "none"} and req.topology.apps > 0


def check_missing_info(req: UserRequest) -> list[MissingRequirement]:
    missing: list[MissingRequirement] = []
    app_languages = [language.strip().lower() for language in req.app_tech_stack.language]
    _has_infra = has_infra_request(req)
    _has_app = has_app_request(req)

    if not req.targets:
        missing.append(
            MissingRequirement(
                field="targets",
                question="Which test server should be used for this run?",
                reason="A target host and SSH access path are required before execution.",
            )
        )
    else:
        for idx, target in enumerate(req.targets):
            prefix = f"targets[{idx}]"
            if not target.host.strip():
                missing.append(
                    MissingRequirement(
                        field=f"{prefix}.host",
                        question="What is the target host IP or hostname?",
                        reason="The supervisor cannot build or deploy without a destination host.",
                    )
                )
            if not target.user.strip():
                missing.append(
                    MissingRequirement(
                        field=f"{prefix}.user",
                        question="Which SSH user should be used on the target host?",
                        reason="Remote execution requires an explicit login account.",
                    )
                )
            if not target.auth_ref.strip():
                missing.append(
                    MissingRequirement(
                        field=f"{prefix}.auth_ref",
                        question="What secret or key reference should be used for SSH authentication?",
                        reason="The target host exists, but no SSH credential reference was provided.",
                    )
                )

    if not _has_infra and not _has_app:
        missing.append(
            MissingRequirement(
                field="infra_tech_stack.components",
                question="Which infra components should be installed, or which application framework should be generated?",
                reason="At least one infra component or an application framework is required before execution.",
            )
        )

    versions = req.infra_tech_stack.versions
    if _has_infra and not versions:
        missing.append(
            MissingRequirement(
                field="infra_tech_stack.versions",
                question="Which component versions should be applied?",
                reason="Version selection is required to generate deterministic install steps.",
            )
        )
    elif _has_infra:
        if "tomcat" in req.infra_tech_stack.components and versions.get("tomcat", "").strip() in {"", "none"}:
            missing.append(
                MissingRequirement(
                    field="infra_tech_stack.versions.tomcat",
                    question="Which Tomcat version should be installed?",
                    reason="Tomcat is selected as a component, but its version is missing.",
                )
            )
        if "kafka" in req.infra_tech_stack.components and versions.get("kafka", "").strip() in {"", "none"}:
            missing.append(
                MissingRequirement(
                    field="infra_tech_stack.versions.kafka",
                    question="Which Kafka version should be installed?",
                    reason="Kafka is selected as a component, but its version is missing.",
                )
            )
        requires_java = (
                any(component in {"tomcat", "kafka"} for component in req.infra_tech_stack.components)
                or any(language.startswith("java") for language in app_languages)
        )
        if requires_java and versions.get("java", "").strip() == "":
            missing.append(
                MissingRequirement(
                    field="infra_tech_stack.versions.java",
                    question="Which Java version should be used for the infra and app stack?",
                    reason="Java is required for Tomcat, Kafka, and most sample app flows.",
                )
            )

    if not req.logging.base_dir.strip():
        missing.append(
            MissingRequirement(
                field="logging.base_dir",
                question="Where should the base log directory be created?",
                reason="The AGENT.md gate requires a log directory policy before execution.",
            )
        )

    return missing
