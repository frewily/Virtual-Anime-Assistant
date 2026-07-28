# 后端 Python/FastAPI 重构设计

> 状态: 待实施 | 日期: 2026-07-20

## 1. 背景与动机

- Java Spring Boot 后端过于臃肿（JVM ~200MB，启动 5s+），不适合这种轻量桌面助手
- Python Agent 独立部署，增加运维成本；与后端语言不统一
- 选择 FastAPI 统一技术栈，代码量降 70%，启动 < 1 秒，天然跨平台

## 2. 目标

1. 用 Python FastAPI 完整替代 Java Spring Boot 后端
2. 合并 Python Agent 到后端内部
3. `desktop-app/` 零改动，只改 API 地址
4. `config/` YAML 配置保持不变，格式兼容

## 3. 新架构

```
desktop-app/ (Electron, 不变)
        │ HTTP + WebSocket
        ▼
backend/ (Python FastAPI)
  ├── api/           # REST + WebSocket 端点
  ├── core/          # 核心服务 (监控/场景/路由/TTS)
  ├── agent/         # 窗口监控 (内置异步任务)
  ├── config/        # YAML 配置加载器
  ├── main.py        # 应用入口
  └── requirements.txt
```

## 4. 模块详述

### 4.1 API 层 (`backend/api/`)

| 文件 | 端点 | 说明 |
|------|------|------|
| `status.py` | `GET /api/status` | 系统状态 |
| `tts.py` | `POST /api/tts/speak` / `GET /api/tts/voices` | TTS |
| `window.py` | `POST /api/report/window` | 窗口上报 |
| `chat.py` | `POST /api/chat/message` | 聊天 |
| `avatar.py` | `GET /api/avatar/*` / `POST /api/avatar/action` | Live2D 控制 |
| `ws.py` | `WS /ws/avatar` | Live2D 实时通信 |

### 4.2 核心服务 (`backend/core/`)

| 文件 | 职责 | 替代对象 |
|------|------|----------|
| `monitor.py` | 系统监控（psutil） | `SystemMonitorService` + OSHI |
| `scenario.py` | 场景检测引擎 | `ScenarioEngine` |
| `router.py` | 消息路由中心 | `MessageRouterService` |
| `tts.py` | 语音合成封装 | `TTSService` |
| `config_loader.py` | YAML 配置加载 | 各 Config 类 |

### 4.3 Agent (`backend/agent/`)

由后台异步任务（asyncio）驱动，无需单独进程：
- `monitor.py` — 窗口监控根循环，按平台选择实现
- `windows.py` — Windows 平台 (`pywin32`)
- `macos.py` — macOS 平台 (`pyobjc`)

### 4.4 配置 (`backend/config/`)

`config_loader.py` 读取 `config/` 目录下的 YAML：
- `scenarios.yml` — 场景规则
- `replies.yml` — 回复模板
- `voices.yml` — 声线配置

## 5. 依赖

```
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
websockets>=11.0
psutil>=5.9.0
pyyaml>=6.0
httpx>=0.26.0        # TTS 外部 API 调用
pydantic>=2.5.0
```

## 6. 文件变更清单

| 操作 | 路径 |
|------|------|
| 删除 | `backend/` (Java, pom.xml) |
| 新建 | `backend/` (Python 项目) |
| 删除 | `agent/` (合并到 backend) |
| 保持 | `desktop-app/` (零改动) |
| 保持 | `config/` (零改动) |
| 保持 | `docs/` (追加设计文档) |
