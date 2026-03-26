import unittest
from unittest.mock import patch

from InfraAutoSetting.agent import InfraAutoSettingAgent
from Supervisor.models import (
    AppTechStack,
    InfraTechStack,
    LoggingConfig,
    RequestConstraints,
    TargetHost,
    UserRequest,
)


class FakeCache:
    def __init__(self, cached_script: str | None) -> None:
        self.cached_script = cached_script
        self.saved_script: str | None = None

    def get(self, key: str) -> str | None:
        return self.cached_script

    def put(self, key: str, script: str, meta: dict) -> None:
        self.saved_script = script


class InfraAutoSettingAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = InfraAutoSettingAgent(workspace_root="/tmp/poc-automation-lab-tests")
        self.request = UserRequest(
            infra_tech_stack=InfraTechStack(
                os="Linux",
                components=["apache", "java"],
                versions={"apache": "2.4.66", "java": "21"},
            ),
            constraints=RequestConstraints(sudo_allowed="limited"),
            logging=LoggingConfig(),
            app_tech_stack=AppTechStack(framework="Spring Boot", language=["Java21"]),
            targets=[
                TargetHost(
                    host="ec2-3-34-172-57.ap-northeast-2.compute.amazonaws.com",
                    user="ec2-user",
                    auth_ref="/tmp/test.pem",
                    auth_method="pem_path",
                    ssh_port=22,
                    os_type="Amazon Linux2023",
                )
            ],
        )

    def test_validate_script_rejects_protected_write_without_sudo(self) -> None:
        script = """#!/usr/bin/env bash
set -euo pipefail
sudo dnf -y install httpd
mkdir -p /opt/sample-springboot-app/src/main/java/lab
"""

        issues = self.agent._validate_script_content(
            script=script,
            request=self.request,
            versions=self.request.infra_tech_stack.versions,
            package_manager="dnf",
        )

        self.assertTrue(any("protected path without sudo" in issue for issue in issues))

    @patch("InfraAutoSetting.agent.InfraScriptGeneratorLLM.generate_install_script", return_value=None)
    def test_invalid_cached_script_is_not_reused(self, _mock_generate) -> None:
        cached_script = """#!/usr/bin/env bash
set -euo pipefail
sudo dnf -y install httpd
mkdir -p /opt/sample-springboot-app/src/main/java/lab
"""
        fake_cache = FakeCache(cached_script)
        self.agent._cache = fake_cache

        resolved_versions, _notes = self.agent._resolve_versions(self.request.infra_tech_stack.versions)
        script, cache_note = self.agent._resolve_script(self.request, resolved_versions, [])

        self.assertTrue(cache_note.startswith("SCRIPT_CACHE_MISS"))
        self.assertNotEqual(cached_script, script)
        self.assertEqual(script, fake_cache.saved_script)


if __name__ == "__main__":
    unittest.main()
