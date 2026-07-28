const WS_URL = 'ws://127.0.0.1:8080/ws/avatar';
const BACKEND_URL = 'http://127.0.0.1:8080';
const {
    enqueueConfirmation,
    handleConfirmationUpdate,
    restorePendingConfirmations
} = require('./tool-confirmation');

let ws;

function connectWebSocket() {
    ws = new WebSocket(WS_URL);
    
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
    if (message.audioUrl) {
        const audio = new Audio(new URL(message.audioUrl, BACKEND_URL));
        audio.play();
    }
    
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
