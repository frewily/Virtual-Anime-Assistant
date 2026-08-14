(() => {
  'use strict';

  const API = '/api/settings';
  const SOURCE_LABELS = Object.freeze({
    default: '默认值',
    persisted: '已保存',
    keychain: '系统凭据库',
    environment: '环境变量接管'
  });
  const CODE_MESSAGES = Object.freeze({
    SUCCESS: '连接成功',
    SETTINGS_AUTH_FAILED: '密码错误，请重试',
    SETTINGS_RATE_LIMITED: '尝试次数过多，请稍后再试',
    SETTINGS_PASSWORD_INVALID: '密码长度必须为 10–128 个字符',
    SETTINGS_CSRF_REJECTED: '页面验证已失效，请重新登录',
    SETTINGS_CONFLICT: '配置已在别处更新，请刷新页面后再修改',
    KEYCHAIN_UNAVAILABLE: '系统凭据库当前不可用',
    TIMED_OUT: '连接超时',
    UNREACHABLE: '无法连接到服务',
    AUTHENTICATION_FAILED: '服务拒绝了凭据',
    RATE_LIMITED: '服务暂时限流',
    INCOMPATIBLE_RESPONSE: '服务响应格式不兼容',
    VALIDATION_FAILED: '请检查当前配置',
    SERVICE_ERROR: '服务暂时不可用'
  });
  const SECRET_PATHS = Object.freeze(['llm.apiKey', 'qq.accessToken']);
  const CLOUD_STATUS_API = '/api/status/cloud';
  const CLOUD_POLL_INTERVAL_MS = 30_000;
  const CLOUD_LABELS = Object.freeze({
    overall: Object.freeze({ healthy: '正常', degraded: '降级', alerting: '需要处理', unknown: '未知' }),
    vaa: Object.freeze({ ready: '正常', not_ready: '未就绪', unavailable: '不可用', unknown: '未知' }),
    onebot: Object.freeze({ connected: '已连接', disconnected: '已断开', disabled: '未启用', misconfigured: '配置错误', unknown: '未知' }),
    backup: Object.freeze({ fresh: '正常', stale: '已过期', missing: '未找到', unknown: '未知' })
  });
  const CLOUD_ALERT_LABELS = Object.freeze({
    vaa_unavailable: 'VAA 不可用，请检查服务状态',
    configuration_required: 'QQ 配置需要处理',
    backup_stale: 'SQLite 备份已过期',
    recovery_exhausted: '自动恢复已达上限，请检查 QQ 登录',
    deployment_in_progress: '正在部署，已暂停自动恢复',
    state_invalid: '监控状态不可用'
  });

  const state = {
    authenticated: false,
    csrfToken: null,
    draft: null,
    dirty: false,
    editEpoch: 0,
    fieldEpochs: new Map(),
    secretEpochs: new Map(),
    restartPending: false,
    reauthPending: false,
    sessionGeneration: 0,
    authAttemptGeneration: 0,
    authBusy: false,
    saveGeneration: 0,
    saveBusy: false,
    logoutGeneration: 0,
    logoutBusy: false,
    probeGeneration: 0,
    statusOperationGeneration: 0,
    transientStatuses: new Map(),
    cloudStatusController: null,
    cloudStatusTimer: null,
    readOnlyPaths: new Set(),
    secrets: {
      'llm.apiKey': { operation: 'retain', value: null },
      'qq.accessToken': { operation: 'retain', value: null }
    },
    probes: new Map()
  };

  class ApiError extends Error {
    constructor(status, code, fields) {
      super('settings request failed');
      this.status = status;
      this.code = code;
      this.fields = fields && typeof fields === 'object' ? fields : {};
    }
  }

  class FieldInputError extends Error {
    constructor(path, message) {
      super('invalid field');
      this.path = path;
      this.safeMessage = message;
    }
  }

  class StaleRequestError extends Error {
    constructor() {
      super('stale settings request');
      this.name = 'StaleRequestError';
    }
  }

  const byId = (id) => document.getElementById(id);
  const controls = () => Array.from(document.querySelectorAll('[data-path]'));
  const controlForPath = (path) => controls().find((node) => node.dataset.path === path) || null;
  const sourceForPath = (path) => Array.from(document.querySelectorAll('[data-source-for]')).find((node) => node.dataset.sourceFor === path) || null;
  const errorForPath = (path) => Array.from(document.querySelectorAll('[data-error-for]')).find((node) => node.dataset.errorFor === path) || null;

  function statusNode(target) {
    return typeof target === 'string' ? byId(target) : target;
  }

  function createStatusOwner(sessionGeneration = state.sessionGeneration) {
    state.statusOperationGeneration += 1;
    return { sessionGeneration, operationToken: state.statusOperationGeneration };
  }

  function setTransientStatus(target, message, owner) {
    const node = statusNode(target);
    if (!node || !owner || owner.sessionGeneration !== state.sessionGeneration) return false;
    const current = state.transientStatuses.get(node);
    if (current && current.operationToken > owner.operationToken) return false;
    state.transientStatuses.set(node, owner);
    node.textContent = message;
    return true;
  }

  function clearTransientStatus(target, owner = null) {
    const node = statusNode(target);
    if (!node) return false;
    const current = state.transientStatuses.get(node);
    if (owner && current !== owner) return false;
    if (!current && owner) return false;
    state.transientStatuses.delete(node);
    node.textContent = '';
    return true;
  }

  function setPersistentStatus(target, message) {
    const node = statusNode(target);
    if (!node) return;
    state.transientStatuses.delete(node);
    node.textContent = message;
  }

  function clearTransientStatusesForGeneration(sessionGeneration) {
    for (const [node, owner] of state.transientStatuses.entries()) {
      if (owner.sessionGeneration !== sessionGeneration) continue;
      state.transientStatuses.delete(node);
      node.textContent = '';
    }
  }

  function safeMessage(error, fallback) {
    if (error instanceof FieldInputError) return error.safeMessage;
    if (error instanceof ApiError) return CODE_MESSAGES[error.code] || fallback;
    return fallback;
  }

  function safeCloudLabel(group, value) {
    const labels = CLOUD_LABELS[group];
    return labels && typeof value === 'string' ? (labels[value] || '未知') : '未知';
  }

  function setCloudValue(id, text, tone = '') {
    const node = byId(id);
    node.textContent = text;
    node.classList.toggle('is-healthy', tone === 'healthy');
    node.classList.toggle('is-alerting', tone === 'alerting');
  }

  function renderCloudStatus(payload) {
    const card = byId('cloud-operations');
    if (!payload || payload.available !== true) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    setCloudValue('cloud-overall-state', safeCloudLabel('overall', payload.overallState), payload.overallState === 'healthy' ? 'healthy' : (payload.overallState === 'alerting' ? 'alerting' : ''));
    setCloudValue('cloud-vaa-state', safeCloudLabel('vaa', payload.vaaState), payload.vaaState === 'ready' ? 'healthy' : '');
    setCloudValue('cloud-onebot-state', safeCloudLabel('onebot', payload.onebotState), payload.onebotState === 'connected' ? 'healthy' : (payload.onebotState === 'misconfigured' ? 'alerting' : ''));
    const backupTime = typeof payload.latestBackupAt === 'string' ? ` · ${payload.latestBackupAt}` : '';
    setCloudValue('cloud-backup-state', `${safeCloudLabel('backup', payload.backupState)}${backupTime}`, payload.backupState === 'fresh' ? 'healthy' : (['stale', 'missing'].includes(payload.backupState) ? 'alerting' : ''));
    const recoveries = Number.isInteger(payload.recoveriesInWindow) ? payload.recoveriesInWindow : 0;
    const lastRecovery = typeof payload.lastRecoveryAt === 'string' ? `，最近 ${payload.lastRecoveryAt}` : '';
    setCloudValue('cloud-recovery-state', `10 分钟内 ${recoveries} 次${lastRecovery}`);
    const alert = typeof payload.alertCode === 'string' ? (CLOUD_ALERT_LABELS[payload.alertCode] || '未知告警') : '无';
    setCloudValue('cloud-alert-state', alert, payload.alertCode ? 'alerting' : 'healthy');
  }

  function stopCloudPolling() {
    if (state.cloudStatusController) state.cloudStatusController.abort();
    state.cloudStatusController = null;
    if (state.cloudStatusTimer !== null) window.clearTimeout(state.cloudStatusTimer);
    state.cloudStatusTimer = null;
  }

  function scheduleCloudPoll(sessionGeneration) {
    if (!state.authenticated || document.visibilityState === 'hidden' || sessionGeneration !== state.sessionGeneration) return;
    state.cloudStatusTimer = window.setTimeout(() => {
      state.cloudStatusTimer = null;
      refreshCloudStatus(sessionGeneration);
    }, CLOUD_POLL_INTERVAL_MS);
  }

  async function refreshCloudStatus(sessionGeneration = state.sessionGeneration) {
    if (!state.authenticated || document.visibilityState === 'hidden' || sessionGeneration !== state.sessionGeneration) return;
    if (state.cloudStatusController) state.cloudStatusController.abort();
    const controller = new AbortController();
    state.cloudStatusController = controller;
    try {
      const payload = await request(CLOUD_STATUS_API, {
        signal: controller.signal
      });
      if (controller !== state.cloudStatusController || sessionGeneration !== state.sessionGeneration) return;
      renderCloudStatus(payload);
    } catch (error) {
      if (controller !== state.cloudStatusController || sessionGeneration !== state.sessionGeneration) return;
      if (!(error && error.name === 'AbortError') && !(error instanceof ApiError && error.status === 401)) {
        byId('cloud-operations').hidden = false;
        setCloudValue('cloud-overall-state', '暂时无法读取云端状态', 'alerting');
      }
    } finally {
      if (controller !== state.cloudStatusController) return;
      state.cloudStatusController = null;
      scheduleCloudPoll(sessionGeneration);
    }
  }

  function startCloudPolling() {
    stopCloudPolling();
    if (!state.authenticated || document.visibilityState === 'hidden') return;
    return refreshCloudStatus(state.sessionGeneration);
  }

  async function request(path, options = {}) {
    const sessionGeneration = options.sessionBound
      ? (Number.isInteger(options.sessionGeneration) ? options.sessionGeneration : state.sessionGeneration)
      : null;
    const headers = { Accept: 'application/json' };
    const init = {
      method: options.method || 'GET',
      credentials: 'same-origin',
      headers,
      signal: options.signal
    };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    if (options.write && state.csrfToken) headers['X-CSRF-Token'] = state.csrfToken;

    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      if (sessionGeneration !== null && sessionGeneration !== state.sessionGeneration) throw new StaleRequestError();
      if (error && error.name === 'AbortError') throw error;
      throw new ApiError(0, 'NETWORK_ERROR', {});
    }
    let payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (sessionGeneration !== null && sessionGeneration !== state.sessionGeneration) throw new StaleRequestError();
    if (response.status === 401) {
      if (sessionGeneration !== null) expireSession(sessionGeneration);
      throw new ApiError(401, 'SETTINGS_UNAUTHORIZED', {});
    }
    if (!response.ok) {
      const detail = payload && payload.error && typeof payload.error === 'object' ? payload.error : {};
      throw new ApiError(response.status, typeof detail.code === 'string' ? detail.code : 'REQUEST_FAILED', detail.fields);
    }
    return payload;
  }

  function abortProbes(message = '测试已过期') {
    state.probeGeneration += 1;
    for (const record of state.probes.values()) {
      record.controller.abort();
      record.button.disabled = false;
    }
    state.probes.clear();
    for (const status of document.querySelectorAll('[data-test-status]')) {
      const owner = state.transientStatuses.get(status);
      if (!owner) continue;
      if (message) setTransientStatus(status, message, owner);
      else clearTransientStatus(status, owner);
    }
  }

  function clearSecretState() {
    for (const path of SECRET_PATHS) {
      const input = Array.from(document.querySelectorAll('[data-secret]')).find((node) => node.dataset.secret === path);
      if (input) input.value = '';
      state.secrets[path].operation = 'retain';
      state.secrets[path].value = null;
      updateSecretState(path);
    }
  }

  function advanceSessionGeneration() {
    const previousGeneration = state.sessionGeneration;
    state.sessionGeneration += 1;
    state.saveGeneration += 1;
    state.saveBusy = false;
    state.logoutGeneration += 1;
    state.logoutBusy = false;
    stopCloudPolling();
    abortProbes('');
    clearTransientStatusesForGeneration(previousGeneration);
    byId('save-settings').disabled = false;
    byId('logout-button').disabled = false;
    return state.sessionGeneration;
  }

  function expireSession(expectedGeneration) {
    if (expectedGeneration !== state.sessionGeneration) return false;
    advanceSessionGeneration();
    state.authenticated = false;
    state.csrfToken = null;
    state.reauthPending = true;
    clearSecretState();
    showAuth(true, '登录已过期，非敏感草稿仍为你保留，请重新登录。');
    return true;
  }

  function showAuth(initialized, message = '') {
    stopCloudPolling();
    byId('auth-panel').hidden = false;
    byId('workspace').hidden = true;
    byId('action-bar').hidden = true;
    byId('setup-form').hidden = initialized;
    byId('login-form').hidden = !initialized;
    byId('auth-title').textContent = initialized ? '解锁设置簿' : '建立本机设置密码';
    byId('auth-description').textContent = initialized
      ? '输入本机设置密码继续。'
      : '第一次使用时，请设置一个只用于本机配置的密码。';
    setPersistentStatus('auth-message', message);
    byId('session-status').textContent = initialized ? '等待登录' : '尚未初始化';
    byId('session-dot').classList.remove('is-on');
    byId('logout-button').hidden = true;
  }

  function showWorkspace() {
    byId('auth-panel').hidden = true;
    byId('workspace').hidden = false;
    byId('action-bar').hidden = false;
    byId('session-status').textContent = '已安全登录';
    byId('session-dot').classList.add('is-on');
    byId('logout-button').hidden = false;
    startCloudPolling();
  }

  function valueAt(object, path) {
    return path.split('.').reduce((value, part) => value && value[part], object);
  }

  function assignControl(control, value) {
    if (control.type === 'checkbox') control.checked = value === true;
    else if (Array.isArray(value)) control.value = value.join(', ');
    else {
      const text = value === null || value === undefined ? '' : String(value);
      if (control.tagName === 'SELECT' && text && !Array.from(control.options).some((option) => option.value === text)) {
        const option = document.createElement('option');
        option.value = text;
        option.textContent = `${text} · 当前配置`;
        control.appendChild(option);
      }
      control.value = text;
    }
  }

  function applySnapshot(snapshot) {
    return mergeSnapshot(snapshot, null);
  }

  function mergeSnapshot(snapshot, saveContext) {
    if (!snapshot || typeof snapshot !== 'object' || !snapshot.draft || !snapshot.presentation) {
      throw new ApiError(0, 'INVALID_RESPONSE', {});
    }
    abortProbes();
    const requestEpoch = saveContext ? saveContext.editEpoch : Number.POSITIVE_INFINITY;
    const preservedFields = new Map();
    const preservedSecrets = new Map();
    if (saveContext) {
      for (const control of controls()) {
        if ((state.fieldEpochs.get(control.dataset.path) || 0) > requestEpoch) {
          preservedFields.set(control.dataset.path, {
            checked: control.checked,
            value: control.value
          });
        }
      }
      for (const path of SECRET_PATHS) {
        const captured = saveContext.secretEpochs.get(path) || 0;
        if ((state.secretEpochs.get(path) || 0) > captured) {
          preservedSecrets.set(path, {
            operation: state.secrets[path].operation,
            value: state.secrets[path].value
          });
        }
      }
    }
    state.draft = snapshot.draft;
    state.readOnlyPaths.clear();
    const fields = snapshot.presentation.fields && typeof snapshot.presentation.fields === 'object'
      ? snapshot.presentation.fields : {};

    for (const control of controls()) {
      const presentation = fields[control.dataset.path];
      const shownValue = presentation && presentation.source === 'environment'
        ? presentation.value : valueAt(snapshot.draft, control.dataset.path);
      assignControl(control, shownValue);
    }
    for (const path of SECRET_PATHS) {
      const input = Array.from(document.querySelectorAll('[data-secret]')).find((node) => node.dataset.secret === path);
      const preserved = preservedSecrets.get(path);
      state.secrets[path] = preserved || { operation: 'retain', value: null };
      if (input) input.value = preserved && preserved.value ? preserved.value : '';
      updateSecretState(path);
    }
    for (const [path, presentation] of Object.entries(fields)) applyPresentation(path, presentation);
    const keychainAvailable = snapshot.presentation.keychainAvailable === true;
    byId('keychain-status').textContent = keychainAvailable
      ? '系统凭据库：可用' : '系统凭据库：不可用，敏感配置无法修改';
    if (!keychainAvailable) {
      for (const path of SECRET_PATHS) {
        const input = Array.from(document.querySelectorAll('[data-secret]')).find((node) => node.dataset.secret === path);
        if (input) input.disabled = true;
        for (const button of document.querySelectorAll('[data-replace], [data-retain], [data-delete]')) {
          if (button.dataset.replace === path || button.dataset.retain === path || button.dataset.delete === path) button.disabled = true;
        }
      }
    }
    for (const [path, preserved] of preservedFields.entries()) {
      if (state.readOnlyPaths.has(path)) continue;
      const control = controlForPath(path);
      if (!control) continue;
      control.checked = preserved.checked;
      control.value = preserved.value;
    }
    if (saveContext) {
      for (const [path, epoch] of state.fieldEpochs.entries()) {
        if (epoch <= requestEpoch) state.fieldEpochs.delete(path);
      }
      for (const [path, epoch] of state.secretEpochs.entries()) {
        if (epoch <= (saveContext.secretEpochs.get(path) || 0)) state.secretEpochs.delete(path);
      }
    } else {
      state.fieldEpochs.clear();
      state.secretEpochs.clear();
    }
    clearErrors();
    state.dirty = preservedFields.size > 0 || preservedSecrets.size > 0;
    updateDirtyNotice();
    updateRestartNotice();
  }

  function applyPresentation(path, presentation) {
    if (!presentation || typeof presentation !== 'object') return;
    const source = sourceForPath(path);
    if (source) {
      const label = SOURCE_LABELS[presentation.source] || '来源未知';
      source.textContent = presentation.readOnly && presentation.environmentVariable
        ? `${label} · ${presentation.environmentVariable}` : label;
    }
    const control = controlForPath(path);
    if (presentation.readOnly) {
      state.readOnlyPaths.add(path);
      if (control) control.disabled = true;
    } else if (control) control.disabled = false;

    if (SECRET_PATHS.includes(path)) {
      const secretInput = Array.from(document.querySelectorAll('[data-secret]')).find((node) => node.dataset.secret === path);
      const unavailable = presentation.readOnly || presentation.source === 'environment' || presentation.missing || !presentation.configured;
      if (secretInput) {
        secretInput.disabled = presentation.readOnly || presentation.source === 'environment';
        secretInput.placeholder = presentation.missing
          ? '凭据缺失，请替换' : (presentation.configured ? '留空即保留原凭据' : '尚未配置');
      }
      for (const button of document.querySelectorAll('[data-replace], [data-retain], [data-delete]')) {
        if (button.dataset.replace === path || button.dataset.retain === path || button.dataset.delete === path) button.disabled = presentation.readOnly || presentation.source === 'environment';
      }
      if (unavailable && presentation.missing) {
        const stateNode = Array.from(document.querySelectorAll('[data-secret-state]')).find((node) => node.dataset.secretState === path);
        if (stateNode) stateNode.textContent = '系统凭据库中缺少已保存的凭据';
      }
    }
  }

  function addVoiceOptions(voices) {
    const select = byId('tts-default-voice-id');
    while (select.firstChild) select.removeChild(select.firstChild);
    for (const voice of Array.isArray(voices) ? voices : []) {
      if (!voice || typeof voice.id !== 'string' || typeof voice.name !== 'string') continue;
      const option = document.createElement('option');
      option.value = voice.id;
      option.textContent = voice.description ? `${voice.name} · ${voice.description}` : voice.name;
      select.appendChild(option);
    }
  }

  async function loadAuthenticatedData(sessionGeneration = state.sessionGeneration) {
    const [snapshot, voices] = await Promise.all([
      request(`${API}/config`, { sessionBound: true, sessionGeneration }),
      request(`${API}/voices`, { sessionBound: true, sessionGeneration })
    ]);
    if (sessionGeneration !== state.sessionGeneration) throw new StaleRequestError();
    addVoiceOptions(voices);
    applySnapshot(snapshot);
    showWorkspace();
  }

  function activateSession(session, expectedGeneration) {
    if (expectedGeneration !== state.sessionGeneration) throw new StaleRequestError();
    const generation = advanceSessionGeneration();
    state.authenticated = true;
    state.csrfToken = typeof session.csrfToken === 'string' ? session.csrfToken : null;
    return generation;
  }

  async function initialize() {
    const sessionGeneration = state.sessionGeneration;
    let expectedGeneration = sessionGeneration;
    try {
      const session = await request(`${API}/session`, { sessionBound: true, sessionGeneration });
      if (!session || !session.authenticated) {
        if (sessionGeneration !== state.sessionGeneration) throw new StaleRequestError();
        showAuth(Boolean(session && session.initialized));
        return;
      }
      const activeGeneration = activateSession(session, sessionGeneration);
      expectedGeneration = activeGeneration;
      await loadAuthenticatedData(activeGeneration);
    } catch (error) {
      if (expectedGeneration !== state.sessionGeneration) return;
      if (error instanceof StaleRequestError) return;
      if (error instanceof ApiError && error.status === 401) return;
      showAuth(true, '设置服务暂时不可用，请稍后刷新。');
    }
  }

  async function authenticate(path, password) {
    state.authAttemptGeneration += 1;
    const attemptGeneration = state.authAttemptGeneration;
    const sessionGeneration = state.sessionGeneration;
    let authStatusOwner = createStatusOwner(sessionGeneration);
    state.authBusy = true;
    byId('setup-submit').disabled = true;
    byId('login-submit').disabled = true;
    setTransientStatus('auth-message', '', authStatusOwner);
    let expectedGeneration = sessionGeneration;
    try {
      let session;
      try {
        session = await request(`${API}/${path}`, { method: 'POST', body: { password } });
      } catch (error) {
        if (attemptGeneration !== state.authAttemptGeneration || sessionGeneration !== state.sessionGeneration) {
          throw new StaleRequestError();
        }
        throw error;
      }
      if (attemptGeneration !== state.authAttemptGeneration || sessionGeneration !== state.sessionGeneration) {
        throw new StaleRequestError();
      }
      const activeGeneration = activateSession(session, sessionGeneration);
      expectedGeneration = activeGeneration;
      authStatusOwner = createStatusOwner(activeGeneration);
      byId('setup-password').value = '';
      byId('setup-confirm').value = '';
      byId('login-password').value = '';
      if (state.reauthPending && state.draft) {
        state.reauthPending = false;
        showWorkspace();
      } else {
        state.reauthPending = false;
        await loadAuthenticatedData(activeGeneration);
      }
    } catch (error) {
      if (attemptGeneration !== state.authAttemptGeneration || expectedGeneration !== state.sessionGeneration) {
        throw new StaleRequestError();
      }
      if (path === 'login') byId('login-password').value = '';
      setTransientStatus(
        'auth-message',
        safeMessage(error, path === 'setup' ? '无法建立密码，请稍后重试。' : '登录失败，请稍后重试。'),
        authStatusOwner
      );
      throw error;
    } finally {
      if (attemptGeneration === state.authAttemptGeneration) {
        state.authBusy = false;
        byId('setup-submit').disabled = false;
        byId('login-submit').disabled = false;
      }
    }
  }

  function parseInteger(path, raw) {
    const value = raw.trim();
    if (value === '' || !/^-?\d+$/.test(value)) throw new FieldInputError(path, '请输入完整整数');
    const number = Number(value);
    if (!Number.isSafeInteger(number)) throw new FieldInputError(path, '整数超出可用范围');
    return number;
  }

  function parseIds(path, raw) {
    const value = raw.trim();
    if (!value) return [];
    const parts = value.split(/[，,\s]+/).filter(Boolean);
    const ids = parts.map((part) => {
      if (!/^\d+$/.test(part)) throw new FieldInputError(path, 'QQ 号码只能包含数字');
      const number = Number(part);
      if (!Number.isSafeInteger(number)) throw new FieldInputError(path, 'QQ 号码超出可用范围');
      return number;
    });
    return Array.from(new Set(ids));
  }

  function readControl(path, useEffectiveValue = false) {
    if (!useEffectiveValue && state.readOnlyPaths.has(path)) return valueAt(state.draft, path);
    const control = controlForPath(path);
    if (!control) throw new FieldInputError(path, '缺少配置控件');
    if (control.type === 'checkbox') return control.checked;
    if (path === 'llm.baseUrl' || path === 'llm.model') return control.value.trim() || null;
    if (path === 'qq.allowedGroupIds' || path === 'qq.allowedUserIds') return parseIds(path, control.value);
    if (control.type === 'number') return parseInteger(path, control.value);
    return control.value.trim();
  }

  function secretMutation(path) {
    if (state.readOnlyPaths.has(path)) return { operation: 'retain' };
    const secret = state.secrets[path];
    if (secret.operation === 'replace') {
      if (typeof secret.value !== 'string' || secret.value.length === 0) throw new FieldInputError(path, '请输入新凭据后再替换');
      return { operation: 'replace', value: secret.value };
    }
    if (secret.operation === 'delete') return { operation: 'delete' };
    return { operation: 'retain' };
  }

  function collectDraft() {
    if (!state.draft || typeof state.draft.revision !== 'string') throw new ApiError(0, 'INVALID_RESPONSE', {});
    return {
      revision: state.draft.revision,
      llm: {
        enabled: readControl('llm.enabled'),
        baseUrl: readControl('llm.baseUrl'),
        model: readControl('llm.model'),
        timeoutSeconds: readControl('llm.timeoutSeconds'),
        maxContextMessages: readControl('llm.maxContextMessages'),
        maxContextChars: readControl('llm.maxContextChars'),
        toolCallingEnabled: readControl('llm.toolCallingEnabled'),
        apiKey: secretMutation('llm.apiKey')
      },
      qq: {
        enabled: readControl('qq.enabled'),
        allowedGroupIds: readControl('qq.allowedGroupIds'),
        allowedUserIds: readControl('qq.allowedUserIds'),
        ratePerMinute: readControl('qq.ratePerMinute'),
        rateBurst: readControl('qq.rateBurst'),
        maxConcurrency: readControl('qq.maxConcurrency'),
        actionTimeoutSeconds: readControl('qq.actionTimeoutSeconds'),
        accessToken: secretMutation('qq.accessToken')
      },
      tts: {
        gptSovitsUrl: readControl('tts.gptSovitsUrl'),
        defaultVoiceId: readControl('tts.defaultVoiceId'),
        audioMaxAgeSeconds: readControl('tts.audioMaxAgeSeconds')
      }
    };
  }

  function collectProbe(section) {
    if (!state.draft || typeof state.draft.revision !== 'string') throw new ApiError(0, 'INVALID_RESPONSE', {});
    if (section === 'llm') return {
      revision: state.draft.revision,
      baseUrl: readControl('llm.baseUrl', true) || '',
      model: readControl('llm.model', true) || '',
      apiKey: secretMutation('llm.apiKey')
    };
    if (section === 'qq') return {
      revision: state.draft.revision,
      enabled: readControl('qq.enabled', true),
      allowedGroupIds: readControl('qq.allowedGroupIds', true),
      allowedUserIds: readControl('qq.allowedUserIds', true),
      ratePerMinute: readControl('qq.ratePerMinute', true),
      rateBurst: readControl('qq.rateBurst', true),
      maxConcurrency: readControl('qq.maxConcurrency', true),
      actionTimeoutSeconds: readControl('qq.actionTimeoutSeconds', true),
      accessToken: secretMutation('qq.accessToken')
    };
    return { gptSovitsUrl: readControl('tts.gptSovitsUrl', true) };
  }

  function updateSecretState(path) {
    const node = Array.from(document.querySelectorAll('[data-secret-state]')).find((item) => item.dataset.secretState === path);
    if (!node) return;
    const labels = { retain: '当前操作：保留', replace: '当前操作：替换（保存后生效）', delete: '当前操作：删除（保存后生效）' };
    node.textContent = labels[state.secrets[path].operation];
  }

  function markFieldDirty(path) {
    state.editEpoch += 1;
    if (path) {
      state.fieldEpochs.set(path, state.editEpoch);
      if (SECRET_PATHS.includes(path)) state.secretEpochs.set(path, state.editEpoch);
    }
    state.dirty = true;
    abortProbes();
    updateDirtyNotice();
  }

  function markDirty() {
    markFieldDirty(null);
  }

  function updateDirtyNotice() {
    byId('dirty-notice').textContent = state.dirty ? '有未保存的修改' : '没有未保存的修改';
    byId('dirty-notice').classList.toggle('is-dirty', state.dirty);
  }

  function updateRestartNotice() {
    byId('restart-notice').hidden = !state.restartPending;
  }

  function clearErrors() {
    byId('error-summary').hidden = true;
    byId('error-summary').textContent = '';
    for (const node of document.querySelectorAll('[data-error-for]')) node.textContent = '';
    for (const control of document.querySelectorAll('[aria-invalid="true"]')) control.removeAttribute('aria-invalid');
  }

  function showFieldErrors(fields, summary) {
    clearErrors();
    const paths = Object.keys(fields || {});
    for (const path of paths) {
      const node = errorForPath(path);
      const control = controlForPath(path) || Array.from(document.querySelectorAll('[data-secret]')).find((item) => item.dataset.secret === path);
      if (node) node.textContent = '配置值无效';
      if (control) control.setAttribute('aria-invalid', 'true');
    }
    const box = byId('error-summary');
    box.textContent = summary;
    box.hidden = false;
    box.focus();
    const first = paths.map((path) => controlForPath(path) || Array.from(document.querySelectorAll('[data-secret]')).find((item) => item.dataset.secret === path)).find(Boolean);
    if (first) first.focus();
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (state.saveBusy) return;
    state.saveBusy = true;
    state.saveGeneration += 1;
    const saveGeneration = state.saveGeneration;
    const sessionGeneration = state.sessionGeneration;
    const statusOwner = createStatusOwner(sessionGeneration);
    clearErrors();
    abortProbes();
    const button = byId('save-settings');
    button.disabled = true;
    setTransientStatus('save-status', '正在安全保存…', statusOwner);
    try {
      const draft = collectDraft();
      const context = {
        editEpoch: state.editEpoch,
        secretEpochs: new Map(state.secretEpochs)
      };
      const snapshot = await request(`${API}/config`, {
        method: 'PUT', write: true, body: draft, sessionBound: true, sessionGeneration
      });
      if (sessionGeneration !== state.sessionGeneration || !state.authenticated) return;
      state.restartPending = state.restartPending || snapshot.restartRequired === true;
      mergeSnapshot(snapshot, context);
      setTransientStatus('save-status', '保存成功', statusOwner);
    } catch (error) {
      if (sessionGeneration !== state.sessionGeneration) return;
      if (error instanceof StaleRequestError) return;
      if (error instanceof FieldInputError) {
        showFieldErrors({ [error.path]: error.safeMessage }, error.safeMessage);
      } else if (error instanceof ApiError && error.status === 409) {
        showFieldErrors({}, '配置已在其他位置更新。请刷新页面，核对后再保存；当前内容不会自动覆盖。');
      } else if (!(error instanceof ApiError && error.status === 401)) {
        showFieldErrors(error instanceof ApiError ? error.fields : {}, safeMessage(error, '保存失败，请检查配置后重试。'));
      }
      clearTransientStatus('save-status', statusOwner);
    } finally {
      if (saveGeneration === state.saveGeneration) {
        state.saveBusy = false;
        button.disabled = false;
      }
    }
  }

  async function runProbe(button) {
    const section = button.dataset.test;
    const status = Array.from(document.querySelectorAll('[data-test-status]')).find((node) => node.dataset.testStatus === section);
    abortProbes();
    const generation = state.probeGeneration;
    const sessionGeneration = state.sessionGeneration;
    const statusOwner = createStatusOwner(sessionGeneration);
    const revision = state.draft && state.draft.revision;
    const controller = new AbortController();
    const record = { button, controller, generation, revision, status, statusOwner };
    state.probes.set(section, record);
    button.disabled = true;
    setTransientStatus(status, '正在测试，不会保存配置…', statusOwner);
    try {
      const body = collectProbe(section);
      const result = await request(`${API}/test/${section}`, {
        method: 'POST', write: true, body, signal: controller.signal,
        sessionBound: true, sessionGeneration
      });
      if (state.probes.get(section) !== record || state.probeGeneration !== generation || !state.draft || state.draft.revision !== revision) return;
      let message = result.ok ? '测试成功' : (CODE_MESSAGES[result.code] || '测试未通过，请检查本节配置');
      if (section === 'qq' && result.status && typeof result.status.state === 'string') {
        const states = { disabled: '未启用', misconfigured: '配置异常', disconnected: '未连接', connected: '已连接' };
        message += `；当前运行配置：${states[result.status.state] || '状态未知'}`;
      }
      setTransientStatus(status, message, statusOwner);
    } catch (error) {
      if (state.probes.get(section) !== record || state.probeGeneration !== generation) return;
      if (error instanceof FieldInputError) {
        showFieldErrors({ [error.path]: error.safeMessage }, error.safeMessage);
        setTransientStatus(status, '请修正本节标记的配置', statusOwner);
      } else if (error && error.name === 'AbortError') setTransientStatus(status, '测试已取消', statusOwner);
      else if (!(error instanceof ApiError && error.status === 401)) setTransientStatus(status, safeMessage(error, '测试失败，请稍后重试'), statusOwner);
    } finally {
      if (state.probes.get(section) === record && state.probeGeneration === generation) {
        state.probes.delete(section);
        button.disabled = false;
      }
    }
  }

  function selectTab(name, focus = false) {
    const previous = document.querySelector('.panel.is-active');
    if (previous && previous.dataset.panel !== name) {
      abortProbes();
    }
    for (const tab of document.querySelectorAll('[role="tab"]')) {
      const selected = tab.dataset.tab === name;
      tab.classList.toggle('is-active', selected);
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    }
    for (const panel of document.querySelectorAll('[role="tabpanel"]')) {
      const selected = panel.dataset.panel === name;
      panel.classList.toggle('is-active', selected);
      panel.hidden = !selected;
    }
  }

  const orientationQuery = window.matchMedia('(max-width: 760px)');

  function updateTabOrientation() {
    const tablist = document.querySelector('[role="tablist"]');
    tablist.setAttribute(
      'aria-orientation',
      orientationQuery.matches ? 'horizontal' : 'vertical'
    );
  }

  orientationQuery.addEventListener('change', updateTabOrientation);
  updateTabOrientation();

  function retainSecret(path) {
    if (state.readOnlyPaths.has(path)) return;
    const input = Array.from(document.querySelectorAll('[data-secret]')).find((node) => node.dataset.secret === path);
    if (input) input.value = '';
    state.secrets[path] = { operation: 'retain', value: null };
    updateSecretState(path);
    markFieldDirty(path);
  }

  function completeLogout(expectedGeneration) {
    if (expectedGeneration !== state.sessionGeneration) return false;
    advanceSessionGeneration();
    clearSecretState();
    state.authenticated = false;
    state.csrfToken = null;
    state.reauthPending = false;
    state.draft = null;
    state.dirty = false;
    state.fieldEpochs.clear();
    state.secretEpochs.clear();
    updateDirtyNotice();
    showAuth(true, '已经退出登录。');
    return true;
  }

  async function performLogout() {
    if (state.logoutBusy) return;
    if (state.dirty && !window.confirm('有尚未保存的修改，仍要退出登录吗？')) return;
    state.logoutBusy = true;
    state.logoutGeneration += 1;
    const logoutGeneration = state.logoutGeneration;
    const sessionGeneration = state.sessionGeneration;
    const statusOwner = createStatusOwner(sessionGeneration);
    const button = byId('logout-button');
    button.disabled = true;
    abortProbes();
    try {
      await request(`${API}/logout`, {
        method: 'POST', write: true, body: {}, sessionBound: true, sessionGeneration
      });
      completeLogout(sessionGeneration);
    } catch (error) {
      if (sessionGeneration !== state.sessionGeneration) return;
      if (error instanceof StaleRequestError) return;
      if (!(error instanceof ApiError && error.status === 401)) {
        setTransientStatus('save-status', safeMessage(error, '退出失败，登录状态和未保存修改仍已保留。'), statusOwner);
      }
    } finally {
      if (logoutGeneration === state.logoutGeneration) {
        state.logoutBusy = false;
        if (state.authenticated) button.disabled = false;
      }
    }
  }

  byId('setup-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.authBusy) return;
    const password = byId('setup-password').value;
    if (password !== byId('setup-confirm').value) {
      setTransientStatus('auth-message', '两次输入的密码不一致。', createStatusOwner());
      return;
    }
    const attemptGeneration = state.authAttemptGeneration + 1;
    try { await authenticate('setup', password); }
    catch (error) { if (attemptGeneration !== state.authAttemptGeneration || error instanceof StaleRequestError) return; }
  });

  byId('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.authBusy) return;
    const attemptGeneration = state.authAttemptGeneration + 1;
    try { await authenticate('login', byId('login-password').value); }
    catch (error) {
      if (attemptGeneration !== state.authAttemptGeneration || error instanceof StaleRequestError) return;
    }
  });

  byId('logout-button').addEventListener('click', performLogout);

  byId('settings-form').addEventListener('submit', saveSettings);
  byId('settings-form').addEventListener('input', (event) => {
    if (event.target.matches('[data-secret]')) {
      const path = event.target.dataset.secret;
      state.secrets[path].value = event.target.value;
      markFieldDirty(path);
      return;
    }
    markFieldDirty(event.target.dataset.path || null);
  });
  byId('settings-form').addEventListener('change', (event) => {
    markFieldDirty(event.target.dataset.path || null);
  });

  for (const button of document.querySelectorAll('[data-replace]')) {
    button.addEventListener('click', () => {
      const path = button.dataset.replace;
      const input = Array.from(document.querySelectorAll('[data-secret]')).find((node) => node.dataset.secret === path);
      state.secrets[path].operation = 'replace';
      state.secrets[path].value = input.value;
      updateSecretState(path);
      input.focus();
      markFieldDirty(path);
    });
  }

  for (const button of document.querySelectorAll('[data-retain]')) {
    button.addEventListener('click', () => retainSecret(button.dataset.retain));
  }

  for (const button of document.querySelectorAll('[data-delete]')) {
    button.addEventListener('click', () => {
      const path = button.dataset.delete;
      if (!window.confirm('确认删除这个已保存的凭据吗？保存设置后才会生效。')) return;
      const input = Array.from(document.querySelectorAll('[data-secret]')).find((node) => node.dataset.secret === path);
      input.value = '';
      state.secrets[path].operation = 'delete';
      state.secrets[path].value = null;
      updateSecretState(path);
      markFieldDirty(path);
    });
  }

  for (const button of document.querySelectorAll('[data-test]')) button.addEventListener('click', () => runProbe(button));
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
    tab.addEventListener('keydown', (event) => {
      const orientation = tab.parentElement.getAttribute('aria-orientation');
      const previousKey = orientation === 'horizontal' ? 'ArrowLeft' : 'ArrowUp';
      const nextKey = orientation === 'horizontal' ? 'ArrowRight' : 'ArrowDown';
      if (![previousKey, nextKey, 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
      let index = tabs.indexOf(tab);
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else index = (index + (event.key === nextKey ? 1 : -1) + tabs.length) % tabs.length;
      selectTab(tabs[index].dataset.tab, true);
    });
  }

  window.addEventListener('beforeunload', (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') stopCloudPolling();
    else if (state.authenticated) startCloudPolling();
  });

  initialize();
})();
