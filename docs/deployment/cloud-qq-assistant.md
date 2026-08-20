# 云端 QQ 助手部署与运维

本文用于在 Alibaba Cloud Linux 3 单机上部署 VAA 与 NapCat。服务只供个人使用，管理入口仅绑定服务器回环地址并通过 SSH 隧道访问。

## 安全边界

- 不修改宝塔 Nginx，不占用或调整公网 80、443 端口。
- 不修改现有网站、博客、防火墙、root 登录或密码登录配置。
- VAA 设置页、NapCat WebUI 和 OneBot 不对公网开放。
- API Key、OneBot Token、SSH 私钥和 QQ 登录数据不得提交 Git 或复制到聊天记录。
- 本文中的 `<服务器地址>`、`<提交 SHA>` 和公钥路径必须替换为实际值；秘密使用无回显方式由用户输入。

## 1. 只读复核

开始前记录当前资源与端口，但不要读取现有容器环境变量：

```bash
. /etc/os-release && echo "$ID $VERSION_ID"
uname -m
free -h
swapon --show
df -h /
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker compose version
ss -lnt
```

确认系统为 Alibaba Cloud Linux 3、架构为 x86_64，并确认 8080、6099 未被其他服务占用。若端口已占用，停止并调整设计，不覆盖现有服务。

## 2. 创建专用部署用户

以下命令需要管理员权限。创建 `vaa-deploy`，但不改变现有管理员、root 或 SSH 服务配置：

```bash
sudo useradd --create-home --shell /bin/bash vaa-deploy
sudo install -d -m 0700 -o vaa-deploy -g vaa-deploy \
  /home/vaa-deploy/.ssh
sudo install -m 0600 -o vaa-deploy -g vaa-deploy \
  /path/to/reviewed-public-key \
  /home/vaa-deploy/.ssh/authorized_keys
```

将部署用户加入 Docker 组会赋予接近 root 的主机控制能力，必须在执行前再次取得用户确认：

```bash
sudo usermod -aG docker vaa-deploy
```

创建目录：

```bash
sudo install -d -m 0750 -o vaa-deploy -g vaa-deploy \
  /opt/virtual-anime-assistant
sudo install -d -m 0750 -o vaa-deploy -g vaa-deploy \
  /opt/virtual-anime-assistant/data
```

将仓库克隆到 `/opt/virtual-anime-assistant/current`。部署工作副本和持久数据必须分开；版本切换不得删除 `data`。

## 3. 配置环境文件

进入生产编排目录，复制模板：

```bash
cd /opt/virtual-anime-assistant/current/deploy/cloud
cp .env.example .env
cp secrets.env.example secrets.env
chmod 600 secrets.env
```

`.env` 保存模型地址、模型名、QQ 白名单等非秘密配置。`secrets.env` 只保存：

- `ASSISTANT_LLM_API_KEY`
- `ASSISTANT_QQ_ACCESS_TOKEN`
- `ASSISTANT_COMPUTER_STATE_REPORT_TOKEN`

OneBot Token 至少 16 个字符。不要使用会将秘密写入 Shell 历史或终端录屏的方式填入。秘密由环境文件提供，因此设置页将秘密字段显示为已配置且只读；模型地址、模型名、QQ 白名单等非秘密字段仍可保存与测试。

验证文件权限和 Compose 语法时不要输出展开后的配置：

```bash
test "$(stat -c '%a' secrets.env)" = 600
docker compose config --quiet
```

## 4. 首次部署

完成服务器目录和环境文件初始化后，通过 main 分支的 CI 触发首次部署。GitHub
Actions 会上传目标提交及部署脚本；不要直接用单个提交参数运行 `deploy.sh`。

部署脚本串行运行，验证部署包后备份现有 SQLite，并检查 live 与 ready。健康检查失败时只回滚应用代码和镜像，不恢复或删除数据库。

查看状态：

```bash
cd /opt/virtual-anime-assistant/current/deploy/cloud
docker compose ps
```

## 5. SSH 隧道与首次配置

在本地电脑运行：

```bash
ssh -L 8080:127.0.0.1:8080 \
    -L 6099:127.0.0.1:6099 \
    vaa-deploy@<服务器地址>
```

隧道保持连接时访问：

- VAA 设置页：`http://127.0.0.1:8080/settings`
- NapCat WebUI：`http://127.0.0.1:6099`

使用专用 QQ 小号扫码登录。在 NapCat 中创建 OneBot 11 反向 WebSocket 客户端：

| 配置项 | 值 |
| --- | --- |
| URL | `ws://vaa-app:8080/ws/qq` |
| Token | 与 `ASSISTANT_QQ_ACCESS_TOKEN` 一致 |
| 消息格式 | `array` |
| 上报自身消息 | 关闭 |

不要发布 OneBot 3000、3001 端口。

## 6. 启用每日备份

管理员将单元文件安装到 systemd，并启用 Timer：

```bash
sudo install -m 0644 deploy/cloud/systemd/vaa-backup.service \
  /etc/systemd/system/vaa-backup.service
sudo install -m 0644 deploy/cloud/systemd/vaa-backup.timer \
  /etc/systemd/system/vaa-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now vaa-backup.timer
systemctl status vaa-backup.timer
```

手动创建一致性备份：

```bash
deploy/cloud/scripts/backup-sqlite.sh
```

系统每天备份 1 次并保留最近 7 份。

## 7. 启用长期运行监控

长期运行监控每分钟检查 VAA、OneBot 与 SQLite 备份。OneBot 连续 3 次断开后，监控只重启 NapCat；10 分钟内最多恢复 2 次，避免重启风暴。VAA 容器不会获得 Docker Socket 或宿主机管理权限。

管理员使用固定路径安装器授予监控所需的最小目录权限，并启用 systemd 单元：

```bash
sudo /opt/virtual-anime-assistant/current/deploy/cloud/scripts/\
install-cloud-monitor.sh
systemctl status vaa-cloud-monitor.timer
```

安装器不会改变 VAA 数据目录的所有者，也不会递归放宽权限。它只通过
`setfacl` 允许 `vaa-deploy` 穿过数据根目录、读取备份元数据并写入脱敏监控
状态，同时仅允许容器 UID `10001` 读取该状态。脚本可安全重复执行。

手动执行一次检查并读取脱敏摘要：

```bash
sudo systemctl start vaa-cloud-monitor.service
systemctl status vaa-cloud-monitor.service
curl -fsS http://127.0.0.1:8080/api/status/cloud
```

设置页登录后会显示「云端运行状态」卡片。接口与卡片只展示固定状态、时间和计数，不展示 Token、API Key、QQ 号或原始日志。

常见告警：

- `configuration_required`：QQ 白名单或 Token 等运行配置不完整，先检查设置页。
- `backup_stale`：最近 36 小时没有成功备份，检查 `vaa-backup.timer`。
- `recovery_exhausted`：10 分钟内已恢复 2 次，监控停止继续重启；通过 SSH 隧道进入 NapCat WebUI，检查 QQ 是否要求重新扫码。
- `deployment_in_progress`：部署正在运行，监控暂时不执行容器操作。
- `state_invalid`：状态文件缺失、损坏或超过 3 分钟未更新，检查 monitor timer。

需要停用自动恢复时执行：

```bash
sudo systemctl disable --now vaa-cloud-monitor.timer
```

停用监控不会删除 QQ 登录、OneBot 配置、SQLite 或备份数据。监控异常不得通过开放公网端口解决。

## 8. 上线验收

```bash
cd /opt/virtual-anime-assistant/current/deploy/cloud
docker compose ps
deploy/cloud/scripts/verify-deployment.sh full
ss -lnt
systemctl status vaa-backup.timer
systemctl status vaa-cloud-monitor.timer
curl -fsS http://127.0.0.1:8080/api/status/cloud
```

逐项确认：

- VAA 仅监听 `127.0.0.1:8080`，NapCat 仅监听 `127.0.0.1:6099`。
- 公网不能访问 8080、6099、3000 或 3001。
- QQ 可以连续多轮对话并调用已授权工具。
- 分别重启 `vaa-app`、NapCat 和服务器后，服务可以恢复。
- 容器内存与 CPU 上限生效，网站和博客保持正常。
- 备份文件能够通过 SQLite `PRAGMA integrity_check`。
- 云端运行状态更新时间不超过 3 分钟，正常状态不会触发 NapCat 重启。

## 9. 日志与故障排查

只查看必要日志，不输出环境变量：

```bash
docker compose logs --tail=200 vaa-app
docker compose logs --tail=200 napcat
journalctl -u vaa-cloud-monitor.service --since today
```

- `ready` 失败：检查数据目录所有权和磁盘空间。
- OneBot 为 `disconnected`：检查 NapCat 登录状态、反向 WebSocket URL 与 Token 是否一致。
- QQ 登录失效：通过 SSH 隧道进入 NapCat WebUI 重新扫码。
- 模型不可用：通过 VAA 设置页测试非秘密配置；不要在日志中打印 API Key。
- 自动恢复耗尽：确认是否需要重新扫码；不要删除 NapCat 持久化数据或扩大端口暴露。
- `invalid deployment bundle path`：检查 Actions 上传的临时文件名是否为
  `/tmp/vaa-deploy-<提交 SHA>.bundle`。
- `deployment bundle target mismatch` 或 `git bundle verify` 失败：停止使用该部署包，
  检查 Actions 检出的提交和 bundle 生成步骤。这些失败发生在数据库备份与容器切换前，
  不会重建当前容器。

## 10. 回滚与数据库恢复

自动部署失败时，`deploy.sh` 会回滚到上一提交，但不会替换 SQLite。

数据库恢复必须人工执行。先停止 `vaa-app`，保留当前数据库副本，再选择一份已验证备份：

```bash
docker compose stop vaa-app
cp data/vaa/sqlite/assistant.db \
  data/vaa/backups/assistant-before-restore.db
cp data/vaa/backups/<选定备份>.db \
  data/vaa/sqlite/assistant.db
docker compose start vaa-app
deploy/cloud/scripts/verify-deployment.sh startup
```

恢复前核对实际挂载路径和文件所有权。不得使用 `docker compose down -v`，也不得在自动回滚中恢复数据库。

## 11. GitHub Actions 自动部署与密钥

main 分支的 CI 成功后，GitHub Actions 精确检出通过验证的提交，生成并校验完整
Git bundle，再通过 SSH 上传。服务器从
`/tmp/vaa-deploy-<提交 SHA>.bundle` 导入目标提交，服务器无需访问 GitHub。

自动部署保留部署锁、SQLite 备份、健康检查和失败回滚。bundle 路径、固定引用或
目标 SHA 校验失败时，流程会在备份和容器切换前停止。临时 bundle、部署脚本和导入
脚本无论部署成功或失败都会自动删除。

工作流需要 5 个 GitHub Secrets：

- `VAA_DEPLOY_HOST`
- `VAA_DEPLOY_USER`
- `VAA_DEPLOY_PORT`
- `VAA_DEPLOY_SSH_KEY`
- `VAA_DEPLOY_KNOWN_HOSTS`

创建 SSH 私钥和 GitHub Secrets 会形成持久访问，必须在实际操作前再次取得用户确认。known_hosts 必须由管理员从可信路径核对，工作流不会自动使用 `ssh-keyscan` 接受未知主机。

## 12. 电脑状态 relay 运维

电脑状态链路为“Mac 最新脱敏快照 → 单次 SSH stdin → 强制 wrapper → 服务器回环 API”。它不使用 SSH 端口转发，不开放新公网端口，也不会改变 Nginx、防火墙、root 或密码登录设置。云端只保留每台设备最新快照，不保存状态历史；Mac 停止上报后 45 秒内显示离线。QQ 只能查询脱敏状态，不能创建、批准或执行电脑操作。

隐私按公开、半敏感、敏感、高度敏感四级处理；屏幕、按键、剪贴板、文件和聊天正文属于高度敏感数据，永不进入 relay。打开应用、打开 URL、调节音量和媒体控制只能在 Mac 本机逐次确认，relay 不提供任意 Shell。

### 12.1 启用顺序

1. 先在 Mac 设置页开启“本机状态采集”，确认所需 macOS 辅助功能权限；只开启本地采集时不需要 SSH。
2. 准备一对只供状态 relay 使用的 ed25519 密钥和独立 known hosts 文件，不复用部署密钥。实际创建或安装持久 SSH 凭证前必须重新确认。
3. 在服务器用无回显方式准备 32～256 字符的 relay Token 文件；同一 Token 同时用于云端 `ASSISTANT_COMPUTER_STATE_REPORT_TOKEN` 和 wrapper 的专用 `0640` 文件，不复制到 Mac。
4. 管理员核对公钥、设备 ID 和 Token 文件后运行：

```bash
sudo deploy/cloud/scripts/install-state-relay-access.sh \
  /path/to/reviewed-state-relay.pub \
  macbook-main \
  /path/to/state-relay-token
```

安装器创建 `vaa-state-relay` 专用账号，并把公钥固定为 `command="/usr/local/libexec/vaa-state-relay macbook-main",restrict`。该身份不能执行远程命令、TTY 或任何端口转发。不要把私钥、Token 或文件正文复制到终端记录、日志或聊天。

5. 在 Mac 仅配置 `ASSISTANT_COMPUTER_DEVICE_ID`、`ASSISTANT_COMPUTER_RELAY_TARGET`、`ASSISTANT_COMPUTER_RELAY_PORT`、`ASSISTANT_COMPUTER_RELAY_IDENTITY_FILE` 和 `ASSISTANT_COMPUTER_RELAY_KNOWN_HOSTS_FILE`。设置页只显示“已配置”，不返回原值。
6. 最后开启“向云端上报脱敏状态”，保存并重启助手。通过 QQ 询问 CPU、内存、锁屏、电池或媒体状态进行只读验收；不要测试电脑操作。

### 12.2 诊断

- 设置页显示“未完整配置”：逐项核对 5 个 Mac 环境变量是否存在、端口是否有效、两个路径是否为绝对路径；不要输出它们的原值。
- SSH 失败：从可信渠道核对主机密钥、公钥指纹、专用文件权限和 relay 用户状态；不要关闭 `StrictHostKeyChecking`，也不要使用 `ssh-keyscan` 自动接受未知主机。
- wrapper 拒绝：确认设备 ID 与公钥强制命令一致、JSON 小于等于 32 KiB，并确认服务器 Token 文件与容器秘密一致；日志中不要打印请求正文或 Token。
- QQ 显示离线：先等待一个 15 秒上报周期；超过 45 秒仍离线时检查 Mac Reporter 和云端回环健康状态，不要为排障开放 8080、6099、3000 或 3001。

### 12.3 停止与撤销

临时停止上报时，在 Mac 设置页关闭远端上报，保存并重启。云端最新快照会在 45 秒内过期；关闭本机状态采集还会停止本机状态工具。停用不会删除聊天、记忆或 QQ 数据。

永久停用时，先完成上述停止流程，再由管理员编辑 `/var/lib/vaa-state-relay/.ssh/authorized_keys`，只删除与目标设备 ID 完全匹配的 managed 行，以撤销专用公钥；不要覆盖其他设备条目。随后安全移除或归档 Mac 上的专用私钥和独立 known hosts 文件。若不再有任何设备，轮换或停用云端 `ASSISTANT_COMPUTER_STATE_REPORT_TOKEN`，并同步处理 `/etc/virtual-anime-assistant/state-relay-token`。每项删除或密钥轮换都应先核对精确目标并单独确认。
