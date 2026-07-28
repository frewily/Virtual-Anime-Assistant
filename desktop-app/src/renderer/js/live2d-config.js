const MODEL_PATH = 'assets/dev-live2d/hiyori/Hiyori.model3.json';
const CORE_PATH = 'assets/dev-live2d/live2dcubismcore4.min.js';
const SCALE_MIN = 0.5;
const SCALE_MAX = 1.2;
const SCALE_DEFAULT = 1;
const MOTION_ALIASES = Object.freeze({
    idle: 'Idle',
    Idle: 'Idle',
    tap_body: 'TapBody',
    TapBody: 'TapBody'
});

function clampScaleMultiplier(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return SCALE_DEFAULT;
    return Math.min(SCALE_MAX, Math.max(SCALE_MIN, numeric));
}

function readStoredScale(value) {
    const numeric = Number.parseInt(value, 10);
    return Number.isFinite(numeric)
        ? clampScaleMultiplier(numeric / 100)
        : SCALE_DEFAULT;
}

function fitScale(containerHeight, modelHeight) {
    if (!(containerHeight > 0) || !(modelHeight > 0)) return SCALE_DEFAULT;
    return Number(((containerHeight / modelHeight) * 0.85).toFixed(6));
}

function resolveMotionGroup(name) {
    return typeof name === 'string' ? MOTION_ALIASES[name] ?? null : null;
}

module.exports = {
    CORE_PATH,
    MODEL_PATH,
    SCALE_DEFAULT,
    SCALE_MAX,
    SCALE_MIN,
    clampScaleMultiplier,
    fitScale,
    readStoredScale,
    resolveMotionGroup
};
