const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');

const {
    readDesktopStartupPreference,
    reconcileDesktopStartup
} = require('../src/desktop-startup');

function responseRequest({ statusCode = 200, body = '' }) {
    return (options, callback) => {
        const request = new EventEmitter();
        request.destroy = () => {};
        queueMicrotask(() => {
            const response = new EventEmitter();
            response.statusCode = statusCode;
            response.resume = () => {};
            response.setEncoding = () => {};
            callback(response);
            if (statusCode === 200) {
                response.emit('data', body);
                response.emit('end');
            }
        });
        assert.deepEqual(options, {
            host: '127.0.0.1',
            port: 8080,
            path: '/api/status/desktop-startup',
            method: 'GET',
            timeout: 1000
        });
        return request;
    };
}

test('reads only the fixed local boolean startup response', async () => {
    assert.equal(await readDesktopStartupPreference({
        request: responseRequest({ body: '{"openAtLogin":true}' })
    }), true);
    assert.equal(await readDesktopStartupPreference({
        request: responseRequest({
            body: '{"openAtLogin":true,"unexpected":"value"}'
        })
    }), null);
    assert.equal(await readDesktopStartupPreference({
        request: responseRequest({ body: '{"openAtLogin":"true"}' })
    }), null);
});

test('applies an opt-in preference only to a supported packaged app', async () => {
    const calls = [];
    const app = {
        isPackaged: true,
        getLoginItemSettings: () => ({ openAtLogin: false }),
        setLoginItemSettings: (settings) => calls.push(settings)
    };

    assert.equal(await reconcileDesktopStartup({
        app,
        platform: 'darwin',
        readPreference: async () => true,
        logger: { info() {} }
    }), true);
    assert.deepEqual(calls, [{ openAtLogin: true }]);

    app.getLoginItemSettings = () => ({ openAtLogin: true });
    assert.equal(await reconcileDesktopStartup({
        app,
        platform: 'darwin',
        readPreference: async () => false,
        logger: { info() {} }
    }), true);
    assert.deepEqual(calls, [
        { openAtLogin: true },
        { openAtLogin: false }
    ]);
});

test('leaves login items untouched for development and unsupported systems', async () => {
    let reads = 0;
    let writes = 0;
    const readPreference = async () => { reads += 1; return true; };
    const app = {
        isPackaged: false,
        getLoginItemSettings: () => ({ openAtLogin: false }),
        setLoginItemSettings: () => { writes += 1; }
    };

    assert.equal(await reconcileDesktopStartup({
        app,
        platform: 'darwin',
        readPreference
    }), false);
    app.isPackaged = true;
    assert.equal(await reconcileDesktopStartup({
        app,
        platform: 'linux',
        readPreference
    }), false);
    assert.equal(reads, 0);
    assert.equal(writes, 0);
});

test('does not mutate login items when the preference cannot be verified', async () => {
    const messages = [];
    const app = {
        isPackaged: true,
        getLoginItemSettings: () => ({ openAtLogin: true }),
        setLoginItemSettings: () => { throw new Error('must not run'); }
    };

    assert.equal(await reconcileDesktopStartup({
        app,
        platform: 'win32',
        readPreference: async () => null,
        logger: { warn: (message) => messages.push(message) }
    }), false);
    assert.deepEqual(messages, [
        '[Startup] Unable to read the saved login-start preference.'
    ]);
});
