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

test('packaged runtime relocates config and generated audio out of the bundle', () => {
    const configLoader = read(path.join(repositoryRoot, 'backend', 'core', 'config_loader.py'));
    const tts = read(path.join(repositoryRoot, 'backend', 'core', 'tts.py'));
    const supervisor = read(path.join(desktopRoot, 'src', 'backend-supervisor.js'));

    assert.match(configLoader, /ASSISTANT_BUNDLED_CONFIG_DIR/);
    assert.match(tts, /ASSISTANT_AUDIO_DIR/);
    assert.match(supervisor, /resourcesPath[\s\S]*'config'/);
    assert.match(supervisor, /userDataPath[\s\S]*'audio'/);
});

test('packaged application carries a local tray icon without a remote source', () => {
    const icon = read(path.join(desktopRoot, 'src', 'assets', 'tray-icon.svg'));
    const main = read(path.join(desktopRoot, 'src', 'main.js'));

    assert.match(icon, /^<svg\b/);
    assert.doesNotMatch(
        icon.replace('http://www.w3.org/2000/svg', ''),
        /https?:\/\/|<script\b|<foreignObject\b/i
    );
    assert.match(main, /nativeImage\.createFromDataURL/);
    assert.match(main, /assets\/tray-icon\.svg|assets', 'tray-icon\.svg/);
});
