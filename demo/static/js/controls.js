import { h, badge, confirmDialog, drawer, toast } from './components.js';

function clearLocal(ctx, allCases) {
  const prefixes = allCases
    ? ctx.state.scenarios.map((scenario) => `demo:${scenario.scenario_id}:`)
    : [`demo:${ctx.scenarioId}:`];
  for (let index = localStorage.length - 1; index >= 0; index -= 1) {
    const key = localStorage.key(index);
    if (key && prefixes.some((prefix) => key.startsWith(prefix))) {
      localStorage.removeItem(key);
    }
  }
}

function rememberSeed(ctx, reply) {
  const of = (kind) =>
    (reply.refs || []).filter((ref) => ref.kind === kind).map((ref) => ref.id);
  const knowledge = of('KnowledgeSnapshot')[0];
  if (knowledge) ctx.remember('knowledgeId', knowledge);
  const recommendation = of('Recommendation')[0];
  if (recommendation) {
    ctx.remember('recommendationId', recommendation);
    ctx.remember('recommendationGeneratedFrom', knowledge);
  }
  const configurations = of('AppliedConfiguration');
  if (configurations.length) ctx.remember('appliedId', configurations[configurations.length - 1]);
  const summaries = of('Summary');
  if (summaries.length) {
    ctx.remember('summaryIds', summaries);
    ctx.remember('lastSummaryId', summaries[summaries.length - 1]);
  }
  const protocol = of('ComparisonProtocol')[0];
  if (protocol) ctx.remember('protocolId', protocol);
  const comparison = of('Comparison')[0];
  if (comparison) ctx.remember('comparisonId', comparison);
  if (reply.data?.change_set_id) ctx.remember('changeSetId', reply.data.change_set_id);
}

export function openConsole(ctx) {
  const existing = document.querySelector('.drawer');
  if (existing) existing.remove();
  const reopen = () => {
    const current = document.querySelector('.drawer');
    if (current) current.remove();
    openConsole(ctx);
  };

  const workspace = ctx.workspace;
  const modeSelect = h('select', { 'aria-label': '演示模式' },
    ...ctx.state.modes.map((mode) =>
      h('option', { value: mode.id, text: mode.label, selected: mode.id === workspace.mode }),
    ),
  );
  modeSelect.addEventListener('change', async () => {
    await ctx.send('demo.mode', { mode: modeSelect.value });
    ctx.reload();
    reopen();
  });

  const scripts = [{ id: 'none', label: '不使用失败剧本' }, ...ctx.state.failureScripts];
  const scriptSelect = h('select', { 'aria-label': '失败剧本' },
    ...scripts.map((script) =>
      h('option', { value: script.id, text: script.label, selected: script.id === workspace.script }),
    ),
  );
  scriptSelect.addEventListener('change', async () => {
    await ctx.send('demo.script', { script: scriptSelect.value });
    ctx.reload();
    reopen();
  });

  const seedButtons = h('div', { class: 'panel-actions' },
    h('button', {
      type: 'button', class: 'primary', text: '加载完整案例（位于推荐后）',
      onClick: async () => {
        const confirmed = await confirmDialog({
          title: '加载完整案例',
          body: '这会为当前案例创建新的 epoch，并替换当前演示状态；另一个案例不受影响。',
          confirmLabel: '加载',
        });
        if (!confirmed) return;
        const seeded = await ctx.send('demo.seed', {
          position: 'recommendation',
          expected_epoch: workspace.workspace_epoch,
        });
        clearLocal(ctx, false);
        rememberSeed(ctx, seeded);
        ctx.reload();
        reopen();
      },
    }),
    h('button', {
      type: 'button', class: 'primary', text: '加载完整案例（位于复测结果）',
      onClick: async () => {
        const confirmed = await confirmDialog({
          title: '加载完整案例',
          body: '这会为当前案例创建新的 epoch，并替换当前演示状态；另一个案例不受影响。',
          confirmLabel: '加载',
        });
        if (!confirmed) return;
        const seeded = await ctx.send('demo.seed', {
          position: 'comparison',
          expected_epoch: workspace.workspace_epoch,
        });
        clearLocal(ctx, false);
        rememberSeed(ctx, seeded);
        ctx.reload();
        reopen();
      },
    }),
    h('button', {
      type: 'button', class: 'ghost', text: '快速模式（约一秒）',
      onClick: async () => {
        await ctx.send('demo.mode', { mode: 'fast' });
        ctx.reload();
        reopen();
      },
    }),
  );

  const resetButtons = h('div', { class: 'panel-actions' },
    h('button', {
      type: 'button', class: 'danger', text: '重置当前案例',
      onClick: async () => {
        const confirmed = await confirmDialog({
          title: '重置当前案例',
          body: '会开启新的 epoch，旧任务与旧页面提交失效；只清理本演示工作区登记的文件。',
          confirmLabel: '重置当前案例',
        });
        if (!confirmed) return;
        await ctx.send('demo.reset', { scope: 'current' });
        clearLocal(ctx, false);
        ctx.reload();
        reopen();
      },
    }),
    h('button', {
      type: 'button', class: 'danger', text: '重置全部案例',
      onClick: async () => {
        const confirmed = await confirmDialog({
          title: '重置全部案例',
          body: '会同时为两个案例开启新 epoch。另一个案例的当前状态也会被清空。',
          confirmLabel: '重置全部',
        });
        if (!confirmed) return;
        await ctx.send('demo.reset', { scope: 'all' });
        clearLocal(ctx, true);
        ctx.reload();
        reopen();
      },
    }),
  );

  const body = h('div', { class: 'stack' },
    h('p', { class: 'muted', text: '这里的控制只影响演示状态与 demo/runtime 中的受管理文件。' }),
    h('h4', { text: '演示模式' }),
    modeSelect,
    h('h4', { text: '失败剧本' }),
    scriptSelect,
    h('p', { class: 'muted', text: '失败剧本只作用于当前案例与 epoch；取消剧本不会删除历史事实。' }),
    h('h4', { text: '一键加载' }),
    seedButtons,
    h('h4', { text: '重置' }),
    h('p', { class: 'muted', text: `当前 epoch ${workspace.workspace_epoch}。重置会递增 epoch。` }),
    resetButtons,
    h('h4', { text: '当前状态' }),
    h('div', { class: 'inline' },
      badge(`mode ${workspace.mode}`, 'info'),
      badge(`script ${workspace.script}`, workspace.script === 'none' ? 'ok' : 'warn'),
      badge(`revision ${workspace.revision}`, 'info'),
    ),
  );
  drawer('演示控制', body);
}
