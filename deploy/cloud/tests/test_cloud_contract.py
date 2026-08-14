import json
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "backend/Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE_PATH = ROOT / "deploy/cloud/docker-compose.yml"
GITIGNORE = ROOT / ".gitignore"
DEPLOY_WORKFLOW = ROOT / ".github/workflows/deploy-cloud.yml"
CLOUD_RUNBOOK = ROOT / "docs/deployment/cloud-qq-assistant.md"
MONITOR_INSTALLER = (
    ROOT / "deploy/cloud/scripts/install-cloud-monitor.sh"
)


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

    def test_backend_image_provides_writable_audio_directory(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn(
            "install -d --owner=vaa --group=vaa --mode=0750 /app/backend/audio",
            dockerfile,
        )

    def test_cloud_build_can_use_a_non_secret_pypi_mirror(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        environment_example = (
            ROOT / "deploy/cloud/.env.example"
        ).read_text(encoding="utf-8")
        build_args = self.compose["services"]["vaa-app"]["build"]["args"]

        self.assertIn("ARG PIP_INDEX_URL", dockerfile)
        self.assertIn("${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL}", dockerfile)
        self.assertEqual(build_args["PIP_INDEX_URL"], "${PIP_INDEX_URL:-}")
        self.assertIn(
            "PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/",
            environment_example.splitlines(),
        )

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

    def test_backup_timer_is_daily_and_persistent(self):
        service = (
            ROOT / "deploy/cloud/systemd/vaa-backup.service"
        ).read_text(encoding="utf-8")
        timer = (
            ROOT / "deploy/cloud/systemd/vaa-backup.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("Type=oneshot", service)
        self.assertIn("User=vaa-deploy", service)
        self.assertIn("OnCalendar=daily", timer)
        self.assertIn("Persistent=true", timer)

    def test_backup_script_uses_in_container_backup_api(self):
        script = (
            ROOT / "deploy/cloud/scripts/backup-sqlite.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("docker compose exec -T vaa-app", script)
        self.assertIn("python -m infrastructure.sqlite_backup", script)
        self.assertNotIn("cp /data/sqlite", script)

    def test_cloud_monitor_timer_is_bounded_persistent_and_non_privileged(self):
        service = (
            ROOT / "deploy/cloud/systemd/vaa-cloud-monitor.service"
        ).read_text(encoding="utf-8")
        timer = (
            ROOT / "deploy/cloud/systemd/vaa-cloud-monitor.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("Type=oneshot", service)
        self.assertIn("User=vaa-deploy", service)
        self.assertIn("Group=vaa-deploy", service)
        self.assertIn("UMask=0027", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn(
            "ExecStart=/opt/virtual-anime-assistant/current/deploy/cloud/"
            "scripts/cloud-monitor.sh",
            service,
        )
        self.assertIn("OnBootSec=2min", timer)
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=10s", timer)

    def test_cloud_monitor_installer_grants_only_required_data_access(self):
        script = MONITOR_INSTALLER.read_text(encoding="utf-8")

        for required in (
            "(( EUID == 0 ))",
            "setfacl -m u:vaa-deploy:--x \"$vaa_data_dir\"",
            "setfacl -m u:vaa-deploy:r-x \"$backup_dir\"",
            "install -d -o vaa-deploy -g vaa-deploy -m 0750",
            "setfacl -m u:10001:r-x,d:u:10001:r-x",
            "systemctl enable --now vaa-cloud-monitor.timer",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)

        for forbidden in (
            "chmod 777",
            "chmod -R",
            "chown -R",
            "setfacl -R",
            "docker.sock",
            "secrets.env",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)

    def test_application_never_receives_docker_socket(self):
        serialized = json.dumps(self.compose)

        self.assertNotIn("docker.sock", serialized)
        self.assertNotIn("/var/run/docker", serialized)

    def test_deploy_script_has_lock_backup_health_and_rollback(self):
        script = (
            ROOT / "deploy/cloud/scripts/deploy.sh"
        ).read_text(encoding="utf-8")

        for required in (
            "flock",
            "^[0-9a-f]{40}$",
            'vaa-import-$target_sha.sh',
            "readonly repo_root=/opt/virtual-anime-assistant/current",
            "backup-sqlite.sh",
            "up -d --build",
            "/api/health/live",
            "/api/health/ready",
            "rollback",
            "previous_sha",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)
        for forbidden in (
            "git reset --hard",
            "docker compose down -v",
            "docker system prune",
            "printenv",
            "env |",
            "secrets.env",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)
        self.assertNotIn("git fetch origin", script)

    def test_verify_script_has_startup_and_full_modes(self):
        script = (
            ROOT / "deploy/cloud/scripts/verify-deployment.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("startup", script)
        self.assertIn("full", script)
        self.assertIn("/api/health/live", script)
        self.assertIn("/api/health/ready", script)
        self.assertIn("/api/health/onebot", script)
        self.assertIn('"status":"connected"', script)
        self.assertNotIn("cat ", script)

    def test_deploy_workflow_has_strict_ci_and_main_gate(self):
        workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_run:", workflow)
        self.assertIn('workflows: ["CI"]', workflow)
        self.assertIn("branches: [main]", workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("workflow_run.conclusion == 'success'", workflow)
        self.assertIn("workflow_run.head_branch == 'main'", workflow)
        self.assertIn("contents: read", workflow)
        for required in (
            "actions/checkout@v4",
            "ref: ${{ github.event.workflow_run.head_sha }}",
            "fetch-depth: 0",
            "refs/heads/vaa-deploy-target",
            "git bundle create",
            "git bundle verify",
            "scp",
            "deploy/cloud/scripts/import-deployment-bundle.sh",
            "trap cleanup EXIT",
        ):
            with self.subTest(required=required):
                self.assertIn(required, workflow)

    def test_deploy_workflow_is_serial_and_uses_only_ssh_secrets(self):
        workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("group: vaa-cloud-production", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        for name in (
            "VAA_DEPLOY_HOST",
            "VAA_DEPLOY_USER",
            "VAA_DEPLOY_PORT",
            "VAA_DEPLOY_SSH_KEY",
            "VAA_DEPLOY_KNOWN_HOSTS",
        ):
            self.assertIn(f"secrets.{name}", workflow)
        self.assertIn("github.event.workflow_run.head_sha", workflow)
        self.assertIn(
            'test "$(git rev-parse HEAD)" = "$TARGET_SHA"', workflow
        )
        self.assertIn("BatchMode=yes", workflow)
        self.assertIn("IdentitiesOnly=yes", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertNotIn("git fetch origin", workflow)
        self.assertNotIn("ssh-keyscan", workflow)
        self.assertNotIn("password", workflow.lower())

    def test_cloud_runbook_covers_setup_tunnels_and_onebot(self):
        runbook = CLOUD_RUNBOOK.read_text(encoding="utf-8")

        for required in (
            "Alibaba Cloud Linux 3",
            "vaa-deploy",
            "/opt/virtual-anime-assistant",
            "chmod 600 secrets.env",
            "8080:127.0.0.1:8080",
            "6099:127.0.0.1:6099",
            "ws://vaa-app:8080/ws/qq",
            "不修改宝塔 Nginx",
        ):
            with self.subTest(required=required):
                self.assertIn(required, runbook)

    def test_cloud_runbook_covers_backup_recovery_and_acceptance(self):
        runbook = CLOUD_RUNBOOK.read_text(encoding="utf-8")

        for required in (
            "backup-sqlite.sh",
            "verify-deployment.sh full",
            "ss -lnt",
            "vaa-backup.timer",
            "网站",
            "博客",
            "回滚",
            "停止 `vaa-app`",
            "vaa-cloud-monitor.timer",
            "install-cloud-monitor.sh",
            "setfacl",
            "systemctl status vaa-cloud-monitor.timer",
            "systemctl start vaa-cloud-monitor.service",
            "recovery_exhausted",
            "/api/status/cloud",
        ):
            with self.subTest(required=required):
                self.assertIn(required, runbook)


if __name__ == "__main__":
    unittest.main()
