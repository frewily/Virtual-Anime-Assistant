const http = require('node:http');

const STATUS_HOST = '127.0.0.1';
const STATUS_PORT = 8080;
const STATUS_PATH = '/api/status/desktop-startup';
const MAX_RESPONSE_BYTES = 1024;
const REQUEST_TIMEOUT_MS = 1000;
const SUPPORTED_PLATFORMS = new Set(['darwin', 'win32']);

function readDesktopStartupPreference({
    request = http.get,
    timeoutMilliseconds = REQUEST_TIMEOUT_MS
} = {}) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            resolve(value);
        };
        const requestInstance = request({
            host: STATUS_HOST,
            port: STATUS_PORT,
            path: STATUS_PATH,
            method: 'GET',
            timeout: timeoutMilliseconds
        }, (response) => {
            if (response.statusCode !== 200) {
                response.resume();
                finish(null);
                return;
            }
            response.setEncoding('utf8');
            let source = '';
            response.on('data', (chunk) => {
                source += chunk;
                if (Buffer.byteLength(source, 'utf8') > MAX_RESPONSE_BYTES) {
                    requestInstance.destroy();
                    finish(null);
                }
            });
            response.once('end', () => {
                if (settled) return;
                try {
                    const payload = JSON.parse(source);
                    const keys = payload && typeof payload === 'object'
                        && !Array.isArray(payload)
                        ? Object.keys(payload)
                        : [];
                    finish(
                        keys.length === 1
                        && keys[0] === 'openAtLogin'
                        && typeof payload.openAtLogin === 'boolean'
                            ? payload.openAtLogin
                            : null
                    );
                } catch {
                    finish(null);
                }
            });
            response.once('error', () => finish(null));
        });
        requestInstance.once('error', () => finish(null));
        requestInstance.once('timeout', () => {
            requestInstance.destroy();
            finish(null);
        });
    });
}

async function reconcileDesktopStartup({
    app,
    platform = process.platform,
    readPreference = readDesktopStartupPreference,
    logger = console
}) {
    if (!app.isPackaged || !SUPPORTED_PLATFORMS.has(platform)) return false;

    const openAtLogin = await readPreference();
    if (typeof openAtLogin !== 'boolean') {
        logger.warn('[Startup] Unable to read the saved login-start preference.');
        return false;
    }

    try {
        const current = app.getLoginItemSettings();
        if (!current || current.openAtLogin !== openAtLogin) {
            app.setLoginItemSettings({ openAtLogin });
        }
        logger.info('[Startup] Login-start preference is applied.');
        return true;
    } catch {
        logger.error('[Startup] Unable to apply the login-start preference.');
        return false;
    }
}

module.exports = {
    readDesktopStartupPreference,
    reconcileDesktopStartup
};
