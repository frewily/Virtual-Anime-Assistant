const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('desktopAssistant', {
    platform: process.platform
});
