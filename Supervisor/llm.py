from __future__ import annotations

from .config import AzureOpenAISettings
from .models import BuildPlan, MissingRequirement, UserRequest


class SupervisorLLM:
    def __init__(self, settings: AzureOpenAISettings):
        self.settings = settings

    def _create_llm(self):
        try:
            from langchain_openai import AzureChatOpenAI
        except ImportError:
            return None

        return AzureChatOpenAI(
            azure_endpoint=self.settings.endpoint,
            api_key=self.settings.api_key,
            azure_deployment=self.settings.deployment_name,
            api_version=self.settings.api_version,
            temperature=self.settings.temperature,
        )

    def summarize_plan(self, request: UserRequest, missing_requirements: list[MissingRequirement]) -> str:
        if self.settings.enabled and self.settings.is_configured:
            azure_summary = self._summarize_with_azure(request, missing_requirements)
            if azure_summary:
                return azure_summary

        return self._fallback_summary(request, missing_requirements)

    def generate_supervisor_reply(self, request: UserRequest, plan: BuildPlan) -> str:
        if self.settings.enabled and self.settings.is_configured:
            azure_reply = self._generate_reply_with_azure(request, plan)
            if azure_reply:
                return azure_reply

        return self._fallback_reply(request, plan)

    def _summarize_with_azure(
        self,
        request: UserRequest,
        missing_requirements: list[MissingRequirement],
    ) -> str | None:
        llm = self._create_llm()
        if llm is None:
            return None

        languages = ", ".join(request.app_tech_stack.language) or "none"
        missing_fields = ", ".join(item.field for item in missing_requirements) or "none"
        prompt = (
            "You are a supervisor agent for infra test lab planning.\n"
            f"Infra components: {', '.join(request.infra_tech_stack.components) or 'none'}\n"
            f"App framework: {request.app_tech_stack.framework}\n"
            f"Languages: {languages}\n"
            f"Additional request: {request.additional_request or 'none'}\n"
            f"Missing fields: {missing_fields}\n"
            "Return one concise planning summary sentence."
        )

        response = llm.invoke(prompt)
        content = getattr(response, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return None

    def _fallback_summary(self, request: UserRequest, missing_requirements: list[MissingRequirement]) -> str:
        components = ", ".join(request.infra_tech_stack.components) or "no components selected"
        summary = f"Prepared LangGraph workflow for {components} with app framework {request.app_tech_stack.framework}."
        if request.additional_request.strip():
            summary += " Included the free-form request in the planning context."
        if missing_requirements:
            summary += " Execution is blocked until the missing requirements are answered."
        return summary

    def _generate_reply_with_azure(self, request: UserRequest, plan: BuildPlan) -> str | None:
        llm = self._create_llm()
        if llm is None:
            return None

        step_lines = "\n".join(f"- {step.name}: {step.detail}" for step in plan.steps) or "- no steps"
        missing_lines = "\n".join(f"- {item.field}: {item.question}" for item in plan.missing_requirements) or "- none"
        prompt = (
            "You are the Supervisor Agent for an infra test automation lab.\n"
            "Respond in Korean.\n"
            "Explain the overall execution order clearly and concisely.\n"
            "If required fields are missing, say execution is blocked and ask for the missing answers.\n"
            "If the request is complete, explain which sub-agents will run in what order.\n"
            f"Infra components: {', '.join(request.infra_tech_stack.components) or 'none'}\n"
            f"App framework: {request.app_tech_stack.framework}\n"
            f"Languages: {', '.join(request.app_tech_stack.language) or 'none'}\n"
            f"Additional request: {request.additional_request or 'none'}\n"
            f"Plan summary: {plan.summary}\n"
            f"Plan steps:\n{step_lines}\n"
            f"Missing requirements:\n{missing_lines}\n"
            "Return a short structured answer with these sections if relevant: 요청 이해, 실행 순서, 추가 확인 필요."
        )

        response = llm.invoke(prompt)
        content = getattr(response, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return None

    def _fallback_reply(self, request: UserRequest, plan: BuildPlan) -> str:
        lines = [
            "요청 이해",
            f"- 인프라 기술스택: {', '.join(request.infra_tech_stack.components) or '없음'}",
            f"- 애플리케이션 프레임워크: {request.app_tech_stack.framework}",
            f"- 언어: {', '.join(request.app_tech_stack.language) or '없음'}",
        ]

        if request.additional_request.strip():
            lines.append(f"- 추가 요청: {request.additional_request}")

        lines.append("")
        if plan.missing_requirements:
            lines.append("추가 확인 필요")
            for item in plan.missing_requirements:
                lines.append(f"- {item.question}")
        else:
            lines.append("실행 순서")
            for index, step in enumerate(plan.steps, start=1):
                lines.append(f"- {index}. {step.name}: {step.detail}")

        return "\n".join(lines)
