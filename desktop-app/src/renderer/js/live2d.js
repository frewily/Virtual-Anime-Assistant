const PIXI = require('pixi.js');
const { Live2DModel } = require('pixi-live2d-display/cubism4');
const {
    MODEL_PATH,
    SCALE_DEFAULT,
    clampScaleMultiplier,
    fitScale,
    readStoredScale,
    resolveMotionGroup
} = require('./live2d-config');

window.PIXI = PIXI;

const SCALE_STORAGE_KEY = 'assistant_scale';
const SCALE_STEP = 0.05;
let app;
let model;
let modelNaturalHeight = 0;
let lifecycleState = 'idle';
let initializationPromise;
let globalControlsBound = false;
let scaleMultiplier = SCALE_DEFAULT;

function showStatus(message) {
    const status = document.getElementById('assistant-status');
    status.textContent = message;
    status.hidden = false;
}

function hideStatus() {
    document.getElementById('assistant-status').hidden = true;
}

function applyScale() {
    if (!model || lifecycleState !== 'ready') return;

    const container = document.getElementById('live2d-container');
    const baseScale = fitScale(container.clientHeight, modelNaturalHeight);
    model.scale.set(baseScale * scaleMultiplier);
    model.x = container.clientWidth / 2;
    model.y = container.clientHeight * 0.52;
}

function saveScale() {
    localStorage.setItem(SCALE_STORAGE_KEY, String(Math.round(scaleMultiplier * 100)));
}

function adjustScale(nextValue) {
    scaleMultiplier = clampScaleMultiplier(nextValue);
    saveScale();
    applyScale();
}

function bindInteractionControls(canvas) {
    canvas.addEventListener('wheel', (event) => {
        if (!event.ctrlKey && !event.metaKey) return;
        event.preventDefault();
        adjustScale(scaleMultiplier + (event.deltaY < 0 ? SCALE_STEP : -SCALE_STEP));
    }, { passive: false });

    if (globalControlsBound) return;
    globalControlsBound = true;

    window.addEventListener('resize', () => {
        if (!app) return;
        const container = document.getElementById('live2d-container');
        app.renderer.resize(container.clientWidth, container.clientHeight);
        applyScale();
    });

    window.addEventListener('keydown', (event) => {
        if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) {
            return;
        }

        if (event.key === '+' || event.key === '=') {
            event.preventDefault();
            adjustScale(scaleMultiplier + SCALE_STEP);
        } else if (event.key === '-') {
            event.preventDefault();
            adjustScale(scaleMultiplier - SCALE_STEP);
        } else if (event.key === '0') {
            event.preventDefault();
            adjustScale(SCALE_DEFAULT);
        }
    });
}

async function loadModel(container, canvas) {
    model = await Live2DModel.from(MODEL_PATH, { autoInteract: true });
    modelNaturalHeight = model.height;
    model.anchor.set(0.5, 0.5);
    model.interactive = true;

    model.on('hit', (hitAreas) => {
        if (Array.isArray(hitAreas) && hitAreas.includes('Body')) {
            playMotion('tap_body');
        }
    });

    app.stage.addChild(model);
    lifecycleState = 'ready';
    scaleMultiplier = readStoredScale(localStorage.getItem(SCALE_STORAGE_KEY));
    applyScale();
    bindInteractionControls(canvas);
    hideStatus();
    console.info('[Live2D] Hiyori development model is ready');
    return true;
}

async function initLive2D() {
    if (initializationPromise) return initializationPromise;

    initializationPromise = (async () => {
        if (!window.Live2DCubismCore) {
            lifecycleState = 'missing';
            showStatus('缺少 Live2D 开发资源，请运行 npm run setup:live2d-dev');
            return false;
        }

        lifecycleState = 'loading';
        showStatus('正在加载 Live2D 模型…');

        const container = document.getElementById('live2d-container');
        const canvas = document.createElement('canvas');
        container.replaceChildren(canvas);
        app = new PIXI.Application({
            view: canvas,
            transparent: true,
            antialias: true,
            autoStart: true,
            autoDensity: true,
            resolution: window.devicePixelRatio || 1,
            width: container.clientWidth || 400,
            height: container.clientHeight || 600
        });

        try {
            return await loadModel(container, canvas);
        } catch (error) {
            lifecycleState = 'error';
            showStatus('Live2D 模型加载失败，请重新准备开发资源后重启');
            console.error(`[Live2D] Model load failed: ${error?.name || 'Error'}`);
            return false;
        }
    })();

    return initializationPromise;
}

function setExpression(expressionName) {
    const manager = model?.internalModel?.motionManager?.expressionManager;
    const definitions = manager?.definitions;
    const hasExpression = Array.isArray(definitions) && definitions.some(
        (definition) => definition?.Name === expressionName || definition?.name === expressionName
    );
    if (lifecycleState !== 'ready' || !hasExpression) return false;

    void model.expression(expressionName).catch((error) => {
        console.warn(`[Live2D] Expression failed: ${error?.name || 'Error'}`);
    });
    return true;
}

function playMotion(motionName) {
    const group = resolveMotionGroup(motionName);
    if (!model || lifecycleState !== 'ready' || !group) {
        if (motionName) {
            console.warn(`[Live2D] Unsupported or unavailable motion: ${motionName}`);
        }
        return false;
    }

    void model.motion(group).catch((error) => {
        console.warn(`[Live2D] Motion failed: ${error?.name || 'Error'}`);
    });
    return true;
}

function getModel() {
    return model;
}

function getLifecycleState() {
    return lifecycleState;
}

window.setExpression = setExpression;
window.playMotion = playMotion;

window.addEventListener('DOMContentLoaded', () => {
    void initLive2D();
});

module.exports = {
    getLifecycleState,
    getModel,
    initLive2D,
    playMotion,
    setExpression
};
