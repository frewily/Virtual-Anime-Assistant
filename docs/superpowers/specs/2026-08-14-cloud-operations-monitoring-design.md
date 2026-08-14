# 云端运行监控与自动恢复设计

## 背景

VAA 与 NapCat 已部署到 Alibaba Cloud Linux 3，并通过 Docker Compose 长期运行。现有健康接口能够判断 VAA、SQLite 与 OneBot 状态，备份任务也由 systemd timer 定期执行，但系统仍缺少以下能力：

- OneBot 持续断开时不会自动恢复。
- 管理员无法在设置页集中查看服务、QQ、备份与恢复状态。
- 恢复失败后只能人工检查多个 systemd 单元和容器日志。

本阶段为单机云端部署增加轻量监控、受控自动恢复和只读运维状态面板。方案不得扩大公网攻击面，不得把 Docker Socket 挂入应用容器，也不得展示 Token、QQ 号或原始日志。

## 目标

- 每分钟检查 VAA 存活、就绪、OneBot 连接与最近备份状态。
- OneBot 连续 3 次断开后自动重启 NapCat。
- 10 分钟内最多自动重启 2 次，避免重启风暴。
- 自动恢复仍失败时进入告警状态，不再持续重启。
- 在 Web 设置页展示云端运行、QQ、备份和自动恢复摘要。
- 服务器重启后自动恢复监控，不依赖管理员保持 SSH 会话。
- 所有运维输出均为固定枚举或计数，不包含秘密和原始日志。

## 非目标

- 不引入邮件、短信或第三方告警平台。
- 不开放新的公网端口，不修改宝塔、Nginx 或现有网站。
- 不让 VAA 直接控制 Docker，也不在容器中挂载 Docker Socket。
- 不自动处理 QQ 扫码登录。需要扫码时只展示明确告警。
- 不做通用容器编排、跨服务器监控或历史指标图表。
- 不替代现有 `verify-deployment.sh`、备份脚本和 Docker 重启策略。

## 方案比较

### 方案 A：主机级 systemd 守护（采用）

主机脚本调用现有健康接口，仅在达到阈值时执行 `docker compose restart napcat`。脚本将固定格式的状态写入 VAA 数据卷，VAA 只读并展示。

优点是权限边界清晰、资源占用低，并能复用现有 systemd 与 Docker Compose 运维方式。缺点是需要维护一个主机脚本和两个 systemd 单元。

### 方案 B：VAA 内置守护

VAA 后端自行检测并重启 NapCat。该方案代码集中，但必须授予应用 Docker 控制权限。一旦后端或依赖被利用，攻击者可控制宿主机容器，风险不可接受。

### 方案 C：独立监控容器

新增监控容器并挂载 Docker Socket。它能隔离监控代码，但仍暴露高权限 Socket，同时增加 2 核 2 GB 服务器的长期资源占用和部署复杂度。

## 架构

系统新增 4 个边界清晰的组件：

1. **主机监控脚本：** 执行一次检查，更新状态机，必要时重启 NapCat。
2. **systemd service 与 timer：** 每分钟启动一次监控脚本，保证开机恢复和单次执行隔离。
3. **运维状态读取模块与 API：** VAA 从固定路径读取、验证并脱敏返回状态。
4. **设置页状态卡片：** 登录后轮询只读 API，展示摘要与处理建议。

数据流如下：

```text
systemd timer
  -> cloud-monitor.sh
     -> VAA health API
     -> backup directory metadata
     -> docker compose restart napcat（仅达到阈值时）
     -> /data/operations/cloud-monitor-state.json
        -> VAA operations reader
           -> GET /api/status/cloud
              -> Web 设置页
```

## 主机监控状态机

### 检查顺序

每次执行按以下顺序检查：

1. `GET /api/health/live` 必须返回 `{"status":"ok"}`。
2. `GET /api/health/ready` 必须返回 `{"status":"ready"}`。
3. `GET /api/health/onebot` 读取 OneBot 状态。
4. 查找 `/data/backups` 中最新的 SQLite 备份文件，并读取修改时间。

脚本使用现有 `verify-deployment.sh` 的 Python 标准库请求模式，不依赖 curl、jq 或新软件包。

### OneBot 恢复规则

- `connected`：清零连续失败次数，保留恢复历史计数，状态为 `healthy`。
- `disabled`：不重启，状态为 `disabled`。
- `misconfigured`：不重启，立即记录 `configuration_required` 告警。
- `disconnected`：连续失败次数加 1。
- VAA 不可用：不重启 NapCat，记录 `vaa_unavailable` 告警。

当 `disconnected` 连续出现 3 次时，脚本检查最近 10 分钟的恢复记录：

- 少于 2 次：执行 `docker compose restart napcat`，记录一次恢复动作，并将连续失败次数归零。
- 已达到 2 次：不再重启，记录 `recovery_exhausted` 告警。

监控脚本不重启 `vaa-app`。VAA 已由 Docker 的 `restart: unless-stopped` 和部署健康检查负责恢复；监控脚本若同时控制 VAA，容易与部署回滚发生竞争。

### 并发与原子性

- 使用独立锁文件和 `flock -n`，同一时间只允许一个监控进程运行。
- 读取旧状态失败时使用安全初始状态，不根据损坏内容执行恢复。
- 状态写入临时文件，执行 `fsync` 后原子替换。
- 自动恢复前再次请求 OneBot 健康接口，避免状态已恢复却仍重启。
- 部署锁存在且已被占用时，只记录 `deployment_in_progress`，不执行容器操作。

## 状态文件契约

状态文件位于容器内 `/data/operations/cloud-monitor-state.json`，宿主机对应 Compose 数据目录。文件只包含以下字段：

```json
{
  "schemaVersion": 1,
  "checkedAt": "2026-08-14T12:00:00Z",
  "overallState": "healthy",
  "vaaState": "ready",
  "onebotState": "connected",
  "backupState": "fresh",
  "latestBackupAt": "2026-08-14T03:00:00Z",
  "consecutiveOnebotFailures": 0,
  "recoveriesInWindow": 0,
  "lastRecoveryAt": null,
  "alertCode": null
}
```

允许的状态值必须使用封闭枚举：

- `overallState`：`healthy`、`degraded`、`alerting`、`unknown`。
- `vaaState`：`ready`、`not_ready`、`unavailable`、`unknown`。
- `onebotState`：`connected`、`disconnected`、`disabled`、`misconfigured`、`unknown`。
- `backupState`：`fresh`、`stale`、`missing`、`unknown`。
- `alertCode`：`null`、`vaa_unavailable`、`configuration_required`、`backup_stale`、`recovery_exhausted`、`deployment_in_progress`、`state_invalid`。

超过 36 小时没有成功备份视为 `stale`。状态文件本身超过 3 分钟未更新时，API 将整体状态降为 `unknown`，防止页面把停止运行的监控误报为健康。

状态文件禁止包含 Token、API Key、QQ 号、容器日志、命令输出、文件绝对宿主机路径或异常原文。

## VAA 运维状态 API

新增只读接口：

```text
GET /api/status/cloud
```

接口行为：

- 仅在 `ASSISTANT_RUNTIME_PROFILE=cloud` 时读取状态文件。
- 桌面环境返回固定的 `{"available":false}`，不访问云端路径。
- 云端状态正常时返回 `available=true` 与经过模型验证的字段。
- 文件缺失、损坏、字段越界或版本未知时返回安全的 `unknown` 状态，不回显解析异常。
- 时间字段统一为 UTC ISO 8601 字符串。
- 接口不返回原始状态文件、内部路径或恢复命令。

该接口本身不执行检查、重启或其他副作用。读取模块与 FastAPI 路由分离，便于独立测试状态校验与陈旧判定。

## Web 设置页

登录后的设置页增加「云端运行状态」卡片。卡片不新增可编辑字段，只展示：

- VAA：正常、未就绪、不可用或未知。
- QQ / OneBot：已连接、已断开、未启用、配置错误或未知。
- SQLite 备份：正常、过期、缺失或未知，以及最近成功时间。
- 自动恢复：最近恢复时间、当前窗口恢复次数。
- 告警：固定中文说明和下一步建议。

页面进入已认证状态后立即请求一次，之后每 30 秒刷新。标签页隐藏时停止轮询，重新可见时立即刷新。退出登录、会话失效或请求取消时清理定时器。

页面只使用服务端固定枚举映射中文文案，不渲染服务器返回的任意 HTML。状态接口不可用时显示「暂时无法读取云端状态」，不得影响模型、QQ、TTS 配置的读取与保存。

## systemd 与部署

新增：

- `vaa-cloud-monitor.service`：`Type=oneshot`，以 `vaa-deploy` 用户运行。
- `vaa-cloud-monitor.timer`：开机后 2 分钟启动，此后每分钟运行一次，启用 `Persistent=true` 和适度随机延迟。

安装流程沿用备份 timer 的方式。部署脚本更新代码后不直接重启 timer；systemd 单元由首次安装步骤复制并启用。运维文档必须包含安装、状态查看、手动执行和停用命令。

监控脚本固定从 `/opt/virtual-anime-assistant/current` 解析 Compose 配置，且只允许重启服务名 `napcat`。不得接受调用方传入任意 Compose 文件、服务名或 Shell 命令。

## 错误处理与安全

- 网络超时、JSON 无效和文件系统异常都转换为固定状态，不记录响应正文。
- Docker 重启失败仅记录 `recovery_exhausted` 和非零结果，不写入 stderr 原文到状态文件。
- systemd 日志仅包含检查阶段、固定状态码和动作结果，不输出 URL 查询参数、HTTP 响应正文或环境变量。
- 状态文件权限为 `0640`，目录权限不高于 `0750`。
- API 使用严格模型并拒绝额外字段，防止状态文件被加入意外敏感字段后继续向页面传播。
- 监控脚本不读取 `secrets.env`，也不执行 `docker compose config`，避免秘密进入日志。

## 测试与验收

### 自动化测试

- 状态读取模块：有效、缺失、损坏、未知版本、额外字段、陈旧时间与枚举越界。
- API：桌面与云端配置、正常与异常状态、响应不含秘密字段。
- 前端契约：卡片存在、30 秒轮询、页面隐藏暂停、会话失效清理、固定文案映射。
- 监控脚本：连续失败阈值、恢复窗口、恢复上限、恢复前复查、部署锁和原子状态写入。
- systemd 与 Compose 契约：用户、路径、频率、权限边界和服务名均符合设计。

Shell 逻辑通过可替换的命令路径和临时目录测试，不调用真实 Docker，不访问真实云端服务。

### 云端验收

1. 安装并启用监控 timer，确认每分钟生成有效状态文件。
2. 正常状态下连续运行 3 次，不发生容器重启。
3. 暂停 NapCat 或使用测试健康端点模拟连续断开，确认第 3 次触发一次恢复。
4. 模拟 10 分钟内恢复 2 次后再次失败，确认进入 `recovery_exhausted` 且不继续重启。
5. 手动恢复 OneBot，确认下次检查回到 `healthy`。
6. 确认设置页能显示 VAA、QQ、备份、恢复与告警摘要。
7. 重启服务器后确认备份 timer、监控 timer、VAA 和 NapCat 均自动恢复。
8. 运行 `verify-deployment.sh full`，并确认公网仍无法访问 8080、6099、3000 或 3001。
9. 检查状态文件、API 响应和 systemd 日志，确认不含 Token、API Key、QQ 号或原始日志。

## 回滚

- 停用并删除 `vaa-cloud-monitor.timer` 与 `vaa-cloud-monitor.service`。
- 删除监控状态文件不会影响 VAA、NapCat、SQLite 或备份数据。
- 回滚应用版本后，旧版 VAA 会忽略新增状态文件。
- 自动恢复只重启 NapCat，不修改 QQ 登录数据和 OneBot 配置，因此停用监控后无需恢复业务配置。

## 完成标准

- 自动恢复严格遵循 3 次失败阈值和 10 分钟 2 次上限。
- 设置页能在不泄露秘密的前提下展示完整云端状态。
- 监控停止、状态损坏和备份过期均不会显示为健康。
- VAA 容器不获得 Docker Socket 或宿主机管理权限。
- 自动化测试、完整部署验收和敏感信息检查全部通过。
