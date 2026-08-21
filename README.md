# Virtual Anime Assistant（二次元桌面助手）

Virtual Anime Assistant 是一个实验阶段的跨平台桌面助手。Electron 负责 Live2D 渲染与用户交互，FastAPI 负责系统监控、场景判断、语音合成和实时消息分发。

## 当前能力

- 监控 CPU、内存和系统运行时间。
- 在 macOS 和 Windows 上识别前台应用。
- 根据 CPU、时间和应用持续时间触发场景提醒。
- 优先调用 GPT-SoVITS，失败时回退到 EdgeTTS。
- 通过 WebSocket 驱动角色表情、动作和语音播放。
- Live2D 在线时，桌面端会为没有现成音频的模型回复按需调用 TTS 并自动播放；语音失败不影响文字回复、动作或 WebSocket。
- 使用统一消息模型和会话编排处理桌面交互与场景事件。默认未启用真实模型时，Demo 网关返回固定演示回复；配置 OpenAI 兼容服务后，聊天改用真实模型。
- 使用 SQLite 持久化会话、消息、长期记忆和模型调用元数据。
- 提供可扩展的工具注册表与权限状态机：低风险工具自动执行，高风险工具必须逐次确认，Electron 会展示待确认队列。
- 使用 SQLite 持久化工具请求、确认状态和脱敏审计事件，并以原子事务处理确认竞争、过期与取消。
- 通过 OneBot 11 反向 WebSocket 接入 QQ 私聊和群聊文字；私聊无需 `@`，群聊只有在允许群内结构化 `@机器人` 时触发。
- 提供仅限本机的 Web 配置界面，统一管理 LLM、QQ / OneBot、TTS 与电脑能力设置，展示配置来源和环境变量接管状态，并支持脱敏连接测试。
- 在 macOS 上按独立开关采集脱敏电脑状态、提供 4 个固定受控操作，并可通过受限 SSH stdin relay 向云端上报最新快照。
- 通过明确的记忆命令或管理 API 维护长期记忆，并按来源和用户隔离数据。
- 支持客户端提供 `messageId` 作为幂等键；重复消息不会再次调用模型，冲突或模型故障只返回安全错误。
- 默认监听 `127.0.0.1` 回环地址，Electron renderer 不具备 Node.js 权限。

QQ 私聊与群聊文字接入已通过真实 NapCat 联调，但默认关闭；NapCat 登录和 WebUI 配置仍需要用户手动完成。macOS 电脑能力也默认全部关闭，只包含读取状态、打开应用、打开公开 HTTPS 地址、调节音量和控制已知播放器，不是通用远程控制。正式安装包仍未实现。仓库当前工作树不直接提供受授权限制的 Live2D 模型或 Cubism Core SDK；本地开发时可以按下文说明从归档标签恢复 Hiyori 样例资源。

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

Electron 会先检查固定的 `127.0.0.1:8080` 健康接口：已有后端时直接复用，未运行时在开发环境以 `python3 backend/main.py` 启动，并在异常退出后执行最多 5 次有界重试。退出 Electron 时只终止由它创建的后端，不会终止用户预先启动的服务。打包环境固定查找 `resources/backend/vaa-backend`（Windows 为 `.exe`）；桌面构建会先用 PyInstaller 生成并附带该文件。

“电脑”设置中的“登录系统时自动启动助手”默认关闭。保存后，正式安装版会在下次启动时通过 Electron 的系统原生登录启动接口应用该选项；开发版不会注册 Electron 开发运行时，Linux 当前也不会修改系统启动项。

`npm run setup:live2d-dev` 仅用于恢复本地开发样例。没有执行该命令时，Electron 仍可启动，并显示缺少 Live2D 开发资源的提示。

### 构建 macOS 安装包

构建依赖与运行依赖分开安装，避免把 PyInstaller 加入云端运行镜像：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.txt -r backend/requirements-build.txt
npm --prefix desktop-app ci
ASSISTANT_PACKAGING_PYTHON="$PWD/.venv/bin/python" npm --prefix desktop-app run build
```

产物位于 `desktop-app/dist/`，后端暂存文件位于被 Git 忽略的
`desktop-app/build-resources/`。安装包只附带公开的根级 `config/*.yml`，不会附带
`config/local/`、Token、API Key、SSH 私钥或本机设置文件。打包后的后端会把生成
音频写入 Electron 用户数据目录，不会尝试写入只读应用包。

当前本地构建未进行 Apple Developer ID 签名和公证，只适合开发验收；公开分发前
必须补齐签名、公证和正式应用图标。受许可限制的 Live2D 开发样例仍不会进入安装包。

## Web 配置界面

后端启动后，访问 `http://127.0.0.1:8080/settings`，或点击 Electron 托盘菜单中的「设置」。首次访问需要创建长度为 10～128 个字符的本地管理密码；后续操作使用 30 分钟绝对有效期的内存会话。会话 Cookie 设为 `HttpOnly`、`SameSite=Strict`、`Path=/` 且不指定 `Domain`，所有写操作还需要 CSRF Token。

页面可以管理以下内容：

- OpenAI 兼容 LLM：启用状态、服务地址、模型、API Key、超时、上下文限制和 Tool Calling。
- QQ / OneBot：启用状态、Access Token、群/用户白名单、速率、突发容量、并发和动作超时。
- TTS：GPT-SoVITS 地址、默认音色和生成音频保留时间。音色定义仍由 `config/voices.yml` 管理。
- 电脑：本机状态、受控操作和远端上报 3 个独立开关；后两项必须先启用状态采集。
- 连接测试：LLM 会发送最小真实请求；QQ 只做本地草稿校验并读取当前连接状态；TTS 只探测服务，不生成音频。

非敏感值写入平台配置目录下的 `settings.json`。LLM API Key 和 QQ Access Token 通过 Python `keyring` 写入 macOS Keychain 或 Windows Credential Manager，不会写入 `settings.json`；页面只允许以 `retain`、`replace`、`delete`（保留、替换、删除）表达凭据操作，也不会回显已有秘密。首版正式支持 macOS 与 Windows 的系统凭据库；Linux 的凭据存储尚未作为支持目标，凭据库不可用时页面会禁用秘密修改并给出提示。

默认配置文件位置如下：

- macOS：`~/Library/Application Support/Virtual Anime Assistant/settings.json`
- Windows：`%LOCALAPPDATA%\Virtual Anime Assistant\settings.json`
- Linux：`$XDG_CONFIG_HOME/Virtual Anime Assistant/settings.json`；未设置 `XDG_CONFIG_HOME` 时通常位于 `~/.config/Virtual Anime Assistant/settings.json`

配置解析优先级为「默认值 < `settings.json` < 系统凭据库 < 环境变量」。环境变量接管的字段会显示为只读，服务端也会拒绝保存对这些字段的修改。保存成功只代表配置已持久化，必须重启后端后才会用于运行时。

如果 `settings.json` 损坏，运行时会使用默认值和有效环境变量继续启动，但设置 API 会拒绝覆盖损坏文件并提示先修复或移走文件。系统凭据库不可用、保存恢复失败或外部服务不可达时，页面只显示稳定、脱敏的错误信息，不会返回地址、密钥或服务响应正文。

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

模型聊天回复通过桌面端按需请求现有 TTS 接口，因此只有 Live2D 窗口在线时才会自动生成语音。场景消息已有音频时直接播放，不会重复合成；QQ 回复不会触发桌面端朗读。

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
| `ASSISTANT_BACKEND_EXECUTABLE` | 空 | Electron 托管的后端可执行文件绝对路径；不接受命令参数或相对路径 |
| `ASSISTANT_PYTHON_EXECUTABLE` | `python3`（Windows 为 `python`） | 仅开发环境使用的 Python 可执行文件绝对路径 |
| `ASSISTANT_GPT_SOVITS_URL` | `http://127.0.0.1:9880` | GPT-SoVITS 服务地址 |
| `ASSISTANT_TTS_DEFAULT_VOICE_ID` | `character_001` | 默认音色 ID；必须存在于 `config/voices.yml` |
| `ASSISTANT_AUDIO_MAX_AGE_SECONDS` | `86400` | 生成音频的最大保留时间 |
| `ASSISTANT_LLM_ENABLED` | `false` | 是否启用真实大模型；支持 `1/true/yes/on` 和 `0/false/no/off` |
| `ASSISTANT_LLM_TOOL_CALLING_ENABLED` | `false` | 是否允许模型调用显式授权的低风险只读工具 |
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
| `ASSISTANT_COMPUTER_STATE_ENABLED` | `false` | 启用本机脱敏状态采集 |
| `ASSISTANT_COMPUTER_ACTIONS_ENABLED` | `false` | 启用 4 个固定 macOS 操作；要求状态采集已启用 |
| `ASSISTANT_COMPUTER_REMOTE_REPORT_ENABLED` | `false` | 启用远端快照上报；要求状态采集和完整 relay 配置 |
| `ASSISTANT_COMPUTER_DEVICE_ID` | 空 | 小写字母、数字和连字符组成的设备 ID，最长 64 字符 |
| `ASSISTANT_COMPUTER_RELAY_TARGET` | 空 | 专用 relay SSH 目标，格式为 `用户@主机` |
| `ASSISTANT_COMPUTER_RELAY_PORT` | 空 | relay SSH 端口，范围为 1～65535 |
| `ASSISTANT_COMPUTER_RELAY_IDENTITY_FILE` | 空 | 专用 SSH 私钥的绝对路径，不写入 `settings.json` |
| `ASSISTANT_COMPUTER_RELAY_KNOWN_HOSTS_FILE` | 空 | 独立 known hosts 文件的绝对路径，不写入 `settings.json` |

以上与 Web 页面重叠的环境变量优先于页面保存值。被环境变量接管的字段会在页面中标记为只读。

Electron 收到一套完整且合法的 relay 环境变量后，会把上表中的 5 项
`ASSISTANT_COMPUTER_*` relay 参数保存到 Electron 私有用户目录下的
`backend-environment.json`，供电脑或应用重启后恢复使用。该文件在 macOS 和
Linux 上使用仅当前用户可读写的权限，不保存模型 API Key、QQ Token 或 SSH
私钥正文；再次提供环境变量时，环境变量始终优先。

> **安全提示：** 记忆、会话等管理 API 当前没有鉴权。CORS 只能约束浏览器，不能阻止非浏览器客户端访问。除非服务位于具备鉴权能力的可信反向代理和网络隔离之后，否则不要把 `ASSISTANT_HOST` 设置为 `0.0.0.0` 或其他非回环地址。

`ASSISTANT_DATA_DIR` 未配置时使用以下目录：

- macOS：`~/Library/Application Support/VirtualAnimeAssistant`
- Windows：`%APPDATA%\VirtualAnimeAssistant`；`APPDATA` 不可用时回退到 `~/AppData/Roaming/VirtualAnimeAssistant`
- Linux：`$XDG_DATA_HOME/virtual-anime-assistant`；`XDG_DATA_HOME` 不可用时回退到 `~/.local/share/virtual-anime-assistant`

### 配置真实模型

推荐通过 Web 配置界面填写兼容服务地址、API Key 和模型名。如果必须使用环境变量，请先在当前终端中安全地设置 `ASSISTANT_LLM_BASE_URL`、`ASSISTANT_LLM_API_KEY` 和 `ASSISTANT_LLM_MODEL`，避免把真实值写入脚本或 Shell 历史，然后在仓库根目录执行：

```bash
export ASSISTANT_LLM_ENABLED=true
export ASSISTANT_LLM_TOOL_CALLING_ENABLED=false
python3 backend/main.py
```

API Key 可以来自环境变量或 Web 页面管理的系统凭据库。不要把真实 Key 写入仓库、`settings.json` 或日志。未配置有效 `ASSISTANT_LLM_API_KEY` 且系统凭据库中也没有已保存值时，不会发送 `Authorization` 请求头，是否可用取决于兼容服务自身。

以上示例保留安全默认值，只启用纯文本模型路径。只有 `ASSISTANT_LLM_ENABLED=true` 与 `ASSISTANT_LLM_TOOL_CALLING_ENABLED=true` 同时成立时，后端才会向模型发送本次请求可用的 OpenAI 兼容 `tools`。需要显式启用时，仅修改工具开关：

```bash
export ASSISTANT_LLM_TOOL_CALLING_ENABLED=true
```

兼容服务必须支持 Chat Completions `tool_calls`。若供应商不支持，关闭工具开关即可回退到纯文本模型路径，无需修改或回滚 SQLite 数据库；系统不会自动改用无工具请求重试同一条消息。模型鉴权、限流、超时、协议和服务错误会转换为有限的安全提示，不返回服务响应体、地址或密钥。

### QQ / OneBot 配置

QQ 渠道默认关闭。启用时必须配置有效 Token，并至少配置 1 个允许群或允许用户。群白名单和用户白名单相互独立：用户位于允许群中，不代表该用户可以私聊机器人。

消息触发规则如下：

- 私聊：发送者必须位于 `ASSISTANT_QQ_ALLOWED_USER_IDS`，不要求 `@`。
- 群聊：群必须位于 `ASSISTANT_QQ_ALLOWED_GROUP_IDS`，并且消息段中必须结构化 `@机器人`。
- 群聊回复：第一段引用原消息并 `@发送者`，后续分段只发送文字。
- 图片、语音、文件和其他富媒体不会被下载或传给模型。
- 未授权、未 `@`、重复和超限消息会被静默忽略。

NapCat 作为 WebSocket 客户端连接后端的 `/ws/qq`。共享 Token 可以来自环境变量或 Web 页面管理的系统凭据库，并通过 `Authorization: Bearer` 请求头进行常量时间校验。同一时间只允许 1 个机器人账号连接；断线、超时或 QQ 配置错误不会阻止 Electron 和本地聊天继续运行。

安全状态可以通过以下接口查询：

```bash
curl http://127.0.0.1:8080/api/qq/status
```

响应只包含 `enabled`、`state`、`allowedGroupCount` 和 `allowedUserCount`，不会返回 Token、机器人 QQ 号或白名单内容。

可选 NapCat Docker 配置、扫码步骤和手动验收方法见 [NapCat QQ 开发环境](qq-bot/README.md)。Docker 不会由 Electron 自动启动，也不是桌面助手的运行依赖。

### 工具权限与安全边界

工具注册表包含时间查询，并按运行配置加入电脑状态和固定 macOS 操作。工具风险由后端根据本地注册信息计算，客户端、模型和 QQ 适配器不能自行把高风险操作降级为低风险：

- 低风险工具自动执行，并记录请求、执行与结果状态。
- 高风险工具创建一次性确认；拒绝、过期或待确认时取消都不会调用处理函数。
- Electron 通过 WebSocket 接收确认通知，断线重连后从本机 API 恢复未过期确认。
- 注册为敏感字段的参数会在持久化和界面展示前替换为 `[REDACTED]`，原始参数不会写入审计记录。
- 超时和内部异常只对外返回稳定错误码，不返回异常正文。

模型 Tool Calling 还受以下边界约束：

- 运行时只有在真实 LLM 已启用、Tool Calling 开关已开启且目录至少能导出 1 个合法工具时才创建模型工具编排器。空目录、仅含高风险工具或仅含无效 schema 工具的目录都会保留 catalog，但应用层注入 `None` 并回退到纯文本模型路径。
- 模型只会看到当前平台、Profile、消息来源和本机开关共同允许的工具；猜测未公开工具名不会执行处理器。
- 本机模型可以提出固定 macOS 操作，但必须等待 Electron 上逐次确认；拒绝、超时、取消、断线或服务重启均不会补执行。
- 每条用户消息最多请求模型 3 次、执行 4 个工具调用，工具按模型给出的顺序串行执行。
- 工具结果属于不可信数据，不能覆盖系统规则或授予新权限；只有结果状态为 `succeeded` 时，模型才能声称操作成功。
- SQLite 独立记录模型来源的工具请求、脱敏结果和审计事件，并在成功或已记录失败的回合中原子保存最终 Assistant 消息与全部模型调用元数据。
- 确认通过后请求只会原子认领一次，工具结果返回模型后才生成最终回复。

当前没有注册任意 Shell、文件读写或删除、键盘输入、鼠标点击、剪贴板读取、屏幕截图或 QQ 主动消息发送。桌面与 QQ 共用应用层模型工具编排器，但 QQ 仍遵守渠道准入规则：允许用户的私聊无需 `@`；群聊必须位于允许群，并在消息段中结构化 `@机器人`。本机 HTTP API 仅作为 Electron 和开发调试接缝，不能用于伪装 QQ 或模型来源。

### 电脑状态、隐私与停止方法

三个电脑能力开关默认均为关闭。建议按“本机状态采集 → 本机受控操作 → 远端上报”的顺序启用，保存后重启助手生效。部分前台应用、全屏、锁屏和媒体状态需要 macOS 辅助功能权限；缺少权限时对应分区只返回稳定的 `unavailable`，不会扩大采集范围。

状态字段按公开、半敏感、敏感、高度敏感四级处理：

- 公开：CPU、内存、系统磁盘、网络是否可用、音量和静音状态。
- 半敏感：在线/空闲/锁屏、按整分钟取整的空闲时间、电池和媒体播放状态。
- 敏感：前台应用名及少量允许公开的标题；浏览器、终端、聊天、邮件和代码工具标题会被隐藏或降级。
- 高度敏感：屏幕内容、按键、鼠标轨迹、剪贴板、文件内容和聊天正文；首版完全不采集。

本机只保留当前内存快照，云端也只保留每台设备的最新快照，不保存状态历史。状态不会写入聊天会话、长期记忆或 SQLite；受控操作仍会留下脱敏审计状态。QQ 只能查询最新脱敏状态，不能看到或批准本机操作工具；停止上报后，云端最迟在 45 秒内把设备标为离线。

本机操作仅限打开经过校验的应用、打开公开绝对 HTTPS 地址、设置 0～100 音量和控制已知播放器。每次操作都要在当前 Electron 会话逐次确认，待确认参数不持久化，也不存在任意 Shell。

远端上报使用专用 SSH 密钥和单次 SSH stdin 会话。公钥在服务器端被固定命令与 `restrict` 限制，不能获得远程命令或端口转发；Mac 不持有云端 Bearer Token。设置 API 和页面只显示 relay 字段“已配置/未配置”，不返回目标或路径。

需要停止时，先关闭“远端上报”并保存、重启；如需同时停止采集，再关闭另两个开关并保存、重启。永久停用还应按云端运维文档撤销专用公钥并处理专用私钥。实际创建或安装持久 SSH 凭证前必须重新确认。

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
| GET | `/api/settings/session` | 获取设置初始化和会话状态 |
| POST | `/api/settings/setup`、`/api/settings/login`、`/api/settings/logout` | 初始化管理密码、登录或退出 |
| GET / PUT | `/api/settings/config` | 获取脱敏配置快照或原子保存完整草稿 |
| GET | `/api/settings/voices` | 获取可选音色摘要 |
| GET | `/api/status/desktop-startup` | 供 Electron 读取单个非敏感开机启动布尔值 |
| POST | `/api/settings/test/llm`、`/api/settings/test/qq`、`/api/settings/test/tts` | 使用未保存草稿执行脱敏连接测试 |
| WS | `/ws/avatar` | 双向角色消息通道 |
| WS | `/ws/qq` | OneBot 11 反向 WebSocket；需要 Bearer Token 和 `X-Self-ID` |

记忆与会话管理 API 当前仅支持本机 `local-user`：来源固定为 `desktop`，可管理的会话固定为 `desktop:local-user`。其他用户或来源的数据不会由这些端点返回或删除。

`/settings` 和 `/api/settings/*` 具有独立的本机安全边界：客户端地址只允许 `127.0.0.1` 或 `::1`，`Host` 必须精确为 `127.0.0.1:8080`，状态变更请求的 `Origin` 必须精确为 `http://127.0.0.1:8080`；受保护接口还需要密码会话和 CSRF Token。这些限制不代表上方旧有管理 API 已获得相同鉴权，部署时仍须遵守前文的本机监听安全提示。

## 测试

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
python3 -m unittest discover -s backend/tests -p 'test_settings_*.py' -v
npm --prefix desktop-app ci
npm --prefix desktop-app run test:unit
npm --prefix desktop-app test
npm --prefix desktop-app run build:renderer
node --check backend/settings/static/settings.js
node --check desktop-app/src/main.js
```

浏览器验收需要先启动本机后端，再使用 Playwright 验证首次设密、登录、配置来源、连接测试和保存后的重启提示，同时检查浏览器控制台以及页面没有外部资源请求。

Web 配置的边界与安全契约见 [本地 Web 配置界面设计规格](docs/superpowers/specs/2026-08-01-web-settings-interface-design.md)，实际任务、提交和验证关卡见 [本地 Web 配置界面实施计划与完成记录](docs/superpowers/plans/2026-08-01-web-settings-interface.md)。其他完整优化清单与实施状态见 [项目优化实施计划](docs/superpowers/plans/2026-07-22-project-hardening.md)、[工具权限状态机实施计划](docs/superpowers/plans/2026-07-29-tool-permission-state-machine.md)、[OneBot QQ 渠道设计](docs/superpowers/specs/2026-07-29-onebot-qq-channel-design.md) 和 [OneBot QQ 渠道实现计划](docs/superpowers/plans/2026-07-29-onebot-qq-channel.md)。

## 项目结构

```text
backend/       FastAPI API、Web 设置、统一消息、工具权限、会话编排、模型网关、SQLite、场景、TTS 和平台监控
config/        声线、回复和场景 YAML 配置
desktop-app/   Electron 主进程、preload 和 renderer
docs/          架构规格与分阶段实施计划
qq-bot/        可选 NapCat Docker 配套和手动配置说明
```

## License

MIT
