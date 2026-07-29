require('./live2d');
require('./websocket');
require('./actions');
require('./chat');
const {
    initializeToolConfirmations
} = require('./tool-confirmation');

window.addEventListener(
    'DOMContentLoaded',
    initializeToolConfirmations
);
