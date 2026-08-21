const assert = require('node:assert/strict');
const test = require('node:test');

const {
    getRuntimeConnection,
    withDesktopAccessHeader
} = require('../src/renderer/js/runtime-connection');

test('renderer accepts only an exact loopback runtime descriptor', () => {
    const previous = global.window;
    global.window = {
        desktopAssistant: {
            runtime: {
                host: '127.0.0.1',
                port: 49152,
                accessToken: 'a'.repeat(43),
                httpOrigin: 'http://127.0.0.1:49152',
                wsOrigin: 'ws://127.0.0.1:49152'
            }
        }
    };
    try {
        assert.equal(getRuntimeConnection().port, 49152);
        assert.equal(
            withDesktopAccessHeader({ Accept: 'application/json' })['X-VAA-Desktop-Token'],
            'a'.repeat(43)
        );
        global.window.desktopAssistant.runtime.httpOrigin = 'https://attacker.invalid';
        assert.throws(() => getRuntimeConnection(), /invalid/);
    } finally {
        global.window = previous;
    }
});
