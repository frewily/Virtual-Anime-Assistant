const { execFileSync } = require('node:child_process');
const path = require('node:path');
const {
    ARCHIVE_TAG,
    restoreLive2DAssets
} = require('./live2d-dev-assets');

const projectRoot = path.resolve(__dirname, '..', '..');
const assetsRoot = path.join(projectRoot, 'desktop-app', 'src', 'renderer', 'assets');
const targetDir = path.join(assetsRoot, 'dev-live2d');

execFileSync('git', ['rev-parse', '--verify', `${ARCHIVE_TAG}^{commit}`], {
    cwd: projectRoot,
    stdio: 'ignore'
});

restoreLive2DAssets({
    assetsRoot,
    targetDir,
    readSourceFile: (sourcePath) => execFileSync(
        'git',
        ['show', `${ARCHIVE_TAG}:${sourcePath}`],
        {
            cwd: projectRoot,
            encoding: 'buffer',
            maxBuffer: 16 * 1024 * 1024
        }
    )
});

console.log(`Live2D development assets restored to ${targetDir}`);
