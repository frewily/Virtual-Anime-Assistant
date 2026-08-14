# 云端 Git Bundle 自动部署设计

## 1. 背景

当前 `Deploy Cloud` 工作流通过 SSH 调用服务器仓库中的 `deploy.sh`，再由
服务器执行 `git fetch origin <sha>`。服务器到 GitHub 的 443 连接多次超时，
导致已经通过 CI 的提交无法自动上线；部署脚本虽然能回滚，但仍需人工上传
Git bundle 收尾。

本阶段改为由 GitHub Actions 生成完整 Git bundle，并通过现有严格 SSH 通道
上传。服务器只消费已上传的提交对象，不再主动访问 GitHub。

## 2. 目标与边界

### 2.1 目标

- 只部署已经通过 `main` 分支 CI 的精确提交。
- 自动部署不依赖服务器访问 GitHub 或其他新增仓库。
- 保留部署锁、SQLite 备份、Docker Compose 构建、健康检查和失败回滚。
- 使用现有 SSH 密钥、known hosts 和回环端口边界。
- 上传文件在成功或失败后都从本机 Runner 与服务器 `/tmp` 清理。

### 2.2 不在本阶段实现

- 不引入 Docker Registry、对象存储、发布服务器或新增凭据。
- 不改变 QQ、NapCat、SQLite、监控 timer、网站、博客或防火墙。
- 不开放 8080、6099、3000、3001。
- 不删除服务器仓库的 Git 历史，也不改变数据库恢复流程。

## 3. 方案比较

### 3.1 采用：Git bundle 直传

Actions 检出 `workflow_run.head_sha`，建立固定临时引用并生成完整 bundle；随后
上传 bundle 与目标提交中的部署脚本。服务器验证 bundle 后导入目标提交并部署。
该方案沿用现有 Git 提交切换与回滚机制，且仓库当前完整 bundle 体积较小。

### 3.2 不采用：tar.gz 发布包

源码压缩包更轻，但不包含 Git 对象，必须新增版本目录、原子软链接切换和另一套
回滚实现，会与现有部署机制形成重复状态。

### 3.3 不采用：预构建 Docker 镜像

镜像部署适合后续规模化，但仍要求服务器稳定访问镜像仓库，并新增 Registry
凭据、镜像签名和保留策略，不能直接解决当前网络边界问题。

## 4. 架构与数据流

1. `CI` 在 `main` 上成功完成。
2. `Deploy Cloud` 检出 `workflow_run.head_sha`，并确认本地 `HEAD` 与目标 SHA
   完全一致。
3. 工作流创建临时引用 `refs/heads/vaa-deploy-target`，生成完整 Git bundle，
   再运行 `git bundle verify`。
4. 工作流通过 `scp` 上传 bundle 和该提交中的 `deploy.sh` 到服务器固定临时路径。
5. 工作流通过 SSH 执行临时 `deploy.sh <sha> <bundle>`。
6. 脚本验证参数、文件位置、bundle 完整性和 bundle 内目标引用，导入对象后确认
   `FETCH_HEAD` 等于目标 SHA。
7. 脚本在部署锁内备份 SQLite，切换到目标提交，运行 Docker Compose 和启动健康
   检查；失败时切回原提交并恢复容器。
8. 工作流的退出陷阱清理 Runner 与服务器上的固定临时文件。

临时脚本必须来自目标提交本身，解决首次启用 bundle 部署时服务器仍运行旧版
`deploy.sh` 的引导问题。脚本内部使用固定生产仓库路径，不根据临时脚本所在的
`/tmp` 推导 Compose 或辅助脚本路径。

## 5. Bundle 与参数契约

- 目标提交必须是 40 位小写十六进制 SHA。
- 服务器 bundle 路径固定为
  `/tmp/vaa-deploy-<sha>.bundle`，拒绝其他目录、扩展名或不匹配的 SHA。
- bundle 内只接受固定引用 `refs/heads/vaa-deploy-target`。
- `git bundle verify` 必须成功。
- 导入后 `git rev-parse FETCH_HEAD` 必须与目标 SHA 完全相同。
- 部署脚本不执行 `git fetch origin`，也不读取 `.env`、`secrets.env` 或环境变量
  列表。

完整 bundle 包含回滚所需历史。SSH 已提供传输完整性与主机身份校验，Git bundle
验证再负责对象结构与引用完整性，不新增独立校验密钥。

## 6. 失败处理与清理

- bundle 生成、校验或上传失败：不连接部署脚本，不改变服务器版本。
- bundle 参数或目标引用不匹配：部署脚本在备份和切换前退出。
- 部署锁被占用：退出码保持为现有约定，不并行修改服务。
- 备份、构建或健康检查失败：沿用现有回滚函数恢复上一提交。
- 清理失败：工作流报告清理问题，但不得覆盖原始部署退出码。
- 任何路径都不删除 SQLite、NapCat 登录数据或 Docker 数据卷。

## 7. 安全边界

- 继续使用 `BatchMode=yes`、`IdentitiesOnly=yes` 和
  `StrictHostKeyChecking=yes`。
- 不使用 `ssh-keyscan`，不关闭 known hosts 校验。
- 工作流权限保持 `contents: read`。
- 上传路径与远程命令不接受用户输入；SHA 只来自成功 CI 的
  `workflow_run.head_sha`。
- 临时文件使用仅当前用户可读写的权限。
- 日志不得输出 SSH 私钥、Token、环境文件、QQ 标识或 API Key。

## 8. 测试与验收

### 8.1 自动测试

- 部署契约锁定精确 SHA checkout、完整历史、bundle 生成与校验、`scp` 上传、
  严格 SSH 和清理陷阱。
- 部署脚本契约锁定 bundle 路径、固定引用、`FETCH_HEAD` 校验，并禁止
  `git fetch origin`。
- 使用临时 Git 仓库生成 bundle，验证正确目标可导入，错误 SHA、错误引用、损坏
  bundle 和非法路径均在切换前失败。
- 云端完整测试、Shell 语法、Compose 校验和 `git diff --check` 全部通过。

### 8.2 云端验收

- 合并后 `Deploy Cloud` 自动成功，服务器 HEAD 等于合并提交。
- `verify-deployment.sh full` 通过。
- 云端状态为 `healthy`，OneBot 为 `connected`，备份为 `fresh`。
- 监控与备份 timer 仍为 enabled、active。
- 8080 与 6099 只绑定 `127.0.0.1`，3000 与 3001 未监听。
- 部署不要求 QQ 重新扫码，网站和博客配置未被修改。

## 9. 回滚

如果新工作流存在问题，可将工作流恢复为上一版本；服务器仍保留完整 Git 仓库和
原有回滚能力。已经部署的应用、SQLite、QQ 登录数据、备份与 systemd timer 不因
工作流回滚而改变。
