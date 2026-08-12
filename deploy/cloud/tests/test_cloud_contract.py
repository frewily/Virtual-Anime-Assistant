import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "backend/Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"


class CloudDeploymentContractTests(unittest.TestCase):
    def test_backend_image_runs_as_non_root(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn("USER vaa", dockerfile)
        self.assertIn('CMD ["python", "main.py"]', dockerfile)

    def test_build_context_excludes_secrets_and_state(self):
        patterns = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()

        self.assertIn("**/secrets.env", patterns)
        self.assertIn("**/*.db", patterns)
        self.assertIn("qq-bot/data", patterns)


if __name__ == "__main__":
    unittest.main()
