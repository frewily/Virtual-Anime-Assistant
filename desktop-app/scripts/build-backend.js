const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const desktopRoot = path.resolve(__dirname, '..');
const repositoryRoot = path.resolve(desktopRoot, '..');
const specPath = path.join(repositoryRoot, 'backend', 'vaa-backend.spec');
const buildRoot = path.join(desktopRoot, 'build-resources');
const distPath = path.join(buildRoot, 'backend');
const workPath = path.join(buildRoot, 'pyinstaller-work');
const configPath = path.join(buildRoot, 'pyinstaller-config');
const executableName = process.platform === 'win32'
    ? 'vaa-backend.exe'
    : 'vaa-backend';
const executablePath = path.join(distPath, executableName);
const configuredPython = process.env.ASSISTANT_PACKAGING_PYTHON;
const python = configuredPython || (process.platform === 'win32' ? 'python' : 'python3');

if (configuredPython && !path.isAbsolute(configuredPython)) {
    throw new Error('ASSISTANT_PACKAGING_PYTHON must be an absolute path');
}
if (!fs.existsSync(specPath)) throw new Error('backend packaging spec is missing');

fs.rmSync(distPath, { recursive: true, force: true });
fs.rmSync(workPath, { recursive: true, force: true });
fs.rmSync(configPath, { recursive: true, force: true });

const result = spawnSync(python, [
    '-m',
    'PyInstaller',
    '--noconfirm',
    '--clean',
    '--distpath',
    distPath,
    '--workpath',
    workPath,
    specPath
], {
    cwd: desktopRoot,
    env: {
        ...process.env,
        PYINSTALLER_CONFIG_DIR: configPath
    },
    shell: false,
    stdio: 'inherit'
});

if (result.error) throw result.error;
if (result.status !== 0) {
    throw new Error(`backend packaging failed with status ${result.status}`);
}
if (!fs.statSync(executablePath).isFile()) {
    throw new Error('backend packaging did not produce the expected executable');
}

console.log(`[Package] Backend ready: ${executableName}`);
