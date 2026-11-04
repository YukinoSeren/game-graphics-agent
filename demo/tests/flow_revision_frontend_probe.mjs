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
  querySelector(selector) {
    return walk(this).find((node) => node !== this && (
      selector === 'button' ? node.tagName === 'BUTTON' :
      selector === '[data-draft-key="feedbackDraft"]' ? node.dataset?.draftKey === 'feedbackDraft' :
      selector.startsWith('.') && !selector.includes(' ') ? node.className.split(' ').includes(selector.slice(1)) : false
    )) || null;
  }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  focus() { document.activeElement = this; }
  get isConnected() { return true; }
  get firstChild() { return this.children[0]; }
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

const library = await import('../static/js/pages/library.js');
const recommendation = await import('../static/js/pages/recommendation.js');
const { recentLogsPanel } = await import('../static/js/recent-logs.js');
const { requireCompatibleService } = await import('../static/js/api.js');
const { readFile } = await import('node:fs/promises');
const fixtureSources = JSON.parse(await readFile(new URL('../fixtures/mw2-rtx4060/sources.json', import.meta.url), 'utf8'));
const sourceRows = Array.isArray(fixtureSources) ? fixtureSources : fixtureSources.sources;
const fixtureSettings = JSON.parse(await readFile(new URL('../fixtures/mw2-rtx4060/settings.json', import.meta.url), 'utf8'));
const settings = Array.isArray(fixtureSettings) ? fixtureSettings : fixtureSettings.settings;
const sources = sourceRows.map((source, index) => ({ ...source, id: `source-${index}`, kind: 'Source' }));
const knowledgeSnapshot = { id: 'know', kind: 'KnowledgeSnapshot', status: 'completed', source_ids: sources.map((source) => source.id), gap_ids: [] };
const card = { id: 'card', kind: 'GameCard', knowledge_id: 'know', recommendation_id: 'rec', context_id: 'ctx-1', game_name: '使命召唤：现代战争 I' };
const entityMap = Object.fromEntries([...sources, knowledgeSnapshot, card].map((item) => [item.id, item]));
{
  let rejected = false; try { requireCompatibleService({ ui_contract: 'conversation/1', capabilities: ['feedback.review'] }); } catch { rejected = true; }
  check('legacy service rejected', rejected);
  requireCompatibleService({ ui_contract: 'conversation/2', capabilities: ['device.check', 'intent.submit', 'log.capture_recent', 'feedback.review'] });
}
{
  const { ctx } = makeCtx({ entities: entityMap, catalog: { settings, rules: {} } });
  const root = new NodeBase('div'); await knowledge.render(root, ctx);
  check('completed library has fifteen cards', walk(root).filter((node) => node.className === 'source-card').length === 15);
  const channels = [...new Set(sources.map((source) => source.channel))];
  check('completed library has six channels', channels.length === 6);
  await button(root, channels[0]).click();
  check('channel filtering preserves subset', walk(root).filter((node) => node.className === 'source-card').length === sources.filter((source) => source.channel === channels[0]).length);
  check('all source bodies readable', sources.every((source) => knowledge.sourceBody(source).textContent.length >= 300));
  check('new source bodies avoid unresolved placeholders', sources.every((source) => !/待确认|待核验|待核对|需要复核|需要确认|资料未核实/.test(knowledge.sourceBody(source).textContent)));
}
{
  const { ctx } = makeCtx({ entities: entityMap }); const root = new NodeBase('div'); await library.render(root, ctx);
  const links = walk(root).filter((node) => node.tagName === 'NAV').map((node) => node.textContent).join('');
  check('five ordered game links', /设备与背景→资料与依据→设置详情→游玩日志→体验反馈/.test(links));
}
{
  const rec = { id: 'rec', kind: 'Recommendation', label: '平衡方案', settings: [{ setting_id: 'mw2.particle_quality', display_name: '粒子质量', value: 'high', group: '特效', evidence_ids: sources.slice(0,2).map((item) => item.id), evidence_decision: { source_views: sources.slice(0,2).map((item) => ({ source_id: item.id, source_key: item.source_key, view: '逐项对照画面取舍' })), alternatives: [{ value: 'low', benefit: '减少特效负担', cost: '场景丰富度减少', not_selected_reason: '先保留丰富感' }], device_context: { gpu: 'RTX4060', resolution: '1080p' }, decision: '保留高粒子', tradeoff: '密集场景可能增加负担', user_goal_fit: '保持丰富度', revisit_when: '爆炸时卡顿' } }] };
  const { ctx } = makeCtx({ entities: { ...entityMap, rec }, catalog: { settings, rules: {}, templates: {} } });
  const root = new NodeBase('div'); await recommendation.render(root, ctx);
  check('card restores recommendation with empty local selection', root.textContent.includes('粒子质量'));
  check('no generation or separate why controls', !/生成主方案|重新生成|为什么这样设置/.test(root.textContent));
  await button(root, '查看依据').click();
  const evidence = document.body.children.at(-1);
  check('rich evidence combines source views and decision', evidence.textContent.includes('逐项对照画面取舍') && evidence.textContent.includes('最终决定') && evidence.textContent.includes('画面与流畅度取舍'));
}
{
  const logs = ['last-play', 'previous-play', 'short-play'].map((id) => ({ id, scene: '密集交战', ended_at: '2026-11-03T10:00:00+08:00', duration_s: 60 }));
  const { ctx, sent } = makeCtx({ api: { recentLogs: async () => logs } });
  const root = await recentLogsPanel(ctx, { id: 'applied' }, {});
  const radios = walk(root).filter((node) => node.tagName === 'INPUT');
  check('three native radios', radios.length === 3 && radios.every((node) => node.attributes.type === 'radio' || node.type === 'radio'));
  radios[1].checked = true; await radios[1].dispatch('change');
  check('radio change does not read log', sent.length === 0);
  await button(root, '读取游玩日志').click();
  check('read selected historic log id', sent[0]?.payload.log_id === 'previous-play');
}
{
  const summary = { id: 'sum', kind: 'Summary', session_id: 'sess', applied_id: 'applied' };
  const applied = { id: 'applied', kind: 'AppliedConfiguration' };
  const { ctx, sent } = makeCtx({ workspace: { current_applied_id: 'applied' }, entities: { sum: summary, applied }, catalog: { settings, rules: { protected_options: [] } }, replies: { 'feedback.review': { refs: [{ kind: 'FeedbackAnalysis', id: 'analysis' }], data: {}, task_id: 'task' } } });
  const root = new NodeBase('div'); await feedback.render(root, ctx);
  const textarea = walk(root).find((node) => node.tagName === 'TEXTAREA'); textarea.value = '爆炸时画面卡顿';
  await walk(root).find((node) => node.tagName === 'FORM').dispatch('submit');
  check('atomic feedback review only', sent.length === 1 && sent[0].action === 'feedback.review');
  check('feedback binds immutable input ids', sent[0].payload.summary_id === 'sum' && sent[0].payload.applied_id === 'applied' && sent[0].payload.session_id === 'sess');
  check('feedback defaults protect texture and resolution', ['mw2.texture_detail', 'mw2.texture_filtering', 'mw2.render_resolution'].every((id) => sent[0].payload.protected_setting_ids.includes(id)));
  const analysis = { id: 'analysis', kind: 'FeedbackAnalysis', status: 'running', task_id: 'task' };
  const restored = makeCtx({ entities: { analysis }, api: { task: async () => ({ status: 'running', phases: [{ label: '对照记录' }], phase_index: 0 }) } });
  const waitingRoot = new NodeBase('div'); await feedback.render(waitingRoot, restored.ctx);
  check('running review restores from server without local state', waitingRoot.textContent.includes('预计需要 30 秒') && !waitingRoot.textContent.includes('建议调整'));
}
{
  const conversation = await import('../static/js/pages/conversation.js');
  const { ctx } = makeCtx(); const root = new NodeBase('div'); await conversation.render(root, ctx);
  check('initial homepage has no game-specific content or card', !/使命召唤|现代战争|地平线/.test(root.textContent) && !walk(root).some((node) => node.className === 'conversation-game-card'));
}
{
  const applied = { id: 'new-applied' };
  const summary = { id: 'sum-new', kind: 'Summary', session_id: 'sess', applied_id: applied.id };
  const changeSet = { id: 'change', changes: { 'mw2.particle_quality': { from: 'high', to: 'medium' } }, base_applied_id: 'old-applied', recovery: { note: '手动恢复' }, evidence_ids: [sources[6].id, sources[10].id], protected_setting_ids: [] };
  const analysis = { id: 'analysis', kind: 'FeedbackAnalysis', status: 'completed', result: { action: 'adjust', reason: '建议降低粒子质量', change_set_id: 'change' } };
  const { ctx } = makeCtx({ workspace: { current_applied_id: applied.id }, entities: { ...entityMap, applied: applied, 'new-applied': applied, 'sum-new': summary, change: changeSet, analysis }, catalog: { settings, rules: {} } });
  const root = new NodeBase('div'); await feedback.render(root, ctx);
  check('stale change cannot be applied repeatedly', button(root, '配置已变化，请重新分析')?.disabled === true);
  await button(root, '查看调整依据').click();
  check('feedback evidence includes actual related source titles', document.body.children.at(-1).textContent.includes(sources[6].title) && document.body.children.at(-1).textContent.includes(sources[10].title));
}
check('structured evidence has no object coercion', !document.body.textContent.includes('[object Object]'));
// A terminal task and its hashchange must not replace a newly edited form twice.
{
  const app = new appModule.App(); app.state.page = 'performance';
  let renders = 0; app.render = async () => { renders += 1; };
  app.navigate('feedback');
  app.navigate('feedback', { updateHash: false });
  check('same-page hashchange does not queue a second form render', renders === 1);
}
{
  const originalInterval = globalThis.setInterval;
  let tick;
  globalThis.setInterval = (callback) => { tick = callback; return 1; };
  try {
    const app = new appModule.App(); app.state.scenarioId = 'mw2-rtx4060';
    app.state.workspaces.set('mw2-rtx4060', { workspace_epoch: 1 });
    app.api = { task: async () => ({ status: 'completed', phase_index: 2, result: {} }) };
    let renders = 0; app.render = async () => { renders += 1; };
    app.trackTask('finished-task'); await tick(); await tick();
    check('terminal task stops polling before its single final render', renders === 1 && app.trackedTask === null);
  } finally { globalThis.setInterval = originalInterval; }
}
// Preserve an edit made while a background render awaits its change-set fetch.
{
  const app = new appModule.App(); app.state.page = 'feedback'; app.state.scenarioId = 'mw2-rtx4060';
  app.state.workspaces.set('mw2-rtx4060', { workspace_epoch: 1, context_id: 'ctx-1', current_applied_id: 'applied' });
  app.state.catalogs.set('mw2-rtx4060', { settings, rules: {} });
  const summary = { id: 'sum', kind: 'Summary', session_id: 'sess', applied_id: 'applied' };
  const analysis = { id: 'review', kind: 'FeedbackAnalysis', status: 'completed', result: { action: 'adjust', change_set_id: 'change', reason: '降低粒子负担' } };
  const change = { id: 'change', base_applied_id: 'applied', changes: { 'mw2.particle_quality': { from: 'high', to: 'medium' } }, recovery: {}, evidence_ids: [], protected_setting_ids: [] };
  const data = { card, sum: summary, review: analysis, applied: { id: 'applied' } };
  let reachedChangeSet, finishChangeSet;
  const renderingChangeSet = new Promise((resolve) => { reachedChangeSet = resolve; });
  const waitChangeSet = new Promise((resolve) => { finishChangeSet = resolve; });
  app.api = {
    entities: async (kind) => Object.values(data).filter((item) => item.kind === kind),
    entity: async (id) => { if (id === 'change') { reachedChangeSet(); return waitChangeSet; } return data[id] || null; },
  };
  const main = document.getElementById('page-root'); main.dataset.page = 'feedback';
  const oldInput = new NodeBase('textarea'); oldInput.dataset.draftKey = 'feedbackDraft'; oldInput.value = '原草稿';
  oldInput.addEventListener('input', () => app.remember('feedbackDraft', oldInput.value));
  main.replaceChildren(oldInput); app.remember('feedbackDraft', '原草稿'); oldInput.focus();
  const rendering = app.render(); await renderingChangeSet;
  oldInput.value = '爆炸时画面卡顿'; await oldInput.dispatch('input');
  finishChangeSet(change); await rendering;
  const replacement = main.querySelector('[data-draft-key="feedbackDraft"]');
  check('in-flight background render preserves latest feedback edit', replacement?.value === '爆炸时画面卡顿');
  check('background render preserves textarea focus', document.activeElement === replacement);
}
{
  const summary = { id: 'cached', kind: 'Summary', label: 'adjusted', applied_id: 'applied', session_id: 'session', artifact_id: 'artifact', quality: 'usable', metrics: { average_present_rate: 60, interval_p95_ms: 17, interval_p99_ms: 20, slowest_1pct_fps: 40, interval_count: 100, quality: 'usable', quality_reason: '演示样例满足本地统计的最小样本条件。' } };
  const { ctx } = makeCtx({ workspace: { current_applied_id: 'applied' }, entities: { cached: summary, applied: { id: 'applied' }, artifact: { id: 'artifact', source: 'recent_log' }, session: { id: 'session', run_conditions: { virtual_log_id: 'previous-play' } } }, store: { lastSummaryId: 'cached' } });
  const root = new NodeBase('div'); await performance.render(root, ctx);
  const badges = walk(root).filter((node) => node.className.startsWith('badge'));
  check('recent log labels actual applied relationship rather than recipe', badges.some((node) => node.textContent === '当前确认设置') && !badges.some((node) => node.textContent === '调整后设置'));
  check('normal log render does not introduce unresolved terms', !/待确认|待核验|待核对|需要复核|需要确认|资料未核实/.test(root.textContent));
}
// Restored analyses can outlive their task queue records; offer a fresh submission.
for (const status of ['running', 'interrupted']) {
  const analysis = { id: 'restored', kind: 'FeedbackAnalysis', status, task_id: 'missing-task', feedback_snapshot: { raw_text: '爆炸时画面卡顿' } };
  const summary = { id: 'restored-summary', kind: 'Summary', session_id: 'session', applied_id: 'applied' };
  const { ctx, sent } = makeCtx({ workspace: { current_applied_id: 'applied' }, entities: { analysis, summary, applied: { id: 'applied' } }, catalog: { settings, rules: {} }, api: { task: async () => { throw new Error('task not found'); } }, replies: { 'feedback.review': { refs: [], task_id: 'fresh-task', data: {} } } });
  const root = new NodeBase('div'); await feedback.render(root, ctx);
  check(`orphan ${status} review offers restored text instead of waiting`, root.textContent.includes('分析记录已恢复，请重新提交反馈') && walk(root).find((node) => node.tagName === 'TEXTAREA')?.value === '爆炸时画面卡顿');
  check(`orphan ${status} review has no invalid resume button`, !button(root, '继续分析') && !button(root, '正在分析…'));
  const form = walk(root).find((node) => node.tagName === 'FORM');
  if (form) await form.dispatch('submit');
  check(`orphan ${status} review resubmits atomically`, sent.length === 1 && sent[0].action === 'feedback.review' && sent[0].payload.raw_text === '爆炸时画面卡顿');
}
{
  const app = new appModule.App(); app.state.page = 'feedback'; app.state.scenarioId = 'mw2-rtx4060';
  app.state.workspaces.set('mw2-rtx4060', { workspace_epoch: 1, context_id: 'ctx-1', current_applied_id: 'applied' });
  app.state.catalogs.set('mw2-rtx4060', { settings, rules: {} });
  const analysis = { id: 'orphan', kind: 'FeedbackAnalysis', status: 'running', task_id: 'missing-task', feedback_snapshot: { raw_text: '恢复后的体验原话' } };
  const summary = { id: 'orphan-summary', kind: 'Summary', session_id: 'session', applied_id: 'applied' };
  const entities = { card, analysis, summary, applied: { id: 'applied' } };
  app.trackTask = () => {}; app.remember('feedbackDraft', null);
  app.api = { entities: async (kind) => Object.values(entities).filter((item) => item.kind === kind), entity: async (id) => entities[id] || null, task: async () => { throw new Error('task not found'); } };
  await app.render();
  check('app render commit preserves restored server feedback without local draft', document.getElementById('page-root').querySelector('[data-draft-key="feedbackDraft"]')?.value === '恢复后的体验原话');
}
const failures = results.filter((row) => !row.ok);
console.log(JSON.stringify({ checks: results.length, failures }, null, 2));
process.exitCode = failures.length ? 1 : 0;

