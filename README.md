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
- 通过明确的记忆命令或管理 API 维护长期记忆，并按来源和用户隔离数据。
- 支持客户端提供 `messageId` 作为幂等键；重复消息不会再次调用模型，冲突或模型故障只返回安全错误。
- 默认监听 `127.0.0.1` 回环地址，Electron renderer 不具备 Node.js 权限。

QQ 接入、主动电脑控制和正式安装包仍未实现。当前只能识别前台应用和接收窗口上报，不能代替用户操作电脑。仓库当前工作树不直接提供受授权限制的 Live2D 模型或 Cubism Core SDK；本地开发时可以按下文说明从归档标签恢复 Hiyori 样例资源。

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
| WS | `/ws/avatar` | 双向角色消息通道 |

记忆与会话管理 API 当前仅支持本机 `local-user`：来源固定为 `desktop`，可管理的会话固定为 `desktop:local-user`。其他用户或来源的数据不会由这些端点返回或删除。

## 测试

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
npm --prefix desktop-app ci
npm --prefix desktop-app test
npm --prefix desktop-app run build:renderer
```

完整优化清单与实施状态见 [项目优化实施计划](docs/superpowers/plans/2026-07-22-project-hardening.md)。

## 项目结构

```text
backend/       FastAPI API、统一消息、会话编排、模型网关、SQLite、场景、TTS 和平台监控
config/        声线、回复和场景 YAML 配置
desktop-app/   Electron 主进程、preload 和 renderer
docs/          架构规格与分阶段实施计划
```

## License

MIT
