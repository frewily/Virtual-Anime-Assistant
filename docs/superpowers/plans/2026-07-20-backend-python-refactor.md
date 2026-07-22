# Python FastAPI 后端重构 实现计划

> **面向 AI 代理的工作者：** 使用 subagent-driven-development 或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 用 Python FastAPI 完整替代 Java Spring Boot 后端，合并 Python Agent，保持 desktop-app 和 config 不变。

**架构：** FastAPI 后端 = API 层 (REST + WebSocket) + 核心服务层 (监控/场景/路由/TTS) + 内置 Agent (窗口监控异步任务)。所有模块依赖通过构造函数注入。

**技术栈：** Python 3.10+, FastAPI, uvicorn, psutil, websockets, PyYAML, httpx, pydantic

---

## 文件结构

```
backend/                      # 新建
├── main.py                   # 入口：启动 uvicorn
├── requirements.txt          # 依赖
├── api/                      # REST + WS 端点
│   ├── __init__.py
│   ├── status.py             # GET /api/status
│   ├── tts.py                # POST /api/tts/speak, GET /api/tts/voices
│   ├── window.py             # POST /api/report/window
│   ├── chat.py               # POST /api/chat/message
│   ├── avatar.py             # GET /api/avatar/status, POST /api/avatar/action
│   └── ws.py                 # WS /ws/avatar
├── core/                     # 核心服务
│   ├── __init__.py
│   ├── monitor.py            # 系统监控 (psutil)
│   ├── scenario.py           # 场景检测引擎
│   ├── router.py             # 消息路由中心
│   ├── tts.py                # 语音合成 (GPT-SoVITS + EdgeTTS 回退)
│   └── config_loader.py      # YAML 配置加载
└── agent/                    # 窗口监控 Agent
    ├── __init__.py
    ├── monitor.py            # 根循环，按平台选择
    ├── windows.py            # Windows 实现
    └── macos.py              # macOS 实现

删除:
  backend/ (Java, pom.xml, src/)
  agent/ (app_identifier.py, reporter.py, window_monitor.py, requirements.txt)
```

---

### 任务 1：清理旧代码

**文件：**
- 删除：`backend/` (Java)
- 删除：`agent/` (Python Agent)

- [ ] **步骤 1：删除 Java 后端**

```bash
rm -rf backend/
```

- [ ] **步骤 2：删除 Python Agent**

```bash
rm -rf agent/
```

- [ ] **步骤 3：Commit**

```bash
git add -A && git commit -m "chore: remove Java backend and Python agent for Python refactor"
```

---

### 任务 2：创建 FastAPI 项目骨架

**文件：**
- 创建：`backend/requirements.txt`
- 创建：`backend/main.py`
- 创建：`backend/api/__init__.py`
- 创建：`backend/core/__init__.py`
- 创建：`backend/agent/__init__.py`

- [ ] **步骤 1：创建 requirements.txt**

```txt
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
websockets>=11.0
psutil>=5.9.0
pyyaml>=6.0
httpx>=0.26.0
pydantic>=2.5.0
```

- [ ] **步骤 2：创建目录和 `__init__.py`**

```bash
mkdir -p backend/api backend/core backend/agent
touch backend/api/__init__.py backend/core/__init__.py backend/agent/__init__.py
```

- [ ] **步骤 3：创建 main.py 入口**

```python
import uvicorn

if __name__ == "__main__":
    uvicorn.run("api.app:app", host="0.0.0.0", port=8080, reload=True)
```

- [ ] **步骤 4：安装依赖验证骨架**

```bash
cd backend && pip install -r requirements.txt && python main.py
```
预期：uvicorn 报错找不到 `api.app:app`（正常，app 还没创建）

- [ ] **步骤 5：Commit**

```bash
git add backend/
git commit -m "feat: create FastAPI project skeleton"
```

---

### 任务 3：实现配置加载器

**文件：**
- 创建：`backend/core/config_loader.py`

- [ ] **步骤 1：编写 config_loader.py**

```python
import os
import yaml
from pathlib import Path

"""YAML 配置加载器。读取 workspace 根目录 config/ 下的配置文件。"""

# 项目根目录 (backend 的父级)
_workspace = Path(__file__).resolve().parent.parent.parent
_config_dir = _workspace / "config"


def _load_yaml(filename: str) -> dict:
    filepath = _config_dir / filename
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_scenarios() -> list:
    """获取场景规则列表"""
    data = _load_yaml("scenarios.yml")
    return data.get("scenarios", [])


def get_replies() -> dict:
    """获取回复模板"""
    data = _load_yaml("replies.yml")
    return data.get("replies", {})


def get_voices() -> dict:
    """获取声线配置"""
    data = _load_yaml("voices.yml")
    return data.get("voices", [])


def get_default_voice() -> str:
    data = _load_yaml("voices.yml")
    return data.get("default", {}).get("voiceId", "character_001")
```

- [ ] **步骤 2：验证配置加载**

```bash
cd backend && python -c "from core.config_loader import get_scenarios; print(len(get_scenarios()))"
```
预期：`4`

- [ ] **步骤 3：Commit**

```bash
git add backend/core/config_loader.py
git commit -m "feat: implement YAML config loader"
```

---

### 任务 4：实现系统监控服务

**文件：**
- 创建：`backend/core/monitor.py`

- [ ] **步骤 1：编写 monitor.py**

```python
import psutil
from datetime import timedelta

"""系统监控服务，使用 psutil 获取硬件状态（跨平台）。"""


class SystemMonitor:
    """系统硬件监控器"""

    def get_status(self) -> dict:
        """获取系统状态快照"""
        cpu_percent = psutil.cpu_percent(interval=0.5)
        memory = psutil.virtual_memory()
        uptime = timedelta(seconds=int(psutil.boot_time()))

        return {
            "cpu": {
                "percent": cpu_percent,
                "cores": psutil.cpu_count(logical=True),
            },
            "memory": {
                "total": _format_bytes(memory.total),
                "used": _format_bytes(memory.used),
                "percent": memory.percent,
            },
            "uptime": str(uptime),
        }


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
```

- [ ] **步骤 2：验证监控服务**

```bash
cd backend && python -c "from core.monitor import SystemMonitor; import json; print(json.dumps(SystemMonitor().get_status(), indent=2))"
```
预期：输出 CPU/内存/运行时间的 JSON

- [ ] **步骤 3：Commit**

```bash
git add backend/core/monitor.py
git commit -m "feat: implement system monitor service"
```

---

### 任务 5：实现场景检测引擎

**文件：**
- 创建：`backend/core/scenario.py`

- [ ] **步骤 1：编写 scenario.py**

```python
import random
import time
from datetime import datetime
from core.config_loader import get_scenarios

"""场景检测引擎。根据系统状态、窗口、时间匹配场景规则。"""


class ScenarioEngine:
    """场景检测引擎"""

    def __init__(self):
        self.scenarios = get_scenarios()
        self._cooldowns: dict[str, float] = {}  # 场景 -> 上次触发时间戳

    def detect(self, system_status: dict, window: dict | None = None) -> dict | None:
        """
        检测当前场景并返回匹配的响应。
        返回 {"text": str, "expression": str, "motion": str} 或 None
        """
        now = datetime.now()

        scenario = self._match_scenario(system_status, window, now)
        if scenario is None:
            return None

        sid = scenario["id"]
        if self._in_cooldown(sid, 60):  # 60 秒冷却
            return None

        self._cooldowns[sid] = time.time()
        response = scenario["response"]
        return {
            "text": random.choice(response["templates"]),
            "expression": response["expression"],
            "motion": response["motion"],
        }

    def _match_scenario(self, status: dict, window: dict | None, now: datetime) -> dict | None:
        for s in self.scenarios:
            trigger = s.get("trigger", {})
            ttype = trigger.get("type")

            if ttype == "cpu_threshold":
                cpu = status.get("cpu", {}).get("percent", 0)
                if cpu >= trigger.get("threshold", 100):
                    return s

            elif ttype == "time_range":
                hour = now.hour
                start_h = int(trigger["start"].split(":")[0])
                end_h = int(trigger["end"].split(":")[0])
                if start_h <= hour or hour < end_h:
                    return s

            elif ttype == "app_detect" and window:
                app_name = window.get("appName", "")
                for target in trigger.get("apps", []):
                    if target in app_name:
                        time_range = trigger.get("timeRange", {})
                        if _in_time_range(now, time_range):
                            return s

            elif ttype == "app_duration":
                pass  # 需要累计时长跟踪，Phase 2 实现

        return None

    def _in_cooldown(self, scenario_id: str, seconds: float) -> bool:
        last = self._cooldowns.get(scenario_id, 0)
        return (time.time() - last) < seconds


def _in_time_range(now: datetime, time_range: dict) -> bool:
    if not time_range:
        return True
    start_h = int(time_range["start"].split(":")[0])
    end_h = int(time_range["end"].split(":")[0])
    return start_h <= now.hour < end_h
```

- [ ] **步骤 2：验证场景引擎**

```bash
cd backend && python -c "
from core.scenario import ScenarioEngine
e = ScenarioEngine()
# 深夜场景
from datetime import datetime
import time
# 测试 CPU 阈值
result = e.detect({'cpu': {'percent': 90}}, None)
print('CPU scenario:', result)
"
```
预期：输出 CPU 高负载场景的台词

- [ ] **步骤 3：Commit**

```bash
git add backend/core/scenario.py
git commit -m "feat: implement scenario detection engine"
```

---

### 任务 6：实现 TTS 服务

**文件：**
- 创建：`backend/core/tts.py`

- [ ] **步骤 1：编写 tts.py**

```python
import os
import uuid
import httpx
from core.config_loader import get_voices, get_default_voice

"""语音合成服务。优先 GPT-SoVITS，回退 EdgeTTS。"""

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "..", "audio")
GPT_SOVITS_URL = "http://localhost:9880"
EDGETTS_VOICE = "zh-CN-XiaoxiaoNeural"


class TTSService:
    """语音合成服务"""

    def __init__(self):
        self.voices = get_voices()
        self.default_voice = get_default_voice()
        os.makedirs(AUDIO_DIR, exist_ok=True)

    async def synthesize(self, text: str, voice_id: str | None = None) -> dict | None:
        """
        合成语音，返回 {"audio_url": str, "text": str} 或 None。
        优先 GPT-SoVITS，失败则回退 EdgeTTS。
        """
        vid = voice_id or self.default_voice

        result = await self._try_gpt_sovits(text, vid)
        if result:
            return result

        result = await self._try_edgetts(text)
        return result

    async def _try_gpt_sovits(self, text: str, voice_id: str) -> dict | None:
        """调用 GPT-SoVITS API"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{GPT_SOVITS_URL}/tts",
                    json={"text": text, "text_language": "zh", "ref_audio_path": voice_id},
                )
                if resp.status_code != 200:
                    return None
                filename = f"{uuid.uuid4().hex}.wav"
                filepath = os.path.join(AUDIO_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(resp.content)
                return {"audio_url": f"/api/tts/audio/{filename}", "text": text}
        except Exception:
            return None

    async def _try_edgetts(self, text: str) -> dict | None:
        """使用 EdgeTTS 作为回退——当前版本返回文字仅文本，语音合成需要 edge-tts 库"""
        # Phase 3 后续：集成 edge-tts Python 库实际生成音频
        return None

    def get_voice_list(self) -> list:
        """获取可用声线列表"""
        return [
            {"id": v["id"], "name": v["name"], "description": v["description"]}
            for v in self.voices
        ]
```

- [ ] **步骤 2：验证 TTS 服务**

```bash
cd backend && python -c "from core.tts import TTSService; t = TTSService(); print(t.get_voice_list())"
```
预期：输出声线列表

- [ ] **步骤 3：Commit**

```bash
git add backend/core/tts.py
git commit -m "feat: implement TTS service with GPT-SoVITS and EdgeTTS fallback"
```

---

### 任务 7：实现消息路由服务

**文件：**
- 创建：`backend/core/router.py`

- [ ] **步骤 1：编写 router.py**

```python
import json
from core.scenario import ScenarioEngine
from core.tts import TTSService

"""中央消息路由。协调场景检测、TTS、WS 广播。"""


class MessageRouter:
    """消息路由中心"""

    def __init__(self):
        self.scenario_engine = ScenarioEngine()
        self.tts = TTSService()
        self._ws_broadcaster = None  # 由外部注入

    def set_ws_broadcaster(self, broadcaster):
        """注入 WebSocket 广播回调"""
        self._ws_broadcaster = broadcaster

    async def handle_scenario_check(self, system_status: dict, window: dict | None = None):
        """场景检测 → 生成响应 → TTS → 广播到桌面"""
        result = self.scenario_engine.detect(system_status, window)
        if result is None:
            return

        audio = await self.tts.synthesize(result["text"])

        payload = {
            "type": "speak",
            "text": result["text"],
            "expression": result["expression"],
            "motion": result["motion"],
            "audioUrl": audio["audio_url"] if audio else None,
        }
        self._broadcast(payload)

    def handle_chat(self, message: dict):
        """处理聊天消息"""
        # Phase 4: 规则匹配 + LLM 生成
        payload = {
            "type": "speak",
            "text": "主人说得有道理~",
            "expression": "happy",
            "motion": "wave",
        }
        self._broadcast(payload)

    def handle_interaction(self, action: dict):
        """处理 Live2D 交互"""
        payload = {
            "type": "action",
            "expression": "surprised",
            "motion": "tap_body",
        }
        self._broadcast(payload)

    def _broadcast(self, payload: dict):
        if self._ws_broadcaster:
            self._ws_broadcaster(json.dumps(payload))
```

- [ ] **步骤 2：验证基本构造**

```bash
cd backend && python -c "from core.router import MessageRouter; r = MessageRouter(); print('OK')"
```
预期：`OK`

- [ ] **步骤 3：Commit**

```bash
git add backend/core/router.py
git commit -m "feat: implement message router service"
```

---

### 任务 8：实现 API 层 - FastAPI 应用和路由

**文件：**
- 创建：`backend/api/app.py`
- 创建：`backend/api/status.py`
- 创建：`backend/api/tts.py`
- 创建：`backend/api/window.py`
- 创建：`backend/api/chat.py`
- 创建：`backend/api/avatar.py`
- 创建：`backend/api/ws.py`
- 修改：`backend/main.py`

- [ ] **步骤 1：编写 api/app.py（FastAPI 应用工厂）**

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.status import router as status_router
from api.tts import router as tts_router
from api.window import router as window_router
from api.chat import router as chat_router
from api.avatar import router as avatar_router
from api.ws import router as ws_router


def create_app() -> FastAPI:
    app = FastAPI(title="Desktop Assistant API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(status_router, prefix="/api")
    app.include_router(tts_router, prefix="/api")
    app.include_router(window_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(avatar_router, prefix="/api")
    app.include_router(ws_router)

    return app


app = create_app()
```

- [ ] **步骤 2：编写 api/status.py**

```python
from fastapi import APIRouter
from core.monitor import SystemMonitor

router = APIRouter(tags=["status"])
monitor = SystemMonitor()


@router.get("/status")
def get_status():
    return monitor.get_status()
```

- [ ] **步骤 3：编写 api/tts.py**

```python
from fastapi import APIRouter
from pydantic import BaseModel
from core.tts import TTSService

router = APIRouter(tags=["tts"])
service = TTSService()


class SpeakRequest(BaseModel):
    text: str
    voice_id: str | None = None


@router.post("/tts/speak")
async def speak(req: SpeakRequest):
    result = await service.synthesize(req.text, req.voice_id)
    return result or {"error": "synthesis failed"}


@router.get("/tts/voices")
def get_voices():
    return service.get_voice_list()
```

- [ ] **步骤 4：编写 api/window.py**

```python
from fastapi import APIRouter

router = APIRouter(tags=["window"])

# 窗口状态共享内存
_current_window: dict | None = None


@router.post("/report/window")
def report_window(window: dict):
    global _current_window
    _current_window = window
    return {"status": "ok"}
```

- [ ] **步骤 5：编写 api/chat.py**

```python
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["chat"])


class ChatMessage(BaseModel):
    source: str
    sender_id: str
    content: str


@router.post("/chat/message")
async def handle_message(msg: ChatMessage):
    # Phase 4: 调用 MessageRouter.handle_chat
    return {"reply": "收到消息了~", "status": "ok"}
```

- [ ] **步骤 6：编写 api/avatar.py**

```python
from fastapi import APIRouter

router = APIRouter(tags=["avatar"])


@router.get("/avatar/status")
def get_avatar_status():
    return {"connected": False, "expression": None}


@router.post("/avatar/action")
def perform_action(action: dict):
    return {"status": "ok", "action": action}
```

- [ ] **步骤 7：编写 api/ws.py**

```python
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

_sessions: list[WebSocket] = []


def broadcast_to_desktop(message: str):
    """全局广播函数，供 MessageRouter 调用"""
    for ws in _sessions:
        try:
            ws.send_text(message)
        except Exception:
            pass


@router.websocket("/ws/avatar")
async def avatar_websocket(ws: WebSocket):
    await ws.accept()
    _sessions.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            print(f"WS received: {data}")
            # Phase 5: 解析消息调用 MessageRouter
    except WebSocketDisconnect:
        _sessions.remove(ws)
```

- [ ] **步骤 8：修改 main.py**

```python
import uvicorn
import asyncio
from api.ws import broadcast_to_desktop
from core.router import MessageRouter

# 初始化消息路由并注入 WS 广播
router = MessageRouter()
router.set_ws_broadcaster(broadcast_to_desktop)

# 后台：每 10 秒检测场景
from core.monitor import SystemMonitor
monitor = SystemMonitor()

async def scenario_loop():
    while True:
        status = monitor.get_status()
        await router.handle_scenario_check(status)
        await asyncio.sleep(10)


async def main():
    config = uvicorn.Config("api.app:app", host="0.0.0.0", port=8080, reload=False)
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), scenario_loop())

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **步骤 9：启动服务验证**

```bash
cd backend && python main.py
```
预期：服务在 8080 端口启动，访问 `http://localhost:8080/api/status` 返回 JSON

- [ ] **步骤 10：验证 API 端点**

```bash
curl http://localhost:8080/api/status | python -m json.tool
```
预期：返回 `{"cpu": {"percent": ...}, "memory": {...}, "uptime": "..."}`

```bash
curl http://localhost:8080/api/tts/voices | python -m json.tool
```
预期：返回声线列表

- [ ] **步骤 11：Commit**

```bash
git add backend/api/ backend/main.py
git commit -m "feat: implement FastAPI API layer and WebSocket endpoint"
```

---

### 任务 9：实现 Agent 窗口监控（macOS）

**文件：**
- 创建：`backend/agent/monitor.py`
- 创建：`backend/agent/macos.py`
- 创建：`backend/agent/windows.py`

- [ ] **步骤 1：编写 agent/macos.py**

```python
"""macOS 窗口监控实现。"""


def get_foreground_app() -> dict | None:
    """获取 macOS 当前前台应用信息"""
    import subprocess

    try:
        script = '''
        tell application "System Events"
            set frontApp to first application process whose frontmost is true
            set appName to name of frontApp
        end tell
        return appName
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        app_name = result.stdout.strip()
        if not app_name:
            return None
        return {"appName": app_name, "appId": app_name}
    except Exception:
        return None
```

- [ ] **步骤 2：编写 agent/windows.py**

```python
"""Windows 窗口监控实现（占位，待 Phase 2 完善）。"""


def get_foreground_app() -> dict | None:
    return None
```

- [ ] **步骤 3：编写 agent/monitor.py**

```python
import sys
import asyncio
import httpx

"""窗口监控 Agent 根循环。按平台选择实现。"""

API_URL = "http://localhost:8080/api/report/window"


def _get_impl():
    if sys.platform == "darwin":
        from agent.macos import get_foreground_app
        return get_foreground_app
    elif sys.platform == "win32":
        from agent.windows import get_foreground_app
        return get_foreground_app
    else:
        return lambda: None


async def run():
    get_app = _get_impl()
    last_app = None

    while True:
        app = get_app()
        if app and app != last_app:
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(API_URL, json=app)
                print(f"[Window] {app['appName']}")
            except Exception as e:
                print(f"[Window Error] {e}")
            last_app = app
        await asyncio.sleep(3)
```

- [ ] **步骤 4：验证 Agent（macOS）**

```bash
cd backend && python -c "from agent.macos import get_foreground_app; print(get_foreground_app())"
```
预期：输出当前前台应用名称

- [ ] **步骤 5：Commit**

```bash
git add backend/agent/
git commit -m "feat: implement window monitoring agent with macOS support"
```

---

### 任务 10：最终验证

- [ ] **步骤 1：启动完整服务**

```bash
cd backend && python main.py
```
预期：服务启动在 8080 端口，输出场景检测日志

- [ ] **步骤 2：测试所有端点**

```bash
echo "=== Test /api/status ==="
curl -s http://localhost:8080/api/status | python -m json.tool

echo "=== Test /api/tts/voices ==="
curl -s http://localhost:8080/api/tts/voices | python -m json.tool

echo "=== Test /api/tts/speak ==="
curl -s -X POST http://localhost:8080/api/tts/speak -H "Content-Type: application/json" -d '{"text":"你好"}'

echo "=== Test /api/avatar/status ==="
curl -s http://localhost:8080/api/avatar/status | python -m json.tool

echo "=== Test /api/report/window ==="
curl -s -X POST http://localhost:8080/api/report/window -H "Content-Type: application/json" -d '{"appName":"Safari","appId":"com.apple.Safari"}'

echo "=== Test /api/chat/message ==="
curl -s -X POST http://localhost:8080/api/chat/message -H "Content-Type: application/json" -d '{"source":"desktop","senderId":"user","content":"你好"}'
```
预期：全部返回 200 和有效 JSON

- [ ] **步骤 3：Commit**

```bash
git add -A && git commit -m "refactor: complete Python FastAPI backend migration"
```
