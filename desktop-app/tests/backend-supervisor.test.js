const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const path = require('node:path');
const test = require('node:test');

const {
    BackendSupervisor,
    createBackendEnvironment,
    resolveBackendLaunch
} = require('../src/backend-supervisor');

class FakeChild extends EventEmitter {
    constructor() {
        super();
        this.exitCode = null;
        this.kills = [];
    }

    kill(signal) {
        this.kills.push(signal);
        return true;
    }
}

function makeSupervisor({ probes = [false, true], child = new FakeChild() } = {}) {
    const spawnCalls = [];
    const timers = [];
    const logs = [];
    let probeIndex = 0;
    const supervisor = new BackendSupervisor({
        launch: {
            command: '/safe/backend',
            args: [],
            cwd: '/safe'
        },
        environment: { PATH: '/usr/bin' },
        spawnProcess(command, args, options) {
            spawnCalls.push({ command, args, options });
            return child;
        },
        async probe() {
            const value = probes[Math.min(probeIndex, probes.length - 1)];
            probeIndex += 1;
            return value;
        },
        async delay() {},
        schedule(callback, milliseconds) {
            const timer = { callback, milliseconds, cancelled: false };
            timers.push(timer);
            return timer;
        },
        cancel(timer) {
            timer.cancelled = true;
        },
        logger: {
            info: (message) => logs.push(['info', message]),
            warn: (message) => logs.push(['warn', message]),
            error: (message) => logs.push(['error', message])
        }
    });
    return { supervisor, spawnCalls, timers, logs, child };
}

test('resolves fixed development and packaged backend launches', () => {
    const development = resolveBackendLaunch({
        appPath: '/repo/desktop-app',
        isPackaged: false,
        resourcesPath: '/unused',
        platform: 'darwin',
        environment: {}
    });
    assert.deepEqual(development, {
        command: 'python3',
        args: [path.resolve('/repo/backend/main.py')],
        cwd: path.resolve('/repo/backend')
    });

    const packaged = resolveBackendLaunch({
        appPath: '/Applications/Desktop Assistant.app/Contents/Resources/app.asar',
        isPackaged: true,
        resourcesPath: '/Applications/Desktop Assistant.app/Contents/Resources',
        platform: 'darwin',
        environment: {}
    });
    assert.deepEqual(packaged, {
        command: path.resolve(
            '/Applications/Desktop Assistant.app/Contents/Resources/backend/vaa-backend'
        ),
        args: [],
        cwd: path.resolve(
            '/Applications/Desktop Assistant.app/Contents/Resources/backend'
        )
    });
});

test('accepts only an absolute backend executable override', () => {
    assert.throws(
        () => resolveBackendLaunch({
            appPath: '/repo/desktop-app',
            isPackaged: false,
            resourcesPath: '/unused',
            platform: 'darwin',
            environment: { ASSISTANT_BACKEND_EXECUTABLE: 'python -m backend' }
        }),
        /absolute/
    );
    assert.equal(
        resolveBackendLaunch({
            appPath: '/repo/desktop-app',
            isPackaged: false,
            resourcesPath: '/unused',
            platform: 'darwin',
            environment: { ASSISTANT_BACKEND_EXECUTABLE: '/opt/vaa/backend' }
        }).command,
        '/opt/vaa/backend'
    );
});

test('forces the managed backend to the fixed loopback endpoint', () => {
    const environment = createBackendEnvironment({
        ASSISTANT_HOST: '0.0.0.0',
        ASSISTANT_PORT: '9000',
        ASSISTANT_COMPUTER_DEVICE_ID: 'macbook-main'
    });
    assert.equal(environment.ASSISTANT_HOST, '127.0.0.1');
    assert.equal(environment.ASSISTANT_PORT, '8080');
    assert.equal(environment.ASSISTANT_COMPUTER_DEVICE_ID, 'macbook-main');
});

test('attaches to an already healthy backend without spawning or owning it', async () => {
    const { supervisor, spawnCalls } = makeSupervisor({ probes: [true] });

    assert.equal(await supervisor.start(), true);
    assert.equal(supervisor.running, true);
    assert.equal(supervisor.ownsProcess, false);
    assert.deepEqual(spawnCalls, []);
    supervisor.stop();
    assert.equal(supervisor.running, false);
});

test('spawns without a shell and becomes ready after the health probe succeeds', async () => {
    const { supervisor, spawnCalls } = makeSupervisor();

    assert.equal(await supervisor.start(), true);
    assert.equal(supervisor.ownsProcess, true);
    assert.equal(spawnCalls.length, 1);
    assert.deepEqual(spawnCalls[0], {
        command: '/safe/backend',
        args: [],
        options: {
            cwd: '/safe',
            env: { PATH: '/usr/bin' },
            shell: false,
            stdio: 'ignore',
            windowsHide: true
        }
    });
});

test('readiness timeout terminates a hung backend with a forced-kill fallback', async () => {
    const { supervisor, timers, child } = makeSupervisor({ probes: [false] });

    assert.equal(await supervisor.start(), false);
    assert.deepEqual(child.kills, ['SIGTERM']);
    assert.equal(timers.length, 1);
    assert.equal(timers[0].milliseconds, 5000);
    timers[0].callback();
    assert.deepEqual(child.kills, ['SIGTERM', 'SIGKILL']);
});

test('unexpected exit schedules one bounded restart', async () => {
    const { supervisor, timers, child } = makeSupervisor();
    await supervisor.start();

    child.exitCode = 1;
    child.emit('exit', 1, null);

    assert.equal(supervisor.running, false);
    assert.equal(timers.length, 1);
    assert.equal(timers[0].milliseconds, 1000);
});

test('stop terminates only an owned child and cancels pending restart', async () => {
    const { supervisor, timers, child } = makeSupervisor();
    await supervisor.start();
    child.exitCode = 1;
    child.emit('exit', 1, null);

    supervisor.stop();

    assert.equal(timers[0].cancelled, true);
    assert.deepEqual(child.kills, []);
});

test('stop sends SIGTERM to a live owned child and never schedules restart', async () => {
    const { supervisor, timers, child } = makeSupervisor();
    await supervisor.start();

    supervisor.stop();

    assert.deepEqual(child.kills, ['SIGTERM']);
    assert.equal(timers.length, 1);
    assert.equal(timers[0].milliseconds, 5000);
    timers[0].callback();
    assert.deepEqual(child.kills, ['SIGTERM', 'SIGKILL']);
    child.emit('exit', null, 'SIGKILL');
    assert.equal(supervisor.running, false);
});

test('clean child exit cancels the forced termination timer', async () => {
    const { supervisor, timers, child } = makeSupervisor();
    await supervisor.start();

    supervisor.stop();
    child.exitCode = 0;
    child.emit('exit', 0, null);

    assert.equal(timers.length, 1);
    assert.equal(timers[0].cancelled, true);
    assert.deepEqual(child.kills, ['SIGTERM']);
});

test('repeated stop calls remain idempotent', async () => {
    const { supervisor, timers, child } = makeSupervisor();
    await supervisor.start();

    supervisor.stop();
    supervisor.stop();

    assert.deepEqual(child.kills, ['SIGTERM']);
    assert.equal(timers.length, 1);
});
