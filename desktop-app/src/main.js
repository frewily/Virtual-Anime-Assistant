const {
    app,
    BrowserWindow,
    ipcMain,
    Tray,
    Menu,
    nativeImage,
    shell
} = require('electron');
const path = require('path');
const { pathToFileURL } = require('node:url');
const { createDesktopBackendSupervisor } = require('./backend-supervisor');
const { reconcileDesktopStartup } = require('./desktop-startup');
const { createRuntimeConnection } = require('./runtime-connection');

const RUNTIME_CHANNEL = 'desktop-runtime:get';

let mainWindow;
let tray;
let backendSupervisor = null;
let runtimeConnection = null;

function rendererEntryUrl() {
    return pathToFileURL(
        path.join(__dirname, 'renderer/index.html')
    ).toString();
}

function publicRuntimeConnection(connection) {
    return {
        host: connection.host,
        port: connection.port,
        accessToken: connection.accessToken,
        httpOrigin: connection.httpOrigin,
        wsOrigin: connection.wsOrigin
    };
}

function installRuntimeIpc(connection) {
    const allowedUrl = rendererEntryUrl();
    ipcMain.on(RUNTIME_CHANNEL, (event) => {
        const senderUrl = event.senderFrame?.url;
        event.returnValue = senderUrl === allowedUrl
            ? publicRuntimeConnection(connection)
            : null;
    });
}

function settingsUrl(connection) {
    const url = new URL('/settings', connection.httpOrigin);
    if (connection.accessToken) {
        url.hash = new URLSearchParams({
            desktopToken: connection.accessToken
        }).toString();
    }
    return url.toString();
}

async function openSettings() {
    try {
        if (!runtimeConnection) throw new Error('runtime unavailable');
        await shell.openExternal(settingsUrl(runtimeConnection));
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
    const iconPath = path.join(__dirname, 'assets/tray-icon.png');
    const icon = nativeImage.createFromPath(iconPath);
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
    runtimeConnection = await createRuntimeConnection({
        isPackaged: app.isPackaged
    });
    installRuntimeIpc(runtimeConnection);
    backendSupervisor = createDesktopBackendSupervisor({
        app,
        connection: runtimeConnection
    });
    await backendSupervisor.start();
    await reconcileDesktopStartup({ app });
    createWindow();
    createTray();
}).catch(() => {
    console.error('[Desktop] Unable to initialize local runtime.');
    app.quit();
});

app.on('before-quit', () => {
    backendSupervisor?.stop();
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
