const crypto = require('node:crypto');
const net = require('node:net');

const LOOPBACK_HOST = '127.0.0.1';
const DEVELOPMENT_PORT = 8080;

function allocateLoopbackPort({ createServer = net.createServer } = {}) {
    return new Promise((resolve, reject) => {
        const server = createServer();
        let settled = false;
        const finish = (error, port) => {
            if (settled) return;
            settled = true;
            if (error) reject(error);
            else resolve(port);
        };
        server.once('error', (error) => finish(error));
        server.listen({ host: LOOPBACK_HOST, port: 0, exclusive: true }, () => {
            const address = server.address();
            if (!address || typeof address === 'string') {
                server.close(() => finish(new Error('unable to allocate backend port')));
                return;
            }
            server.close((error) => finish(error, address.port));
        });
    });
}

function buildConnection(port, accessToken) {
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error('backend port is invalid');
    }
    if (accessToken !== null && !/^[A-Za-z0-9_-]{43}$/.test(accessToken)) {
        throw new Error('desktop access token is invalid');
    }
    return Object.freeze({
        host: LOOPBACK_HOST,
        port,
        accessToken,
        httpOrigin: `http://${LOOPBACK_HOST}:${port}`,
        wsOrigin: `ws://${LOOPBACK_HOST}:${port}`
    });
}

async function createRuntimeConnection({
    isPackaged,
    allocatePort = allocateLoopbackPort,
    randomBytes = crypto.randomBytes
}) {
    if (!isPackaged) return buildConnection(DEVELOPMENT_PORT, null);
    const port = await allocatePort();
    const accessToken = randomBytes(32).toString('base64url');
    return buildConnection(port, accessToken);
}

module.exports = {
    DEVELOPMENT_PORT,
    LOOPBACK_HOST,
    allocateLoopbackPort,
    buildConnection,
    createRuntimeConnection
};
