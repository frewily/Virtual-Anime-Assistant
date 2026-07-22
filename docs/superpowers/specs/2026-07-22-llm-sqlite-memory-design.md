# 大模型网关与 SQLite 记忆设计

> 状态：用户已批准
>
> 日期：2026-07-22
>
> 适用阶段：可扩展虚拟助手架构第 2 阶段

## 1. 背景

项目已经完成统一消息、响应、会话串行化和应用编排入口。当前 `AssistantApplication` 能够统一处理桌面聊天、交互事件和主动场景，但聊天回复仍为固定文本，会话状态也只存在于进程内。

本阶段在现有编排入口后增加两个稳定接缝：

1. 大模型网关：通过 OpenAI 兼容的 Chat Completions 协议接入云端或本地模型。
2. 本地持久化：使用 SQLite 保存会话、消息、模型调用元数据和用户明确授权的长期记忆。

本阶段不向模型开放电脑控制、QQ、文件系统或任意 Shell 工具。工具调用必须等待独立的权限策略和确认状态机完成，不能因接入大模型而提前绕过安全边界。

## 2. 已确认的决策

1. 大模型能力通过项目内部 `LanguageModelGateway` 接口暴露，应用层不依赖具体厂商 SDK。
2. 首个真实适配器使用项目已有的 `httpx`，调用 OpenAI 兼容的 `/chat/completions` 接口。
3. 首版采用非流式文字回复；流式输出、视觉输入和 Tool Calling 不在本阶段实现。
4. 模型地址、模型名称和 API Key 通过环境变量配置，不硬编码具体厂商或当前模型名称。
5. 未启用大模型时保留本地演示回复；已启用但调用失败时返回明确错误，不伪造成功。
6. SQLite 是当前桌面端的默认数据库，数据库操作通过 Repository 接口隔离，为未来迁移 MySQL 保留边界。
7. 会话记录默认保存在本地 SQLite。
8. 只有用户明确输入长期记忆命令或主动调用记忆管理 API 时，内容才进入长期记忆。
9. 普通聊天不会由模型自动提炼成长期记忆。
10. 用户可以查询和删除会话及长期记忆。
11. API Key 不写入 SQLite、YAML、日志或 Git。
12. 当前环境内置 SQLite 版本为 `3.50.4`，本阶段不默认启用 WAL；打包阶段验证运行时版本后再决定是否启用。

## 3. 目标与非目标

### 3.1 目标

- 将固定聊天回复替换为可配置的大模型回复，同时保留无配置时的可运行状态。
- 允许 OpenAI、DeepSeek、Ollama 和其他实现兼容协议的服务通过配置接入。
- 保存会话和消息，使应用重启后仍可恢复近期上下文。
- 提供可查看、可删除且必须由用户明确授权的长期记忆。
- 避免模型厂商 DTO、HTTP 错误和 SQLite SQL 泄漏到核心编排模块。
- 为模型超时、限流、协议错误、数据库失败和重复消息提供确定行为。
- 让模型网关、记忆策略和 Repository 能够使用测试替身独立验证。

### 3.2 非目标

- 不实现模型 Tool Calling、电脑控制、QQ 发送或高风险确认。
- 不实现流式生成、增量 TTS 或逐字驱动 Live2D。
- 不实现向量数据库、Embedding 或语义检索。
- 不实现模型自动判断并永久保存用户偏好。
- 不实现自动会话摘要；本阶段使用受预算限制的近期消息。
- 不实现多用户云端部署、跨设备同步或 MySQL 适配器。
- 不引入 LiteLLM、LangChain 或通用 Agent 框架。
- 不自动重试可能产生重复费用的模型请求。

## 4. 总体架构

```text
Desktop / WebSocket / 后续 QQ
              │
              ▼
       IncomingMessage
              │
              ▼
    AssistantApplication
       │              │
       │              ├── ConversationRepository
       │              └── MemoryRepository
       │                         │
       ▼                         ▼
LanguageModelGateway         SqliteStore
       │
       ▼
OpenAICompatibleGateway
       │
       ├── 云端兼容服务
       └── Ollama 等本地服务
```

`AssistantApplication` 只负责用例顺序，不直接发送 HTTP 请求或执行 SQL。模型网关负责协议差异，Repository 负责持久化差异，现有渠道适配器继续只处理输入输出协议转换。

## 5. 大模型网关

### 5.1 内部契约

领域或应用层定义与厂商无关的数据模型：

- `ModelMessage`：角色和文本内容。
- `ModelRequest`：系统指令、上下文消息、模型参数和关联 ID。
- `ModelReply`：回复文本、模型名称、结束原因、Token 用量和服务端请求 ID。
- `LanguageModelGateway`：异步 `complete(request)` 接口。

适配器不得把 OpenAI 兼容响应中的原始 `choices`、HTTP Response 或厂商异常对象返回给应用层。

首版接口只接受文本消息和以下角色：

- `system`
- `user`
- `assistant`

工具消息、多模态内容和流式事件在需要时通过新类型扩展，不能复用自由格式字典绕过校验。

### 5.2 OpenAI 兼容适配器

`OpenAICompatibleGateway` 使用 `httpx.AsyncClient` 请求：

```text
POST {base_url}/chat/completions
Authorization: Bearer {api_key}
Content-Type: application/json
```

请求体仅发送兼容范围内的字段：

- `model`
- `messages`
- `stream=false`
- 可选的 `temperature`
- 可选的输出长度限制

不发送 `tools`、`tool_choice`、厂商专用推理参数或自动存储参数。厂商专有能力以后通过独立适配器或显式扩展配置实现，不能污染通用契约。

`base_url` 由用户提供并去除末尾 `/`，可以是：

- 包含 `/v1` 的标准兼容地址。
- DeepSeek 等服务公布的兼容根地址。
- `http://127.0.0.1:11434/v1` 等本地地址。

项目不硬编码当前可用模型名称。模型名称变化时只更新环境变量，不修改代码。

### 5.3 配置

本阶段增加以下环境变量：

```env
ASSISTANT_LLM_ENABLED=false
ASSISTANT_LLM_BASE_URL=
ASSISTANT_LLM_API_KEY=
ASSISTANT_LLM_MODEL=
ASSISTANT_LLM_TIMEOUT_SECONDS=60
ASSISTANT_LLM_MAX_CONTEXT_MESSAGES=20
ASSISTANT_LLM_MAX_CONTEXT_CHARS=12000
```

配置规则：

- `ASSISTANT_LLM_ENABLED=false` 时，不要求模型配置，使用明确标记的本地演示网关。
- 启用后，`BASE_URL` 和 `MODEL` 必须存在，否则应用启动失败并输出不包含密钥的配置错误。
- `API_KEY` 对本地无鉴权服务可以为空；非空时才发送 Authorization Header。
- 超时和上下文预算必须有合理上下限，避免错误配置造成无限等待或过大请求。
- 日志只能记录启用状态、模型名称和脱敏后的服务标识，不能记录 API Key 或完整 Prompt。

生产安装包后续优先从操作系统密钥链读取 API Key。本阶段环境变量只是开发和首次集成接缝。

### 5.4 错误模型

网关将外部异常归一化为有限错误类型：

| 错误类型 | 典型来源 | 应用行为 |
|---|---|---|
| `ModelConfigurationError` | 缺少地址或模型名称 | 启动失败或返回配置错误 |
| `ModelAuthenticationError` | HTTP 401 / 403 | 返回鉴权错误，不记录响应正文 |
| `ModelRateLimitError` | HTTP 429 | 提示稍后重试 |
| `ModelTimeoutError` | 连接或读取超时 | 返回超时错误 |
| `ModelProtocolError` | 响应缺少有效文本 | 返回协议错误并记录结构摘要 |
| `ModelServiceError` | 其他 5xx 或网络异常 | 返回服务不可用 |

首版不自动重试模型请求。自动重试可能产生重复费用或重复生成，应在后续加入幂等策略和可见的重试状态后再启用。

### 5.5 本地演示网关

禁用大模型时，使用实现相同契约的 `DemoLanguageModelGateway` 返回固定演示内容。演示回复必须在状态接口或日志中标记为 `demo`，避免用户误以为真实模型已接入。

启用真实网关后，如果调用失败，不允许自动退回演示回复，否则会掩盖故障并生成看似成功的虚假回答。

## 6. SQLite 持久化

### 6.1 存储位置

数据库文件名使用 `assistant.db`。

路径优先级：

1. `ASSISTANT_DATA_DIR` 指定的目录。
2. Electron 启动 sidecar 时传入的用户数据目录。
3. 开发环境中的平台用户数据目录。

数据库不能放入仓库、Electron 资源目录或签名后的 `.app` 内容。项目只提交空目录占位或配置示例，不提交真实数据库。

### 6.2 Repository 边界

应用层依赖以下接口：

- `ConversationRepository`
  - 创建或更新会话。
  - 按消息 ID 判断是否已处理。
  - 保存用户或助手消息。
  - 读取近期消息。
  - 查询和删除会话。
- `MemoryRepository`
  - 保存明确授权的长期记忆。
  - 按用户身份读取有效记忆。
  - 查询和删除长期记忆。
- `ModelCallRepository`
  - 记录模型调用的结果元数据。

接口使用领域模型，不返回数据库行或 SQL 游标。未来增加 MySQL 适配器时，只替换 Repository 和事务实现。

### 6.3 表结构

#### `schema_migrations`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `version` | INTEGER | PRIMARY KEY | 迁移版本 |
| `name` | TEXT | NOT NULL | 迁移名称 |
| `applied_at` | TEXT | NOT NULL | UTC 时间 |

#### `conversations`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | `conversation_id` |
| `source` | TEXT | NOT NULL | Desktop、QQ 等来源 |
| `owner_id` | TEXT | NOT NULL | 渠道内用户 ID |
| `title` | TEXT | NULL | 可选会话标题 |
| `created_at` | TEXT | NOT NULL | UTC 时间 |
| `updated_at` | TEXT | NOT NULL | UTC 时间 |

#### `messages`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | 内部消息或响应 ID |
| `conversation_id` | TEXT | FOREIGN KEY | 所属会话 |
| `correlation_id` | TEXT | NULL | 关联的输入消息 ID |
| `role` | TEXT | NOT NULL | `user`、`assistant` 或 `system` |
| `content` | TEXT | NOT NULL | 消息文本 |
| `model` | TEXT | NULL | 生成该回复的模型 |
| `status` | TEXT | NOT NULL | `completed` 或 `failed` |
| `created_at` | TEXT | NOT NULL | UTC 时间 |

`messages.id` 同时承担幂等键。相同输入消息重发时，应用读取已有结果或明确返回重复状态，不再次调用模型。

#### `memory_items`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | 记忆 ID |
| `source` | TEXT | NOT NULL | 创建记忆的渠道 |
| `owner_id` | TEXT | NOT NULL | 记忆所属用户 |
| `content` | TEXT | NOT NULL | 用户明确要求保存的内容 |
| `normalized_content` | TEXT | NOT NULL | 用于确定性去重和删除 |
| `source_message_id` | TEXT | NULL | 产生记忆的消息 |
| `created_at` | TEXT | NOT NULL | UTC 时间 |
| `updated_at` | TEXT | NOT NULL | UTC 时间 |

`source + owner_id + normalized_content` 建立唯一索引，避免同一用户重复保存完全相同的记忆。不同 QQ 用户或不同身份之间不能共享长期记忆。

#### `model_calls`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | TEXT | PRIMARY KEY | 调用 ID |
| `message_id` | TEXT | FOREIGN KEY | 触发调用的用户消息 |
| `model` | TEXT | NOT NULL | 配置的模型名称 |
| `status` | TEXT | NOT NULL | `succeeded` 或受控错误类型 |
| `latency_ms` | INTEGER | NOT NULL | 调用耗时 |
| `prompt_tokens` | INTEGER | NULL | 服务返回的输入 Token |
| `completion_tokens` | INTEGER | NULL | 服务返回的输出 Token |
| `provider_request_id` | TEXT | NULL | 脱敏后的服务请求 ID |
| `created_at` | TEXT | NOT NULL | UTC 时间 |

该表不保存 API Key、Authorization Header、完整 Prompt、完整 HTTP 响应或服务端错误正文。

### 6.4 SQLite 连接策略

首版使用 Python 标准库 `sqlite3`，不增加 ORM。`SqliteStore` 统一负责：

- 初始化连接和执行迁移。
- 开启 `PRAGMA foreign_keys=ON`。
- 设置有限的 `busy_timeout`。
- 使用短事务完成单次写入。
- 将长时间的模型网络调用放在事务外。
- 在应用关闭时提交或回滚并关闭连接。

本阶段保留默认回滚日志模式，不默认执行 `PRAGMA journal_mode=WAL`。SQLite 官方说明 WAL 可以提高读写并发，但也要求同机共享状态，且部分版本存在已披露的 WAL Reset 并发缺陷。当前开发环境的 `3.50.4` 不在官方列出的修复版本中。桌面单用户、单后端进程的写入量很小，默认日志模式足以满足本阶段需求。

打包阶段需要检查 Python sidecar 实际捆绑的 SQLite 版本。确认版本已修复并完成并发、断电恢复和备份测试后，才允许通过配置启用 WAL。

### 6.5 迁移策略

迁移按递增整数版本执行，每个迁移只执行一次并记录到 `schema_migrations`。初始化流程必须满足：

1. 新数据库可以从版本 0 升级到当前版本。
2. 已升级数据库重复启动不会重复执行迁移。
3. 单个迁移失败时事务回滚，应用拒绝以不完整 Schema 继续运行。
4. 迁移文件或代码进入 Git 后不能原地修改，后续变更增加新版本。

本阶段不引入 Alembic，因为没有 SQLAlchemy 依赖且 Schema 较小。未来采用 ORM 或增加 MySQL 时再评估迁移工具。

## 7. 长期记忆规则

### 7.1 明确授权

聊天入口只识别以下全角或半角冒号命令：

```text
记住：我喜欢咖啡
记住: 我喜欢咖啡
忘记：我喜欢咖啡
忘记: 我喜欢咖啡
```

命令必须位于消息开头，冒号后的内容不能为空。普通句子中的「记住」或「忘记」不触发持久化。

记忆命令由本地确定性解析器处理：

- `记住`：保存内容，并返回本地确认响应。
- `忘记`：按当前用户和标准化后的完整内容删除，并返回删除结果。

记忆命令及其内容不发送给云端模型。这样既减少隐私暴露，也避免模型自行决定写入结果。

### 7.2 查询与删除

本阶段提供以下本地 API：

- `GET /api/memories`：查询当前本地用户的长期记忆。
- `POST /api/memories`：通过明确的 UI 操作创建长期记忆。
- `DELETE /api/memories/{memory_id}`：按 ID 永久删除长期记忆。
- `GET /api/conversations/{conversation_id}/messages`：查询会话消息。
- `DELETE /api/conversations/{conversation_id}`：永久删除会话及其消息。

删除长期记忆时不在审计表保留原始内容。删除会话使用外键级联删除消息和相关模型调用元数据，确保用户的删除意图实际生效。

### 7.3 Prompt 注入边界

长期记忆属于用户提供的不可信数据，不能直接拼接为系统指令。上下文构建器需要：

1. 使用结构化、明确分隔的区域承载记忆。
2. 在系统指令中说明记忆仅用于参考，不能覆盖系统规则或授权工具。
3. 对记忆内容进行长度限制和 JSON 安全序列化。
4. 工具权限阶段仍由本地策略判断风险，不能因为记忆内容而跳过确认。

长期记忆只用于改善回复，不具备授权能力。

## 8. 上下文组装

模型上下文按以下顺序构建：

1. 角色和安全系统指令。
2. 当前用户的有效长期记忆，标记为不可信参考数据。
3. 当前会话近期的用户和助手消息。
4. 本次用户输入。

裁剪规则：

- 优先保留系统指令和本次用户输入。
- 从最新消息向前选择，最多保留配置的消息数。
- 同时限制历史文本总字符数，避免单条超长消息挤占全部预算。
- 不假设具体厂商 tokenizer；服务返回 Token 用量时只用于记录和后续调优。
- 长期记忆单独设置数量和字符上限，不能无限增长后全部注入。

自动会话摘要推迟到后续阶段。摘要需要额外模型调用、失败恢复和可追溯更新策略，当前先使用确定性的近期上下文完成闭环。

## 9. 完整处理流程

### 9.1 普通聊天

```text
接收 IncomingMessage
        ↓
校验文本与 message_id
        ↓
按 conversation_id 获取现有会话锁
        ↓
检查 message_id 是否已经处理
        ├── 是：返回已保存响应或重复状态
        └── 否
             ↓
保存或更新 conversation
             ↓
保存用户消息
             ↓
读取近期消息与当前用户长期记忆
             ↓
构建 ModelRequest
             ↓
在数据库事务外调用 LanguageModelGateway
             ↓
保存模型调用元数据与助手消息
             ↓
生成并发布 AssistantResponse
```

同一会话继续由 `SessionRegistry` 串行处理。不同会话可以并发调用模型，但 SQLite 写事务必须短小且由存储层协调。

### 9.2 记忆命令

```text
接收聊天消息
      ↓
MemoryCommandParser 匹配命令
      ├── 记住：写入 MemoryRepository
      └── 忘记：从 MemoryRepository 删除
      ↓
保存用户消息和本地确认回复
      ↓
发布 AssistantResponse
```

该流程不进入大模型网关。

### 9.3 模型调用失败

用户消息在调用模型前保存。调用失败后：

1. `model_calls` 写入受控错误类型和耗时。
2. 不保存虚假的助手消息。
3. 返回 `ResponseKind.ERROR`，携带适合用户理解的简短提示。
4. 日志记录关联 ID、错误类型和脱敏后的状态，不记录 Prompt、密钥或完整错误正文。
5. 用户可以再次发送或后续使用显式重试功能。

数据库不可写时，不调用模型，避免产生无法关联和无法审计的外部费用。

## 10. API 与兼容性

现有 `POST /api/chat/message` 和 WebSocket 消息格式保持兼容。内部响应增加模型错误时，HTTP 入口应返回明确的非 2xx 状态或结构化错误；WebSocket 通过 `type=error` 发送统一错误响应。

记忆和会话管理 API 只监听本机后端地址。正式安装包增加本地访问令牌后，这些接口必须同样鉴权，不能因属于本机接口而跳过访问控制。

桌面端本阶段不要求完成完整记忆管理界面，但 API 和后端测试必须先完成。后续 UI 可以在不修改 Repository 和记忆规则的情况下接入。

## 11. 测试策略

### 11.1 单元测试

- `LanguageModelGateway` 测试替身驱动应用编排，不访问真实模型服务。
- OpenAI 兼容适配器覆盖成功、鉴权失败、限流、超时、5xx 和响应缺少文本。
- 配置覆盖禁用、启用但缺少必填项、无鉴权本地服务和边界值。
- 上下文构建器覆盖顺序、消息数上限、字符上限和长期记忆隔离。
- 记忆命令解析器覆盖全角冒号、半角冒号、空内容和普通句子误匹配。
- Repository 使用临时 SQLite 数据库验证迁移、外键、幂等和级联删除。
- 不同用户的长期记忆不能互相读取或删除。

### 11.2 集成测试

- HTTP 和 WebSocket 均经过同一个应用编排入口调用模型替身。
- 应用重启后可以恢复会话历史和长期记忆。
- 重复 `message_id` 不产生第 2 次模型调用。
- 记忆命令不调用模型网关。
- 模型失败后用户消息和错误元数据仍可查询。
- 删除会话或记忆后，相关内容不再进入上下文。

### 11.3 验证命令

实施完成后至少运行：

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -p 'test_*.py'
npm --prefix desktop-app test
npm --prefix desktop-app run build:renderer
git diff --check
```

真实模型冒烟测试需要用户主动提供自己的服务地址、模型名称和密钥，不作为默认自动化测试的一部分。

## 12. 可观测性与隐私

允许记录：

- `correlation_id` 和内部调用 ID。
- 模型名称、成功或受控错误类型、耗时和 Token 用量。
- 会话消息数量和裁剪数量。
- 数据库迁移版本。

禁止记录：

- API Key、Authorization Header 和 Cookie。
- 完整 Prompt、长期记忆内容和聊天正文。
- 模型服务返回的完整错误正文。
- 用户数据目录中的完整敏感路径。

未来增加可选诊断日志时，必须默认关闭，并明确告知可能包含的隐私范围。

## 13. 实施顺序与 Git 检查点

本设计批准后单独编写实施计划。建议按以下顺序形成可回退提交：

1. 大模型领域契约、配置和测试替身。
2. OpenAI 兼容适配器及错误归一化。
3. SQLite 初始化、迁移和 Repository。
4. 长期记忆命令与管理 API。
5. 对话上下文组装和 `AssistantApplication` 集成。
6. HTTP、WebSocket、配置示例和文档收尾。

每个检查点必须先运行对应测试和 `git diff --check`，只提交该检查点相关文件。

## 14. 验收标准

- 未配置大模型时，项目仍能启动并清楚显示演示模式。
- 配置任意符合约定的 OpenAI 兼容服务后，桌面聊天可以获得真实模型回复。
- 更换 `base_url` 和 `model` 不需要修改应用层、渠道或 Live2D 代码。
- 会话消息在应用重启后仍存在，并按预算进入后续上下文。
- 只有明确记忆命令或记忆管理 API 能创建长期记忆。
- 记忆命令不会访问云端模型。
- 用户能够查看和永久删除自己的会话及长期记忆。
- 不同用户的记忆严格隔离。
- 模型失败不会产生伪造回复，也不会泄露密钥或完整隐私内容。
- 重复输入消息不会产生重复模型调用。
- 数据库实现可以由测试内存适配器替换，未来 MySQL 迁移不需要修改应用编排。
- 自动化测试、Renderer 构建和 `git diff --check` 全部通过。

## 15. 参考资料

- [OpenAI API Reference](https://platform.openai.com/docs/api-reference/backward-compatibility)
- [OpenAI API 数据控制](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)
- [DeepSeek API Docs](https://api-docs.deepseek.com/)
- [Ollama OpenAI Compatibility](https://docs.ollama.com/api/openai-compatibility)
- [SQLite Write-Ahead Logging](https://www.sqlite.org/wal.html)
- [SQLite Foreign Key Support](https://www.sqlite.org/foreignkeys.html)
- [SQLite Backup API](https://www.sqlite.org/backup.html)
