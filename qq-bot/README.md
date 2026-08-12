# NapCat QQ 本地开发环境

> 本目录只用于本地开发。云端生产部署请参考 [云端 QQ 助手部署与运维](../docs/deployment/cloud-qq-assistant.md)。

本目录提供可选的 NapCat Docker 配套，用于把 QQ 的 OneBot 11 事件通过反向 WebSocket 发送给本机助手。Docker 只运行 NapCat，不运行 FastAPI、Electron、SQLite 或大模型服务。

## 安全边界

- WebUI 只绑定 `127.0.0.1:6099`，不会监听局域网地址。
- Compose 不暴露 OneBot HTTP 或正向 WebSocket 的 3000、3001 端口。
- QQ 登录数据和 NapCat 配置保存在 `qq-bot/data/`，该目录已被 Git 忽略。
- OneBot Token 只写入后端环境变量和 NapCat WebUI，不要写入 `.env.example`、Compose、日志或 Git。
- Docker 不由 Electron 自动启动。扫码登录、协议确认和 WebUI 配置均由用户手动完成。

## 前置条件

- 已安装 Docker Desktop 或兼容的 Docker Compose 环境。
- 本机后端可以在 `127.0.0.1:8080` 启动。
- 已准备至少 16 个字符的随机 OneBot Token。
- 已确认允许接入的 QQ 用户号或群号。

## 1. 配置并启动后端

先在项目根目录配置 QQ 渠道。私聊白名单与群白名单彼此独立，至少填写其中一项：

```bash
export ASSISTANT_QQ_ENABLED=true
export ASSISTANT_QQ_ACCESS_TOKEN='<至少 16 个字符的随机 Token>'
export ASSISTANT_QQ_ALLOWED_USER_IDS='123456789'
export ASSISTANT_QQ_ALLOWED_GROUP_IDS='987654321'

cd backend
python3 main.py
```

保持后端运行。可以在另一个终端查看安全状态：

```bash
curl http://127.0.0.1:8080/api/qq/status
```

未连接 NapCat 时，正常状态为 `disconnected`。接口只返回启用状态和白名单数量，不返回 Token 或具体 QQ 号。

## 2. 启动 NapCat

在项目根目录运行：

```bash
cd qq-bot
cp .env.example .env
docker compose up -d
docker compose logs -f napcat
```

首次启动后，根据容器日志和 WebUI 提示完成 QQ 扫码登录。WebUI 地址为：

```text
http://127.0.0.1:6099
```

不要把 WebUI 凭据、QQ 密码、Cookie 或登录数据复制到仓库。

## 3. 创建反向 WebSocket 客户端

在 NapCat WebUI 中手动创建 OneBot 11 WebSocket 客户端，使用以下配置：

| 配置项 | 值 |
|---|---|
| URL | `ws://host.docker.internal:8080/ws/qq` |
| Token | 与 `ASSISTANT_QQ_ACCESS_TOKEN` 完全一致 |
| 消息格式 | `array` |
| 上报自身消息 | 关闭 |

macOS 和 Windows 通过 `host.docker.internal` 访问宿主机。Compose 的 `extra_hosts` 同时为支持 `host-gateway` 的 Linux 环境提供映射。

保存并启用客户端后，再次请求状态接口。连接成功时，`state` 应为 `connected`。

## 4. 验收消息行为

- 私聊：只有允许用户可以触发，无需 `@`。
- 群聊：只有允许群可以触发，并且消息必须使用结构化消息段 `@机器人`。
- 群聊回复：第一段引用原消息并 `@发送者`。
- 图片、语音、文件和其他富媒体不会被下载或送入模型。
- 超限、未授权和未 `@` 的消息会被静默忽略。

## 停止与排错

停止容器但保留登录数据：

```bash
docker compose down
```

查看最近日志：

```bash
docker compose logs --tail=200 napcat
```

常见状态：

| 状态 | 含义 | 检查项 |
|---|---|---|
| `disabled` | QQ 渠道未启用 | 检查 `ASSISTANT_QQ_ENABLED` |
| `misconfigured` | 配置不完整或越界 | 检查 Token、白名单和数值范围 |
| `disconnected` | 配置有效但 NapCat 未连接 | 检查 URL、Token、容器网络和客户端开关 |
| `connected` | 反向 WebSocket 已连接 | 发送白名单内的测试消息 |

排错时不要删除 `qq-bot/data/`。删除该目录会移除本地 NapCat 配置和 QQ 登录数据，需要重新配置和登录。
