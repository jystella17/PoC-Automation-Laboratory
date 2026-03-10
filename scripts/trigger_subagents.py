from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from InfraAutoSetting import InfraAutoSettingAgent
from SampleAppGen import SampleAppAgent
from Supervisor.config import load_settings
from Supervisor.models import AgentExecution, UserRequest


DEFAULT_USER_REQUEST: dict[str, Any] = {
    "infra_tech_stack": {
        "os": "Linux",
        "components": ["apache", "tomcat"],
        "versions": {
            "apache": "2.4.66",
            "tomcat": "10",
            "kafka": "None",
            "pinpoint": "None",
            "java": "17",
        },
        "instances": {
            "apache": 1,
            "tomcat": 1,
            "kafka_consumer": 0,
            "pinpoint_agent": 0,
        },
    },
    "load_profile": {
        "tps": 2000,
        "payload_bytes": 1024,
        "duration_sec": 300,
        "concurrency": 100,
    },
    "topology": {
        "nodes": 1,
        "apps": 1,
    },
    "constraints": {
        "no_public_upload": True,
        "security_policy_notes": [],
        "sudo_allowed": "limited",
        "network_policy": {
            "allow_open_port_80": True,
            "allow_firewall_changes": False,
        },
        "apache_config_mode": "system_prompt_default",
    },
    "targets": [
        {
            "host": "10.0.0.10",
            "user": "ec2-user",
            "auth_ref": "/path/to/key.pem",
            "auth_method": "pem_path",
            "ssh_port": 22,
            "os_type": "Ubuntu22.04",
        }
    ],
    "logging": {
        "base_dir": "/var/log/infra-test-lab",
        "gc_log_dir": "/var/log/infra-test-lab/gc",
        "app_log_dir": "/var/log/infra-test-lab/app",
    },
    "app_tech_stack": {
        "framework": "FastAPI",
        "minor_version": "FastAPI 0.135.1",
        "language": ["Python3.12"],
        "databases": "None",
        "db_user": "",
        "db_pw": "",
    },
    "additional_request": "기본 테스트 실행. 80 포트는 허용 정책을 따른다.",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trigger SampleApp/Infra sub-agents without Supervisor for local testing.",
    )
    parser.add_argument(
        "--agent",
        choices=["sample", "infra", "both"],
        default="both",
        help="Which sub-agent to trigger.",
    )
    parser.add_argument(
        "--request-json",
        type=str,
        default="",
        help="Path to a UserRequest JSON file. If omitted, built-in sample request is used.",
    )
    parser.add_argument(
        "--workspace-root",
        type=str,
        default="",
        help="Optional workspace root for generated files/scripts.",
    )
    parser.add_argument(
        "--infra-execute",
        action="store_true",
        help="Actually execute SSH in infra agent (default is dry-run).",
    )
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=2,
        help="Max repair rounds for SampleApp agent.",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Enable Azure OpenAI calls. Default is disabled for deterministic local tests.",
    )
    return parser.parse_args()


def load_request(request_json_path: str) -> UserRequest:
    if not request_json_path:
        return UserRequest.model_validate(DEFAULT_USER_REQUEST)

    path = Path(request_json_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return UserRequest.model_validate(payload)


def default_prior_for_sample() -> list[AgentExecution]:
    return [
        AgentExecution(
            agent="infra_build",
            success=True,
            executed_commands=["install_component --name apache", "install_component --name tomcat"],
            notes=[
                "HOST: 10.0.0.10",
                "OPEN_PORTS: 22,80,8080",
                "RUNTIME_READY: java17",
            ],
        )
    ]


def default_prior_for_infra(sample_result: AgentExecution | None = None) -> list[AgentExecution]:
    if sample_result:
        return [sample_result]
    return [
        AgentExecution(
            agent="sample_app",
            success=True,
            executed_commands=["build_code --path /tmp/sample --output /tmp/sample.zip"],
            notes=[
                "DEPLOY_CMD: docker run -d -p 8000:8000 sample-app/test:latest",
                "APP_LOG_DIR=/var/log/infra-test-lab/app",
            ],
        )
    ]


def run_sample_agent(
    request: UserRequest,
    workspace_root: str | None,
    max_repairs: int,
    use_llm: bool,
    prior_executions: list[AgentExecution] | None = None,
) -> tuple[dict[str, Any], AgentExecution]:
    settings = load_settings().azure_openai
    settings.enabled = use_llm
    agent = SampleAppAgent(settings=settings, workspace_root=workspace_root, max_repairs=max_repairs)
    result = agent.run(request=request, prior_executions=prior_executions or default_prior_for_sample())
    return result.model_dump(mode="json"), result.execution


def run_infra_agent(
    request: UserRequest,
    workspace_root: str | None,
    dry_run: bool,
    use_llm: bool,
    prior_executions: list[AgentExecution] | None = None,
) -> dict[str, Any]:
    settings = load_settings().azure_openai
    settings.enabled = use_llm
    agent = InfraAutoSettingAgent(settings=settings, workspace_root=workspace_root, dry_run=dry_run)
    result = agent.run(request=request, prior_executions=prior_executions or default_prior_for_infra())
    return result.model_dump(mode="json")


def main() -> None:
    args = parse_args()
    request = load_request(args.request_json)
    workspace_root = args.workspace_root or None

    output: dict[str, Any] = {
        "request": request.model_dump(mode="json"),
        "agent": args.agent,
        "infra_dry_run": not args.infra_execute,
        "use_llm": args.use_llm,
        "results": {},
    }

    if args.agent == "sample":
        sample_payload, _sample_execution = run_sample_agent(
            request=request,
            workspace_root=workspace_root,
            max_repairs=args.max_repairs,
            use_llm=args.use_llm,
        )
        output["results"]["sample_app"] = sample_payload

    elif args.agent == "infra":
        infra_payload = run_infra_agent(
            request=request,
            workspace_root=workspace_root,
            dry_run=not args.infra_execute,
            use_llm=args.use_llm,
        )
        output["results"]["infra_build"] = infra_payload

    else:
        sample_payload, sample_execution = run_sample_agent(
            request=request,
            workspace_root=workspace_root,
            max_repairs=args.max_repairs,
            use_llm=args.use_llm,
        )
        infra_payload = run_infra_agent(
            request=request,
            workspace_root=workspace_root,
            dry_run=not args.infra_execute,
            use_llm=args.use_llm,
            prior_executions=default_prior_for_infra(sample_execution),
        )
        output["results"]["sample_app"] = sample_payload
        output["results"]["infra_build"] = infra_payload

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
