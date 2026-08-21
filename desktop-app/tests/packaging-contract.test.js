const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const desktopRoot = path.resolve(__dirname, '..');
const repositoryRoot = path.resolve(desktopRoot, '..');
const read = (filePath) => fs.readFileSync(filePath, 'utf8');

test('desktop package builds the backend before electron-builder', () => {
    const packageJson = JSON.parse(read(path.join(desktopRoot, 'package.json')));
    const build = packageJson.scripts.build;

    assert.equal(packageJson.scripts['build:backend'], 'node scripts/build-backend.js');
    assert.match(build, /build:renderer/);
    assert.match(build, /build:backend/);
    assert.match(build, /electron-builder/);
    assert.ok(build.indexOf('build:backend') < build.indexOf('electron-builder'));
});

test('electron-builder includes only the staged backend and public config files', () => {
    const source = read(path.join(desktopRoot, 'electron-builder.yml'));

    assert.match(source, /from: build-resources\/backend[\s\S]*to: backend/);
    assert.match(source, /vaa-backend\.exe/);
    assert.match(source, /from: \.\.\/config[\s\S]*"\*\.yml"/);
    assert.match(source, /target: dmg/);
    assert.doesNotMatch(source, /config\/local|\.env|secrets/);
});

test('PyInstaller spec carries local settings assets and dynamic keyring backends', () => {
    const source = read(path.join(repositoryRoot, 'backend', 'vaa-backend.spec'));

    assert.match(source, /settings"\s*\/\s*"static"/);
    assert.match(source, /collect_submodules\("keyring\.backends"\)/);
    assert.match(source, /name="vaa-backend"/);
    assert.doesNotMatch(source, /backend\/tests|config\/local/);
});

test('backend build script uses argv execution and an isolated staging directory', () => {
    const source = read(path.join(desktopRoot, 'scripts', 'build-backend.js'));

    assert.match(source, /spawnSync\(python, \[/);
    assert.match(source, /shell:\s*false/);
    assert.match(source, /ASSISTANT_PACKAGING_PYTHON must be an absolute path/);
    assert.match(source, /build-resources/);
    assert.match(source, /PYINSTALLER_CONFIG_DIR:\s*configPath/);
    assert.doesNotMatch(source, /execSync|shell:\s*true/);
});

test('packaged runtime relocates writable state and requires ephemeral access', () => {
    const configLoader = read(path.join(repositoryRoot, 'backend', 'core', 'config_loader.py'));
    const tts = read(path.join(repositoryRoot, 'backend', 'core', 'tts.py'));
    const supervisor = read(path.join(desktopRoot, 'src', 'backend-supervisor.js'));

    assert.match(configLoader, /ASSISTANT_BUNDLED_CONFIG_DIR/);
    assert.match(tts, /ASSISTANT_AUDIO_DIR/);
    assert.match(supervisor, /resourcesPath[\s\S]*'config'/);
    assert.match(supervisor, /userDataPath[\s\S]*'audio'/);
    assert.match(supervisor, /ASSISTANT_CONFIG_DIR/);
    assert.match(supervisor, /ASSISTANT_DATA_DIR/);
    assert.match(supervisor, /ASSISTANT_DESKTOP_ACCESS_TOKEN/);
    assert.match(supervisor, /PACKAGED_READY_ATTEMPTS\s*=\s*120/);
});

test('packaged application carries a local tray icon without a remote source', () => {
    const iconPath = path.join(desktopRoot, 'src', 'assets', 'tray-icon.png');
    const main = read(path.join(desktopRoot, 'src', 'main.js'));

    assert.equal(fs.readFileSync(iconPath).subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
    assert.match(main, /nativeImage\.createFromPath/);
    assert.match(main, /assets\/tray-icon\.png|assets', 'tray-icon\.png/);
});
