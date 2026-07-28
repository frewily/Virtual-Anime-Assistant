const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const rendererRoot = path.resolve(__dirname, '..', 'src', 'renderer');

test('loads Cubism Core before the renderer bundle', () => {
    const html = fs.readFileSync(path.join(rendererRoot, 'index.html'), 'utf8');
    const core = html.indexOf('assets/dev-live2d/live2dcubismcore4.min.js');
    const bundle = html.indexOf('dist/renderer.js');

    assert.ok(core >= 0);
    assert.ok(bundle > core);
    assert.match(html, /class="drag-handle"/);
});

test('renderer uses the Cubism 4 entry and real Hiyori motion groups', () => {
    const source = fs.readFileSync(path.join(rendererRoot, 'js', 'live2d.js'), 'utf8');

    assert.match(source, /pixi-live2d-display\/cubism4/);
    assert.match(source, /resolveMotionGroup/);
    assert.doesNotMatch(source, /tap_head/);
});
