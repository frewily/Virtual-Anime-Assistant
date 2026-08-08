# 本地 Web 配置界面实施计划与完成记录

> 本文根据已批准设计、提交历史与会话执行记录重建。原计划曾位于临时工作树且未进入 Git，因临时目录被清理而丢失；本文用于准确记录已经执行的范围、提交与验证关卡，不声称逐字恢复原来的 67 个 TDD 步骤。

## 1. 依据与状态

- 设计依据：[本地 Web 配置界面设计规格](../specs/2026-08-01-web-settings-interface-design.md)
- 设计提交：`1a97314`（`docs: 设计本地 Web 配置界面`）
- 实施分支：`codex/web-settings-interface`
- 实施范围：LLM、QQ / OneBot、GPT-SoVITS、默认音色与音频保留时间
- 任务状态：12 / 12 已完成；命令行验证与浏览器验收证据均已记录

## 2. 目标与交付边界

本次实施在现有 FastAPI 服务中加入仅限本机的设置页面，并通过 Electron 托盘打开固定地址。用户可以设置本地管理密码，查看配置来源，修改非敏感字段，以保留、替换或删除的方式管理秘密，并使用未保存草稿执行连接测试。

安全与兼容性边界如下：

- 配置优先级固定为「默认值 < `settings.json` < 系统凭据库 < 环境变量」。
- 环境变量接管字段只读，服务端拒绝保存对应变更。
- 秘密只写入操作系统凭据库，`settings.json`、API 响应、日志和异常均不得包含明文。
- 设置页面和设置 API 只允许回环客户端、精确 `Host` 和精确本地 `Origin`。
- 写操作需要有效密码会话和 CSRF Token；保存后必须重启后端才会应用新配置。
- 首版不包含热更新、远程访问、多用户、NapCat 生命周期管理或音色文件编辑。

## 3. 架构与数据流

| 模块 | 职责 |
|---|---|
| `SettingsPaths` / 持久化模型 | 定义平台配置目录、严格版本化 JSON 结构与秘密操作模型 |
| `SettingsFileStore` | 严格读取、原子写入 `settings.json` 与保存日志 |
| `KeychainSecretStore` / 事务协调器 | 通过不透明引用管理系统凭据，并协调文件与凭据库的可恢复保存 |
| `SettingsResolver` | 合并默认值、文件、凭据库和环境变量，输出运行时设置与脱敏来源元数据 |
| `SettingsAuthService` | 管理密码哈希、登录限速、内存会话与 CSRF Token |
| `SettingsValidationService` | 共享草稿校验以及 LLM、QQ、TTS 的脱敏连接测试 |
| `SettingsService` | 提供首次设密、会话、配置快照、保存、音色与测试的统一门面 |
| 设置路由与安全中间件 | 提供 `/settings`、`/api/settings/*`，执行本机网络与浏览器安全策略 |
| 静态页面与 Electron 入口 | 呈现桌面控制台，并从托盘打开固定本地 URL |

启动时，应用工厂只注册惰性工厂，不读取配置或凭据；首次需要运行时设置或设置服务时才读取磁盘。保存时先校验完整草稿和修订号，再写入新凭据引用、保存恢复日志、原子替换配置文件，最后清理旧引用。连接测试使用草稿快照，不持久化草稿，也不改变当前运行时。

## 4. 执行方法与质量关卡

每个任务均按以下顺序执行：

1. 先编写失败测试并确认 RED。
2. 实现最小功能并确认 GREEN。
3. 扩展边界测试、执行实现者自审并提交。
4. 由全新规格审查者检查是否完整符合设计，不接受范围缺失或额外行为。
5. 规格通过后，再由全新代码质量审查者检查并发、资源、安全、可维护性与测试质量。
6. 审查发现的问题退回原实现者修复并重新审查，双重审查通过后才进入下一任务。

任务 12 的文档同样先运行一次性文档契约检查确认 RED，再修改 README 和本记录。所有完成声明必须以当前工作树中新鲜运行的验证命令为依据。

## 5. 任务与实际提交

### [x] 任务 1：严格配置模型与路径

文件：`backend/settings/models.py`、`backend/settings/paths.py`、`backend/settings/__init__.py`、`backend/requirements.txt`、`backend/tests/test_settings_models.py`

关键验收：

- 使用严格 Pydantic 模型保存 LLM、QQ、TTS 和认证记录，拒绝未知字段与字符串化布尔值、整数和 QQ ID。
- 提供 `retain`、`replace`、`delete` 秘密操作，并确保校验错误不回显秘密。
- 使用 `platformdirs` 生成平台配置目录，引入固定版本的 `platformdirs` 与 `keyring`。

实际提交：`36f15abc`、`8cbe91fe`。TDD、自审、规格审查和代码质量审查均通过。

### [x] 任务 2：原子文件存储

文件：`backend/settings/file_store.py`、`backend/tests/test_settings_file_store.py`

关键验收：

- 严格读取版本化配置和保存日志，未知版本或无效 JSON 失败关闭。
- 在同目录写临时文件，执行文件 `fsync`、原子替换与目录 `fsync`。
- 权限错误、进程控制异常和序列化失败不会遗留临时文件或泄漏输入。

实际提交：`ba878f28`、`4b7e8b86`、`9ba47e22`。TDD、自审与双重审查均通过。

### [x] 任务 3：凭据库与跨存储事务

文件：`backend/settings/secrets.py`、`backend/settings/transactions.py`、`backend/settings/file_store.py`、对应 `test_settings_*` 测试

关键验收：

- 通过 `keyring` 适配操作系统凭据库，缺失或不可用时稳定、脱敏地失败关闭。
- 使用版本化秘密引用和保存日志完成文件与凭据库的可恢复事务。
- 支持中断恢复、删除型事务、引用碰撞重试、活动引用保护和磁盘快照 CAS，拒绝陈旧保存。

实际提交：`167bddc9`、`13112eef`。TDD、自审与双重审查均通过。

### [x] 任务 4：统一解析配置来源

文件：`backend/settings/resolver.py`、`backend/llm/config.py`、`backend/channels/onebot/config.py`、相关测试

关键验收：

- 按「默认值 < 文件 < 凭据库 < 环境变量」解析 LLM、QQ 和 TTS 全部字段。
- 为页面输出来源、只读、已配置、缺失和凭据库可用状态。
- 深度冻结展示模型，并在赋值、复制、序列化与 `repr` 边界阻止秘密泄漏。

实际提交：`0de64e0c`、`e147e79d`、`1d92bbb9`、`9409427c`。TDD、自审与双重审查均通过。

### [x] 任务 5：运行时接线与 TTS

文件：`backend/core/runtime.py`、`backend/core/config_loader.py`、`backend/core/tts.py`、`backend/api/app.py`、相关测试

关键验收：

- `AssistantRuntime` 可以接收统一运行时设置，显式依赖注入仍具有更高优先级。
- 应用工厂保持无导入或构造副作用，生命周期入口按需解析设置。
- TTS 接收地址、默认音色和保留时间；音色 YAML 严格加载、深隔离，音频清理跳过符号链接并隔离单项错误。

实际提交：`dcd64b70`、`e95a6cdb`、`1a67dee6`。TDD、自审与双重审查均通过。

### [x] 任务 6：本地设置鉴权

文件：`backend/settings/auth.py`、`backend/tests/test_settings_auth.py`

关键验收：

- 使用固定参数 `scrypt` 哈希长度为 10～128 个字符的密码，并拒绝恶意高成本认证记录。
- 提供 30 分钟绝对有效期的内存会话、CSRF Token、注销和常量时间比较。
- 以 60 秒滚动窗口限制登录失败，约束客户端、会话和并发密码校验资源；慢哈希不得持有全局会话锁。

实际提交：`21b37550`、`bcdc6d43`、`c6827f18`、`493a339f`。TDD、自审与双重审查均通过。

### [x] 任务 7：共享校验与连接测试

文件：`backend/settings/validation.py`、`backend/tests/test_settings_validation.py`

关键验收：

- 严格校验 LLM、QQ、TTS 草稿；HTTP(S) URL 禁止 userinfo、查询参数、片段和控制字符。
- 校验 QQ Token、白名单、速率关系、数值范围和默认音色存在性。
- LLM 发送最小真实请求并使用固定 15 秒超时；QQ 只做本地校验并返回当前运行状态；TTS 先探测 `/openapi.json`，必要时回退 `/`，固定 10 秒且不生成音频。
- 外部认证、服务、协议、网络和超时错误分类稳定，不读取或回显不可信错误正文。

实际提交：`1b4bea87`、`43cdb611`、`fe94f3db`、`47d9c27a`。TDD、自审与双重审查均通过。

### [x] 任务 8：设置服务门面

文件：`backend/settings/service.py`、`backend/tests/test_settings_service.py`

关键验收：

- 提供首次设密、会话状态、登录、退出、脱敏配置快照与音色摘要。
- 保存完整版本化草稿，支持凭据保留、替换和删除；环境变量接管字段禁止修改。
- 保存返回 `restartRequired`；损坏配置不被覆盖，运行时只回退到默认值和有效环境变量。
- 并发首次设密、陈旧标签页、秘密输入变更、中断回滚和错误序列化均安全处理。

实际提交：`5f874605`、`ffd6a002`、`aa725761`、`0ef8f6a6`、`cd36e59e`、`d1652f63`。TDD、自审与双重审查均通过。

### [x] 任务 9：本机安全 API

文件：`backend/settings/security.py`、`backend/settings/routes.py`、`backend/api/app.py`、`backend/api/dependencies.py`、`backend/settings/service.py`、`backend/tests/test_settings_api.py`

关键验收：

- 设置路径只允许 `127.0.0.1` / `::1` 客户端和精确 `Host: 127.0.0.1:8080`；状态变更要求精确本地 `Origin`。
- 受保护读取需要会话，写操作需要会话和 CSRF Token；Cookie 为 host-only、`HttpOnly`、`SameSite=Strict`、`Path=/`。
- 全部设置响应包含 `no-store`、CSP、禁止嗅探、禁止框架和无来源泄漏等安全头。
- 提供 session、setup、login、logout、config、voices 和 3 类连接测试端点；应用工厂仍按并发安全方式惰性构造服务。

实际提交：`e73d7d5e`、`bb778dcc`、`2b0471ac`、`d1f82820`、`0dbea034`。TDD、自审与双重审查均通过。

### [x] 任务 10：Web 设置页面

文件：`backend/settings/static/index.html`、`backend/settings/static/settings.css`、`backend/settings/static/settings.js`、设置 API / Service 回归测试、`desktop-app/tests/settings-ui-*.test.js`

关键验收：

- 实现暖色桌面控制台，覆盖首次设密、登录、LLM、QQ、语音、来源标签、环境接管和凭据库状态。
- 支持连接测试、保存后重启提示、键盘与响应式布局，不执行外部脚本，不使用 `innerHTML`。
- 会话失效清空秘密输入；异步请求通过代次和快照避免陈旧响应覆盖新会话、新编辑或新状态。

实际提交：`466b2243`、`dc6b2aac`、`94090918`、`80a43511`、`f0866691`。TDD、自审与双重审查均通过。

浏览器验收发现浏览器自动请求 favicon 时返回 404，随后以 `f2b644b` 增量修复：新增本地自包含的 32 × 32 SVG，在 `index.html` 中显式引用 `/settings/favicon.svg`，并只通过现有静态资源白名单提供 GET / HEAD。favicon 实测返回 HTTP 200，HEAD 无响应体，`no-store` 与既有安全响应头保持不变；未知路径和遍历路径继续拒绝。该增量的规格审查与代码质量审查均为 PASS。

### [x] 任务 11：Electron 固定入口

文件：`desktop-app/src/main.js`、`desktop-app/tests/settings-entry-contract.test.js`

关键验收：

- 托盘「设置」只调用 `shell.openExternal('http://127.0.0.1:8080/settings')`。
- 打开失败不会退出主进程，不允许动态 URL，也不新增凭据 IPC 通道。

实际提交：`e6846bd5`。TDD、自审与双重审查均通过。

### [x] 任务 12：README、计划重建与完整验证

文件：`README.md`、`docs/superpowers/plans/2026-08-01-web-settings-interface.md`

关键验收：

- README 说明入口、首次密码、会话、安全边界、凭据库、配置优先级、保存后重启、错误恢复与测试方法。
- 明确 Linux 凭据库不是首版正式支持目标；不提供真实密钥、Token 或测试密码示例。
- 本文件按批准设计、Git 历史与执行记录重建，记录任务、文件、验收条件和实际提交，不伪造已丢失计划的原文。
- 执行后端全量测试、桌面单元测试、JavaScript 语法检查、桌面完整测试与本机浏览器验收，并记录最终证据。

实际提交：本文件所在的任务 12 文档提交；提交对象无法在自身内容中记录自身 SHA，可通过 `git log -1 -- README.md docs/superpowers/plans/2026-08-01-web-settings-interface.md` 审计。文档规格与代码质量审查由主控在提交后执行。

## 6. 命令行验证记录

以下命令必须在分支最终状态重新运行，不能使用历史输出替代：

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -p 'test_*.py' -v
python3 -m unittest discover -s backend/tests -p 'test_settings_*.py' -v
npm --prefix desktop-app run test:unit
node --check backend/settings/static/settings.js
node --check desktop-app/src/main.js
npm --prefix desktop-app test
git diff --check
git status --short --branch
```

任务 12 的命令行证据如下：

| 检查 | 结果 | 证据 |
|---|---|---|
| Python 编译 | 通过 | `python3 -m compileall -q backend`，退出码 0 |
| 后端全量测试 | 通过 | 577 项通过，0 项失败，最终复跑耗时 8.155 秒 |
| Settings 定向测试 | 通过 | 190 项通过，0 项失败，最终复跑耗时 4.883 秒 |
| Electron 单元测试 | 通过 | `npm run test:unit` 为 35 / 35 项通过，0 项失败 |
| JavaScript 语法 | 通过 | `settings.js` 与 `main.js` 的 `node --check` 均为退出码 0 |
| Electron 完整测试 | 通过 | 初次因没有 `node_modules` 而报 `esbuild: command not found`；从 npm 官方源执行锁定安装后，`npm test` 的单元测试阶段 35 / 35 项通过，renderer 构建、主进程与 preload 语法检查全部通过 |
| 文档契约与 Git 检查 | 通过 | 一次性契约脚本、`git diff --check` 均为退出码 0；最终状态在提交前再次检查 |

## 7. 浏览器验收证据

主控于 2026-08-08 使用全新临时配置目录启动 `127.0.0.1:8080` 本机服务，并通过 Playwright 完成下表所列场景。验收未使用远程部署或宽松 Host；表外场景不属于本轮浏览器验收结论。

| 项目 | 最终证据 |
|---|---|
| 会话与保存 | 首次设密通过；保存返回 `restartRequired` 并显示重启提示；登出后秘密输入已清除；重新登录通过 |
| 桌面布局 | 垂直页签显示正常 |
| 移动布局 | 390 × 844 视口显示水平页签；ArrowRight 键盘导航通过；`clientWidth = 390`、`scrollWidth = 390`，无横向溢出 |
| favicon | `/settings/favicon.svg` 实测 HTTP 200；资源为本地自包含 32 × 32 SVG，不产生外部请求 |
| 浏览器错误 | `consoleErrors = 0`、`pageErrors = 0` |
| 外部资源请求 | `externalRequests = 0` |
| 截图 | 验收产物：`vaa-settings-desktop.png`、`vaa-settings-mobile.png` |

## 8. 重建说明与已知环境约束

- 原计划从未进入 Git，因此无法从对象数据库逐字恢复；本记录只采用可验证的设计、代码、测试和提交事实。
- 设计规格示例写的是 Windows `%APPDATA%`，当前实现使用 `platformdirs.user_config_path(..., roaming=False)`，README 按实现记录为 `%LOCALAPPDATA%`。
- 系统凭据适配器使用通用 `keyring` API，但首版只把 macOS Keychain 和 Windows Credential Manager 作为支持目标；Linux 缺少可用后端时会安全地报告不可用。
- 工作树初始没有 `desktop-app/node_modules`。`package-lock.json` 与 npm 配置均指向官方仓库；执行 `npm --prefix desktop-app ci --registry=https://registry.npmjs.org --replace-registry-host=always` 安装 410 个锁定包后完成桌面全量测试，未修改锁文件。npm 同时报告 4 个 high severity 依赖审计项，属于现有依赖树的后续维护事项，本任务未擅自执行可能改写版本的 `npm audit fix`。
