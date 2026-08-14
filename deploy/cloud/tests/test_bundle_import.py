import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
IMPORTER = ROOT / "deploy/cloud/scripts/import-deployment-bundle.sh"


def git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class BundleImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.destination.mkdir()
        git("init", "-q", cwd=self.source)
        git("config", "user.name", "Bundle Test", cwd=self.source)
        git(
            "config",
            "user.email",
            "bundle@example.invalid",
            cwd=self.source,
        )
        (self.source / "version.txt").write_text("one\n", encoding="utf-8")
        git("add", "version.txt", cwd=self.source)
        git("commit", "-qm", "initial", cwd=self.source)
        self.previous_sha = git(
            "rev-parse", "HEAD", cwd=self.source
        ).stdout.strip()
        (self.source / "version.txt").write_text("two\n", encoding="utf-8")
        git("commit", "-qam", "target", cwd=self.source)
        self.target_sha = git(
            "rev-parse", "HEAD", cwd=self.source
        ).stdout.strip()
        git(
            "update-ref",
            "refs/heads/vaa-deploy-target",
            self.target_sha,
            cwd=self.source,
        )
        self.bundle = Path(f"/tmp/vaa-deploy-{self.target_sha}.bundle")
        git(
            "bundle",
            "create",
            str(self.bundle),
            "refs/heads/vaa-deploy-target",
            cwd=self.source,
        )
        git("init", "-q", cwd=self.destination)

    def tearDown(self):
        self.bundle.unlink(missing_ok=True)
        self.temporary.cleanup()

    def run_importer(self, target=None, bundle=None):
        return subprocess.run(
            [
                "bash",
                str(IMPORTER),
                target or self.target_sha,
                str(bundle or self.bundle),
                str(self.destination),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_valid_bundle_imports_exact_target_without_checkout(self):
        result = self.run_importer()

        self.assertEqual(result.returncode, 0, result.stderr)
        fetched = git(
            "rev-parse", "FETCH_HEAD", cwd=self.destination
        ).stdout.strip()
        self.assertEqual(fetched, self.target_sha)
        head = git(
            "rev-parse",
            "--verify",
            "HEAD",
            cwd=self.destination,
            check=False,
        )
        self.assertNotEqual(head.returncode, 0)

    def test_invalid_path_is_rejected_before_import(self):
        invalid = self.root / self.bundle.name
        invalid.write_bytes(self.bundle.read_bytes())

        result = self.run_importer(bundle=invalid)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid deployment bundle path", result.stderr)

    def test_corrupt_bundle_is_rejected_before_import(self):
        self.bundle.write_bytes(b"not-a-git-bundle")

        result = self.run_importer()

        self.assertNotEqual(result.returncode, 0)

    def test_wrong_ref_is_rejected_before_import(self):
        self.bundle.unlink()
        git(
            "update-ref",
            "refs/heads/wrong-target",
            self.target_sha,
            cwd=self.source,
        )
        git(
            "bundle",
            "create",
            str(self.bundle),
            "refs/heads/wrong-target",
            cwd=self.source,
        )

        result = self.run_importer()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deployment bundle target mismatch", result.stderr)

    def test_mismatched_sha_is_rejected_before_import(self):
        mismatched = Path(f"/tmp/vaa-deploy-{self.previous_sha}.bundle")
        try:
            mismatched.write_bytes(self.bundle.read_bytes())
            result = self.run_importer(
                target=self.previous_sha,
                bundle=mismatched,
            )
        finally:
            mismatched.unlink(missing_ok=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deployment bundle target mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
