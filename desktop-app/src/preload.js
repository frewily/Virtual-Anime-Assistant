const { contextBridge, ipcRenderer } = require('electron');

const runtime = ipcRenderer.sendSync('desktop-runtime:get');

contextBridge.exposeInMainWorld('desktopAssistant', {
    platform: process.platform,
    runtime
});
