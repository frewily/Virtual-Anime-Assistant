import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "qq-bot/docker-compose.yml"
ENV_EXAMPLE_PATH = ROOT / "qq-bot/.env.example"
GITIGNORE_PATH = ROOT / ".gitignore"


class OneBotDockerContractTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load(
            COMPOSE_PATH.read_text(encoding="utf-8")
        )
        self.napcat = self.compose["services"]["napcat"]

    def test_compose_uses_configurable_official_napcat_image(self):
        self.assertEqual(
            self.napcat["image"],
            "${NAPCAT_IMAGE:-mlikiowa/napcat-docker:latest}",
        )
        self.assertEqual(self.napcat["restart"], "unless-stopped")

    def test_webui_only_binds_loopback_port_6099(self):
        self.assertEqual(
            self.napcat["ports"],
            ["127.0.0.1:6099:6099"],
        )

    def test_onebot_ports_3000_and_3001_are_not_exposed(self):
        serialized_ports = "\n".join(self.napcat.get("ports", []))

        self.assertNotIn("3000", serialized_ports)
        self.assertNotIn("3001", serialized_ports)

    def test_qq_and_napcat_config_volumes_are_persistent(self):
        self.assertEqual(
            set(self.napcat["volumes"]),
            {
                "./data/qq:/app/.config/QQ",
                "./data/config:/app/napcat/config",
            },
        )

    def test_host_docker_internal_mapping_exists(self):
        self.assertIn(
            "host.docker.internal:host-gateway",
            self.napcat["extra_hosts"],
        )

    def test_runtime_data_directories_are_gitignored(self):
        ignored = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()

        self.assertIn("qq-bot/data/", ignored)
        self.assertIn("qq-bot/.env", ignored)

    def test_example_environment_contains_no_token_or_qq_password(self):
        example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        lowered = example.lower()

        self.assertEqual(
            example.strip(),
            "NAPCAT_IMAGE=mlikiowa/napcat-docker:latest",
        )
        self.assertNotIn("token", lowered)
        self.assertNotIn("password", lowered)
        self.assertNotIn("cookie", lowered)


if __name__ == "__main__":
    unittest.main()
