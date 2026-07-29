const API_BASE = 'http://127.0.0.1:8080/api';

async function requestJson(path, options = {}) {
    const response = await fetch(`${API_BASE}${path}`, options);
    const payload = response.status === 204
        ? null
        : await response.json();
    if (!response.ok) {
        throw new Error('request_failed');
    }
    return payload;
}

async function getStatus() {
    return requestJson('/status');
}

async function sendChatMessage(message) {
    return requestJson('/chat/message', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(message)
    });
}

async function listToolConfirmations() {
    return requestJson('/tools/confirmations');
}

async function decideToolConfirmation(id, decision) {
    return requestJson(
        `/tools/confirmations/${encodeURIComponent(id)}/decision`,
        {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ decision })
        }
    );
}

module.exports = {
    getStatus,
    sendChatMessage,
    listToolConfirmations,
    decideToolConfirmation
};
