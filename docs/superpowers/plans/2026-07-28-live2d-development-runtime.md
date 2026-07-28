# Live2D 开发运行时实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 从归档标签确定性恢复开发期 Hiyori 资源，并在 Electron 中实现稳定的待机、眨眼、物理、视线跟随、点击动作和缩放交互。

**架构：** Node 资源恢复模块将归档文件原子写入 Git 忽略的开发目录，renderer 通过独立纯配置模块解析资源、动作和缩放规则。`pixi-live2d-display@0.4.0` 与 Pixi 6 稳定组合负责 Cubism 4 渲染，开发资源由 builder 显式排除。

**技术栈：** Electron、Node.js、Node Test Runner、PixiJS 6、pixi-live2d-display 0.4、Live2D Cubism 4、esbuild

---

## 文件结构

- 创建：`desktop-app/scripts/live2d-dev-assets.js`，实现可测试的资源恢复逻辑。
- 创建：`desktop-app/scripts/setup-live2d-dev.js`，提供命令行入口。
- 创建：`desktop-app/tests/live2d-dev-assets.test.js`，验证恢复成功、失败原子性和路径安全。
- 创建：`desktop-app/src/renderer/js/live2d-config.js`，提供模型配置、动作映射和缩放纯函数。
- 创建：`desktop-app/tests/live2d-config.test.js`，验证动作和缩放规则。
- 修改：`desktop-app/src/renderer/js/live2d.js`，实现加载生命周期、交互和容错。
- 修改：`desktop-app/src/renderer/index.html`，加载开发期 Cubism Core 并增加拖动区域。
- 修改：`desktop-app/src/renderer/styles/main.css`，分离拖动区和 Canvas 交互区。
- 修改：`desktop-app/package.json` 与 `desktop-app/package-lock.json`，固定 Pixi 6 并增加准备、测试脚本。
- 修改：`.gitignore`，忽略生成的开发资源。
- 修改：`desktop-app/electron-builder.yml`，排除开发资源。
- 创建：`desktop-app/THIRD_PARTY_DEV_ASSETS.md`，记录样例资源版权与限制。
- 修改：`README.md`，增加准备、运行和故障排查说明。

### 任务 1：开发资源恢复工具

**文件：**

- 创建：`desktop-app/scripts/live2d-dev-assets.js`
- 创建：`desktop-app/scripts/setup-live2d-dev.js`
- 创建：`desktop-app/tests/live2d-dev-assets.test.js`
- 修改：`desktop-app/package.json`
- 修改：`.gitignore`
- 修改：`desktop-app/electron-builder.yml`

- [ ] **步骤 1：编写失败的资源恢复测试**

测试必须构造内存资源读取器，不依赖真实 Git 标签：

```js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
    REQUIRED_RESOURCE_PATHS,
    restoreLive2DAssets
} = require('../scripts/live2d-dev-assets');

function sourceFiles() {
    return new Map(
        REQUIRED_RESOURCE_PATHS.map((sourcePath) => [
            sourcePath,
            Buffer.from(`fixture:${sourcePath}`)
        ])
    );
}

test('restores every required resource into the development layout', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-assets-'));
    const targetDir = path.join(root, 'dev-live2d');
    const files = sourceFiles();

    restoreLive2DAssets({
        targetDir,
        readSourceFile: (sourcePath) => files.get(sourcePath)
    });

    assert.equal(
        fs.readFileSync(path.join(targetDir, 'live2dcubismcore4.min.js'), 'utf8'),
        'fixture:desktop-app/assets/live2dcubismcore4.min.js'
    );
    assert.ok(fs.existsSync(path.join(targetDir, 'hiyori', 'Hiyori.model3.json')));
    assert.ok(fs.existsSync(path.join(targetDir, 'hiyori', 'motions', 'Hiyori_m10.motion3.json')));
});

test('does not replace an existing target when one source is missing', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-assets-'));
    const targetDir = path.join(root, 'dev-live2d');
    fs.mkdirSync(targetDir, { recursive: true });
    fs.writeFileSync(path.join(targetDir, 'sentinel.txt'), 'keep');
    const files = sourceFiles();
    files.delete(REQUIRED_RESOURCE_PATHS.at(-1));

    assert.throws(
        () => restoreLive2DAssets({
            targetDir,
            readSourceFile: (sourcePath) => files.get(sourcePath)
        }),
        /Missing archived Live2D resource/
    );
    assert.equal(fs.readFileSync(path.join(targetDir, 'sentinel.txt'), 'utf8'), 'keep');
});

test('rejects a target outside an explicitly supplied renderer assets root', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-assets-'));
    const assetsRoot = path.join(root, 'assets');

    assert.throws(
        () => restoreLive2DAssets({
            assetsRoot,
            targetDir: path.join(root, '..', 'escaped'),
            readSourceFile: () => Buffer.from('x')
        }),
        /Target directory must stay inside renderer assets/
    );
});
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```bash
cd desktop-app
node --test tests/live2d-dev-assets.test.js
```

预期：FAIL，错误为 `Cannot find module '../scripts/live2d-dev-assets'`。

- [ ] **步骤 3：实现原子资源恢复模块**

`live2d-dev-assets.js` 必须导出固定清单与恢复函数：

```js
const fs = require('node:fs');
const path = require('node:path');

const ARCHIVE_TAG = 'archive/legacy-java-qq-live2d-2026-07-28';
const SOURCE_ROOT = 'desktop-app/assets/';
const REQUIRED_RESOURCE_PATHS = Object.freeze([
    `${SOURCE_ROOT}live2dcubismcore4.min.js`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.model3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.moc3`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.2048/texture_00.png`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.2048/texture_01.png`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.physics3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.pose3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.userdata3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.cdi3.json`,
    ...Array.from(
        { length: 10 },
        (_, index) => `${SOURCE_ROOT}models/hiyori/motions/Hiyori_m${String(index + 1).padStart(2, '0')}.motion3.json`
    )
]);

function assertInside(root, target) {
    const relative = path.relative(path.resolve(root), path.resolve(target));
    if (relative.startsWith('..') || path.isAbsolute(relative) || relative === '') {
        throw new Error('Target directory must stay inside renderer assets');
    }
}

function destinationFor(sourcePath) {
    if (sourcePath.endsWith('live2dcubismcore4.min.js')) {
        return 'live2dcubismcore4.min.js';
    }
    const modelPrefix = `${SOURCE_ROOT}models/hiyori/`;
    if (!sourcePath.startsWith(modelPrefix)) {
        throw new Error(`Unsupported archived Live2D resource: ${sourcePath}`);
    }
    return path.join('hiyori', sourcePath.slice(modelPrefix.length));
}

function restoreLive2DAssets({ targetDir, assetsRoot = path.dirname(targetDir), readSourceFile }) {
    assertInside(assetsRoot, targetDir);
    const temporaryDir = `${targetDir}.tmp-${process.pid}-${Date.now()}`;
    const backupDir = `${targetDir}.backup-${process.pid}`;

    try {
        for (const sourcePath of REQUIRED_RESOURCE_PATHS) {
            const content = readSourceFile(sourcePath);
            if (!Buffer.isBuffer(content)) {
                throw new Error(`Missing archived Live2D resource: ${sourcePath}`);
            }
            const outputPath = path.join(temporaryDir, destinationFor(sourcePath));
            assertInside(temporaryDir, outputPath);
            fs.mkdirSync(path.dirname(outputPath), { recursive: true });
            fs.writeFileSync(outputPath, content);
        }

        if (fs.existsSync(targetDir)) {
            fs.renameSync(targetDir, backupDir);
        }
        fs.renameSync(temporaryDir, targetDir);
        fs.rmSync(backupDir, { recursive: true, force: true });
    } catch (error) {
        fs.rmSync(temporaryDir, { recursive: true, force: true });
        if (!fs.existsSync(targetDir) && fs.existsSync(backupDir)) {
            fs.renameSync(backupDir, targetDir);
        }
        throw error;
    }
}

module.exports = {
    ARCHIVE_TAG,
    REQUIRED_RESOURCE_PATHS,
    restoreLive2DAssets
};
```

- [ ] **步骤 4：实现安全的命令行入口**

`setup-live2d-dev.js` 使用 `execFileSync` 参数数组读取归档，不使用 Shell：

```js
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const {
    ARCHIVE_TAG,
    restoreLive2DAssets
} = require('./live2d-dev-assets');

const projectRoot = path.resolve(__dirname, '..', '..');
const assetsRoot = path.join(projectRoot, 'desktop-app', 'src', 'renderer', 'assets');
const targetDir = path.join(assetsRoot, 'dev-live2d');

execFileSync('git', ['rev-parse', '--verify', `${ARCHIVE_TAG}^{commit}`], {
    cwd: projectRoot,
    stdio: 'ignore'
});

restoreLive2DAssets({
    assetsRoot,
    targetDir,
    readSourceFile: (sourcePath) => execFileSync(
        'git',
        ['show', `${ARCHIVE_TAG}:${sourcePath}`],
        { cwd: projectRoot, encoding: 'buffer', maxBuffer: 16 * 1024 * 1024 }
    )
});

console.log(`Live2D development assets restored to ${targetDir}`);
```

- [ ] **步骤 5：增加脚本、忽略与打包排除规则**

`package.json` 增加：

```json
{
  "scripts": {
    "setup:live2d-dev": "node scripts/setup-live2d-dev.js",
    "test:unit": "node --test tests/*.test.js"
  }
}
```

根 `.gitignore` 增加：

```gitignore
desktop-app/src/renderer/assets/dev-live2d/
```

`electron-builder.yml` 的 `files` 增加：

```yaml
  - "!src/renderer/assets/dev-live2d/**/*"
```

- [ ] **步骤 6：运行测试和真实恢复**

运行：

```bash
cd desktop-app
npm run test:unit
npm run setup:live2d-dev
test -f src/renderer/assets/dev-live2d/live2dcubismcore4.min.js
test -f src/renderer/assets/dev-live2d/hiyori/Hiyori.model3.json
git status --short
```

预期：测试通过；两个文件存在；`git status` 不显示 `dev-live2d` 资源。

- [ ] **步骤 7：提交任务 1**

```bash
git add .gitignore desktop-app/electron-builder.yml desktop-app/package.json \
  desktop-app/scripts/live2d-dev-assets.js desktop-app/scripts/setup-live2d-dev.js \
  desktop-app/tests/live2d-dev-assets.test.js
git commit -m "feat: 增加 Live2D 开发资源恢复工具"
```

### 任务 2：模型配置与兼容依赖

**文件：**

- 创建：`desktop-app/src/renderer/js/live2d-config.js`
- 创建：`desktop-app/tests/live2d-config.test.js`
- 修改：`desktop-app/package.json`
- 修改：`desktop-app/package-lock.json`

- [ ] **步骤 1：编写失败的配置单元测试**

```js
const assert = require('node:assert/strict');
const test = require('node:test');
const {
    clampScaleMultiplier,
    fitScale,
    readStoredScale,
    resolveMotionGroup
} = require('../src/renderer/js/live2d-config');

test('maps supported motion aliases and rejects unknown motions', () => {
    assert.equal(resolveMotionGroup('idle'), 'Idle');
    assert.equal(resolveMotionGroup('Idle'), 'Idle');
    assert.equal(resolveMotionGroup('tap_body'), 'TapBody');
    assert.equal(resolveMotionGroup('wave'), null);
});

test('clamps scale and rejects invalid stored values', () => {
    assert.equal(clampScaleMultiplier(0.1), 0.5);
    assert.equal(clampScaleMultiplier(2), 1.2);
    assert.equal(readStoredScale('85'), 0.85);
    assert.equal(readStoredScale('invalid'), 1);
});

test('calculates a safe model fit scale', () => {
    assert.equal(fitScale(600, 1000), 0.51);
    assert.equal(fitScale(0, 1000), 1);
    assert.equal(fitScale(600, 0), 1);
});
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```bash
cd desktop-app
node --test tests/live2d-config.test.js
```

预期：FAIL，错误为 `Cannot find module '../src/renderer/js/live2d-config'`。

- [ ] **步骤 3：实现配置纯函数**

```js
const MODEL_PATH = 'assets/dev-live2d/hiyori/Hiyori.model3.json';
const CORE_PATH = 'assets/dev-live2d/live2dcubismcore4.min.js';
const SCALE_MIN = 0.5;
const SCALE_MAX = 1.2;
const SCALE_DEFAULT = 1;
const MOTION_ALIASES = Object.freeze({
    idle: 'Idle',
    Idle: 'Idle',
    tap_body: 'TapBody',
    TapBody: 'TapBody'
});

function clampScaleMultiplier(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return SCALE_DEFAULT;
    return Math.min(SCALE_MAX, Math.max(SCALE_MIN, numeric));
}

function readStoredScale(value) {
    const numeric = Number.parseInt(value, 10);
    return Number.isFinite(numeric)
        ? clampScaleMultiplier(numeric / 100)
        : SCALE_DEFAULT;
}

function fitScale(containerHeight, modelHeight) {
    if (!(containerHeight > 0) || !(modelHeight > 0)) return SCALE_DEFAULT;
    return Number(((containerHeight / modelHeight) * 0.85).toFixed(6));
}

function resolveMotionGroup(name) {
    return typeof name === 'string' ? MOTION_ALIASES[name] ?? null : null;
}

module.exports = {
    CORE_PATH,
    MODEL_PATH,
    SCALE_DEFAULT,
    SCALE_MAX,
    SCALE_MIN,
    clampScaleMultiplier,
    fitScale,
    readStoredScale,
    resolveMotionGroup
};
```

- [ ] **步骤 4：固定 Pixi 6 兼容版本**

运行：

```bash
cd desktop-app
npm install --save-exact pixi.js@6.5.10
npm ls pixi.js pixi-live2d-display
```

预期：输出包含 `pixi.js@6.5.10` 和 `pixi-live2d-display@0.4.0`，没有 invalid peer dependency。

- [ ] **步骤 5：运行配置测试和 renderer 构建**

```bash
cd desktop-app
npm run test:unit
npm run build:renderer
```

预期：单元测试与 esbuild 均退出 0。

- [ ] **步骤 6：提交任务 2**

```bash
git add desktop-app/package.json desktop-app/package-lock.json \
  desktop-app/src/renderer/js/live2d-config.js desktop-app/tests/live2d-config.test.js
git commit -m "refactor: 固定 Live2D 兼容依赖与配置"
```

### 任务 3：Live2D 加载、动作与窗口交互

**文件：**

- 修改：`desktop-app/src/renderer/js/live2d.js`
- 修改：`desktop-app/src/renderer/index.html`
- 修改：`desktop-app/src/renderer/styles/main.css`
- 修改：`desktop-app/package.json`

- [ ] **步骤 1：为 HTML 和代码契约增加静态检查**

在 `desktop-app/tests/live2d-renderer-contract.test.js` 中写入：

```js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const rendererRoot = path.resolve(__dirname, '..', 'src', 'renderer');

test('loads Cubism Core before the renderer bundle', () => {
    const html = fs.readFileSync(path.join(rendererRoot, 'index.html'), 'utf8');
    const core = html.indexOf('assets/dev-live2d/live2dcubismcore4.min.js');
    const bundle = html.indexOf('dist/renderer.js');
    assert.ok(core >= 0);
    assert.ok(bundle > core);
    assert.match(html, /class="drag-handle"/);
});

test('renderer uses the Cubism 4 entry and real Hiyori motion groups', () => {
    const source = fs.readFileSync(path.join(rendererRoot, 'js', 'live2d.js'), 'utf8');
    assert.match(source, /pixi-live2d-display\/cubism4/);
    assert.match(source, /resolveMotionGroup/);
    assert.doesNotMatch(source, /tap_head/);
});
```

- [ ] **步骤 2：运行契约测试并确认失败**

```bash
cd desktop-app
node --test tests/live2d-renderer-contract.test.js
```

预期：FAIL，因为 HTML 尚未加载 Cubism Core，且 renderer 尚未使用 cubism4 入口。

- [ ] **步骤 3：更新 HTML 和拖动区域**

`index.html` 在容器后增加：

```html
<div class="drag-handle" aria-label="拖动桌面助手窗口"></div>
```

并在 renderer bundle 前增加：

```html
<script src="assets/dev-live2d/live2dcubismcore4.min.js"></script>
```

`main.css` 将拖动规则改为：

```css
body {
    background: transparent;
    overflow: hidden;
    -webkit-app-region: no-drag;
}

#live2d-container,
#live2d-container canvas {
    -webkit-app-region: no-drag;
}

.drag-handle {
    position: absolute;
    top: 0;
    left: 12px;
    right: 12px;
    height: 32px;
    z-index: 20;
    cursor: grab;
    -webkit-app-region: drag;
}

.drag-handle:active {
    cursor: grabbing;
}
```

- [ ] **步骤 4：实现明确的加载状态和模型交互**

`live2d.js` 必须：

1. 在模块加载后设置 `window.PIXI = PIXI`。
2. 从 `pixi-live2d-display/cubism4` 导入 `Live2DModel`。
3. 缺少 `window.Live2DCubismCore` 时显示准备命令并返回 `false`。
4. 使用单一 `initializationPromise` 防止重复初始化。
5. 使用 `Live2DModel.from(MODEL_PATH, { autoInteract: true })`。
6. 在 `hit` 事件包含 `Body` 时执行 `model.motion('TapBody')`。
7. 使用 `fitScale` 和倍率定位模型。
8. 捕获模型加载错误并显示不含绝对路径的安全消息。
9. 将 `playMotion` 与 `setExpression` 设计为返回布尔值。
10. 绑定窗口 resize、快捷键和 `Ctrl/Command + wheel`。

核心动作接口：

```js
function playMotion(name) {
    const group = resolveMotionGroup(name);
    if (!model || lifecycleState !== 'ready' || !group) {
        if (name) console.warn(`[Live2D] Unsupported or unavailable motion: ${name}`);
        return false;
    }
    void model.motion(group).catch((error) => {
        console.warn(`[Live2D] Motion failed: ${error.name}`);
    });
    return true;
}

function setExpression(name) {
    if (!model || lifecycleState !== 'ready' || !name) return false;
    const manager = model.internalModel?.motionManager?.expressionManager;
    if (!manager || !manager.definitions?.length) return false;
    void model.expression(name).catch((error) => {
        console.warn(`[Live2D] Expression failed: ${error.name}`);
    });
    return true;
}
```

- [ ] **步骤 5：把完整单元测试加入默认测试**

`package.json` 的 `test` 改为：

```json
"test": "npm run test:unit && npm run build:renderer && node --check src/main.js && node --check src/preload.js"
```

- [ ] **步骤 6：运行测试和构建**

```bash
cd desktop-app
npm test
npm run build:renderer
```

预期：所有 Node 测试通过，renderer 构建成功，Electron 主进程与 preload 语法检查成功。

- [ ] **步骤 7：提交任务 3**

```bash
git add desktop-app/package.json desktop-app/src/renderer/index.html \
  desktop-app/src/renderer/styles/main.css desktop-app/src/renderer/js/live2d.js \
  desktop-app/tests/live2d-renderer-contract.test.js
git commit -m "feat: 实现 Live2D 模型运行与交互"
```

### 任务 4：文档、打包隔离与视觉验收

**文件：**

- 创建：`desktop-app/THIRD_PARTY_DEV_ASSETS.md`
- 修改：`README.md`
- 修改：`docs/superpowers/plans/2026-07-28-live2d-development-runtime.md`

- [ ] **步骤 1：写入第三方开发资源说明**

文档必须包含：

```markdown
# Third-party Live2D development assets

`npm run setup:live2d-dev` restores Hiyori Momose sample data and
Live2D Cubism Core from the repository archive tag for local development.

This content uses sample data owned and copyrighted by Live2D Inc.

The generated `src/renderer/assets/dev-live2d/` directory is Git-ignored
and excluded from application packages. Review Live2D's Sample Data Terms
and SDK Publication License before distributing any model or Cubism Core.
```

- [ ] **步骤 2：更新 README**

README 增加以下可执行流程：

```bash
cd desktop-app
npm install
npm run setup:live2d-dev
npm start
```

同时说明：

- 没有运行准备命令时，Electron 仍可启动并显示资源提示。
- Hiyori 只用于本地开发验证。
- 正式安装包默认不包含开发资源。
- 归档标签和恢复命令。

- [ ] **步骤 3：验证打包配置排除开发资源**

运行：

```bash
cd desktop-app
npm run build -- --dir
find dist -type f -path '*dev-live2d*'
```

预期：builder 退出 0；`find` 无输出。

- [ ] **步骤 4：运行完整自动验证**

```bash
python3 -m compileall -q backend
python3 -m unittest discover -s backend/tests -v
cd desktop-app
npm test
```

预期：后端全部测试通过，桌面端全部测试与构建通过。

- [ ] **步骤 5：启动 Electron 进行视觉验收**

运行：

```bash
cd desktop-app
npm start
```

检查：

- 模型可见且居中。
- 待机、眨眼和物理摆动可见。
- 鼠标移动触发视线跟随。
- 点击身体播放 `TapBody`。
- 顶部拖动区与模型点击互不冲突。
- `+`、`-`、`0` 和 `Ctrl/Command + 滚轮` 有效。

- [ ] **步骤 6：验证缺失资源降级**

将生成目录暂时移动到同级备份目录，启动 Electron，确认显示：

`缺少 Live2D 开发资源，请运行 npm run setup:live2d-dev`

验收后恢复生成目录，不删除归档资源。

- [ ] **步骤 7：提交文档与验收记录**

```bash
git add README.md desktop-app/THIRD_PARTY_DEV_ASSETS.md \
  docs/superpowers/plans/2026-07-28-live2d-development-runtime.md
git commit -m "docs: 补充 Live2D 开发与授权说明"
```

- [ ] **步骤 8：最终检查**

```bash
git diff --check origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
```

预期：没有空白错误，工作树干净，提交按资源恢复、兼容配置、运行交互和文档顺序排列。

