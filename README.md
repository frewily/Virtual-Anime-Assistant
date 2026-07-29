# Virtual Anime Assistant（二次元桌面助手）

Virtual Anime Assistant 是一个实验阶段的跨平台桌面助手。Electron 负责 Live2D 渲染与用户交互，FastAPI 负责系统监控、场景判断、语音合成和实时消息分发。

## 当前能力

- 监控 CPU、内存和系统运行时间。
- 在 macOS 和 Windows 上识别前台应用。
- 根据 CPU、时间和应用持续时间触发场景提醒。
- 优先调用 GPT-SoVITS，失败时回退到 EdgeTTS。
- 通过 WebSocket 驱动角色表情、动作和语音播放。
- 使用统一消息模型和会话编排处理桌面交互与场景事件。默认未启用真实模型时，Demo 网关返回固定演示回复；配置 OpenAI 兼容服务后，聊天改用真实模型。
- 使用 SQLite 持久化会话、消息、长期记忆和模型调用元数据。
- 提供可扩展的工具注册表与权限状态机：低风险工具自动执行，高风险工具必须逐次确认，Electron 会展示待确认队列。
- 使用 SQLite 持久化工具请求、确认状态和脱敏审计事件，并以原子事务处理确认竞争、过期与取消。
- 通过 OneBot 11 反向 WebSocket 接入 QQ 私聊和群聊文字；私聊无需 `@`，群聊只有在允许群内结构化 `@机器人` 时触发。
- 通过明确的记忆命令或管理 API 维护长期记忆，并按来源和用户隔离数据。
- 支持客户端提供 `messageId` 作为幂等键；重复消息不会再次调用模型，冲突或模型故障只返回安全错误。
- 默认监听 `127.0.0.1` 回环地址，Electron renderer 不具备 Node.js 权限。

QQ 私聊与群聊文字接入已通过真实 NapCat 联调，但默认关闭；NapCat 登录和 WebUI 配置仍需要用户手动完成。主动电脑控制和正式安装包仍未实现。当前只能识别前台应用和接收窗口上报，不能代替用户操作电脑。仓库当前工作树不直接提供受授权限制的 Live2D 模型或 Cubism Core SDK；本地开发时可以按下文说明从归档标签恢复 Hiyori 样例资源。

## 环境要求

- Python 3.10+
- Node.js 20+
- npm 10+

## 安装与运行

### 1. 启动后端

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 main.py
```

Windows PowerShell 激活虚拟环境时使用：

```powershell
.venv\Scripts\Activate.ps1
```

后端默认监听 `http://127.0.0.1:8080`。

### 2. 启动桌面端

```bash
cd desktop-app
npm ci
npm run setup:live2d-dev
npm start
```

`npm start` 会先通过 esbuild 生成本地 renderer bundle，再启动 Electron。页面不会从远程 CDN 执行脚本。

`npm run setup:live2d-dev` 仅用于恢复本地开发样例。没有执行该命令时，Electron 仍可启动，并显示缺少 Live2D 开发资源的提示。

## Live2D 开发模型

项目使用 `pixi-live2d-display@0.4.0` 和 PixiJS 6 渲染 Cubism 4 模型。执行以下命令可以从归档标签恢复 Hiyori Momose、动作、纹理和 Cubism Core：

```bash
git fetch origin tag archive/legacy-java-qq-live2d-2026-07-28
cd desktop-app
npm run setup:live2d-dev
```

生成目录为：

```text
desktop-app/src/renderer/assets/dev-live2d/
├── live2dcubismcore4.min.js
└── hiyori/
    ├── Hiyori.model3.json
    ├── Hiyori.moc3
    ├── Hiyori.2048/
    └── motions/
```

该目录受 Git 忽略，并由 `electron-builder.yml` 明确排除，不会默认进入 `.exe` 或 `.dmg`。Hiyori 当前只用于本地 SDK 集成验证，不能视为正式产品角色。

模型加载成功后支持：

- 随机待机动作、眨眼、物理摆动和姿势更新。
- 鼠标视线跟随。
- 点击身体播放 `TapBody` 动作。
- 使用 `+`、`-`、`0` 调整或重置缩放。
- 使用 `Ctrl/Command + 滚轮` 调整缩放。
- 从窗口顶部透明区域拖动窗口。

资源损坏或缺失时，重新运行 `npm run setup:live2d-dev` 并重启 Electron。第三方资源来源、版权说明和发布限制见 [Live2D 第三方开发资源说明](desktop-app/THIRD_PARTY_DEV_ASSETS.md)。

## GPT-SoVITS 配置

声线配置位于 `config/voices.yml`。每条声线可以配置参考音频和提示文本：

```yaml
referenceAudio: voices/character_001.wav
promptText: 大家好，我是小樱，很高兴认识你们！
```

默认 GPT-SoVITS 地址为 `http://127.0.0.1:9880`，可以通过环境变量覆盖：

```bash
export ASSISTANT_GPT_SOVITS_URL=http://127.0.0.1:9880
```

可用环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASSISTANT_HOST` | `127.0.0.1` | FastAPI 监听地址；建议保持为回环地址 |
| `ASSISTANT_PORT` | `8080` | FastAPI 监听端口 |
| `ASSISTANT_GPT_SOVITS_URL` | `http://127.0.0.1:9880` | GPT-SoVITS 服务地址 |
| `ASSISTANT_AUDIO_MAX_AGE_SECONDS` | `86400` | 生成音频的最大保留时间 |
| `ASSISTANT_LLM_ENABLED` | `false` | 是否启用真实大模型；支持 `1/true/yes/on` 和 `0/false/no/off` |
| `ASSISTANT_LLM_BASE_URL` | 空 | OpenAI 兼容服务根地址，例如 `https://api.example.com/v1` |
| `ASSISTANT_LLM_API_KEY` | 空 | OpenAI 兼容服务 API Key |
| `ASSISTANT_LLM_MODEL` | 空 | 兼容服务支持的模型名称 |
| `ASSISTANT_LLM_TIMEOUT_SECONDS` | `60` | 单次模型调用超时，范围为 1～300 秒 |
| `ASSISTANT_LLM_MAX_CONTEXT_MESSAGES` | `20` | 近期会话消息数量上限，范围为 1～100 |
| `ASSISTANT_LLM_MAX_CONTEXT_CHARS` | `12000` | 模型上下文字符上限，范围为 4000～100000 |
| `ASSISTANT_DATA_DIR` | 平台用户数据目录 | SQLite 数据库目录；文件名固定为 `assistant.db` |
| `ASSISTANT_QQ_ENABLED` | `false` | 是否启用 QQ 渠道；支持 `1/true/yes/on` 和 `0/false/no/off` |
| `ASSISTANT_QQ_ACCESS_TOKEN` | 空 | 启用时必填；去除首尾空格后长度为 16～512 |
| `ASSISTANT_QQ_ALLOWED_GROUP_IDS` | 空 | 允许触发机器人的群号，使用英文逗号分隔的正整数 |
| `ASSISTANT_QQ_ALLOWED_USER_IDS` | 空 | 允许私聊机器人的用户号，使用英文逗号分隔的正整数 |
| `ASSISTANT_QQ_RATE_PER_MINUTE` | `10` | 每个 QQ 发送者每分钟限额，范围为 1～120 |
| `ASSISTANT_QQ_RATE_BURST` | `2` | 瞬时容量，范围为 1～20，且不能大于每分钟限额 |
| `ASSISTANT_QQ_MAX_CONCURRENCY` | `4` | QQ 渠道全局并发上限，范围为 1～32 |
| `ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS` | `10` | OneBot 出站动作超时，范围为 1～60 秒 |

> **安全提示：** 记忆、会话等管理 API 当前没有鉴权。CORS 只能约束浏览器，不能阻止非浏览器客户端访问。除非服务位于具备鉴权能力的可信反向代理和网络隔离之后，否则不要把 `ASSISTANT_HOST` 设置为 `0.0.0.0` 或其他非回环地址。

`ASSISTANT_DATA_DIR` 未配置时使用以下目录：

- macOS：`~/Library/Application Support/VirtualAnimeAssistant`
- Windows：`%APPDATA%\VirtualAnimeAssistant`；`APPDATA` 不可用时回退到 `~/AppData/Roaming/VirtualAnimeAssistant`
- Linux：`$XDG_DATA_HOME/virtual-anime-assistant`；`XDG_DATA_HOME` 不可用时回退到 `~/.local/share/virtual-anime-assistant`

### 配置真实模型

在仓库根目录执行以下示例，并将地址、Key 和模型名替换为兼容服务提供的真实值：

```bash
export ASSISTANT_LLM_ENABLED=true
export ASSISTANT_LLM_BASE_URL=https://api.example.com/v1
export ASSISTANT_LLM_API_KEY=your-api-key
export ASSISTANT_LLM_MODEL=your-model
python3 backend/main.py
```

API Key 只从环境变量读取。不要把真实 Key 写入仓库、配置文件或日志。未配置 `ASSISTANT_LLM_API_KEY` 时不会发送 `Authorization` 请求头，是否可用取决于兼容服务自身。

当前模型请求只发送消息和基础生成参数，不启用 Tool Calling，也不控制电脑。模型鉴权、限流、超时、协议和服务错误会转换为有限的安全提示，不返回服务响应体、地址或密钥。

### QQ / OneBot 配置

QQ 渠道默认关闭。启用时必须配置有效 Token，并至少配置 1 个允许群或允许用户。群白名单和用户白名单相互独立：用户位于允许群中，不代表该用户可以私聊机器人。

消息触发规则如下：

- 私聊：发送者必须位于 `ASSISTANT_QQ_ALLOWED_USER_IDS`，不要求 `@`。
- 群聊：群必须位于 `ASSISTANT_QQ_ALLOWED_GROUP_IDS`，并且消息段中必须结构化 `@机器人`。
- 群聊回复：第一段引用原消息并 `@发送者`，后续分段只发送文字。
- 图片、语音、文件和其他富媒体不会被下载或传给模型。
- 未授权、未 `@`、重复和超限消息会被静默忽略。

NapCat 作为 WebSocket 客户端连接后端的 `/ws/qq`。共享 Token 只从环境变量读取，并通过 `Authorization: Bearer` 请求头进行常量时间校验。同一时间只允许 1 个机器人账号连接；断线、超时或 QQ 配置错误不会阻止 Electron 和本地聊天继续运行。

安全状态可以通过以下接口查询：

```bash
curl http://127.0.0.1:8080/api/qq/status
```

响应只包含 `enabled`、`state`、`allowedGroupCount` 和 `allowedUserCount`，不会返回 Token、机器人 QQ 号或白名单内容。

可选 NapCat Docker 配置、扫码步骤和手动验收方法见 [NapCat QQ 开发环境](qq-bot/README.md)。Docker 不会由 Electron 自动启动，也不是桌面助手的运行依赖。

### 工具权限与安全边界

当前生产注册表只包含只读工具 `system.current_time`，用于读取本地或指定 IANA 时区的当前时间。工具风险由后端根据本地注册信息计算，客户端、模型和 QQ 适配器不能自行把高风险操作降级为低风险：

- 低风险工具自动执行，并记录请求、执行与结果状态。
- 高风险工具创建一次性确认；拒绝、过期或待确认时取消都不会调用处理函数。
- Electron 通过 WebSocket 接收确认通知，断线重连后从本机 API 恢复未过期确认。
- 注册为敏感字段的参数会在持久化和界面展示前替换为 `[REDACTED]`，原始参数不会写入审计记录。
- 超时和内部异常只对外返回稳定错误码，不返回异常正文。

当前没有注册任意 Shell、文件删除、键盘输入、应用启动、QQ 主动消息发送或其他真实电脑控制工具。QQ 适配器已直接调用统一应用服务，并使用受信任的 `qq` 来源身份；后续大模型 Tool Calling 也必须复用同一安全边界。本机 HTTP API 仅作为 Electron 和开发调试接缝，不能用于伪装 QQ 或模型来源。

### 本地记忆与会话

聊天输入以下命令可以管理当前用户的长期记忆：

```text
记住：我喜欢清淡口味
忘记：我喜欢清淡口味
```

`记住：内容` 和 `忘记：内容` 由本地确定性解析器处理，不调用模型服务。只有明确记忆命令或记忆管理 API 会写入长期记忆；普通聊天不会自动写入。SQLite 同时保存会话消息和模型调用状态、耗时、Token 用量等元数据。

HTTP 和 WebSocket 聊天请求可以提供长度为 1～200 的 `messageId`。相同会话、内容和 `messageId` 的重发会复用已保存的回复，不会再次调用模型；跨会话复用或改变内容会返回安全冲突错误。

## 场景配置

场景规则位于 `config/scenarios.yml`。规则支持：

- `priority`：同时匹配时优先级较高的场景先触发。
- `cooldownSeconds`：同一场景的冷却时间。
- `duration`：CPU 规则按秒计算，应用持续时间规则按分钟计算。
- 跨午夜时间范围，例如 `23:00` 到 `06:00`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 获取 CPU、内存和运行时间 |
| POST | `/api/tts/speak` | 合成语音 |
| GET | `/api/tts/voices` | 获取声线列表 |
| POST | `/api/report/window` | 上报窗口变化，供外部适配器使用 |
| POST | `/api/chat/message` | 发送聊天消息；可提供 `messageId` 作为幂等键 |
| GET | `/api/memories` | 查询本机用户的长期记忆 |
| POST | `/api/memories` | 创建或更新本机用户的长期记忆 |
| DELETE | `/api/memories/{memory_id}` | 按 ID 永久删除本机用户的长期记忆 |
| GET | `/api/conversations/desktop:local-user/messages` | 查询本机会话消息 |
| DELETE | `/api/conversations/desktop:local-user` | 永久删除本机会话及关联消息和模型调用记录 |
| GET | `/api/avatar/status` | 获取桌面端连接状态 |
| POST | `/api/avatar/action` | 触发角色动作 |
| POST | `/api/tools/requests` | 提交已注册工具请求；低风险直接执行，高风险返回待确认状态 |
| GET | `/api/tools/confirmations` | 查询仍待决定且未过期的本机确认 |
| POST | `/api/tools/confirmations/{confirmation_id}/decision` | 对高风险操作执行一次性批准或拒绝 |
| GET | `/api/tools/requests/{request_id}` | 查询工具请求的安全状态与结果 |
| POST | `/api/tools/requests/{request_id}/cancel` | 取消待确认或支持安全取消的运行请求 |
| GET | `/api/qq/status` | 获取不含 Token 和白名单内容的 QQ 渠道状态 |
| WS | `/ws/avatar` | 双向角色消息通道 |
| WS | `/ws/qq` | OneBot 11 反向 WebSocket；需要 Bearer Token 和 `X-Self-ID` |

记忆与会话管理 API 当前仅支持本机 `local-user`：来源固定为 `desktop`，可管理的会话固定为 `desktop:local-user`。其他用户或来源的数据不会由这些端点返回或删除。

## 测试

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
npm --prefix desktop-app ci
npm --prefix desktop-app test
npm --prefix desktop-app run build:renderer
```

完整优化清单与实施状态见 [项目优化实施计划](docs/superpowers/plans/2026-07-22-project-hardening.md)、[工具权限状态机实施计划](docs/superpowers/plans/2026-07-29-tool-permission-state-machine.md)、[OneBot QQ 渠道设计](docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md) 和 [OneBot QQ 渠道实现计划](docs/superpowers/plans/2026-07-29-onebot-qq-channel.md)。

## 项目结构

```text
backend/       FastAPI API、统一消息、工具权限、会话编排、模型网关、SQLite、场景、TTS 和平台监控
config/        声线、回复和场景 YAML 配置
desktop-app/   Electron 主进程、preload 和 renderer
docs/          架构规格与分阶段实施计划
qq-bot/        可选 NapCat Docker 配套和手动配置说明
```

## License

MIT
