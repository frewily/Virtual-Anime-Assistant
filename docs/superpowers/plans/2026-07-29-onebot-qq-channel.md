# OneBot 11 QQ 渠道实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 通过 OneBot 11 反向 WebSocket 接入 NapCat，实现带鉴权、独立白名单、群聊 `@` 触发、限速、幂等和可靠响应关联的 QQ 私聊与群聊文字渠道。

**架构：** QQ 适配器作为 FastAPI 进程内的独立渠道，依次完成协议解析、准入策略、统一消息转换和回复转换。它只调用 `AssistantApplication.process()`，不直接访问模型、记忆、工具或 Electron 广播；OneBot 连接管理器独立负责单连接、`echo` 关联、超时和断线清理。NapCat Docker 仅作为可选开发配套。

**技术栈：** Python 3.12、FastAPI、Pydantic 2、`asyncio`、OneBot 11、SQLite、PyYAML、Docker Compose、Python `unittest`。

---

## 文件结构

### OneBot 渠道

- 创建 `backend/channels/onebot/__init__.py`：导出渠道装配所需的稳定公开类型。
- 创建 `backend/channels/onebot/config.py`：解析 QQ 环境变量，保存安全默认值和配置状态。
- 创建 `backend/channels/onebot/models.py`：定义解析后的消息、动作和稳定错误。
- 创建 `backend/channels/onebot/parser.py`：把不可信 OneBot JSON 对象解析为受信任消息。
- 创建 `backend/channels/onebot/policy.py`：执行白名单、群聊触发、发送者限速和重复事件占用。
- 创建 `backend/channels/onebot/connection.py`：管理鉴权、单活动连接、`echo` 等待者、超时和断线。
- 创建 `backend/channels/onebot/channel.py`：转换统一消息与 QQ 回复，并编排应用服务。

### 应用、API 与运行时

- 修改 `backend/application/assistant.py`：拆分 `process()` 与 `handle()`，增加已处理消息查询。
- 创建 `backend/api/qq.py`：提供 `/ws/qq` 和 `/api/qq/status`。
- 修改 `backend/api/app.py`：注册 QQ 路由。
- 修改 `backend/core/runtime.py`：装配 QQ 配置、连接管理器和渠道，并在关闭时清理连接。

### 测试与配套

- 创建 `backend/tests/test_onebot_config_models.py`：配置、错误和动作模型测试。
- 创建 `backend/tests/test_onebot_parser_policy.py`：解析、白名单、触发、限速和重复事件测试。
- 修改 `backend/tests/test_application_foundation.py`：应用输出隔离和已处理消息测试。
- 创建 `backend/tests/test_onebot_connection.py`：鉴权、单连接、`echo`、超时和断线测试。
- 创建 `backend/tests/test_onebot_channel.py`：统一消息转换、幂等、回复分段和并发测试。
- 创建 `backend/tests/test_onebot_api.py`：状态 API、WebSocket 和运行时隔离测试。
- 创建 `backend/tests/test_onebot_docker_contract.py`：Compose 静态安全契约测试。
- 创建 `qq-bot/docker-compose.yml`：可选 NapCat 容器配置。
- 创建 `qq-bot/.env.example`：非敏感 Docker 示例变量。
- 创建 `qq-bot/README.md`：扫码、WebUI 和反向 WebSocket 配置说明。
- 修改 `.gitignore`：忽略 NapCat 登录与运行数据。
- 修改 `README.md`：记录 QQ 能力、环境变量、状态接口和本地验收流程。

## 统一类型约定

后续任务必须使用以下名称，避免渠道、API 和测试各自定义近似类型：

```python
class QQState(str, Enum):
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"


@dataclass(frozen=True, slots=True)
class OneBotSettings:
    enabled: bool
    access_token: str = field(repr=False)
    allowed_group_ids: frozenset[int]
    allowed_user_ids: frozenset[int]
    rate_per_minute: int
    rate_burst: int
    max_concurrency: int
    action_timeout_seconds: float
    configuration_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.enabled and self.configuration_error is None


@dataclass(frozen=True, slots=True)
class ParsedOneBotMessage:
    self_id: int
    user_id: int
    message_id: int
    message_type: Literal["private", "group"]
    group_id: int | None
    text: str
    mentioned_bot: bool

    @property
    def stable_message_id(self) -> str:
        return f"qq:{self.self_id}:{self.message_id}"

    @property
    def conversation_id(self) -> str:
        if self.message_type == "private":
            return f"qq:private:{self.user_id}"
        return f"qq:group:{self.group_id}:user:{self.user_id}"


class OneBotChannelError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
```

`OneBotChannelError` 的字符串形式只能包含稳定错误码，不能携带 Token、消息全文、OneBot 响应正文或 Cookie。
`OneBotSettings.from_env(environ: Mapping[str, str] | None = None) -> OneBotSettings`
是配置的唯一构造入口，详细行为见任务 1 步骤 3。

## 任务 1：配置、协议模型与安全错误

**文件：**

- 创建：`backend/channels/onebot/__init__.py`
- 创建：`backend/channels/onebot/config.py`
- 创建：`backend/channels/onebot/models.py`
- 测试：`backend/tests/test_onebot_config_models.py`

- [ ] **步骤 1：编写失败的配置和模型测试**

创建 `OneBotConfigTests`，测试方法名固定为：

- `test_channel_is_disabled_by_default`
- `test_enabled_configuration_parses_token_allowlists_and_limits`
- `test_group_and_user_allowlists_remain_independent`
- `test_enabled_channel_requires_a_16_character_token`
- `test_enabled_channel_requires_at_least_one_allowlist_entry`
- `test_invalid_boolean_id_and_numeric_ranges_become_misconfigured`
- `test_burst_cannot_exceed_per_minute_rate`
- `test_token_is_absent_from_repr_and_configuration_errors`
- `test_parsed_message_builds_stable_ids`
- `test_channel_error_only_exposes_a_stable_code`

有效配置断言：

```python
settings = OneBotSettings.from_env(
    {
        "ASSISTANT_QQ_ENABLED": "true",
        "ASSISTANT_QQ_ACCESS_TOKEN": "0123456789abcdef",
        "ASSISTANT_QQ_ALLOWED_GROUP_IDS": "10001, 10002",
        "ASSISTANT_QQ_ALLOWED_USER_IDS": "20001",
        "ASSISTANT_QQ_RATE_PER_MINUTE": "12",
        "ASSISTANT_QQ_RATE_BURST": "3",
        "ASSISTANT_QQ_MAX_CONCURRENCY": "5",
        "ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS": "8",
    }
)
self.assertTrue(settings.ready)
self.assertEqual(settings.allowed_group_ids, frozenset({10001, 10002}))
self.assertEqual(settings.allowed_user_ids, frozenset({20001}))
self.assertNotIn("0123456789abcdef", repr(settings))
```

- [ ] **步骤 2：运行测试并确认缺少模块**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_config_models -v
```

预期：失败并报告 `channels.onebot` 不存在。

- [ ] **步骤 3：实现不会阻断主应用启动的配置解析**

`OneBotSettings.from_env()` 必须满足：

- 未传 `environ` 时读取 `os.environ`，测试可传普通映射，不修改进程环境。
- 默认 `enabled=False`，其余字段使用设计文档中的默认值。
- 关闭状态忽略缺失 Token 和空白名单，但仍校验显式提供的数值。
- 启用状态要求 Token 去除首尾空格后长度为 16～512，并至少存在一个允许群或允许用户。
- ID 列表只接受逗号分隔的正整数，去重后保存为 `frozenset[int]`。
- 任一 QQ 配置错误都返回 `configuration_error="qq_misconfigured"`，不抛出到 `AssistantRuntime`。
- `ASSISTANT_QQ_ENABLED` 非法时按用户试图启用处理，即 `enabled=True` 且状态为 `misconfigured`。
- 错误文本和对象 `repr` 不包含原始配置值。

- [ ] **步骤 4：实现协议模型和稳定错误**

`backend/channels/onebot/models.py` 至少实现：

```python
@dataclass(frozen=True, slots=True)
class OneBotAction:
    action: str
    params: dict[str, object]


@dataclass(frozen=True, slots=True)
class ParsedOneBotMessage:
    # 使用“统一类型约定”中的字段

    @property
    def stable_message_id(self) -> str:
        return f"qq:{self.self_id}:{self.message_id}"

    @property
    def conversation_id(self) -> str:
        if self.message_type == "private":
            return f"qq:private:{self.user_id}"
        return f"qq:group:{self.group_id}:user:{self.user_id}"
```

稳定错误码常量完整包含：

```python
QQ_DISABLED = "qq_disabled"
QQ_MISCONFIGURED = "qq_misconfigured"
ONEBOT_AUTHENTICATION_FAILED = "onebot_authentication_failed"
ONEBOT_DUPLICATE_CONNECTION = "onebot_duplicate_connection"
ONEBOT_INVALID_EVENT = "onebot_invalid_event"
ONEBOT_RATE_LIMITED = "onebot_rate_limited"
ONEBOT_DISCONNECTED = "onebot_disconnected"
ONEBOT_ACTION_TIMEOUT = "onebot_action_timeout"
ONEBOT_ACTION_FAILED = "onebot_action_failed"
```

- [ ] **步骤 5：运行测试并确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_config_models -v
```

预期：全部配置与模型测试显示 `ok`。

- [ ] **步骤 6：检查并提交**

运行：

```bash
git diff --check
git status --short
git add backend/channels/onebot backend/tests/test_onebot_config_models.py
git commit -m "feat: 定义 OneBot 配置与协议模型"
```

预期：提交只包含本任务文件，Token 示例不是可用秘密。

## 任务 2：事件解析、准入策略、限速与重复占用

**文件：**

- 创建：`backend/channels/onebot/parser.py`
- 创建：`backend/channels/onebot/policy.py`
- 测试：`backend/tests/test_onebot_parser_policy.py`

- [ ] **步骤 1：编写失败的事件解析测试**

覆盖以下输入：

- 私聊字符串和消息段数组都能提取纯文本。
- 群聊只接受消息段数组，并通过 `{"type": "at", "data": {"qq": "<self_id>"}}` 识别机器人。
- 只合并 `text` 段，忽略图片、语音、文件等其他段，不访问其 URL。
- 移除指向机器人的 `at` 段后文本为空时忽略。
- 非 `message` 事件、自发消息、错误 `self_id`、非正整数 ID 和超过 4000 字符的文本安全忽略。
- `group_id` 只在群聊中必填，私聊不读取伪造的 `group_id`。
- 群聊字符串中的 CQ 码不作为 `@` 解析。

核心断言：

```python
parsed = parse_onebot_event(
    {
        "post_type": "message",
        "message_type": "group",
        "self_id": 123,
        "user_id": 456,
        "group_id": 789,
        "message_id": 10,
        "message": [
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": " 你好 "}},
        ],
    },
    expected_self_id=123,
)
self.assertEqual(parsed.text, "你好")
self.assertTrue(parsed.mentioned_bot)
self.assertEqual(parsed.stable_message_id, "qq:123:10")
self.assertEqual(parsed.conversation_id, "qq:group:789:user:456")
```

- [ ] **步骤 2：编写失败的策略、限速和重复占用测试**

使用可注入的单调时钟，创建 `OneBotAdmissionPolicyTests`，测试方法名固定为：

- `test_private_chat_only_requires_user_allowlist`
- `test_group_chat_requires_group_allowlist_and_bot_mention`
- `test_group_membership_does_not_grant_private_access`
- `test_private_and_group_messages_share_the_sender_bucket`
- `test_bucket_allows_burst_then_refills_at_configured_rate`
- `test_idle_buckets_are_removed_without_changing_active_limits`
- `test_recent_message_registry_rejects_concurrent_and_recent_duplicates`
- `test_failed_preprocessing_can_release_a_claim_for_redelivery`

`OneBotAdmissionPolicy.admit()` 返回 `None` 表示允许，返回稳定错误码表示拒绝；白名单或未 `@` 的拒绝使用 `None` 的静默决策类型，不把未授权目标告知 QQ。为避免混淆，使用：

```python
class AdmissionOutcome(str, Enum):
    ALLOW = "allow"
    IGNORE = "ignore"
    RATE_LIMITED = "rate_limited"
```

- [ ] **步骤 3：运行测试并确认失败**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_parser_policy -v
```

预期：失败并报告 `parser` 或 `policy` 模块不存在。

- [ ] **步骤 4：实现纯事件解析器**

实现：

```python
def parse_onebot_event(
    payload: Mapping[str, object],
    *,
    expected_self_id: int,
) -> ParsedOneBotMessage | None:
    """返回受支持的文字消息；不支持或非法事件返回 None。"""
```

解析器不得依赖 FastAPI、WebSocket、数据库或应用服务。类型错误、缺字段和不支持事件统一返回 `None`，不得把原始正文拼进异常。

- [ ] **步骤 5：实现准入、令牌桶和最近事件注册表**

实现以下公开签名：

- `OneBotAdmissionPolicy(settings: OneBotSettings, limiter: SenderRateLimiter)`
- `OneBotAdmissionPolicy.admit(message: ParsedOneBotMessage) -> AdmissionOutcome`
- `SenderRateLimiter.allow(self_id: int, user_id: int) -> bool`
- `SenderRateLimiter.prune() -> None`
- `RecentMessageRegistry.claim(stable_message_id: str) -> bool`
- `RecentMessageRegistry.release(stable_message_id: str) -> None`
- `RecentMessageRegistry.prune() -> None`

令牌桶键固定为 `(self_id, user_id)`，补充速率为 `rate_per_minute / 60`，容量为 `rate_burst`。最近事件保留 10 分钟、最多 10,000 项；达到上限时先清理过期项，再移除最早项。只有在应用服务调用前失败时释放占用，已进入应用服务的消息保留到过期。

- [ ] **步骤 6：运行测试并确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_parser_policy -v
```

预期：全部解析与策略测试显示 `ok`。

- [ ] **步骤 7：检查并提交**

运行：

```bash
git diff --check
git add backend/channels/onebot/parser.py backend/channels/onebot/policy.py backend/tests/test_onebot_parser_policy.py
git commit -m "feat: 实现 QQ 消息解析与准入策略"
```

## 任务 3：拆分应用处理与发布边界

**文件：**

- 修改：`backend/application/assistant.py`
- 修改：`backend/tests/test_application_foundation.py`

- [ ] **步骤 1：编写失败的输出隔离和幂等查询测试**

增加：

```python
def test_process_runs_business_logic_without_publishing():
    response = asyncio.run(application.process(message()))
    self.assertEqual(response.text, "模型回答")
    subscriber.assert_not_awaited()


def test_handle_processes_then_publishes_once():
    response = asyncio.run(application.handle(message()))
    subscriber.assert_awaited_once_with(response)


def test_has_seen_message_uses_the_store_without_publishing():
    item = message(message_id="qq:123:456", source=MessageSource.QQ)
    self.assertFalse(asyncio.run(application.has_seen_message(item.message_id)))
    asyncio.run(application.process(item))
    self.assertTrue(asyncio.run(application.has_seen_message(item.message_id)))
    subscriber.assert_not_awaited()
```

- [ ] **步骤 2：运行测试并确认缺少入口**

运行：

```bash
python3 -m unittest backend.tests.test_application_foundation.ApplicationFoundationTests -v
```

预期：新测试失败，原因是 `process()` 或 `has_seen_message()` 不存在。

- [ ] **步骤 3：实现应用入口拆分**

修改为：

```python
async def process(self, message: IncomingMessage) -> AssistantResponse:
    return await self.sessions.run(message, self._handle_in_session)


async def handle(self, message: IncomingMessage) -> AssistantResponse:
    response = await self.process(message)
    await self.publisher.publish(response)
    return response


async def has_seen_message(self, message_id: str) -> bool:
    return await self.store.find_message(message_id) is not None
```

现有 Desktop HTTP、Desktop WebSocket、交互和场景调用点不改为 `process()`。

- [ ] **步骤 4：运行应用和 API 回归测试**

运行：

```bash
python3 -m unittest backend.tests.test_application_foundation backend.tests.test_api backend.tests.test_runtime -v
```

预期：新增测试和原有发布行为全部通过。

- [ ] **步骤 5：检查并提交**

运行：

```bash
git diff --check
git add backend/application/assistant.py backend/tests/test_application_foundation.py
git commit -m "refactor: 分离助手处理与渠道发布"
```

## 任务 4：OneBot 鉴权、单连接与动作响应关联

**文件：**

- 创建：`backend/channels/onebot/connection.py`
- 测试：`backend/tests/test_onebot_connection.py`

- [ ] **步骤 1：编写失败的鉴权测试**

覆盖：

- 只接受精确的 `Authorization: Bearer <token>`。
- Token 使用 `hmac.compare_digest()` 比较；通过补丁断言调用，不用计时测试。
- 缺失、错误方案、错误 Token 均返回 `onebot_authentication_failed`。
- `X-Self-ID` 只接受十进制正整数。
- 异常字符串不包含收到的请求头或正确 Token。

接口固定为：

```python
def authenticate_onebot(
    authorization: str | None,
    self_id_header: str | None,
    settings: OneBotSettings,
) -> int:
    """成功时返回机器人 QQ 号，失败时抛出 OneBotChannelError。"""
```

- [ ] **步骤 2：编写失败的连接与动作测试**

使用只实现 `send_json()` 和 `close()` 的异步 WebSocket 替身，覆盖：

- 第一条连接可占用，第二条连接得到 `onebot_duplicate_connection`。
- 断开旧连接后新连接可以占用。
- `send_action()` 生成 UUID `echo`，收到匹配成功响应后返回。
- 未知 `echo` 安全忽略且不完成其他等待者。
- `status != "ok"` 或 `retcode != 0` 返回 `onebot_action_failed`。
- 超时返回 `onebot_action_timeout`，不进行第二次 `send_json()`。
- 断线清理全部等待者并返回 `onebot_disconnected`。
- 旧连接的迟到断开不能清除已经接替的新连接。

- [ ] **步骤 3：运行测试并确认失败**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_connection -v
```

预期：失败并报告 `connection` 模块不存在。

- [ ] **步骤 4：实现连接管理器**

`OneBotConnectionManager` 的公开签名固定为：

- `__init__(action_timeout_seconds: float)`
- `connected: bool` 只读属性
- `attach(websocket: object, self_id: int) -> None`
- `detach(websocket: object) -> None`
- `send_action(action: OneBotAction) -> None`
- `resolve_action_response(payload: Mapping[str, object]) -> bool`
- `aclose() -> None`

实现约束：

- 使用 `asyncio.Lock` 原子维护活动连接。
- 使用 `uuid4().hex` 作为 `echo`。
- `send_action()` 先注册 Future，再发送，避免快速响应丢失。
- `resolve_action_response()` 只消费字符串 `echo` 与已知等待者。
- 清理和超时都从等待者映射移除 Future。
- 关闭码和日志只使用稳定错误码。

- [ ] **步骤 5：运行连接测试并确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_connection -v
```

预期：鉴权、单连接、动作成功、失败、超时和断线测试全部显示 `ok`。

- [ ] **步骤 6：检查并提交**

运行：

```bash
git diff --check
git add backend/channels/onebot/connection.py backend/tests/test_onebot_connection.py
git commit -m "feat: 管理 OneBot 反向 WebSocket 连接"
```

## 任务 5：QQ 渠道编排、幂等和回复转换

**文件：**

- 创建：`backend/channels/onebot/channel.py`
- 修改：`backend/channels/onebot/__init__.py`
- 创建：`backend/tests/test_onebot_channel.py`

- [ ] **步骤 1：编写失败的入站转换和输出隔离测试**

测试应用替身只暴露
`process(message: IncomingMessage) -> AssistantResponse` 和
`has_seen_message(message_id: str) -> bool` 两个异步方法。

覆盖：

- 私聊映射为 `source=MessageSource.QQ`、`sender.id=str(user_id)`、`qq:private:{user_id}`。
- 群聊映射为 `qq:group:{group_id}:user:{user_id}`。
- `metadata` 只保存数字 `self_id`、`user_id`、可选 `group_id` 和 `message_id`。
- 白名单拒绝、未 `@`、限速和重复事件均不调用 `process()` 或 `send_action()`。
- 应用处理调用 `process()` 而不是 `handle()`，因此 QQ 响应不会进入 Electron publisher。
- 已存在于 SQLite 的稳定消息 ID 被静默忽略，覆盖进程重启后的重放。
- 两个并发的同一事件只有一个进入应用服务。
- `has_seen_message()` 之前的渠道预处理失败时释放最近事件占用；一旦开始调用 `process()` 就不释放。

- [ ] **步骤 2：编写失败的回复和并发测试**

覆盖：

- 私聊使用 `send_private_msg` 和单个 `text` 段。
- 群聊使用 `send_group_msg`，第一段依次为 `reply`、`at`、`text`。
- 超过 4000 字符按字符边界拆分，每段不超过 4000；群聊仅第一段带 `reply` 和 `at`。
- 空文本和 `ResponseKind.ACTION` 不发送。
- 模型或本地业务返回的安全 `ResponseKind.ERROR` 文本仍可发送。
- QQ 全局并发不超过 `max_concurrency`，不同 QQ 会话可并行。
- `onebot_action_timeout` 和 `onebot_disconnected` 被渠道边界捕获，不影响下一条入站事件。

- [ ] **步骤 3：运行测试并确认失败**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_channel -v
```

预期：失败并报告 `channel` 模块不存在。

- [ ] **步骤 4：实现消息和回复纯转换函数**

实现以下纯函数签名：

- `to_incoming_message(message: ParsedOneBotMessage) -> IncomingMessage`
- `split_reply(text: str, limit: int = 4000) -> Sequence[str]`
- `private_reply_action(message: ParsedOneBotMessage, text: str) -> OneBotAction`
- `group_reply_action(message: ParsedOneBotMessage, text: str, *, first_chunk: bool) -> OneBotAction`

群聊第一段的消息数组必须严格为：

```python
[
    {"type": "reply", "data": {"id": str(message.message_id)}},
    {"type": "at", "data": {"qq": str(message.user_id)}},
    {"type": "text", "data": {"text": text}},
]
```

- [ ] **步骤 5：实现渠道编排**

`OneBotChannel` 构造参数固定为 `application`、`settings`、`connection`，并允许注入
`policy` 和 `recent_messages` 测试替身。公开入口固定为
`handle_event(payload: Mapping[str, object], *, self_id: int) -> None`。

固定顺序为：解析 → 白名单与 `@` → 最近事件占用 → `has_seen_message()` → 限速 → 全局 Semaphore → `process()` → 分段发送。未授权和限速均静默，不向攻击者回显策略。

- [ ] **步骤 6：运行渠道测试并确认通过**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_channel -v
```

预期：全部渠道、幂等、分段和并发测试显示 `ok`。

- [ ] **步骤 7：运行应用集成回归**

运行：

```bash
python3 -m unittest backend.tests.test_application_foundation backend.tests.test_onebot_channel backend.tests.test_memory_context -v
```

预期：QQ 用户会话与记忆隔离测试通过，Desktop 行为不变。

- [ ] **步骤 8：检查并提交**

运行：

```bash
git diff --check
git add backend/channels/onebot backend/tests/test_onebot_channel.py
git commit -m "feat: 接入统一 QQ 对话与回复流程"
```

## 任务 6：FastAPI WebSocket、状态接口与运行时生命周期

**文件：**

- 创建：`backend/api/qq.py`
- 修改：`backend/api/app.py`
- 修改：`backend/core/runtime.py`
- 修改：`backend/tests/test_runtime.py`
- 创建：`backend/tests/test_onebot_api.py`

- [ ] **步骤 1：编写失败的运行时和状态 API 测试**

覆盖：

- `AssistantRuntime` 默认构造关闭状态的 QQ 组件。
- 可注入 `OneBotSettings`、连接管理器和渠道，测试不读取真实环境。
- 无效 QQ 配置不会阻止 Runtime、Desktop HTTP 或工具服务构造。
- `status_payload()` 只返回 `enabled`、`state`、`allowedGroupCount` 和 `allowedUserCount`。
- `connected` 只在配置就绪且连接管理器存在活动连接时返回。
- `aclose()` 幂等关闭 OneBot 等待者后再关闭数据库。

状态断言：

```python
self.assertEqual(
    get_qq_status(runtime),
    {
        "enabled": True,
        "state": "disconnected",
        "allowedGroupCount": 1,
        "allowedUserCount": 2,
    },
)
self.assertNotIn("token", json.dumps(get_qq_status(runtime)).lower())
```

- [ ] **步骤 2：编写失败的 WebSocket 路由测试**

使用 FastAPI `TestClient` 或直接异步路由替身覆盖：

- 关闭状态以 `qq_disabled` 拒绝。
- 错误配置以 `qq_misconfigured` 拒绝。
- 缺失或错误 Token、非法 `X-Self-ID` 以 `onebot_authentication_failed` 拒绝。
- 重复连接以 `onebot_duplicate_connection` 拒绝，不替换旧连接。
- 合法对象中的动作响应先交给 `resolve_action_response()`。
- 其他合法对象交给 `OneBotChannel.handle_event()`。
- 单条非法 JSON 或 JSON 非对象不中断连接。
- 连续第 3 条非法帧以 WebSocket 代码 `1003` 关闭。
- 任意合法 JSON 对象把非法帧计数重置为 0。
- 单条渠道异常被隔离，后续合法事件仍能处理。
- WebSocket 断开后连接和 `echo` 等待者被清理。

- [ ] **步骤 3：运行测试并确认失败**

运行：

```bash
python3 -m unittest backend.tests.test_runtime backend.tests.test_onebot_api -v
```

预期：失败并报告 QQ Runtime 属性或 `api.qq` 不存在。

- [ ] **步骤 4：装配运行时组件**

为 `AssistantRuntime.__init__()` 增加可选参数：

```python
qq_settings: OneBotSettings | None = None
qq_connection: OneBotConnectionManager | None = None
qq_channel: OneBotChannel | None = None
```

默认装配顺序：

```python
self.qq_settings = qq_settings or OneBotSettings.from_env()
self.qq_connection = qq_connection or OneBotConnectionManager(
    action_timeout_seconds=self.qq_settings.action_timeout_seconds,
)
self.qq_channel = qq_channel or OneBotChannel(
    application=self.application,
    settings=self.qq_settings,
    connection=self.qq_connection,
)
```

即使 `disabled` 或 `misconfigured` 也构造无网络副作用的对象，状态 API 因而始终可用。`aclose()` 调用 `qq_connection.aclose()`；异常时仍继续关闭数据库，并在所有资源尝试关闭后重新抛出第一个异常。

- [ ] **步骤 5：实现 QQ API**

`backend/api/qq.py` 提供同步
`get_qq_status(runtime: AssistantRuntime = Depends(get_runtime)) -> dict[str, object]`
和异步 `qq_websocket(ws: WebSocket) -> None`。路由分别固定为
`GET /qq/status` 与 `WS /ws/qq`。

WebSocket 流程：

1. 在 `accept()` 前检查配置、Token 和 `X-Self-ID`。
2. 通过连接管理器原子占用连接，再执行 `accept()`。
3. 使用 `receive_text()`，仅通过 `json.loads()` 接收 JSON。
4. JSON 对象先尝试匹配 `echo`，未匹配时交给渠道。
5. 无效帧只记录 `onebot_invalid_event`，不记录正文。
6. `finally` 使用当前 WebSocket 身份执行 `detach()`。

在 `backend/api/app.py` 中：

```python
app.include_router(qq_router, prefix="/api")  # GET /api/qq/status
app.include_router(qq_websocket_router)       # WS /ws/qq
```

如果使用同一个 `router`，只给 HTTP 路径写完整 `/api/qq/status`，避免给 `/ws/qq` 错加 `/api` 前缀。

- [ ] **步骤 6：运行 QQ API 和运行时测试**

运行：

```bash
python3 -m unittest backend.tests.test_runtime backend.tests.test_onebot_api -v
```

预期：QQ 状态、鉴权、帧处理、清理和主应用隔离测试全部显示 `ok`。

- [ ] **步骤 7：运行现有 API、工具与集成回归**

运行：

```bash
python3 -m unittest backend.tests.test_api backend.tests.test_integration backend.tests.test_tool_service -v
```

预期：Desktop HTTP、Electron WebSocket 和工具权限行为不变。

- [ ] **步骤 8：检查并提交**

运行：

```bash
git diff --check
git add backend/api/app.py backend/api/qq.py backend/core/runtime.py backend/tests/test_runtime.py backend/tests/test_onebot_api.py
git commit -m "feat: 暴露 OneBot WebSocket 与状态接口"
```

## 任务 7：NapCat Docker 配套与静态安全契约

**文件：**

- 创建：`qq-bot/docker-compose.yml`
- 创建：`qq-bot/.env.example`
- 创建：`qq-bot/README.md`
- 修改：`.gitignore`
- 创建：`backend/tests/test_onebot_docker_contract.py`

- [ ] **步骤 1：先编写失败的 Compose 契约测试**

使用已固定版本的 PyYAML 解析，不调用 Docker、不拉取镜像。创建
`OneBotDockerContractTests`，测试方法名固定为：

- `test_compose_uses_configurable_official_napcat_image`
- `test_webui_only_binds_loopback_port_6099`
- `test_onebot_ports_3000_and_3001_are_not_exposed`
- `test_qq_and_napcat_config_volumes_are_persistent`
- `test_host_docker_internal_mapping_exists`
- `test_runtime_data_directories_are_gitignored`
- `test_example_environment_contains_no_sensitive_credentials`

- [ ] **步骤 2：运行测试并确认配套文件缺失**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_docker_contract -v
```

预期：失败并报告 `qq-bot/docker-compose.yml` 不存在。

- [ ] **步骤 3：创建最小 Compose 配置**

`qq-bot/docker-compose.yml`：

```yaml
services:
  napcat:
    image: ${NAPCAT_IMAGE:-mlikiowa/napcat-docker:latest}
    restart: unless-stopped
    ports:
      - "127.0.0.1:6099:6099"
    volumes:
      - ./data/qq:/app/.config/QQ
      - ./data/config:/app/napcat/config
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

`.env.example` 只包含：

```dotenv
NAPCAT_IMAGE=mlikiowa/napcat-docker:latest
```

`.gitignore` 增加：

```gitignore
qq-bot/data/
qq-bot/.env
```

- [ ] **步骤 4：记录需要用户完成的 NapCat 配置**

`qq-bot/README.md` 明确：

- Docker 只用于 NapCat，不启动后端、Electron、SQLite 或模型服务。
- 先启动本机后端，再运行 `docker compose up -d`。
- 在 `http://127.0.0.1:6099` 手动扫码登录并接受相关协议。
- 在 WebUI 手动创建反向 WebSocket 客户端。
- URL 为 `ws://host.docker.internal:8080/ws/qq`。
- Token 与本机 `ASSISTANT_QQ_ACCESS_TOKEN` 相同，但不得写入仓库或 `.env.example`。
- 消息格式选择 `array`，关闭上报自身消息。
- macOS/Windows 使用 `host.docker.internal`；Linux 由 `extra_hosts` 提供映射。
- 停止和排错命令不删除 `qq-bot/data/`。

- [ ] **步骤 5：运行 Docker 契约测试**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_docker_contract -v
```

预期：全部静态契约测试显示 `ok`，没有 Docker 网络活动。

- [ ] **步骤 6：可用时执行 Compose 语法检查**

运行：

```bash
docker compose -f qq-bot/docker-compose.yml config --quiet
```

预期：若本机已安装 Docker Compose，则退出码为 0；若未安装，只记录“未执行可选检查”，不得安装 Docker、拉取镜像或阻塞提交。

- [ ] **步骤 7：检查并提交**

运行：

```bash
git diff --check
git add .gitignore qq-bot backend/tests/test_onebot_docker_contract.py
git commit -m "feat: 增加可选 NapCat Docker 配套"
```

## 任务 8：主文档、完整验证与草稿 Pull Request

**文件：**

- 修改：`README.md`
- 检查：`docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md`
- 检查：`docs/superpowers/plans/2026-07-29-onebot-qq-channel.md`

- [ ] **步骤 1：更新项目状态和 QQ 配置文档**

在 `README.md` 中：

- 把“QQ 接入仍未实现”改为“已实现 OneBot 11 私聊与群聊文字接入，默认关闭”。
- 增加 8 个 `ASSISTANT_QQ_*` 环境变量及范围。
- 增加 `/api/qq/status` 与 `/ws/qq`。
- 说明私聊用户白名单、群白名单和群聊 `@` 规则。
- 说明 QQ 只调用统一应用服务，不能绕过工具风险与确认策略。
- 链接 `qq-bot/README.md`、批准的设计规格和本实现计划。
- 明确 Docker、真实 QQ 登录和 WebUI 配置不是自动化测试的一部分。

- [ ] **步骤 2：运行敏感信息与占位符检查**

运行：

```bash
rg -n "T[O]DO|T[B]D|F[I]XME|g[h]o_|q[q]_password|ASSISTANT_QQ_ACCESS_TOKEN=[^<[:space:]]" backend/channels/onebot backend/api/qq.py qq-bot README.md docs/superpowers/plans/2026-07-29-onebot-qq-channel.md
```

预期：没有实现占位符、GitHub Token、QQ 密码或真实 OneBot Token；文档中的变量名和 `<token>` 示例可以保留。

- [ ] **步骤 3：运行后端编译和完整测试**

运行：

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
```

预期：编译退出码为 0，全部后端测试显示 `OK`。

- [ ] **步骤 4：运行桌面端完整回归**

运行：

```bash
npm test --prefix desktop-app
npm run build:renderer --prefix desktop-app
```

预期：Node 测试全部通过，Renderer 构建成功；QQ 响应没有进入 Electron 广播契约。

- [ ] **步骤 5：执行最终差异和历史检查**

运行：

```bash
git diff --check
git status --short
git log --oneline origin/main..HEAD
```

预期：只有 `README.md` 尚未提交；历史包含按任务拆分的 OneBot 提交。

- [ ] **步骤 6：提交发布文档**

运行：

```bash
git add README.md docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md docs/superpowers/plans/2026-07-29-onebot-qq-channel.md
git commit -m "docs: 记录 QQ 渠道配置与验收流程"
```

- [ ] **步骤 7：再次验证干净工作树**

运行：

```bash
git diff --check
git status --short
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
npm test --prefix desktop-app
```

预期：工作树干净，后端和桌面测试全部通过。

- [ ] **步骤 8：推送分支并创建草稿 Pull Request**

先使用 `apply_patch` 创建 `/tmp/onebot-qq-pr-body.md`，内容固定为：

```markdown
## 内容

- 接入 OneBot 11 反向 WebSocket，支持 QQ 私聊和群聊文字。
- 私聊无需 @；群聊必须位于允许群并结构化 @ 机器人。
- 增加 Token 鉴权、独立白名单、发送者限速、单连接和 echo 超时。
- QQ 调用 AssistantApplication.process()，回复不会进入 Electron 广播。
- 增加仅绑定本机 WebUI 的可选 NapCat Docker 配套。

## 验证

- `python3 -m compileall -q backend`
- `python3 -m unittest discover -s backend/tests -p 'test_*.py' -v`
- `npm test --prefix desktop-app`
- `npm run build:renderer --prefix desktop-app`

## 手动验收

真实 QQ 扫码登录和 NapCat WebUI 反向 WebSocket 联调仍需主人完成；完成前保持 Draft。
```

运行：

```bash
git push -u origin codex/onebot-channel
gh pr create --draft --base main --head codex/onebot-channel --title "feat: 接入 OneBot 11 QQ 渠道" --body-file /tmp/onebot-qq-pr-body.md
```

PR 正文写明：

- 私聊无需 `@`，群聊需允许群且结构化 `@机器人`。
- Token、白名单、限速、单连接和 `echo` 超时边界。
- QQ 回复与 Electron 广播隔离。
- Docker 仅为可选 NapCat 配套。
- 已执行的自动化验证命令。
- 真实 QQ 扫码和 NapCat WebUI 联调仍需主人手动完成。

预期：分支推送成功并得到草稿 PR URL；在真实 QQ 手动验收完成前不合并。

## 最终验收清单

- [ ] QQ 默认关闭；关闭或配置错误不影响 FastAPI、Electron、Live2D、模型、记忆和工具。
- [ ] 私聊只检查用户白名单且不要求 `@`。
- [ ] 群聊只检查群白名单并要求结构化 `@机器人`。
- [ ] 群聊回复第一段包含 `reply + at + text`，后续段只包含 `text`。
- [ ] QQ 用户在私聊和群聊共享限速，但会话与长期记忆按设计隔离。
- [ ] 重放事件不重复调用模型，也不重复回复。
- [ ] QQ 调用 `process()`，不会把回复发布到 Electron。
- [ ] 出站动作使用 UUID `echo`，失败或超时不自动重试。
- [ ] 状态 API、日志、异常、SQLite、Git 和示例文件不泄露 Token、Cookie 或消息全文。
- [ ] Compose 只绑定 `127.0.0.1:6099`，不暴露 3000/3001。
- [ ] CI 不启动 Docker、不拉取镜像、不登录真实 QQ。
- [ ] 完整后端测试、桌面测试、Renderer 构建和 `git diff --check` 全部通过。
