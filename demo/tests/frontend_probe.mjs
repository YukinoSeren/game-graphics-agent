/*
 * Headless regression probe for the demo front end.
 *
 * It does not replace a real browser check: it uses a tiny DOM substitute to
 * execute the page modules' real callbacks. Run with:
 *   node demo/tests/frontend_probe.mjs
 */

const results = [];
function check(name, condition, detail = '') {
  results.push({ name, ok: Boolean(condition), detail });
  if (!condition) console.error(`FAIL ${name}${detail ? `: ${detail}` : ''}`);
}

class NodeBase {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.className = '';
    this._text = '';
    this.hidden = false;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.files = [];
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node === null || node === undefined || node === false) continue;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = '';
    this.append(...nodes);
  }
  remove() {
    this.children = [];
  }
  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  async dispatch(type, event = {}) {
    for (const handler of this.listeners[type] || []) {
      await handler({ preventDefault() {}, ...event });
    }
  }
  click() {
    return this.dispatch('click');
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
  }
  getAttribute(key) {
    return this.attributes[key];
  }
  querySelector() {
    return null;
  }
  querySelectorAll() {
    return [];
  }
  closest() {
    return null;
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent ?? '').join('');
  }
  set textContent(value) {
    this.children = [];
    this._text = String(value);
  }
}

globalThis.Node = NodeBase;

const ids = new Map();
globalThis.document = {
  body: new NodeBase('body'),
  createElement: (tag) => new NodeBase(tag),
  createElementNS: (namespace, tag) => new NodeBase(tag),
  createTextNode: (text) => {
    const node = new NodeBase('#text');
    node.textContent = text;
    return node;
  },
  getElementById: (id) => {
    if (!ids.has(id)) ids.set(id, new NodeBase('div'));
    return ids.get(id);
  },
  querySelector: () => null,
  querySelectorAll: () => [],
};
for (const id of ['page-root', 'draft-bar', 'service-status', 'page-list', 'scenario-select']) {
  document.getElementById(id);
}

const storage = new Map();
globalThis.localStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
  key: (index) => Array.from(storage.keys())[index] ?? null,
  get length() {
    return storage.size;
  },
};
globalThis.location = { search: '', hash: '' };

function walk(node, out = []) {
  out.push(node);
  for (const child of node.children || []) walk(child, out);
  return out;
}

function byText(root, text) {
  return walk(root).find((node) => node.textContent.includes(text));
}

function button(root, text) {
  return walk(root).find((node) => node.tagName === 'BUTTON' && node.textContent.includes(text));
}

function contextSnapshot(overrides = {}) {
  return {
    id: 'ctx-1',
    revision: 1,
    game_name: '使命召唤：现代战争 II',
    gpu_name: 'NVIDIA GeForce RTX 4060',
    resolution: { width: 1920, height: 1080 },
    applicability: 'preset',
    unknown_fields: ['cpu'],
    cpu: null,
    memory_gb: null,
    vram_gb: null,
    driver_version: null,
    game_version: null,
    display_refresh_hz: null,
    ...overrides,
  };
}

function makeCtx({
  workspace = {},
  catalog = { context: { scene_tag: '交战' }, settings: [], rules: { protected_options: [] } },
  entities = {},
  replies = {},
  api = {},
  store = {},
} = {}) {
  const remembered = { ...store };
  const sent = [];
  const ctx = {
    scenarioId: 'mw2-rtx4060',
    state: { scenarios: [] },
    workspace: {
      workspace_epoch: 1,
      revision: 1,
      context_id: 'ctx-1',
      current_applied_id: null,
      mode: 'narrated',
      ...workspace,
    },
    catalog,
    recall: (key) => (key in remembered ? remembered[key] : null),
    remember: (key, value) => {
      remembered[key] = value;
    },
    entity: async (id) => entities[id] ?? null,
    entities: async (kind) => Object.values(entities).filter((item) => item.kind === kind),
    send: async (action, payload) => {
      sent.push({ action, payload });
      const reply = replies[action];
      if (typeof reply === 'function') return reply(payload, sent);
      return reply ?? { refs: [], data: {}, task_id: null };
    },
    upload: async () => ({ refs: [{ kind: 'File', id: 'uploaded-file' }] }),
    reload: async () => {},
    navigate: () => {},
    trackTask: () => {},
    toast: () => {},
    badge: () => new NodeBase('span'),
    api: {
      entities: async () => [],
      task: async () => null,
      fileUrl: async () => 'blob:demo',
      ...api,
    },
  };
  return { ctx, remembered, sent };
}

globalThis.__DEMO_NO_AUTOBOOT__ = true;
const appModule = await import('../static/js/app.js');
const performance = await import('../static/js/pages/performance.js');
const knowledge = await import('../static/js/pages/knowledge.js');
const overview = await import('../static/js/pages/overview.js');
const feedback = await import('../static/js/pages/feedback.js');
const forms = await import('../static/js/forms.js');

// The local-information button is a working action, not a static label.
{
  const profile = {
    cpu: 'Intel Core i7-14650HX',
    gpu_name: 'NVIDIA GeForce RTX 4060 Laptop GPU',
    memory_gb: 16,
    vram_gb: 8,
    driver_version: '621.01',
    display_refresh_hz: 165,
    os_name: 'Windows 11 专业版',
    desktop_resolution: { width: 2560, height: 1600 },
  };
  const { ctx, remembered, sent } = makeCtx({
    entities: {
      'ctx-1': contextSnapshot(),
      'device-check-1': { recognized: profile },
    },
    replies: {
      'device.check': {
        refs: [{ kind: 'DeviceScan', id: 'device-check-1' }],
        data: { recognized: profile },
      },
    },
  });
  const root = new NodeBase('div');
  await overview.render(root, ctx);
  const inspect = button(root, '检查本机信息');
  check('device check button is available', Boolean(inspect));
  if (inspect) {
    await inspect.click();
    check('device check sends the dedicated action', sent.some((x) => x.action === 'device.check'));
    check('device check renders CPU and GPU', root.textContent.includes(profile.cpu) && root.textContent.includes(profile.gpu_name));
    check('device check remembers the saved result', remembered.deviceCheckId === 'device-check-1');
    check('device check re-enables after completion', inspect.disabled === false);
    const refreshed = new NodeBase('div');
    await overview.render(refreshed, ctx);
    check('device check result survives page rerender', refreshed.textContent.includes(profile.cpu) && refreshed.textContent.includes(profile.os_name));
  }
}

// R01: an upload association must become the analysis target.
{
  const run = {
    segmentId: 'seg-1',
    segmentRevision: 4,
    bindingRevision: 2,
    segmentStatus: 'closed',
    taskId: null,
  };
  const artifacts = [
    { id: 'cap-art', segment_id: 'seg-1', label: 'recommended', source: 'capture', file_id: 'file-cap', content_hash: 'a'.repeat(64) },
    { id: 'up-art', segment_id: 'seg-1', label: 'uploaded', source: 'uploaded', file_id: 'file-up', content_hash: 'b'.repeat(64) },
  ];
  const { ctx, remembered, sent } = makeCtx({
    workspace: { current_applied_id: 'a1' },
    entities: {
      a1: { id: 'a1', settings: {} },
      'seg-1': { id: 'seg-1', revision: 4, status: 'closed', binding_id: 'bind-1' },
      'bind-1': { id: 'bind-1', revision: 2 },
    },
    store: { performanceRun: run, summaryIds: [] },
    api: {
      entities: async () => artifacts,
      task: async () => null,
    },
    replies: {
      'log.inspect': { refs: [], data: { inspection: { frame_count: 10, duration_s: 1, issues: [], content_hash: 'c'.repeat(64) } } },
      'log.associate': { refs: [{ kind: 'LogArtifact', id: 'up-art-new' }], data: {} },
    },
  });
  const root = new NodeBase('div');
  await performance.render(root, ctx);
  const fileInput = walk(root).find((node) => node.tagName === 'INPUT' && node.getAttribute('type') === 'file');
  fileInput.files = [{ name: 'frames.csv', type: 'text/csv' }];
  await button(root, '上传并预览').click();
  const associate = button(root, '关联到已结束的测试');
  check('R01 upload panel offers association', Boolean(associate));
  await associate.click();
  check('R01 association repoints the analysis artifact', remembered.performanceRun?.artifactId === 'up-art-new', String(remembered.performanceRun?.artifactId));
  check('R01 association triggers log.associate', sent.some((item) => item.action === 'log.associate'));
}

// R02: the background save button submits a real form.
{
  const { ctx, sent } = makeCtx({
    workspace: { current_applied_id: null },
    entities: { 'ctx-1': contextSnapshot() },
  });
  const root = new NodeBase('div');
  await overview.render(root, ctx);
  const form = walk(root).find((node) => node.tagName === 'FORM');
  check('R02 background editor uses a form element', Boolean(form));
  const save = button(root, '保存背景修订');
  check('R02 save button is inside the form', Boolean(save) && walk(form).includes(save));
  await form.dispatch('submit');
  const saved = sent.find((item) => item.action === 'context.save');
  check('R02 form submit sends context.save', Boolean(saved) && saved.payload.fields.gpu_name === 'NVIDIA GeForce RTX 4060');
}

// R06: an interrupted capture can continue, be abandoned, and is not stoppable.
{
  const run = { taskId: 'task-1', segmentId: 'seg-1', segmentStatus: 'open', segmentRevision: 2, bindingRevision: 1 };
  const { ctx, remembered, sent } = makeCtx({
    workspace: { current_applied_id: 'a1' },
    entities: {
      a1: { id: 'a1', settings: {} },
      'seg-1': { id: 'seg-1', revision: 2, status: 'open', binding_id: 'bind-1' },
      'bind-1': { id: 'bind-1', revision: 1 },
    },
    store: { performanceRun: run, summaryIds: [] },
    api: {
      entities: async () => [],
      task: async () => ({ id: 'task-1', status: 'interrupted', phase_index: 0, phases: [{ key: 'wait' }, { key: 'finalize' }], result: {} }),
    },
    replies: { 'task.resume': { refs: [], data: {}, task_id: 'task-2' } },
  });
  const root = new NodeBase('div');
  await performance.render(root, ctx);
  check('R06 terminal capture is not offered as stoppable', !button(root, '提前停止采集'));
  check('R06 interrupted capture offers resume', Boolean(button(root, '继续剩余阶段')));
  check('R06 interrupted capture offers abandon', Boolean(button(root, '放弃本次')));
  await button(root, '继续剩余阶段').click();
  check('R06 resume stores the new task id', remembered.performanceRun?.taskId === 'task-2', String(remembered.performanceRun?.taskId));
  check('R06 resume calls task.resume', sent.some((item) => item.action === 'task.resume'));
}

// R07: step mode exposes explicit task advancement.
{
  const run = { taskId: 'task-3', segmentId: 'seg-1', segmentStatus: 'open', segmentRevision: 2, bindingRevision: 1 };
  const { ctx, sent } = makeCtx({
    workspace: { mode: 'step', current_applied_id: 'a1' },
    entities: {
      a1: { id: 'a1', settings: {} },
      'seg-1': { id: 'seg-1', revision: 2, status: 'open', binding_id: 'bind-1' },
      'bind-1': { id: 'bind-1', revision: 1 },
    },
    store: { performanceRun: run, summaryIds: [] },
    api: {
      entities: async () => [],
      task: async () => ({ id: 'task-3', status: 'running', phase_index: 0, phases: [{ key: 'wait' }] }),
    },
    replies: { 'task.advance': { refs: [], data: {}, task_id: null } },
  });
  const root = new NodeBase('div');
  await performance.render(root, ctx);
  const advance = button(root, '推进下一阶段');
  check('R07 step mode shows an advance action', Boolean(advance));
  await advance.click();
  check('R07 advance calls task.advance', sent.some((item) => item.action === 'task.advance'));
}

// R07b: the knowledge task panel also advances in step mode.
{
  const snapshot = {
    id: 'k-new', status: 'completed', source_ids: [], gap_ids: [], record_ids: [],
    accepted_count: 0, quarantined_count: 0, independent_source_count: 0,
    fingerprint: 'f', revision: 1,
  };
  const { ctx, sent } = makeCtx({
    workspace: { mode: 'step' },
    store: { knowledgeTaskId: 'kt-1', knowledgeId: 'k-new' },
    entities: { 'k-new': snapshot },
    api: {
      task: async () => ({ id: 'kt-1', status: 'running', phase_index: 0, phases: [{ key: 'discover', label: '发现' }] }),
    },
    replies: { 'task.advance': { refs: [], data: {}, task_id: null } },
  });
  const root = new NodeBase('div');
  await knowledge.render(root, ctx);
  const advance = button(root, '继续下一阶段');
  check('R07 knowledge page shows an advance action', Boolean(advance));
  await advance.click();
  check('R07 knowledge advance calls task.advance', sent.some((item) => item.action === 'task.advance'));
}

// R08: a completed collection task must not overwrite a newer knowledge id.
{
  const snapshot = {
    id: 'k-new', status: 'completed', source_ids: [], gap_ids: [], record_ids: [],
    accepted_count: 0, quarantined_count: 0, independent_source_count: 0,
    fingerprint: 'f', revision: 1,
  };
  const oldSnapshot = { ...snapshot, id: 'k-old' };
  const { ctx, remembered } = makeCtx({
    store: { knowledgeTaskId: 'kt-2' },
    entities: { 'k-new': snapshot, 'k-old': oldSnapshot },
    api: {
      task: async () => ({ id: 'kt-2', status: 'completed', phase_index: 1, phases: [{ key: 'discover' }], result: { knowledge_id: 'k-old' } }),
    },
  });
  await knowledge.render(new NodeBase('div'), ctx);
  check('R08 task result is consumed once', remembered.knowledgeId === 'k-old', String(remembered.knowledgeId));
  // The user imports or updates a package after the task finished.
  remembered.knowledgeId = 'k-new';
  await knowledge.render(new NodeBase('div'), ctx);
  check('R08 old task result does not replace the new knowledge id', remembered.knowledgeId === 'k-new', String(remembered.knowledgeId));
}

// R09: a persisted keep result clears an older proposal without prefilled feedback.
{
  const summary = { id: 's-1', kind: 'Summary', label: 'recommended', session_id: 'sess-1', applied_id: 'a1' };
  const analysis = { id: 'analysis', kind: 'FeedbackAnalysis', status: 'completed', task_id: 'task', result: { action: 'keep', change_set_id: null, reason: '满意' } };
  const { ctx } = makeCtx({ workspace: { current_applied_id: 'a1' }, entities: { 's-1': summary, a1: { id: 'a1' }, analysis }, store: { changeSetId: 'old-C1' } });
  const root = new NodeBase('div'); await feedback.render(root, ctx);
  const textarea = walk(root).find((node) => node.tagName === 'TEXTAREA');
  check('R09 feedback text starts empty', textarea.value === '');
  check('R09 persisted keep does not show old change set', !button(root, '记录已按建议调整'));
  check('R09 persisted keep appears without local action selection', root.textContent.includes('保持当前方案'));
}

// R03: a delayed refresh must not write into another scenario's cache.
{
  const app = new appModule.App();
  app.state.scenarioId = 'case-A';
  app.state.workspaces.set('case-A', { scenario_id: 'case-A', workspace_epoch: 1, revision: 1 });
  let resolveRequest;
  app.api = {
    get: () => new Promise((resolve) => {
      resolveRequest = resolve;
    }),
  };
  const pending = app.refresh();
  app.state.scenarioId = 'case-B';
  resolveRequest({ workspace: { scenario_id: 'case-A', workspace_epoch: 1, revision: 7 } });
  await pending;
  check('R03 delayed response is stored under its own case', app.state.workspaces.get('case-A')?.revision === 7);
  check('R03 delayed response does not create a case-B cache', !app.state.workspaces.has('case-B'));
}

// R05: a replayed reply still restores the business pointers.
{
  const app = new appModule.App();
  app.state.scenarioId = 'case-A';
  app.applyReply(
    { action: 'recommendation.generate', payload: { knowledge_id: 'k-1' } },
    { refs: [{ kind: 'Recommendation', id: 'rec-1' }] },
  );
  check('R05 retried reply restores the recommendation pointer', app.recall('recommendationId') === 'rec-1');
  check('R05 retried reply restores its knowledge source', app.recall('recommendationGeneratedFrom') === 'k-1');
}

// R04: persisted drafts are listed at selection time and shown for retry.
{
  const app = new appModule.App();
  app.state.scenarioId = 'case-A';
  app.drafts = {
    list: async () => [
      { requestId: 'draft-1', scenarioId: 'case-A', epoch: 1, createdAt: 1, action: 'plan.save', payload: { scene: '交战' }, status: 'network_error' },
    ],
  };
  await app.restoreDrafts({ workspace_epoch: 1 });
  const bar = document.getElementById('draft-bar');
  check('R04 persisted draft is recovered', bar.textContent.includes('你的输入和已整理内容都还在'));
  check('R04 recovered draft shows its input', app.lastDraft?.payload.scene === '交战');
  check('R04 recovered draft offers the same request key retry', Boolean(button(bar, '重试同一请求')));
  check('R04 draft retry keeps the original key', app.lastDraft?.requestId === 'draft-1');
}

const failed = results.filter((item) => !item.ok);
console.log(`frontend probe: ${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) process.exitCode = 1;
