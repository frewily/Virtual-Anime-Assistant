# DeepSeek 工具调用协议兼容设计

## 1. 背景与根因

项目已经具备供应商无关的 Tool Calling 编排器，内部工具名称使用分层命名，例如 system.current_time。开启工具调用后，真实 DeepSeek V4 请求返回服务错误；关闭工具调用时，普通聊天正常。

根因由代码与 DeepSeek 官方 Chat Completions 文档共同确认：

1. DeepSeek 工具名称只允许字母、数字、下划线和短横线，最长 64 个字符。内部名称 system.current_time 含句点，当前网关未做协议别名转换。
2. DeepSeek 思考模式发生工具调用后，下一轮请求必须回传该 assistant 消息的 reasoning_content。当前响应模型会忽略该字段，工具编排器也无法回传。

本阶段修复这两条协议差异，同时保持内部工具命名、权限状态机、SQLite 审计和其他 OpenAI 兼容供应商行为不变。

## 2. 目标与范围

### 2.1 包含

- 在 OpenAI 兼容网关边界生成合法、稳定、无冲突的供应商工具别名。
- 请求序列化时把内部工具名转换为供应商别名。
- 响应解析时把供应商别名严格映射回本次请求声明的内部工具名。
- 解析 DeepSeek reasoning_content，并只在同一轮工具编排的下一次模型请求中回传。
- 使用真实 DeepSeek V4 和低风险 system.current_time 工具完成一次端到端验收。
- 保持工具请求次数、工具调用次数、来源授权、参数验证和审计规则不变。

### 2.2 不包含

- 不把 DeepSeek 或其他供应商名称硬编码到应用编排层。
- 不关闭 DeepSeek 思考模式。
- 不显示、持久化、广播或记录 reasoning_content。
- 不修改高风险工具确认策略。
- 不新增电脑控制工具。
- 不放宽无工具请求、空回复、未知工具或畸形参数的协议校验。

## 3. 工具名称别名

### 3.1 供应商约束

供应商名称必须满足：

~~~text
^[A-Za-z0-9_-]{1,64}$
~~~

请求中的 tools、assistant.tool_calls 和 tool.name 必须使用同一份双向映射。

### 3.2 生成规则

网关在每次 complete() 调用开始时，根据 request.tools 构建映射：

- 合法且没有冲突的内部名称保持原样。
- 非法或超过 64 字符的名称先把非法字符序列替换为单个下划线。
- 别名后附内部名称 SHA-256 的前 8 个十六进制字符，避免不同内部名称清洗为同一结果。
- 前缀按 64 字符总长度截断。
- 如果候选名称仍与本次请求中的合法原名或已生成别名冲突，使用包含工具顺序编号和摘要的退避别名。
- request.tools 出现重复内部名称时，在发送网络请求前抛出 ModelConfigurationError。

示例：

~~~text
system.current_time → system_current_time_<8 位摘要>
~~~

别名只存在于单次模型请求边界。工具注册表、权限服务、SQLite 和审计继续使用 system.current_time。

### 3.3 响应反向映射

模型返回 tool_calls 时：

- function.name 必须存在于本次供应商别名映射中。
- 合法映射后创建的 ModelToolCall 使用内部名称。
- 未知名称、重复调用 ID、空名称或畸形参数继续抛出 ModelProtocolError。
- 不允许根据字符串相似度猜测工具，也不自动重试无工具请求。

## 4. 思考内容的内存边界

### 4.1 模型契约

ModelReply 增加可选 reasoning_content；ModelMessage 增加相同字段，但只允许 assistant 且同时存在 tool_calls 时使用。

约束如下：

- 必须是去除首尾空白后仍非空的字符串。
- 最长 64,000 个字符；超过限制按协议错误拒绝。
- 标准供应商不返回该字段时保持 None。
- 只有包含工具调用的响应才把 reasoning_content 放入 ModelReply；最终文字回复中的思考内容立即丢弃。

### 4.2 编排回传

工具编排器收到工具调用后，追加 assistant 消息时同时携带：

- tool_calls：内部工具名。
- reasoning_content：上一轮供应商返回的思考内容。

下一次 gateway.complete() 会使用同一套工具映射，把 assistant.tool_calls 和 tool.name 转换为供应商别名，并原样回传 reasoning_content。

工具轮次结束后，该字段随局部 messages 列表释放。

### 4.3 禁止外泄

reasoning_content 不得进入：

- StoredMessage.content。
- ModelCallRecord。
- SQLite 会话、消息、工具请求或审计表。
- HTTP 和 WebSocket 响应。
- 应用日志、异常文本和设置页。
- TTS 或 Live2D 消息。

## 5. 数据流

~~~text
内部工具定义 system.current_time
  → Gateway 生成安全别名
  → DeepSeek 返回安全别名 + reasoning_content
  → Gateway 映射回 system.current_time
  → 权限服务执行低风险工具
  → Orchestrator 追加 assistant 工具调用与 reasoning_content
  → Gateway 再次转换工具名并回传思考内容
  → DeepSeek 返回最终文字
  → 只保存和发布最终文字
~~~

## 6. 错误处理

以下情况必须在本地、发送前失败：

- 重复内部工具名称。
- 无法生成唯一合法别名。
- assistant 或 tool 消息引用未在 request.tools 声明的内部工具。

以下情况必须作为 ModelProtocolError 处理：

- 供应商返回未知工具别名。
- reasoning_content 类型错误、为空白或超过 64,000 字符。
- 工具调用 ID 重复。
- 工具参数不是 JSON 对象或超过现有限制。

错误消息继续保持稳定、脱敏，不包含请求负载、供应商响应正文、思考内容或 API Key。

## 7. 测试策略

### 7.1 模型契约

覆盖：

- reasoning_content 只能出现在 assistant 工具调用消息。
- 空白和超长思考内容被拒绝。
- ModelReply 最终输出规则保持不变。

### 7.2 网关

覆盖：

- system.current_time 转换为合法别名。
- 合法短名称保持不变。
- 超长名称、清洗冲突和退避别名仍唯一且不超过 64 字符。
- assistant.tool_calls 与 tool.name 使用相同别名。
- 响应别名反向映射为内部名称。
- 未知别名、重复内部工具和超长思考内容安全失败。
- 无工具请求不发送 tools 或 reasoning_content。
- 最终文字响应中的 reasoning_content 不向上返回。

### 7.3 编排器与持久化

覆盖：

- 第一轮工具调用的 reasoning_content 出现在第二轮 assistant 消息。
- 标准供应商没有 reasoning_content 时现有路径不变。
- 最终消息、模型调用记录和工具审计不包含思考内容。
- 3 次模型请求、4 个工具调用和权限来源限制保持通过。

### 7.4 完整与真实验收

- 运行全部后端测试与桌面端测试。
- 在 Web 设置中开启工具调用并重启后端。
- 使用 DeepSeek V4 请求当前时间。
- 只记录聊天 HTTP 状态、工具执行状态、模型调用次数和最终回复。
- 不读取、输出或保存 API Key、reasoning_content 或供应商错误正文。

## 8. 成功标准

- DeepSeek V4 可以实际调用 system.current_time 并返回包含当前时间的最终文字。
- 普通聊天、Live2D 自动朗读、QQ、记忆和工具权限测试无回归。
- reasoning_content 仅存在于一次工具编排的进程内局部状态。
- 工具调用关闭时请求负载和运行行为与当前版本一致。
