const { spawn } = require('node:child_process');
const http = require('node:http');
const path = require('node:path');
const { loadDesktopBackendEnvironment } = require('./backend-environment-store');
const { buildConnection, DEVELOPMENT_PORT } = require('./runtime-connection');

const BACKEND_HOST = '127.0.0.1';
const HEALTH_PATH = '/api/health/live';
const READY_ATTEMPTS = 40;
const PACKAGED_READY_ATTEMPTS = 120;
const READY_DELAY_MS = 250;
const MAX_RESTARTS = 5;
const MAX_RESTART_DELAY_MS = 30_000;
const TERMINATION_TIMEOUT_MS = 5000;

function resolveAbsoluteOverride(value, name) {
    if (typeof value !== 'string' || value.trim() === '') {
        return null;
    }
    const candidate = value.trim();
    if (!path.isAbsolute(candidate)) {
        throw new Error(`${name} must be an absolute path`);
    }
    return path.normalize(candidate);
}

function resolveBackendLaunch({
    appPath,
    isPackaged,
    resourcesPath,
    platform,
    environment
}) {
    const executableOverride = resolveAbsoluteOverride(
        environment.ASSISTANT_BACKEND_EXECUTABLE,
        'backend executable'
    );
    if (executableOverride !== null) {
        return {
            command: executableOverride,
            args: [],
            cwd: path.dirname(executableOverride)
        };
    }

    if (isPackaged) {
        const executableName = platform === 'win32'
            ? 'vaa-backend.exe'
            : 'vaa-backend';
        const command = path.resolve(resourcesPath, 'backend', executableName);
        return {
            command,
            args: [],
            cwd: path.dirname(command)
        };
    }

    const pythonOverride = resolveAbsoluteOverride(
        environment.ASSISTANT_PYTHON_EXECUTABLE,
        'Python executable'
    );
    const command = pythonOverride || (platform === 'win32' ? 'python' : 'python3');
    const backendEntry = path.resolve(appPath, '..', 'backend', 'main.py');
    return {
        command,
        args: [backendEntry],
        cwd: path.dirname(backendEntry)
    };
}

function createBackendEnvironment(environment, {
    isPackaged = false,
    resourcesPath = '',
    userDataPath = '',
    connection = buildConnection(DEVELOPMENT_PORT, null)
} = {}) {
    const childEnvironment = {
        ...environment,
        ASSISTANT_HOST: BACKEND_HOST,
        ASSISTANT_PORT: String(connection.port)
    };
    delete childEnvironment.ASSISTANT_BACKEND_EXECUTABLE;
    delete childEnvironment.ASSISTANT_PYTHON_EXECUTABLE;
    if (isPackaged) {
        if (!path.isAbsolute(resourcesPath) || !path.isAbsolute(userDataPath)) {
            throw new Error('packaged backend paths must be absolute');
        }
        if (!connection.accessToken) {
            throw new Error('packaged backend requires a desktop access token');
        }
        childEnvironment.ASSISTANT_BUNDLED_CONFIG_DIR = path.join(
            resourcesPath,
            'config'
        );
        childEnvironment.ASSISTANT_AUDIO_DIR = path.join(userDataPath, 'audio');
        childEnvironment.ASSISTANT_CONFIG_DIR = path.join(userDataPath, 'config');
        childEnvironment.ASSISTANT_DATA_DIR = path.join(userDataPath, 'data');
        childEnvironment.ASSISTANT_DESKTOP_ACCESS_TOKEN = connection.accessToken;
    } else {
        delete childEnvironment.ASSISTANT_DESKTOP_ACCESS_TOKEN;
    }
    return childEnvironment;
}

function probeBackend({
    request = http.get,
    timeoutMilliseconds = 1000,
    connection = buildConnection(DEVELOPMENT_PORT, null)
} = {}) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            resolve(value);
        };
        const requestInstance = request({
            host: BACKEND_HOST,
            port: connection.port,
            path: HEALTH_PATH,
            method: 'GET',
            headers: connection.accessToken
                ? { 'X-VAA-Desktop-Token': connection.accessToken }
                : {},
            timeout: timeoutMilliseconds
        }, (response) => {
            response.resume();
            finish(response.statusCode === 200);
        });
        requestInstance.once('error', () => finish(false));
        requestInstance.once('timeout', () => {
            requestInstance.destroy();
            finish(false);
        });
    });
}

class BackendSupervisor {
    constructor({
        launch,
        environment,
        spawnProcess = spawn,
        probe = probeBackend,
        delay = (milliseconds) => new Promise(
            (resolve) => setTimeout(resolve, milliseconds)
        ),
        schedule = setTimeout,
        cancel = clearTimeout,
        readyAttempts = READY_ATTEMPTS,
        logger = console
    }) {
        if (!Number.isInteger(readyAttempts) || readyAttempts < 1) {
            throw new Error('backend readiness attempts must be a positive integer');
        }
        this.launch = launch;
        this.environment = environment;
        this.spawnProcess = spawnProcess;
        this.probe = probe;
        this.delay = delay;
        this.schedule = schedule;
        this.cancel = cancel;
        this.readyAttempts = readyAttempts;
        this.logger = logger;
        this.child = null;
        this.externalBackend = false;
        this.stopping = false;
        this.restartTimer = null;
        this.terminationTimer = null;
        this.restartAttempts = 0;
        this.startPromise = null;
    }

    get running() {
        return !this.stopping && (this.externalBackend || this.child !== null);
    }

    get ownsProcess() {
        return this.child !== null;
    }

    async start() {
        if (this.running) return true;
        if (this.startPromise !== null) return this.startPromise;
        this.stopping = false;
        this.startPromise = this._start();
        try {
            return await this.startPromise;
        } finally {
            this.startPromise = null;
        }
    }

    async _start() {
        if (await this.probe()) {
            this.externalBackend = true;
            this.logger.info('[Backend] Connected to existing local service.');
            return true;
        }

        let child;
        try {
            child = this.spawnProcess(
                this.launch.command,
                this.launch.args,
                {
                    cwd: this.launch.cwd,
                    env: this.environment,
                    shell: false,
                    stdio: 'ignore',
                    windowsHide: true
                }
            );
        } catch {
            this.logger.error('[Backend] Unable to start local service.');
            this._scheduleRestart();
            return false;
        }

        this.child = child;
        this.externalBackend = false;
        child.once('error', () => this._handleChildExit(child));
        child.once('exit', () => this._handleChildExit(child));

        for (let attempt = 0; attempt < this.readyAttempts; attempt += 1) {
            if (this.stopping || this.child !== child) return false;
            if (await this.probe()) {
                this.logger.info('[Backend] Local service is ready.');
                return true;
            }
            await this.delay(READY_DELAY_MS);
        }

        this.logger.error('[Backend] Local service did not become ready.');
        this._terminateChild(child);
        return false;
    }

    _handleChildExit(child) {
        if (this.child !== child) return;
        this.child = null;
        if (this.terminationTimer !== null) {
            this.cancel(this.terminationTimer);
            this.terminationTimer = null;
        }
        if (this.stopping) return;
        this.logger.warn('[Backend] Local service stopped unexpectedly.');
        this._scheduleRestart();
    }

    _scheduleRestart() {
        if (this.stopping || this.restartTimer !== null) return;
        if (this.restartAttempts >= MAX_RESTARTS) {
            this.logger.error('[Backend] Automatic restart limit reached.');
            return;
        }
        const milliseconds = Math.min(
            2 ** this.restartAttempts * 1000,
            MAX_RESTART_DELAY_MS
        );
        this.restartAttempts += 1;
        this.restartTimer = this.schedule(async () => {
            this.restartTimer = null;
            if (!this.stopping) await this.start();
        }, milliseconds);
    }

    _terminateChild(child) {
        if (this.child !== child || child.exitCode !== null) return;
        child.kill('SIGTERM');
        if (this.terminationTimer !== null) return;
        this.terminationTimer = this.schedule(() => {
            this.terminationTimer = null;
            if (this.child === child && child.exitCode === null) {
                child.kill('SIGKILL');
            }
        }, TERMINATION_TIMEOUT_MS);
    }

    stop() {
        if (this.stopping) return;
        this.stopping = true;
        this.externalBackend = false;
        if (this.restartTimer !== null) {
            this.cancel(this.restartTimer);
            this.restartTimer = null;
        }
        const child = this.child;
        if (child !== null) this._terminateChild(child);
    }
}

function createDesktopBackendSupervisor({
    app,
    connection = buildConnection(DEVELOPMENT_PORT, null),
    environment = process.env,
    platform = process.platform,
    resourcesPath = process.resourcesPath,
    logger = console
}) {
    const managedEnvironment = loadDesktopBackendEnvironment({
        directory: app.getPath('userData'),
        environment,
        platform,
        logger
    });
    const launch = resolveBackendLaunch({
        appPath: app.getAppPath(),
        isPackaged: app.isPackaged,
        resourcesPath,
        platform,
        environment: managedEnvironment
    });
    return new BackendSupervisor({
        launch,
        environment: createBackendEnvironment(managedEnvironment, {
            isPackaged: app.isPackaged,
            resourcesPath,
            userDataPath: app.getPath('userData'),
            connection
        }),
        probe: () => probeBackend({ connection }),
        readyAttempts: app.isPackaged
            ? PACKAGED_READY_ATTEMPTS
            : READY_ATTEMPTS,
        logger
    });
}

module.exports = {
    BackendSupervisor,
    createBackendEnvironment,
    createDesktopBackendSupervisor,
    probeBackend,
    resolveBackendLaunch
};
