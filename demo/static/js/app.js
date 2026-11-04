import { ApiError, createApi, requireCompatibleService } from './api.js';
import { openDraftStore } from './drafts.js';
import { acceptSnapshot, createState } from './state.js';
import { h, toast, badge } from './components.js';
import * as knowledge from './pages/knowledge.js';
import * as recommendation from './pages/recommendation.js';
import * as performance from './pages/performance.js';
import * as feedback from './pages/feedback.js';
import * as history from './pages/history.js';
import * as conversation from './pages/conversation.js';
import * as library from './pages/library.js';
import * as background from './pages/background.js';

const PAGES = [
  { id: 'conversation', label: '与助手对话', module: conversation, primary: true },
  { id: 'library', label: '我的游戏', module: library, primary: true },
  { id: 'recommendation', label: '设置详情', module: recommendation },
  { id: 'performance', label: '游玩日志', module: performance },
  { id: 'feedback', label: '体验反馈', module: feedback },
  { id: 'knowledge', label: '资料与依据', module: knowledge },
  { id: 'background', label: '设备与背景', module: background },
  { id: 'history', label: '历史记录', module: history, primary: true },
];

let fieldSeq = 0;

function associateLabels(root) {
  for (const field of root.querySelectorAll('.field')) {
    const label = field.querySelector(':scope > label');
    const control = field.querySelector('input, select, textarea');
    if (!label || !control || control.closest('label')) continue;
    if (!control.id) control.id = `demo-field-${++fieldSeq}`;
    label.htmlFor = control.id;
  }
}

export class App {
  constructor() {
    this.api = createApi();
    this.drafts = openDraftStore();
    this.state = createState();
    this.draftFallback = null;
    this.lastDraft = null;
    this.trackedTask = null;
    this.pollTimer = null;
    this.rendering = false;
    this.renderAgain = false;
    this.selectionToken = 0;
    this.wakeTimer = null;
    this.wakeDeadline = null;
  }

  async init() {
    const bootstrap = await this.api.get('/bootstrap');
    requireCompatibleService(bootstrap);
    this.state.scenarios = bootstrap.scenarios;
    const first = this.state.scenarios[0];
    if (!first) throw new Error('当前没有可用的游戏资料。');
    this.buildShell();
    await this.selectScenario(first.scenario_id);
  }

  buildShell() {
    const addPages = (nav, pages) => nav.replaceChildren(...pages.map((page) =>
        h(
          'li',
          {
            dataset: { page: page.id },
            onClick: () => this.navigate(page.id),
          },
          h('button', { type: 'button', class: 'nav-link', text: page.label }),
        ),
      ));
    addPages(document.getElementById('page-list'), PAGES.filter((page) => page.primary));
    addPages(document.getElementById('more-page-list'), PAGES.filter((page) => !page.primary));
    window.addEventListener('hashchange', () => {
      const page = location.hash.replace('#', '') || 'conversation';
      if (PAGES.some((item) => item.id === page)) this.navigate(page, { updateHash: false });
    });
  }

  scenarioId() {
    return this.state.scenarioId;
  }

  workspace() {
    return this.state.workspaces.get(this.state.scenarioId);
  }

  catalog() {
    return this.state.catalogs.get(this.state.scenarioId);
  }

  async selectScenario(scenarioId) {
    if (!scenarioId) return;
    const token = ++this.selectionToken;
    this.state.scenarioId = scenarioId;
    this.trackedTask = null;
    this.lastDraft = null;
    this.hideDraftBar();
    const workspace = await this.api.get(`/workspaces/${encodeURIComponent(scenarioId)}`);
    const previous = this.state.workspaces.get(scenarioId);
    if (acceptSnapshot(previous, workspace.workspace)) {
      this.state.workspaces.set(scenarioId, workspace.workspace);
    }
    if (token !== this.selectionToken) return;
    if (!this.state.catalogs.has(scenarioId)) {
      const catalog = await this.api.get(`/catalogs/${encodeURIComponent(scenarioId)}`);
      if (token !== this.selectionToken) return;
      this.state.catalogs.set(scenarioId, catalog.catalog);
    }
    const page = location.hash.replace('#', '');
    if (PAGES.some((item) => item.id === page)) this.state.page = page;
    await this.render();
    if (token !== this.selectionToken) return;
    await this.restoreDrafts(this.state.workspaces.get(scenarioId));
  }

  async refresh(scenarioId = this.state.scenarioId) {
    // Capture the key before awaiting: a scenario switch while the request is
    // in flight must not write the response into the other scenario's cache.
    const current = this.state.workspaces.get(scenarioId);
    const response = await this.api.get(`/workspaces/${encodeURIComponent(scenarioId)}`);
    const workspace = response.workspace;
    if (!workspace || (workspace.scenario_id && workspace.scenario_id !== scenarioId)) {
      return current;
    }
    if (acceptSnapshot(current, workspace)) {
      this.state.workspaces.set(scenarioId, workspace);
    }
    return workspace;
  }

  async restoreDrafts(workspace) {
    let drafts = [];
    try {
      drafts = await this.drafts.list();
    } catch {
      return;
    }
    if (!Array.isArray(drafts) || !drafts.length) return;
    const scenarioId = this.state.scenarioId;
    const epoch = workspace?.workspace_epoch;
    const sameCase = drafts.filter(
      (draft) => draft && draft.scenarioId === scenarioId && typeof draft.createdAt === 'number',
    );
    const current = sameCase
      .filter((draft) => draft.epoch === epoch)
      .sort((a, b) => a.createdAt - b.createdAt);
    const stale = sameCase.filter((draft) => draft.epoch !== epoch);
    if (current.length) {
      const draft = current[current.length - 1];
      this.lastDraft = draft;
      const notes = [];
      if (current.length > 1) notes.push(`另有 ${current.length - 1} 条未完成操作`);
      if (stale.length) notes.push(`另有 ${stale.length} 条较早的操作记录`);
      this.showDraftBar(draft, { code: draft.status || 'unknown' }, notes.join('；') || null);
    } else if (stale.length) {
      toast(`有 ${stale.length} 条较早的操作记录已保留。`, 'warn');
    }
  }

  applyReply(draft, reply) {
    if (!draft || !reply) return;
    const scenarioId = draft.scenarioId || this.state.scenarioId;
    const refs = Array.isArray(reply.refs) ? reply.refs : [];
    const ids = (kind) => refs.filter((ref) => ref.kind === kind).map((ref) => ref.id);
    const first = (kind) => ids(kind)[0];
    const remember = (key, value) => this.rememberFor(scenarioId, key, value);
    const recall = (key) => this.recallFor(scenarioId, key);

    switch (draft.action) {
      case 'device.check':
        if (first('DeviceScan')) remember('deviceCheckId', first('DeviceScan'));
        break;
      case 'intent.submit':
        if (first('DeviceScan')) remember('deviceCheckId', first('DeviceScan'));
        if (first('GameIntent')) remember('gameIntentId', first('GameIntent'));
        if (first('GameCard')) remember('gameCardId', first('GameCard'));
        break;
      case 'knowledge.collect':
        if (reply.task_id) remember('knowledgeTaskId', reply.task_id);
        break;
      case 'session.create': {
        const data = reply.data || {};
        if (data.segment_id) {
          const run = recall('performanceRun') || {};
          run.sessionId = first('Session') || run.sessionId;
          run.segmentId = data.segment_id;
          run.segmentRevision = data.segment_revision;
          run.bindingId = data.binding_id;
          run.bindingRevision = data.binding_revision;
          run.segmentStatus = 'open';
          remember('performanceRun', run);
        }
        break;
      }
      case 'capture.start': {
        const run = this.recall('performanceRun') || {};
        const payload = draft.payload || {};
        run.sessionId = payload.session_id ?? run.sessionId;
        run.segmentId = payload.segment_id ?? run.segmentId;
        run.segmentRevision = payload.expected_segment_revision ?? run.segmentRevision;
        run.bindingRevision = payload.expected_binding_revision ?? run.bindingRevision;
        run.segmentStatus = 'open';
        run.taskId = reply.task_id;
        remember('performanceRun', run);
        this.trackTask(reply.task_id, scenarioId);
        break;
      }
      case 'log.associate': {
        const artifactId = first('LogArtifact');
        if (artifactId) {
          const run = recall('performanceRun') || {};
          run.artifactId = artifactId;
          remember('performanceRun', run);
        }
        break;
      }
      case 'log.capture_recent': {
        const data = reply.data || {};
        remember('performanceRun', {
          sessionId: data.session_id, segmentId: data.segment_id,
          bindingId: data.binding_id, bindingRevision: data.binding_revision,
          segmentRevision: data.segment_revision, segmentStatus: 'closed',
          artifactId: data.artifact_id, taskId: null, source: 'recent_log',
        });
        remember('recentLogId', data.log?.id);
        break;
      }
      case 'feedback.review':
      case 'feedback.next': {
        const data = reply.data || {};
        remember('feedbackAnalysisId', first('FeedbackAnalysis') || data.analysis_id);
        remember('feedbackTaskId', reply.task_id);
        remember('feedbackAction', null);
        remember('changeSetId', null);
        if (reply.task_id) this.trackTask(reply.task_id, scenarioId);
        break;
      }
      default:
        break;
    }

    const knowledge = first('KnowledgeSnapshot');
    if (knowledge) remember('knowledgeId', knowledge);
    const recommendation = first('Recommendation');
    if (recommendation) {
      remember('recommendationId', recommendation);
      if (draft.action === 'recommendation.generate' && draft.payload?.knowledge_id) {
        remember('recommendationGeneratedFrom', draft.payload.knowledge_id);
      }
    }
    const applied = ids('AppliedConfiguration');
    if (applied.length) remember('appliedId', applied[applied.length - 1]);
    const summaries = ids('Summary');
    if (summaries.length) {
      const list = [...(recall('summaryIds') || [])];
      for (const id of summaries) if (!list.includes(id)) list.push(id);
      remember('summaryIds', list);
      remember('lastSummaryId', summaries[summaries.length - 1]);
    }
    const protocol = first('ComparisonProtocol');
    if (protocol) remember('protocolId', protocol);
    const comparison = first('Comparison');
    if (comparison) remember('comparisonId', comparison);
    const feedback = first('Feedback');
    if (feedback) remember('feedbackId', feedback);
    const outcome = first('Outcome');
    if (outcome) remember('outcomeId', outcome);
    const plan = first('TestPlan');
    if (plan) remember('planId', plan);
  }

  context() {
    const app = this;
    const scenarioId = this.state.scenarioId;
    return {
      api: this.api,
      state: this.state,
      get scenarioId() {
        return scenarioId;
      },
      get scenario() {
        return app.state.scenarios.find((item) => item.scenario_id === scenarioId);
      },
      get workspace() {
        return app.state.workspaces.get(scenarioId);
      },
      get catalog() {
        return app.state.catalogs.get(scenarioId);
      },
      entity: (id, kind) => app.entityFor(scenarioId, id, kind),
      entities: (kind) => app.api.entities(kind, {
        scenario: scenarioId,
        epoch: app.state.workspaces.get(scenarioId)?.workspace_epoch,
      }),
      selectScenario: (id) => app.selectScenario(id),
      send: (action, payload, options) => app.send(action, payload, options, scenarioId),
      upload: (file, options) => app.uploadFile(file, options, scenarioId),
      navigate: (page) => app.navigate(page),
      reload: () => app.render(),
      remember: (key, value) => app.rememberFor(scenarioId, key, value),
      recall: (key) => app.recallFor(scenarioId, key),
      trackTask: (taskId) => app.trackTask(taskId, scenarioId),
      scheduleRefreshAt: (timestamp) => app.scheduleRefreshAt(timestamp),
      toast,
      badge,
    };
  }

  remember(key, value) {
    this.rememberFor(this.state.scenarioId, key, value);
  }

  rememberFor(scenarioId, key, value) {
    try {
      localStorage.setItem(`demo:${scenarioId}:${key}`, JSON.stringify(value));
    } catch {
      /* presentation drafts only */
    }
  }

  recall(key) {
    return this.recallFor(this.state.scenarioId, key);
  }

  recallFor(scenarioId, key) {
    try {
      return JSON.parse(localStorage.getItem(`demo:${scenarioId}:${key}`));
    } catch {
      return null;
    }
  }

  async entity(id, kind) {
    return this.entityFor(this.state.scenarioId, id, kind);
  }

  async entityFor(scenarioId, id, kind) {
    const workspace = this.state.workspaces.get(scenarioId);
    if (!workspace) return null;
    return this.api.entity(id, {
      scenario: scenarioId,
      epoch: workspace.workspace_epoch,
      kind,
    });
  }

  async send(action, payload, { quiet = false } = {}, scenarioId = this.state.scenarioId) {
    const workspace = this.state.workspaces.get(scenarioId);
    if (!workspace) throw new Error('当前游戏信息尚未准备好。');
    const draft = {
      requestId: crypto.randomUUID(),
      action,
      scenarioId,
      epoch: workspace.workspace_epoch,
      revision: workspace.revision,
      payload: payload || {},
      status: 'pending',
      createdAt: Date.now(),
    };
    try {
      await this.drafts.prepare(draft);
    } catch (error) {
      this.draftFallback = draft;
      throw new Error(
        `无法写入本地草稿存储，已阻止发送。可复制输入后手动重试：${error.message}`,
      );
    }
    try {
      const reply = await this.api.command(draft);
      await this.drafts.remove(draft.requestId).catch(() => {});
      this.lastDraft = null;
      this.hideDraftBar();
      this.applyReply(draft, reply);
      await this.refresh(scenarioId);
      if (!quiet) toast('操作已保存', 'ok');
      return reply;
    } catch (error) {
      const status = error instanceof ApiError ? error.code : 'network_error';
      await this.drafts.mark(draft.requestId, status, error.message).catch(() => {});
      this.lastDraft = draft;
      this.showDraftBar(draft, error);
      if (!quiet) toast(error.message, 'error');
      throw error;
    }
  }

  async retryLastDraft() {
    const draft = this.lastDraft;
    if (!draft) return;
    try {
      const reply = await this.api.command(draft);
      await this.drafts.remove(draft.requestId).catch(() => {});
      this.lastDraft = null;
      this.hideDraftBar();
      this.applyReply(draft, reply);
      await this.refresh(draft.scenarioId || this.state.scenarioId);
      toast('操作已完成', 'ok');
      await this.render();
      return reply;
    } catch (error) {
      this.showDraftBar(draft, error);
      toast(error.message, 'error');
      throw error;
    }
  }

  showDraftBar(draft, error, note = null) {
    const bar = document.getElementById('draft-bar');
    bar.hidden = false;
    const labels = {
      'intent.submit': '发送需求',
      'knowledge.collect': '整理资料',
      'configuration.adopt': '确认采用',
      'capture.start': '开始测试',
      'feedback.save': '保存反馈',
      'feedback.review': '分析体验反馈',
    };
    const label = labels[draft.action] || '保存操作';
    bar.replaceChildren(
      h('span', {
        text: `${label}暂时没有完成。你的输入和已整理内容都还在，可以安全重试。${note ? ` ${note}` : ''}`,
      }),
      h('button', {
        type: 'button',
        class: 'primary',
        text: '重试同一请求',
        onClick: () => this.retryLastDraft(),
      }),
      h('button', {
        type: 'button',
        class: 'ghost',
        text: '放弃草稿',
        onClick: async () => {
          await this.drafts.remove(draft.requestId).catch(() => {});
          this.lastDraft = null;
          this.hideDraftBar();
        },
      }),
    );
  }

  hideDraftBar() {
    document.getElementById('draft-bar').hidden = true;
  }

  async uploadFile(file, { quiet = false } = {}, scenarioId = this.state.scenarioId) {
    const workspace = this.state.workspaces.get(scenarioId);
    if (!workspace) throw new Error('当前游戏信息尚未准备好。');
    const draft = {
      requestId: crypto.randomUUID(),
      scenarioId,
      epoch: workspace.workspace_epoch,
      revision: workspace.revision,
      file,
      filename: file.name,
    };
    try {
      const reply = await this.api.upload(draft);
      await this.refresh(scenarioId);
      if (!quiet) toast('文件已保存', 'ok');
      return reply;
    } catch (error) {
      toast(error.message, 'error');
      throw error;
    }
  }

  navigate(page, { updateHash = true } = {}) {
    if (!PAGES.some((item) => item.id === page) || this.state.page === page) return;
    this.state.page = page;
    if (updateHash) location.hash = page;
    this.render();
  }

  scheduleRefreshAt(timestamp) {
    const deadline = new Date(timestamp).getTime();
    if (!Number.isFinite(deadline) || deadline <= Date.now()) return;
    if (this.wakeTimer && this.wakeDeadline <= deadline) return;
    clearTimeout(this.wakeTimer);
    this.wakeDeadline = deadline;
    this.wakeTimer = setTimeout(() => {
      this.wakeTimer = null;
      this.wakeDeadline = null;
      this.render();
    }, deadline - Date.now() + 40);
  }

  trackTask(taskId, scenarioId = this.state.scenarioId) {
    if (this.trackedTask !== taskId || this.trackedScenario !== scenarioId) this.lastTaskSignature = null;
    this.trackedTask = taskId;
    this.trackedScenario = scenarioId;
    if (this.pollTimer) return;
    this.pollTimer = setInterval(async () => {
      if (!this.trackedTask) return;
      const taskId = this.trackedTask;
      const scenarioId = this.trackedScenario;
      const workspace = this.state.workspaces.get(scenarioId);
      if (!workspace) {
        this.trackedTask = null;
        return;
      }
      try {
        const task = await this.api.task(taskId, {
          scenario: scenarioId,
          epoch: workspace.workspace_epoch,
        });
        if (this.trackedTask !== taskId || this.trackedScenario !== scenarioId) return;
        const terminal = ['completed', 'partial', 'failed', 'cancelled', 'interrupted', 'superseded'].includes(task.status);
        const paced = (task.input?.card_not_before || task.input?.ready_not_before) && ['queued', 'running'].includes(task.status);
        const signature = `${task.status}:${task.phase_index}:${paced ? Math.floor(Date.now() / 5000) : ''}`;
        if (task.status === 'completed' && task.result?.card_id &&
            this.recallFor(this.trackedScenario, 'cardNoticeShown') !== task.result.card_id) {
          this.rememberFor(this.trackedScenario, 'cardNoticeShown', task.result.card_id);
          toast(task.result.recommendation_id ? '已整理 15 份资料并生成设置方案，游戏已加入“我的游戏”。' : '游戏资料已整理完成。', 'ok');
        }
        if (task.status === 'completed' && task.result?.analysis_id) {
          this.rememberFor(this.trackedScenario, 'feedbackAction', task.result);
          this.rememberFor(this.trackedScenario, 'changeSetId', task.result.change_set_id || null);
        }
        if (terminal) {
          this.trackedTask = null;
          this.lastTaskSignature = null;
          await this.render();
        } else if (signature !== this.lastTaskSignature) {
          this.lastTaskSignature = signature;
          await this.render();
        }
      } catch {
        if (this.trackedTask === taskId && this.trackedScenario === scenarioId) this.trackedTask = null;
      }
    }, 1000);
  }

  async render() {
    if (this.rendering) {
      this.renderAgain = true;
      return;
    }
    this.rendering = true;
    const main = document.getElementById('page-root');
    const oldThread = main.querySelector('.conversation-thread');
    const oldInput = main.querySelector('.composer textarea');
    if (oldThread) this.chatViewport = {
      scrollTop: oldThread.scrollTop,
      atBottom: oldThread.scrollHeight - oldThread.clientHeight - oldThread.scrollTop < 48,
      focused: document.activeElement === oldInput,
      selection: oldInput?.selectionStart,
    };
    const scenarioId = this.state.scenarioId;
    const cards = await this.api.entities('GameCard', {
      scenario: scenarioId,
      epoch: this.state.workspaces.get(scenarioId)?.workspace_epoch,
    }).catch(() => []);
    const hasGame = cards.length > 0;
    const card = cards.at(-1);
    if (card) {
      this.remember('knowledgeId', card.knowledge_id);
      if (this.recall('gameCardId') !== card.id || this.recall('cardRecommendationId') !== (card.recommendation_id || null) || !this.recall('recommendationId')) {
        this.remember('gameCardId', card.id);
        this.remember('cardRecommendationId', card.recommendation_id || null);
        this.remember('recommendationId', card.recommendation_id || null);
      }
    }
    const analyses = await this.api.entities('FeedbackAnalysis', {
      scenario: scenarioId, epoch: this.state.workspaces.get(scenarioId)?.workspace_epoch,
    }).catch(() => []);
    const analysis = analyses.at(-1);
    if (analysis && ['queued', 'running'].includes(analysis.status)) this.trackTask(analysis.task_id, scenarioId);
    document.querySelector('.more-nav')?.toggleAttribute('hidden', !hasGame);
    if (!hasGame && !['conversation', 'library'].includes(this.state.page)) {
      this.state.page = this.state.page === 'history' ? 'library' : 'conversation';
      if (location.hash !== `#${this.state.page}`) location.hash = this.state.page;
    }
    document.body.dataset.page = this.state.page;
    document.querySelectorAll('.side-nav li').forEach((item) => {
      const current = item.dataset.page === this.state.page;
      item.classList.toggle('active', current);
      if (current) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
    document.getElementById('service-status').textContent = '已就绪';
    const page = PAGES.find((item) => item.id === this.state.page);
    if (main.dataset.page !== page.id) main.replaceChildren(h('p', { class: 'empty', text: '正在准备内容…' }));
    try {
      const root = h('div', { class: 'page' });
      if (!['conversation', 'library'].includes(this.state.page)) {
        root.append(h('div', { class: 'page-return' }, h('button', {
          type: 'button', class: 'back-button', text: '← 返回我的游戏',
          onClick: () => this.navigate('library'),
        })));
      }
      await page.module.render(root, this.context());
      if (scenarioId !== this.state.scenarioId || page.id !== this.state.page) {
        // The user switched cases while this page was loading; discard the
        // stale render instead of mixing two workspaces in one view.
        this.renderAgain = true;
        return;
      }
      associateLabels(root);
      const previousFeedback = main.querySelector('[data-draft-key="feedbackDraft"]');
      const nextFeedback = root.querySelector('[data-draft-key="feedbackDraft"]');
      const feedbackFocused = previousFeedback && document.activeElement === previousFeedback;
      if (nextFeedback) nextFeedback.value = this.recall('feedbackDraft') ?? nextFeedback.value;
      main.replaceChildren(root);
      main.dataset.page = page.id;
      if (feedbackFocused && nextFeedback) {
        nextFeedback.focus({ preventScroll: true });
        nextFeedback.setSelectionRange(previousFeedback.selectionStart, previousFeedback.selectionEnd);
      }
      if (this.state.page === 'conversation') {
        const thread = root.querySelector('.conversation-thread');
        if (thread) thread.scrollTop = this.chatViewport && !this.chatViewport.atBottom
          ? this.chatViewport.scrollTop : thread.scrollHeight;
        if (this.chatViewport?.focused) {
          const input = root.querySelector('.composer textarea');
          input?.focus({ preventScroll: true });
          if (input) input.setSelectionRange(this.chatViewport.selection, this.chatViewport.selection);
        }
      }
    } catch (error) {
      main.replaceChildren(
        h('div', { class: 'alert error', role: 'alert' }, `页面读取失败：${error.message}`),
      );
    } finally {
      this.rendering = false;
      if (this.renderAgain) {
        this.renderAgain = false;
        this.render();
      }
    }
  }
}

const app = new App();
if (!globalThis.__DEMO_NO_AUTOBOOT__) {
  app.init().catch((error) => {
    document.getElementById('page-root').replaceChildren(
      h('div', { class: 'alert error', role: 'alert' }, `暂时无法读取内容：${error.message}`),
    );
    document.getElementById('service-status').textContent = '连接中断';
  });
}
