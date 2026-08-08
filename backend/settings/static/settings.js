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
    saveGeneration: 0,
    saveBusy: false,
    logoutBusy: false,
    probeGeneration: 0,
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

  const byId = (id) => document.getElementById(id);
  const controls = () => Array.from(document.querySelectorAll('[data-path]'));
  const controlForPath = (path) => controls().find((node) => node.dataset.path === path) || null;
  const sourceForPath = (path) => Array.from(document.querySelectorAll('[data-source-for]')).find((node) => node.dataset.sourceFor === path) || null;
  const errorForPath = (path) => Array.from(document.querySelectorAll('[data-error-for]')).find((node) => node.dataset.errorFor === path) || null;

  function safeMessage(error, fallback) {
    if (error instanceof FieldInputError) return error.safeMessage;
    if (error instanceof ApiError) return CODE_MESSAGES[error.code] || fallback;
    return fallback;
  }

  async function request(path, options = {}) {
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
      if (error && error.name === 'AbortError') throw error;
      throw new ApiError(0, 'NETWORK_ERROR', {});
    }
    let payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (response.status === 401) {
      expireSession();
      throw new ApiError(401, 'SETTINGS_UNAUTHORIZED', {});
    }
    if (!response.ok) {
      const detail = payload && payload.error && typeof payload.error === 'object' ? payload.error : {};
      throw new ApiError(response.status, typeof detail.code === 'string' ? detail.code : 'REQUEST_FAILED', detail.fields);
    }
    return payload;
  }

  function abortProbes() {
    state.probeGeneration += 1;
    for (const record of state.probes.values()) {
      record.controller.abort();
      record.button.disabled = false;
      if (record.status) record.status.textContent = '测试已过期';
    }
    state.probes.clear();
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

  function expireSession() {
    state.sessionGeneration += 1;
    state.authenticated = false;
    state.csrfToken = null;
    state.reauthPending = true;
    abortProbes();
    clearSecretState();
    showAuth(true, '登录已过期，非敏感草稿仍为你保留，请重新登录。');
  }

  function showAuth(initialized, message = '') {
    byId('auth-panel').hidden = false;
    byId('workspace').hidden = true;
    byId('action-bar').hidden = true;
    byId('setup-form').hidden = initialized;
    byId('login-form').hidden = !initialized;
    byId('auth-title').textContent = initialized ? '解锁设置簿' : '建立本机设置密码';
    byId('auth-description').textContent = initialized
      ? '输入本机设置密码继续。'
      : '第一次使用时，请设置一个只用于本机配置的密码。';
    byId('auth-message').textContent = message;
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

  async function loadAuthenticatedData() {
    const [snapshot, voices] = await Promise.all([
      request(`${API}/config`),
      request(`${API}/voices`)
    ]);
    addVoiceOptions(voices);
    applySnapshot(snapshot);
    showWorkspace();
  }

  async function initialize() {
    try {
      const session = await request(`${API}/session`);
      if (!session || !session.authenticated) {
        showAuth(Boolean(session && session.initialized));
        return;
      }
      state.authenticated = true;
      state.csrfToken = typeof session.csrfToken === 'string' ? session.csrfToken : null;
      await loadAuthenticatedData();
    } catch (_) {
      showAuth(true, '设置服务暂时不可用，请稍后刷新。');
    }
  }

  async function authenticate(path, password) {
    const session = await request(`${API}/${path}`, { method: 'POST', body: { password } });
    state.authenticated = true;
    state.csrfToken = typeof session.csrfToken === 'string' ? session.csrfToken : null;
    byId('setup-password').value = '';
    byId('setup-confirm').value = '';
    byId('login-password').value = '';
    if (state.reauthPending && state.draft) {
      state.reauthPending = false;
      showWorkspace();
    } else {
      state.reauthPending = false;
      await loadAuthenticatedData();
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
    clearErrors();
    abortProbes();
    const button = byId('save-settings');
    button.disabled = true;
    byId('save-status').textContent = '正在安全保存…';
    try {
      const draft = collectDraft();
      const context = {
        editEpoch: state.editEpoch,
        secretEpochs: new Map(state.secretEpochs)
      };
      const snapshot = await request(`${API}/config`, { method: 'PUT', write: true, body: draft });
      if (sessionGeneration !== state.sessionGeneration || !state.authenticated) return;
      state.restartPending = state.restartPending || snapshot.restartRequired === true;
      mergeSnapshot(snapshot, context);
      byId('save-status').textContent = '保存成功';
    } catch (error) {
      if (error instanceof FieldInputError) {
        showFieldErrors({ [error.path]: error.safeMessage }, error.safeMessage);
      } else if (error instanceof ApiError && error.status === 409) {
        showFieldErrors({}, '配置已在其他位置更新。请刷新页面，核对后再保存；当前内容不会自动覆盖。');
      } else if (!(error instanceof ApiError && error.status === 401)) {
        showFieldErrors(error instanceof ApiError ? error.fields : {}, safeMessage(error, '保存失败，请检查配置后重试。'));
      }
      byId('save-status').textContent = '';
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
    const revision = state.draft && state.draft.revision;
    const controller = new AbortController();
    const record = { button, controller, generation, revision, status };
    state.probes.set(section, record);
    button.disabled = true;
    status.textContent = '正在测试，不会保存配置…';
    try {
      const body = collectProbe(section);
      const result = await request(`${API}/test/${section}`, {
        method: 'POST', write: true, body, signal: controller.signal
      });
      if (state.probes.get(section) !== record || state.probeGeneration !== generation || !state.draft || state.draft.revision !== revision) return;
      let message = result.ok ? '测试成功' : (CODE_MESSAGES[result.code] || '测试未通过，请检查本节配置');
      if (section === 'qq' && result.status && typeof result.status.state === 'string') {
        const states = { disabled: '未启用', misconfigured: '配置异常', disconnected: '未连接', connected: '已连接' };
        message += `；当前运行配置：${states[result.status.state] || '状态未知'}`;
      }
      status.textContent = message;
    } catch (error) {
      if (state.probes.get(section) !== record || state.probeGeneration !== generation) return;
      if (error instanceof FieldInputError) {
        showFieldErrors({ [error.path]: error.safeMessage }, error.safeMessage);
        status.textContent = '请修正本节标记的配置';
      } else if (error && error.name === 'AbortError') status.textContent = '测试已取消';
      else if (!(error instanceof ApiError && error.status === 401)) status.textContent = safeMessage(error, '测试失败，请稍后重试');
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

  function completeLogout() {
    state.sessionGeneration += 1;
    state.saveGeneration += 1;
    state.saveBusy = false;
    abortProbes();
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
  }

  async function performLogout() {
    if (state.logoutBusy) return;
    if (state.dirty && !window.confirm('有尚未保存的修改，仍要退出登录吗？')) return;
    state.logoutBusy = true;
    const button = byId('logout-button');
    button.disabled = true;
    abortProbes();
    try {
      await request(`${API}/logout`, { method: 'POST', write: true, body: {} });
      completeLogout();
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        byId('save-status').textContent = safeMessage(error, '退出失败，登录状态和未保存修改仍已保留。');
      }
    } finally {
      state.logoutBusy = false;
      if (state.authenticated) button.disabled = false;
    }
  }

  byId('setup-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const password = byId('setup-password').value;
    if (password !== byId('setup-confirm').value) {
      byId('auth-message').textContent = '两次输入的密码不一致。';
      return;
    }
    try { await authenticate('setup', password); }
    catch (error) { byId('auth-message').textContent = safeMessage(error, '无法建立密码，请稍后重试。'); }
  });

  byId('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try { await authenticate('login', byId('login-password').value); }
    catch (error) { byId('login-password').value = ''; byId('auth-message').textContent = safeMessage(error, '登录失败，请稍后重试。'); }
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

  initialize();
})();
