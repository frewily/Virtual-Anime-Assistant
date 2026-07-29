# OneBot 动作响应自阻塞修复设计

> 状态：设计已获批准，待用户审查书面规格
>
> 适用分支：`codex/onebot-channel`

## 1. 背景

真实 QQ / NapCat 联调已经验证以下链路：

- NapCat 可以通过 OneBot 11 反向 WebSocket 连接 FastAPI。
- 私聊消息可以进入统一应用服务，并由 NapCat 实际发送回复。
- 群聊结构化 `@机器人` 可以触发回复，第一段包含引用原消息和 `@发送者`。

联调同时发现，后端在两次成功发送后均记录
`onebot_action_timeout`。NapCat 已经执行动作，但后端没有及时消费带
`echo` 的动作响应。

## 2. 根因

`qq_websocket()` 当前在唯一的 WebSocket 接收循环中直接执行：

```python
await runtime.qq_channel.handle_event(payload, self_id=self_id)
```

`handle_event()` 最终调用 `OneBotConnectionManager.send_action()`。该方法发送
动作后，会等待同一 `echo` 对应的响应。

动作响应也由 `qq_websocket()` 的接收循环读取并交给
`resolve_action_response()`。接收循环尚未结束 `handle_event()`，因此无法读取
响应，形成自阻塞：

```text
接收事件
  → 等待 handle_event()
    → 等待 send_action() 的 echo 响应
      → echo 响应等待接收循环读取
        → 接收循环仍在等待 handle_event()
```

超时后接收循环恢复，NapCat 已经执行的动作不会自动重试，但后端会留下错误的
失败记录。

## 3. 目标

1. WebSocket 接收循环始终能够继续读取动作响应。
2. 入站事件仍复用现有白名单、限速、幂等、会话串行化和全局并发限制。
3. 事件处理异常只记录稳定错误类型，不泄露消息、Token 或响应正文。
4. WebSocket 断开时不遗留失去所有者的事件任务。
5. 不改变 OneBot 动作格式、`echo` 生成规则或失败后不重试的约定。

## 4. 非目标

- 不增加新的 QQ 消息类型。
- 不实现主动发送、群发或定时消息。
- 不修改现有白名单和群聊 `@` 触发规则。
- 不引入消息队列、独立 QQ 网关进程或新的常驻线程。

## 5. 方案比较

### 5.1 受管理的事件任务（采用）

接收循环把非响应帧交给独立的 `asyncio.Task`。循环本身立即返回
`receive_text()`，因此可以继续读取并解析 `echo` 响应。

优点：

- 修改范围集中在 FastAPI 的 OneBot WebSocket 适配层。
- 复用 `OneBotChannel` 已有的会话串行化和全局并发限制。
- 不改变连接管理器和渠道接口。
- 可以显式管理断线清理和异常日志。

代价：

- WebSocket 路由需要维护当前连接拥有的任务集合。
- 测试必须覆盖任务回收和异常消费，避免出现
  `Task exception was never retrieved`。

### 5.2 单独事件队列和工作循环（不采用）

接收循环只负责读帧，把事件写入队列，由常驻工作循环消费。

该方案边界清晰，但会增加队列关闭、背压、工作循环生命周期和异常传播逻辑。
现有 `OneBotChannel` 已经提供全局并发限制，本阶段引入队列属于重复控制。

### 5.3 不等待动作响应（不采用）

发送动作后立即返回可以绕过自阻塞，但会丢失动作失败和超时信息，破坏现有可靠
响应关联设计。

## 6. 详细设计

### 6.1 事件任务集合

每个已认证的 WebSocket 连接在 `qq_websocket()` 内维护一个局部
`set[asyncio.Task[None]]`。只有无法由
`resolve_action_response()` 处理的合法 JSON 对象才创建事件任务。

任务创建后立即加入集合，并注册完成回调。完成回调负责：

1. 从集合移除任务。
2. 消费任务结果。
3. 忽略由连接关闭导致的 `CancelledError`。
4. 对其他异常记录稳定的异常类型，不记录事件正文。

### 6.2 接收顺序

每帧按以下顺序处理：

1. 读取文本帧并解析 JSON。
2. 拒绝非对象帧，保留现有连续无效帧计数。
3. 先调用 `resolve_action_response()`。
4. 已匹配 `echo` 时立即进入下一次接收。
5. 其他对象作为 OneBot 事件创建受管理任务。

该顺序保证动作响应不会进入事件解析器。

### 6.3 断线清理

连接退出时执行以下顺序：

1. 调用 `OneBotConnectionManager.detach()`，使等待中的动作收到
   `onebot_disconnected`。
2. 取消仍未完成的事件任务。
3. 使用 `asyncio.gather(..., return_exceptions=True)` 等待全部任务结束。

先 `detach()` 可以让正在等待动作响应的任务得到明确连接错误。后续取消负责处理
仍在模型、会话锁或并发许可中等待的任务。

### 6.4 并发与顺序

路由层允许多个事件任务同时存在，但业务顺序仍由现有组件约束：

- 相同 QQ 会话由 `SessionRegistry` 串行处理。
- 不同会话受 `ASSISTANT_QQ_MAX_CONCURRENCY` 限制。
- 重复消息由现有幂等机制拦截。

路由层不新增第二套业务并发限制。

### 6.5 错误处理

- 事件任务异常继续使用 `OneBot event failed: <ExceptionType>` 格式。
- `OneBotChannelError` 仍只包含稳定错误码。
- 动作超时不自动重试，避免 NapCat 已发送但响应丢失时产生重复消息。
- 未匹配的 `echo` 保持安全忽略。

## 7. 测试设计

### 7.1 回归测试

增加一个真实路由级 WebSocket 回归测试：

1. 建立已鉴权的 OneBot WebSocket。
2. 发送允许的私聊事件。
3. 接收后端发出的 `send_private_msg` 动作。
4. 在事件处理尚未结束时回送带相同 `echo` 的成功响应。
5. 断言动作等待正常结束，且没有
   `onebot_action_timeout`。

该测试在修复前必须因自阻塞而失败，修复后通过。

### 7.2 生命周期测试

补充以下场景：

- 动作响应优先于普通事件处理。
- WebSocket 断开会取消并回收未完成事件任务。
- 事件任务抛出异常后不会关闭主 WebSocket 接收循环。
- 未匹配 `echo` 不会被误当作成功响应。

### 7.3 完整验证

实现后运行：

```bash
python3 -m unittest backend.tests.test_onebot_api -v
python3 -m unittest backend.tests.test_onebot_connection -v
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
npm test --prefix desktop-app
npm run build:renderer --prefix desktop-app
git diff --check
```

随后重新执行真实 QQ 验收：

1. 允许用户私聊，无需 `@`，收到 1 次回复。
2. 允许群内结构化 `@机器人`，收到带引用和 `@发送者` 的 1 次回复。
3. 允许群内不 `@机器人`，机器人保持沉默。
4. 后端不再记录 `onebot_action_timeout`。

## 8. 完成标准

- 回归测试能证明接收循环可在事件处理期间消费动作响应。
- 所有事件任务均被回收，断线后没有后台任务泄漏。
- 现有 OneBot、后端和桌面端测试全部通过。
- 真实私聊和群聊回复各发送 1 次，不产生重复回复。
- 未 `@` 的群聊消息保持静默。
- 后端不再错误记录 `onebot_action_timeout`。
