const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const mainPath = path.resolve(__dirname, '..', 'src', 'main.js');
const SETTINGS_URL = 'http://127.0.0.1:8080/settings';

async function loadMain({ openExternal = async () => {} } = {}) {
    const source = fs.readFileSync(mainPath, 'utf8');
    const state = {
        openExternalCalls: [],
        backendStarts: 0,
        backendStops: 0,
        quitCalls: 0,
        templates: [],
        appEvents: new Map(),
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
            whenReady: () => Promise.resolve(),
            on(event, callback) { state.appEvents.set(event, callback); },
            quit: () => { state.quitCalls += 1; }
        },
        BrowserWindow,
        Tray,
        Menu: {
            buildFromTemplate(template) {
                state.templates.push(template);
                return template;
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
        __dirname: path.dirname(mainPath),
        require(request) {
            if (request === 'electron') return electron;
            if (request === 'fs') return { existsSync: () => true };
            if (request === 'path') return path;
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
            throw new Error(`Unexpected require: ${request}`);
        }
    };

    vm.runInNewContext(source, sandbox, { filename: mainPath });
    await new Promise((resolve) => setImmediate(resolve));
    return { source, state };
}

test('tray settings entry opens only the fixed local settings URL', async () => {
    const { state } = await loadMain();
    assert.equal(state.templates.length, 1);
    assert.equal(state.backendStarts, 1);

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

test('main process exposes no dynamic settings URL or credential IPC path', async () => {
    const { source } = await loadMain();
    assert.match(source, /const SETTINGS_URL\s*=\s*['"]http:\/\/127\.0\.0\.1:8080\/settings['"]/);
    assert.match(source, /shell\.openExternal\(SETTINGS_URL\)/);
    assert.doesNotMatch(source, /ipcMain|ipcRenderer/);
    assert.doesNotMatch(source, /BrowserWindow[^;]*settings|loadURL\([^)]*settings/i);
});
