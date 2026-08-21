const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const test = require('node:test');

const {
    allocateLoopbackPort,
    buildConnection,
    createRuntimeConnection
} = require('../src/runtime-connection');

test('development runtime preserves the fixed local endpoint without a token', async () => {
    const connection = await createRuntimeConnection({ isPackaged: false });
    assert.deepEqual(connection, {
        host: '127.0.0.1',
        port: 8080,
        accessToken: null,
        httpOrigin: 'http://127.0.0.1:8080',
        wsOrigin: 'ws://127.0.0.1:8080'
    });
});

test('packaged runtime uses an allocated port and a 256-bit token', async () => {
    const connection = await createRuntimeConnection({
        isPackaged: true,
        allocatePort: async () => 49152,
        randomBytes(size) {
            assert.equal(size, 32);
            return Buffer.alloc(size, 7);
        }
    });
    assert.equal(connection.port, 49152);
    assert.match(connection.accessToken, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(connection.httpOrigin, 'http://127.0.0.1:49152');
    assert.ok(Object.isFrozen(connection));
});

test('loopback allocator closes the reservation before returning the port', async () => {
    const server = new EventEmitter();
    let closed = false;
    server.listen = (options, callback) => {
        assert.deepEqual(options, {
            host: '127.0.0.1',
            port: 0,
            exclusive: true
        });
        callback();
    };
    server.address = () => ({ address: '127.0.0.1', family: 'IPv4', port: 54321 });
    server.close = (callback) => {
        closed = true;
        callback();
    };

    assert.equal(await allocateLoopbackPort({ createServer: () => server }), 54321);
    assert.equal(closed, true);
});

test('runtime connection rejects invalid ports and tokens', () => {
    assert.throws(() => buildConnection(0, null), /port/);
    assert.throws(() => buildConnection(8080, 'short'), /token/);
});
