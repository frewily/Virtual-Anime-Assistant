# 云端 Python 包镜像实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 允许云端 Docker 构建使用可配置的 PyPI 镜像，同时保持本地和 CI 默认行为不变。

**架构：** Dockerfile 接收可选 `PIP_INDEX_URL` 构建参数，仅在参数非空时向 `pip install` 传入 `--index-url`。云端 Compose 从非秘密 `.env` 传递该参数，生产模板使用阿里云镜像。

**技术栈：** Docker、Docker Compose、Python `unittest`、YAML。

---

### 任务 1：增加可配置的云端 PyPI 镜像

**文件：**
- 修改：`backend/Dockerfile`
- 修改：`deploy/cloud/docker-compose.yml`
- 修改：`deploy/cloud/.env.example`
- 测试：`deploy/cloud/tests/test_cloud_contract.py`

- [ ] **步骤 1：编写失败的契约测试**

断言 Dockerfile 声明 `ARG PIP_INDEX_URL`，`pip install` 仅在非空时使用该地址；断言 Compose 的 `vaa-app.build.args.PIP_INDEX_URL` 来自环境变量，并且 `.env.example` 使用 `https://mirrors.aliyun.com/pypi/simple/`。

- [ ] **步骤 2：运行测试验证失败**

运行：`python3 -m unittest deploy.cloud.tests.test_cloud_contract -v`

预期：新增测试失败，指出尚未声明或传递 `PIP_INDEX_URL`。

- [ ] **步骤 3：实现最少配置**

在 Dockerfile 中使用 Shell 参数展开，仅在值非空时生成 `--index-url`；在 Compose 中加入 `build.args`；在 `.env.example` 中加入阿里云镜像地址。

- [ ] **步骤 4：运行验证**

运行：

```bash
python3 -m unittest deploy.cloud.tests.test_cloud_contract -v
docker compose -f deploy/cloud/docker-compose.yml config --quiet
git diff --check
```

预期：全部 exit 0。

- [ ] **步骤 5：提交**

```bash
git add backend/Dockerfile deploy/cloud/docker-compose.yml deploy/cloud/.env.example deploy/cloud/tests/test_cloud_contract.py docs/superpowers/specs/2026-08-13-cloud-pip-mirror-design.md docs/superpowers/plans/2026-08-13-cloud-pip-mirror.md
git commit -m "fix: allow cloud builds to use a PyPI mirror"
```
