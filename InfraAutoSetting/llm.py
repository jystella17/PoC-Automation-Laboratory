from __future__ import annotations

import json

from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step
from base_llm import BaseLLM
from shared.utils import extract_prior_notes

SYSTEM_PROMPT = """You are a senior system engineer / infrastructure automation engineer.
Generate safe, idempotent Linux/Windows shell scripts for infra setup based on the request.

Script structure rules:
- Must start with #!/usr/bin/env bash and set -euo pipefail.
- Do NOT end the script with 'exit 0' or 'exit'. Let the script complete naturally.
- Define ALL shell variables before referencing them. Never leave variables unbound under set -u.
- Use literal paths for logging directories (e.g. mkdir -p /var/log/app), not just shell variables that may not be visible to post-validation.
- Respect sudo policy and logging directory policy.
- Do not include destructive commands.

Component installation rules:
- Java on Amazon Linux: java-{major}-amazon-corretto-devel (via dnf)
- Java on RHEL/CentOS/Rocky/Fedora/AlmaLinux: java-{major}-openjdk-devel (via dnf)
- Java on Ubuntu/Debian: openjdk-{major}-jdk (via apt)
- Apache on RHEL/Amazon Linux: httpd (via dnf). Apache on Ubuntu/Debian: apache2 (via apt).
- After Java install, verify the installed major version matches the requested version.
- When a full resolved version is provided in resolved_versions, prefer it over major-only tokens.

Network and security policy:
- Deny by default for firewall/security-group modification.
- If constraints.network_policy.allow_firewall_changes is false or missing, do not generate commands that modify ufw/firewalld/iptables/nftables or cloud security groups.
- When blocked by policy, emit a concise manual-change checklist via safe echo comments, not active change commands.

Port 80 policy for Apache:
- Default policy allows TCP/80 for HTTP ingress.
- If the user explicitly asks to use another port in additional_request, that instruction overrides the default.
- If port 80 is blocked, propose safe alternatives (8010/8443/reverse proxy/internal LB) as comments.

Apache/Tomcat config policy:
- If constraints.apache_config_mode is system_prompt_default or missing, use safe default handling: backup existing config, apply minimal changes, validate config syntax, then reload/restart.
- Never overwrite existing config without creating a backup and rollback hint.
- Backup destination must be OUTSIDE the source directory to avoid self-referential copy errors. Use /tmp or /var/backups, NOT a subdirectory of the config directory itself (e.g. backup /etc/httpd to /tmp/httpd-backup-TIMESTAMP, NOT to /etc/httpd/backups/).
- Detect OS/distribution-specific config paths before applying edits.
"""

logger = get_agent_logger("infra_auto_setting.llm", "infra_auto_setting.log")


class InfraScriptGeneratorLLM(BaseLLM):
    def __init__(self, settings: AzureOpenAISettings):
        super().__init__(settings)

    def generate_install_script(
        self,
        request: UserRequest,
        resolved_versions: dict[str, str],
        package_manager: str,
        prior_executions: list[AgentExecution],
        fallback_script: str,
    ) -> str | None:
        with timed_step(
            logger,
            "infra_auto_setting.llm.generate_install_script",
            component_count=len(request.infra_tech_stack.components),
        ):
            prior_notes = extract_prior_notes(prior_executions)
            human_prompt = (
                "Return only raw bash script content. No markdown fences.\n"
                "Generate a full infra bootstrap script for the target host.\n"
                "Script requirements:\n"
                "- Must include: #!/usr/bin/env bash and set -euo pipefail\n"
                "- Must not contain destructive commands like rm -rf /\n"
                "- Must honor constraints.sudo_allowed\n"
                "- Must create and chmod logging directories\n"
                "- Use package manager aligned to target OS\n"
                "- If request.infra_tech_stack.versions and Resolved versions JSON differ, always use Resolved versions JSON as the source of truth\n"
                "- Never build install commands from a major-only version token when a resolved full version is provided\n"
                f"Package manager: {package_manager}\n"
                f"Resolved versions JSON:\n{json.dumps(resolved_versions, ensure_ascii=False, indent=2)}\n"
                f"User request JSON:\n{request.model_dump_json(indent=2)}\n"
                f"Prior execution notes:\n{prior_notes}\n"
                "If an installation is uncertain, emit a safe placeholder echo command rather than a dangerous operation.\n"
                f"Fallback deterministic script (reference):\n{fallback_script}\n"
            )

            script = self._invoke_llm(SYSTEM_PROMPT, human_prompt, strip_fences=True)
            if script is None:
                return None

            if not self._basic_guard(script):
                log_event(logger, "infra_auto_setting.llm.generate_install_script.rejected", reason="basic_guard_failed")
                return None

            log_event(
                logger,
                "infra_auto_setting.llm.generate_install_script.result",
                length=len(script),
            )
            return script if script.endswith("\n") else script + "\n"

    def repair_script(
        self,
        script: str,
        issues: list[str],
        request: UserRequest,
        resolved_versions: dict[str, str],
        package_manager: str,
        fallback_script: str,
    ) -> str | None:
        with timed_step(logger, "infra_auto_setting.llm.repair_script", issue_count=len(issues)):
            issues_text = "\n".join(f"- {issue}" for issue in issues)
            human_prompt = (
                "The following infra bootstrap script has validation issues that must be fixed.\n"
                "Return only the corrected raw bash script. No markdown fences.\n\n"
                f"Current script:\n{script}\n\n"
                f"Validation issues:\n{issues_text}\n\n"
                f"Package manager: {package_manager}\n"
                f"Resolved versions JSON:\n{json.dumps(resolved_versions, ensure_ascii=False, indent=2)}\n"
                f"User request JSON:\n{request.model_dump_json(indent=2)}\n"
                f"Fallback deterministic script (reference):\n{fallback_script}\n\n"
                "Fix ALL listed issues. Do not introduce new issues. "
                "Do not end the script with 'exit 0'. "
                "Define all shell variables before use.\n"
            )
            result = self._invoke_llm(SYSTEM_PROMPT, human_prompt, strip_fences=True)
            if result is None:
                return None
            if not self._basic_guard(result):
                log_event(logger, "infra_auto_setting.llm.repair_script.rejected", reason="basic_guard_failed")
                return None
            log_event(logger, "infra_auto_setting.llm.repair_script.result", length=len(result))
            return result if result.endswith("\n") else result + "\n"

    def _basic_guard(self, script: str) -> bool:
        if "#!/usr/bin/env bash" not in script:
            return False
        if "set -euo pipefail" not in script:
            return False
        if "rm -rf /" in script:
            return False
        return True

