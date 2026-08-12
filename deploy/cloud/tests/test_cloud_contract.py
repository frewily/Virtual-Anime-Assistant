import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "backend/Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE_PATH = ROOT / "deploy/cloud/docker-compose.yml"
GITIGNORE = ROOT / ".gitignore"


class CloudDeploymentContractTests(unittest.TestCase):
    def setUp(self):
        if COMPOSE_PATH.exists():
            self.compose = yaml.safe_load(
                COMPOSE_PATH.read_text(encoding="utf-8")
            )

    def test_backend_image_runs_as_non_root(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn("USER vaa", dockerfile)
        self.assertIn('CMD ["python", "main.py"]', dockerfile)

    def test_build_context_excludes_secrets_and_state(self):
        patterns = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()

        self.assertIn("**/secrets.env", patterns)
        self.assertIn("**/*.db", patterns)
        self.assertIn("qq-bot/data", patterns)

    def test_cloud_compose_has_only_required_services(self):
        self.assertEqual(
            set(self.compose["services"]),
            {"vaa-app", "napcat"},
        )

    def test_management_ports_only_bind_loopback(self):
        services = self.compose["services"]

        self.assertEqual(
            services["vaa-app"]["ports"],
            ["127.0.0.1:8080:8080"],
        )
        self.assertEqual(
            services["napcat"]["ports"],
            ["127.0.0.1:6099:6099"],
        )
        serialized = json.dumps(services)
        self.assertNotIn("3000", serialized)
        self.assertNotIn("3001", serialized)

    def test_services_have_resource_and_log_limits(self):
        services = self.compose["services"]
        expectations = {
            "vaa-app": ("512m", 0.8),
            "napcat": ("768m", 0.8),
        }
        for name, (memory, cpus) in expectations.items():
            with self.subTest(service=name):
                service = services[name]
                self.assertEqual(service["mem_limit"], memory)
                self.assertEqual(service["cpus"], cpus)
                self.assertEqual(service["restart"], "unless-stopped")
                self.assertEqual(service["logging"]["driver"], "json-file")
                self.assertEqual(
                    service["logging"]["options"],
                    {"max-size": "10m", "max-file": "3"},
                )

    def test_services_share_non_internal_private_network(self):
        services = self.compose["services"]

        self.assertEqual(services["vaa-app"]["networks"], ["vaa-internal"])
        self.assertEqual(services["napcat"]["networks"], ["vaa-internal"])
        self.assertNotEqual(
            self.compose["networks"]["vaa-internal"].get("internal"),
            True,
        )

    def test_cloud_runtime_state_and_secrets_are_ignored(self):
        ignored = GITIGNORE.read_text(encoding="utf-8").splitlines()

        self.assertIn("deploy/cloud/.env", ignored)
        self.assertIn("deploy/cloud/secrets.env", ignored)
        self.assertIn("deploy/cloud/data/", ignored)

    def test_secret_example_contains_only_empty_secret_slots(self):
        example = (
            ROOT / "deploy/cloud/secrets.env.example"
        ).read_text(encoding="utf-8")

        self.assertEqual(
            example.splitlines(),
            ["ASSISTANT_LLM_API_KEY=", "ASSISTANT_QQ_ACCESS_TOKEN="],
        )


if __name__ == "__main__":
    unittest.main()
