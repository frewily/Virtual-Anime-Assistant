# 云端 Git Bundle 自动部署实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 GitHub Actions 通过 SSH 上传经过验证的 Git bundle 完成云端部署，消除服务器主动访问 GitHub 的依赖。

**架构：** 新增一个只负责验证和导入 bundle 的 Shell 脚本，并让现有 `deploy.sh` 在部署锁内调用它。`Deploy Cloud` 精确检出成功 CI 对应的提交，生成固定引用的完整 bundle，上传 bundle 与目标提交中的部署脚本，再沿用现有备份、构建、健康检查和回滚链路。

**技术栈：** GitHub Actions、Git bundle、OpenSSH、Bash、Python `unittest`、Docker Compose

---

## 文件结构

- 创建 `deploy/cloud/scripts/import-deployment-bundle.sh`：验证固定 bundle 路径、固定引用与目标 SHA，并将目标对象导入服务器仓库。
- 创建 `deploy/cloud/tests/test_bundle_import.py`：使用临时 Git 仓库覆盖成功导入、非法路径、损坏 bundle、错误引用和 SHA 不匹配。
- 修改 `deploy/cloud/scripts/deploy.sh`：从临时上传的脚本启动，调用 bundle 导入器后沿用部署锁、备份、健康检查和回滚。
- 修改 `.github/workflows/deploy-cloud.yml`：精确 checkout、生成并校验 bundle、严格上传、远程执行和双端清理。
- 修改 `deploy/cloud/tests/test_cloud_contract.py`：锁定工作流与部署脚本的安全契约。
- 修改 `docs/deployment/cloud-qq-assistant.md`：说明自动部署不再依赖服务器访问 GitHub，并增加 bundle 故障排查。

### 任务 1：实现可独立验证的 Bundle 导入器

**文件：**
- 创建：`deploy/cloud/scripts/import-deployment-bundle.sh`
- 创建：`deploy/cloud/tests/test_bundle_import.py`

- [ ] **步骤 1：编写成功导入的失败测试**

创建 `deploy/cloud/tests/test_bundle_import.py`。测试在临时目录创建源仓库和目标仓库，生成固定引用的完整 bundle：

```python
import os
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
        git("config", "user.email", "bundle@example.invalid", cwd=self.source)
        (self.source / "version.txt").write_text("one\n", encoding="utf-8")
        git("add", "version.txt", cwd=self.source)
        git("commit", "-qm", "initial", cwd=self.source)
        self.previous_sha = git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
        (self.source / "version.txt").write_text("two\n", encoding="utf-8")
        git("commit", "-qam", "target", cwd=self.source)
        self.target_sha = git("rev-parse", "HEAD", cwd=self.source).stdout.strip()
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
        self.assertNotEqual(
            git("rev-parse", "--verify", "HEAD", cwd=self.destination, check=False).returncode,
            0,
        )
```

- [ ] **步骤 2：运行测试，确认导入器不存在**

运行：

```bash
python3 -m unittest deploy.cloud.tests.test_bundle_import.BundleImportTests.test_valid_bundle_imports_exact_target_without_checkout -v
```

预期：FAIL，错误包含 `No such file or directory` 或退出码非 0。

- [ ] **步骤 3：实现最小 bundle 导入器**

创建可执行文件 `deploy/cloud/scripts/import-deployment-bundle.sh`：

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

readonly target_sha=${1:-}
readonly bundle_file=${2:-}
readonly repo_root=${3:-}
readonly bundle_ref=refs/heads/vaa-deploy-target

if [[ ! "$target_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid deployment commit" >&2
  exit 2
fi

readonly expected_bundle="/tmp/vaa-deploy-$target_sha.bundle"
if [[ "$bundle_file" != "$expected_bundle" || ! -f "$bundle_file" ]]; then
  echo "invalid deployment bundle path" >&2
  exit 2
fi

git -C "$repo_root" rev-parse --git-dir >/dev/null
git bundle verify "$bundle_file" >/dev/null
bundle_target=$(git bundle list-heads "$bundle_file" "$bundle_ref" \
  | awk 'NR == 1 {print $1}')
readonly bundle_target
if [[ "$bundle_target" != "$target_sha" ]]; then
  echo "deployment bundle target mismatch" >&2
  exit 2
fi

git -C "$repo_root" fetch "$bundle_file" "$bundle_ref"
if [[ "$(git -C "$repo_root" rev-parse FETCH_HEAD)" != "$target_sha" ]]; then
  echo "imported deployment target mismatch" >&2
  exit 2
fi
```

设置权限：

```bash
chmod 0755 deploy/cloud/scripts/import-deployment-bundle.sh
```

- [ ] **步骤 4：补齐失败闭合测试**

在 `BundleImportTests` 增加：

```python
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
```

- [ ] **步骤 5：运行导入器测试与 Shell 语法检查**

运行：

```bash
python3 -m unittest deploy.cloud.tests.test_bundle_import -v
bash -n deploy/cloud/scripts/import-deployment-bundle.sh
```

预期：5 项测试全部 PASS，Shell 语法检查退出码为 0。

- [ ] **步骤 6：提交 bundle 导入器**

```bash
git add -- deploy/cloud/scripts/import-deployment-bundle.sh \
  deploy/cloud/tests/test_bundle_import.py
git commit -m "feat: verify cloud deployment bundles"
```

### 任务 2：让部署编排只消费已上传 Bundle

**文件：**
- 修改：`deploy/cloud/scripts/deploy.sh`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：修改部署脚本契约并确认失败**

将 `test_deploy_script_has_lock_backup_health_and_rollback` 的必需字符串改为：

```python
        for required in (
            "flock",
            "^[0-9a-f]{40}$",
            "import-deployment-bundle.sh",
            'readonly repo_root=/opt/virtual-anime-assistant/current',
            "backup-sqlite.sh",
            "up -d --build",
            "/api/health/live",
            "/api/health/ready",
            "rollback",
            "previous_sha",
        ):
            self.assertIn(required, script)
        self.assertNotIn("git fetch origin", script)
```

同时增加以下禁止项：

```python
        for forbidden in (
            "git reset --hard",
            "docker compose down -v",
            "docker system prune",
            "printenv",
            "env |",
            "secrets.env",
        ):
            self.assertNotIn(forbidden, script)
```

运行：

```bash
python3 -m unittest deploy.cloud.tests.test_cloud_contract.CloudDeploymentContractTests.test_deploy_script_has_lock_backup_health_and_rollback -v
```

预期：FAIL，仍命中 `git fetch origin`，且固定仓库路径与导入器调用不存在。

- [ ] **步骤 2：重构 `deploy.sh` 的路径与参数**

将脚本开头改为固定生产路径，并要求 bundle 参数：

```bash
readonly target_sha=${1:-}
readonly bundle_file=${2:-}
readonly repo_root=/opt/virtual-anime-assistant/current
readonly repo_script_dir="$repo_root/deploy/cloud/scripts"
readonly import_script="/tmp/vaa-import-$target_sha.sh"
readonly lock_file=${VAA_DEPLOY_LOCK:-/opt/virtual-anime-assistant/deploy.lock}
readonly live_url=http://127.0.0.1:8080/api/health/live
readonly ready_url=http://127.0.0.1:8080/api/health/ready

if [[ ! "$target_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: deploy.sh <40-character-commit-sha> <bundle-file>" >&2
  exit 2
fi
if [[ ! -x "$import_script" ]]; then
  echo "deployment bundle importer is missing" >&2
  exit 2
fi
```

所有辅助脚本改为从 `repo_script_dir` 调用，Compose 文件固定为
`$repo_root/deploy/cloud/docker-compose.yml`。删除基于临时脚本位置推导仓库根目录的
逻辑。

- [ ] **步骤 3：在部署锁内先导入，再建立回滚边界**

锁定仓库后使用：

```bash
cd "$repo_root"
previous_sha=$(git rev-parse HEAD)
readonly previous_sha

"$import_script" "$target_sha" "$bundle_file" "$repo_root"

rollback_required=true
```

然后保持原有 rollback 函数、备份、`git checkout --detach "$target_sha"`、
Compose 构建和 90 秒健康检查。bundle 导入失败发生在
`rollback_required=true` 之前，不重建未变化的容器。

- [ ] **步骤 4：运行部署契约、导入测试和语法检查**

```bash
python3 -m unittest \
  deploy.cloud.tests.test_bundle_import \
  deploy.cloud.tests.test_cloud_contract.CloudDeploymentContractTests.test_deploy_script_has_lock_backup_health_and_rollback -v
bash -n deploy/cloud/scripts/deploy.sh
```

预期：全部 PASS，Shell 语法检查退出码为 0。

- [ ] **步骤 5：提交 bundle 部署编排**

```bash
git add -- deploy/cloud/scripts/deploy.sh \
  deploy/cloud/tests/test_cloud_contract.py
git commit -m "feat: deploy cloud commits from verified bundles"
```

### 任务 3：让 GitHub Actions 生成、上传并清理 Bundle

**文件：**
- 修改：`.github/workflows/deploy-cloud.yml`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：增加工作流失败契约**

在 `test_deploy_workflow_has_strict_ci_and_main_gate` 增加：

```python
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
            self.assertIn(required, workflow)
```

在 `test_deploy_workflow_is_serial_and_uses_only_ssh_secrets` 增加：

```python
        self.assertIn('test "$(git rev-parse HEAD)" = "$TARGET_SHA"', workflow)
        self.assertIn("BatchMode=yes", workflow)
        self.assertIn("IdentitiesOnly=yes", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertNotIn("git fetch origin", workflow)
```

运行两项工作流测试，预期因 checkout、bundle 与 scp 缺失而 FAIL。

- [ ] **步骤 2：精确检出并生成完整 bundle**

在工作流增加：

```yaml
      - name: Check out verified main commit
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.workflow_run.head_sha }}
          fetch-depth: 0

      - name: Build verified Git bundle
        env:
          TARGET_SHA: ${{ github.event.workflow_run.head_sha }}
        run: |
          set -Eeuo pipefail
          test "$(git rev-parse HEAD)" = "$TARGET_SHA"
          git update-ref refs/heads/vaa-deploy-target "$TARGET_SHA"
          bundle_file="$RUNNER_TEMP/vaa-deploy-$TARGET_SHA.bundle"
          git bundle create "$bundle_file" refs/heads/vaa-deploy-target
          git bundle verify "$bundle_file"
          chmod 0600 "$bundle_file"
```

- [ ] **步骤 3：上传脚本与 bundle，并保证双端清理**

将部署步骤改为定义复用的 SSH 参数数组和固定远程路径：

```bash
bundle_file="$RUNNER_TEMP/vaa-deploy-$TARGET_SHA.bundle"
remote_bundle="/tmp/vaa-deploy-$TARGET_SHA.bundle"
remote_deploy="/tmp/vaa-deploy-$TARGET_SHA.sh"
remote_import="/tmp/vaa-import-$TARGET_SHA.sh"
ssh_args=(
  -i "$RUNNER_TEMP/vaa-ssh/key"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="$RUNNER_TEMP/vaa-ssh/known_hosts"
  -p "$DEPLOY_PORT"
)
target="$DEPLOY_USER@$DEPLOY_HOST"
scp_args=(
  -i "$RUNNER_TEMP/vaa-ssh/key"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="$RUNNER_TEMP/vaa-ssh/known_hosts"
  -P "$DEPLOY_PORT"
)

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  ssh "${ssh_args[@]}" "$target" \
    "rm -f '$remote_bundle' '$remote_deploy' '$remote_import'"
  rm -f "$bundle_file"
  rm -rf "$RUNNER_TEMP/vaa-ssh"
  exit "$status"
}
trap cleanup EXIT
```

使用以下命令将 3 个文件分别上传到固定路径并启动部署：

```bash
scp "${scp_args[@]}" "$bundle_file" "$target:$remote_bundle"
scp "${scp_args[@]}" deploy/cloud/scripts/deploy.sh \
  "$target:$remote_deploy"
scp "${scp_args[@]}" \
  deploy/cloud/scripts/import-deployment-bundle.sh \
  "$target:$remote_import"
ssh "${ssh_args[@]}" "$target" \
  "chmod 0600 '$remote_bundle' && chmod 0700 '$remote_deploy' '$remote_import' && '$remote_deploy' '$TARGET_SHA' '$remote_bundle'"
```

`scp` 的端口参数使用大写 `-P "$DEPLOY_PORT"`。不得使用 `ssh-keyscan` 或关闭
主机密钥校验。

- [ ] **步骤 4：运行工作流契约与 YAML 解析**

```bash
python3 -m unittest deploy.cloud.tests.test_cloud_contract -v
python3 - <<'PY'
from pathlib import Path
import yaml
yaml.safe_load(Path('.github/workflows/deploy-cloud.yml').read_text())
PY
```

预期：云端契约全部 PASS，YAML 解析退出码为 0。

- [ ] **步骤 5：提交 Actions bundle 传输**

```bash
git add -- .github/workflows/deploy-cloud.yml \
  deploy/cloud/tests/test_cloud_contract.py
git commit -m "ci: upload verified bundles for cloud deploys"
```

### 任务 4：补充运维说明并完成验证

**文件：**
- 修改：`docs/deployment/cloud-qq-assistant.md`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：先增加文档契约**

在 `test_cloud_runbook_covers_backup_recovery_and_acceptance` 的必需词组中增加：

```python
            "Git bundle",
            "服务器无需访问 GitHub",
            "vaa-deploy-<提交 SHA>.bundle",
```

运行该测试，预期因运维文档缺少新流程说明而 FAIL。

- [ ] **步骤 2：更新自动部署与故障排查说明**

在云端部署文档说明：

- main CI 成功后，Actions 自动生成并上传完整 Git bundle。
- 服务器只从 `/tmp/vaa-deploy-<提交 SHA>.bundle` 导入目标提交，无需访问 GitHub。
- 临时 bundle 和脚本在成功或失败后自动删除。
- `invalid deployment bundle path`、`deployment bundle target mismatch` 和
  `git bundle verify` 失败均发生在数据库备份与容器切换之前。
- 自动部署仍保留备份、部署锁、健康检查与回滚。

- [ ] **步骤 3：运行全部云端测试与安全检查**

```bash
python3 -m unittest discover -s deploy/cloud/tests -p 'test_*.py' -v
for script in deploy/cloud/scripts/*.sh; do bash -n "$script"; done
python3 - <<'PY'
from pathlib import Path
import yaml
yaml.safe_load(Path('.github/workflows/deploy-cloud.yml').read_text())
PY
rg -n "git fetch origin|StrictHostKeyChecking=no|ssh-keyscan|printenv|env \|" \
  .github/workflows/deploy-cloud.yml \
  deploy/cloud/scripts/deploy.sh \
  deploy/cloud/scripts/import-deployment-bundle.sh
git diff --check
```

预期：所有测试与语法检查退出码为 0；`rg` 无命中；差异检查无输出。

- [ ] **步骤 4：运行后端与桌面完整回归**

```bash
python3 -m unittest discover -s backend/tests -p 'test_*.py'
npm test --prefix desktop-app
```

预期：两套完整回归均无失败或错误。

- [ ] **步骤 5：提交文档与最终契约**

```bash
git add -- docs/deployment/cloud-qq-assistant.md \
  deploy/cloud/tests/test_cloud_contract.py
git commit -m "docs: describe bundle-based cloud deployment"
```

- [ ] **步骤 6：检查提交序列与工作区**

```bash
git status --short --branch
git log --oneline origin/main..HEAD
```

预期：工作区干净，设计、计划和 4 个实现提交均位于
`codex/cloud-bundle-deployment` 分支。

## 合并后的云端验收

1. 确认 main CI 成功后触发 `Deploy Cloud`，且工作流不出现服务器端
   `git fetch origin`。
2. 确认服务器 HEAD 等于合并提交。
3. 运行 `deploy/cloud/scripts/verify-deployment.sh full`。
4. 检查 `/api/status/cloud` 为 `healthy`、OneBot 为 `connected`、备份为
   `fresh`。
5. 检查 `vaa-cloud-monitor.timer` 与 `vaa-backup.timer` 均为 enabled、active。
6. 检查 8080 与 6099 只绑定 `127.0.0.1`，3000 与 3001 未监听。
7. 确认 QQ 不要求重新扫码，网站和博客配置未被修改。
8. 确认服务器 `/tmp` 不残留本次 bundle 与临时部署脚本。
