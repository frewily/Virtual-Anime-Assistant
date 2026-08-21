const { createSpeechPlayback } = require('./speech-playback');
const { getRuntimeConnection } = require('./runtime-connection');
const {
    enqueueConfirmation,
    handleConfirmationUpdate,
    restorePendingConfirmations
} = require('./tool-confirmation');

const runtime = getRuntimeConnection();
const speechPlayback = createSpeechPlayback({
    backendUrl: runtime.httpOrigin,
    accessToken: runtime.accessToken
});

let ws;

function connectWebSocket() {
    const url = `${runtime.wsOrigin}/ws/avatar`;
    const protocols = runtime.accessToken
        ? [`vaa.desktop.${runtime.accessToken}`]
        : undefined;
    ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url);
    
    ws.onopen = () => {
        console.log('WebSocket connected');
        restorePendingConfirmations();
    };
    
    ws.onmessage = (event) => {
        const message = JSON.parse(event.data);
        handleMessage(message);
    };
    
    ws.onclose = () => {
        console.log('WebSocket disconnected, reconnecting...');
        setTimeout(connectWebSocket, 3000);
    };
    
    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

function handleMessage(message) {
    switch (message.type) {
        case 'speak':
            handleSpeak(message);
            break;
        case 'action':
            handleAction(message);
            break;
        case 'status':
            handleStatus(message);
            break;
        case 'tool_confirmation_required':
            enqueueConfirmation(message.confirmation);
            break;
        case 'tool_confirmation_updated':
            handleConfirmationUpdate(message);
            break;
        default:
            console.log('Unknown message type:', message.type);
    }
}

function handleSpeak(message) {
    void speechPlayback.handleSpeakAudio(message);
    
    if (message.motion) {
        window.playMotion && window.playMotion(message.motion);
    }
    
    if (message.expression) {
        window.setExpression && window.setExpression(message.expression);
    }
}

function handleAction(message) {
    if (message.expression) {
        window.setExpression && window.setExpression(message.expression);
    }
    
    if (message.motion) {
        window.playMotion && window.playMotion(message.motion);
    }
}

function handleStatus(message) {
    console.log('Status update:', message);
}

function sendToBackend(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
    }
}

window.addEventListener('DOMContentLoaded', connectWebSocket);

module.exports = {
    sendToBackend
};
