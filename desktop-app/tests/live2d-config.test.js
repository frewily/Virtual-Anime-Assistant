const assert = require('node:assert/strict');
const test = require('node:test');
const {
    clampScaleMultiplier,
    fitScale,
    readStoredScale,
    resolveMotionGroup
} = require('../src/renderer/js/live2d-config');

test('maps supported motion aliases and rejects unknown motions', () => {
    assert.equal(resolveMotionGroup('idle'), 'Idle');
    assert.equal(resolveMotionGroup('Idle'), 'Idle');
    assert.equal(resolveMotionGroup('tap_body'), 'TapBody');
    assert.equal(resolveMotionGroup('TapBody'), 'TapBody');
    assert.equal(resolveMotionGroup('wave'), null);
    assert.equal(resolveMotionGroup(null), null);
});

test('clamps scale and rejects invalid stored values', () => {
    assert.equal(clampScaleMultiplier(0.1), 0.5);
    assert.equal(clampScaleMultiplier(2), 1.2);
    assert.equal(clampScaleMultiplier(Number.NaN), 1);
    assert.equal(readStoredScale('85'), 0.85);
    assert.equal(readStoredScale('invalid'), 1);
});

test('calculates a safe model fit scale', () => {
    assert.equal(fitScale(600, 1000), 0.51);
    assert.equal(fitScale(0, 1000), 1);
    assert.equal(fitScale(600, 0), 1);
});
