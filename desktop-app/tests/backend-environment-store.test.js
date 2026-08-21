const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
    CONFIG_FILENAME,
    loadDesktopBackendEnvironment
} = require('../src/backend-environment-store');

const RELAY_ENVIRONMENT = Object.freeze({
    ASSISTANT_COMPUTER_DEVICE_ID: 'macbook-main',
    ASSISTANT_COMPUTER_RELAY_TARGET: 'relay@cloud.example',
    ASSISTANT_COMPUTER_RELAY_PORT: '22',
    ASSISTANT_COMPUTER_RELAY_IDENTITY_FILE: '/private/relay/identity_ed25519',
    ASSISTANT_COMPUTER_RELAY_KNOWN_HOSTS_FILE: '/private/relay/known_hosts'
});

function temporaryDirectory(t) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'vaa-backend-env-'));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    return directory;
}

function makeLogger() {
    const messages = [];
    return {
        messages,
        logger: {
            info: (message) => messages.push(['info', message]),
            warn: (message) => messages.push(['warn', message]),
            error: (message) => messages.push(['error', message])
        }
    };
}

test('persists only the complete relay configuration with private permissions', (t) => {
    const directory = temporaryDirectory(t);
    const environment = loadDesktopBackendEnvironment({
        directory,
        environment: {
            ...RELAY_ENVIRONMENT,
            ASSISTANT_LLM_API_KEY: 'must-not-be-persisted'
        },
        platform: 'darwin'
    });
    const configPath = path.join(directory, CONFIG_FILENAME);
    const source = fs.readFileSync(configPath, 'utf8');
    const stat = fs.statSync(configPath);

    assert.deepEqual(
        Object.fromEntries(
            Object.keys(RELAY_ENVIRONMENT).map((name) => [name, environment[name]])
        ),
        RELAY_ENVIRONMENT
    );
    assert.equal(stat.mode & 0o077, 0);
    assert.doesNotMatch(source, /must-not-be-persisted|API_KEY/);
});

test('loads the persisted relay configuration after the environment is gone', (t) => {
    const directory = temporaryDirectory(t);
    loadDesktopBackendEnvironment({
        directory,
        environment: RELAY_ENVIRONMENT,
        platform: 'darwin'
    });

    const restored = loadDesktopBackendEnvironment({
        directory,
        environment: { PATH: '/usr/bin' },
        platform: 'darwin'
    });

    for (const [name, value] of Object.entries(RELAY_ENVIRONMENT)) {
        assert.equal(restored[name], value);
    }
    assert.equal(restored.PATH, '/usr/bin');
});

test('explicit environment values override a stored relay field', (t) => {
    const directory = temporaryDirectory(t);
    loadDesktopBackendEnvironment({
        directory,
        environment: RELAY_ENVIRONMENT,
        platform: 'darwin'
    });

    const restored = loadDesktopBackendEnvironment({
        directory,
        environment: {
            ASSISTANT_COMPUTER_DEVICE_ID: 'replacement-mac'
        },
        platform: 'darwin'
    });

    assert.equal(restored.ASSISTANT_COMPUTER_DEVICE_ID, 'replacement-mac');
    assert.equal(
        restored.ASSISTANT_COMPUTER_RELAY_TARGET,
        RELAY_ENVIRONMENT.ASSISTANT_COMPUTER_RELAY_TARGET
    );
});

test('does not persist an incomplete or invalid relay configuration', (t) => {
    const directory = temporaryDirectory(t);
    const { logger, messages } = makeLogger();

    loadDesktopBackendEnvironment({
        directory,
        environment: {
            ASSISTANT_COMPUTER_DEVICE_ID: 'macbook-main',
            ASSISTANT_COMPUTER_RELAY_TARGET: 'invalid target'
        },
        platform: 'darwin',
        logger
    });

    assert.equal(fs.existsSync(path.join(directory, CONFIG_FILENAME)), false);
    assert.equal(messages.length, 0);
});

test('ignores a relay file that is readable by other users', (t) => {
    const directory = temporaryDirectory(t);
    const configPath = path.join(directory, CONFIG_FILENAME);
    fs.writeFileSync(configPath, JSON.stringify({
        version: 1,
        computerRelay: {
            deviceId: 'macbook-main',
            target: 'relay@cloud.example',
            port: 22,
            identityFile: '/private/relay/identity_ed25519',
            knownHostsFile: '/private/relay/known_hosts'
        }
    }), { mode: 0o644 });
    fs.chmodSync(configPath, 0o644);
    const { logger, messages } = makeLogger();

    const environment = loadDesktopBackendEnvironment({
        directory,
        environment: {},
        platform: 'darwin',
        logger
    });

    assert.equal(environment.ASSISTANT_COMPUTER_DEVICE_ID, undefined);
    assert.deepEqual(messages, [[
        'warn',
        '[Backend] Ignored unsafe persisted relay configuration.'
    ]]);
});

test('invalid persisted content is rejected without exposing its values', (t) => {
    const directory = temporaryDirectory(t);
    const configPath = path.join(directory, CONFIG_FILENAME);
    fs.writeFileSync(configPath, '{"secret":"must-not-appear"}', { mode: 0o600 });
    const { logger, messages } = makeLogger();

    const environment = loadDesktopBackendEnvironment({
        directory,
        environment: {},
        platform: 'darwin',
        logger
    });

    assert.equal(environment.ASSISTANT_COMPUTER_DEVICE_ID, undefined);
    assert.equal(messages.length, 1);
    assert.doesNotMatch(JSON.stringify(messages), /secret|must-not-appear/);
});

test('a failed atomic replacement preserves the previous configuration', (t) => {
    const directory = temporaryDirectory(t);
    loadDesktopBackendEnvironment({
        directory,
        environment: RELAY_ENVIRONMENT,
        platform: 'darwin'
    });
    const configPath = path.join(directory, CONFIG_FILENAME);
    const previousSource = fs.readFileSync(configPath, 'utf8');
    const { logger, messages } = makeLogger();
    const failingFileSystem = {
        ...fs,
        renameSync: () => {
            throw new Error('simulated replacement failure');
        }
    };

    loadDesktopBackendEnvironment({
        directory,
        environment: {
            ...RELAY_ENVIRONMENT,
            ASSISTANT_COMPUTER_DEVICE_ID: 'replacement-mac'
        },
        platform: 'darwin',
        logger,
        fsModule: failingFileSystem,
        processId: 1234,
        now: () => 5678
    });

    assert.equal(fs.readFileSync(configPath, 'utf8'), previousSource);
    assert.equal(
        fs.existsSync(`${configPath}.1234.5678.tmp`),
        false
    );
    assert.deepEqual(messages, [[
        'error',
        '[Backend] Unable to persist relay configuration.'
    ]]);
});
