# Virtual Anime Assistant 项目优化实施计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 将当前技术原型优化为可重复安装、能完整演示且具备基本安全边界的 MVP。

**架构：** Electron 只负责桌面窗口、Live2D 渲染与用户交互；FastAPI 通过一个统一运行时管理窗口监控、场景判断、TTS 和消息分发。HTTP 与 WebSocket 是同一交互分发模块的适配器，不再通过本机 HTTP 在后端进程内部传递状态。

**技术栈：** Python 3.10+、FastAPI、Pydantic、psutil、EdgeTTS、Electron 28、PixiJS 7、esbuild、Node.js 18+

---

## 基线状态

- 后端路由可以导入，现有 3 个单元测试通过。
- Electron 依赖未安装，没有 `package-lock.json`。
- 托盘图标和 Live2D 模型缺失，桌面端不能完成真实角色渲染。
- WebSocket 入站消息只打印，没有进入消息路由。
- Electron 开启 Node.js 集成并执行远程 CDN 脚本，存在高风险执行边界。
- 后端监听所有网卡，CORS 允许所有来源。
- Windows 前台窗口监控尚未实现。
- 当前重构包含大量未提交变更，本计划不擅自提交用户工作；所有优化在 `codex/project-hardening` 分支完成。

## 优先级与完成标准

| 优先级 | 范围 | 完成标准 |
|---|---|---|
| P0 | 可运行性、安全边界、核心交互 | Electron 可构建渲染脚本；不执行远程脚本；后端仅监听本机；WebSocket 点击事件可以触发动作回推 |
| P1 | TTS、场景、跨平台、错误处理 | TTS 配置生效并可测试回退；场景规则支持优先级和配置冷却；Windows 可以读取前台应用；关键失败有日志或 HTTP 错误 |
| P2 | 测试、CI、依赖与开发体验 | 依赖有锁定方式；CI 覆盖 Python 测试和前端构建；README 与实际命令一致 |

## 任务 1：收紧 Electron 执行边界并建立前端构建

**文件：**

- 修改：`desktop-app/src/main.js`
- 修改：`desktop-app/src/preload.js`
- 修改：`desktop-app/src/renderer/index.html`
- 创建：`desktop-app/src/renderer/js/renderer.js`
- 修改：`desktop-app/src/renderer/js/websocket.js`
- 修改：`desktop-app/src/renderer/js/chat.js`
- 修改：`desktop-app/package.json`
- 修改：`desktop-app/electron-builder.yml`

- [x] 关闭 `nodeIntegration`，启用 `contextIsolation` 和沙箱，配置受限 preload。
- [x] 拒绝页面跳转和新窗口，托盘图标缺失时安全降级。
- [x] 移除远程 CDN 脚本，增加只允许本地脚本与本机 API 的 CSP。
- [x] 使用 esbuild 将 renderer 依赖打包为单文件。
- [x] 运行 `npm --prefix desktop-app run build:renderer`，预期退出码为 0。
- [x] 运行 `node --check desktop-app/src/main.js`，预期退出码为 0。

## 任务 2：收紧本地 API 并打通 WebSocket 交互

**文件：**

- 修改：`backend/main.py`
- 修改：`backend/api/app.py`
- 修改：`backend/api/ws.py`
- 修改：`backend/core/router.py`
- 修改：`desktop-app/src/renderer/js/websocket.js`
- 测试：`backend/tests/test_api.py`

- [x] 后端默认只监听 `127.0.0.1`，CORS 只允许 Electron 的本地来源。
- [x] 校验 WebSocket Origin，拒绝浏览器中的非本地网页。
- [x] 解析 WebSocket JSON 入站消息并统一交给消息路由。
- [x] 为未知消息、无效 JSON 和交互动作增加稳定错误响应。
- [x] 添加 WebSocket 入站分发测试。
- [x] 运行 `python3 -m unittest discover -s backend/tests -v`，预期全部通过。

## 任务 3：加深运行时模块并消除进程内自调用 HTTP

**文件：**

- 修改：`backend/api/app.py`
- 修改：`backend/agent/monitor.py`
- 修改：`backend/core/runtime.py`
- 修改：`backend/core/monitor.py`
- 测试：`backend/tests/test_runtime.py`

- [x] 窗口监控通过回调直接更新运行时，不再请求自己的 REST API。
- [x] 将 AppleScript、Windows API 和 CPU 采样移出异步事件循环的阻塞路径。
- [x] 为后台任务异常增加结构化日志。
- [x] 添加窗口变化去重和运行时状态测试。
- [x] 运行 `python3 -m unittest discover -s backend/tests -v`，预期全部通过。

## 任务 4：完善 TTS 配置、回退与音频生命周期

**文件：**

- 修改：`backend/core/config_loader.py`
- 修改：`backend/core/tts.py`
- 修改：`backend/api/tts.py`
- 修改：`config/voices.yml`
- 测试：`backend/tests/test_tts.py`

- [x] 从 YAML 读取 GPT-SoVITS 参考音频、提示文本和 EdgeTTS 回退声线。
- [x] GPT-SoVITS 请求禁用环境代理，并从环境变量读取服务地址。
- [x] TTS 失败返回明确的 `503`，限制空文本和超长文本。
- [x] 记录提供方失败原因，但不泄漏用户文本。
- [x] 定期清理过期音频，避免缓存无限增长。
- [x] 使用 mock 验证 GPT-SoVITS 成功、失败回退和全部失败路径。

## 任务 5：完善场景规则与 Windows 支持

**文件：**

- 修改：`backend/core/scenario.py`
- 修改：`config/scenarios.yml`
- 修改：`backend/agent/windows.py`
- 测试：`backend/tests/test_scenario.py`
- 测试：`backend/tests/test_windows_agent.py`

- [x] 场景支持 `priority` 和 `cooldownSeconds`，不再依赖 YAML 顺序和硬编码冷却。
- [x] 时间范围精确到分钟，正确处理跨午夜区间。
- [x] CPU 场景使用配置的持续时间，瞬时峰值不触发提醒。
- [x] 使用 Windows `ctypes` 与 psutil 实现前台应用读取。
- [x] 覆盖优先级、冷却、持续时间和平台错误路径测试。

## 任务 6：补齐工程化与项目文档

**文件：**

- 创建：`desktop-app/package-lock.json`
- 创建：`.github/workflows/ci.yml`
- 修改：`README.md`
- 修改：`.gitignore`
- 修改：本计划文件

- [x] 运行 `npm install --prefix desktop-app` 生成依赖锁文件。
- [x] 增加 `npm test` 和前端构建脚本。
- [x] CI 使用 Python 3.12 与 Node.js 20，运行后端测试和 renderer 构建。
- [x] README 记录真实安装、运行、测试步骤和模型资源要求。
- [x] 更新本计划所有检查框状态，并记录无法由代码自动解决的外部资源项。
- [x] 运行完整验证命令并记录结果。

## 外部资源与暂不自动处理的事项

- Live2D Cubism Core SDK 和模型受授权条款约束，不能凭空生成或从不明来源下载。代码应在资源缺失时给出明确提示，用户需要放入合法授权的模型。
- 角色品牌图标属于视觉设计资产。本轮让托盘功能在图标缺失时安全降级，不擅自确定最终品牌形象。
- 自动拉起和打包 Python 后端需要确定发布形态（系统 Python、PyInstaller 或独立安装器）。本轮先保证开发环境和 CI 可重复运行，在后续发布计划中选择方案。

## 完整验证命令

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -v
npm --prefix desktop-app ci
npm --prefix desktop-app run build:renderer
npm --prefix desktop-app test
git diff --check
```

## 2026-07-22 执行结果

- Python 编译检查通过。
- 后端单元与集成测试共 23 个，全部通过。
- `npm ci` 可以根据 `package-lock.json` 完成干净安装。
- renderer bundle 构建和 Node.js 语法检查通过。
- `npm audit --omit=dev --audit-level=high` 报告 0 个漏洞。
- 后端实际启动在 `127.0.0.1:8080`，状态接口返回 200，WebSocket 点击消息收到动作回推。
- Electron 43 首次启动需要从外部下载平台二进制；当前网络下载未在验收窗口内完成，因此没有把本机 GUI 首启记为通过。renderer 构建本身已经通过。
- Live2D 模型与 Cubism Core SDK 仍需要用户提供合法授权的资源，界面在缺失时会显示明确提示。
