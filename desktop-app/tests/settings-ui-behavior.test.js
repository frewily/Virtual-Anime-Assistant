const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticRoot = path.resolve(__dirname, '..', '..', 'backend', 'settings', 'static');
const html = fs.readFileSync(path.join(staticRoot, 'index.html'), 'utf8');
const originalSource = fs.readFileSync(path.join(staticRoot, 'settings.js'), 'utf8');

class FakeClassList {
    constructor(value = '') { this.values = new Set(value.split(/\s+/).filter(Boolean)); }
    add(value) { this.values.add(value); }
    remove(value) { this.values.delete(value); }
    toggle(value, force) {
        if (force === undefined) force = !this.values.has(value);
        if (force) this.values.add(value); else this.values.delete(value);
    }
    contains(value) { return this.values.has(value); }
}

class FakeElement {
    constructor(tagName, attributes = {}) {
        this.tagName = tagName.toUpperCase();
        this.id = attributes.id || '';
        this.type = attributes.type || '';
        this.value = '';
        this.checked = false;
        this.disabled = false;
        this.hidden = Object.hasOwn(attributes, 'hidden');
        this.textContent = '';
        this.dataset = {};
        this.attributes = new Map(Object.entries(attributes));
        this.classList = new FakeClassList(attributes.class || '');
        this.listeners = new Map();
        this.children = [];
        this.options = this.children;
        this.parentElement = null;
        for (const [name, value] of Object.entries(attributes)) {
            if (name.startsWith('data-')) {
                const key = name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
                this.dataset[key] = value;
            }
        }
    }
    get firstChild() { return this.children[0] || null; }
    appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    async dispatch(name, target = this) {
        const callback = this.listeners.get(name);
        if (callback) return callback({ target, preventDefault() {} });
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) || null; }
    removeAttribute(name) { this.attributes.delete(name); }
    matches(selector) {
        if (selector === '[data-secret]') return this.dataset.secret !== undefined;
        return false;
    }
    focus() {}
}

function parseAttributes(raw) {
    const attributes = {};
    for (const match of raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
        attributes[match[1]] = match[2] === undefined ? '' : match[2];
    }
    return attributes;
}

function makeHarness(fetchImpl) {
    const elements = [];
    const ids = new Map();
    for (const match of html.matchAll(/<([a-z]+)([^>]*)>/g)) {
        const attributes = parseAttributes(match[2]);
        const element = new FakeElement(match[1], attributes);
        elements.push(element);
        if (element.id) ids.set(element.id, element);
    }
    const tablist = elements.find((node) => node.getAttribute('role') === 'tablist');
    for (const tab of elements.filter((node) => node.getAttribute('role') === 'tab')) tab.parentElement = tablist;

    function withDataset(key) { return elements.filter((node) => node.dataset[key] !== undefined); }
    const document = {
        getElementById: (id) => ids.get(id),
        createElement: (name) => new FakeElement(name),
        querySelector(selector) {
            if (selector === '[role="tablist"]') return tablist;
            if (selector === '.panel.is-active') return elements.find((node) => node.classList.contains('panel') && node.classList.contains('is-active')) || null;
            return null;
        },
        querySelectorAll(selector) {
            const datasets = {
                '[data-path]': 'path', '[data-source-for]': 'sourceFor',
                '[data-error-for]': 'errorFor', '[data-secret]': 'secret',
                '[data-secret-state]': 'secretState', '[data-replace]': 'replace',
                '[data-retain]': 'retain', '[data-delete]': 'delete', '[data-test]': 'test',
                '[data-test-status]': 'testStatus'
            };
            if (datasets[selector]) return withDataset(datasets[selector]);
            if (selector === '[data-replace], [data-delete]') return elements.filter((node) => node.dataset.replace !== undefined || node.dataset.delete !== undefined);
            if (selector === '[data-replace], [data-retain], [data-delete]') return elements.filter((node) => node.dataset.replace !== undefined || node.dataset.retain !== undefined || node.dataset.delete !== undefined);
            if (selector === '[role="tab"]') return elements.filter((node) => node.getAttribute('role') === 'tab');
            if (selector === '[role="tabpanel"]') return elements.filter((node) => node.getAttribute('role') === 'tabpanel');
            if (selector === '[aria-invalid="true"]') return elements.filter((node) => node.getAttribute('aria-invalid') === 'true');
            return [];
        }
    };
    const window = {
        confirm: () => true,
        addEventListener() {},
        matchMedia: () => ({ matches: false, addEventListener() {} })
    };
    const hooks = {};
    const source = originalSource.replace(
        '  initialize();',
        `  Object.assign(window.__hooks, {
          state, applySnapshot, collectDraft, collectProbe, markFieldDirty,
          request, loadAuthenticatedData, authenticate, saveSettings, runProbe,
          performLogout, retainSecret
        });`
    );
    vm.runInNewContext(source, {
        window: Object.assign(window, { __hooks: hooks }), document,
        fetch: fetchImpl, AbortController, JSON, Object, Array, Map, Set,
        String, Number, RegExp, Error, Promise, Boolean
    });
    return { hooks, ids, elements };
}

function snapshot(revision, overrides = {}) {
    const draft = {
        revision,
        llm: { enabled: true, baseUrl: 'persisted-url', model: 'saved-model', timeoutSeconds: 60, maxContextMessages: 20, maxContextChars: 12000, toolCallingEnabled: false, apiKey: { operation: 'retain' } },
        qq: { enabled: false, allowedGroupIds: [], allowedUserIds: [], ratePerMinute: 10, rateBurst: 2, maxConcurrency: 4, actionTimeoutSeconds: 10, accessToken: { operation: 'retain' } },
        tts: { gptSovitsUrl: 'persisted-tts', defaultVoiceId: 'character_001', audioMaxAgeSeconds: 86400 }
    };
    return {
        restartRequired: true,
        presentation: { fields: overrides.fields || {}, keychainAvailable: true },
        draft: Object.assign(draft, overrides.draft || {})
    };
}

function response(payload, status = 200) {
    return { status, ok: status >= 200 && status < 300, json: async () => payload };
}

function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
}

test('save response merges baseline without losing newer model or secret edits', async () => {
    let resolveSave;
    const pending = new Promise((resolve) => { resolveSave = resolve; });
    const harness = makeHarness(() => pending);
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf';

    const save = hooks.saveSettings({ preventDefault() {} });
    ids.get('llm-model').value = 'newer-model';
    hooks.markFieldDirty('llm.model');
    ids.get('llm-api-key').value = 'newer-secret';
    hooks.state.secrets['llm.apiKey'] = { operation: 'replace', value: 'newer-secret' };
    hooks.markFieldDirty('llm.apiKey');
    resolveSave(response(snapshot('b'.repeat(64))));
    await save;

    assert.equal(hooks.state.draft.revision, 'b'.repeat(64));
    assert.equal(ids.get('llm-model').value, 'newer-model');
    assert.equal(hooks.state.secrets['llm.apiKey'].operation, 'replace');
    assert.equal(hooks.state.secrets['llm.apiKey'].value, 'newer-secret');
    assert.equal(hooks.state.dirty, true);
});

test('probe payloads use only their section and effective environment values', async () => {
    const payloads = [];
    const harness = makeHarness(async (_path, init) => {
        payloads.push(JSON.parse(init.body));
        return response({ ok: true, code: 'SUCCESS' });
    });
    const { hooks, ids, elements } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64), { fields: {
        'llm.baseUrl': { source: 'environment', value: 'effective-llm', readOnly: true, environmentVariable: 'LLM_ENV' },
        'qq.ratePerMinute': { source: 'environment', value: 9999, readOnly: true, environmentVariable: 'QQ_RATE_ENV' },
        'tts.gptSovitsUrl': { source: 'environment', value: 'effective-tts', readOnly: true, environmentVariable: 'TTS_ENV' }
    } }));
    assert.equal(hooks.collectDraft().llm.baseUrl, 'persisted-url');
    assert.equal(hooks.collectDraft().qq.ratePerMinute, 10);
    assert.equal(hooks.collectProbe('llm').baseUrl, 'effective-llm');
    assert.equal(hooks.collectProbe('qq').ratePerMinute, 9999);
    ids.get('qq-rate-per-minute').value = '';
    assert.equal(hooks.collectProbe('tts').gptSovitsUrl, 'effective-tts');

    hooks.state.csrfToken = 'csrf';
    await hooks.runProbe(elements.find((node) => node.dataset.test === 'llm'));
    ids.get('qq-rate-per-minute').value = '9999';
    await hooks.runProbe(elements.find((node) => node.dataset.test === 'qq'));
    ids.get('qq-rate-per-minute').value = '';
    await hooks.runProbe(elements.find((node) => node.dataset.test === 'tts'));
    assert.equal(payloads[0].baseUrl, 'effective-llm');
    assert.equal(payloads[1].ratePerMinute, 9999);
    assert.equal(payloads[2].gptSovitsUrl, 'effective-tts');
});

test('stale probe response is ignored after an edit', async () => {
    let resolveProbe;
    const pending = new Promise((resolve) => { resolveProbe = resolve; });
    const harness = makeHarness(() => pending);
    const { hooks, ids, elements } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.csrfToken = 'csrf';
    const button = elements.find((node) => node.dataset.test === 'tts');
    const status = elements.find((node) => node.dataset.testStatus === 'tts');

    const probe = hooks.runProbe(button);
    ids.get('tts-gpt-sovits-url').value = 'newer-tts';
    hooks.markFieldDirty('tts.gptSovitsUrl');
    resolveProbe(response({ ok: true, code: 'SUCCESS' }));
    await probe;
    assert.notEqual(status.textContent, '测试成功');
});

test('failed logout keeps authenticated dirty state and retain clears a secret', async () => {
    const harness = makeHarness(async () => response({ error: { code: 'SERVICE_ERROR' } }, 503));
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf';
    hooks.markFieldDirty('llm.model');

    await hooks.performLogout();
    assert.equal(hooks.state.authenticated, true);
    assert.equal(hooks.state.dirty, true);
    assert.ok(hooks.state.draft);

    ids.get('llm-api-key').value = 'discard-me';
    hooks.state.secrets['llm.apiKey'] = { operation: 'replace', value: 'discard-me' };
    hooks.retainSecret('llm.apiKey');
    assert.equal(hooks.state.secrets['llm.apiKey'].operation, 'retain');
    assert.equal(hooks.state.secrets['llm.apiKey'].value, null);
    assert.equal(ids.get('llm-api-key').value, '');
});

test('stale four-step 401 cannot expire a newer authenticated session', async () => {
    const oldConfig = deferred();
    const harness = makeHarness((path) => {
        if (path.endsWith('/login')) return response({ authenticated: true, csrfToken: 'csrf-new' });
        return oldConfig.promise;
    });
    const { hooks } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf-old';

    const oldRequest = hooks.request('/api/settings/config', { sessionBound: true });
    hooks.state.reauthPending = true;
    await hooks.authenticate('login', 'new-password');
    oldConfig.resolve(response({}, 401));
    await assert.rejects(oldRequest);

    assert.equal(hooks.state.authenticated, true);
    assert.equal(hooks.state.csrfToken, 'csrf-new');
});

test('old config and voices 200 responses cannot overwrite a newly logged-in snapshot', async () => {
    const oldConfig = deferred();
    const oldVoices = deferred();
    const harness = makeHarness((path) => {
        if (path.endsWith('/config')) return oldConfig.promise;
        if (path.endsWith('/voices')) return oldVoices.promise;
        return response({ authenticated: true, csrfToken: 'csrf-new' });
    });
    const { hooks } = harness;
    hooks.applySnapshot(snapshot('n'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf-old';

    const oldLoad = hooks.loadAuthenticatedData();
    hooks.state.reauthPending = true;
    await hooks.authenticate('login', 'new-password');
    oldConfig.resolve(response(snapshot('o'.repeat(64))));
    oldVoices.resolve(response([{ id: 'old', name: 'Old' }]));
    await oldLoad.catch(() => {});

    assert.equal(hooks.state.draft.revision, 'n'.repeat(64));
    assert.equal(hooks.state.csrfToken, 'csrf-new');
});

test('reversed double-login responses only allow the latest attempt to activate', async () => {
    const first = deferred();
    const second = deferred();
    const harness = makeHarness((_path, init) => {
        const password = JSON.parse(init.body).password;
        if (password === 'first-password') return first.promise;
        if (password === 'second-password') return second.promise;
        return response(snapshot('z'.repeat(64)));
    });
    const { hooks } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.reauthPending = true;

    const older = hooks.authenticate('login', 'first-password');
    const newer = hooks.authenticate('login', 'second-password');
    second.resolve(response({ authenticated: true, csrfToken: 'csrf-second' }));
    await newer;
    first.resolve(response({ authenticated: true, csrfToken: 'csrf-first' }));
    await older.catch(() => {});

    assert.equal(hooks.state.authenticated, true);
    assert.equal(hooks.state.csrfToken, 'csrf-second');
});

test('login UI disables both auth actions and ignores duplicate submission', async () => {
    const login = deferred();
    let loginRequests = 0;
    const harness = makeHarness((path) => {
        if (path.endsWith('/login')) {
            loginRequests += 1;
            return login.promise;
        }
        return response({});
    });
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.reauthPending = true;
    ids.get('login-password').value = 'new-password';

    const first = ids.get('login-form').dispatch('submit');
    const duplicate = ids.get('login-form').dispatch('submit');
    assert.equal(loginRequests, 1);
    assert.equal(ids.get('setup-submit').disabled, true);
    assert.equal(ids.get('login-submit').disabled, true);
    login.resolve(response({ authenticated: true, csrfToken: 'csrf-new' }));
    await Promise.all([first, duplicate]);

    assert.equal(ids.get('setup-submit').disabled, false);
    assert.equal(ids.get('login-submit').disabled, false);
});

test('a stale 401 cannot clear a secret entered in the new session', async () => {
    const staleProbe = deferred();
    const harness = makeHarness((path) => {
        if (path.endsWith('/test/llm')) return staleProbe.promise;
        return response({ authenticated: true, csrfToken: 'csrf-new' });
    });
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf-old';

    const oldRequest = hooks.request('/api/settings/test/llm', { method: 'POST', sessionBound: true, body: {} });
    hooks.state.reauthPending = true;
    await hooks.authenticate('login', 'new-password');
    ids.get('llm-api-key').value = 'new-session-secret';
    hooks.state.secrets['llm.apiKey'] = { operation: 'replace', value: 'new-session-secret' };
    staleProbe.resolve(response({}, 401));
    await oldRequest.catch(() => {});

    assert.equal(ids.get('llm-api-key').value, 'new-session-secret');
    assert.equal(hooks.state.secrets['llm.apiKey'].value, 'new-session-secret');
    assert.equal(hooks.state.csrfToken, 'csrf-new');
});

test('a stale logout 5xx cannot write an error into the new session UI', async () => {
    const oldLogout = deferred();
    const harness = makeHarness((path) => {
        if (path.endsWith('/logout')) return oldLogout.promise;
        return response({ authenticated: true, csrfToken: 'csrf-new' });
    });
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf-old';

    const logout = hooks.performLogout();
    hooks.state.reauthPending = true;
    await hooks.authenticate('login', 'new-password');
    ids.get('save-status').textContent = 'new-session-status';
    oldLogout.resolve(response({ error: { code: 'SERVICE_ERROR' } }, 503));
    await logout;

    assert.equal(hooks.state.authenticated, true);
    assert.equal(hooks.state.csrfToken, 'csrf-new');
    assert.equal(ids.get('save-status').textContent, 'new-session-status');
});

test('a current-session 401 expires the session and clears secret input', async () => {
    const harness = makeHarness(async () => response({}, 401));
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf-current';
    hooks.state.restartPending = true;
    hooks.markFieldDirty('llm.model');
    ids.get('llm-api-key').value = 'sensitive';
    hooks.state.secrets['llm.apiKey'] = { operation: 'replace', value: 'sensitive' };

    await assert.rejects(hooks.request('/api/settings/config', { sessionBound: true }));

    assert.equal(hooks.state.authenticated, false);
    assert.equal(hooks.state.csrfToken, null);
    assert.equal(ids.get('llm-api-key').value, '');
    assert.equal(hooks.state.secrets['llm.apiKey'].operation, 'retain');
    assert.equal(hooks.state.restartPending, true);
    assert.equal(hooks.state.dirty, true);
    assert.match(ids.get('auth-message').textContent, /登录已过期/);
    assert.equal(ids.get('auth-panel').hidden, false);
    assert.equal(ids.get('workspace').hidden, true);
});

test('save 401 followed by reauthentication clears the old saving status', async () => {
    const saveResponse = deferred();
    const harness = makeHarness((path, init) => {
        if (path.endsWith('/config') && init.method === 'PUT') return saveResponse.promise;
        return response({ authenticated: true, csrfToken: 'csrf-new' });
    });
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf-old';

    const save = hooks.saveSettings({ preventDefault() {} });
    assert.equal(ids.get('save-status').textContent, '正在安全保存…');
    saveResponse.resolve(response({}, 401));
    await save;
    assert.equal(hooks.state.authenticated, false);
    await hooks.authenticate('login', 'new-password');

    assert.equal(hooks.state.authenticated, true);
    assert.equal(ids.get('save-status').textContent, '');
    assert.equal(hooks.state.dirty, false);
});

test('stale save catch and finally cannot clear a newer save operation status', async () => {
    const oldResponse = deferred();
    const newResponse = deferred();
    let saves = 0;
    const harness = makeHarness((path, init) => {
        if (path.endsWith('/config') && init.method === 'PUT') {
            saves += 1;
            return saves === 1 ? oldResponse.promise : newResponse.promise;
        }
        return response({ authenticated: true, csrfToken: 'csrf-new' });
    });
    const { hooks, ids } = harness;
    hooks.applySnapshot(snapshot('a'.repeat(64)));
    hooks.state.authenticated = true;
    hooks.state.csrfToken = 'csrf-old';

    const oldSave = hooks.saveSettings({ preventDefault() {} });
    hooks.state.reauthPending = true;
    await hooks.authenticate('login', 'new-password');
    const newSave = hooks.saveSettings({ preventDefault() {} });
    assert.equal(ids.get('save-status').textContent, '正在安全保存…');
    assert.equal(ids.get('save-settings').disabled, true);

    oldResponse.resolve(response({ error: { code: 'SETTINGS_CONFLICT' } }, 409));
    await oldSave;
    assert.equal(ids.get('save-status').textContent, '正在安全保存…');
    assert.equal(ids.get('save-settings').disabled, true);

    newResponse.resolve(response(snapshot('b'.repeat(64))));
    await newSave;
    assert.equal(ids.get('save-status').textContent, '保存成功');
    assert.equal(ids.get('save-settings').disabled, false);
});
