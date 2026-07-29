const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const rendererRoot = path.resolve(__dirname, '..', 'src', 'renderer');

function read(relativePath) {
    return fs.readFileSync(path.join(rendererRoot, relativePath), 'utf8');
}

test('renderer loads the confirmation queue and exposes safe controls', () => {
    const renderer = read('js/renderer.js');
    const moduleSource = read('js/tool-confirmation.js');
    const html = read('index.html');
    const styles = read('styles/main.css');

    assert.match(renderer, /require\('\.\/tool-confirmation'\)/);
    assert.match(moduleSource, /textContent/);
    assert.doesNotMatch(moduleSource, /innerHTML/);
    assert.match(html, /id="tool-confirmation"/);
    assert.match(html, /aria-live="polite"/);
    assert.match(html, /data-decision="approve"/);
    assert.match(html, /data-decision="reject"/);
    assert.match(
        styles,
        /#tool-confirmation\s*\{[^}]*-webkit-app-region:\s*no-drag/s
    );
});

test('desktop API uses the exact tool confirmation endpoints', () => {
    const source = read('js/api.js');

    assert.match(source, /\/tools\/confirmations/);
    assert.match(source, /\/decision/);
    assert.match(source, /encodeURIComponent/);
});

test('websocket restores and updates the confirmation queue', () => {
    const source = read('js/websocket.js');

    assert.match(source, /tool_confirmation_required/);
    assert.match(source, /tool_confirmation_updated/);
    assert.match(source, /restorePendingConfirmations/);
});
