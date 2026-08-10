# Live2D 模型回复自动朗读实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（- [ ]）语法来跟踪进度。

**目标：** Live2D 桌面端收到只有文字的模型 speak 消息时，按需调用现有 TTS 接口并自动播放语音。

**架构：** 新增独立的 Renderer 语音播放模块，封装已有音频播放、按需 TTS、同源 URL 校验、重复抑制和安全降级。WebSocket 模块只负责把 speak 消息交给该模块，并继续独立处理动作与表情；后端聊天、QQ 和模型网关保持不变。

**技术栈：** Electron、CommonJS、浏览器 Fetch API、HTMLAudioElement、Node.js Test Runner、esbuild

---

## 文件结构

- 创建：desktop-app/src/renderer/js/speech-playback.js，提供可注入、可单测的语音合成与播放生命周期。
- 创建：desktop-app/tests/speech-playback.test.js，覆盖直接播放、动态合成、失败降级、重复抑制和有界缓存。
- 修改：desktop-app/src/renderer/js/websocket.js，把 speak 音频处理委托给语音播放模块。
- 修改：desktop-app/tests/live2d-renderer-contract.test.js，锁定 WebSocket 与语音模块的接线契约。
- 修改：README.md，记录桌面模型回复自动朗读能力与降级边界。
- 修改：本计划，记录真实执行和验收结果。

## 任务 1：实现独立语音播放模块

**文件：**

- 创建：desktop-app/src/renderer/js/speech-playback.js
- 创建：desktop-app/tests/speech-playback.test.js

- [x] **步骤 1：编写失败的语音播放测试**

创建 desktop-app/tests/speech-playback.test.js。测试使用注入的 fetch、Audio 和 logger 替身，并精确覆盖：

1. 已有 /api/tts/audio/example.mp3 时直接播放，fetch 调用次数为 0。
2. 只有 text 时请求 http://127.0.0.1:8080/api/tts/speak，请求方法为 POST，请求体只包含去除首尾空白后的 text，随后播放返回的 audio_url。
3. 空白 text 不请求 TTS；跨源音频 URL 不创建 Audio。
4. TTS 非 2xx、JSON 解析失败、audio_url 缺失与 Audio.play() 拒绝均返回 false，日志只能包含稳定类别。
5. 相同 correlationId 只处理 1 次；处理 101 个不同标识后 processedCount() 为 100，最早标识可再次处理。

测试骨架：

~~~javascript
const assert = require('node:assert/strict');
const test = require('node:test');
const {
    MAX_PROCESSED_CORRELATIONS,
    createSpeechPlayback
} = require('../src/renderer/js/speech-playback');

function audioHarness({ rejectPlay = false } = {}) {
    const urls = [];
    class FakeAudio {
        constructor(url) {
            urls.push(url);
        }

        play() {
            return rejectPlay
                ? Promise.reject(new Error('blocked'))
                : Promise.resolve();
        }
    }
    return { AudioCtor: FakeAudio, urls };
}
~~~

动态合成测试必须断言：

~~~javascript
assert.equal(request.url, 'http://127.0.0.1:8080/api/tts/speak');
assert.equal(request.options.method, 'POST');
assert.equal(request.options.headers['Content-Type'], 'application/json');
assert.deepEqual(JSON.parse(request.options.body), { text: '主人你好' });
assert.deepEqual(audio.urls, [
    'http://127.0.0.1:8080/api/tts/audio/generated.mp3'
]);
~~~

失败测试必须断言日志严格等于：

~~~javascript
[
    'Live2D TTS request failed',
    'Live2D audio playback failed'
]
~~~

- [x] **步骤 2：运行测试并确认模块缺失**

运行：

~~~bash
node --test desktop-app/tests/speech-playback.test.js
~~~

预期：FAIL，错误包含 Cannot find module '../src/renderer/js/speech-playback'。

- [x] **步骤 3：实现最小语音播放模块**

创建 desktop-app/src/renderer/js/speech-playback.js：

~~~javascript
const DEFAULT_BACKEND_URL = 'http://127.0.0.1:8080';
const MAX_PROCESSED_CORRELATIONS = 100;

function createSpeechPlayback({
    fetchImpl = (...args) => window.fetch(...args),
    AudioCtor = window.Audio,
    logger = console,
    backendUrl = DEFAULT_BACKEND_URL
} = {}) {
    const backend = new URL(backendUrl);
    const processed = new Set();

    function remember(correlationId) {
        if (typeof correlationId !== 'string' || !correlationId.trim()) {
            return true;
        }
        const normalized = correlationId.trim();
        if (processed.has(normalized)) return false;
        processed.add(normalized);
        while (processed.size > MAX_PROCESSED_CORRELATIONS) {
            processed.delete(processed.values().next().value);
        }
        return true;
    }

    function resolveAudioUrl(value) {
        if (typeof value !== 'string' || !value.trim()) return null;
        try {
            const resolved = new URL(value.trim(), backend);
            return resolved.origin === backend.origin ? resolved.toString() : null;
        } catch {
            return null;
        }
    }

    async function playAudio(value) {
        const url = resolveAudioUrl(value);
        if (!url) return false;
        try {
            const audio = new AudioCtor(url);
            await Promise.resolve(audio.play());
            return true;
        } catch {
            logger.warn('Live2D audio playback failed');
            return false;
        }
    }

    async function handleSpeakAudio(message) {
        const audioUrl = resolveAudioUrl(message?.audioUrl);
        const text = typeof message?.text === 'string'
            ? message.text.trim()
            : '';
        if (!audioUrl && !text) return false;
        if (!remember(message?.correlationId)) return false;
        if (audioUrl) return playAudio(audioUrl);

        try {
            const response = await fetchImpl(
                new URL('/api/tts/speak', backend).toString(),
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text })
                }
            );
            if (!response.ok) throw new Error('tts request failed');
            const payload = await response.json();
            const generated = resolveAudioUrl(payload?.audio_url);
            if (!generated) throw new Error('invalid tts response');
            return playAudio(generated);
        } catch {
            logger.warn('Live2D TTS request failed');
            return false;
        }
    }

    return {
        handleSpeakAudio,
        processedCount: () => processed.size
    };
}

module.exports = {
    DEFAULT_BACKEND_URL,
    MAX_PROCESSED_CORRELATIONS,
    createSpeechPlayback
};
~~~

- [x] **步骤 4：运行语音播放测试并确认通过**

运行：

~~~bash
node --test desktop-app/tests/speech-playback.test.js
~~~

预期：5 项测试通过，0 项失败。

- [x] **步骤 5：提交独立模块**

~~~bash
git add -- desktop-app/src/renderer/js/speech-playback.js \
  desktop-app/tests/speech-playback.test.js
git commit -m "feat: 增加 Live2D 按需语音播放模块"
~~~

## 任务 2：接入 WebSocket speak 消息

**文件：**

- 修改：desktop-app/src/renderer/js/websocket.js
- 修改：desktop-app/tests/live2d-renderer-contract.test.js

- [x] **步骤 1：编写失败的 Renderer 接线契约**

在 desktop-app/tests/live2d-renderer-contract.test.js 末尾增加：

~~~javascript
test('websocket delegates speak audio to the bounded playback module', () => {
    const source = fs.readFileSync(
        path.join(rendererRoot, 'js', 'websocket.js'),
        'utf8'
    );

    assert.match(source, /require\('\.\/speech-playback'\)/);
    assert.match(source, /createSpeechPlayback\(\)/);
    assert.match(source, /speechPlayback\.handleSpeakAudio\(message\)/);
    assert.doesNotMatch(source, /new Audio\(/);
});
~~~

- [x] **步骤 2：运行契约测试并确认失败**

~~~bash
node --test desktop-app/tests/live2d-renderer-contract.test.js
~~~

预期：FAIL，WebSocket 尚未引用 speech-playback。

- [x] **步骤 3：把音频处理委托给新模块**

在 desktop-app/src/renderer/js/websocket.js 顶部增加：

~~~javascript
const { createSpeechPlayback } = require('./speech-playback');
const speechPlayback = createSpeechPlayback();
~~~

把 handleSpeak() 中现有的 Audio 逻辑替换为：

~~~javascript
function handleSpeak(message) {
    void speechPlayback.handleSpeakAudio(message);

    if (message.motion) {
        window.playMotion && window.playMotion(message.motion);
    }

    if (message.expression) {
        window.setExpression && window.setExpression(message.expression);
    }
}
~~~

不得改变 handleMessage() 的消息分类、WebSocket 重连或工具确认分发逻辑。

- [x] **步骤 4：运行定向测试和 Renderer 构建**

~~~bash
node --test \
  desktop-app/tests/speech-playback.test.js \
  desktop-app/tests/live2d-renderer-contract.test.js
npm run build:renderer --prefix desktop-app
~~~

预期：定向测试全部通过；esbuild 退出码为 0。

- [x] **步骤 5：提交 WebSocket 接线**

~~~bash
git add -- desktop-app/src/renderer/js/websocket.js \
  desktop-app/tests/live2d-renderer-contract.test.js
git commit -m "feat: 自动朗读 Live2D 模型回复"
~~~

## 任务 3：文档、完整验证与真实联调

**文件：**

- 修改：README.md
- 修改：docs/superpowers/plans/2026-08-10-live2d-chat-tts.md

- [x] **步骤 1：更新 README 能力和降级说明**

在 README.md 的「当前能力」中增加：

~~~markdown
- Live2D 在线时，桌面端会为没有现成音频的模型回复按需调用 TTS 并自动播放；语音失败不影响文字回复、动作或 WebSocket。
~~~

在 Live2D 开发模型说明后补充：

~~~markdown
模型聊天回复通过桌面端按需请求现有 TTS 接口，因此只有 Live2D 窗口在线时才会自动生成语音。场景消息已有音频时直接播放，不会重复合成；QQ 回复不会触发桌面端朗读。
~~~

- [x] **步骤 2：运行桌面端完整验证**

~~~bash
npm test --prefix desktop-app
git diff --check
~~~

预期：新增 5 项语音模块测试和 1 项接线契约后，共 41 项 Node.js 测试通过；Renderer 构建、src/main.js 和 src/preload.js 语法检查通过；git diff --check 无输出。

- [x] **步骤 3：进行真实联调**

确保后端与开发版 Electron 正在运行：

~~~bash
curl --fail --silent --show-error \
  http://127.0.0.1:8080/api/avatar/status
~~~

预期：返回 {"connected":true,"expression":null}。

发送固定、无敏感信息的消息：

~~~bash
curl --silent --show-error --max-time 45 \
  -H 'Content-Type: application/json' \
  -d '{"source":"desktop","senderId":"local-user","content":"请只回复：自动朗读联调成功","messageId":"live2d-chat-tts-smoke-20260810"}' \
  http://127.0.0.1:8080/api/chat/message
~~~

预期：聊天接口返回 HTTP 200；访问日志随后出现 POST /api/tts/speak 200 和 GET /api/tts/audio/... 200，证明在线 Renderer 完成合成与音频读取。日志不得包含供应商响应正文或凭据。

- [x] **步骤 4：验证失败降级**

使用单元测试替身验证 TTS 503 和 Audio.play() 拒绝。不得在真实环境中故意破坏用户配置。确认失败只产生稳定日志，聊天、动作和 WebSocket 契约保持通过。

- [x] **步骤 5：记录验收结果**

在本计划末尾增加「执行结果」章节，只记录：

- 自动测试数量与退出码。
- Renderer 构建结果。
- Live2D 在线状态。
- 聊天、TTS 和音频读取的 HTTP 状态码。
- TTS 使用 GPT-SoVITS 或 EdgeTTS 回退，但不记录服务响应正文或地址参数。

- [x] **步骤 6：提交文档与验收记录**

~~~bash
git add -- README.md docs/superpowers/plans/2026-08-10-live2d-chat-tts.md
git commit -m "docs: 记录 Live2D 自动朗读验收结果"
~~~

- [x] **步骤 7：最终检查**

~~~bash
git status --short --branch
git log --oneline main..HEAD
git diff --check main...HEAD
~~~

预期：工作树干净；分支包含规格、语音模块、WebSocket 接线和验收文档提交；差异检查无输出。

## 执行结果（2026-08-10）

- TDD 红灯确认：语音模块测试首次运行因 `speech-playback` 模块不存在而失败；WebSocket 接线契约首次运行因尚未引用该模块而失败。
- 定向验证：语音模块 5 项测试通过；语音模块与 Live2D Renderer 契约合计 10 项测试通过。
- 桌面端完整验证：41 项 Node.js 测试全部通过，0 项失败；Renderer 由 esbuild 成功构建，`src/main.js` 与 `src/preload.js` 语法检查通过。
- Live2D 开发版重新启动后，`GET /api/avatar/status` 返回 HTTP 200，连接状态为 `true`。
- 真实模型联调：`POST /api/chat/message` 返回 HTTP 200；在线 Renderer 随后请求 `POST /api/tts/speak` 并获得 HTTP 200，再以 `GET /api/tts/audio/...` 读取音频并获得 HTTP 206。
- 本次真实联调中本机 GPT-SoVITS 不可达，后端使用 EdgeTTS 回退生成 MP3。记录未包含供应商响应正文、地址参数、模型密钥或其他凭据。
- Mac 在视觉复核阶段处于锁屏状态，因此没有把人工听感记为验收证据；Renderer 对音频资源的 HTTP 206 读取证明浏览器媒体播放流程已经启动。
- 最终检查再次运行桌面端完整测试，41 项全部通过；分支差异检查无输出，提交前工作树干净。
