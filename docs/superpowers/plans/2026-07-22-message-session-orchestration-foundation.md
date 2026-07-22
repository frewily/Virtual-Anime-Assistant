# 统一消息、会话与对话编排基础实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 用类型化消息、会话串行化、进程内响应事件和应用编排替换现有字典驱动的集中式 `MessageRouter`，同时保持桌面聊天、点击互动、场景 TTS 和 WebSocket 行为兼容。

**架构：** `domain/` 定义不依赖渠道的稳定数据模型，`channels/desktop.py` 负责 Electron 协议转换，`application/` 负责会话内按序编排和响应发布。FastAPI、WebSocket 和场景循环只调用 `AssistantApplication`，具体桌面 JSON 格式停留在渠道适配器内。

**技术栈：** Python 3.10+、FastAPI、Pydantic 2、asyncio、unittest、Electron WebSocket

---

## 范围

本计划只完成总体规格中的第 1 个子项目：统一消息、会话和对话编排基础。真实大模型、SQLite 记忆、电脑控制、QQ、正式 Live2D SDK 和安装包分别使用后续计划实现。

当前工作目录已经位于 `codex/project-hardening` 分支，并包含此前项目加固产生的未提交改动。任务 0 先验证并保存该基线，避免后续架构改动和历史加固混在同一个提交中。

## 文件结构

### 新建文件

- `backend/domain/__init__.py`：领域模型包入口。
- `backend/domain/messages.py`：统一输入消息、发送者和内容联合类型。
- `backend/domain/responses.py`：统一助手响应和角色语义提示。
- `backend/channels/__init__.py`：渠道适配器包入口。
- `backend/channels/desktop.py`：Desktop HTTP、WebSocket 和场景结果的双向转换。
- `backend/application/__init__.py`：应用编排包入口。
- `backend/application/events.py`：类型化进程内响应发布器。
- `backend/application/sessions.py`：按会话串行执行和临时会话状态。
- `backend/application/assistant.py`：聊天、互动和场景响应的统一编排入口。
- `backend/tests/test_domain_models.py`：统一消息与响应模型测试。
- `backend/tests/test_desktop_channel.py`：Desktop 渠道转换契约测试。
- `backend/tests/test_application_foundation.py`：事件、会话和应用编排测试。

### 修改文件

- `backend/core/runtime.py`：持有 `AssistantApplication`，并将场景结果转换为统一消息。
- `backend/core/scenario.py`：场景结果增加稳定的 `scenarioId`。
- `backend/api/app.py`：在生命周期内订阅和取消订阅 Desktop 输出。
- `backend/api/chat.py`：将 HTTP 请求转换为统一消息。
- `backend/api/avatar.py`：将动作请求转换为统一互动消息。
- `backend/api/ws.py`：解析 Desktop 消息并发布统一响应。
- `backend/tests/test_api.py`：改为观察类型化响应事件。
- `backend/tests/test_integration.py`：验证 HTTP 和 WebSocket 的兼容行为。
- `backend/tests/test_runtime.py`：验证场景经过统一消息入口。
- `backend/tests/test_scenario.py`：验证场景 ID 被保留。
- `README.md`：更新模块结构和第一阶段完成状态。
- `docs/superpowers/plans/2026-07-22-message-session-orchestration-foundation.md`：执行过程中勾选步骤并记录结果。

### 删除文件

- `backend/core/router.py`：其职责由 `AssistantApplication`、`SessionRegistry`、`ResponsePublisher` 和 Desktop 渠道适配器接管。

## 任务 0：验证并保存当前项目加固基线

**文件：**

- 检查：当前所有未提交文件
- 提交：FastAPI 重构、Electron 安全加固、TTS、场景、Windows 监控、CI、README 和既有计划文档

- [ ] **步骤 1：检查未提交范围和敏感信息**

运行：

```bash
git status --short
git diff --stat
git diff -- . ':!desktop-app/package-lock.json'
rg -n "(api[_-]?key|token|secret|password)\s*[:=]\s*[^$<{]" . \
  -g '!desktop-app/node_modules/**' \
  -g '!desktop-app/package-lock.json' \
  -g '!.git/**'
```

预期：改动只包含已审查的项目加固内容；未发现真实 API Key、Token、密码或 Cookie。示例环境变量名称和测试假数据允许保留。

- [ ] **步骤 2：运行完整基线验证**

运行：

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -v
npm --prefix desktop-app ci
npm --prefix desktop-app run build:renderer
npm --prefix desktop-app test
git diff --check
```

预期：Python 编译退出码为 0，现有 23 个测试全部通过，renderer 构建和 Node.js 语法检查通过，`git diff --check` 无输出。

- [ ] **步骤 3：暂存基线并确认提交范围**

运行：

```bash
git add .github .gitignore README.md agent backend config desktop-app docs/superpowers/plans/2026-07-20-backend-python-refactor.md docs/superpowers/plans/2026-07-22-project-hardening.md docs/superpowers/specs/2026-07-20-backend-python-refactor-design.md
git diff --cached --check
git diff --cached --name-status
```

预期：暂存区不包含本计划后续尚未实现的 `backend/application/`、`backend/channels/` 和 `backend/domain/`；格式检查无输出。

- [ ] **步骤 4：提交加固基线**

运行：

```bash
git commit -m "refactor: 完成 FastAPI 项目加固基线"
```

预期：提交成功，工作区不再包含本次项目加固基线的代码改动。

## 任务 1：建立统一领域消息与响应模型

**文件：**

- 创建：`backend/domain/__init__.py`
- 创建：`backend/domain/messages.py`
- 创建：`backend/domain/responses.py`
- 测试：`backend/tests/test_domain_models.py`

- [ ] **步骤 1：编写失败的领域模型测试**

创建 `backend/tests/test_domain_models.py`：

```python
import sys
import unittest
from datetime import timezone
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.messages import ChatContent, IncomingMessage, MessageSource, SenderIdentity
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class DomainModelTests(unittest.TestCase):
    def test_message_has_unique_id_and_aware_timestamp(self):
        sender = SenderIdentity(id="local-user", display_name="本机用户")
        first = IncomingMessage(
            conversation_id="desktop:local-user",
            source=MessageSource.DESKTOP,
            sender=sender,
            content=ChatContent(text="你好"),
        )
        second = IncomingMessage(
            conversation_id="desktop:local-user",
            source=MessageSource.DESKTOP,
            sender=sender,
            content=ChatContent(text="再见"),
        )

        self.assertNotEqual(first.message_id, second.message_id)
        self.assertIs(first.timestamp.tzinfo, timezone.utc)

    def test_chat_content_rejects_blank_text(self):
        with self.assertRaises(ValidationError):
            ChatContent(text="   ")

    def test_avatar_intensity_is_limited_to_unit_interval(self):
        with self.assertRaises(ValidationError):
            AvatarCue(emotion="happy", motion="wave", intensity=1.1)

    def test_speak_response_requires_text(self):
        with self.assertRaises(ValidationError):
            AssistantResponse(
                correlation_id="message-1",
                conversation_id="desktop:local-user",
                kind=ResponseKind.SPEAK,
                avatar=AvatarCue(emotion="happy", motion="wave"),
            )
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_domain_models.py' -v
```

预期：FAIL，错误包含 `ModuleNotFoundError: No module named 'domain'`。

- [ ] **步骤 3：实现统一消息模型**

创建空文件 `backend/domain/__init__.py`，创建 `backend/domain/messages.py`：

```python
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class MessageSource(str, Enum):
    DESKTOP = "desktop"
    QQ = "qq"
    SCENARIO = "scenario"
    SYSTEM = "system"


class SenderIdentity(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)


class ChatContent(BaseModel):
    type: Literal["chat"] = "chat"
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("chat text must not be blank")
        return value


class InteractionContent(BaseModel):
    type: Literal["interaction"] = "interaction"
    action: str = Field(min_length=1, max_length=100)
    x: float | None = None
    y: float | None = None


class ScenarioContent(BaseModel):
    type: Literal["scenario"] = "scenario"
    scenario_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)
    expression: str | None = Field(default=None, max_length=100)
    motion: str | None = Field(default=None, max_length=100)


MessageContent = Annotated[
    ChatContent | InteractionContent | ScenarioContent,
    Field(discriminator="type"),
]


class IncomingMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: uuid4().hex)
    conversation_id: str = Field(min_length=1, max_length=200)
    source: MessageSource
    sender: SenderIdentity
    content: MessageContent
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **步骤 4：实现统一响应模型**

创建 `backend/domain/responses.py`：

```python
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ResponseKind(str, Enum):
    SPEAK = "speak"
    ACTION = "action"
    STATUS = "status"
    ERROR = "error"


class AvatarCue(BaseModel):
    emotion: str | None = Field(default=None, max_length=100)
    intent: str | None = Field(default=None, max_length=100)
    expression: str | None = Field(default=None, max_length=100)
    motion: str | None = Field(default=None, max_length=100)
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)


class AssistantResponse(BaseModel):
    response_id: str = Field(default_factory=lambda: uuid4().hex)
    correlation_id: str
    conversation_id: str
    kind: ResponseKind
    text: str | None = Field(default=None, max_length=4000)
    avatar: AvatarCue | None = None
    audio_url: str | None = None

    @model_validator(mode="after")
    def validate_kind_payload(self):
        if self.kind is ResponseKind.SPEAK and not self.text:
            raise ValueError("speak response requires text")
        if self.kind is ResponseKind.ACTION and self.avatar is None:
            raise ValueError("action response requires avatar cue")
        return self
```

- [ ] **步骤 5：运行领域模型测试**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_domain_models.py' -v
```

预期：4 个测试全部通过。

- [ ] **步骤 6：提交领域模型**

运行：

```bash
git add backend/domain backend/tests/test_domain_models.py
git diff --cached --check
git commit -m "refactor: 建立统一消息与响应模型"
```

预期：提交成功，只包含领域模型与对应测试。

## 任务 2：建立 Desktop 渠道适配器

**文件：**

- 创建：`backend/channels/__init__.py`
- 创建：`backend/channels/desktop.py`
- 测试：`backend/tests/test_desktop_channel.py`

- [ ] **步骤 1：编写失败的渠道契约测试**

创建 `backend/tests/test_desktop_channel.py`：

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channels.desktop import (
    client_payload_to_message,
    desktop_chat_to_message,
    response_to_desktop_payload,
    scenario_result_to_message,
)
from domain.messages import ChatContent, InteractionContent, ScenarioContent
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class DesktopChannelTests(unittest.TestCase):
    def test_http_chat_is_normalized(self):
        message = desktop_chat_to_message("user-1", "你好")

        self.assertEqual(message.conversation_id, "desktop:user-1")
        self.assertIsInstance(message.content, ChatContent)
        self.assertEqual(message.content.text, "你好")

    def test_websocket_interaction_is_normalized(self):
        message = client_payload_to_message(
            {"type": "interaction", "action": "click", "x": 10, "y": 20}
        )

        self.assertIsInstance(message.content, InteractionContent)
        self.assertEqual(message.content.action, "click")
        self.assertEqual(message.content.x, 10)

    def test_scenario_result_is_normalized(self):
        message = scenario_result_to_message(
            {
                "scenarioId": "high_cpu",
                "text": "电脑好热啊",
                "expression": "worried",
                "motion": "shake",
            }
        )

        self.assertIsInstance(message.content, ScenarioContent)
        self.assertEqual(message.content.scenario_id, "high_cpu")

    def test_response_is_flattened_for_existing_renderer(self):
        response = AssistantResponse(
            correlation_id="message-1",
            conversation_id="desktop:user-1",
            kind=ResponseKind.SPEAK,
            text="你好",
            avatar=AvatarCue(expression="happy", motion="wave"),
            audio_url="/api/tts/audio/example.wav",
        )

        self.assertEqual(
            response_to_desktop_payload(response),
            {
                "type": "speak",
                "text": "你好",
                "expression": "happy",
                "motion": "wave",
                "audioUrl": "/api/tts/audio/example.wav",
                "correlationId": "message-1",
            },
        )

    def test_unknown_websocket_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported message type"):
            client_payload_to_message({"type": "unknown"})
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_desktop_channel.py' -v
```

预期：FAIL，错误包含 `ModuleNotFoundError: No module named 'channels'`。

- [ ] **步骤 3：实现 Desktop 渠道转换**

创建空文件 `backend/channels/__init__.py`，创建 `backend/channels/desktop.py`：

```python
from typing import Any

from domain.messages import (
    ChatContent,
    IncomingMessage,
    InteractionContent,
    MessageSource,
    ScenarioContent,
    SenderIdentity,
)
from domain.responses import AssistantResponse


LOCAL_USER = SenderIdentity(id="local-user", display_name="本机用户")
SCENARIO_SENDER = SenderIdentity(id="scenario-engine", display_name="场景引擎")


def desktop_chat_to_message(sender_id: str, content: str) -> IncomingMessage:
    sender = SenderIdentity(id=sender_id)
    return IncomingMessage(
        conversation_id=f"desktop:{sender_id}",
        source=MessageSource.DESKTOP,
        sender=sender,
        content=ChatContent(text=content),
    )


def client_payload_to_message(payload: dict[str, Any]) -> IncomingMessage:
    message_type = payload.get("type")
    sender_id = str(payload.get("senderId") or LOCAL_USER.id)
    conversation_id = str(payload.get("conversationId") or f"desktop:{sender_id}")

    if message_type == "chat":
        return IncomingMessage(
            conversation_id=conversation_id,
            source=MessageSource.DESKTOP,
            sender=SenderIdentity(id=sender_id),
            content=ChatContent(text=str(payload.get("content") or "")),
        )
    if message_type == "interaction":
        return IncomingMessage(
            conversation_id=conversation_id,
            source=MessageSource.DESKTOP,
            sender=SenderIdentity(id=sender_id),
            content=InteractionContent(
                action=str(payload.get("action") or "click"),
                x=payload.get("x"),
                y=payload.get("y"),
            ),
        )
    raise ValueError(f"unsupported message type: {message_type!r}")


def scenario_result_to_message(result: dict[str, Any]) -> IncomingMessage:
    scenario_id = str(result["scenarioId"])
    return IncomingMessage(
        conversation_id=f"scenario:{scenario_id}",
        source=MessageSource.SCENARIO,
        sender=SCENARIO_SENDER,
        content=ScenarioContent(
            scenario_id=scenario_id,
            text=str(result["text"]),
            expression=result.get("expression"),
            motion=result.get("motion"),
        ),
    )


def response_to_desktop_payload(response: AssistantResponse) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": response.kind.value,
        "correlationId": response.correlation_id,
    }
    if response.text is not None:
        payload["text"] = response.text
    if response.avatar is not None:
        if response.avatar.expression is not None:
            payload["expression"] = response.avatar.expression
        if response.avatar.motion is not None:
            payload["motion"] = response.avatar.motion
    if response.audio_url is not None:
        payload["audioUrl"] = response.audio_url
    return payload
```

- [ ] **步骤 4：运行渠道契约测试**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_desktop_channel.py' -v
```

预期：5 个测试全部通过。

- [ ] **步骤 5：提交 Desktop 渠道适配器**

运行：

```bash
git add backend/channels backend/tests/test_desktop_channel.py
git diff --cached --check
git commit -m "refactor: 隔离桌面消息协议"
```

预期：提交成功，只包含 Desktop 渠道适配器与契约测试。

## 任务 3：建立响应事件和会话串行化

**文件：**

- 创建：`backend/application/__init__.py`
- 创建：`backend/application/events.py`
- 创建：`backend/application/sessions.py`
- 测试：`backend/tests/test_application_foundation.py`

- [ ] **步骤 1：编写失败的事件与会话测试**

创建 `backend/tests/test_application_foundation.py`：

```python
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.events import ResponsePublisher
from application.sessions import SessionRegistry
from domain.messages import ChatContent, IncomingMessage, MessageSource, SenderIdentity
from domain.responses import AssistantResponse, ResponseKind


def message(conversation_id: str = "desktop:user-1") -> IncomingMessage:
    return IncomingMessage(
        conversation_id=conversation_id,
        source=MessageSource.DESKTOP,
        sender=SenderIdentity(id="user-1"),
        content=ChatContent(text="你好"),
    )


def response_for(item: IncomingMessage) -> AssistantResponse:
    return AssistantResponse(
        correlation_id=item.message_id,
        conversation_id=item.conversation_id,
        kind=ResponseKind.SPEAK,
        text="收到",
    )


class ApplicationFoundationTests(unittest.TestCase):
    def test_publisher_isolates_failed_subscribers(self):
        publisher = ResponsePublisher()
        failed = AsyncMock(side_effect=RuntimeError("offline"))
        healthy = AsyncMock()
        publisher.subscribe(failed)
        publisher.subscribe(healthy)
        item = message()

        asyncio.run(publisher.publish(response_for(item)))

        failed.assert_awaited_once()
        healthy.assert_awaited_once()

    def test_unsubscribe_stops_delivery(self):
        publisher = ResponsePublisher()
        subscriber = AsyncMock()
        unsubscribe = publisher.subscribe(subscriber)
        unsubscribe()

        asyncio.run(publisher.publish(response_for(message())))

        subscriber.assert_not_awaited()

    def test_same_conversation_is_processed_sequentially(self):
        registry = SessionRegistry()
        active = 0
        max_active = 0

        async def handler(item, state):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return response_for(item)

        async def run_both():
            await asyncio.gather(
                registry.run(message(), handler),
                registry.run(message(), handler),
            )

        asyncio.run(run_both())

        self.assertEqual(max_active, 1)
        self.assertEqual(registry.get_state("desktop:user-1").turn_count, 2)
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_application_foundation.py' -v
```

预期：FAIL，错误包含 `ModuleNotFoundError: No module named 'application'`。

- [ ] **步骤 3：实现类型化响应发布器**

创建空文件 `backend/application/__init__.py`，创建 `backend/application/events.py`：

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable

from domain.responses import AssistantResponse

logger = logging.getLogger(__name__)
ResponseSubscriber = Callable[[AssistantResponse], Awaitable[None]]


class ResponsePublisher:
    def __init__(self):
        self._subscribers: list[ResponseSubscriber] = []

    def subscribe(self, subscriber: ResponseSubscriber) -> Callable[[], None]:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

        return unsubscribe

    async def publish(self, response: AssistantResponse) -> None:
        results = await asyncio.gather(
            *(subscriber(response) for subscriber in tuple(self._subscribers)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error(
                    "Response subscriber failed: %s",
                    type(result).__name__,
                )
```

- [ ] **步骤 4：实现会话串行化**

创建 `backend/application/sessions.py`：

```python
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from domain.messages import IncomingMessage
from domain.responses import AssistantResponse


@dataclass
class SessionState:
    conversation_id: str
    turn_count: int = 0
    last_message_id: str | None = None


SessionHandler = Callable[
    [IncomingMessage, SessionState],
    Awaitable[AssistantResponse],
]


class SessionRegistry:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, SessionState] = {}

    async def run(
        self,
        message: IncomingMessage,
        handler: SessionHandler,
    ) -> AssistantResponse:
        lock = self._locks.setdefault(message.conversation_id, asyncio.Lock())
        state = self._states.setdefault(
            message.conversation_id,
            SessionState(conversation_id=message.conversation_id),
        )
        async with lock:
            response = await handler(message, state)
            state.turn_count += 1
            state.last_message_id = message.message_id
            return response

    def get_state(self, conversation_id: str) -> SessionState | None:
        return self._states.get(conversation_id)
```

- [ ] **步骤 5：运行事件与会话测试**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_application_foundation.py' -v
```

预期：3 个测试全部通过。日志允许记录测试订阅者的 `RuntimeError`，测试进程仍以 0 退出。

- [ ] **步骤 6：提交应用基础模块**

运行：

```bash
git add backend/application backend/tests/test_application_foundation.py
git diff --cached --check
git commit -m "refactor: 增加会话与响应事件基础"
```

预期：提交成功，只包含事件、会话和对应测试。

## 任务 4：建立统一对话编排入口

**文件：**

- 创建：`backend/application/assistant.py`
- 修改：`backend/tests/test_application_foundation.py`

- [ ] **步骤 1：编写失败的对话编排测试**

向 `backend/tests/test_application_foundation.py` 增加导入：

```python
from application.assistant import AssistantApplication
from domain.messages import InteractionContent, ScenarioContent
```

在测试文件末尾增加：

```python
class AssistantApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tts = AsyncMock()
        self.tts.synthesize.return_value = {
            "audio_url": "/api/tts/audio/example.wav",
            "text": "电脑好热啊",
        }
        self.publisher = ResponsePublisher()
        self.subscriber = AsyncMock()
        self.publisher.subscribe(self.subscriber)
        self.application = AssistantApplication(
            tts=self.tts,
            publisher=self.publisher,
        )

    def test_chat_returns_compatible_fixed_reply(self):
        result = asyncio.run(self.application.handle(message()))

        self.assertEqual(result.kind, ResponseKind.SPEAK)
        self.assertEqual(result.text, "主人说得有道理~")
        self.subscriber.assert_awaited_once_with(result)

    def test_interaction_returns_avatar_action(self):
        item = message()
        item.content = InteractionContent(action="click", x=10, y=20)

        result = asyncio.run(self.application.handle(item))

        self.assertEqual(result.kind, ResponseKind.ACTION)
        self.assertEqual(result.avatar.motion, "tap_body")

    def test_scenario_synthesizes_audio(self):
        item = IncomingMessage(
            conversation_id="scenario:high_cpu",
            source=MessageSource.SCENARIO,
            sender=SenderIdentity(id="scenario-engine"),
            content=ScenarioContent(
                scenario_id="high_cpu",
                text="电脑好热啊",
                expression="worried",
                motion="shake",
            ),
        )

        result = asyncio.run(self.application.handle(item))

        self.tts.synthesize.assert_awaited_once_with("电脑好热啊")
        self.assertEqual(result.audio_url, "/api/tts/audio/example.wav")
        self.assertEqual(result.avatar.expression, "worried")
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_application_foundation.py' -v
```

预期：FAIL，错误包含 `ModuleNotFoundError: No module named 'application.assistant'`。

- [ ] **步骤 3：实现统一应用编排**

创建 `backend/application/assistant.py`：

```python
from application.events import ResponsePublisher
from application.sessions import SessionRegistry, SessionState
from domain.messages import (
    ChatContent,
    IncomingMessage,
    InteractionContent,
    ScenarioContent,
)
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class AssistantApplication:
    def __init__(
        self,
        *,
        tts,
        publisher: ResponsePublisher | None = None,
        sessions: SessionRegistry | None = None,
    ):
        self.tts = tts
        self.publisher = publisher or ResponsePublisher()
        self.sessions = sessions or SessionRegistry()

    async def handle(self, message: IncomingMessage) -> AssistantResponse:
        response = await self.sessions.run(message, self._handle_in_session)
        await self.publisher.publish(response)
        return response

    async def _handle_in_session(
        self,
        message: IncomingMessage,
        _: SessionState,
    ) -> AssistantResponse:
        content = message.content
        if isinstance(content, ChatContent):
            return AssistantResponse(
                correlation_id=message.message_id,
                conversation_id=message.conversation_id,
                kind=ResponseKind.SPEAK,
                text="主人说得有道理~",
                avatar=AvatarCue(
                    emotion="happy",
                    expression="happy",
                    motion="wave",
                ),
            )
        if isinstance(content, InteractionContent):
            return AssistantResponse(
                correlation_id=message.message_id,
                conversation_id=message.conversation_id,
                kind=ResponseKind.ACTION,
                avatar=AvatarCue(
                    emotion="surprised",
                    expression="surprised",
                    motion="tap_body",
                ),
            )
        if isinstance(content, ScenarioContent):
            audio = await self.tts.synthesize(content.text)
            return AssistantResponse(
                correlation_id=message.message_id,
                conversation_id=message.conversation_id,
                kind=ResponseKind.SPEAK,
                text=content.text,
                avatar=AvatarCue(
                    expression=content.expression,
                    motion=content.motion,
                ),
                audio_url=audio.get("audio_url") if audio else None,
            )
        raise ValueError(f"unsupported content type: {type(content).__name__}")
```

- [ ] **步骤 4：运行应用编排测试**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_application_foundation.py' -v
```

预期：原有 3 个测试和新增 3 个测试全部通过。

- [ ] **步骤 5：提交应用编排入口**

运行：

```bash
git add backend/application/assistant.py backend/tests/test_application_foundation.py
git diff --cached --check
git commit -m "refactor: 统一助手对话编排入口"
```

预期：提交成功，只包含应用编排和对应测试。

## 任务 5：迁移 Runtime、HTTP 和 WebSocket

**文件：**

- 修改：`backend/core/runtime.py`
- 修改：`backend/core/scenario.py`
- 修改：`backend/api/app.py`
- 修改：`backend/api/chat.py`
- 修改：`backend/api/avatar.py`
- 修改：`backend/api/ws.py`
- 修改：`backend/tests/test_api.py`
- 修改：`backend/tests/test_integration.py`
- 修改：`backend/tests/test_runtime.py`
- 修改：`backend/tests/test_scenario.py`
- 删除：`backend/core/router.py`

- [ ] **步骤 1：先修改测试以描述统一入口**

在 `backend/tests/test_scenario.py` 的最高优先级测试中增加：

```python
self.assertEqual(result["scenarioId"], "high")
```

将 `backend/tests/test_runtime.py` 替换为：

```python
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.monitor import ForegroundWindowMonitor
from core.runtime import AssistantRuntime


class RuntimeTests(unittest.TestCase):
    def test_window_state_is_copied_at_the_runtime_boundary(self):
        runtime = AssistantRuntime(monitor=Mock(), application=Mock())
        report = {"appName": "Code", "appId": "code"}

        runtime.report_window(report)
        report["appName"] = "Mutated"

        self.assertEqual(runtime.current_window()["appName"], "Code")

    def test_scenario_check_uses_unified_application(self):
        monitor = Mock()
        monitor.get_status.return_value = {"cpu": {"percent": 5}}
        application = Mock()
        application.handle = AsyncMock()
        scenario_engine = Mock()
        scenario_engine.detect.return_value = {
            "scenarioId": "focus_mode",
            "text": "休息一下",
            "expression": "happy",
            "motion": "wave",
        }
        runtime = AssistantRuntime(
            monitor=monitor,
            application=application,
            scenario_engine=scenario_engine,
        )
        runtime.report_window({"appName": "Code"})

        asyncio.run(runtime.check_scenarios())

        application.handle.assert_awaited_once()
        message = application.handle.await_args.args[0]
        self.assertEqual(message.content.scenario_id, "focus_mode")
        self.assertEqual(message.content.text, "休息一下")

    def test_foreground_monitor_reports_only_changes(self):
        reports = []
        get_app = Mock(return_value={"appName": "Code", "appId": "code"})
        monitor = ForegroundWindowMonitor(get_app, reports.append)

        asyncio.run(monitor.poll_once())
        asyncio.run(monitor.poll_once())

        self.assertEqual(reports, [{"appName": "Code", "appId": "code"}])
        self.assertEqual(get_app.call_count, 2)
```

将 `backend/tests/test_api.py` 替换为：

```python
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.chat import ChatMessage, handle_message
from api.ws import broadcast_to_desktop, is_allowed_origin, parse_client_message
from channels.desktop import client_payload_to_message
from core.runtime import runtime
from core.scenario import ScenarioEngine
from domain.responses import AssistantResponse, AvatarCue, ResponseKind


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.subscriber = AsyncMock()
        self.unsubscribe = runtime.application.publisher.subscribe(self.subscriber)

    def tearDown(self):
        self.unsubscribe()

    def test_chat_endpoint_routes_a_message_to_the_desktop(self):
        response = asyncio.run(
            handle_message(ChatMessage(source="desktop", senderId="user", content="你好"))
        )

        self.assertEqual(response, {"reply": "主人说得有道理~", "status": "ok"})
        self.subscriber.assert_awaited_once()

    def test_broadcast_serializes_response_for_each_live_websocket(self):
        class Socket:
            def __init__(self):
                self.send_text = AsyncMock()

        from api import ws

        socket = Socket()
        response = AssistantResponse(
            correlation_id="message-1",
            conversation_id="desktop:local-user",
            kind=ResponseKind.ACTION,
            avatar=AvatarCue(expression="surprised", motion="tap_body"),
        )
        ws._sessions.add(socket)
        try:
            asyncio.run(broadcast_to_desktop(response))
        finally:
            ws._sessions.discard(socket)

        payload = json.loads(socket.send_text.await_args.args[0])
        self.assertEqual(payload["type"], "action")
        self.assertEqual(payload["correlationId"], "message-1")
        self.assertEqual(payload["motion"], "tap_body")

    def test_app_duration_scenario_triggers_after_the_configured_minutes(self):
        engine = ScenarioEngine()
        focus = next(s for s in engine.scenarios if s["id"] == "focus_mode")
        focus["trigger"]["duration"] = 0

        result = engine.detect({"cpu": {"percent": 0}}, {"appName": "VS Code"})

        self.assertIsNotNone(result)
        self.assertEqual(result["expression"], "happy")

    def test_client_interaction_uses_the_shared_application(self):
        response = asyncio.run(
            runtime.application.handle(
                client_payload_to_message({"type": "interaction", "action": "click"})
            )
        )

        self.assertEqual(response.kind, ResponseKind.ACTION)
        self.subscriber.assert_awaited_once_with(response)

    def test_unknown_client_message_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported message type"):
            client_payload_to_message({"type": "unknown"})

    def test_websocket_message_must_be_a_json_object(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_client_message("[]")

    def test_websocket_origin_is_restricted_to_local_electron(self):
        self.assertTrue(is_allowed_origin(None))
        self.assertTrue(is_allowed_origin("file://"))
        self.assertTrue(is_allowed_origin("null"))
        self.assertFalse(is_allowed_origin("https://example.com"))
```

在 `backend/tests/test_integration.py` 中增加 HTTP 兼容测试：

```python
    def test_chat_http_contract_remains_compatible(self):
        with TestClient(app) as client:
            response = client.post(
                "/api/chat/message",
                json={
                    "source": "desktop",
                    "senderId": "user-1",
                    "content": "你好",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"reply": "主人说得有道理~", "status": "ok"},
        )
```

- [ ] **步骤 2：运行测试验证迁移尚未完成**

运行：

```bash
python3 -m unittest discover -s backend/tests -v
```

预期：FAIL，错误指出 `AssistantRuntime` 尚不接受 `application`、场景结果缺少 `scenarioId` 或 `runtime.application` 尚不存在。

- [ ] **步骤 3：让场景结果保留场景 ID**

在 `backend/core/scenario.py` 的返回值中加入：

```python
return {
    "scenarioId": selected["id"],
    "text": random.choice(response["templates"]).format(
        appName=(window or {}).get("appName", "当前应用")
    ),
    "expression": response["expression"],
    "motion": response["motion"],
}
```

- [ ] **步骤 4：迁移 Runtime 到统一应用入口**

将 `backend/core/runtime.py` 改为：

```python
"""Application runtime shared by HTTP, WebSocket, and background adapters."""

from application.assistant import AssistantApplication
from application.events import ResponsePublisher
from channels.desktop import scenario_result_to_message
from core.monitor import SystemMonitor
from core.scenario import ScenarioEngine
from core.tts import TTSService


class AssistantRuntime:
    def __init__(
        self,
        monitor=None,
        application=None,
        scenario_engine=None,
    ):
        self.monitor = monitor or SystemMonitor()
        if application is None:
            publisher = ResponsePublisher()
            application = AssistantApplication(
                tts=TTSService(),
                publisher=publisher,
            )
        self.application = application
        self.scenario_engine = scenario_engine or ScenarioEngine()
        self._current_window: dict | None = None

    def report_window(self, window: dict) -> None:
        self._current_window = dict(window)

    def current_window(self) -> dict | None:
        return dict(self._current_window) if self._current_window else None

    def status(self) -> dict:
        return self.monitor.get_status()

    async def check_scenarios(self) -> None:
        result = self.scenario_engine.detect(self.status(), self.current_window())
        if result is not None:
            await self.application.handle(scenario_result_to_message(result))


runtime = AssistantRuntime()
```

- [ ] **步骤 5：迁移 HTTP 入口**

将 `backend/api/chat.py` 的处理函数改为：

```python
from channels.desktop import desktop_chat_to_message


@router.post("/chat/message")
async def handle_message(msg: ChatMessage):
    response = await runtime.application.handle(
        desktop_chat_to_message(msg.sender_id, msg.content)
    )
    return {"reply": response.text, "status": "ok"}
```

在 `backend/api/avatar.py` 中增加请求模型并修改处理函数：

```python
from pydantic import BaseModel, Field

from channels.desktop import client_payload_to_message, response_to_desktop_payload


class AvatarActionRequest(BaseModel):
    action: str = Field(default="click", min_length=1, max_length=100)
    x: float | None = None
    y: float | None = None


@router.post("/avatar/action")
async def perform_action(action: AvatarActionRequest):
    message = client_payload_to_message(
        {"type": "interaction", **action.model_dump()}
    )
    response = await runtime.application.handle(message)
    return {"status": "ok", "action": response_to_desktop_payload(response)}
```

- [ ] **步骤 6：迁移 WebSocket 输入与输出**

在 `backend/api/ws.py` 中导入转换器和响应类型：

```python
from channels.desktop import client_payload_to_message, response_to_desktop_payload
from domain.responses import AssistantResponse
```

将广播函数替换为：

```python
async def broadcast_to_desktop(response: AssistantResponse) -> None:
    message = json.dumps(response_to_desktop_payload(response), ensure_ascii=False)
    disconnected: list[WebSocket] = []
    for ws in tuple(_sessions):
        try:
            await ws.send_text(message)
        except (RuntimeError, WebSocketDisconnect):
            disconnected.append(ws)
    for ws in disconnected:
        _sessions.discard(ws)
```

将 WebSocket 循环中的分发替换为：

```python
            try:
                payload = parse_client_message(data)
                await runtime.application.handle(client_payload_to_message(payload))
            except (ValueError, TypeError) as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
```

- [ ] **步骤 7：在应用生命周期订阅 Desktop 输出**

将 `backend/api/app.py` 生命周期开始和结束部分改为：

```python
@asynccontextmanager
async def lifespan(_: FastAPI):
    unsubscribe = runtime.application.publisher.subscribe(broadcast_to_desktop)
    tasks = [
        asyncio.create_task(supervise("scenario-loop", scenario_loop)),
        asyncio.create_task(
            supervise("window-monitor", lambda: run_window_monitor(runtime.report_window))
        ),
    ]
    try:
        yield
    finally:
        unsubscribe()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
```

- [ ] **步骤 8：删除旧路由并修正剩余测试引用**

删除 `backend/core/router.py`。运行以下搜索：

```bash
rg -n "MessageRouter|runtime\.router|set_ws_broadcaster|handle_client_message" backend
```

预期：没有生产代码命中。若测试仍命中，按步骤 1 的统一入口改写，不能保留兼容别名。

- [ ] **步骤 9：运行后端回归测试**

运行：

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -v
```

预期：全部测试通过，测试总数不少于 35 个。

- [ ] **步骤 10：提交 Runtime 和 API 迁移**

运行：

```bash
git add backend/api backend/core backend/tests
git diff --cached --check
git commit -m "refactor: 统一运行时消息处理链路"
```

预期：提交成功，旧 `backend/core/router.py` 被删除，HTTP、WebSocket 和场景入口均使用 `AssistantApplication`。

## 任务 6：文档、完整验证与阶段收尾

**文件：**

- 修改：`README.md`
- 修改：`docs/superpowers/plans/2026-07-22-message-session-orchestration-foundation.md`

- [ ] **步骤 1：更新 README 架构状态**

在 README 当前能力中增加：

```markdown
- 使用统一消息模型和会话编排处理桌面交互与场景事件。
```

将聊天占位说明改为：

```markdown
聊天已经经过统一会话入口，但回复生成仍是固定占位逻辑；大模型、QQ 机器人和正式安装包尚未实现。
```

将项目结构更新为：

```text
backend/       FastAPI API、统一消息、会话编排、场景、TTS 和平台监控
config/        声线、回复和场景 YAML 配置
desktop-app/   Electron 主进程、preload 和 renderer
docs/          架构规格与分阶段实施计划
```

- [ ] **步骤 2：执行完整验证**

运行：

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -v
npm --prefix desktop-app ci
npm --prefix desktop-app run build:renderer
npm --prefix desktop-app test
git diff --check
```

预期：Python 编译、全部后端测试、renderer 构建和 Node.js 语法检查均通过；`git diff --check` 无输出。

- [ ] **步骤 3：检查需求覆盖和未提交范围**

运行：

```bash
rg -n "MessageRouter|runtime\.router|set_ws_broadcaster" backend
git status --short
git diff --stat
```

预期：旧路由搜索无命中；未提交文件只包含 README 和本计划执行状态，不包含遗漏的生产代码。

- [ ] **步骤 4：记录执行结果并提交阶段文档**

在本计划末尾追加实际测试数量、构建结果和已知外部资源限制，然后运行：

```bash
git add README.md docs/superpowers/plans/2026-07-22-message-session-orchestration-foundation.md
git diff --cached --check
git commit -m "docs: 记录消息编排基础实施结果"
```

预期：提交成功，工作区没有本阶段遗留改动。

- [ ] **步骤 5：检查分支提交序列**

运行：

```bash
git log --oneline --decorate -10
git status --short --branch
```

预期：可以看到基线、领域模型、Desktop 适配器、会话事件、对话编排、运行时迁移和文档收尾等独立提交；分支名为 `codex/project-hardening`。

## 完成标准

- HTTP、WebSocket 和场景事件使用同一个 `AssistantApplication` 入口。
- 内部处理不再依赖渠道原始字典，只有渠道适配器接触 Electron JSON 格式。
- 同一 `conversation_id` 的消息按序执行，不同会话保留未来并发空间。
- 响应发布者隔离单个订阅者失败，Desktop 断线不会阻止应用生成响应。
- 旧 `MessageRouter` 被删除，没有兼容别名继续泄漏旧接口。
- 现有聊天、点击、场景 TTS 和 WebSocket 行为保持兼容。
- 新增测试覆盖领域模型、渠道契约、会话串行化、订阅失败隔离和统一应用编排。
- 每个逻辑阶段都有独立 Git 提交，完整回归通过后再进入大模型与 SQLite 记忆计划。
