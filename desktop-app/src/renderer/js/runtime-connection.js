const FALLBACK_RUNTIME = Object.freeze({
    host: '127.0.0.1',
    port: 8080,
    accessToken: null,
    httpOrigin: 'http://127.0.0.1:8080',
    wsOrigin: 'ws://127.0.0.1:8080'
});

function getRuntimeConnection() {
    const candidate = typeof window === 'object'
        ? window.desktopAssistant?.runtime
        : null;
    if (!candidate) return FALLBACK_RUNTIME;
    const port = candidate.port;
    const token = candidate.accessToken;
    if (
        candidate.host !== '127.0.0.1'
        || !Number.isInteger(port)
        || port < 1
        || port > 65535
        || (token !== null && !/^[A-Za-z0-9_-]{43}$/.test(token))
        || candidate.httpOrigin !== `http://127.0.0.1:${port}`
        || candidate.wsOrigin !== `ws://127.0.0.1:${port}`
    ) {
        throw new Error('invalid desktop runtime connection');
    }
    return Object.freeze({ ...candidate });
}

function withDesktopAccessHeader(headers = {}) {
    const runtime = getRuntimeConnection();
    return runtime.accessToken
        ? { ...headers, 'X-VAA-Desktop-Token': runtime.accessToken }
        : { ...headers };
}

module.exports = {
    FALLBACK_RUNTIME,
    getRuntimeConnection,
    withDesktopAccessHeader
};
