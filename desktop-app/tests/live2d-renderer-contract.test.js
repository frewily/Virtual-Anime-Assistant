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

test('allows same-origin Live2D resources through the connect policy', () => {
    const html = fs.readFileSync(path.join(rendererRoot, 'index.html'), 'utf8');
    const contentSecurityPolicy = html.match(
        /http-equiv="Content-Security-Policy" content="([^"]+)"/
    );

    assert.ok(contentSecurityPolicy);
    assert.match(contentSecurityPolicy[1], /connect-src[^;]*'self'/);
});

test('gives the Live2D container a full-window height chain', () => {
    const styles = fs.readFileSync(path.join(rendererRoot, 'styles', 'main.css'), 'utf8');

    assert.match(styles, /html,\s*body\s*\{[^}]*height:\s*100%/s);
    assert.match(styles, /#live2d-container\s*\{[^}]*height:\s*100%/s);
});

test('renderer uses the Cubism 4 entry and real Hiyori motion groups', () => {
    const source = fs.readFileSync(path.join(rendererRoot, 'js', 'live2d.js'), 'utf8');
    const unsafeEval = source.indexOf('installUnsafeEval(PIXI)');
    const application = source.indexOf('new PIXI.Application');
    const loadModel = source.indexOf('async function loadModel');
    const cubismRuntime = source.indexOf("require('pixi-live2d-display/cubism4')");

    assert.match(source, /pixi-live2d-display\/cubism4/);
    assert.ok(unsafeEval >= 0);
    assert.ok(application > unsafeEval);
    assert.ok(cubismRuntime > loadModel);
    assert.match(source, /backgroundAlpha:\s*0/);
    assert.doesNotMatch(source, /\btransparent:\s*true/);
    assert.match(source, /resolveMotionGroup/);
    assert.doesNotMatch(source, /tap_head/);
});

test('websocket delegates speak audio to the bounded playback module', () => {
    const source = fs.readFileSync(
        path.join(rendererRoot, 'js', 'websocket.js'),
        'utf8'
    );

    assert.match(source, /require\('\.\/speech-playback'\)/);
    assert.match(source, /createSpeechPlayback\(\{[\s\S]*accessToken/);
    assert.match(source, /vaa\.desktop\.\$\{runtime\.accessToken\}/);
    assert.match(source, /speechPlayback\.handleSpeakAudio\(message\)/);
    assert.doesNotMatch(source, /new Audio\(/);
});
