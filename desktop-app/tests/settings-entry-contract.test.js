const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');

const mainPath = path.resolve(__dirname, '..', 'src', 'main.js');
const SETTINGS_URL = 'http://127.0.0.1:8080/settings';
const ACCESS_TOKEN = 't'.repeat(43);

async function loadMain({
    openExternal = async () => {},
    packaged = false
} = {}) {
    const source = fs.readFileSync(mainPath, 'utf8');
    const state = {
        openExternalCalls: [],
        backendStarts: 0,
        backendStops: 0,
        startupReconciles: 0,
        quitCalls: 0,
        templates: [],
        appEvents: new Map(),
        ipcHandlers: new Map(),
        errors: []
    };
    const shell = {
        openExternal: async (...args) => {
            state.openExternalCalls.push(args);
            return openExternal(...args);
        }
    };

    class BrowserWindow {
        constructor() {
            this.webContents = {
                setWindowOpenHandler() {},
                on() {}
            };
        }

        loadFile() {}
        on() {}
    }

    class Tray {
        setToolTip() {}
        setContextMenu() {}
    }

    const electron = {
        app: {
            isPackaged: packaged,
            whenReady: () => Promise.resolve(),
            on(event, callback) { state.appEvents.set(event, callback); },
            quit: () => { state.quitCalls += 1; }
        },
        BrowserWindow,
        ipcMain: {
            on(channel, handler) { state.ipcHandlers.set(channel, handler); }
        },
        Tray,
        Menu: {
            buildFromTemplate(template) {
                state.templates.push(template);
                return template;
            }
        },
        nativeImage: {
            createFromPath(value) {
                assert.match(value, /assets[\\/]tray-icon\.png$/);
                return { isEmpty: () => false };
            }
        },
        shell
    };
    const sandbox = {
        console: {
            warn() {},
            error: (...args) => state.errors.push(args)
        },
        module: { exports: {} },
        exports: {},
        process: { platform: 'darwin' },
        URL,
        URLSearchParams,
        __dirname: path.dirname(mainPath),
        require(request) {
            if (request === 'electron') return electron;
            if (request === 'path') return path;
            if (request === 'node:url') return { pathToFileURL };
            if (request === './backend-supervisor') {
                return {
                    createDesktopBackendSupervisor() {
                        return {
                            async start() { state.backendStarts += 1; },
                            stop() { state.backendStops += 1; }
                        };
                    }
                };
            }
            if (request === './desktop-startup') {
                return {
                    async reconcileDesktopStartup() {
                        state.startupReconciles += 1;
                    }
                };
            }
            if (request === './runtime-connection') {
                return {
                    async createRuntimeConnection({ isPackaged }) {
                        const port = isPackaged ? 49152 : 8080;
                        const accessToken = isPackaged ? ACCESS_TOKEN : null;
                        return {
                            host: '127.0.0.1',
                            port,
                            accessToken,
                            httpOrigin: `http://127.0.0.1:${port}`,
                            wsOrigin: `ws://127.0.0.1:${port}`
                        };
                    }
                };
            }
            throw new Error(`Unexpected require: ${request}`);
        }
    };

    vm.runInNewContext(source, sandbox, { filename: mainPath });
    await new Promise((resolve) => setImmediate(resolve));
    return { source, state };
}

test('development tray entry keeps the fixed local settings URL', async () => {
    const { state } = await loadMain();
    assert.equal(state.templates.length, 1);
    assert.equal(state.backendStarts, 1);
    assert.equal(state.startupReconciles, 1);

    const template = state.templates[0];
    const labels = Array.from(template)
        .filter((item) => item.label)
        .map((item) => item.label);
    assert.deepEqual(labels, ['显示', '隐藏', '设置', '退出']);

    const settingsItem = template.find((item) => item.label === '设置');
    assert.equal(typeof settingsItem.click, 'function');
    await settingsItem.click({ url: 'https://attacker.invalid' });
    assert.deepEqual(state.openExternalCalls, [[SETTINGS_URL]]);
});

test('packaged settings entry carries the ephemeral token only in the fragment', async () => {
    const { state } = await loadMain({ packaged: true });
    const settingsItem = state.templates[0].find((item) => item.label === '设置');

    await settingsItem.click();
    const opened = new URL(state.openExternalCalls[0][0]);
    assert.equal(opened.origin, 'http://127.0.0.1:49152');
    assert.equal(opened.pathname, '/settings');
    assert.equal(opened.search, '');
    assert.equal(new URLSearchParams(opened.hash.slice(1)).get('desktopToken'), ACCESS_TOKEN);
});

test('desktop lifecycle starts and stops the managed backend', async () => {
    const { state } = await loadMain();

    assert.equal(state.backendStarts, 1);
    assert.equal(typeof state.appEvents.get('before-quit'), 'function');
    state.appEvents.get('before-quit')();
    assert.equal(state.backendStops, 1);
});

test('settings entry handles browser launch rejection without quitting', async () => {
    const { state } = await loadMain({
        openExternal: async () => { throw new Error('secret URL and token'); }
    });
    const settingsItem = state.templates[0].find((item) => item.label === '设置');

    await assert.doesNotReject(settingsItem.click());
    assert.equal(state.quitCalls, 0);
    assert.equal(state.errors.length, 1);
    assert.doesNotMatch(state.errors.flat().join(' '), /secret|token|https?:\/\//i);
});

test('runtime IPC returns credentials only to the exact renderer entry', async () => {
    const { source, state } = await loadMain({ packaged: true });
    const handler = state.ipcHandlers.get('desktop-runtime:get');
    assert.equal(typeof handler, 'function');

    const denied = { senderFrame: { url: 'file:///tmp/attacker.html' } };
    handler(denied);
    assert.equal(denied.returnValue, null);

    const allowed = {
        senderFrame: {
            url: pathToFileURL(path.join(path.dirname(mainPath), 'renderer/index.html')).toString()
        }
    };
    handler(allowed);
    assert.equal(allowed.returnValue.accessToken, ACCESS_TOKEN);
    assert.equal(allowed.returnValue.port, 49152);
    assert.match(source, /senderUrl === allowedUrl/);
    assert.doesNotMatch(source, /BrowserWindow[^;]*settings|loadURL\([^)]*settings/i);
});
