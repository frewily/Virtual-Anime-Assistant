# Virtual Anime Assistant（二次元桌面助手）

Virtual Anime Assistant 是一个实验阶段的跨平台桌面助手。Electron 负责 Live2D 渲染与用户交互，FastAPI 负责系统监控、场景判断、语音合成和实时消息分发。

## 当前能力

- 监控 CPU、内存和系统运行时间。
- 在 macOS 和 Windows 上识别前台应用。
- 根据 CPU、时间和应用持续时间触发场景提醒。
- 优先调用 GPT-SoVITS，失败时回退到 EdgeTTS。
- 通过 WebSocket 驱动角色表情、动作和语音播放。
- 使用统一消息模型和会话编排处理桌面交互与场景事件。
- 仅在本机回环地址暴露 API，Electron renderer 不具备 Node.js 权限。

聊天已经经过统一会话入口，但回复生成仍是固定占位逻辑；大模型、QQ 机器人和正式安装包尚未实现。

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
npm start
```

`npm start` 会先通过 esbuild 生成本地 renderer bundle，再启动 Electron。页面不会从远程 CDN 执行脚本。

## Live2D 模型

仓库不附带受授权限制的 Cubism Core SDK 和 Live2D 模型。请将合法授权的模型放入：

```text
desktop-app/src/renderer/assets/models/hiyori/
└── hiyori.model3.json
```

模型相关的 `.moc3`、纹理、动作和表情文件必须保持模型配置声明的相对目录结构。资源缺失时 Electron 仍可启动，但控制台会显示模型加载错误。

Cubism 4 模型还需要官方 Cubism Core SDK。将获得授权的 `live2dcubismcore.min.js` 放到 renderer 的本地资源目录，并在 `index.html` 的 `dist/renderer.js` 之前通过本地 `<script>` 标签加载。不要恢复运行时 CDN 脚本。

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
| `ASSISTANT_HOST` | `127.0.0.1` | FastAPI 监听地址 |
| `ASSISTANT_PORT` | `8080` | FastAPI 监听端口 |
| `ASSISTANT_GPT_SOVITS_URL` | `http://127.0.0.1:9880` | GPT-SoVITS 服务地址 |
| `ASSISTANT_AUDIO_MAX_AGE_SECONDS` | `86400` | 生成音频的最大保留时间 |

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
| POST | `/api/chat/message` | 发送聊天消息 |
| GET | `/api/avatar/status` | 获取桌面端连接状态 |
| POST | `/api/avatar/action` | 触发角色动作 |
| WS | `/ws/avatar` | 双向角色消息通道 |

## 测试

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -v
npm --prefix desktop-app ci
npm --prefix desktop-app test
```

完整优化清单与实施状态见 [项目优化实施计划](docs/superpowers/plans/2026-07-22-project-hardening.md)。

## 项目结构

```text
backend/       FastAPI API、统一消息、会话编排、场景、TTS 和平台监控
config/        声线、回复和场景 YAML 配置
desktop-app/   Electron 主进程、preload 和 renderer
docs/          架构规格与分阶段实施计划
```

## License

MIT
