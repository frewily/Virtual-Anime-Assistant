# `main` 合并与资源保留实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 保存旧 Java/QQ/Live2D 资源的可恢复归档，以当前 Python 模块化架构解决 PR 冲突，并在完整验证后合入 `main`。

**架构：** 归档标签固定指向合并前的 `origin/main`，保证旧 QQ、NapCat、Hiyori 和 Cubism 文件可按需恢复。合并提交使用当前分支文件树作为最终结果，同时把旧 `main` 纳入父历史，避免双后端和重复 Live2D 运行时进入当前工作树。

**技术栈：** Git、GitHub CLI、Python `unittest`、Electron、esbuild

---

## 文件与引用

- 创建：`docs/superpowers/plans/2026-07-28-main-merge-resource-preservation.md`，记录可重复执行的合并步骤。
- 引用：`docs/superpowers/specs/2026-07-28-main-merge-resource-preservation-design.md`，定义合并边界。
- 不修改运行代码；合并后的文件树应与合并前的当前功能分支一致。

### 任务 1：固定归档引用

- [ ] **步骤 1：重新获取远端状态**

运行：

```bash
git fetch origin main codex/project-hardening
git status --short
```

预期：工作树除本计划文档外无未提交变更，`origin/main` 是待归档的旧实现提交。

- [ ] **步骤 2：验证归档目标包含关键资源**

运行：

```bash
git ls-tree -r --name-only origin/main -- qq-bot desktop-app/assets backend/src/main/java/com/assistant
```

预期：输出包含 `qq-bot/docker-compose.yml`、Hiyori 模型、Cubism Core、`QQBotService.java` 和 `OneBotWebSocketHandler.java`。

- [ ] **步骤 3：创建带说明的归档标签**

运行：

```bash
git tag -a archive/legacy-java-qq-live2d-2026-07-28 origin/main -m "archive: preserve legacy Java QQ and Live2D resources"
git show --no-patch --decorate archive/legacy-java-qq-live2d-2026-07-28
```

预期：标签解引用到合并前的 `origin/main`。

### 任务 2：建立明确的合并结果

- [ ] **步骤 1：记录合并前文件树**

运行：

```bash
git rev-parse HEAD^{tree}
```

预期：得到当前 Python 模块化分支的树对象 ID，供合并后比对。

- [ ] **步骤 2：创建保留当前架构的合并提交**

运行：

```bash
git merge -s ours --no-ff origin/main -m "merge: 同步 main 并保留模块化架构"
```

预期：创建一个有两个父提交的合并提交，不把旧 Java 后端和未确认授权的 Live2D 文件恢复到工作树。

- [ ] **步骤 3：验证合并历史和文件树**

运行：

```bash
git show --no-patch --format=%P HEAD
git rev-parse HEAD^{tree}
git status --short
```

预期：第一条命令输出两个父提交；树对象 ID 与任务 2 步骤 1 相同；工作树干净。

### 任务 3：完成新鲜验证

- [ ] **步骤 1：检查 Python 语法**

运行：

```bash
python3 -m compileall -q backend
```

预期：退出码为 0。

- [ ] **步骤 2：运行后端完整测试**

运行：

```bash
python3 -m unittest discover -s backend/tests -v
```

预期：全部测试通过，失败数和错误数均为 0。

- [ ] **步骤 3：运行桌面端检查**

运行：

```bash
npm test
npm run build:renderer
```

工作目录：`desktop-app`

预期：两条命令退出码均为 0。

- [ ] **步骤 4：检查提交内容**

运行：

```bash
git diff --check HEAD^1..HEAD
git status --short
```

预期：合并提交相对当前架构父提交没有文件变化，工作树干净。

### 任务 4：推送并完成 GitHub 合并

- [ ] **步骤 1：推送功能分支和归档标签**

运行：

```bash
git push origin codex/project-hardening
git push origin archive/legacy-java-qq-live2d-2026-07-28
```

预期：分支和归档标签均成功推送。

- [ ] **步骤 2：将 PR 转为可审查状态并检查可合并性**

运行：

```bash
gh pr ready 1 --repo frewily/Virtual-Anime-Assistant-Long-term-project-
gh pr view 1 --repo frewily/Virtual-Anime-Assistant-Long-term-project- --json isDraft,mergeable,mergeStateStatus,statusCheckRollup
```

预期：`isDraft` 为 `false`，GitHub 不再报告架构冲突；新检查可能仍在运行。

- [ ] **步骤 3：等待 GitHub CI**

运行：

```bash
gh pr checks 1 --repo frewily/Virtual-Anime-Assistant-Long-term-project- --watch --interval 10
```

预期：backend 与 desktop 检查全部成功。

- [ ] **步骤 4：合并 PR**

运行：

```bash
gh pr merge 1 --repo frewily/Virtual-Anime-Assistant-Long-term-project- --merge
```

预期：PR 状态变为 `MERGED`，不删除功能分支和归档标签。

- [ ] **步骤 5：验证远端最终状态**

运行：

```bash
gh pr view 1 --repo frewily/Virtual-Anime-Assistant-Long-term-project- --json state,mergedAt,mergeCommit,url
git fetch origin main
git merge-base --is-ancestor archive/legacy-java-qq-live2d-2026-07-28 origin/main
```

预期：PR 状态为 `MERGED`，返回合并提交；归档标签是新 `origin/main` 的祖先。

