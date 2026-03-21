from __future__ import annotations

import json
import re

from Supervisor.config import AzureOpenAISettings
from Supervisor.models import AgentExecution, UserRequest
from agent_logging import get_agent_logger, log_event, timed_step
from base_llm import BaseLLM
from shared.utils import extract_prior_notes

SYSTEM_PROMPT = """You are a senior infrastructure automation engineer.
Generate safe, idempotent Linux shell scripts for infra setup based on the request.
Respect sudo policy and logging directory policy.
Do not include destructive commands.
Return JSON only when asked. Return raw script text only when asked for script.

Network and security policy:
- Deny by default for firewall/security-group modification.
- If constraints.network_policy.allow_firewall_changes is false or missing, do not generate commands that modify ufw/firewalld/iptables/nftables or cloud security groups.
- When blocked by policy, emit a concise manual-change checklist via safe echo comments, not active change commands.

Port 80 policy for Apache:
- Default policy allows TCP/80 for HTTP ingress.
- If the user explicitly asks to block or avoid port 80 in additional_request, that instruction overrides the default.
- If port 80 is blocked, propose safe alternatives (8080/8443/reverse proxy/internal LB) as comments.

Apache/Tomcat config policy:
- If constraints.apache_config_mode is system_prompt_default or missing, use safe default handling: backup existing config, apply minimal changes, validate config syntax, then reload/restart.
- Never overwrite existing config without creating a backup and rollback hint.
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
            llm = self._create_llm()
            if llm is None:
                log_event(logger, "infra_auto_setting.llm.generate_install_script.skipped", reason="llm_not_available")
                return None

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

            try:
                response = llm.invoke([("system", SYSTEM_PROMPT), ("human", human_prompt)])
            except Exception as exc:
                log_event(logger, "infra_auto_setting.llm.generate_install_script.error", error=str(exc))
                return None

            content = getattr(response, "content", "")
            if not isinstance(content, str) or not content.strip():
                log_event(logger, "infra_auto_setting.llm.generate_install_script.empty")
                return None

            script = self._strip_code_fences(content)
            if not self._basic_guard(script):
                log_event(logger, "infra_auto_setting.llm.generate_install_script.rejected", reason="basic_guard_failed")
                return None

            log_event(
                logger,
                "infra_auto_setting.llm.generate_install_script.result",
                length=len(script),
            )
            return script if script.endswith("\n") else script + "\n"

    def _basic_guard(self, script: str) -> bool:
        if "#!/usr/bin/env bash" not in script:
            return False
        if "set -euo pipefail" not in script:
            return False
        if "rm -rf /" in script:
            return False
        return True

