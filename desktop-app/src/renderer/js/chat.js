document.addEventListener('click', (event) => {
    const message = {
        type: 'interaction',
        action: 'click',
        x: event.clientX,
        y: event.clientY
    };
    
    const { sendToBackend } = require('./websocket');
    sendToBackend(message);
});

function startChat() {
    console.log('Chat started');
}

function endChat() {
    console.log('Chat ended');
}

module.exports = {
    startChat,
    endChat
};
