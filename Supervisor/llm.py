from __future__ import annotations

from .models import AgentExecution, SupervisorRunResult
from .config import AzureOpenAISettings
from .models import BuildPlan, MissingRequirement, PlanStep, UserRequest


SUPERVISOR_SYSTEM_PROMPT = """You are the Supervisor Agent for an infra test automation lab.
You are also a senior Infra Engineer with 20 years of hands-on experience.
Your role is to assess user requests, identify missing requirements, and orchestrate infra/app execution safely.
Prioritize operational correctness, deterministic planning, deployment safety, and explicit assumptions.
Do not fabricate missing facts. If required information is missing, clearly state that execution is blocked and ask for the exact missing inputs.
When the request is complete, explain what work will be performed, in what order, and why.
Do not expose internal implementation labels such as plan, dispatch, build_infra, generate_app, or LangGraph unless the user explicitly asks for them.
Keep answers concise, structured, practical, and suitable for an engineering handoff.
"""


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

    def generate_supervisor_reply(
        self,
        request: UserRequest,
        plan: BuildPlan,
        run_result: SupervisorRunResult | None = None,
    ) -> str:
        if self.settings.enabled and self.settings.is_configured:
            azure_reply = self._generate_reply_with_azure(request, plan, run_result)
            if azure_reply:
                return azure_reply

        return self._fallback_reply(request, plan, run_result)

    def _target_summary(self, request: UserRequest) -> str:
        if not request.targets:
            return "없음"
        return ", ".join(f"{target.host} ({target.user})" for target in request.targets)

    def _humanize_step(self, step: PlanStep) -> str:
        if step.name == "plan":
            return "입력된 요구사항과 대상 환경 정보를 검토하고, 바로 실행 가능한 상태인지 확인합니다."
        if step.name == "build_infra":
            if step.status == "failed":
                return "필수 정보가 아직 부족해 인프라 설치 및 환경 구성 작업은 시작할 수 없습니다."
            return "대상 서버에 필요한 인프라 구성요소를 설치하고, 로그 경로와 기본 실행 환경을 준비합니다."
        if step.name == "generate_app":
            if step.status == "failed":
                return "필수 정보가 아직 부족해 샘플 애플리케이션 생성 및 배포 준비 작업은 시작할 수 없습니다."
            return "요청한 프레임워크와 언어 기준으로 샘플 애플리케이션을 생성하고, 배포 가능한 산출물을 준비합니다."
        return step.detail

    def _summarize_with_azure(
        self,
        request: UserRequest,
        missing_requirements: list[MissingRequirement],
    ) -> str | None:
        llm = self._create_llm()
        if llm is None:
            return None

        components = ", ".join(request.infra_tech_stack.components) or "none"
        languages = ", ".join(request.app_tech_stack.language) or "none"
        missing_fields = ", ".join(item.field for item in missing_requirements) or "none"
        human_prompt = (
            "Respond in Korean.\n"
            "Write a single planning summary sentence in natural language.\n"
            "Do not mention internal node names or implementation labels.\n"
            f"Infra components: {components}\n"
            f"App framework: {request.app_tech_stack.framework}\n"
            f"Languages: {languages}\n"
            f"Targets: {self._target_summary(request)}\n"
            f"Additional request: {request.additional_request or 'none'}\n"
            f"Missing fields: {missing_fields}\n"
            "If required fields are missing, explicitly say execution is blocked."
        )

        response = llm.invoke([
            ("system", SUPERVISOR_SYSTEM_PROMPT),
            ("human", human_prompt),
        ])
        content = getattr(response, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return None

    def _fallback_summary(self, request: UserRequest, missing_requirements: list[MissingRequirement]) -> str:
        components = ", ".join(request.infra_tech_stack.components) or "선택된 인프라 구성요소 없음"
        framework = request.app_tech_stack.framework or "애플리케이션 프레임워크 미지정"
        summary = f"{components} 환경과 {framework} 애플리케이션 구성을 기준으로 실행 준비 상태를 검토했습니다."
        if request.additional_request.strip():
            summary += " 추가 요청 사항도 함께 반영해 작업 범위를 정리했습니다."
        if missing_requirements:
            summary += " 다만 필수 정보가 아직 부족해 실제 작업은 보류된 상태입니다."
        else:
            summary += " 필요한 정보가 충족되어 후속 작업을 순서대로 진행할 수 있습니다."
        return summary

    def _generate_reply_with_azure(
        self,
        request: UserRequest,
        plan: BuildPlan,
        run_result: SupervisorRunResult | None = None,
    ) -> str | None:
        llm = self._create_llm()
        if llm is None:
            return None

        components = ", ".join(request.infra_tech_stack.components) or "none"
        languages = ", ".join(request.app_tech_stack.language) or "none"
        step_lines = "\n".join(
            f"{index}. {self._humanize_step(step)}" for index, step in enumerate(plan.steps, start=1)
        ) or "1. 현재 수행 예정 작업이 정리되지 않았습니다."
        missing_lines = "\n".join(f"- {item.question}" for item in plan.missing_requirements) or "- 없음"
        execution_summary = self._execution_summary(run_result)
        human_prompt = (
            "Respond in Korean.\n"
            "Explain the request in user-facing language.\n"
            "Under the section '수행할 작업 설명', describe the work in natural language instead of internal agent names or plan node names.\n"
            "If execution results are available, include them under the section '실행 결과'.\n"
            "If required fields are missing, say execution is blocked and ask for the missing answers.\n"
            "If the request is complete, explain what will be done first, next, and why.\n"
            f"Infra components: {components}\n"
            f"App framework: {request.app_tech_stack.framework or 'none'}\n"
            f"Languages: {languages}\n"
            f"Targets: {self._target_summary(request)}\n"
            f"Additional request: {request.additional_request or 'none'}\n"
            f"Plan summary: {plan.summary}\n"
            f"Planned work descriptions:\n{step_lines}\n"
            f"Missing requirements:\n{missing_lines}\n"
            f"Execution summary:\n{execution_summary}\n"
            "Return a structured answer with these sections if relevant: 요청 내용, 수행할 작업 설명, 실행 결과, 추가 확인 필요."
        )

        response = llm.invoke([
            ("system", SUPERVISOR_SYSTEM_PROMPT),
            ("human", human_prompt),
        ])
        content = getattr(response, "content", "")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return None

    def _fallback_reply(
        self,
        request: UserRequest,
        plan: BuildPlan,
        run_result: SupervisorRunResult | None = None,
    ) -> str:
        components = ", ".join(request.infra_tech_stack.components) or "없음"
        languages = ", ".join(request.app_tech_stack.language) or "없음"
        lines = [
            "요청 내용",
            f"- 인프라 기술스택: {components}",
            f"- 애플리케이션 프레임워크: {request.app_tech_stack.framework or '없음'}",
            f"- 언어: {languages}",
            f"- 대상 서버: {self._target_summary(request)}",
        ]

        if request.additional_request.strip():
            lines.append(f"- 추가 요청: {request.additional_request}")

        lines.append("")
        if plan.missing_requirements:
            lines.append("추가 확인 필요")
            lines.append("- 필수 정보가 부족해 현재는 실제 작업을 시작할 수 없습니다.")
            for item in plan.missing_requirements:
                lines.append(f"- {item.question}")
        else:
            lines.append("수행할 작업 설명")
            for index, step in enumerate(plan.steps, start=1):
                lines.append(f"- {index}. {self._humanize_step(step)}")

        if run_result is not None:
            lines.append("")
            lines.append("실행 결과")
            lines.extend(self._execution_summary_lines(run_result))

        return "\n".join(lines)

    def _execution_summary(self, run_result: SupervisorRunResult | None) -> str:
        if run_result is None:
            return "none"
        return "\n".join(self._execution_summary_lines(run_result))

    def _execution_summary_lines(self, run_result: SupervisorRunResult) -> list[str]:
        lines = [f"- 최종 상태: {run_result.final_summary or 'n/a'}"]
        for execution in run_result.executed:
            lines.append(
                f"- {execution.agent}: {'success' if execution.success else 'failed'}"
            )
            if execution.executed_commands:
                lines.append(f"- {execution.agent} commands: {', '.join(execution.executed_commands[:3])}")
            note = self._first_meaningful_note(execution)
            if note:
                lines.append(f"- {execution.agent} note: {note}")
        if run_result.generated_outputs:
            lines.append(f"- generated outputs: {', '.join(run_result.generated_outputs[:3])}")
        return lines

    def _first_meaningful_note(self, execution: AgentExecution) -> str:
        for note in execution.notes:
            if note.strip():
                return note
        return ""
