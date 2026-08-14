# macOS 本机状态助手与受控操作实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为 macOS 桌面版增加可扩展的实时状态感知与逐次确认操作，并让云端 QQ 通过受限 SSH 隧道查询本机主动上报的脱敏最新状态。

**架构：** 新增版本化状态模型、Provider/ActionProvider、能力注册表和渠道策略；本机只运行固定系统探针并在内存缓存快照。桌面模型可提议固定高风险操作并等待 Live2D 确认；云端只保存每台设备的最新快照，通过专用只读工具向 QQ 提供状态。

**技术栈：** Python 3.12、FastAPI、Pydantic、psutil、asyncio subprocess、SQLite、Electron、原生 JavaScript、OpenSSH、Python `unittest`、Node.js `node:test`

---

## 文件结构

- 创建 `backend/computer/models.py`：版本化快照、状态分区、能力与渠道枚举。
- 创建 `backend/computer/capabilities.py`：Provider/ActionProvider 协议、能力注册表和渠道策略。
- 创建 `backend/computer/privacy.py`：保守的 macOS 应用隐私分级。
- 创建 `backend/computer/macos.py`：固定命令执行器、macOS 状态 Provider 和操作适配器。
- 创建 `backend/computer/state.py`：采集调度、最新快照缓存、TTL 与远端最新状态存储。
- 创建 `backend/computer/reporter.py`：受限 SSH 隧道生命周期与脱敏状态上报。
- 创建 `backend/computer/tools.py`：状态查询及 4 个固定操作的工具定义。
- 创建 `backend/api/computer.py`：本机状态读取和云端设备状态上报 API。
- 修改 `backend/domain/tools.py`、`backend/tools/registry.py`、`backend/tools/catalog.py`：持久化原始消息渠道并支持显式高风险提议。
- 修改 `backend/application/model_tools.py`、`backend/application/assistant.py`：按消息渠道生成目录并等待确认终态。
- 修改 `backend/core/runtime.py`、`backend/core/deployment.py`、`backend/api/app.py`：按运行模式组装能力和后台任务。
- 修改 `backend/settings/` 与 `backend/settings/static/`：增加 3 个独立开关和安全配置展示。
- 修改 `backend/infrastructure/sqlite_store.py`：为工具请求增加原始消息渠道迁移。
- 创建 `deploy/cloud/scripts/install-state-relay-access.sh`：安装只允许转发到回环 8080 的专用 SSH 公钥。
- 修改 `deploy/cloud/.env.example`、`deploy/cloud/secrets.env.example`、`deploy/cloud/tests/test_cloud_contract.py`：云端状态接收配置与安全契约。
- 创建 `backend/tests/test_computer_models.py`、`test_computer_privacy.py`、`test_macos_computer.py`、`test_computer_state.py`、`test_computer_tools.py`、`test_computer_report_api.py`、`test_computer_reporter.py`。
- 修改 `backend/tests/test_model_tool_catalog.py`、`test_model_tool_orchestrator.py`、`test_tool_service.py`、`test_sqlite_store.py`、`test_runtime.py`、`test_settings_api.py`：锁定渠道、确认、持久化和配置行为。
- 修改 `desktop-app/tests/tool-confirmation-contract.test.js`：锁定电脑操作确认展示与断线行为。
- 修改 `README.md`、`docs/deployment/cloud-qq-assistant.md`：配置、隐私、授权和运维说明。

## 阶段 A：本机状态基础

### 任务 1：定义版本化状态与扩展接口

**文件：**
- 创建：`backend/computer/__init__.py`
- 创建：`backend/computer/models.py`
- 创建：`backend/computer/capabilities.py`
- 测试：`backend/tests/test_computer_models.py`

- [ ] **步骤 1：编写失败的模型与注册表测试**

测试必须覆盖：`device_id` 格式、UTC 时间、45 秒 TTL、未知扩展字段兼容、重复能力拒绝、平台和渠道过滤。核心断言：

```python
snapshot = ComputerSnapshot(
    device_id="macbook-main",
    platform=ComputerPlatform.MACOS,
    collected_at=now,
    expires_at=now + timedelta(seconds=45),
    capabilities=frozenset({"system.resources"}),
    state={"system.resources": {"status": "available", "cpuPercent": 20}},
)
self.assertEqual(snapshot.schema_version, 1)
self.assertTrue(snapshot.is_fresh(now + timedelta(seconds=44)))
self.assertFalse(snapshot.is_fresh(now + timedelta(seconds=45)))
```

- [ ] **步骤 2：运行测试并确认因模块不存在而失败**

运行：`python3 -m unittest backend.tests.test_computer_models -v`

预期：FAIL，包含 `ModuleNotFoundError: No module named 'computer'`。

- [ ] **步骤 3：实现最小模型与接口**

实现以下稳定类型，字段使用严格 Pydantic 配置：

```python
class ComputerPlatform(str, Enum):
    MACOS = "macos"

class ModelAccess(str, Enum):
    HIDDEN = "hidden"
    READ_ONLY = "read_only"
    PROPOSE_WITH_CONFIRMATION = "propose_with_confirmation"

class ComputerSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    device_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
    platform: ComputerPlatform
    collected_at: datetime
    expires_at: datetime
    capabilities: frozenset[str]
    state: dict[str, dict[str, Any]]

    def is_fresh(self, now: datetime) -> bool:
        return now < self.expires_at
```

`CapabilityDefinition` 必须包含名称、平台、风险、模型访问模式、允许消息渠道和 Provider。`CapabilityRegistry.register()` 拒绝重复名称；`ChannelPolicy.list_for()` 同时检查平台、运行模式和消息渠道。

- [ ] **步骤 4：运行定向测试**

运行：`python3 -m unittest backend.tests.test_computer_models -v`

预期：全部 PASS。

- [ ] **步骤 5：提交扩展基础**

```bash
git add -- backend/computer/__init__.py backend/computer/models.py \
  backend/computer/capabilities.py backend/tests/test_computer_models.py
git commit -m "feat: define extensible computer capabilities"
```

### 任务 2：实现本机隐私分级

**文件：**
- 创建：`backend/computer/privacy.py`
- 测试：`backend/tests/test_computer_privacy.py`

- [ ] **步骤 1：编写失败的隐私测试**

覆盖 `SHOW`、`BROWSER`、`HIDE_TITLE`、`SECRET` 和未知应用默认隐藏。至少断言：

```python
self.assertEqual(classify_app("Safari").level, PrivacyLevel.BROWSER)
self.assertIsNone(sanitize_foreground("Safari", "Bank - Account").window_title)
self.assertEqual(sanitize_foreground("1Password", "Vault").app_name, "私密应用")
self.assertIsNone(sanitize_foreground("UnknownApp", "Sensitive title").window_title)
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest backend.tests.test_computer_privacy -v`

预期：FAIL，隐私模块不存在。

- [ ] **步骤 3：实现保守分类器**

分类表使用规范化应用名与 Bundle ID；浏览器、聊天、邮件、终端、银行、密码管理器和验证器使用代码内静态集合。未识别应用返回 `HIDE_TITLE`。所有标题先移除控制字符再截断到 128 字符。

- [ ] **步骤 4：运行测试和敏感词静态扫描**

```bash
python3 -m unittest backend.tests.test_computer_privacy -v
rg -n "Bank - Account|Sensitive title|Vault" backend/computer
```

预期：测试 PASS；`rg` 仅命中静态分类测试数据，不命中生产日志语句。

- [ ] **步骤 5：提交隐私分级**

```bash
git add -- backend/computer/privacy.py backend/tests/test_computer_privacy.py
git commit -m "feat: classify private macOS application state"
```

### 任务 3：实现固定 macOS 状态 Provider

**文件：**
- 创建：`backend/computer/macos.py`
- 测试：`backend/tests/test_macos_computer.py`

- [ ] **步骤 1：编写失败的固定命令与解析测试**

注入 `ProcessRunner` 替身，验证所有命令均为参数元组；覆盖资源、电池、前台应用、全屏、锁屏、空闲、网络、媒体和音量。禁止测试替身接收字符串命令：

```python
await provider.collect()
self.assertIn(("/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"), runner.calls)
self.assertTrue(all(isinstance(call, tuple) for call in runner.calls))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest backend.tests.test_macos_computer -v`

预期：FAIL，`MacOSStateProvider` 不存在。

- [ ] **步骤 3：实现异步固定执行器和独立 Provider**

`AsyncProcessRunner.run(argv, timeout=3)` 必须使用 `asyncio.create_subprocess_exec(*argv)`，捕获有限 stdout/stderr，超时后 kill 并返回 `state_probe_timeout`。AppleScript 内容只能来自模块常量，不能拼接采集结果或模型输入。

Provider 返回独立命名空间对象：

```python
ProviderResult(
    capability="system.resources",
    state={"status": "available", "cpuPercent": cpu, "memoryPercent": memory},
)
```

- [ ] **步骤 4：运行测试与禁止项扫描**

```bash
python3 -m unittest backend.tests.test_macos_computer -v
rg -n "shell=True|create_subprocess_shell|subprocess\.run\([^[]" backend/computer/macos.py
```

预期：测试 PASS；扫描无命中。

- [ ] **步骤 5：提交 macOS Provider**

```bash
git add -- backend/computer/macos.py backend/tests/test_macos_computer.py
git commit -m "feat: collect bounded macOS computer state"
```

### 任务 4：调度采集并缓存最新状态

**文件：**
- 创建：`backend/computer/state.py`
- 测试：`backend/tests/test_computer_state.py`
- 修改：`backend/core/runtime.py`
- 修改：`backend/api/app.py`
- 修改：`backend/tests/test_runtime.py`

- [ ] **步骤 1：编写失败的状态服务测试**

覆盖并行 Provider、部分失败、5 秒刷新、15 秒本地陈旧、45 秒远端离线和 `aclose()` 停止。测试时使用注入时钟，不真实等待。

- [ ] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest backend.tests.test_computer_state -v`

预期：FAIL，`DesktopStateService` 不存在。

- [ ] **步骤 3：实现状态服务和运行时生命周期**

`collect_once()` 使用 `asyncio.gather(..., return_exceptions=True)`；每个异常转换为对应 capability 的 `unavailable`。`latest()` 只返回安全模型副本。FastAPI lifespan 仅在 desktop + macOS + 功能开启时启动采集循环，关闭时先 cancel 再等待清理。

- [ ] **步骤 4：运行状态和运行时测试**

```bash
python3 -m unittest backend.tests.test_computer_state backend.tests.test_runtime -v
```

预期：全部 PASS，现有资源关闭顺序不变。

- [ ] **步骤 5：提交状态生命周期**

```bash
git add -- backend/computer/state.py backend/core/runtime.py backend/api/app.py \
  backend/tests/test_computer_state.py backend/tests/test_runtime.py
git commit -m "feat: cache live computer state in memory"
```

## 阶段 B：桌面查询与受控操作

### 任务 5：按消息渠道提供只读状态工具

**文件：**
- 创建：`backend/computer/tools.py`
- 修改：`backend/tools/catalog.py`
- 修改：`backend/application/model_tools.py`
- 修改：`backend/application/assistant.py`
- 测试：`backend/tests/test_computer_tools.py`
- 修改：`backend/tests/test_model_tool_catalog.py`
- 修改：`backend/tests/test_model_tool_orchestrator.py`

- [ ] **步骤 1：编写失败的渠道目录测试**

断言 Desktop 使用本地状态实现，QQ 使用远端状态实现；关闭功能或状态缺失时工具隐藏或返回稳定错误。`ModelToolCatalog.list()` 改为显式接收 `MessageSource`：

```python
desktop = catalog.list(MessageSource.DESKTOP)
qq = catalog.list(MessageSource.QQ)
self.assertIn("computer.current_state", {tool.name for tool in desktop})
self.assertNotIn("computer.open_application", {tool.name for tool in qq})
```

- [ ] **步骤 2：运行测试确认当前静态目录不满足契约**

运行：`python3 -m unittest backend.tests.test_computer_tools backend.tests.test_model_tool_catalog -v`

预期：FAIL，目录不接受消息渠道且状态工具不存在。

- [ ] **步骤 3：实现只读工具与渠道目录**

`computer.current_state` 使用空参数模型、`ToolRisk.LOW` 和 `ModelAccess.READ_ONLY`。`AssistantApplication` 将 `IncomingMessage.source` 传给编排器；编排器按本轮原始渠道取得目录，不能把客户端提交的字符串当作渠道。

- [ ] **步骤 4：运行目录、编排器和应用回归**

```bash
python3 -m unittest backend.tests.test_computer_tools \
  backend.tests.test_model_tool_catalog \
  backend.tests.test_model_tool_orchestrator \
  backend.tests.test_application_foundation -v
```

预期：全部 PASS。

- [ ] **步骤 5：提交只读工具**

```bash
git add -- backend/computer/tools.py backend/tools/catalog.py \
  backend/application/model_tools.py backend/application/assistant.py \
  backend/tests/test_computer_tools.py backend/tests/test_model_tool_catalog.py \
  backend/tests/test_model_tool_orchestrator.py
git commit -m "feat: expose channel-scoped computer state"
```

### 任务 6：实现 4 个固定 macOS 操作

**文件：**
- 修改：`backend/computer/macos.py`
- 修改：`backend/computer/tools.py`
- 修改：`backend/tests/test_macos_computer.py`
- 修改：`backend/tests/test_computer_tools.py`

- [ ] **步骤 1：编写失败的参数与命令测试**

覆盖应用名路径/选项/控制字符拒绝、HTTPS URL、用户信息、内网主机、IP、URL 查询值脱敏、播放器枚举和 `0～100` 音量。批准后的精确命令示例：

```python
self.assertEqual(runner.calls[-1], ("/usr/bin/open", "-a", "Safari"))
self.assertEqual(runner.calls[-1], ("/usr/bin/open", "https://example.com/docs"))
```

- [ ] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest backend.tests.test_macos_computer backend.tests.test_computer_tools -v`

预期：FAIL，操作参数模型和 ActionProvider 不存在。

- [ ] **步骤 3：实现操作参数与 Provider**

4 个工具均为 `ToolRisk.HIGH`、`ModelAccess.PROPOSE_WITH_CONFIRMATION`、只允许 Desktop 原始渠道。URL 工具把 `url` 声明为敏感字段，确认摘要由专用 sanitizer 生成主机、路径和已隐藏查询值。

- [ ] **步骤 4：运行操作测试与命令扫描**

```bash
python3 -m unittest backend.tests.test_macos_computer backend.tests.test_computer_tools -v
rg -n "shell=True|create_subprocess_shell|os\.system" backend/computer
```

预期：测试 PASS；禁止项无命中。

- [ ] **步骤 5：提交固定操作**

```bash
git add -- backend/computer/macos.py backend/computer/tools.py \
  backend/tests/test_macos_computer.py backend/tests/test_computer_tools.py
git commit -m "feat: add confirmed macOS actions"
```

### 任务 7：让桌面模型等待确认后恢复本轮

**文件：**
- 修改：`backend/domain/tools.py`
- 修改：`backend/tools/registry.py`
- 修改：`backend/tools/service.py`
- 修改：`backend/application/model_tools.py`
- 修改：`backend/infrastructure/sqlite_store.py`
- 修改：`backend/tests/test_tool_service.py`
- 修改：`backend/tests/test_model_tool_orchestrator.py`
- 修改：`backend/tests/test_sqlite_store.py`
- 修改：`desktop-app/tests/tool-confirmation-contract.test.js`

- [ ] **步骤 1：编写失败的高风险提议测试**

测试必须证明：QQ 看不到工具；Desktop 模型请求进入 `pending_confirmation`；编排器等待；批准后只执行一次并继续下一轮模型请求；拒绝、60 秒超时、Electron 离线和服务关闭不执行。

- [ ] **步骤 2：运行测试确认当前模型高风险路径被拒绝**

```bash
python3 -m unittest backend.tests.test_tool_service \
  backend.tests.test_model_tool_orchestrator backend.tests.test_sqlite_store -v
```

预期：FAIL，现有服务把所有 MODEL 高风险请求当作不可用。

- [ ] **步骤 3：持久化可信原始渠道**

为 `ToolRequest` 和 `ToolRequestRecord` 增加 `origin: MessageSource`；SQLite 新迁移为 `tool_requests.origin` 增加非空约束，旧记录按可信 `source` 映射到 `desktop` 或 `system`，`model` 旧记录固定映射为 `system`，不得推断为 QQ。

- [ ] **步骤 4：实现显式提议与终态等待器**

只有定义同时满足 `HIGH + PROPOSE_WITH_CONFIRMATION + origin=DESKTOP` 且确认客户端在线时，MODEL 请求才允许创建确认。`ToolExecutionService.wait_for_terminal(request_id, timeout=60)` 使用 Future 和发布器事件完成；服务关闭时所有 waiter 以 `confirmation_client_unavailable` 结束。

- [ ] **步骤 5：让编排器依据真实终态继续**

收到 `PENDING_CONFIRMATION` 时等待终态；只有 `SUCCEEDED` 返回结果，拒绝、过期、取消和失败均作为真实 Tool 结果交给模型。系统提示继续禁止模型在成功结果前声称操作完成。

- [ ] **步骤 6：运行后端与桌面契约测试**

```bash
python3 -m unittest backend.tests.test_tool_service \
  backend.tests.test_model_tool_orchestrator backend.tests.test_sqlite_store -v
npm test --prefix desktop-app
```

预期：全部 PASS；确认 UI 继续只使用 `textContent`。

- [ ] **步骤 7：提交模型确认恢复**

```bash
git add -- backend/domain/tools.py backend/tools/registry.py backend/tools/service.py \
  backend/application/model_tools.py backend/infrastructure/sqlite_store.py \
  backend/tests/test_tool_service.py backend/tests/test_model_tool_orchestrator.py \
  backend/tests/test_sqlite_store.py desktop-app/tests/tool-confirmation-contract.test.js
git commit -m "feat: resume desktop actions after confirmation"
```

## 阶段 C：QQ 只读状态桥接

### 任务 8：实现云端最新快照 API 与存储

**文件：**
- 修改：`backend/computer/state.py`
- 创建：`backend/api/computer.py`
- 修改：`backend/api/app.py`
- 创建：`backend/tests/test_computer_report_api.py`
- 修改：`backend/core/deployment.py`
- 修改：`backend/core/runtime.py`

- [ ] **步骤 1：编写失败的上报 API 测试**

覆盖正确 Bearer Token、错误 Token、32 KiB 上限、版本、设备 ID、UTC 时间窗口、旧快照拒绝、最新替换、45 秒离线和响应脱敏。

- [ ] **步骤 2：运行测试确认 API 不存在**

运行：`python3 -m unittest backend.tests.test_computer_report_api -v`

预期：FAIL 或 HTTP 404。

- [ ] **步骤 3：实现只在 cloud profile 启用的上报路由**

路由使用常量时间比较 Bearer Token。`RemoteDeviceStateStore.put()` 原子比较 `collected_at`，只保留每台设备最新模型；日志只记录设备 ID 和稳定状态，不记录正文。

- [ ] **步骤 4：让云端状态工具读取默认设备**

Runtime 在 cloud profile 组装 `RemoteDeviceStateStore`，QQ 的 `computer.current_state` 读取默认设备；设备缺失或过期返回 `device_offline`。

- [ ] **步骤 5：运行 API、Runtime 和 OneBot 回归**

```bash
python3 -m unittest backend.tests.test_computer_report_api \
  backend.tests.test_runtime backend.tests.test_onebot_api -v
```

预期：全部 PASS，QQ 目录只有只读状态能力。

- [ ] **步骤 6：提交云端状态接收**

```bash
git add -- backend/computer/state.py backend/api/computer.py backend/api/app.py \
  backend/core/deployment.py backend/core/runtime.py \
  backend/tests/test_computer_report_api.py
git commit -m "feat: receive current computer state in cloud"
```

### 任务 9：实现受限 SSH 隧道上报器

**文件：**
- 创建：`backend/computer/reporter.py`
- 测试：`backend/tests/test_computer_reporter.py`
- 修改：`backend/core/runtime.py`
- 修改：`backend/api/app.py`

- [ ] **步骤 1：编写失败的 Reporter 测试**

注入 SSH 进程工厂和 HTTP 客户端，验证严格 SSH 参数、15 秒心跳、旧快照不重复发送、指数退避上限、关闭清理和秘密不进日志。

- [ ] **步骤 2：运行测试确认 Reporter 不存在**

运行：`python3 -m unittest backend.tests.test_computer_reporter -v`

预期：FAIL，模块不存在。

- [ ] **步骤 3：实现严格隧道与上报循环**

SSH 参数必须包含：

```python
(
    "/usr/bin/ssh", "-N",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", f"UserKnownHostsFile={known_hosts}",
    "-i", str(identity_file),
    "-p", str(port),
    "-L", f"127.0.0.1:{local_port}:127.0.0.1:8080",
    target,
)
```

Reporter 只提交 `ComputerSnapshot.model_dump(mode="json")`，Bearer Token 只放请求头。关闭时终止 SSH 子进程并等待；失败退避最大 60 秒。

- [ ] **步骤 4：运行 Reporter 与禁止项扫描**

```bash
python3 -m unittest backend.tests.test_computer_reporter -v
rg -n "StrictHostKeyChecking=no|ssh-keyscan|printenv|env \|" backend/computer/reporter.py
```

预期：测试 PASS；扫描无命中。

- [ ] **步骤 5：提交本机上报器**

```bash
git add -- backend/computer/reporter.py backend/core/runtime.py backend/api/app.py \
  backend/tests/test_computer_reporter.py
git commit -m "feat: report computer state through strict SSH"
```

### 任务 10：提供专用 SSH 访问安装契约

**文件：**
- 创建：`deploy/cloud/scripts/install-state-relay-access.sh`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`
- 修改：`deploy/cloud/.env.example`
- 修改：`deploy/cloud/secrets.env.example`

- [ ] **步骤 1：编写失败的云端安全契约**

断言专用用户或密钥只允许 `permitopen="127.0.0.1:8080"`，强制固定长运行命令，禁止 PTY、Agent/X11 Forwarding，并且 Compose 不公开新端口。秘密模板只增加空槽 `ASSISTANT_COMPUTER_STATE_REPORT_TOKEN=`。

- [ ] **步骤 2：运行云端契约确认失败**

运行：`python3 -m unittest deploy.cloud.tests.test_cloud_contract -v`

预期：FAIL，安装器和新配置不存在。

- [ ] **步骤 3：实现幂等安装器**

脚本要求 root、读取一个已审查的 ed25519 公钥文件，并写入独立账号的 `authorized_keys`。固定选项包含：

```text
command="/usr/bin/sleep infinity",restrict,port-forwarding,permitopen="127.0.0.1:8080"
```

脚本拒绝包含换行、选项前缀或非 `ssh-ed25519` 的公钥；不修改全局 `sshd_config`、root 登录、密码登录、Nginx 或防火墙。

- [ ] **步骤 4：运行契约与 Shell 语法检查**

```bash
python3 -m unittest deploy.cloud.tests.test_cloud_contract -v
bash -n deploy/cloud/scripts/install-state-relay-access.sh
```

预期：全部 PASS。

- [ ] **步骤 5：提交访问契约**

```bash
git add -- deploy/cloud/scripts/install-state-relay-access.sh \
  deploy/cloud/tests/test_cloud_contract.py deploy/cloud/.env.example \
  deploy/cloud/secrets.env.example
git commit -m "ops: restrict computer state relay access"
```

## 阶段 D：配置、文档与最终验证

### 任务 11：增加 3 个独立设置开关

**文件：**
- 修改：`backend/settings/models.py`
- 修改：`backend/settings/resolver.py`
- 修改：`backend/settings/validation.py`
- 修改：`backend/settings/service.py`
- 修改：`backend/settings/static/index.html`
- 修改：`backend/settings/static/settings.js`
- 修改：`backend/tests/test_settings_api.py`
- 修改：`desktop-app/tests/settings-page-contract.test.js`

- [ ] **步骤 1：编写失败的设置契约**

新增 `computer.stateEnabled`、`computer.actionsEnabled`、`computer.remoteReportEnabled`，默认均为 false；后两个要求前一个为 true。云端 Profile 只允许状态接收配置，不显示本机操作开关。保存结果保持 `restartRequired=true`。

- [ ] **步骤 2：运行设置测试确认失败**

```bash
python3 -m unittest backend.tests.test_settings_api -v
node --test desktop-app/tests/settings-page-contract.test.js
```

预期：FAIL，新设置节不存在。

- [ ] **步骤 3：实现持久模型、解析、校验和页面**

增加 `ComputerSettings` 和 `ComputerRuntimeSettings`。环境变量优先级遵循现有规则：

```text
ASSISTANT_COMPUTER_STATE_ENABLED
ASSISTANT_COMPUTER_ACTIONS_ENABLED
ASSISTANT_COMPUTER_REMOTE_REPORT_ENABLED
```

`ComputerRuntimeSettings` 还从环境读取并严格校验 `ASSISTANT_COMPUTER_DEVICE_ID`、
`ASSISTANT_COMPUTER_RELAY_TARGET`、`ASSISTANT_COMPUTER_RELAY_PORT`、
`ASSISTANT_COMPUTER_RELAY_IDENTITY_FILE`、
`ASSISTANT_COMPUTER_RELAY_KNOWN_HOSTS_FILE`、
`ASSISTANT_COMPUTER_RELAY_LOCAL_PORT` 和 `ASSISTANT_COMPUTER_STATE_REPORT_TOKEN`。
这些值不写入普通设置 JSON；Token 只来自 Keychain 或环境，路径和目标只在响应中显示
“已配置”，不返回原值。

设置页必须说明采集字段、明确不采集的内容、macOS 辅助功能权限和 QQ 可见范围。所有状态使用 `textContent`，不展示 SSH 路径或 Token 内容。

- [ ] **步骤 4：运行设置与桌面测试**

```bash
python3 -m unittest backend.tests.test_settings_api -v
npm test --prefix desktop-app
```

预期：全部 PASS。

- [ ] **步骤 5：提交设置界面**

```bash
git add -- backend/settings/models.py backend/settings/resolver.py \
  backend/settings/validation.py backend/settings/service.py \
  backend/settings/static/index.html backend/settings/static/settings.js \
  backend/tests/test_settings_api.py \
  desktop-app/tests/settings-page-contract.test.js
git commit -m "feat: configure computer capabilities safely"
```

### 任务 12：补齐文档并完成全量验收

**文件：**
- 修改：`README.md`
- 修改：`docs/deployment/cloud-qq-assistant.md`
- 修改：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：增加失败的文档契约**

要求文档包含：实时不留历史、隐私四级、QQ 只读、45 秒离线、专用 SSH 密钥、逐次确认、无任意 Shell、关闭流程和密钥撤销。

- [ ] **步骤 2：更新本地与云端操作说明**

说明启用顺序、macOS 权限、状态字段、设置开关、隧道诊断、QQ 查询、停止上报和撤销专用公钥。创建或安装持久 SSH 凭证必须在实际执行前重新取得用户确认。

- [ ] **步骤 3：运行全部定向测试**

```bash
python3 -m unittest discover -s backend/tests -p 'test_computer*.py' -v
python3 -m unittest deploy.cloud.tests.test_cloud_contract -v
for script in deploy/cloud/scripts/*.sh; do bash -n "$script"; done
```

预期：全部 PASS。

- [ ] **步骤 4：运行完整回归与安全扫描**

```bash
python3 -m unittest discover -s backend/tests -p 'test_*.py'
npm test --prefix desktop-app
python3 -m unittest discover -s deploy/cloud/tests -p 'test_*.py' -v
rg -n "shell=True|create_subprocess_shell|os\.system|StrictHostKeyChecking=no|ssh-keyscan|printenv|env \|" \
  backend/computer deploy/cloud/scripts/install-state-relay-access.sh
git diff --check
```

预期：测试无失败；安全扫描无命中；差异检查无输出。

- [ ] **步骤 5：提交文档与验收记录**

```bash
git add -- README.md docs/deployment/cloud-qq-assistant.md \
  deploy/cloud/tests/test_cloud_contract.py
git commit -m "docs: explain computer state assistant operations"
```

- [ ] **步骤 6：检查提交序列和工作区**

```bash
git status --short --branch
git log --oneline origin/main..HEAD
```

预期：工作区干净；设计、计划和任务 1～12 的独立提交均位于
`codex/macos-state-assistant`。

## 合并后的人工验收

1. 合并前只运行本地状态采集，不创建持久 SSH 凭证。
2. 用户明确授权后，在 Mac 与服务器创建专用状态上报密钥和 Token。
3. 确认专用 SSH 身份不能执行远程命令，只能转发到服务器回环 8080。
4. 确认 Desktop 能查询脱敏状态并逐次确认 4 个固定操作。
5. 确认 QQ 能查询最新状态，但完全看不到操作工具。
6. 确认 Mac 断网或停止上报后 45 秒内显示离线。
7. 确认浏览器、终端和私密应用的原始标题不出现在 API、日志、SQLite 或模型上下文。
8. 确认 8080、6099 只绑定回环，3000、3001 未监听，网站和博客不受影响。
