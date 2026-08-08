const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const staticRoot = path.resolve(__dirname, '..', '..', 'backend', 'settings', 'static');

function read(name) {
    return fs.readFileSync(path.join(staticRoot, name), 'utf8');
}

test('settings page is self-contained, semantic, and covers every editable field', () => {
    const html = read('index.html');
    const ids = [
        'setup-password', 'setup-confirm', 'login-password',
        'llm-enabled', 'llm-base-url', 'llm-model', 'llm-api-key',
        'llm-timeout-seconds', 'llm-max-context-messages',
        'llm-max-context-chars', 'llm-tool-calling-enabled',
        'qq-enabled', 'qq-access-token', 'qq-allowed-group-ids',
        'qq-allowed-user-ids', 'qq-rate-per-minute', 'qq-rate-burst',
        'qq-max-concurrency', 'qq-action-timeout-seconds',
        'tts-gpt-sovits-url', 'tts-default-voice-id',
        'tts-audio-max-age-seconds', 'save-settings', 'restart-notice',
        'dirty-notice', 'error-summary', 'keychain-status'
    ];

    for (const id of ids) assert.match(html, new RegExp(`id="${id}"`));
    assert.match(html, /role="tablist"/);
    assert.equal((html.match(/role="tab"/g) || []).length, 3);
    assert.match(html, /aria-live="(?:polite|assertive)"/);
    assert.match(html, /<script src="\/settings\/settings\.js" defer><\/script>/);
    assert.match(html, /<link rel="stylesheet" href="\/settings\/settings\.css">/);
    assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/);
    assert.doesNotMatch(html, /<style\b|\son\w+=/);
    assert.doesNotMatch(html, /https?:\/\//);
});

test('settings controller preserves secrets and uses the secured API contract', () => {
    const source = read('settings.js');
    assert.match(source, /const API = ['"]\/api\/settings['"]/);
    for (const suffix of ['session', 'logout', 'config', 'voices']) {
        assert.match(source, new RegExp(`API\\}\\/${suffix}`));
    }
    assert.match(source, /authenticate\('setup'/);
    assert.match(source, /authenticate\('login'/);
    assert.match(source, /API\}\/\$\{path\}/);
    assert.match(source, /API\}\/test\/\$\{section\}/);

    assert.match(source, /credentials:\s*['"]same-origin['"]/);
    assert.match(source, /X-CSRF-Token/);
    assert.match(source, /method:\s*['"]PUT['"]/);
    assert.match(source, /window\.confirm/);
    assert.match(source, /beforeunload/);
    assert.match(source, /409/);
    assert.match(source, /401/);
    assert.match(source, /clearSecretState/);
    assert.match(source, /restartRequired/);
    assert.match(source, /AbortController/);
    assert.match(source, /apiKey:\s*secretMutation\('llm\.apiKey'\)/);
    assert.match(source, /accessToken:\s*secretMutation\('qq\.accessToken'\)/);
    assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function/);
    assert.doesNotMatch(source, /localStorage|sessionStorage|console\./);
});

test('restart state survives edits and responsive tabs expose matching orientation', () => {
    const source = read('settings.js');
    const dirtyFunction = source.match(/function markDirty\(\)\s*\{([\s\S]*?)\n\s*\}/);

    assert.match(source, /restartPending:\s*false/);
    assert.match(source, /state\.restartPending\s*=\s*state\.restartPending\s*\|\|\s*snapshot\.restartRequired/);
    assert.ok(dirtyFunction);
    assert.doesNotMatch(dirtyFunction[1], /restart-notice|restartPending/);
    assert.match(source, /matchMedia\(['"]\(max-width:\s*760px\)['"]\)/);
    assert.match(source, /aria-orientation/);
    assert.match(source, /orientationQuery\.addEventListener\(['"]change['"]/);
    assert.match(source, /orientation === 'horizontal'/);
});

test('settings styles are warm, responsive, accessible, and motion-aware', () => {
    const css = read('settings.css');
    assert.match(css, /--paper:/);
    assert.match(css, /--brick:/);
    assert.match(css, /min-height:\s*44px/);
    assert.match(css, /@media\s*\(max-width:/);
    assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    assert.doesNotMatch(css, /url\s*\(\s*['"]?https?:\/\//);
});
