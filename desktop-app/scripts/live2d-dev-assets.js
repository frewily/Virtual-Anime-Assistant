const fs = require('node:fs');
const path = require('node:path');

const ARCHIVE_TAG = 'archive/legacy-java-qq-live2d-2026-07-28';
const SOURCE_ROOT = 'desktop-app/assets/';
const REQUIRED_RESOURCE_PATHS = Object.freeze([
    `${SOURCE_ROOT}live2dcubismcore4.min.js`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.model3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.moc3`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.2048/texture_00.png`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.2048/texture_01.png`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.physics3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.pose3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.userdata3.json`,
    `${SOURCE_ROOT}models/hiyori/Hiyori.cdi3.json`,
    ...Array.from(
        { length: 10 },
        (_, index) => (
            `${SOURCE_ROOT}models/hiyori/motions/Hiyori_m${String(index + 1).padStart(2, '0')}.motion3.json`
        )
    )
]);

function assertInside(root, target) {
    const relative = path.relative(path.resolve(root), path.resolve(target));
    if (relative.startsWith('..') || path.isAbsolute(relative) || relative === '') {
        throw new Error('Target directory must stay inside renderer assets');
    }
}

function destinationFor(sourcePath) {
    if (sourcePath === `${SOURCE_ROOT}live2dcubismcore4.min.js`) {
        return 'live2dcubismcore4.min.js';
    }

    const modelPrefix = `${SOURCE_ROOT}models/hiyori/`;
    if (!sourcePath.startsWith(modelPrefix)) {
        throw new Error(`Unsupported archived Live2D resource: ${sourcePath}`);
    }
    return path.join('hiyori', sourcePath.slice(modelPrefix.length));
}

function recoverInterruptedSwap(targetDir, backupDir) {
    if (!fs.existsSync(backupDir)) return;

    if (fs.existsSync(targetDir)) {
        fs.rmSync(backupDir, { recursive: true, force: true });
        return;
    }
    fs.renameSync(backupDir, targetDir);
}

function restoreLive2DAssets({
    targetDir,
    assetsRoot = path.dirname(targetDir),
    readSourceFile
}) {
    if (typeof readSourceFile !== 'function') {
        throw new TypeError('readSourceFile must be a function');
    }

    assertInside(assetsRoot, targetDir);
    const temporaryDir = `${targetDir}.tmp-${process.pid}-${Date.now()}`;
    const backupDir = `${targetDir}.backup-${process.pid}`;
    recoverInterruptedSwap(targetDir, backupDir);

    try {
        for (const sourcePath of REQUIRED_RESOURCE_PATHS) {
            const content = readSourceFile(sourcePath);
            if (!Buffer.isBuffer(content)) {
                throw new Error(`Missing archived Live2D resource: ${sourcePath}`);
            }

            const outputPath = path.join(temporaryDir, destinationFor(sourcePath));
            assertInside(temporaryDir, outputPath);
            fs.mkdirSync(path.dirname(outputPath), { recursive: true });
            fs.writeFileSync(outputPath, content);
        }

        if (fs.existsSync(targetDir)) {
            fs.renameSync(targetDir, backupDir);
        }
        fs.renameSync(temporaryDir, targetDir);
        fs.rmSync(backupDir, { recursive: true, force: true });
    } catch (error) {
        fs.rmSync(temporaryDir, { recursive: true, force: true });
        if (!fs.existsSync(targetDir) && fs.existsSync(backupDir)) {
            fs.renameSync(backupDir, targetDir);
        }
        throw error;
    }
}

module.exports = {
    ARCHIVE_TAG,
    REQUIRED_RESOURCE_PATHS,
    restoreLive2DAssets
};
