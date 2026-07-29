# OneBot 动作响应自阻塞修复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 OneBot WebSocket 接收循环在处理 QQ 事件期间继续消费动作响应，消除实际发送成功却记录 `onebot_action_timeout` 的自阻塞问题。

**架构：** FastAPI 的 OneBot 路由为普通事件创建受管理的 `asyncio.Task`，主循环只负责持续读帧并优先解析 `echo` 响应。连接断开时先让连接管理器失败所有动作等待者，再取消并回收该连接拥有的事件任务；业务并发继续由现有 `OneBotChannel` 和 `SessionRegistry` 控制。

**技术栈：** Python 3.12+、FastAPI、`asyncio`、OneBot 11、Python `unittest`、Docker Compose、NapCat。

---

## 文件结构

- 修改：`backend/api/qq.py`
  - 创建、跟踪、消费和清理 OneBot 事件任务。
  - 保持 WebSocket 接收循环可以持续读取动作响应。
- 修改：`backend/tests/test_onebot_api.py`
  - 增加可协调收发的 WebSocket 测试替身。
  - 增加动作响应自阻塞、断线取消和异常隔离回归测试。
- 修改：`docs/superpowers/specs/2026-07-29-onebot-action-response-deadlock-design.md`
  - 把状态更新为已实现并记录真实验收结果。
- 修改：`docs/superpowers/plans/2026-07-29-onebot-action-response-deadlock.md`
  - 勾选已完成步骤并记录验证结果。
- 修改：`docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md`
  - 更新 OneBot 渠道阶段状态，移除“待真实联调”的过期说明。
- 修改：`README.md`
  - 把 QQ 能力状态更新为已通过真实 NapCat 联调。

## 任务 1：用路由级测试复现动作响应自阻塞

**文件：**

- 修改：`backend/tests/test_onebot_api.py`
- 测试：`backend/tests/test_onebot_api.py`

- [ ] **步骤 1：导入真实连接管理器和动作模型**

在测试文件中增加：

```python
from dataclasses import replace

from channels.onebot.connection import OneBotConnectionManager
from channels.onebot.models import (
    ONEBOT_AUTHENTICATION_FAILED,
    ONEBOT_DUPLICATE_CONNECTION,
    QQ_DISABLED,
    QQ_MISCONFIGURED,
    OneBotAction,
    OneBotChannelError,
)
```

`replace()` 用于把测试动作超时缩短到 `0.05` 秒，不修改生产默认值。

- [ ] **步骤 2：增加可协调收发的 WebSocket 测试替身**

在 `FakeWebSocket` 后增加：

```python
class QueuedWebSocket:
    DISCONNECT = object()

    def __init__(self, runtime) -> None:
        self.app = SimpleNamespace(
            state=SimpleNamespace(runtime=runtime)
        )
        self.headers = {
            "authorization": "Bearer 0123456789abcdef",
            "x-self-id": "123",
        }
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.send_json = AsyncMock(side_effect=self._send_json)
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def _send_json(self, payload: dict[str, object]) -> None:
        await self.sent.put(payload)

    async def receive_text(self) -> str:
        frame = await self._incoming.get()
        if frame is self.DISCONNECT:
            raise WebSocketDisconnect()
        return str(frame)

    async def push_json(self, payload: dict[str, object]) -> None:
        await self._incoming.put(json.dumps(payload))

    async def disconnect(self) -> None:
        await self._incoming.put(self.DISCONNECT)
```

该替身让测试可以在后端发出动作后，再精确回送同一 `echo` 的响应。

- [ ] **步骤 3：编写自阻塞回归测试**

在 `OneBotWebSocketApiTests` 中增加：

```python
async def test_action_response_resolves_while_event_is_running(self):
    configured = replace(
        ready_settings(),
        action_timeout_seconds=0.05,
    )
    connection = OneBotConnectionManager(
        action_timeout_seconds=configured.action_timeout_seconds
    )
    completed = asyncio.Event()

    async def handle_event(payload, *, self_id):
        await connection.send_action(
            OneBotAction(
                "send_private_msg",
                {"user_id": 456, "message": []},
            )
        )
        completed.set()

    runtime = runtime_for(
        configured,
        connection=connection,
        channel=SimpleNamespace(handle_event=handle_event),
    )
    websocket = QueuedWebSocket(runtime)
    route = asyncio.create_task(qq_websocket(websocket))

    await websocket.push_json({"post_type": "message"})
    action = await asyncio.wait_for(websocket.sent.get(), 0.1)
    await websocket.push_json(
        {
            "status": "ok",
            "retcode": 0,
            "echo": action["echo"],
        }
    )

    await asyncio.wait_for(completed.wait(), 0.1)
    await websocket.disconnect()
    await asyncio.wait_for(route, 0.1)

    self.assertEqual(connection.pending_action_count, 0)
```

- [ ] **步骤 4：运行测试确认先失败**

运行：

```bash
python3 -m unittest \
  backend.tests.test_onebot_api.OneBotWebSocketApiTests.test_action_response_resolves_while_event_is_running \
  -v
```

预期：`ERROR`，`completed.wait()` 抛出 `TimeoutError`。这证明动作响应已经进入
WebSocket，但接收循环仍被 `handle_event()` 阻塞。

## 任务 2：管理事件任务并保持接收循环畅通

**文件：**

- 修改：`backend/api/qq.py`
- 测试：`backend/tests/test_onebot_api.py`

- [ ] **步骤 1：增加事件任务完成回调**

在 `backend/api/qq.py` 中增加：

```python
import asyncio
from collections.abc import Coroutine
from typing import Any


def _finish_event_task(
    task: asyncio.Task[None],
    tasks: set[asyncio.Task[None]],
) -> None:
    tasks.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.error(
            "OneBot event failed: %s",
            type(exc).__name__,
        )


def _start_event_task(
    coroutine: Coroutine[Any, Any, None],
    tasks: set[asyncio.Task[None]],
) -> None:
    try:
        task = asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise
    tasks.add(task)
    task.add_done_callback(
        lambda completed: _finish_event_task(completed, tasks)
    )
```

该辅助函数与 `backend/api/app.py` 的后台任务创建模式保持一致。创建失败时主动
关闭协程，完成时消费异常。

- [ ] **步骤 2：把普通事件移入受管理任务**

在 `qq_websocket()` 完成连接后创建局部集合：

```python
event_tasks: set[asyncio.Task[None]] = set()
```

把同步事件处理：

```python
try:
    await runtime.qq_channel.handle_event(
        payload,
        self_id=self_id,
    )
except Exception as exc:
    logger.error(
        "OneBot event failed: %s",
        type(exc).__name__,
    )
```

替换为：

```python
_start_event_task(
    runtime.qq_channel.handle_event(
        payload,
        self_id=self_id,
    ),
    event_tasks,
)
```

`resolve_action_response()` 必须继续位于任务创建之前。

- [ ] **步骤 3：在断线时回收事件任务**

把路由的 `finally` 更新为：

```python
finally:
    await runtime.qq_connection.detach(ws)
    pending_tasks = tuple(event_tasks)
    for task in pending_tasks:
        task.cancel()
    if pending_tasks:
        await asyncio.gather(
            *pending_tasks,
            return_exceptions=True,
        )
```

先 `detach()`，让动作等待者收到 `onebot_disconnected`。随后取消模型调用、会话锁
或并发许可中仍未完成的事件任务。

- [ ] **步骤 4：运行自阻塞回归测试确认通过**

运行：

```bash
python3 -m unittest \
  backend.tests.test_onebot_api.OneBotWebSocketApiTests.test_action_response_resolves_while_event_is_running \
  -v
```

预期：`ok`，动作响应在事件任务运行期间被消费，待处理动作数量回到 `0`。

- [ ] **步骤 5：运行 OneBot API 现有测试**

运行：

```bash
python3 -m unittest backend.tests.test_onebot_api -v
```

预期：全部显示 `ok`。如果旧测试依赖同步执行顺序，只调整测试等待条件，不把生产
代码改回同步等待。

## 任务 3：覆盖断线清理与异常隔离

**文件：**

- 修改：`backend/tests/test_onebot_api.py`
- 测试：`backend/tests/test_onebot_api.py`

- [ ] **步骤 1：编写断线取消测试**

增加：

```python
async def test_disconnect_cancels_and_reaps_running_event_tasks(self):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle_event(payload, *, self_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runtime = runtime_for(
        ready_settings(),
        channel=SimpleNamespace(handle_event=handle_event),
    )
    websocket = QueuedWebSocket(runtime)
    route = asyncio.create_task(qq_websocket(websocket))

    await websocket.push_json({"post_type": "message"})
    await asyncio.wait_for(started.wait(), 0.1)
    await websocket.disconnect()
    await asyncio.wait_for(route, 0.1)

    await asyncio.wait_for(cancelled.wait(), 0.1)
    runtime.qq_connection.detach.assert_awaited_once_with(websocket)
```

- [ ] **步骤 2：强化异常隔离测试**

把现有 `test_channel_failure_isolated_from_following_event` 改为使用两个显式完成信号：

```python
async def test_channel_failure_isolated_from_following_event(self):
    second_completed = asyncio.Event()
    calls = 0

    async def handle_event(payload, *, self_id):
        nonlocal calls
        calls += 1
        if payload == {"event": 1}:
            raise RuntimeError("private failure")
        second_completed.set()

    runtime = runtime_for(
        ready_settings(),
        channel=SimpleNamespace(handle_event=handle_event),
    )
    websocket = QueuedWebSocket(runtime)
    route = asyncio.create_task(qq_websocket(websocket))

    await websocket.push_json({"event": 1})
    await websocket.push_json({"event": 2})
    await asyncio.wait_for(second_completed.wait(), 0.1)
    await websocket.disconnect()
    await asyncio.wait_for(route, 0.1)

    self.assertEqual(calls, 2)
    websocket.close.assert_not_awaited()
```

该测试证明第一个任务失败不会停止接收循环或阻止第二个事件。

- [ ] **步骤 3：运行生命周期测试**

运行：

```bash
python3 -m unittest \
  backend.tests.test_onebot_api.OneBotWebSocketApiTests.test_disconnect_cancels_and_reaps_running_event_tasks \
  backend.tests.test_onebot_api.OneBotWebSocketApiTests.test_channel_failure_isolated_from_following_event \
  -v
```

预期：2 个测试均显示 `ok`，且输出中没有
`Task exception was never retrieved`。

- [ ] **步骤 4：运行完整 OneBot 测试**

运行：

```bash
python3 -m unittest \
  backend.tests.test_onebot_api \
  backend.tests.test_onebot_connection \
  backend.tests.test_onebot_channel \
  -v
```

预期：全部显示 `ok`。

- [ ] **步骤 5：提交代码和测试**

```bash
git add backend/api/qq.py backend/tests/test_onebot_api.py
git commit -m "fix: 避免 OneBot 动作响应接收自阻塞"
```

## 任务 4：执行完整自动化验证

**文件：**

- 验证：`backend/`
- 验证：`desktop-app/`

- [ ] **步骤 1：检查 Python 语法**

运行：

```bash
python3 -m compileall -q backend
```

预期：退出码为 `0`，无输出。

- [ ] **步骤 2：运行全部后端测试**

运行：

```bash
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
```

预期：全部测试通过，末尾显示 `OK`。

- [ ] **步骤 3：运行桌面端测试**

运行：

```bash
npm test --prefix desktop-app
```

预期：全部测试通过，退出码为 `0`。

- [ ] **步骤 4：构建 Renderer**

运行：

```bash
npm run build:renderer --prefix desktop-app
```

预期：构建完成，退出码为 `0`。

- [ ] **步骤 5：执行差异检查**

运行：

```bash
git diff --check
git status --short
```

预期：`git diff --check` 无输出；工作树只包含尚未提交的计划勾选或验收文档。

## 任务 5：重新执行真实 QQ 验收

**文件：**

- 本地配置：`config/local/qq.env`（已被 Git 忽略）
- 本地配置：`qq-bot/data/`（已被 Git 忽略）

- [ ] **步骤 1：使用本地配置重启后端**

运行：

```bash
cd backend
set -a
source ../config/local/qq.env
set +a
python3 main.py
```

预期：后端监听 `127.0.0.1:8080`，NapCat 自动重新连接。

- [ ] **步骤 2：确认安全状态**

运行：

```bash
curl --silent --show-error --fail \
  http://127.0.0.1:8080/api/qq/status
```

预期：

```json
{
  "enabled": true,
  "state": "connected",
  "allowedGroupCount": 1,
  "allowedUserCount": 1
}
```

- [ ] **步骤 3：验收私聊**

由 `config/local/qq.env` 中已配置的允许用户私聊机器人，发送
`私聊回归测试`。

预期：收到且只收到 1 次回复；后端没有记录
`onebot_action_timeout`。

- [ ] **步骤 4：验收带 `@` 的群聊**

在 `config/local/qq.env` 中已配置的允许群中结构化 `@` 机器人并发送
`群聊回归测试`。

预期：收到且只收到 1 次回复；第一段引用原消息并 `@发送者`；后端没有记录
`onebot_action_timeout`。

- [ ] **步骤 5：验收未 `@` 的群聊**

在允许群中不 `@` 机器人，发送 `未艾特静默测试`。

预期：NapCat 收到该消息，但机器人不回复，后端也不调用模型。

## 任务 6：更新状态文档并保存验收结果

**文件：**

- 修改：`README.md`
- 修改：`docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md`
- 修改：`docs/superpowers/specs/2026-07-29-onebot-action-response-deadlock-design.md`
- 修改：`docs/superpowers/plans/2026-07-29-onebot-action-response-deadlock.md`

- [ ] **步骤 1：更新阶段状态**

把 OneBot QQ 渠道状态更新为：

```markdown
> 状态：自动化验证与真实 QQ / NapCat 联调均已完成
```

README 项目状态改为“QQ 私聊与群聊文字接入已通过真实 NapCat 联调，默认关闭”。

- [ ] **步骤 2：记录验证证据**

在本计划末尾增加：

```markdown
## 执行结果

- OneBot API 回归测试：通过。
- 后端完整测试：通过。
- 桌面端测试：通过。
- Renderer 构建：通过。
- 真实私聊：单次回复，无动作超时。
- 真实群聊 `@`：引用并 `@发送者`，单次回复，无动作超时。
- 真实群聊未 `@`：静默。
```

只记录通过数量和行为结论，不记录 Token、Cookie、QQ 消息正文或 NapCat 响应正文。

- [ ] **步骤 3：执行文档安全扫描**

运行：

```bash
rg -n \
  "T[O]DO|T[B]D|F[I]XME|g[h]o_|q[q]_password|ASSISTANT_QQ_ACCESS_TOKEN=[^<[:space:]]" \
  README.md \
  docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md \
  docs/superpowers/specs/2026-07-29-onebot-action-response-deadlock-design.md \
  docs/superpowers/plans/2026-07-29-onebot-action-response-deadlock.md
```

预期：没有占位符、GitHub Token、QQ 密码或真实 OneBot Token。

- [ ] **步骤 4：验证并提交文档**

运行：

```bash
git diff --check
git status --short
git add \
  README.md \
  docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md \
  docs/superpowers/specs/2026-07-29-onebot-action-response-deadlock-design.md \
  docs/superpowers/plans/2026-07-29-onebot-action-response-deadlock.md
git commit -m "docs: 记录 OneBot 真实联调结果"
```

预期：提交成功，忽略的本地 Token、QQ 登录数据和 SQLite 数据库不进入提交。

- [ ] **步骤 5：最终检查分支**

运行：

```bash
git status --short --branch
git log -3 --oneline
```

预期：工作树干净；最近提交包含代码修复、验收文档和本实现计划。
