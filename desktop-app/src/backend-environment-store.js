const fs = require('node:fs');
const path = require('node:path');

const CONFIG_FILENAME = 'backend-environment.json';
const MAX_CONFIG_BYTES = 8192;
const RELAY_ENVIRONMENT_NAMES = Object.freeze([
    'ASSISTANT_COMPUTER_DEVICE_ID',
    'ASSISTANT_COMPUTER_RELAY_TARGET',
    'ASSISTANT_COMPUTER_RELAY_PORT',
    'ASSISTANT_COMPUTER_RELAY_IDENTITY_FILE',
    'ASSISTANT_COMPUTER_RELAY_KNOWN_HOSTS_FILE'
]);
const DEVICE_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const TARGET_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/;

function isPlainObject(value) {
    return value !== null
        && typeof value === 'object'
        && !Array.isArray(value)
        && Object.getPrototypeOf(value) === Object.prototype;
}

function relayFromEnvironment(environment) {
    const values = RELAY_ENVIRONMENT_NAMES.map((name) => environment[name]);
    if (values.some((value) => typeof value !== 'string' || value === '')) {
        return null;
    }
    const [deviceId, target, portSource, identityFile, knownHostsFile] = values;
    const port = Number(portSource);
    if (
        !DEVICE_PATTERN.test(deviceId)
        || !TARGET_PATTERN.test(target)
        || !Number.isInteger(port)
        || port < 1
        || port > 65535
        || !path.isAbsolute(identityFile)
        || !path.isAbsolute(knownHostsFile)
    ) {
        return null;
    }
    return { deviceId, target, port, identityFile, knownHostsFile };
}

function environmentFromRelay(relay) {
    if (!isPlainObject(relay)) return null;
    const keys = Object.keys(relay).sort();
    const expected = [
        'deviceId',
        'identityFile',
        'knownHostsFile',
        'port',
        'target'
    ];
    if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
        return null;
    }
    if (
        typeof relay.deviceId !== 'string'
        || typeof relay.target !== 'string'
        || !Number.isInteger(relay.port)
        || typeof relay.identityFile !== 'string'
        || typeof relay.knownHostsFile !== 'string'
    ) {
        return null;
    }
    const environment = {
        ASSISTANT_COMPUTER_DEVICE_ID: relay.deviceId,
        ASSISTANT_COMPUTER_RELAY_TARGET: relay.target,
        ASSISTANT_COMPUTER_RELAY_PORT: String(relay.port),
        ASSISTANT_COMPUTER_RELAY_IDENTITY_FILE: relay.identityFile,
        ASSISTANT_COMPUTER_RELAY_KNOWN_HOSTS_FILE: relay.knownHostsFile
    };
    return relayFromEnvironment(environment) === null ? null : environment;
}

function readPersistedEnvironment({ fsModule, configPath, platform, logger }) {
    try {
        const stat = fsModule.lstatSync(configPath);
        if (
            !stat.isFile()
            || stat.size > MAX_CONFIG_BYTES
            || (platform !== 'win32' && (stat.mode & 0o077) !== 0)
        ) {
            logger.warn('[Backend] Ignored unsafe persisted relay configuration.');
            return {};
        }
        const document = JSON.parse(fsModule.readFileSync(configPath, 'utf8'));
        if (
            !isPlainObject(document)
            || document.version !== 1
            || Object.keys(document).length !== 2
            || !Object.prototype.hasOwnProperty.call(document, 'computerRelay')
        ) {
            throw new Error('invalid relay configuration');
        }
        const environment = environmentFromRelay(document.computerRelay);
        if (environment === null) throw new Error('invalid relay configuration');
        return environment;
    } catch (error) {
        if (error && error.code === 'ENOENT') return {};
        logger.warn('[Backend] Ignored invalid persisted relay configuration.');
        return {};
    }
}

function persistEnvironment({
    fsModule,
    directory,
    configPath,
    relay,
    logger,
    processId,
    now,
    platform
}) {
    fsModule.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const temporaryPath = `${configPath}.${processId}.${now()}.tmp`;
    let descriptor = null;
    try {
        descriptor = fsModule.openSync(temporaryPath, 'wx', 0o600);
        const payload = `${JSON.stringify({
            version: 1,
            computerRelay: relay
        }, null, 2)}\n`;
        fsModule.writeFileSync(descriptor, payload, 'utf8');
        fsModule.fsyncSync(descriptor);
        fsModule.closeSync(descriptor);
        descriptor = null;
        fsModule.renameSync(temporaryPath, configPath);
        if (platform !== 'win32') fsModule.chmodSync(configPath, 0o600);
    } catch {
        if (descriptor !== null) {
            try {
                fsModule.closeSync(descriptor);
            } catch {}
        }
        try {
            fsModule.unlinkSync(temporaryPath);
        } catch {}
        logger.error('[Backend] Unable to persist relay configuration.');
    }
}

function loadDesktopBackendEnvironment({
    directory,
    environment,
    platform = process.platform,
    logger = console,
    fsModule = fs,
    processId = process.pid,
    now = Date.now
}) {
    const configPath = path.join(directory, CONFIG_FILENAME);
    const persisted = readPersistedEnvironment({
        fsModule,
        configPath,
        platform,
        logger
    });
    const merged = { ...persisted, ...environment };
    const relay = relayFromEnvironment(merged);
    const explicitRelay = relayFromEnvironment(environment);
    if (relay !== null && explicitRelay !== null) {
        persistEnvironment({
            fsModule,
            directory,
            configPath,
            relay,
            logger,
            processId,
            now,
            platform
        });
    }
    return merged;
}

module.exports = {
    CONFIG_FILENAME,
    RELAY_ENVIRONMENT_NAMES,
    loadDesktopBackendEnvironment
};
