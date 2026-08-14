# 云端运行监控与自动恢复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为单机云端部署增加受限的 OneBot 自动恢复、持久化运维状态和 Web 设置页只读状态卡片。

**架构：** 宿主机上的 Python 3.6 兼容监控器由 systemd timer 每分钟执行，通过现有健康接口和备份目录生成严格 JSON 状态；达到阈值时仅重启 NapCat。VAA 只读取状态文件并通过只读 API 返回脱敏摘要，设置页登录后每 30 秒展示一次，不向应用容器授予 Docker 权限。

**技术栈：** Python 3.12、Python 3.6 标准库兼容脚本、FastAPI、Pydantic、原生 JavaScript、Node.js `node:test`、Docker Compose、systemd、Python `unittest`

---

## 文件结构

- 创建 `backend/core/cloud_operations.py`：严格解析云端监控状态，执行陈旧判定并返回安全摘要。
- 创建 `backend/tests/test_cloud_operations.py`：覆盖状态解析、缺失、损坏、越界、陈旧和秘密字段拒绝。
- 修改 `backend/core/deployment.py`：为云端状态文件提供配置路径，桌面环境不读取该路径。
- 修改 `backend/tests/test_deployment_config.py`：锁定默认路径和环境覆盖规则。
- 修改 `backend/api/status.py`：新增无副作用的 `GET /api/status/cloud`。
- 创建 `backend/tests/test_cloud_status_api.py`：验证桌面、云端、异常和脱敏响应。
- 修改 `backend/settings/static/index.html`：新增「云端运行状态」只读卡片。
- 修改 `backend/settings/static/settings.js`：管理认证后轮询、可见性暂停、固定文案映射和会话清理。
- 修改 `backend/settings/static/settings.css`：增加状态网格、徽标与移动端布局。
- 修改 `desktop-app/tests/settings-ui-contract.test.js`：锁定安全 DOM、API 路径和轮询契约。
- 修改 `desktop-app/tests/settings-ui-behavior.test.js`：验证登录、登出、隐藏标签页和错误状态下的轮询生命周期。
- 创建 `deploy/cloud/scripts/cloud_monitor.py`：Python 3.6 兼容的单次检查、状态机、锁与原子写入实现。
- 创建 `deploy/cloud/scripts/cloud-monitor.sh`：解析固定部署目录并启动监控器，不接受外部服务名或命令。
- 创建 `deploy/cloud/tests/test_cloud_monitor.py`：用临时目录与假命令覆盖阈值、恢复窗口、部署锁、备份和敏感信息边界。
- 创建 `deploy/cloud/systemd/vaa-cloud-monitor.service`：以 `vaa-deploy` 用户运行一次检查。
- 创建 `deploy/cloud/systemd/vaa-cloud-monitor.timer`：开机后 2 分钟启动，此后每分钟执行。
- 修改 `deploy/cloud/tests/test_cloud_contract.py`：锁定 systemd、权限、路径与 Docker Socket 禁止项。
- 修改 `docs/deployment/cloud-qq-assistant.md`：补充安装、查看、手动运行、停用、告警处理和验收说明。

### 任务 1：实现严格的云端状态读取模型

**文件：**
- 创建：`backend/core/cloud_operations.py`
- 创建：`backend/tests/test_cloud_operations.py`
- 修改：`backend/core/deployment.py`
- 修改：`backend/tests/test_deployment_config.py`

- [ ] **步骤 1：编写部署路径与有效状态的失败测试**

在 `backend/tests/test_deployment_config.py` 增加：

```python
def test_cloud_monitor_state_path_has_safe_default_and_override(self):
    default = DeploymentSettings.from_env(
        {"ASSISTANT_RUNTIME_PROFILE": "cloud"}
    )
    overridden = DeploymentSettings.from_env(
        {
            "ASSISTANT_RUNTIME_PROFILE": "cloud",
            "ASSISTANT_CLOUD_MONITOR_STATE_FILE": "/tmp/monitor.json",
        }
    )

    self.assertEqual(
        default.cloud_monitor_state_file,
        Path("/data/operations/cloud-monitor-state.json"),
    )
    self.assertEqual(
        overridden.cloud_monitor_state_file,
        Path("/tmp/monitor.json"),
    )
```

创建 `backend/tests/test_cloud_operations.py`，先覆盖有效载荷与桌面禁用：

```python
class CloudOperationsTests(unittest.TestCase):
    def test_valid_cloud_state_is_returned_as_safe_camel_case_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(valid_state()), encoding="utf-8")
            reader = CloudOperationsReader(
                profile="cloud",
                state_file=path,
                now=lambda: datetime(2026, 8, 14, 12, 1, tzinfo=timezone.utc),
            )

            payload = reader.snapshot()

        self.assertTrue(payload.available)
        self.assertEqual(payload.overall_state, "healthy")
        self.assertEqual(payload.onebot_state, "connected")

    def test_desktop_profile_never_reads_cloud_file(self):
        reader = CloudOperationsReader(
            profile="desktop",
            state_file=ExplodingPath(),
        )

        self.assertEqual(reader.snapshot().model_dump(by_alias=True), {
            "available": False,
        })
```

- [ ] **步骤 2：运行测试，确认因类型和字段不存在而失败**

运行：

```bash
python -m unittest backend.tests.test_deployment_config backend.tests.test_cloud_operations -v
```

预期：FAIL，包含 `No module named 'core.cloud_operations'` 或 `cloud_monitor_state_file` 不存在。

- [ ] **步骤 3：实现部署路径、严格模型和状态读取器**

在 `backend/core/deployment.py` 为 `DeploymentSettings` 增加：

```python
cloud_monitor_state_file: Path = Path(
    "/data/operations/cloud-monitor-state.json"
)
```

`from_env()` 使用：

```python
cloud_monitor_state_file=Path(
    values.get(
        "ASSISTANT_CLOUD_MONITOR_STATE_FILE",
        "/data/operations/cloud-monitor-state.json",
    ).strip()
),
```

在 `backend/core/cloud_operations.py` 定义严格 Pydantic 模型：

```python
class CloudOperationsState(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )

    schema_version: Literal[1]
    checked_at: datetime
    overall_state: Literal["healthy", "degraded", "alerting", "unknown"]
    vaa_state: Literal["ready", "not_ready", "unavailable", "unknown"]
    onebot_state: Literal[
        "connected", "disconnected", "disabled", "misconfigured", "unknown"
    ]
    backup_state: Literal["fresh", "stale", "missing", "unknown"]
    latest_backup_at: datetime | None
    consecutive_onebot_failures: int = Field(ge=0, le=1000)
    recoveries_in_window: int = Field(ge=0, le=2)
    last_recovery_at: datetime | None
    alert_code: Literal[
        "vaa_unavailable",
        "configuration_required",
        "backup_stale",
        "recovery_exhausted",
        "deployment_in_progress",
        "state_invalid",
    ] | None
```

定义 `CloudOperationsSnapshot` 与 `CloudOperationsReader`。读取器必须：

- 桌面环境直接返回 `available=False`。
- 捕获 `OSError`、`ValidationError` 和 JSON 错误并返回 `unknown`。
- `checked_at` 距当前时间超过 3 分钟时将 `overall_state` 改为 `unknown`，并使用 `alert_code="state_invalid"`。
- 只从模型字段构造响应，不返回原始字典或异常文本。

- [ ] **步骤 4：补齐损坏、额外字段、陈旧与秘密字段测试**

加入表驱动测试：

```python
def test_invalid_or_stale_state_fails_closed_without_echoing_content(self):
    cases = (
        "not-json",
        json.dumps({**valid_state(), "token": "private-token-value"}),
        json.dumps({**valid_state(), "schemaVersion": 2}),
        json.dumps({**valid_state(), "recoveriesInWindow": 3}),
    )
    for content in cases:
        with self.subTest(content=content[:12]):
            payload = snapshot_for(content).model_dump_json()
            self.assertIn('"overallState":"unknown"', payload)
            self.assertNotIn("private-token-value", payload)

def test_state_older_than_three_minutes_is_unknown(self):
    payload = snapshot_for(
        json.dumps(valid_state(checked_at="2026-08-14T11:56:59Z")),
        now=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    self.assertEqual(payload.overall_state, "unknown")
    self.assertEqual(payload.alert_code, "state_invalid")
```

- [ ] **步骤 5：运行任务测试与格式检查**

运行：

```bash
python -m unittest backend.tests.test_deployment_config backend.tests.test_cloud_operations -v
python -m compileall -q backend/core backend/tests
```

预期：全部 PASS，`compileall` 退出码为 0。

- [ ] **步骤 6：提交状态读取器**

```bash
git add backend/core/cloud_operations.py backend/core/deployment.py \
  backend/tests/test_cloud_operations.py backend/tests/test_deployment_config.py
git commit -m "feat: read redacted cloud operations state"
```

### 任务 2：提供无副作用的云端状态 API

**文件：**
- 修改：`backend/api/status.py`
- 创建：`backend/tests/test_cloud_status_api.py`

- [ ] **步骤 1：编写失败的 API 测试**

创建 `backend/tests/test_cloud_status_api.py`：

```python
class CloudStatusApiTests(unittest.TestCase):
    def test_cloud_status_route_returns_reader_snapshot(self):
        snapshot = CloudOperationsSnapshot(
            available=True,
            overall_state="healthy",
            vaa_state="ready",
            onebot_state="connected",
            backup_state="fresh",
            checked_at="2026-08-14T12:00:00Z",
            latest_backup_at="2026-08-14T03:00:00Z",
            consecutive_onebot_failures=0,
            recoveries_in_window=0,
            last_recovery_at=None,
            alert_code=None,
        )
        with patch("api.status.CloudOperationsReader") as reader:
            reader.from_deployment.return_value.snapshot.return_value = snapshot
            with make_client(profile="cloud") as client:
                response = client.get("/api/status/cloud")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["onebotState"], "connected")
        self.assertNotIn("token", response.text.lower())

    def test_desktop_status_is_fixed_unavailable(self):
        with make_client(profile="desktop") as client:
            response = client.get("/api/status/cloud")
        self.assertEqual(response.json(), {"available": False})
```

- [ ] **步骤 2：运行测试，确认路由不存在**

运行：

```bash
python -m unittest backend.tests.test_cloud_status_api -v
```

预期：FAIL，`/api/status/cloud` 返回 404。

- [ ] **步骤 3：实现路由**

在 `backend/api/status.py` 增加：

```python
@router.get("/status/cloud")
def get_cloud_status(request: Request):
    deployment = request.app.state.deployment_settings
    return CloudOperationsReader.from_deployment(deployment).snapshot()
```

保持现有 `/api/status` 行为不变。读取器内部处理文件异常，路由不捕获并回显异常。

- [ ] **步骤 4：增加损坏状态与秘密不回显测试**

```python
def test_invalid_state_returns_safe_unknown_payload(self):
    with temporary_cloud_state('{"token":"do-not-leak"}') as deployment:
        with make_client(deployment=deployment) as client:
            response = client.get("/api/status/cloud")

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["overallState"], "unknown")
    self.assertNotIn("do-not-leak", response.text)
```

- [ ] **步骤 5：运行 API 与健康接口回归测试**

运行：

```bash
python -m unittest backend.tests.test_cloud_status_api backend.tests.test_health_api -v
```

预期：全部 PASS。

- [ ] **步骤 6：提交云端状态 API**

```bash
git add backend/api/status.py backend/tests/test_cloud_status_api.py
git commit -m "feat: expose safe cloud operations status"
```

### 任务 3：在设置页展示云端运行状态

**文件：**
- 修改：`backend/settings/static/index.html`
- 修改：`backend/settings/static/settings.js`
- 修改：`backend/settings/static/settings.css`
- 修改：`desktop-app/tests/settings-ui-contract.test.js`
- 修改：`desktop-app/tests/settings-ui-behavior.test.js`

- [ ] **步骤 1：编写失败的前端契约测试**

在 `desktop-app/tests/settings-ui-contract.test.js` 增加：

```javascript
test('cloud operations card uses a read-only safe status contract', () => {
    const html = read('index.html');
    const source = read('settings.js');

    for (const id of [
        'cloud-operations', 'cloud-overall-state', 'cloud-vaa-state',
        'cloud-onebot-state', 'cloud-backup-state', 'cloud-recovery-state',
        'cloud-alert-state'
    ]) assert.match(html, new RegExp(`id="${id}"`));

    assert.match(source, /\/api\/status\/cloud/);
    assert.match(source, /30_000/);
    assert.match(source, /visibilitychange/);
    assert.doesNotMatch(source, /innerHTML|localStorage|sessionStorage|console\./);
});
```

在行为测试的 DOM 夹具中加入状态节点，并增加：

```javascript
test('cloud polling starts after authentication and stops on logout', async () => {
    const harness = await createHarness({ authenticated: true });
    assert.equal(harness.requests('/api/status/cloud').length, 1);

    await harness.logout();
    harness.advanceTimersByTime(30_000);

    assert.equal(harness.requests('/api/status/cloud').length, 1);
});

test('hidden page pauses cloud polling and visible page refreshes immediately', async () => {
    const harness = await createHarness({ authenticated: true });
    harness.setVisibility('hidden');
    harness.advanceTimersByTime(60_000);
    assert.equal(harness.requests('/api/status/cloud').length, 1);

    harness.setVisibility('visible');
    assert.equal(harness.requests('/api/status/cloud').length, 2);
});
```

- [ ] **步骤 2：运行前端测试，确认卡片和轮询缺失**

运行：

```bash
node --test desktop-app/tests/settings-ui-contract.test.js \
  desktop-app/tests/settings-ui-behavior.test.js
```

预期：FAIL，提示 `cloud-operations` 或 `/api/status/cloud` 不存在。

- [ ] **步骤 3：添加语义化只读卡片与样式**

在 `backend/settings/static/index.html` 的认证后主界面顶部增加：

```html
<section id="cloud-operations" class="operations-card" aria-labelledby="cloud-operations-title" hidden>
  <header>
    <p class="section-number">CLOUD · OPERATIONS</p>
    <h2 id="cloud-operations-title">云端运行状态</h2>
  </header>
  <dl class="operations-grid">
    <div><dt>总体</dt><dd id="cloud-overall-state">正在读取…</dd></div>
    <div><dt>VAA</dt><dd id="cloud-vaa-state">未知</dd></div>
    <div><dt>QQ / OneBot</dt><dd id="cloud-onebot-state">未知</dd></div>
    <div><dt>SQLite 备份</dt><dd id="cloud-backup-state">未知</dd></div>
    <div><dt>自动恢复</dt><dd id="cloud-recovery-state">尚无记录</dd></div>
    <div><dt>告警</dt><dd id="cloud-alert-state">无</dd></div>
  </dl>
</section>
```

在 `settings.css` 添加 `.operations-card`、`.operations-grid` 和状态徽标样式；移动端将网格降为单列。沿用现有 `--paper`、`--jade`、`--brick` 与 `prefers-reduced-motion` 规则，不引入外部资源。

- [ ] **步骤 4：实现轮询生命周期和固定文案映射**

在 `settings.js` 的 `state` 中增加：

```javascript
cloudStatusController: null,
cloudStatusTimer: null,
```

定义封闭映射与更新函数：

```javascript
const CLOUD_POLL_INTERVAL_MS = 30_000;
const CLOUD_LABELS = Object.freeze({
  overall: { healthy: '正常', degraded: '降级', alerting: '需要处理', unknown: '未知' },
  vaa: { ready: '正常', not_ready: '未就绪', unavailable: '不可用', unknown: '未知' },
  onebot: { connected: '已连接', disconnected: '已断开', disabled: '未启用', misconfigured: '配置错误', unknown: '未知' },
  backup: { fresh: '正常', stale: '已过期', missing: '未找到', unknown: '未知' },
});

function safeCloudLabel(group, value) {
  return CLOUD_LABELS[group][value] || '未知';
}
```

`startCloudPolling()` 仅在认证成功且 `document.visibilityState === 'visible'` 时请求；`stopCloudPolling()` 必须取消定时器与 `AbortController`。登录成功调用启动，登出、401 和会话清理调用停止。`visibilitychange` 在隐藏时停止，在重新可见且仍认证时立即启动。

对 `available=false` 隐藏整张卡片；请求失败时保留卡片并显示「暂时无法读取云端状态」。所有内容仅通过 `textContent` 写入。

- [ ] **步骤 5：运行前端测试**

运行：

```bash
node --test desktop-app/tests/settings-ui-contract.test.js \
  desktop-app/tests/settings-ui-behavior.test.js \
  desktop-app/tests/settings-entry-contract.test.js
```

预期：全部 PASS。

- [ ] **步骤 6：提交设置页状态卡片**

```bash
git add backend/settings/static/index.html backend/settings/static/settings.js \
  backend/settings/static/settings.css \
  desktop-app/tests/settings-ui-contract.test.js \
  desktop-app/tests/settings-ui-behavior.test.js
git commit -m "feat: show cloud operations status in settings"
```

### 任务 4：实现主机监控状态机与受控恢复

**文件：**
- 创建：`deploy/cloud/scripts/cloud_monitor.py`
- 创建：`deploy/cloud/scripts/cloud-monitor.sh`
- 创建：`deploy/cloud/tests/test_cloud_monitor.py`

- [ ] **步骤 1：编写状态转换的失败测试**

创建 `deploy/cloud/tests/test_cloud_monitor.py`，通过 `importlib.util` 加载带连字符目录中的脚本模块，并定义固定 UTC 时钟。先覆盖阈值：

```python
def test_third_consecutive_disconnect_requests_one_recovery(self):
    state = base_state(consecutiveOnebotFailures=2)

    result = evaluate(
        previous=state,
        observation=Observation("ready", "disconnected", "fresh", BACKUP_AT),
        now=NOW,
    )

    self.assertTrue(result.restart_napcat)
    self.assertEqual(result.state["consecutiveOnebotFailures"], 0)
    self.assertEqual(result.state["recoveriesInWindow"], 1)

def test_recovery_limit_enters_alert_without_restart(self):
    state = base_state(
        consecutiveOnebotFailures=2,
        recoveryTimestamps=[ten_minutes_ago(), five_minutes_ago()],
    )

    result = evaluate(state, disconnected_observation(), NOW)

    self.assertFalse(result.restart_napcat)
    self.assertEqual(result.state["alertCode"], "recovery_exhausted")
```

同时覆盖 `connected` 清零、`disabled` 不恢复、`misconfigured` 立即告警、VAA 不可用不重启。

- [ ] **步骤 2：运行测试，确认监控模块不存在**

运行：

```bash
python -m unittest deploy.cloud.tests.test_cloud_monitor -v
```

预期：FAIL，提示无法加载 `cloud_monitor.py`。

- [ ] **步骤 3：实现纯状态转换函数**

在 `cloud_monitor.py` 中只使用 Python 3.6 语法与标准库，定义：

```python
Observation = collections.namedtuple(
    "Observation", "vaa_state onebot_state backup_state latest_backup_at"
)
Evaluation = collections.namedtuple("Evaluation", "state restart_napcat")

FAILURES_BEFORE_RECOVERY = 3
RECOVERY_WINDOW_SECONDS = 600
MAX_RECOVERIES_IN_WINDOW = 2
BACKUP_STALE_SECONDS = 36 * 60 * 60
```

`evaluate(previous, observation, now)` 必须是无 I/O 的纯函数。内部状态可以保存 `recoveryTimestamps` 供下一次计算，但写入公开状态文件前必须移除该内部字段；公开 `recoveriesInWindow` 由窗口内时间戳数量计算。

- [ ] **步骤 4：编写 I/O、锁、原子写入和恢复前复查的失败测试**

```python
def test_restart_is_skipped_when_recheck_has_recovered(self):
    runner = FakeRunner(
        health=[disconnected_observation(), connected_observation()]
    )
    run_once(config(), runner=runner, now=lambda: NOW)
    self.assertNotIn(["docker", "compose", "restart", "napcat"], runner.commands)

def test_state_write_is_atomic_and_contains_no_sensitive_content(self):
    runner = FakeRunner(health=[connected_observation()])
    run_once(config(state_file=self.state_file), runner=runner, now=lambda: NOW)
    payload = self.state_file.read_text()
    self.assertEqual(json.loads(payload)["schemaVersion"], 1)
    for forbidden in ("token", "apiKey", "2994508531", "601888065"):
        self.assertNotIn(forbidden, payload)

def test_locked_deployment_records_state_without_docker_action(self):
    runner = FakeRunner(deployment_lock_busy=True)
    run_once(config(), runner=runner, now=lambda: NOW)
    self.assertEqual(read_state()["alertCode"], "deployment_in_progress")
    self.assertFalse(runner.restart_called)
```

- [ ] **步骤 5：实现一次监控执行**

实现以下职责：

```python
def run_once(config, runner, now):
    with runner.acquire_monitor_lock(config.monitor_lock_file):
        previous = load_private_state(config.private_state_file)
        observation = runner.observe(config)
        evaluation = evaluate(previous, observation, now())
        if evaluation.restart_napcat:
            if runner.deployment_lock_busy(config.deploy_lock_file):
                evaluation = deployment_in_progress(evaluation, now())
            elif runner.observe_onebot(config) == "disconnected":
                runner.restart_napcat(config.compose_file)
                evaluation = record_recovery(evaluation, now())
        atomic_write_json(config.private_state_file, evaluation.state, mode=0o640)
        atomic_write_json(
            config.public_state_file,
            public_state(evaluation.state),
            mode=0o640,
        )
```

`SubprocessRunner` 的健康请求使用 `urllib.request.urlopen(..., timeout=5)` 并只接受精确 JSON。Docker 命令固定为：

```python
["docker", "compose", "-f", config.compose_file, "restart", "napcat"]
```

不得使用 `shell=True`，不得读取 `.env` 或 `secrets.env`，不得把响应正文、异常原文或命令输出写入状态文件。

`cloud-monitor.sh` 固定计算仓库与 Compose 路径：

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_dir/cloud_monitor.py" \
  --compose-file "$script_dir/../docker-compose.yml" \
  --public-state-file "$script_dir/../data/vaa/operations/cloud-monitor-state.json" \
  --private-state-file "$script_dir/../data/monitor/cloud-monitor-private.json" \
  --backup-directory "$script_dir/../data/vaa/backups"
```

- [ ] **步骤 6：增加 Python 3.6 语法与脚本权限测试**

测试中运行：

```python
subprocess.run(
    [sys.executable, str(SCRIPT), "--help"],
    check=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
```

并在 CI 契约中禁止脚本出现 `shell=True`、`secrets.env`、`docker compose config`、`docker compose down` 和可变服务名。

- [ ] **步骤 7：运行监控测试**

运行：

```bash
python -m unittest deploy.cloud.tests.test_cloud_monitor -v
bash -n deploy/cloud/scripts/cloud-monitor.sh
python -m compileall -q deploy/cloud/scripts/cloud_monitor.py
```

预期：全部 PASS，Shell 与 Python 语法检查退出码为 0。

- [ ] **步骤 8：提交监控器**

```bash
git add deploy/cloud/scripts/cloud_monitor.py \
  deploy/cloud/scripts/cloud-monitor.sh \
  deploy/cloud/tests/test_cloud_monitor.py
git commit -m "feat: add bounded cloud recovery monitor"
```

### 任务 5：接入 systemd 并锁定部署安全边界

**文件：**
- 创建：`deploy/cloud/systemd/vaa-cloud-monitor.service`
- 创建：`deploy/cloud/systemd/vaa-cloud-monitor.timer`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：编写失败的 systemd 契约测试**

在 `test_cloud_contract.py` 增加：

```python
def test_cloud_monitor_timer_is_bounded_persistent_and_non_privileged(self):
    service = (ROOT / "deploy/cloud/systemd/vaa-cloud-monitor.service").read_text()
    timer = (ROOT / "deploy/cloud/systemd/vaa-cloud-monitor.timer").read_text()

    self.assertIn("Type=oneshot", service)
    self.assertIn("User=vaa-deploy", service)
    self.assertIn("Group=vaa-deploy", service)
    self.assertIn("ExecStart=/opt/virtual-anime-assistant/current/deploy/cloud/scripts/cloud-monitor.sh", service)
    self.assertIn("OnBootSec=2min", timer)
    self.assertIn("OnUnitActiveSec=1min", timer)
    self.assertIn("Persistent=true", timer)
    self.assertIn("RandomizedDelaySec=10s", timer)

def test_application_never_receives_docker_socket(self):
    serialized = json.dumps(self.compose)
    self.assertNotIn("docker.sock", serialized)
    self.assertNotIn("/var/run/docker", serialized)
```

- [ ] **步骤 2：运行契约测试，确认单元文件缺失**

运行：

```bash
python -m unittest deploy.cloud.tests.test_cloud_contract -v
```

预期：FAIL，提示 `vaa-cloud-monitor.service` 不存在。

- [ ] **步骤 3：创建 systemd 单元**

`vaa-cloud-monitor.service`：

```ini
[Unit]
Description=Monitor Virtual Anime Assistant cloud operation
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
User=vaa-deploy
Group=vaa-deploy
WorkingDirectory=/opt/virtual-anime-assistant/current/deploy/cloud
ExecStart=/opt/virtual-anime-assistant/current/deploy/cloud/scripts/cloud-monitor.sh
```

`vaa-cloud-monitor.timer`：

```ini
[Unit]
Description=Run Virtual Anime Assistant cloud monitor every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
RandomizedDelaySec=10s
Persistent=true
Unit=vaa-cloud-monitor.service

[Install]
WantedBy=timers.target
```

- [ ] **步骤 4：运行部署契约测试**

运行：

```bash
python -m unittest deploy.cloud.tests.test_cloud_contract -v
```

预期：全部 PASS。

- [ ] **步骤 5：提交 systemd 接入**

```bash
git add deploy/cloud/systemd/vaa-cloud-monitor.service \
  deploy/cloud/systemd/vaa-cloud-monitor.timer \
  deploy/cloud/tests/test_cloud_contract.py
git commit -m "ops: schedule cloud recovery monitor"
```

### 任务 6：补充运维文档并执行完整验证

**文件：**
- 修改：`docs/deployment/cloud-qq-assistant.md`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：先扩展文档契约测试**

在 `test_cloud_runbook_covers_backup_recovery_and_acceptance` 的必需词组中加入：

```python
for required in (
    "vaa-cloud-monitor.timer",
    "systemctl status vaa-cloud-monitor.timer",
    "systemctl start vaa-cloud-monitor.service",
    "recovery_exhausted",
    "/api/status/cloud",
):
    self.assertIn(required, runbook)
```

- [ ] **步骤 2：运行契约测试，确认文档内容缺失**

运行：

```bash
python -m unittest deploy.cloud.tests.test_cloud_contract.CloudDeploymentContractTests.test_cloud_runbook_covers_backup_recovery_and_acceptance -v
```

预期：FAIL，提示 `vaa-cloud-monitor.timer` 不在文档中。

- [ ] **步骤 3：编写安装、查看、手动执行和停用说明**

在 `docs/deployment/cloud-qq-assistant.md` 增加「长期运行监控」章节，包含可直接运行的命令：

```bash
sudo install -m 0644 deploy/cloud/systemd/vaa-cloud-monitor.service \
  /etc/systemd/system/vaa-cloud-monitor.service
sudo install -m 0644 deploy/cloud/systemd/vaa-cloud-monitor.timer \
  /etc/systemd/system/vaa-cloud-monitor.timer
sudo systemctl daemon-reload
sudo systemctl enable --now vaa-cloud-monitor.timer
systemctl status vaa-cloud-monitor.timer
systemctl start vaa-cloud-monitor.service
```

同时说明：

- `recovery_exhausted` 表示 10 分钟内恢复已达 2 次，需要检查 QQ 是否要求扫码。
- `/api/status/cloud` 只返回固定摘要，不应包含 Token、QQ 号或日志。
- 停用使用 `sudo systemctl disable --now vaa-cloud-monitor.timer`，不会删除 QQ 登录或 SQLite 数据。
- 监控状态异常不得通过开放公网端口解决。

- [ ] **步骤 4：运行各组件定向测试**

运行：

```bash
python -m unittest \
  backend.tests.test_deployment_config \
  backend.tests.test_cloud_operations \
  backend.tests.test_cloud_status_api \
  backend.tests.test_health_api \
  deploy.cloud.tests.test_cloud_monitor \
  deploy.cloud.tests.test_cloud_contract -v
node --test desktop-app/tests/settings-ui-contract.test.js \
  desktop-app/tests/settings-ui-behavior.test.js \
  desktop-app/tests/settings-entry-contract.test.js
bash -n deploy/cloud/scripts/cloud-monitor.sh
```

预期：全部 PASS。

- [ ] **步骤 5：运行完整回归测试**

运行：

```bash
python -m unittest discover -s backend/tests -p 'test_*.py'
python -m unittest discover -s deploy/cloud/tests -p 'test_*.py'
npm test --prefix desktop-app
git diff --check
```

预期：全部命令退出码为 0，无失败、错误或空白问题。

- [ ] **步骤 6：执行敏感信息与安全边界检查**

运行：

```bash
rg -n "docker\.sock|/var/run/docker|shell=True|secrets\.env|printenv|env \|" \
  backend deploy/cloud docs/deployment/cloud-qq-assistant.md
```

预期：只命中文档中明确说明的禁止项与现有 `env_file: secrets.env`；监控脚本、状态模型和 API 中没有命中。

运行：

```bash
rg -n "token|apiKey|allowedUserIds|allowedGroupIds" \
  deploy/cloud/scripts/cloud_monitor.py backend/core/cloud_operations.py
```

预期：实现文件不读取或输出这些秘密与标识字段；测试中的禁止词断言允许出现。

- [ ] **步骤 7：提交文档与最终契约**

```bash
git add docs/deployment/cloud-qq-assistant.md deploy/cloud/tests/test_cloud_contract.py
git commit -m "docs: add cloud monitor operations guide"
```

- [ ] **步骤 8：检查提交序列和工作区**

运行：

```bash
git status --short --branch
git log --oneline origin/main..HEAD
```

预期：工作区干净，设计、计划与 6 个实现提交均位于 `codex/cloud-operations-monitoring` 分支。

## 云端部署后的人工验收

实现分支通过 CI 并合并后，再在服务器执行以下不可由单元测试代替的验收：

1. 安装并启用 `vaa-cloud-monitor.timer`。
2. 手动运行 `vaa-cloud-monitor.service`，确认 `/api/status/cloud` 为 `healthy`。
3. 确认 Web 设置页展示 VAA、QQ、备份与恢复状态。
4. 在不删除数据卷的前提下模拟 OneBot 断开，验证第 3 次检查只重启一次 NapCat。
5. QQ 若要求扫码，由用户亲自完成；随后运行 `verify-deployment.sh full`。
6. 验证公网端口、WordPress、博客、备份 timer 与现有数据库均未受影响。
