# OneBot 11 QQ 渠道设计

> 状态：自动化验证与真实 QQ / NapCat 联调均已完成
>
> 适用阶段：可扩展虚拟助手架构的 QQ / OneBot 渠道阶段

## 1. 背景

项目已经具备统一消息与响应模型、会话串行化、大模型网关、SQLite 会话与记忆、工具权限状态机，以及 Electron + Live2D 桌面运行时。旧 Java 分支曾提供基础 QQ 收发、OneBot WebSocket 和 NapCat Docker 配置，但该实现会绕过当前 Python 模块化架构，也缺少鉴权、白名单、限速、可靠的响应关联和渠道隔离。

本阶段在现有 `AssistantApplication` 外增加 OneBot 11 渠道适配器。NapCat 作为 WebSocket 客户端，主动连接 FastAPI 的 `/ws/qq`。QQ 消息必须先经过鉴权、白名单、触发规则和协议解析，再转换为统一 `IncomingMessage`；回复继续复用现有对话、记忆、模型和幂等逻辑。

## 2. 目标

1. 支持 OneBot 11 反向 WebSocket 的私聊和群聊文字消息。
2. 私聊无需 `@`；群聊只有在白名单群内 `@机器人` 时触发。
3. 群聊回复引用原消息并 `@发送者`；私聊直接回复文字。
4. 群和私聊用户分别使用白名单，未授权目标不能调用模型。
5. 共享令牌只从环境变量读取，通过 `Authorization: Bearer` 请求头校验。
6. QQ 消息复用统一应用服务、SQLite 会话、长期记忆和模型错误处理。
7. QQ 断线、配置错误或协议错误不影响 Electron、Live2D 和本地聊天。
8. NapCat Docker 只作为可选开发配套，不成为桌面助手的运行依赖。

## 3. 非目标

本阶段不实现以下能力：

- 图片、语音、视频、文件、位置、合并转发等富媒体处理。
- 好友申请、加群申请、通知、撤回、禁言、踢人或群管理。
- 主动群发、定时 QQ 消息或由模型任意调用的 QQ 发送工具。
- 自动扫码、QQ 密码保存、Cookie 提取或 NapCat WebUI 自动配置。
- 多个 QQ 机器人账号同时连接。
- 跨进程 QQ 网关、消息队列或云端多用户部署。
- 出站动作的自动重试；超时后重发可能造成重复消息。

## 4. 方案选择

### 4.1 采用：进程内反向 WebSocket 适配器

在 FastAPI 进程内实现 OneBot 适配器：

- 优点：直接复用 `AssistantApplication`，无需增加内部 HTTP、额外鉴权和新进程生命周期。
- 优点：现有会话、记忆、模型和错误归一化逻辑保持单一来源。
- 优点：以后仍可沿渠道接口拆成独立进程。
- 代价：QQ 协议处理与主后端共享进程，需要严格隔离异常和连接状态。

### 4.2 不采用：独立 QQ 网关进程

故障隔离更强，但会立即引入内部 API、进程管理、部署、打包和二次鉴权，超出首版需要。

### 4.3 不采用：HTTP 回调接收 + HTTP API 发送

实现简单，但收发使用两套连接，状态、响应关联和错误处理更分散。NapCat 官方支持反向 WebSocket 双向通信，本阶段不需要额外 HTTP 通道。

## 5. 总体架构

```text
NapCat WebSocket 客户端
        ↓
FastAPI /ws/qq
        ↓
OneBotConnection
  鉴权、单连接、echo 响应关联
        ↓
OneBotEventParser
  事件校验、文字与 at 段解析
        ↓
OneBotChannel
  白名单、触发规则、幂等键、限速
        ↓
IncomingMessage(source=qq)
        ↓
AssistantApplication.process()
  会话、记忆、模型、SQLite
        ↓
AssistantResponse
        ↓
OneBotChannel
  群聊：reply + at + text
  私聊：text
        ↓
OneBotConnection.send_action()
        ↓
NapCat / QQ
```

QQ 适配器不能直接调用模型、记忆 Repository、工具处理器或 Electron WebSocket。其职责只包括渠道协议、准入规则和输入输出转换。

## 6. 模块与文件职责

```text
backend/
├── channels/
│   └── onebot/
│       ├── __init__.py       # 公开稳定类型
│       ├── config.py         # 环境变量解析与安全默认值
│       ├── models.py         # OneBot 事件、消息段和动作响应模型
│       ├── parser.py         # 不可信 JSON 到领域输入的纯解析
│       ├── policy.py         # 白名单、触发规则和限速
│       ├── connection.py     # 单连接、echo、超时和断线清理
│       └── channel.py        # IncomingMessage 与 AssistantResponse 转换
├── api/
│   └── qq.py                 # /ws/qq 与只读状态 API
└── core/
    └── runtime.py            # 可选 QQ 组件装配与生命周期

qq-bot/
├── docker-compose.yml        # 可选 NapCat 开发环境
├── .env.example              # 非敏感镜像与 UID/GID 示例
└── README.md                 # WebUI、Token、反向 WS 和扫码步骤
```

每个模块保持单一职责。协议模型与解析器不依赖 FastAPI；白名单和限速不依赖 WebSocket；连接管理不依赖对话业务；渠道编排通过明确接口调用应用服务。

## 7. 配置

环境变量如下：

| 变量 | 默认值 | 约束 |
|---|---:|---|
| `ASSISTANT_QQ_ENABLED` | `false` | 支持 `1/true/yes/on` 和 `0/false/no/off` |
| `ASSISTANT_QQ_ACCESS_TOKEN` | 空 | 启用时必填，去除首尾空格后长度为 16～512 |
| `ASSISTANT_QQ_ALLOWED_GROUP_IDS` | 空 | 逗号分隔的正整数 QQ 群号 |
| `ASSISTANT_QQ_ALLOWED_USER_IDS` | 空 | 逗号分隔的正整数 QQ 用户号 |
| `ASSISTANT_QQ_RATE_PER_MINUTE` | `10` | 每个发送者每分钟 1～120 条 |
| `ASSISTANT_QQ_RATE_BURST` | `2` | 瞬时容量 1～20，不能大于每分钟限额 |
| `ASSISTANT_QQ_MAX_CONCURRENCY` | `4` | 不同 QQ 会话的全局并发上限 1～32 |
| `ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS` | `10` | OneBot 动作等待时间 1～60 秒 |

默认关闭 QQ 渠道。启用时必须配置有效 Token，并至少配置 1 个允许群或允许用户。配置不完整时，QQ 组件进入 `misconfigured` 状态，但主应用继续启动；`/ws/qq` 拒绝连接，只读状态 API 返回安全状态，不返回原始配置值。

白名单群和用户相互独立。用户出现在允许群内，不代表该用户获准私聊机器人。

## 8. WebSocket 鉴权与连接生命周期

NapCat 连接 `/ws/qq` 时必须提供：

```http
Authorization: Bearer <ASSISTANT_QQ_ACCESS_TOKEN>
X-Self-ID: <机器人 QQ 号>
```

服务端使用常量时间比较校验 Token。Token 缺失或错误时以策略违规关闭连接；日志只记录 `onebot_authentication_failed`。`X-Self-ID` 必须是正整数，用于识别机器人自身和构造稳定消息编号。

同一时刻只允许 1 条活动连接：

- 没有活动连接时接受已鉴权连接。
- 已有活动连接时拒绝第二条连接，不替换旧连接。
- 连接关闭后，原子清理活动连接和所有尚未完成的 `echo` 等待者。
- 断线中的出站动作返回稳定的 `onebot_disconnected`。
- 动作超时返回 `onebot_action_timeout`，不自动重发。

## 9. 入站事件与消息解析

首版只接受：

```json
{
  "post_type": "message",
  "message_type": "private | group",
  "self_id": 123456,
  "user_id": 234567,
  "group_id": 345678,
  "message_id": 456789,
  "message": []
}
```

规则如下：

1. `self_id` 必须等于握手时的 `X-Self-ID`。
2. `user_id`、`group_id` 和 `message_id` 必须是正整数；非法事件安全忽略。
3. `user_id == self_id` 的自发消息始终忽略。
4. `post_type` 不是 `message` 的通知、请求和心跳不进入应用服务。
5. 消息段数组只读取 `text` 和 `at`；其他段不下载、不解析。
6. 私聊允许 OneBot 字符串消息并按纯文本处理。
7. 群聊必须使用消息段数组，以结构化 `at` 段判断是否提及机器人；字符串形式的群消息安全忽略，避免不完整解析 CQ 码。
8. 移除指向机器人的 `at` 段后合并文字并去除首尾空白；结果为空时忽略。
9. 最终文字继续受 `ChatContent` 的 4000 字符上限约束。

稳定内部消息编号：

```text
qq:{self_id}:{message_id}
```

会话和用户隔离：

```text
私聊：qq:private:{user_id}
群聊：qq:group:{group_id}:user:{user_id}
```

群聊按「群 + 用户」隔离会话，避免不同群成员共享上下文、长期记忆或 SQLite 会话所有权。

## 10. 白名单、触发与限速

准入顺序固定为：

1. 校验事件结构和机器人账号。
2. 过滤自发消息和非文字事件。
3. 私聊检查用户白名单；群聊检查群白名单。
4. 群聊检查是否存在指向机器人的结构化 `at` 段。
5. 检查稳定消息编号是否已经由应用层处理。
6. 执行发送者令牌桶限速。
7. 获取 QQ 渠道全局并发许可。
8. 调用统一应用服务。

限速键使用 `self_id + user_id`，群聊和私聊共享同一用户额度。超过限额的事件不调用模型、不写入聊天消息，也不自动发送警告，防止机器人在攻击流量下继续刷屏。令牌桶使用单调时钟，并定期清除空闲条目。

同一会话仍由现有 `SessionRegistry` 串行处理；全局并发限制只约束不同 QQ 会话。

## 11. 应用服务与输出隔离

现有 `AssistantApplication.handle()` 会处理消息并发布给桌面响应订阅者。QQ 直接调用该方法会把 QQ 回复误发到 Electron。

本阶段将应用入口拆成：

```python
async def process(message: IncomingMessage) -> AssistantResponse:
    """执行业务编排，但不发布到任何具体输出渠道。"""

async def handle(message: IncomingMessage) -> AssistantResponse:
    """调用 process()，然后发布给现有桌面响应订阅者。"""
```

Desktop HTTP、Desktop WebSocket、交互和场景继续调用 `handle()`。OneBot 渠道调用 `process()`，再由自身发送 QQ 回复。这样 QQ 故障不会进入桌面广播，QQ 内容也不会意外显示在本机 Live2D 窗口。

## 12. 出站动作

私聊动作：

```json
{
  "action": "send_private_msg",
  "params": {
    "user_id": 234567,
    "message": [
      {"type": "text", "data": {"text": "回复内容"}}
    ]
  },
  "echo": "唯一动作编号"
}
```

群聊动作：

```json
{
  "action": "send_group_msg",
  "params": {
    "group_id": 345678,
    "message": [
      {"type": "reply", "data": {"id": "456789"}},
      {"type": "at", "data": {"qq": "234567"}},
      {"type": "text", "data": {"text": " 回复内容"}}
    ]
  },
  "echo": "唯一动作编号"
}
```

`echo` 使用不可预测的本地 UUID。连接管理器只接受与等待者匹配的响应；未知 `echo` 安全忽略。仅当 `status == "ok"` 且 `retcode == 0` 时视为发送成功。OneBot 响应正文、QQ 消息全文和 Token 不写入日志。

模型或本地业务返回的安全错误文本可以作为普通回复发送。空文本、动作型响应或超过 4000 字符的单条回复不直接发送；长文本按不超过 4000 字符的边界拆分，只有第一段群回复包含引用和 `@`。

## 13. 状态 API

增加只读接口：

```text
GET /api/qq/status
```

响应只包含：

```json
{
  "enabled": true,
  "state": "disabled | misconfigured | disconnected | connected",
  "allowedGroupCount": 1,
  "allowedUserCount": 2
}
```

接口不返回 Token、机器人 QQ 号、白名单内容、NapCat URL、消息内容或 Docker 路径。

## 14. Docker 配套

NapCat 官方 Docker 项目当前文档使用 `mlikiowa/napcat-docker`，并说明 QQ 数据目录为 `/app/.config/QQ`、NapCat 配置目录为 `/app/napcat/config`。本项目 Compose 使用：

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

不映射 OneBot HTTP 或正向 WebSocket 端口。NapCat WebUI 中手动创建 WebSocket 客户端：

```text
URL: ws://host.docker.internal:8080/ws/qq
Token: 与 ASSISTANT_QQ_ACCESS_TOKEN 相同
消息格式: array
上报自身消息: false
```

Docker 不由 Electron 自动启动。扫码登录、修改 WebUI Token 和接受 QQ/NapCat 相关协议必须由用户完成。

## 15. 错误处理与日志

稳定错误码至少包括：

- `qq_disabled`
- `qq_misconfigured`
- `onebot_authentication_failed`
- `onebot_duplicate_connection`
- `onebot_invalid_event`
- `onebot_rate_limited`
- `onebot_disconnected`
- `onebot_action_timeout`
- `onebot_action_failed`

协议错误、业务错误和发送错误在 WebSocket 接收循环内隔离。单条畸形事件不能关闭有效连接；连续 3 帧无法解析为 JSON 对象时以不支持的数据格式关闭连接，任意一帧合法 JSON 对象会把计数重置为 0。所有日志只记录事件类别、稳定错误码和匿名化标识，不记录完整消息、响应正文、Token 或 Cookie。

## 16. 测试策略

### 16.1 纯单元测试

- 环境变量解析、布尔值、ID 白名单、范围和 Token 约束。
- 字符串私聊、数组私聊、数组群聊、`at` 移除和空文本。
- 非文字段、字符串群聊、自发消息、错误账号和非法数字字段。
- 群与用户白名单独立、群聊必须 `@`、私聊无需 `@`。
- 令牌桶补充、瞬时容量、超限和空闲条目清理。
- 私聊和群聊出站段顺序、长文本拆分及稳定错误。

### 16.2 连接测试

使用 WebSocket 测试替身验证：

- Bearer Token 与 `X-Self-ID`。
- 单活动连接和重复连接拒绝。
- `echo` 成功、未知 `echo`、动作失败、超时和断线清理。
- Token、消息内容和 OneBot 响应正文不出现在异常文本。

### 16.3 应用与 API 集成测试

- QQ 入站消息经过真实 `AssistantApplication.process()`、SQLite 测试库和模型替身。
- 重复 `message_id` 不重复调用模型。
- 群聊引用并 `@`，私聊普通回复。
- QQ 断线后 Desktop HTTP、Electron WebSocket 和工具 API 继续工作。
- 状态 API 不泄露敏感配置。
- FastAPI 生命周期正确装配和关闭 QQ 组件。

### 16.4 Docker 契约测试

- Compose 文件可以通过 `docker compose config` 或 YAML 解析。
- WebUI 只绑定 `127.0.0.1:6099`。
- 不暴露 3000/3001。
- 数据卷和 `host.docker.internal` 配置存在。
- CI 不拉取镜像、不启动容器、不登录真实 QQ。

## 17. Git 检查点

实现阶段按以下独立提交推进：

1. `feat: 定义 OneBot 配置与协议模型`
2. `feat: 实现 QQ 消息解析与准入策略`
3. `feat: 管理 OneBot 反向 WebSocket 连接`
4. `feat: 接入统一 QQ 对话与回复流程`
5. `feat: 增加可选 NapCat Docker 配套`
6. `docs: 记录 QQ 渠道配置与验收流程`

每个提交前运行相关测试与 `git diff --check`。最终运行后端完整测试、桌面测试、Compose 契约测试，推送功能分支并创建草稿 Pull Request。

## 18. 验收标准

- 未启用 QQ 时，现有桌面、Live2D、模型、记忆和工具能力行为不变。
- Token 错误、目标不在白名单或群聊未 `@` 时绝不调用模型。
- 私聊无需 `@`，群聊引用原消息并 `@发送者`。
- 重复 OneBot 事件不会造成重复模型调用或重复回复。
- 不同 QQ 用户的会话和长期记忆互相隔离。
- QQ 断线不会导致 FastAPI 或 Electron 失效。
- 出站动作有 `echo` 关联、超时和稳定错误，不自动重发。
- 状态 API、日志、SQLite 和 Git 不包含 Token、Cookie 或完整协议错误正文。
- Docker 未安装或 NapCat 未启动时，桌面助手核心能力仍可运行。
- 所有自动化测试和 GitHub CI 通过后才允许合并。

## 19. 参考资料

- [NapCat-Docker 官方仓库](https://github.com/NapNeko/NapCat-Docker)
- [NapCatQQ 接入框架](https://napneko.github.io/use/integration)
- [NapCatQQ OneBot 网络基础](https://napneko.github.io/onebot/network)
- [NapCatQQ OneBot 11 API 兼容情况](https://napneko.github.io/develop/api)
- [NapCatQQ OneBot 11 事件与消息结构](https://napneko.github.io/onebot/basic_event)
