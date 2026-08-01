# 本地 Web 配置界面设计规格

## 1. 背景与目标

Virtual Anime Assistant 当前主要通过环境变量与 YAML 文件完成配置。此方式适合开发，但普通用户需要手工编辑文件、记忆环境变量名称，并在配置错误时自行排查。

本项目将在现有 FastAPI 服务中增加仅限本机访问的 Web 配置界面，并在 Electron 托盘菜单中提供固定入口。首版覆盖以下配置：

- OpenAI 兼容的 LLM 服务；
- QQ / OneBot 通道；
- GPT-SoVITS 服务、默认音色和生成音频保留时间。

保存后的配置只在后端重启后生效。环境变量始终拥有最高优先级，密钥和 Token 只存入操作系统凭据库，不写入明文配置文件。

## 2. 成功标准

实现完成后，用户可以：

1. 通过 `http://127.0.0.1:8080/settings` 或 Electron 托盘菜单打开同一设置页面；
2. 使用本地管理密码登录，并安全地查看和修改非敏感配置；
3. 新增、替换或明确删除 LLM API Key 与 QQ Access Token，而页面和 API 均不会回传已有秘密；
4. 识别被环境变量接管的字段，且无法在页面中修改这些字段；
5. 使用尚未保存的草稿执行 LLM、QQ 和 TTS 检查；
6. 保存配置并获得「重启后生效」提示；
7. 在钥匙串不可用、保存中断或外部服务不可达时得到脱敏且可操作的错误提示。

## 3. 首版范围

### 3.1 LLM 配置

管理以下字段：

- 启用状态；
- OpenAI 兼容服务地址；
- 模型名称；
- API Key；
- 请求超时；
- 上下文消息数上限；
- 上下文字符数上限；
- 模型工具调用开关。

对应现有环境变量：

- `ASSISTANT_LLM_ENABLED`
- `ASSISTANT_LLM_BASE_URL`
- `ASSISTANT_LLM_API_KEY`
- `ASSISTANT_LLM_MODEL`
- `ASSISTANT_LLM_TIMEOUT_SECONDS`
- `ASSISTANT_LLM_MAX_CONTEXT_MESSAGES`
- `ASSISTANT_LLM_MAX_CONTEXT_CHARS`
- `ASSISTANT_LLM_TOOL_CALLING_ENABLED`

### 3.2 QQ / OneBot 配置

管理以下字段：

- 启用状态；
- Access Token；
- 允许的群 ID；
- 允许的用户 ID；
- 每分钟速率上限；
- 突发请求上限；
- 最大并发数；
- OneBot Action 超时。

对应现有环境变量：

- `ASSISTANT_QQ_ENABLED`
- `ASSISTANT_QQ_ACCESS_TOKEN`
- `ASSISTANT_QQ_ALLOWED_GROUP_IDS`
- `ASSISTANT_QQ_ALLOWED_USER_IDS`
- `ASSISTANT_QQ_RATE_PER_MINUTE`
- `ASSISTANT_QQ_RATE_BURST`
- `ASSISTANT_QQ_MAX_CONCURRENCY`
- `ASSISTANT_QQ_ACTION_TIMEOUT_SECONDS`

### 3.3 TTS 配置

管理以下字段：

- GPT-SoVITS 服务地址；
- 默认音色 ID；
- 生成音频保留时间，单位为秒。

环境变量覆盖项为：

- `ASSISTANT_GPT_SOVITS_URL`
- `ASSISTANT_TTS_DEFAULT_VOICE_ID`（新增）
- `ASSISTANT_AUDIO_MAX_AGE_SECONDS`

音色定义继续由 `config/voices.yml` 管理。Web 页面只读取可用音色并选择默认项，不提供音色新增、编辑、上传、试听或删除功能。所选音色 ID 必须存在于该文件；文件不可读或选项失效时禁止保存，并显示明确错误。

### 3.4 明确不做

首版不包含：

- 运行时热更新；
- 非本机访问；
- 多用户与权限分级；
- NapCat 自动安装或生命周期管理；
- 音色定义管理与参考音频上传；
- 通用文件编辑器；
- Electron 内嵌凭据输入或凭据传输。

## 4. 方案选择

评估过以下 3 种实现方式：

1. **集成到现有 FastAPI 应用（采用）**：复用端口和运行方式，将设置模块与聊天运行时隔离。部署简单，且 Electron 只需打开固定 URL。
2. **独立设置服务**：隔离更彻底，但会增加进程、端口、启动协调和打包成本，首版收益不足。
3. **Electron 原生设置窗口**：桌面体验统一，但会让 Electron 接触秘密，并形成另一套 API 与状态管理边界。

采用方案 1。设置路由集成到 FastAPI，但配置解析、存储、鉴权与验证均作为独立模块，不直接依赖聊天会话或 `AssistantRuntime` 的可变状态。

## 5. 界面设计

界面沿用已批准的暖色桌面控制台风格，使用轻量 HTML、CSS 与 JavaScript，不引入前端构建链。

页面结构：

- 左侧导航：模型、QQ、语音；
- 右侧表单：当前分区的字段、来源状态和连接测试；
- 顶部安全状态：登录状态、操作系统凭据库状态；
- 底部固定提示：存在未保存修改或保存后需要重启；
- 窄屏下左侧导航折叠为顶部页签，表单保持单列。

字段状态必须同时使用文字和样式表达，不只依赖颜色：

- `默认值`：来自程序默认配置；
- `已保存`：来自 `settings.json`；
- `系统凭据库`：秘密已配置，但不显示秘密内容；
- `环境变量接管`：字段只读并显示对应环境变量名称。

秘密字段的空输入表示「保留现有值」。替换秘密必须输入新值并选择替换；删除必须使用独立操作并再次确认，避免把空字符串误当成删除。

## 6. 模块边界

### 6.1 `SettingsResolver`

负责把配置解析为统一、不可变的有效配置对象。合并顺序从低到高为：

1. 程序默认值；
2. `settings.json` 中的非敏感配置；
3. 配置文件引用的操作系统凭据；
4. 环境变量。

解析结果同时携带每个字段的来源元数据，供运行时和 Web 页面共用。现有 LLM、OneBot 与 TTS 初始化逻辑改为消费解析后的配置对象，避免 Web 配置与环境变量配置形成两条不一致的路径。

### 6.2 `SettingsFileStore`

负责读取、校验和原子写入带版本号的 `settings.json`。默认位置使用操作系统应用数据目录：

- macOS：`~/Library/Application Support/Virtual Anime Assistant/settings.json`
- Windows：`%APPDATA%\\Virtual Anime Assistant\\settings.json`

测试和开发环境可以注入其他目录。写入流程使用同目录临时文件、刷新文件内容并原子替换目标文件。该文件不得包含 API Key、Access Token、管理密码或会话标识。

### 6.3 `KeychainSecretStore`

负责访问 macOS Keychain 或 Windows Credential Manager。秘密以随机版本 ID 引用，配置文件只保存引用：

```json
{
  "llmApiKeyRef": "llm-api-key:7d0c...",
  "qqAccessTokenRef": "qq-access-token:9af1..."
}
```

凭据库不可用时采用失败关闭策略：可以读取和保存非秘密字段，但涉及新增、替换或删除秘密的操作必须失败，且不得回退到明文存储。

### 6.4 `SettingsAuthService`

负责管理密码与内存会话：

- 首次访问在尚未设置密码时进入初始化页；
- 密码长度为 10～128 个字符；
- 使用随机 16 字节盐和 `scrypt` 保存派生值，只持久化算法参数、盐和哈希；
- 使用常量时间比较验证密码；
- 连续登录失败按来源地址执行内存限速；
- 登录成功创建 30 分钟绝对有效期的随机内存会话，后端重启后全部失效。

Cookie 名为 `vaa_settings_session`，设置 `HttpOnly`、`SameSite=Strict`、`Path=/`，不设置 `Domain`，从而保持 host-only。由于首版固定使用本地 HTTP，不设置与 HTTP 不兼容的 `Secure`；如果未来引入 HTTPS，必须同时启用 `Secure`。

### 6.5 `SettingsValidationService`

负责统一的字段格式校验和草稿连接测试。保存和测试调用同一套校验规则，避免「测试成功但无法保存」或「保存后启动失败」。外部错误只映射为超时、认证失败、地址不可达、响应不兼容等稳定类别，日志也不得记录请求头、秘密或完整外部响应正文。

### 6.6 设置路由与静态资源

设置模块提供 `/settings` 页面和 `/api/settings/*` API。路由依赖只访问上述服务，不直接修改正在运行的 `AssistantRuntime`。保存成功只返回 `restartRequired: true`。

### 6.7 Electron 入口

托盘菜单新增「设置」，调用系统默认浏览器打开固定地址 `http://127.0.0.1:8080/settings`。不得接收页面传入的 URL，不使用 Electron IPC 传递密码、密钥、Token 或设置内容。

## 7. 持久化模型

`settings.json` 使用显式模式版本，首版结构如下：

```json
{
  "schemaVersion": 1,
  "auth": {
    "algorithm": "scrypt",
    "n": 32768,
    "r": 8,
    "p": 1,
    "salt": "base64...",
    "hash": "base64..."
  },
  "llm": {
    "enabled": false,
    "baseUrl": null,
    "model": null,
    "timeoutSeconds": 60,
    "maxContextMessages": 20,
    "maxContextChars": 12000,
    "toolCallingEnabled": false,
    "apiKeyRef": null
  },
  "qq": {
    "enabled": false,
    "allowedGroupIds": [],
    "allowedUserIds": [],
    "ratePerMinute": 10,
    "rateBurst": 2,
    "maxConcurrency": 4,
    "actionTimeoutSeconds": 10,
    "accessTokenRef": null
  },
  "tts": {
    "gptSovitsUrl": "http://127.0.0.1:9880",
    "defaultVoiceId": "character_001",
    "audioMaxAgeSeconds": 86400
  }
}
```

未知的 `schemaVersion` 必须拒绝加载并给出可恢复提示，不得静默覆盖。解析器可以为缺失字段补默认值，但不得接受未知字段，从而尽早发现拼写或版本错误。

## 8. 跨存储原子保存

文件系统和操作系统凭据库无法参与同一个原子事务，因此使用无秘密的保存日志 `settings.save-journal.json` 协调恢复：

1. 校验完整草稿并生成新的秘密版本引用；
2. 原子写入保存日志，其中只包含旧引用、新引用和事务阶段；
3. 把需要新增或替换的秘密写入新引用；
4. 原子替换 `settings.json`，使其指向新引用；
5. 删除不再使用的旧凭据；
6. 删除保存日志并返回成功。

启动恢复规则：

- 如果当前 `settings.json` 已引用新版本，则保留新凭据并继续清理旧凭据；
- 如果配置仍引用旧版本，则保留旧凭据并清理本次新建的凭据；
- 凭据清理失败不破坏当前配置，保留日志以便下次启动重试；
- 恢复过程只操作日志中列出的精确引用，不枚举或删除其他应用凭据。

这样可以确保任意时刻至少有一组完整配置可用，同时避免把秘密放入事务日志。

## 9. HTTP 安全边界

设置页面与所有 `/api/settings/*` 路由都执行独立安全检查：

- 客户端地址必须是 `127.0.0.1` 或 `::1`；
- `Host` 必须精确匹配 `127.0.0.1:8080`；
- 带 `Origin` 的请求必须精确匹配 `http://127.0.0.1:8080`；
- 状态变更请求必须通过登录会话与 CSRF 双重校验；
- CSRF Token 绑定会话，由会话状态 API 返回，并通过 `X-CSRF-Token` 请求头提交；
- 设置路由不加入现有面向 Electron `file://` / `null` Origin 的 CORS 放行范围；
- 响应添加 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` 和限制严格的 Content Security Policy；
- 页面不加载外部脚本、字体或统计资源。

即使主服务误绑定到 `0.0.0.0`，设置路由也必须拒绝非回环客户端。反向代理转发头不作为客户端身份依据。

首次密码初始化没有既有会话可用，因此只豁免会话与 CSRF 校验，仍执行回环地址、Host、Origin、请求体限制和初始化状态检查；密码一旦存在，该接口永久拒绝再次初始化。

## 10. API 契约

所有响应使用 JSON；成功的状态变更可以返回 `204 No Content`。错误结构统一为：

```json
{
  "error": {
    "code": "SETTINGS_VALIDATION_FAILED",
    "message": "请检查标记的配置项",
    "fields": {
      "llm.baseUrl": "请输入有效的 HTTP 或 HTTPS 地址"
    }
  }
}
```

主要接口：

| 方法 | 路径 | 会话 | CSRF | 用途 |
|---|---|---:|---:|---|
| `GET` | `/api/settings/session` | 否 | 否 | 返回初始化、登录和会话状态；已登录时返回 CSRF Token |
| `POST` | `/api/settings/setup` | 否 | 否 | 仅首次设置管理密码并建立会话 |
| `POST` | `/api/settings/login` | 否 | 否 | 验证密码并建立会话 |
| `POST` | `/api/settings/logout` | 是 | 是 | 销毁当前会话 |
| `GET` | `/api/settings/config` | 是 | 否 | 返回脱敏配置、字段来源和凭据库状态 |
| `PUT` | `/api/settings/config` | 是 | 是 | 校验并保存完整配置草稿 |
| `GET` | `/api/settings/voices` | 是 | 否 | 返回 `voices.yml` 中可选择的音色摘要 |
| `POST` | `/api/settings/test/llm` | 是 | 是 | 用草稿执行最小 LLM 请求 |
| `POST` | `/api/settings/test/qq` | 是 | 是 | 校验草稿并返回当前 OneBot 运行状态 |
| `POST` | `/api/settings/test/tts` | 是 | 是 | 用草稿探测 GPT-SoVITS 服务状态 |

`GET /api/settings/config` 不返回秘密值，只返回类似以下状态：

```json
{
  "llm": {
    "apiKey": {
      "configured": true,
      "source": "keychain",
      "environmentVariable": null
    }
  }
}
```

保存与测试请求对秘密使用显式操作：

```json
{
  "apiKey": {
    "operation": "retain"
  }
}
```

允许的操作为 `retain`、`replace` 和 `delete`。只有 `replace` 携带 `value`；环境变量接管时只允许 `retain`，避免产生看似保存但永远不生效的秘密。

## 11. 草稿验证与连接测试

### 11.1 通用验证

- URL 只允许 `http` 或 `https`，禁止用户名和密码出现在 URL 中；
- 数值范围沿用现有运行时约束；
- QQ 启用时 Token 长度必须为 16～512 个字符，且群或用户白名单至少有一项；
- `rateBurst` 不得大于 `ratePerMinute`；
- TTS 默认音色必须存在于当前 `voices.yml`；
- 被环境变量接管的字段必须保持服务器返回值，不接受客户端覆盖。

### 11.2 LLM 测试

使用草稿中的 URL、模型和秘密，在内存中构造与现有 OpenAI 兼容适配器一致的最小 `chat/completions` 请求，不启用工具，不写入聊天记录或记忆。测试设置独立的 15 秒上限，并只返回成功、认证失败、超时、不可达或协议不兼容等结果。

### 11.3 QQ 测试

保存前只执行本地字段校验，不主动操纵 NapCat 或建立第二条 OneBot 连接。接口额外读取当前后端已有的 OneBot 连接状态，并明确标记该状态对应「当前运行配置」。保存后必须重启，重启后的状态页才用于确认新配置是否连接成功。

### 11.4 TTS 测试

测试先请求 GPT-SoVITS 的 `/openapi.json`；如果服务不提供该端点，再请求服务根路径。任一端点返回 `2xx` 或 `3xx` 响应即表示服务可达；不提交文本、不生成音频。测试使用 10 秒上限，不把外部响应正文返回给浏览器。

## 12. 错误处理

- 配置文件不存在：使用默认值，并允许首次保存；
- 配置文件格式或版本错误：拒绝保存，提示用户修复或备份现有文件，不自动覆盖；
- 凭据引用缺失：字段显示「凭据缺失」，启用相关功能前必须替换或删除该引用；
- 操作系统凭据库不可用：显示持久状态，秘密变更失败关闭；
- 验证失败：返回字段级错误，不执行任何写入；
- 外部测试失败：保持草稿，不影响已保存配置；
- 保存中断：下次启动按保存日志恢复；
- 会话过期：返回 `401`，前端保留当前非秘密草稿并要求重新登录；秘密输入在会话失效后立即从页面内存清除；
- CSRF、Origin、Host 或客户端地址不合法：返回通用 `403`，不泄露具体安全检查细节。

日志允许记录错误类别、字段路径和事务 ID，不得记录密码、API Key、Access Token、Cookie、CSRF Token 或外部响应正文。

## 13. 数据流

### 13.1 启动

1. FastAPI 创建设置服务；
2. `SettingsFileStore` 恢复未完成的保存事务；
3. `SettingsResolver` 合并默认值、文件、凭据库和环境变量；
4. 解析后的配置用于构造 LLM、OneBot 与 TTS 组件；
5. 设置页面读取同一个解析结果及来源元数据。

### 13.2 保存

1. 浏览器提交完整草稿与秘密操作；
2. 安全依赖校验回环地址、Host、Origin、会话与 CSRF；
3. `SettingsValidationService` 解析并校验草稿；
4. `SettingsFileStore` 与 `KeychainSecretStore` 按保存日志协议提交；
5. API 返回最新脱敏快照和 `restartRequired: true`；
6. 当前聊天运行时保持不变，直到用户重启后端。

### 13.3 测试

1. 浏览器提交当前分区草稿；
2. 服务端把 `retain` 解析为已有凭据，把 `replace` 值只保留在请求内存；
3. 验证服务执行一次有界测试；
4. 临时客户端关闭并丢弃草稿秘密；
5. 浏览器只收到结构化、脱敏的测试结果。

## 14. 测试策略

### 14.1 单元测试

- 每种来源的优先级与字段来源元数据；
- 环境变量布尔值、数值边界和无效值；
- `settings.json` 模式版本、未知字段与默认补全；
- 原子文件替换与失败注入；
- 凭据新增、保留、替换、删除和不可用状态；
- 保存日志在每个事务阶段中断后的恢复；
- `scrypt` 密码验证、常量时间比较、会话过期和登录限速；
- URL、QQ 白名单、速率关系和音色 ID 校验；
- 外部错误分类与日志脱敏。

### 14.2 API 测试

- 首次初始化只能执行一次；
- 未登录、过期会话和伪造 Cookie 被拒绝；
- 非回环地址、错误 Host、错误 Origin 和缺失 CSRF 被拒绝；
- 配置读取从不返回秘密；
- 空秘密输入保留原值，替换和删除必须显式表达；
- 环境变量接管字段不可修改；
- 保存成功返回 `restartRequired: true`，且运行时实例不热更新；
- 3 类连接测试只使用草稿并正确清理临时客户端；
- TTS 音色文件异常和无效默认音色返回字段错误。

### 14.3 前端与 Electron 契约测试

- 登录、分区切换、脏状态提示、保存提示和错误聚焦；
- 秘密字段不被配置响应回填；
- 会话失效时清除秘密输入；
- 环境变量接管标签可见且输入禁用；
- 窄屏布局保持可操作；
- Electron 托盘菜单只打开固定本地 URL，且不通过 IPC 传递配置。

### 14.4 回归验证

- 运行完整后端测试套件；
- 运行完整桌面端测试套件；
- 在无 LLM、无 QQ、无 GPT-SoVITS 的默认环境下启动后端与 Electron；
- 手工验证首次设密、登录、保存、重启生效和退出登录；
- 在 macOS Keychain 与 Windows Credential Manager 各完成至少一次秘密替换和删除验收。

## 15. 迁移与兼容性

- 现有环境变量无需修改，并继续拥有最高优先级；
- 没有 `settings.json` 时，行为应与当前默认配置一致；
- 现有 `config/voices.yml` 继续作为音色定义唯一来源；
- 新增的 `ASSISTANT_TTS_DEFAULT_VOICE_ID` 只覆盖默认音色选择，不修改音色定义；
- 首版不自动迁移环境变量中的秘密到操作系统凭据库，避免未经用户确认复制凭据；
- 配置解析模块替代各组件直接读取环境变量后，必须保留当前校验边界和默认值。

## 16. 交付边界

本规格可以由一个实现计划覆盖。建议实现顺序为：配置模型与解析器、文件和凭据存储、鉴权与安全依赖、API 与连接测试、Web 页面、Electron 入口、端到端验证。各阶段均先添加失败测试，再实现最小行为，最后运行相关回归测试。
