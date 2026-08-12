# 云端 QQ 助手部署实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将 VAA 后端与 NapCat 以资源受限、仅回环管理、可自动部署和回滚的方式运行在 Alibaba Cloud Linux 3，使 QQ 助手脱离本地电脑全天在线。

**架构：** 新增云端运行配置、健康接口和后端镜像，在 `deploy/cloud` 中用 Docker Compose 连接 `vaa-app` 与 NapCat。数据与受限环境文件留在服务器；GitHub Actions 只触发串行部署脚本，脚本以健康检查为提交点并在失败时恢复旧提交。

**技术栈：** Python 3.12、FastAPI、SQLite、Docker、Docker Compose v2、NapCat、GitHub Actions、Bash、Python `unittest`

---

## 文件结构

### 创建

- `backend/core/deployment.py`：严格解析 `desktop` 和 `cloud` 运行模式。
- `backend/api/health.py`：提供脱敏的存活、就绪和 OneBot 健康接口。
- `backend/infrastructure/sqlite_backup.py`：使用 SQLite Backup API 创建一致性备份并轮转。
- `backend/Dockerfile`、`.dockerignore`：构建非 root 后端镜像并排除秘密与状态。
- `deploy/cloud/docker-compose.yml`：生产编排、回环端口、资源上限、卷和日志限制。
- `deploy/cloud/.env.example`、`deploy/cloud/secrets.env.example`：非秘密配置和空秘密模板。
- `deploy/cloud/scripts/{healthcheck,verify-deployment,backup-sqlite,deploy}.sh`：健康、备份、部署与回滚。
- `deploy/cloud/systemd/vaa-backup.{service,timer}`：每日备份任务。
- `deploy/cloud/tests/test_cloud_contract.py`：静态验证生产安全边界。
- `.github/workflows/deploy-cloud.yml`：`main` CI 成功后的串行云端部署。
- `docs/deployment/cloud-qq-assistant.md`：准备、隧道、上线与恢复手册。
- 对应 Python 测试：`backend/tests/test_deployment_config.py`、`test_health_api.py`、`test_sqlite_backup.py`。

### 修改

- `backend/api/app.py`：注册健康路由，云端不启动桌面窗口监控。
- `backend/settings/paths.py`：支持 `ASSISTANT_CONFIG_DIR`。
- `backend/tests/test_runtime.py`、`test_settings_file_store.py`：覆盖新配置和生命周期。
- `.github/workflows/ci.yml`：验证镜像、Compose、脚本和云端契约。
- `.gitignore`：忽略真实环境文件和云端数据。
- `qq-bot/README.md`：明确现有 Compose 仅用于本地开发，并链接云端手册。

## 固定接口与约定

- `ASSISTANT_RUNTIME_PROFILE` 只能为 `desktop`（默认）或 `cloud`。
- 云端 `ASSISTANT_CONFIG_DIR=/data/config`，`ASSISTANT_DATA_DIR=/data/sqlite`。
- `GET /api/health/live` 返回 `200 {"status":"ok"}`。
- `GET /api/health/ready` 证明配置可加载且 SQLite 可读写；不要求 QQ 已登录。
- `GET /api/health/onebot` 只返回 `disabled`、`misconfigured`、`disconnected` 或 `connected`。
- 自动部署门禁使用 `live` 和 `ready`；上线验收额外要求 `onebot=connected`。
- `/opt/virtual-anime-assistant/current` 是 Git 工作副本；持久数据在 `/opt/virtual-anime-assistant/data`。
- `secrets.env` 权限为 `0600`，仅保存模型 API Key 与 OneBot Token。秘密由环境文件提供，设置页显示只读；其他配置仍可保存。

### 任务 1：云端运行配置与持久化路径

**文件：**
- 创建：`backend/core/deployment.py`
- 修改：`backend/settings/paths.py`
- 测试：`backend/tests/test_deployment_config.py`
- 测试：`backend/tests/test_settings_file_store.py`

- [ ] **步骤 1：编写失败测试**

```python
class DeploymentSettingsTests(unittest.TestCase):
    def test_cloud_profile_disables_desktop_monitor(self):
        settings = DeploymentSettings.from_env(
            {"ASSISTANT_RUNTIME_PROFILE": "cloud"}
        )
        self.assertEqual(settings.profile, "cloud")
        self.assertFalse(settings.desktop_monitor_enabled)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid runtime profile"):
            DeploymentSettings.from_env(
                {"ASSISTANT_RUNTIME_PROFILE": "production-ish"}
            )
```

另测试 `ASSISTANT_CONFIG_DIR` 指向临时目录时，`SettingsPaths.default().settings_file` 为该目录下的 `settings.json`。

- [ ] **步骤 2：运行并确认失败**

```bash
python -m unittest backend.tests.test_deployment_config   backend.tests.test_settings_file_store -v
```

预期：FAIL，`core.deployment` 不存在且默认路径不读环境变量。

- [ ] **步骤 3：实现最小解析**

```python
@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    profile: Literal["desktop", "cloud"]
    desktop_monitor_enabled: bool

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        profile = values.get(
            "ASSISTANT_RUNTIME_PROFILE", "desktop"
        ).strip()
        if profile not in {"desktop", "cloud"}:
            raise ValueError("invalid runtime profile")
        return cls(profile=profile, desktop_monitor_enabled=profile == "desktop")
```

`SettingsPaths.default()` 优先使用清理后的 `ASSISTANT_CONFIG_DIR`，空值继续使用现有 `platformdirs` 路径。

- [ ] **步骤 4：运行同一组测试，预期全部 PASS**
- [ ] **步骤 5：提交**

```bash
git add backend/core/deployment.py backend/settings/paths.py   backend/tests/test_deployment_config.py   backend/tests/test_settings_file_store.py
git commit -m "feat: add cloud runtime profile"
```

### 任务 2：分层健康检查与云端生命周期

**文件：**
- 创建：`backend/api/health.py`
- 修改：`backend/api/app.py`
- 测试：`backend/tests/test_health_api.py`
- 测试：`backend/tests/test_runtime.py`

- [ ] **步骤 1：编写失败测试**

```python
def test_live_health_is_minimal(self):
    response = self.client.get("/api/health/live")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json(), {"status": "ok"})

def test_onebot_health_is_redacted(self):
    payload = self.client.get("/api/health/onebot").json()
    self.assertEqual(payload, {"status": "connected"})
    self.assertNotIn("accessToken", json.dumps(payload))
```

另以 `DeploymentSettings("cloud", False)` 创建应用生命周期，mock `run_window_monitor`，断言云端不调用它；桌面模式保留现有行为。

- [ ] **步骤 2：运行并确认 404 与无条件监控导致失败**

```bash
python -m unittest backend.tests.test_health_api backend.tests.test_runtime -v
```

- [ ] **步骤 3：实现路由**

```python
@router.get("/health/live")
def live():
    return {"status": "ok"}

@router.get("/health/ready")
def ready(runtime: AssistantRuntime = Depends(get_runtime)):
    runtime.store.list_conversations(limit=1, offset=0)
    return {"status": "ready"}

@router.get("/health/onebot")
def onebot(runtime: AssistantRuntime = Depends(get_runtime)):
    return {"status": runtime.qq_channel.status()}
```

使用现有 Store/Channel 的真实只读签名；存储异常返回固定的 HTTP 503 `{"status":"not_ready"}`，响应不得含 Token、QQ 号或模型配置。

- [ ] **步骤 4：为 `create_app()` 注入 `DeploymentSettings`，仅在 `desktop_monitor_enabled` 为真时启动窗口监控**
- [ ] **步骤 5：运行定向测试，预期全部 PASS**
- [ ] **步骤 6：提交**

```bash
git add backend/api/health.py backend/api/app.py   backend/tests/test_health_api.py backend/tests/test_runtime.py
git commit -m "feat: add cloud health checks"
```

### 任务 3：非 root 后端镜像

**文件：**
- 创建：`backend/Dockerfile`
- 创建：`.dockerignore`
- 创建：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：编写失败契约测试**

```python
def test_backend_image_runs_as_non_root(self):
    dockerfile = (ROOT / "backend/Dockerfile").read_text()
    self.assertIn("USER vaa", dockerfile)
    self.assertIn('CMD ["python", "main.py"]', dockerfile)

def test_build_context_excludes_secrets_and_state(self):
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    self.assertIn("**/secrets.env", patterns)
    self.assertIn("**/*.db", patterns)
```

- [ ] **步骤 2：运行 `python -m unittest deploy.cloud.tests.test_cloud_contract -v`，预期 FAIL**
- [ ] **步骤 3：实现镜像**

使用 `python:3.12-slim`，安装 `backend/requirements.txt`，复制 `backend` 与 `config`，创建固定 UID/GID 的 `vaa` 用户，工作目录为 `/app/backend`，最后执行 `USER vaa` 和 `CMD ["python", "main.py"]`。镜像层不得包含环境文件。

- [ ] **步骤 4：构建验证**

```bash
docker build -f backend/Dockerfile -t vaa-backend:test .
docker run --rm --entrypoint id vaa-backend:test
```

预期：构建成功，UID 不为 0。

- [ ] **步骤 5：运行契约测试并提交**

```bash
git add backend/Dockerfile .dockerignore deploy/cloud/tests/test_cloud_contract.py
git commit -m "build: add backend container image"
```

### 任务 4：生产 Compose 编排

**文件：**
- 创建：`deploy/cloud/docker-compose.yml`
- 创建：`deploy/cloud/.env.example`
- 创建：`deploy/cloud/secrets.env.example`
- 创建：`deploy/cloud/scripts/healthcheck.sh`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`
- 修改：`.gitignore`

- [ ] **步骤 1：扩展失败契约测试**

断言两个服务名、`127.0.0.1:8080:8080`、`127.0.0.1:6099:6099`、512/768 MiB、各 0.8 CPU、专用网络、持久化目录和日志 `max-size/max-file`；断言不发布 3000/3001，真实 `.env`、`secrets.env` 和 `data/` 被忽略。

- [ ] **步骤 2：运行契约测试，预期 FAIL**
- [ ] **步骤 3：实现 Compose**

```yaml
services:
  vaa-app:
    build:
      context: ../..
      dockerfile: backend/Dockerfile
    restart: unless-stopped
    env_file: [.env, secrets.env]
    environment:
      ASSISTANT_RUNTIME_PROFILE: cloud
      ASSISTANT_HOST: 0.0.0.0
      ASSISTANT_CONFIG_DIR: /data/config
      ASSISTANT_DATA_DIR: /data/sqlite
    ports: ["127.0.0.1:8080:8080"]
    volumes: ["./data/vaa:/data"]
    networks: [vaa-internal]
    mem_limit: 512m
    cpus: 0.8
  napcat:
    image: ${NAPCAT_IMAGE:-mlikiowa/napcat-docker:latest}
    restart: unless-stopped
    ports: ["127.0.0.1:6099:6099"]
    volumes:
      - ./data/napcat/qq:/app/.config/QQ
      - ./data/napcat/config:/app/napcat/config
    networks: [vaa-internal]
    mem_limit: 768m
    cpus: 0.8
networks:
  vaa-internal: {}
```

网络不能设置 `internal: true`，否则 NapCat 和模型 API 无法访问外网；安全边界由不发布内部 OneBot 端口实现。

- [ ] **步骤 4：增加健康脚本与模板**

`healthcheck.sh` 使用 Python 标准库访问 `127.0.0.1:8080/api/health/live`。`secrets.env.example` 只能有空的 `ASSISTANT_LLM_API_KEY=` 和 `ASSISTANT_QQ_ACCESS_TOKEN=`。

- [ ] **步骤 5：验证展开**

```bash
cp deploy/cloud/.env.example deploy/cloud/.env
cp deploy/cloud/secrets.env.example deploy/cloud/secrets.env
docker compose -f deploy/cloud/docker-compose.yml config --quiet
rm deploy/cloud/.env deploy/cloud/secrets.env
python -m unittest deploy.cloud.tests.test_cloud_contract -v
```

预期：全部 exit 0；不得把展开配置输出到 CI 日志。

- [ ] **步骤 6：提交 `feat: add cloud compose stack`**

### 任务 5：一致性 SQLite 备份

**文件：**
- 创建：`backend/infrastructure/sqlite_backup.py`
- 创建：`backend/tests/test_sqlite_backup.py`
- 创建：`deploy/cloud/scripts/backup-sqlite.sh`
- 创建：`deploy/cloud/systemd/vaa-backup.service`
- 创建：`deploy/cloud/systemd/vaa-backup.timer`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：编写失败测试**

创建含 3 行数据的临时 SQLite，连续生成 9 次备份，断言仅保留 7 份；打开最后一份执行 `PRAGMA integrity_check` 得到 `ok`，行数仍为 3。

- [ ] **步骤 2：运行 `python -m unittest backend.tests.test_sqlite_backup -v`，预期 FAIL**
- [ ] **步骤 3：实现 `create_backup(source, backup_dir, now, keep=7)`**

使用 `sqlite3.Connection.backup()` 写临时文件，执行完整性检查，原子重命名为 `assistant-<UTC>.db`，最后删除超出 `keep` 的最旧文件。CLI 默认源为 `DatabaseSettings.from_env().database_path`，输出为 `/data/backups`。

- [ ] **步骤 4：实现宿主机脚本与 Timer**

```bash
docker compose exec -T vaa-app   python -m infrastructure.sqlite_backup --output /data/backups --keep 7
```

Service 为 `Type=oneshot`；Timer 为 `OnCalendar=daily`、`Persistent=true`。

- [ ] **步骤 5：运行备份和云端契约测试，预期全部 PASS**
- [ ] **步骤 6：提交 `feat: add sqlite backup rotation`**

### 任务 6：串行部署、验证与回滚

**文件：**
- 创建：`deploy/cloud/scripts/deploy.sh`
- 创建：`deploy/cloud/scripts/verify-deployment.sh`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：编写失败契约测试**

断言脚本包含 `flock`、40 位 SHA 校验、`git fetch origin`、部署前备份、Compose 构建启动、live/ready、失败 trap 和旧提交恢复；断言不含 `git reset --hard`、`down -v`、`docker system prune` 或环境变量打印。

- [ ] **步骤 2：运行契约测试，预期 FAIL**
- [ ] **步骤 3：实现部署脚本**

启用 `set -Eeuo pipefail`，仅接收 `^[0-9a-f]{40}$`，用 `flock` 锁住部署。记录 `previous_sha`，备份后 fetch，并 `git checkout --detach "$target_sha"`。启动后最多等待 90 秒检查 live/ready。失败 trap 切回旧 SHA、重新构建启动并再次检查 live；不得删除持久化目录。

- [ ] **步骤 4：实现验收脚本**

`verify-deployment.sh startup` 检查 live/ready；`full` 额外严格检查 OneBot 为 connected。仅访问 `127.0.0.1:8080`，错误只输出阶段与 HTTP 状态，不输出响应体。

- [ ] **步骤 5：验证**

```bash
bash -n deploy/cloud/scripts/deploy.sh
bash -n deploy/cloud/scripts/verify-deployment.sh
python -m unittest deploy.cloud.tests.test_cloud_contract -v
```

预期：全部 exit 0。

- [ ] **步骤 6：提交 `feat: add atomic cloud deployment`**

### 任务 7：GitHub Actions 自动部署

**文件：**
- 创建：`.github/workflows/deploy-cloud.yml`
- 修改：`.github/workflows/ci.yml`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：编写失败工作流契约测试**

断言仅在 `CI` 对 `main` 成功后触发；`contents: read`；concurrency 为 `vaa-cloud-production` 且不取消运行中部署；只传 head SHA；使用 Host、User、Port、SSH Key、known_hosts 5 个 Secrets；不使用密码或 `ssh-keyscan`。

- [ ] **步骤 2：运行契约测试，预期 FAIL**
- [ ] **步骤 3：实现工作流**

将私钥与已审核 known_hosts 写入临时 `0600` 文件，SSH 执行：

```bash
/opt/virtual-anime-assistant/current/deploy/cloud/scripts/deploy.sh   '<workflow_run.head_sha>'
```

用 trap 删除临时文件。GitHub Secrets 会创建持久访问，实际配置前必须再次征得用户确认。

- [ ] **步骤 4：扩展 CI**

保留全量 Python/Node 测试，增加 Docker build、云端契约测试、Compose `config --quiet` 和全部 Bash `bash -n`。

- [ ] **步骤 5：解析所有工作流 YAML 并运行契约测试，预期全部 PASS**
- [ ] **步骤 6：提交 `ci: deploy cloud stack after main passes`**

### 任务 8：安全运维手册

**文件：**
- 创建：`docs/deployment/cloud-qq-assistant.md`
- 修改：`qq-bot/README.md`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：编写失败文档契约测试**

断言包含 Alibaba Cloud Linux 3、`vaa-deploy`、部署目录、`chmod 600 secrets.env`、8080/6099 隧道、`ws://vaa-app:8080/ws/qq`、备份恢复、回滚、端口检查、网站验收，以及不修改宝塔 Nginx 的警告。

- [ ] **步骤 2：运行契约测试，预期 FAIL**
- [ ] **步骤 3：编写服务器准备章节**

逐步创建用户、公钥、项目 Docker 权限、目录、配置模板与权限。明确不改变 root、密码登录、防火墙、宝塔或 Nginx。

- [ ] **步骤 4：编写隧道和首次配置**

```bash
ssh -L 8080:127.0.0.1:8080     -L 6099:127.0.0.1:6099     vaa-deploy@<服务器地址>
```

访问 `http://127.0.0.1:8080/settings` 和 `http://127.0.0.1:6099`。秘密从环境提供且页面只读；非秘密配置可保存。NapCat 反向 WebSocket 使用 `ws://vaa-app:8080/ws/qq`。

- [ ] **步骤 5：编写验收和恢复**

包含 `docker compose ps`、`verify-deployment.sh full`、`ss -lnt`、备份 Timer 状态。恢复 SQLite 前停止应用、保留当前库副本，再显式选取备份；自动回滚不得恢复数据库。

- [ ] **步骤 6：更新本地 QQ README，运行契约测试，预期 PASS**
- [ ] **步骤 7：提交 `docs: add cloud operations runbook`**

### 任务 9：完整本地与容器验证

- [ ] **步骤 1：后端全量测试**

```bash
python -m unittest discover -s backend/tests -v
```

预期：0 failures，0 errors。

- [ ] **步骤 2：云端契约与脚本检查**

```bash
python -m unittest deploy.cloud.tests.test_cloud_contract -v
bash -n deploy/cloud/scripts/healthcheck.sh
bash -n deploy/cloud/scripts/backup-sqlite.sh
bash -n deploy/cloud/scripts/verify-deployment.sh
bash -n deploy/cloud/scripts/deploy.sh
```

预期：全部 exit 0。

- [ ] **步骤 3：镜像与 Compose 冒烟**

创建仅含无效占位值、权限 `0600` 的临时环境文件，然后：

```bash
docker build -f backend/Dockerfile -t vaa-backend:test .
docker compose -f deploy/cloud/docker-compose.yml config --quiet
docker compose -f deploy/cloud/docker-compose.yml up -d --build vaa-app
deploy/cloud/scripts/verify-deployment.sh startup
docker compose -f deploy/cloud/docker-compose.yml down
```

预期：镜像构建、配置解析、live/ready 均通过。删除临时环境文件，不使用 `down -v`。

- [ ] **步骤 4：运行 `npm test --prefix desktop-app`，预期全部 PASS**
- [ ] **步骤 5：运行 `git diff --check`、检查工作区，并用 `git grep` 扫描真实凭据模式**
- [ ] **步骤 6：若发现缺陷，仅修改对应实现文件，重跑全部受影响验证并提交 `fix: harden cloud deployment verification`；无修复则不创建空提交**

### 任务 10：受控服务器上线与真实验收

- [ ] **步骤 1：安全敏感操作前再次确认**

列出将创建的 `vaa-deploy` 公钥、Docker 管理权限、GitHub 部署私钥和 known_hosts；收到明确确认后才继续。不改变 root 登录、密码登录、防火墙、宝塔或 Nginx。

- [ ] **步骤 2：只读复核**

确认系统、磁盘、内存、Swap、Docker、现有容器和监听端口。`sudo docker ps` 只输出名称、镜像、端口，不读取环境变量。

- [ ] **步骤 3：创建部署用户、目录和受限密钥文件**

API Key、Token、密码和私钥由用户输入或用不回显方式写入；不得复制到聊天、命令日志或截图。

- [ ] **步骤 4：首次部署与 SSH 隧道配置**

运行目标 SHA 的 `deploy.sh`，建立两个隧道，配置模型非秘密字段，完成 QQ 扫码和 OneBot 反向 WebSocket。

- [ ] **步骤 5：真实验收**

运行 full 验收，测试 QQ 多轮对话、真实工具调用、两个容器和整机重启恢复；确认公网端口不可达、网站博客正常、资源上限生效、每日备份启用。

- [ ] **步骤 6：再次确认后配置 5 个 GitHub Secrets，验证成功自动部署和可控失败回滚**
- [ ] **步骤 7：只记录提交 SHA、容器状态、健康结果、资源和结论；不记录 IP、QQ 号或任何秘密**
