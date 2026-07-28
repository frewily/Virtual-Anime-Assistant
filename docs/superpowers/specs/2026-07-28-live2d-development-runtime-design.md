# Live2D 开发运行时设计

## 背景

当前 Electron 渲染端已经依赖 `pixi.js` 和 `pixi-live2d-display`，并存在模型加载入口，但当前工作树没有 Cubism Core、模型、纹理或动作文件，因此只能显示资源缺失提示。旧 `main` 中曾包含 Hiyori 示例模型、Cubism Core 和整套 Cubism Framework，这些文件已由标签 `archive/legacy-java-qq-live2d-2026-07-28` 保存。

本阶段只建立一个可验证的开发期 Live2D 运行闭环，不把 Hiyori 或 Cubism Core 默认放入正式 `.exe`、`.dmg`，也不实现完整的模型商店或任意模型导入器。

## 目标

1. 使用归档中的 Hiyori 示例资源验证 Cubism 4 模型在 Electron 中稳定渲染。
2. 支持待机动作、眨眼、物理摆动、鼠标视线跟随和身体点击动作。
3. 让模型资源可以通过明确命令恢复，同时保持工作树和安装包的第三方资源边界。
4. 让资源缺失、加载失败和不支持的动作安全降级，不影响聊天或后端。
5. 为后续替换为自有模型和实现模型选择器建立单一配置入口。

## 非目标

- 不在本阶段把 Hiyori 作为正式产品角色。
- 不把开发样例资源加入正式安装包。
- 不实现模型下载、在线市场、ZIP 导入或任意目录选择。
- 不新增 Hiyori 不具备的表情和动作。
- 不更换 Electron + PixiJS 技术路线。

## 授权边界

Hiyori Momose 属于 Live2D 原创样例角色。开发和 SDK 集成测试必须遵守 Live2D 的样例数据条款，不得修改角色设计，并需要提供规定的版权说明：

> This content uses sample data owned and copyrighted by Live2D Inc.

Cubism SDK 可以用于开发验证；应用正式发布时需要按组织规模和应用类型重新确认 Publication License，尤其要确认允许用户扩展模型的桌面应用是否属于 Expandable Application。

参考：

- <https://www.live2d.com/en/learn/sample/model-terms/>
- <https://www.live2d.com/en/sdk/license/>
- <https://www.live2d.com/en/sdk/download/web/>

本阶段采用以下约束：

1. Hiyori 和 Cubism Core 只恢复到 Git 忽略的开发目录。
2. `electron-builder` 显式排除该开发目录。
3. README 和第三方说明标记资源来源、用途和发布限制。
4. 正式模型与发布许可确认前，构建产物不得包含这些开发资源。

## 方案比较

### 方案 A：直接提交第三方资源

将 Hiyori 和 Cubism Core 复制到 `src/renderer/assets` 并随应用打包。

- 优点：克隆后可以直接运行。
- 缺点：源码分发和安装包默认包含第三方资源，开发与发布边界不清楚。
- 结论：不采用。

### 方案 B：可恢复的本地开发资源

由 Node 脚本从归档标签恢复资源到 Git 忽略目录，开发端按固定路径加载，打包配置显式排除。

- 优点：资源可重复恢复、运行路径稳定、不会误入安装包。
- 缺点：首次运行前需要执行一次准备命令。
- 结论：采用。

### 方案 C：立即实现任意外部模型目录

通过文件选择器、自定义协议和持久化配置加载用户模型。

- 优点：最终扩展性最好。
- 缺点：需要额外处理路径授权、协议安全、资源根目录和安装包生命周期，超出首个运行闭环。
- 结论：作为后续独立阶段，不在本阶段实现。

## 依赖兼容

当前依赖组合是 `pixi-live2d-display@0.4.0` 与 Pixi 7.4.3。前者声明的 Pixi peer dependencies 是 6.x，因此当前组合存在运行时兼容风险。

本阶段使用稳定组合：

- `pixi-live2d-display@0.4.0`
- `pixi.js@6.5.10`

不升级到 `pixi-live2d-display@0.5.0-beta`，避免同时引入 beta 运行时、ESM 调整和交互 API 变化。

## 文件结构

```text
desktop-app/
├── scripts/
│   └── setup-live2d-dev.js
├── src/renderer/
│   ├── assets/
│   │   └── dev-live2d/              # 生成目录，不纳入 Git 和安装包
│   │       ├── live2dcubismcore4.min.js
│   │       └── hiyori/
│   ├── js/
│   │   ├── live2d-config.js         # 路径、动作别名和缩放范围
│   │   └── live2d.js                # 加载、渲染和交互生命周期
│   ├── index.html
│   └── styles/main.css
└── tests/
    ├── live2d-config.test.js
    └── setup-live2d-dev.test.js
```

项目根目录同时更新：

- `.gitignore`：忽略 `desktop-app/src/renderer/assets/dev-live2d/`。
- `README.md`：增加开发资源准备、运行和授权说明。
- `desktop-app/electron-builder.yml`：排除开发资源目录。
- `desktop-app/package.json`：增加准备命令与 Node 测试命令。

## 资源恢复组件

`scripts/setup-live2d-dev.js` 只负责资源恢复，不负责启动 Electron。

固定来源：

`archive/legacy-java-qq-live2d-2026-07-28`

固定恢复范围：

- `desktop-app/assets/live2dcubismcore4.min.js`
- `desktop-app/assets/models/hiyori/Hiyori.model3.json`
- `desktop-app/assets/models/hiyori/Hiyori.moc3`
- `desktop-app/assets/models/hiyori/Hiyori.2048/`
- `desktop-app/assets/models/hiyori/Hiyori.physics3.json`
- `desktop-app/assets/models/hiyori/Hiyori.pose3.json`
- `desktop-app/assets/models/hiyori/Hiyori.userdata3.json`
- `desktop-app/assets/models/hiyori/Hiyori.cdi3.json`
- `desktop-app/assets/models/hiyori/motions/`

脚本行为：

1. 验证当前目录属于 Git 工作树。
2. 验证归档标签存在。
3. 使用参数数组调用 Git，不通过 Shell 拼接路径。
4. 只允许预定义的资源路径，拒绝路径穿越。
5. 写入临时目录并逐项验证后，再替换开发资源目录。
6. 缺少任何必需文件时退出非零，不留下半成品。
7. 重复执行产生相同结果。

## 模型配置

`live2d-config.js` 暴露不可变配置和纯函数：

- Cubism Core 相对路径。
- Hiyori `model3.json` 相对路径。
- 模型动作组：`Idle`、`TapBody`。
- 兼容别名：`idle -> Idle`、`tap_body -> TapBody`。
- 缩放最小值、最大值和默认值。
- 根据容器尺寸和模型原始尺寸计算适配缩放。
- 将未知动作解析为 `null`。

场景中的 `wave`、`shake`、`tilt`、`pinch_in` 不映射成错误动作。调用者收到 `null` 后只记录警告。

## 加载与交互生命周期

渲染器使用四种显式状态：

```text
missing -> loading -> ready
                    -> error
```

### `missing`

当 `window.Live2DCubismCore` 不存在时，状态区域显示：

`缺少 Live2D 开发资源，请运行 npm run setup:live2d-dev`

聊天和 WebSocket 继续初始化。

### `loading`

创建透明 Pixi Application 并异步读取模型。状态区域保持可见，重复初始化返回同一个 Promise，避免创建多个 Canvas 和 Ticker。

### `ready`

1. 模型锚点居中并按容器高度自适应。
2. Pixi ticker 驱动模型更新。
3. `Idle` 组由 motion manager 自动随机播放。
4. 模型文件中的 EyeBlink、Physics 和 Pose 配置生效。
5. 自动交互启用，鼠标位置驱动 Focus。
6. `Body` 命中后播放 `TapBody`。
7. 状态区域隐藏。

### `error`

状态区域显示不包含本机绝对路径的安全错误摘要。控制台保留适合开发排查的错误类型，但不输出模型二进制内容。

## 窗口交互

当前 `body` 整体设置了 `-webkit-app-region: drag`，可能阻止 Canvas 接收指针事件。本阶段改为：

1. `body` 和 Canvas 使用 `no-drag`。
2. 顶部增加独立透明拖动区域。
3. 拖动区域不覆盖状态提示和后续聊天控件。
4. Canvas 接收 pointer move 与 pointer tap。
5. `Ctrl/Command + 滚轮` 调整缩放。
6. `+`、`-` 和 `0` 分别放大、缩小和恢复默认缩放。
7. 窗口尺寸变化时重新计算模型适配缩放。

缩放倍率写入 `localStorage`，非法值回退到默认值。

## 对外动作接口

保留现有全局兼容接口：

- `window.playMotion(name)`
- `window.setExpression(name)`

`playMotion` 先通过配置解析别名，再调用真实动作组。未知动作返回 `false`。

Hiyori 没有 Expression 配置，因此 `setExpression` 在本模型上返回 `false`，不会抛出异常。后续自有模型具有表情文件时可以复用同一入口。

## 测试设计

### 资源恢复测试

使用 Node 临时目录和伪造的 Git 命令执行器验证：

1. 必需资源全部恢复。
2. 缺少归档标签时失败。
3. 缺少单个资源时不替换已有目录。
4. 重复执行结果一致。
5. 目标路径不能逃出指定开发资源根目录。

### 配置单元测试

验证：

1. `idle` 与 `tap_body` 映射到正确动作组。
2. 未知动作返回 `null`。
3. 缩放倍率被限制在允许范围。
4. 无效本地存储值回退到默认值。
5. 容器或模型尺寸无效时使用安全默认缩放。

### 自动构建验证

```bash
npm test
npm run build:renderer
python3 -m unittest discover -s backend/tests -v
```

### Electron 视觉验收

使用真实 Electron 窗口验证：

1. 模型在透明窗口中可见且居中。
2. 空闲时有动作、眨眼和物理摆动。
3. 鼠标移动时视线跟随。
4. 点击身体播放 `TapBody`。
5. 顶部区域可以拖动窗口，模型区域仍可点击。
6. 缩放快捷键和滚轮有效。
7. 删除开发资源后显示准备命令，其他界面不崩溃。

## 提交边界

1. `feat: 增加 Live2D 开发资源恢复工具`
2. `feat: 实现 Live2D 模型运行与交互`
3. `docs: 补充 Live2D 开发与授权说明`

每次提交前运行对应单元测试，最终提交前运行完整后端、桌面端和视觉验收。

## 完成标准

1. 开发资源可以从远端归档标签确定性恢复。
2. 资源不会出现在 Git 跟踪文件或安装包配置中。
3. Hiyori 在 Electron 中展示待机、眨眼、物理、视线和点击动作。
4. 缺失资源和未知动作都能安全降级。
5. 自动测试、renderer 构建和 Electron 视觉验收全部通过。
6. 文档明确说明第三方资源来源、版权和正式发布前的许可检查。

