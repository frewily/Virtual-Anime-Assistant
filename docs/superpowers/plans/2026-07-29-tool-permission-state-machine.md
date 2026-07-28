# 工具权限与确认状态机实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 建立本地计算风险、低风险自动执行、高风险逐次确认，并支持超时、取消、SQLite 审计和 Electron 确认界面的工具安全边界。

**架构：** 领域模型和策略保持纯 Python，`ToolExecutionService` 编排注册、确认和执行，`SqliteStore` 通过第 2 版迁移提供原子状态转换。FastAPI 暴露本机 API 并通过现有 WebSocket 通知 Electron，Electron 只展示脱敏确认信息并回传一次性决定。

**技术栈：** Python 3.11+、Pydantic 2、FastAPI、SQLite、`asyncio`、Electron、原生 JavaScript、Node.js Test Runner。

---

## 文件结构

### 后端领域与应用

- 创建 `backend/domain/tools.py`：风险、请求状态、确认状态、请求记录、确认记录、结果和审计模型。
- 创建 `backend/tools/__init__.py`：工具包边界。
- 创建 `backend/tools/registry.py`：工具定义和代码内注册表。
- 创建 `backend/tools/policy.py`：风险读取和参数脱敏摘要。
- 创建 `backend/tools/repositories.py`：工具状态仓储协议。
- 创建 `backend/tools/service.py`：请求、确认、执行、超时和取消编排。
- 创建 `backend/tools/builtin.py`：`system.current_time` 低风险工具。

### 持久化与运行时

- 修改 `backend/infrastructure/sqlite_store.py`：第 2 版迁移和工具仓储方法。
- 修改 `backend/core/runtime.py`：构建生产注册表和工具服务。

### API 与桌面事件

- 创建 `backend/api/tools.py`：请求、查询、确认和取消接口。
- 修改 `backend/api/app.py`：注册工具路由和工具事件订阅。
- 修改 `backend/api/ws.py`：增加安全 JSON 广播函数。
- 修改 `desktop-app/src/renderer/js/api.js`：确认列表和决定 API。
- 创建 `desktop-app/src/renderer/js/tool-confirmation.js`：确认队列与交互。
- 修改 `desktop-app/src/renderer/js/websocket.js`：分发确认事件并在重连后补齐。
- 修改 `desktop-app/src/renderer/js/renderer.js`：加载确认模块。
- 修改 `desktop-app/src/renderer/index.html`：确认卡片结构。
- 修改 `desktop-app/src/renderer/styles/main.css`：确认卡片样式。

### 测试与文档

- 创建 `backend/tests/test_tool_domain_policy.py`：模型、注册和脱敏策略。
- 创建 `backend/tests/test_tool_service.py`：状态机全部路径。
- 修改 `backend/tests/test_sqlite_store.py`：迁移、原子状态和审计。
- 修改 `backend/tests/test_integration.py`：HTTP 和 WebSocket 集成。
- 创建 `desktop-app/tests/tool-confirmation-contract.test.js`：桌面确认契约。
- 修改 `README.md`：记录当前工具能力和安全边界。

## 任务 1：领域模型、注册表与风险策略

**文件：**

- 创建：`backend/domain/tools.py`
- 创建：`backend/tools/__init__.py`
- 创建：`backend/tools/registry.py`
- 创建：`backend/tools/policy.py`
- 测试：`backend/tests/test_tool_domain_policy.py`

- [x] **步骤 1：编写失败的领域和策略测试**

```python
class SecretArguments(BaseModel):
    target: str
    token: str


async def handler(arguments: SecretArguments) -> dict:
    return {"target": arguments.target}


def test_registry_rejects_duplicate_names_and_request_cannot_override_risk():
    registry = ToolRegistry()
    definition = ToolDefinition(
        name="example.read",
        title="读取示例",
        arguments_model=SecretArguments,
        risk=ToolRisk.LOW,
        impact="只读取示例状态",
        timeout_seconds=2,
        cancellable=True,
        sensitive_fields=frozenset({"token"}),
        handler=handler,
    )
    registry.register(definition)
    with pytest.raises(ValueError):
        registry.register(definition)

    request = ToolRequest(
        correlation_id="message-1",
        source=ToolSource.DESKTOP,
        tool_name="example.read",
        arguments={"target": "demo", "token": "private"},
    )
    assert not hasattr(request, "risk")
    assert ToolPolicy().risk_for(definition, request.arguments) is ToolRisk.LOW


def test_argument_summary_redacts_and_limits_untrusted_values():
    summary = summarize_arguments(
        {
            "target": "x" * 300,
            "token": "private",
            "nested": {"token": "also-private"},
        },
        frozenset({"token"}),
    )
    assert summary["token"] == "[REDACTED]"
    assert summary["nested"]["token"] == "[REDACTED]"
    assert len(summary["target"]) == 201
    assert summary["target"].endswith("…")
```

- [x] **步骤 2：运行测试并确认缺少模块**

运行：

```bash
python3 -m unittest backend.tests.test_tool_domain_policy -v
```

预期：失败并报告 `domain.tools` 或 `tools.registry` 不存在。

- [x] **步骤 3：实现最小领域模型**

`backend/domain/tools.py` 至少包含：

```python
class ToolRisk(str, Enum):
    LOW = "low"
    HIGH = "high"


class ToolRequestState(str, Enum):
    CREATED = "created"
    PENDING_CONFIRMATION = "pending_confirmation"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ConfirmationState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ToolDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ToolSource(str, Enum):
    DESKTOP = "desktop"
    MODEL = "model"
    QQ = "qq"
    SYSTEM = "system"


class ToolRequest(BaseModel):
    model_config = {"frozen": True}
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    correlation_id: str = Field(min_length=1, max_length=200)
    source: ToolSource
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
```

同时定义 `ToolRequestRecord`、`ToolConfirmationRecord`、`ToolExecutionResult`、`ToolAuditEvent` 和对外 `ToolRequestView`，所有时间必须带时区。

- [x] **步骤 4：实现注册表和脱敏策略**

`backend/tools/registry.py`：

```python
ToolHandler = Callable[[BaseModel], Awaitable[dict[str, Any]]]


class ToolNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    title: str
    arguments_model: type[BaseModel]
    risk: ToolRisk
    impact: str
    timeout_seconds: float
    cancellable: bool
    sensitive_fields: frozenset[str]
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"duplicate tool: {definition.name}")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def list(self) -> Sequence[ToolDefinition]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))
```

`backend/tools/policy.py`：

```python
class ToolPolicy:
    def risk_for(
        self,
        definition: ToolDefinition,
        _: Mapping[str, Any],
    ) -> ToolRisk:
        return definition.risk


def summarize_arguments(
    arguments: Mapping[str, Any],
    sensitive_fields: frozenset[str],
) -> dict[str, Any]:
    sensitive = {field.casefold() for field in sensitive_fields}
    return {
        str(key): (
            "[REDACTED]"
            if str(key).casefold() in sensitive
            else _summarize_value(value, sensitive, depth=1)
        )
        for key, value in list(arguments.items())[:20]
    }
```

摘要递归最多 4 层，每层最多 20 项，字符串最多 200 个字符，敏感键不区分大小写。

- [x] **步骤 5：运行测试并确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_tool_domain_policy -v
```

预期：所有领域、注册和脱敏测试通过。

- [x] **步骤 6：提交领域边界**

```bash
git add backend/domain/tools.py backend/tools backend/tests/test_tool_domain_policy.py
git commit -m "feat: 建立工具领域模型与风险策略"
```

## 任务 2：工具执行与确认状态机

**文件：**

- 创建：`backend/tools/repositories.py`
- 创建：`backend/tools/service.py`
- 测试：`backend/tests/test_tool_service.py`

- [x] **步骤 1：编写失败的状态机测试**

使用测试内 `InMemoryToolRepository` 和可计数处理函数覆盖：

```python
async def test_low_risk_executes_automatically():
    result = await service.request(low_request)
    assert result.state is ToolRequestState.SUCCEEDED
    assert calls == 1


async def test_high_risk_waits_for_one_approval():
    pending = await service.request(high_request)
    assert pending.state is ToolRequestState.PENDING_CONFIRMATION
    assert calls == 0

    first, second = await asyncio.gather(
        service.decide(pending.confirmation.id, ToolDecision.APPROVE),
        service.decide(pending.confirmation.id, ToolDecision.APPROVE),
    )
    assert calls == 1
    assert {first.state, second.state} == {ToolRequestState.RUNNING, ToolRequestState.SUCCEEDED} or (
        first.state is ToolRequestState.SUCCEEDED
        and second.state is ToolRequestState.SUCCEEDED
    )


async def test_reject_expire_cancel_timeout_and_failure_never_leak_errors():
    rejected = await service.decide(
        rejected_confirmation.id,
        ToolDecision.REJECT,
    )
    assert rejected.state is ToolRequestState.REJECTED
    assert calls == 0

    expired = await expired_service.decide(
        expired_confirmation.id,
        ToolDecision.APPROVE,
    )
    assert expired.state is ToolRequestState.EXPIRED
    assert expired_calls == 0

    cancelled = await service.cancel(cancel_confirmation.request_id)
    assert cancelled.state is ToolRequestState.CANCELLED
    assert calls == 0

    timed_out = await timeout_service.request(timeout_request)
    assert timed_out.state is ToolRequestState.FAILED
    assert timed_out.error_code == "execution_timeout"

    failed = await failing_service.request(failing_request)
    assert failed.state is ToolRequestState.FAILED
    assert failed.error_code == "execution_failed"
    assert "private exception body" not in failed.model_dump_json()
```

测试必须明确断言拒绝、过期和待确认取消时处理函数调用次数为 0；不可取消的运行请求返回 `ToolStateConflictError`；异常结果不包含原始异常文本。

- [x] **步骤 2：运行测试并确认服务缺失**

运行：

```bash
python3 -m unittest backend.tests.test_tool_service -v
```

预期：失败并报告 `tools.service` 不存在。

- [x] **步骤 3：定义仓储协议和稳定错误**

`backend/tools/repositories.py`：

```python
class ToolRepository(Protocol):
    async def create_request(self, record: ToolRequestRecord) -> None:
        pass

    async def create_confirmation(
        self,
        request: ToolRequestRecord,
        confirmation: ToolConfirmationRecord,
        events: Sequence[ToolAuditEvent],
    ) -> None:
        pass

    async def claim_decision(
        self,
        confirmation_id: str,
        decision: ToolDecision,
        now: datetime,
    ) -> ToolRequestRecord | None:
        pass

    async def transition_request(
        self,
        request_id: str,
        expected: set[ToolRequestState],
        state: ToolRequestState,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        event: ToolAuditEvent,
    ) -> ToolRequestRecord | None:
        pass

    async def get_request(
        self,
        request_id: str,
    ) -> ToolRequestRecord | None:
        pass

    async def get_confirmation(
        self,
        confirmation_id: str,
    ) -> ToolConfirmationRecord | None:
        pass

    async def list_pending_confirmations(
        self,
        now: datetime,
    ) -> list[ToolConfirmationRecord]:
        pass
```

`backend/tools/service.py` 从注册表导入 `ToolNotFoundError`，并定义 `ToolArgumentsError`、`ToolStateConflictError` 和 `ToolExecutionService`。

- [x] **步骤 4：实现请求、决定与执行**

服务构造函数：

```python
class ToolExecutionService:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        repository: ToolRepository,
        policy: ToolPolicy | None = None,
        confirmation_timeout_seconds: float = 60,
        publisher: ToolEventPublisher | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.registry = registry
        self.repository = repository
        self.policy = policy or ToolPolicy()
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self.publisher = publisher or ToolEventPublisher()
        self.clock = clock
        self._pending_arguments: dict[str, BaseModel] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
```

关键实现要求：

- `request()` 先校验再持久化，数据库失败时不执行。
- 高风险完整参数保存在 `_pending_arguments`，不写 SQLite。
- `decide()` 依赖仓储原子认领，只有认领者调用 `_execute()`。
- `_execute()` 使用 `asyncio.timeout(definition.timeout_seconds)`。
- `_running_tasks` 只保存可取消的当前任务。
- `cancel()` 对待确认请求转换终态，对运行任务执行取消。
- 所有状态变化附带 `ToolAuditEvent`。

- [x] **步骤 5：运行状态机测试并确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_tool_service -v
```

预期：低风险、高风险、并发批准、拒绝、过期、取消、超时和异常测试全部通过。

- [x] **步骤 6：提交应用状态机**

```bash
git add backend/tools/repositories.py backend/tools/service.py backend/tests/test_tool_service.py
git commit -m "feat: 实现工具确认与执行状态机"
```

## 任务 3：SQLite 第 2 版迁移与原子仓储

**文件：**

- 修改：`backend/infrastructure/sqlite_store.py`
- 修改：`backend/tests/test_sqlite_store.py`

- [x] **步骤 1：扩展失败的迁移和仓储测试**

将期望表增加：

```python
EXPECTED_TABLES |= {
    "tool_requests",
    "tool_confirmations",
    "tool_audit_events",
}
```

增加以下测试。版本升级测试先用版本 1 的建表语句创建会话数据，再打开 `SqliteStore`：

```python
def test_version_one_database_upgrades_to_version_two_without_data_loss():
    create_version_one_database(self.database_path)
    store = self.open_store()

    self.assertEqual(store.schema_version, 2)
    with sqlite3.connect(self.database_path) as connection:
        row = connection.execute(
            "SELECT source, owner_id FROM conversations WHERE id = ?",
            ("conversation-before-upgrade",),
        ).fetchone()
    self.assertEqual(row, ("desktop", "local-user"))


def test_concurrent_confirmation_claim_has_one_winner():
    store = self.open_store()
    request, confirmation, events = high_risk_records()
    asyncio.run(store.create_confirmation(request, confirmation, events))

    async def claim_twice():
        return await asyncio.gather(
            store.claim_decision(
                confirmation.id,
                ToolDecision.APPROVE,
                utc_now(),
            ),
            store.claim_decision(
                confirmation.id,
                ToolDecision.APPROVE,
                utc_now(),
            ),
        )

    winners = [item for item in asyncio.run(claim_twice()) if item is not None]
    self.assertEqual(len(winners), 1)
    self.assertEqual(winners[0].state, ToolRequestState.RUNNING)
```

其余测试使用同一组 `high_risk_records()`，分别断言高风险请求与确认记录同时存在、审计 JSON 中敏感字段等于 `"[REDACTED]"`，以及过期确认在列表查询后转换为 `expired`。

- [x] **步骤 2：运行 SQLite 测试并确认版本仍为 1**

运行：

```bash
python3 -m unittest backend.tests.test_sqlite_store -v
```

预期：新表、版本 2 和工具仓储方法相关测试失败。

- [x] **步骤 3：增加第 2 版迁移**

在 `sqlite_store.py` 增加 `_MIGRATION_2_STATEMENTS`，创建 3 张表和以下索引：

```sql
CREATE INDEX idx_tool_requests_correlation
ON tool_requests(correlation_id);

CREATE INDEX idx_tool_requests_state_created
ON tool_requests(state, created_at);

CREATE INDEX idx_tool_confirmations_state_expires
ON tool_confirmations(state, expires_at);

CREATE INDEX idx_tool_audit_request_created
ON tool_audit_events(request_id, created_at);
```

`_apply_migrations()` 按版本顺序分别使用 `BEGIN IMMEDIATE` 应用未执行迁移，并写入：

```text
1 initial_schema
2 tool_permissions
```

- [x] **步骤 4：实现异步仓储方法**

公开异步方法通过 `asyncio.to_thread()` 调用同步实现。以下操作必须使用单个事务：

- `create_confirmation()`：请求、确认和初始审计一起保存。
- `claim_decision()`：检查过期、转换确认、转换请求、写审计。
- `transition_request()`：比较旧状态、写结果和审计。

JSON 使用 `json.dumps(payload, ensure_ascii=False, sort_keys=True)`，读取时只接受 JSON 对象。

- [x] **步骤 5：运行 SQLite 与全量后端测试**

运行：

```bash
python3 -m unittest backend.tests.test_sqlite_store -v
python3 -m unittest discover -s backend/tests -v
```

预期：SQLite 测试和原有后端测试全部通过。

- [x] **步骤 6：提交持久化迁移**

```bash
git add backend/infrastructure/sqlite_store.py backend/tests/test_sqlite_store.py
git commit -m "feat: 持久化工具确认与审计记录"
```

## 任务 4：内置低风险工具与运行时装配

**文件：**

- 创建：`backend/tools/builtin.py`
- 修改：`backend/core/runtime.py`
- 修改：`backend/tests/test_runtime.py`
- 创建：`backend/tests/test_builtin_tools.py`

- [x] **步骤 1：编写失败的时间工具和运行时测试**

```python
def test_current_time_defaults_to_local_timezone_and_accepts_iana_zone():
    utc = asyncio.run(current_time(CurrentTimeArguments(timezone="UTC")))
    assert utc["timezone"] == "UTC"
    assert utc["iso"].endswith("+00:00")


def test_runtime_registers_only_approved_builtin_tools():
    runtime = AssistantRuntime(store=fake_store)
    assert [item.name for item in runtime.tool_registry.list()] == [
        "system.current_time"
    ]
```

同时测试无效时区返回 `invalid_timezone`，生产注册表没有高风险示例工具。

- [x] **步骤 2：运行测试并确认内置工具缺失**

运行：

```bash
python3 -m unittest backend.tests.test_builtin_tools backend.tests.test_runtime -v
```

预期：失败并报告 `tools.builtin` 或 `runtime.tool_service` 缺失。

- [x] **步骤 3：实现并注册时间工具**

```python
class CurrentTimeArguments(BaseModel):
    timezone: str | None = Field(default=None, max_length=100)


async def current_time(arguments: CurrentTimeArguments) -> dict[str, str]:
    zone = (
        ZoneInfo(arguments.timezone)
        if arguments.timezone
        else datetime.now().astimezone().tzinfo
    )
    now = datetime.now(zone)
    return {"iso": now.isoformat(), "timezone": str(zone)}
```

`build_builtin_registry()` 只注册 `system.current_time`，风险为 `LOW`，超时为 2 秒。

- [x] **步骤 4：装配运行时**

`AssistantRuntime` 接受可选 `tool_registry` 和 `tool_service`。默认使用当前 `SqliteStore`、`ToolPolicy`、生产注册表和 `ToolExecutionService`。显式注入 `application` 的现有测试不得因此构造新数据库。

- [x] **步骤 5：运行相关测试并提交**

```bash
python3 -m unittest backend.tests.test_builtin_tools backend.tests.test_runtime -v
git add backend/tools/builtin.py backend/core/runtime.py backend/tests/test_builtin_tools.py backend/tests/test_runtime.py
git commit -m "feat: 注册只读系统时间工具"
```

## 任务 5：FastAPI 工具接口与 WebSocket 通知

**文件：**

- 创建：`backend/api/tools.py`
- 修改：`backend/api/app.py`
- 修改：`backend/api/ws.py`
- 修改：`backend/tests/test_integration.py`
- 修改：`backend/tests/test_api.py`

- [ ] **步骤 1：编写失败的 HTTP 和 WebSocket 集成测试**

```python
def test_low_risk_tool_completes_without_confirmation():
    response = client.post(
        "/api/tools/requests",
        json={
            "tool": "system.current_time",
            "arguments": {"timezone": "UTC"},
            "correlationId": "manual-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "succeeded"


def test_high_risk_tool_broadcasts_and_waits_for_approval():
    with client.websocket_connect("/ws/avatar") as websocket:
        response = client.post("/api/tools/requests", json=high_payload)
        event = websocket.receive_json()
    assert response.status_code == 202
    assert event["type"] == "tool_confirmation_required"
    assert calls == 0
```

继续覆盖批准、拒绝、待确认列表、取消、未知工具和非法参数的安全响应。

- [ ] **步骤 2：运行集成测试并确认路由不存在**

运行：

```bash
python3 -m unittest backend.tests.test_integration backend.tests.test_api -v
```

预期：工具接口返回 404 或缺少广播函数。

- [ ] **步骤 3：实现安全请求与响应模型**

`backend/api/tools.py`：

```python
class ToolRequestBody(BaseModel):
    model_config = {"populate_by_name": True}
    tool: str = Field(min_length=3, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(
        default=None,
        alias="correlationId",
        min_length=1,
        max_length=200,
    )


class ToolDecisionBody(BaseModel):
    decision: ToolDecision
```

路由将领域错误映射为 404、422 和 409，不返回异常文本。

- [ ] **步骤 4：增加通用 WebSocket JSON 广播**

`backend/api/ws.py`：

```python
async def broadcast_json(payload: dict) -> None:
    disconnected = []
    for ws in tuple(_sessions):
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except (RuntimeError, WebSocketDisconnect):
            disconnected.append(ws)
    for ws in disconnected:
        _sessions.discard(ws)
```

`broadcast_to_desktop()` 复用该函数。`api.app.lifespan()` 订阅工具事件并转换为 `tool_confirmation_required` JSON。

- [ ] **步骤 5：运行 API、集成和全量测试**

```bash
python3 -m unittest backend.tests.test_integration backend.tests.test_api -v
python3 -m unittest discover -s backend/tests -v
```

预期：新增接口与所有原有测试通过。

- [ ] **步骤 6：提交 API 接缝**

```bash
git add backend/api/tools.py backend/api/app.py backend/api/ws.py backend/tests/test_integration.py backend/tests/test_api.py
git commit -m "feat: 暴露工具确认与取消接口"
```

## 任务 6：Electron 确认队列界面

**文件：**

- 修改：`desktop-app/src/renderer/js/api.js`
- 创建：`desktop-app/src/renderer/js/tool-confirmation.js`
- 修改：`desktop-app/src/renderer/js/websocket.js`
- 修改：`desktop-app/src/renderer/js/renderer.js`
- 修改：`desktop-app/src/renderer/index.html`
- 修改：`desktop-app/src/renderer/styles/main.css`
- 创建：`desktop-app/tests/tool-confirmation-contract.test.js`

- [ ] **步骤 1：编写失败的桌面契约测试**

```javascript
test('renderer loads the confirmation queue and exposes safe controls', () => {
    const renderer = read('src/renderer/js/renderer.js');
    const moduleSource = read('src/renderer/js/tool-confirmation.js');
    const html = read('src/renderer/index.html');

    assert.match(renderer, /require\\('\\.\\/tool-confirmation'\\)/);
    assert.match(moduleSource, /textContent/);
    assert.doesNotMatch(moduleSource, /innerHTML/);
    assert.match(html, /id="tool-confirmation"/);
    assert.match(html, /data-decision="approve"/);
    assert.match(html, /data-decision="reject"/);
});


test('desktop API uses the exact tool confirmation endpoints', () => {
    const source = read('src/renderer/js/api.js');
    assert.match(source, /\\/tools\\/confirmations/);
    assert.match(source, /\\/decision/);
});
```

- [ ] **步骤 2：运行测试并确认模块缺失**

运行：

```bash
cd desktop-app
npm run test:unit
```

预期：确认模块、DOM 和 API 契约测试失败。

- [ ] **步骤 3：实现 API 和队列**

`api.js` 增加：

```javascript
async function requestJson(path, options = {}) {
    const response = await fetch(`${API_BASE}${path}`, options);
    const payload = response.status === 204 ? null : await response.json();
    if (!response.ok) {
        throw new Error(payload?.detail?.code || 'request_failed');
    }
    return payload;
}

async function listToolConfirmations() {
    return requestJson('/tools/confirmations');
}

async function decideToolConfirmation(id, decision) {
    return requestJson(`/tools/confirmations/${encodeURIComponent(id)}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision })
    });
}
```

`tool-confirmation.js` 导出：

```javascript
const queue = [];
const queuedIds = new Set();

function enqueueConfirmation(confirmation) {
    if (!confirmation?.id || queuedIds.has(confirmation.id)) return false;
    queuedIds.add(confirmation.id);
    queue.push(confirmation);
    renderNextConfirmation();
    return true;
}

async function restorePendingConfirmations() {
    const confirmations = await listToolConfirmations();
    confirmations.forEach(enqueueConfirmation);
}

function handleConfirmationUpdate(update) {
    if (!update?.confirmationId) return;
    removeConfirmation(update.confirmationId);
    renderNextConfirmation();
}
```

队列以确认 ID 去重，所有外部字段通过 `textContent` 设置。提交时禁用按钮，失败时恢复按钮并显示“无法提交决定，请稍后重试”。

- [ ] **步骤 4：接入 WebSocket 和 DOM**

`websocket.js`：

```javascript
case 'tool_confirmation_required':
    enqueueConfirmation(message.confirmation);
    break;
case 'tool_confirmation_updated':
    handleConfirmationUpdate(message);
    break;
```

WebSocket `onopen` 调用 `restorePendingConfirmations()`。HTML 使用原生按钮和 `aria-live="polite"`，样式确保卡片可点击且不属于拖动区域。

- [ ] **步骤 5：运行桌面测试和构建**

```bash
cd desktop-app
npm test
```

预期：Node.js 单元测试、renderer 构建和语法检查全部通过。

- [ ] **步骤 6：提交桌面确认界面**

```bash
git add desktop-app/src/renderer desktop-app/tests/tool-confirmation-contract.test.js
git commit -m "feat: 增加桌面工具确认界面"
```

## 任务 7：文档、完整验证与发布

**文件：**

- 修改：`README.md`
- 修改：`docs/superpowers/plans/2026-07-29-tool-permission-state-machine.md`

- [ ] **步骤 1：更新 README**

说明：

- 当前生产工具只有 `system.current_time`。
- 低风险自动执行，高风险逐次确认。
- 不提供任意 Shell、文件删除、键盘输入或 QQ 发送。
- 工具请求、确认和审计保存在 SQLite，敏感参数不会写入审计。
- API 只作为本机开发接缝，后续模型和 QQ 通过应用服务接入。

- [ ] **步骤 2：运行完成前验证**

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -v
cd desktop-app
npm test
cd ..
git diff --check
git status --short
```

预期：所有命令退出码为 0；工作树只包含本阶段 README 和计划勾选变化。

- [ ] **步骤 3：执行手动开发验证**

启动后端和 Electron：

```bash
python3 backend/main.py
cd desktop-app
npm start
```

验证：

1. 调用 `system.current_time` 返回 `succeeded`，不出现确认卡片。
2. 注入测试高风险工具时，卡片显示名称、脱敏参数、影响和过期时间。
3. 点击拒绝后处理函数未执行。
4. 点击允许一次后只执行 1 次。
5. Electron 重连后能恢复未过期确认。

- [ ] **步骤 4：提交文档**

```bash
git add README.md docs/superpowers/plans/2026-07-29-tool-permission-state-machine.md
git commit -m "docs: 记录工具权限与确认流程"
```

- [ ] **步骤 5：推送并创建草稿 PR**

```bash
git push -u origin codex/action-policy
gh pr create --draft --base main --head codex/action-policy \
  --title "实现工具权限与确认状态机" \
  --body-file /private/tmp/virtual-anime-assistant-action-policy-pr.md
```

PR 描述必须列出状态机、SQLite 迁移、API、Electron 确认 UI、测试数量和仍未实现的真实电脑控制能力。
