const API_BASE = 'http://127.0.0.1:8080/api';

async function getStatus() {
    const response = await fetch(`${API_BASE}/status`);
    return response.json();
}

async function sendChatMessage(message) {
    const response = await fetch(`${API_BASE}/chat/message`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(message)
    });
    return response.json();
}

module.exports = {
    getStatus,
    sendChatMessage
};
