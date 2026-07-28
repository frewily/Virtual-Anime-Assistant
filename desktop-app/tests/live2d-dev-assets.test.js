const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
    REQUIRED_RESOURCE_PATHS,
    restoreLive2DAssets
} = require('../scripts/live2d-dev-assets');

function sourceFiles() {
    return new Map(
        REQUIRED_RESOURCE_PATHS.map((sourcePath) => [
            sourcePath,
            Buffer.from(`fixture:${sourcePath}`)
        ])
    );
}

test('restores every required resource into the development layout', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-assets-'));
    const targetDir = path.join(root, 'dev-live2d');
    const files = sourceFiles();

    restoreLive2DAssets({
        targetDir,
        readSourceFile: (sourcePath) => files.get(sourcePath)
    });

    assert.equal(
        fs.readFileSync(path.join(targetDir, 'live2dcubismcore4.min.js'), 'utf8'),
        'fixture:desktop-app/assets/live2dcubismcore4.min.js'
    );
    assert.ok(fs.existsSync(path.join(targetDir, 'hiyori', 'Hiyori.model3.json')));
    assert.ok(fs.existsSync(path.join(targetDir, 'hiyori', 'motions', 'Hiyori_m10.motion3.json')));
});

test('does not replace an existing target when one source is missing', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-assets-'));
    const targetDir = path.join(root, 'dev-live2d');
    fs.mkdirSync(targetDir, { recursive: true });
    fs.writeFileSync(path.join(targetDir, 'sentinel.txt'), 'keep');
    const files = sourceFiles();
    files.delete(REQUIRED_RESOURCE_PATHS.at(-1));

    assert.throws(
        () => restoreLive2DAssets({
            targetDir,
            readSourceFile: (sourcePath) => files.get(sourcePath)
        }),
        /Missing archived Live2D resource/
    );
    assert.equal(fs.readFileSync(path.join(targetDir, 'sentinel.txt'), 'utf8'), 'keep');
});

test('repeated restore replaces the generated directory deterministically', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-assets-'));
    const targetDir = path.join(root, 'dev-live2d');
    const files = sourceFiles();

    restoreLive2DAssets({
        targetDir,
        readSourceFile: (sourcePath) => files.get(sourcePath)
    });
    fs.writeFileSync(path.join(targetDir, 'unexpected.txt'), 'remove me');
    restoreLive2DAssets({
        targetDir,
        readSourceFile: (sourcePath) => files.get(sourcePath)
    });

    assert.equal(fs.existsSync(path.join(targetDir, 'unexpected.txt')), false);
    assert.ok(fs.existsSync(path.join(targetDir, 'hiyori', 'Hiyori.moc3')));
});

test('rejects a target outside an explicitly supplied renderer assets root', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'live2d-assets-'));
    const assetsRoot = path.join(root, 'assets');

    assert.throws(
        () => restoreLive2DAssets({
            assetsRoot,
            targetDir: path.join(root, '..', 'escaped'),
            readSourceFile: () => Buffer.from('x')
        }),
        /Target directory must stay inside renderer assets/
    );
});
