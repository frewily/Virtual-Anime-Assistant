const {
    decideToolConfirmation,
    listToolConfirmations
} = require('./api');

const queue = [];
const queuedIds = new Set();
let submitting = false;

function elements() {
    if (typeof document === 'undefined') {
        return null;
    }
    const container = document.getElementById('tool-confirmation');
    if (!container) {
        return null;
    }
    return {
        container,
        title: document.getElementById('tool-confirmation-title'),
        tool: document.getElementById('tool-confirmation-name'),
        arguments: document.getElementById('tool-confirmation-arguments'),
        impact: document.getElementById('tool-confirmation-impact'),
        expires: document.getElementById('tool-confirmation-expires'),
        error: document.getElementById('tool-confirmation-error'),
        buttons: Array.from(
            container.querySelectorAll('[data-decision]')
        )
    };
}

function displayValue(value) {
    if (value === null) {
        return 'null';
    }
    if (typeof value === 'object') {
        try {
            return JSON.stringify(value);
        } catch {
            return '[无法显示]';
        }
    }
    return String(value);
}

function formatArguments(argumentsValue) {
    if (!argumentsValue || typeof argumentsValue !== 'object') {
        return '无';
    }
    const entries = Object.entries(argumentsValue);
    if (entries.length === 0) {
        return '无';
    }
    return entries
        .map(([key, value]) => `${key}: ${displayValue(value)}`)
        .join('\n');
}

function renderNextConfirmation() {
    const view = elements();
    if (!view) {
        return;
    }
    const confirmation = queue[0];
    view.container.hidden = !confirmation;
    if (!confirmation) {
        return;
    }

    view.title.textContent = confirmation.title || '需要确认的操作';
    view.tool.textContent = confirmation.tool || '未知工具';
    view.arguments.textContent = formatArguments(
        confirmation.arguments
    );
    view.impact.textContent = confirmation.impact || '影响范围未知';
    view.expires.textContent = confirmation.expiresAt
        ? `确认将在 ${confirmation.expiresAt} 过期`
        : '确认有效期未知';
    view.error.textContent = '';
    view.buttons.forEach((button) => {
        button.disabled = submitting;
    });
}

function enqueueConfirmation(confirmation) {
    if (
        !confirmation
        || typeof confirmation.id !== 'string'
        || !confirmation.id
        || queuedIds.has(confirmation.id)
    ) {
        return false;
    }
    queuedIds.add(confirmation.id);
    queue.push(confirmation);
    renderNextConfirmation();
    return true;
}

function removeConfirmation(confirmationId) {
    if (!queuedIds.has(confirmationId)) {
        return false;
    }
    queuedIds.delete(confirmationId);
    const index = queue.findIndex(
        (confirmation) => confirmation.id === confirmationId
    );
    if (index >= 0) {
        queue.splice(index, 1);
    }
    return true;
}

async function restorePendingConfirmations() {
    try {
        const confirmations = await listToolConfirmations();
        if (Array.isArray(confirmations)) {
            confirmations.forEach(enqueueConfirmation);
        }
        renderNextConfirmation();
    } catch {
        const view = elements();
        if (view && !queue.length) {
            view.container.hidden = false;
            view.title.textContent = '暂时无法恢复待确认操作';
            view.tool.textContent = '';
            view.arguments.textContent = '';
            view.impact.textContent = '';
            view.expires.textContent = '';
            view.error.textContent = '请等待连接恢复后重试';
            view.buttons.forEach((button) => {
                button.disabled = true;
            });
        }
    }
}

function confirmationIdFromUpdate(update) {
    return update?.confirmationId
        || update?.confirmation?.id
        || update?.request?.confirmation?.id
        || null;
}

function handleConfirmationUpdate(update) {
    const confirmationId = confirmationIdFromUpdate(update);
    if (!confirmationId) {
        return false;
    }
    const removed = removeConfirmation(confirmationId);
    renderNextConfirmation();
    return removed;
}

async function submitDecision(decision) {
    const confirmation = queue[0];
    if (!confirmation || submitting) {
        return;
    }
    submitting = true;
    renderNextConfirmation();
    let failed = false;
    try {
        await decideToolConfirmation(confirmation.id, decision);
        removeConfirmation(confirmation.id);
    } catch {
        failed = true;
    } finally {
        submitting = false;
        renderNextConfirmation();
        if (failed) {
            const view = elements();
            if (view) {
                view.error.textContent = '无法提交决定，请稍后重试';
            }
        }
    }
}

function initializeToolConfirmations() {
    const view = elements();
    if (!view) {
        return;
    }
    view.buttons.forEach((button) => {
        button.addEventListener('click', () => {
            submitDecision(button.dataset.decision);
        });
    });
    renderNextConfirmation();
}

module.exports = {
    enqueueConfirmation,
    handleConfirmationUpdate,
    initializeToolConfirmations,
    restorePendingConfirmations
};
