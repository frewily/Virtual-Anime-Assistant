const scenarioActions = {
    'high_cpu': {
        expression: 'f04',
        motion: 'tap_body',
        text: '主人，电脑好热啊...'
    },
    'gaming': {
        expression: 'f06',
        motion: 'shake',
        text: '又在玩游戏！'
    },
    'late_night': {
        expression: 'f03',
        motion: 'pinch_in',
        text: '好困...主人早点休息吧'
    },
    'idle': {
        expression: 'f01',
        motion: 'idle',
        text: null
    },
    'happy': {
        expression: 'f01',
        motion: 'wave',
        text: '好开心~'
    },
    'curious': {
        expression: 'f02',
        motion: 'tilt',
        text: '嗯？'
    }
};

function triggerAction(scenarioType) {
    const action = scenarioActions[scenarioType];
    if (action) {
        if (action.expression) {
            window.setExpression && window.setExpression(action.expression);
        }
        if (action.motion) {
            window.playMotion && window.playMotion(action.motion);
        }
        return action.text;
    }
    return null;
}

function getScenarioActions() {
    return scenarioActions;
}

module.exports = {
    triggerAction,
    getScenarioActions
};
