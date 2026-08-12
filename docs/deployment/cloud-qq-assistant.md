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

OneBot Token 至少 16 个字符。不要使用会将秘密写入 Shell 历史或终端录屏的方式填入。秘密由环境文件提供，因此设置页将秘密字段显示为已配置且只读；模型地址、模型名、QQ 白名单等非秘密字段仍可保存与测试。

验证文件权限和 Compose 语法时不要输出展开后的配置：

```bash
test "$(stat -c '%a' secrets.env)" = 600
docker compose config --quiet
```

## 4. 首次部署

在部署用户会话中执行目标提交：

```bash
cd /opt/virtual-anime-assistant/current
deploy/cloud/scripts/deploy.sh <提交 SHA>
```

部署脚本串行运行，启动前备份现有 SQLite，并检查 live 与 ready。健康检查失败时只回滚应用代码和镜像，不恢复或删除数据库。

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

## 7. 上线验收

```bash
cd /opt/virtual-anime-assistant/current/deploy/cloud
docker compose ps
deploy/cloud/scripts/verify-deployment.sh full
ss -lnt
systemctl status vaa-backup.timer
```

逐项确认：

- VAA 仅监听 `127.0.0.1:8080`，NapCat 仅监听 `127.0.0.1:6099`。
- 公网不能访问 8080、6099、3000 或 3001。
- QQ 可以连续多轮对话并调用已授权工具。
- 分别重启 `vaa-app`、NapCat 和服务器后，服务可以恢复。
- 容器内存与 CPU 上限生效，网站和博客保持正常。
- 备份文件能够通过 SQLite `PRAGMA integrity_check`。

## 8. 日志与故障排查

只查看必要日志，不输出环境变量：

```bash
docker compose logs --tail=200 vaa-app
docker compose logs --tail=200 napcat
```

- `ready` 失败：检查数据目录所有权和磁盘空间。
- OneBot 为 `disconnected`：检查 NapCat 登录状态、反向 WebSocket URL 与 Token 是否一致。
- QQ 登录失效：通过 SSH 隧道进入 NapCat WebUI 重新扫码。
- 模型不可用：通过 VAA 设置页测试非秘密配置；不要在日志中打印 API Key。

## 9. 回滚与数据库恢复

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

## 10. GitHub Actions 部署密钥

工作流需要 5 个 GitHub Secrets：

- `VAA_DEPLOY_HOST`
- `VAA_DEPLOY_USER`
- `VAA_DEPLOY_PORT`
- `VAA_DEPLOY_SSH_KEY`
- `VAA_DEPLOY_KNOWN_HOSTS`

创建 SSH 私钥和 GitHub Secrets 会形成持久访问，必须在实际操作前再次取得用户确认。known_hosts 必须由管理员从可信路径核对，工作流不会自动使用 `ssh-keyscan` 接受未知主机。
