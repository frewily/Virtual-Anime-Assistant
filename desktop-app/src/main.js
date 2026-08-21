const { app, BrowserWindow, Tray, Menu, nativeImage, shell } = require('electron');
const fs = require('fs');
const path = require('path');
const { createDesktopBackendSupervisor } = require('./backend-supervisor');
const { reconcileDesktopStartup } = require('./desktop-startup');

const SETTINGS_URL = 'http://127.0.0.1:8080/settings';

let mainWindow;
let tray;
const backendSupervisor = createDesktopBackendSupervisor({ app });

async function openSettings() {
    try {
        await shell.openExternal(SETTINGS_URL);
    } catch {
        console.error('[Settings] Unable to open settings page.');
    }
}

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 400,
        height: 600,
        transparent: true,
        frame: false,
        alwaysOnTop: true,
        skipTaskbar: true,
        resizable: false,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            nodeIntegration: false,
            contextIsolation: true,
            sandbox: true
        }
    });

    mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    mainWindow.webContents.on('will-navigate', (event) => event.preventDefault());
    mainWindow.loadFile(path.join(__dirname, 'renderer/index.html'));

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

function createTray() {
    const iconPath = path.join(__dirname, 'assets/tray-icon.svg');
    if (!fs.existsSync(iconPath)) {
        console.warn(`[Tray] Icon not found: ${iconPath}`);
        return;
    }

    const source = fs.readFileSync(iconPath, 'utf8');
    const icon = nativeImage.createFromDataURL(
        `data:image/svg+xml;base64,${Buffer.from(source).toString('base64')}`
    );
    if (icon.isEmpty()) {
        console.warn('[Tray] Icon could not be loaded.');
        return;
    }
    tray = new Tray(icon);
    
    const contextMenu = Menu.buildFromTemplate([
        { label: '显示', click: () => mainWindow?.show() },
        { label: '隐藏', click: () => mainWindow?.hide() },
        { type: 'separator' },
        { label: '设置', click: openSettings },
        { type: 'separator' },
        { label: '退出', click: () => app.quit() }
    ]);
    
    tray.setToolTip('桌面助手');
    tray.setContextMenu(contextMenu);
}

app.whenReady().then(async () => {
    await backendSupervisor.start();
    await reconcileDesktopStartup({ app });
    createWindow();
    createTray();
});

app.on('before-quit', () => {
    backendSupervisor.stop();
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('activate', () => {
    if (mainWindow === null) {
        createWindow();
    }
});
