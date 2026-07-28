# `main` 合并与资源保留设计

## 背景

`codex/project-hardening` 已将旧 Spring Boot 后端替换为 Python/FastAPI 模块化单体，并完成统一消息、会话、SQLite 记忆和大模型适配基础。分支创建后，`main` 又在旧 Java 架构上加入 QQ/OneBot 原型、NapCat Docker 配置、Hiyori Live2D 示例模型、Cubism Core 和整套 Cubism Framework，因此 GitHub 无法自动合并。

本次合并的目标不是把两套后端拼接在一起，而是在不丢失旧资源的前提下确立唯一的长期架构。

## 方案比较

### 方案 A：直接保留两套实现

同时保留 Java 与 Python 后端，并把旧 Live2D 目录全部加入当前工作树。

- 优点：旧 QQ 原型和模型文件可以立即看到。
- 缺点：产生两个后端入口、两套消息路由和重复的 Cubism 依赖；安装包边界与维护责任不明确。
- 结论：不采用。

### 方案 B：完全删除旧提交

只保留当前分支的文件，不额外记录旧 `main` 的资源位置。

- 优点：工作树最干净。
- 缺点：后续迁移 QQ 与 Live2D 时难以定位旧实现和恢复资源。
- 结论：不采用。

### 方案 C：新架构作为工作树，旧资源作为可恢复归档

使用当前 Python 模块化架构解决合并结果，同时让旧 `main` 提交成为合并提交的父历史，并在合并前创建明确的归档标签。旧 QQ、NapCat、Hiyori、Cubism Core 与 Framework 均可从归档提交恢复，但不默认进入当前运行树或安装包。

- 优点：架构唯一、资源不丢失、授权边界清晰、以后可以按组件迁移。
- 缺点：QQ 和正式 Live2D 仍需在新架构下分别实施。
- 结论：采用。

## 合并边界

合并后的工作树遵循以下规则：

1. 保留 Python/FastAPI 后端和现有 Electron 渲染入口。
2. 不恢复旧 Java/Spring Boot 后端，避免双后端并存。
3. 不把旧 QQ 服务直接接入当前运行时；后续按统一渠道契约实现 Python OneBot 适配器。
4. 不把整套 `cubism5/` 和 `cubism-framework/` 复制回当前工作树；当前渲染器继续使用 npm 依赖。
5. Hiyori 模型、动作、Cubism Core、NapCat 配置和旧 QQ 代码由归档标签与合并父历史保留。
6. 在明确 Live2D 示例模型和 Cubism Core 的再分发授权前，不把这些文件加入正式安装包。

## 归档与恢复

在改变 `main` 的最终树之前，为当前远端 `main` 创建带说明的标签：

`archive/legacy-java-qq-live2d-2026-07-28`

需要恢复资源时，可以从该标签按目录读取：

- `backend/src/main/java/com/assistant/service/QQBotService.java`
- `backend/src/main/java/com/assistant/websocket/OneBotWebSocketHandler.java`
- `qq-bot/`
- `desktop-app/assets/models/hiyori/`
- `desktop-app/assets/live2dcubismcore*.min.js`
- `desktop-app/assets/cubism5/`
- `desktop-app/cubism-framework/`

归档标签只用于保存与迁移，不代表其中所有第三方文件都已获准重新分发。

## 验证与完成标准

1. 归档标签准确指向合并前的远端 `main`。
2. 合并提交同时包含旧 `main` 与当前功能分支的历史。
3. 合并后的文件树与已验证的 Python 模块化分支一致。
4. 后端完整测试、桌面端测试与渲染构建全部通过。
5. 功能分支和归档标签均成功推送。
6. PR 检查通过并合入 `main`。

