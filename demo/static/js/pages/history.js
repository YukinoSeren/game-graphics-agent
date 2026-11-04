import {
  h, panel, badge, alertBox, definitionList, emptyState, formatNumber, keyValueTable, toast,
} from '../components.js';

const EVENT_LABELS = {
  'configuration.adopted': '保存/采用配置',
  'configuration.edited': '手动调整配置',
  'configuration.rolled_back': '回滚生成新版本',
  'configuration.recorded': '补录过去配置',
  'performance.analyzed': '完成性能分析',
  'comparison.frozen': '冻结比较口径',
  'comparison.run': '运行前后比较',
  'feedback.saved': '保存反馈',
  'feedback.next': '生成下一步建议',
  'outcome.saved': '记录结果接受',
  'knowledge.imported': '整理资料',
  'task.queued': '开始整理',
  'knowledge.collect.queued': '开始整理资料',
  'game.card.published': '添加游戏卡',
  'session.created': '创建测试记录',
  'capture.started': '开始性能测试',
  'capture.stopped': '完成性能测试',
  'log.recent.captured': '读取游玩日志',
};

async function downloadFile(ctx, fileId) {
  const url = await ctx.api.fileUrl(fileId, {
    scenario: ctx.scenarioId,
    epoch: ctx.workspace.workspace_epoch,
  });
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = '';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

export async function render(root, ctx) {
  const workspace = ctx.workspace;
  const history = await ctx.api.history({
    scenario: ctx.scenarioId,
    epoch: workspace.workspace_epoch,
    limit: 200,
  });
  const events = history.events.filter((event) => ![
    'demo.seed', 'demo.reset', 'demo.mode', 'demo.script',
  ].includes(event.event));
  const configIds = new Set();
  const summaryIds = new Set();
  const comparisonIds = new Set();
  for (const event of events) {
    if (event.payload?.configuration_id) configIds.add(event.payload.configuration_id);
    if (event.payload?.summary_id) summaryIds.add(event.payload.summary_id);
    if (event.payload?.comparison_id) comparisonIds.add(event.payload.comparison_id);
  }
  if (workspace.current_applied_id) configIds.add(workspace.current_applied_id);

  const configurations = [];
  const configurationSeen = new Set();
  const collectConfiguration = (entity) => {
    if (entity && !configurationSeen.has(entity.id)) {
      configurationSeen.add(entity.id);
      configurations.push(entity);
    }
  };
  for (const id of configIds) {
    collectConfiguration(await ctx.entity(id, 'AppliedConfiguration').catch(() => null));
  }
  const listedConfigurations = await ctx.api
    .entities('AppliedConfiguration', { scenario: ctx.scenarioId, epoch: workspace.workspace_epoch })
    .catch(() => []);
  for (const entity of listedConfigurations) collectConfiguration(entity);

  const summaries = [];
  const summarySeen = new Set();
  const collectSummary = (entity) => {
    if (entity && !summarySeen.has(entity.id)) {
      summarySeen.add(entity.id);
      summaries.push(entity);
    }
  };
  for (const id of summaryIds) {
    collectSummary(await ctx.entity(id, 'Summary').catch(() => null));
  }
  const listedSummaries = await ctx.api
    .entities('Summary', { scenario: ctx.scenarioId, epoch: workspace.workspace_epoch })
    .catch(() => []);
  for (const entity of listedSummaries) collectSummary(entity);
  summaries.sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));

  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('p', { class: 'eyebrow', text: '历史记录' }),
        h('h2', { text: '设置与测试记录' }),
        h('p', { text: '历史只追加；回滚会生成新版本，补录配置不会移动当前指针。' }),
      ),
    ),
  );

  if (!events.length && !configurations.length) {
    root.append(panel('历史', emptyState('完成方案或测试后，记录会显示在这里。')));
  }

  root.append(
    panel('配置版本链',
      configurations.length
        ? chainList(configurations, workspace.current_applied_id, ctx.catalog.settings)
        : h('p', { class: 'empty', text: '尚无配置版本。' }),
    ),
  );

  root.append(
    h('div', { class: 'grid two' },
      panel('测试记录',
        summaries.length
          ? keyValueTable(
            [
              { key: 'label', label: '测试', render: (row) => ({ baseline: '初始设置', recommended: '当前设置', adjusted: '调整后设置', short: '补充测试', no_gain: '再次测试' })[row.label] || '测试记录' },
              { key: 'fps', label: '平均帧率', render: (row) => row.metrics.average_present_rate === null ? '未知' : formatNumber(row.metrics.average_present_rate, 1) },
            ],
            summaries,
          )
          : h('p', { class: 'empty', text: '尚无摘要。' }),
      ),
      panel('事件时间线',
        h('div', { class: 'timeline' },
          ...events.slice(-40).map((event) =>
            h('div', { class: 'phase done' },
              h('span', { class: 'phase-dot' }),
              h('span', { text: EVENT_LABELS[event.event] || '更新记录' }),
              h('span', { class: 'right muted', text: new Intl.DateTimeFormat('zh-CN', {
                dateStyle: 'medium', timeStyle: 'short',
              }).format(new Date(event.created_at)) }),
            ),
          ),
        ),
      ),
    ),
  );

  root.append(
    h('div', { class: 'grid two' },
      panel('导出记录',
        h('p', { class: 'muted', text: '下载当前设置、测试结果与体验反馈。' }),
        h('div', { class: 'panel-actions' },
          h('button', {
            type: 'button', class: 'primary', text: '导出 Markdown',
            onClick: async () => {
              const reply = await ctx.send('export.create', { format: 'markdown' });
              await downloadFile(ctx, reply.refs.find((ref) => ref.kind === 'File')?.id);
            },
          }),
          h('button', {
            type: 'button', class: 'ghost', text: '导出 JSON',
            onClick: async () => {
              const reply = await ctx.send('export.create', { format: 'json' });
              await downloadFile(ctx, reply.refs.find((ref) => ref.kind === 'File')?.id);
            },
          }),
        ),
      ),
      panel('保存与恢复记录',
        h('p', { class: 'muted', text: '保存当前工作区，之后可从文件恢复。' }),
        h('div', { class: 'panel-actions' },
          h('button', {
            type: 'button', class: 'ghost', text: '保存备份',
            onClick: async () => {
              const reply = await ctx.send('backup.create', {});
              await downloadFile(ctx, reply.refs.find((ref) => ref.kind === 'File')?.id);
            },
          }),
        ),
        h('div', { class: 'field' },
          h('label', { text: '选择备份文件' }),
          h('input', { type: 'file', accept: '.json,application/json', id: 'backup-file' }),
        ),
        h('div', { class: 'panel-actions' },
          h('button', {
            type: 'button', class: 'danger', text: '恢复备份',
            onClick: async () => {
              const file = document.getElementById('backup-file').files[0];
              if (!file) {
                toast('请先选择备份文件。', 'error');
                return;
              }
              const upload = await ctx.upload(file);
              const fileId = upload.refs.find((ref) => ref.kind === 'File')?.id;
              await ctx.send('backup.restore', { file_id: fileId });
              await ctx.reload();
            },
          }),
        ),
      ),
    ),
  );

  const occurredAt = h('input', { type: 'datetime-local', 'aria-label': '过去时间' });
  const basis = h('input', { type: 'text', value: '玩家回忆并手工补录', 'aria-label': '补录依据' });
  const settingsText = h('textarea', {
    rows: 4,
    'aria-label': '补录设置 JSON',
  });
  settingsText.value = JSON.stringify({ shadow_quality: 'medium' }, null, 2);
  root.append(
    panel('补录过去的配置（不移动当前指针）',
      h('p', { class: 'muted', text: '填写游戏内设置项及你当时使用的数值。' }),
      h('div', { class: 'form-grid' },
        h('div', { class: 'field' }, h('label', { text: '过去时间' }), occurredAt),
        h('div', { class: 'field' }, h('label', { text: '依据' }), basis),
      ),
      h('div', { class: 'field' }, h('label', { text: '设置 JSON' }), settingsText),
      h('div', { class: 'panel-actions' },
        h('button', {
          type: 'button', class: 'ghost', text: '保存补录',
          onClick: async () => {
            let settings;
            try {
              settings = JSON.parse(settingsText.value);
            } catch {
              toast('设置 JSON 无法解析。', 'error');
              return;
            }
            await ctx.send('configuration.record_history', {
              settings,
              occurred_at: occurredAt.value || new Date().toISOString(),
              basis: basis.value,
            });
            await ctx.reload();
          },
        }),
      ),
    ),
  );
}

function chainOrder(configurations) {
  const byId = new Map(configurations.map((item) => [item.id, item]));
  const depth = (item, seen) => {
    if (!item.parent_id || !byId.has(item.parent_id)) return 0;
    if (seen.has(item.id)) return 0;
    seen.add(item.id);
    return 1 + depth(byId.get(item.parent_id), seen);
  };
  return [...configurations].sort((a, b) =>
    depth(a, new Set()) - depth(b, new Set())
    || a.created_at.localeCompare(b.created_at)
    || a.id.localeCompare(b.id));
}

function chainList(configurations, currentId, settings = []) {
  const list = h('div', { class: 'chain' });
  const ordered = chainOrder(configurations);
  const settingNames = new Map(settings.map((setting) => [setting.id, setting.display_name]));
  for (const [index, config] of ordered.entries()) {
    const parent = config.parent_id
      ? configurations.find((item) => item.id === config.parent_id)
      : null;
    const origin = ({ baseline: '当前设置', recommendation: '方案建议', manual: '手动调整', rollback: '恢复版本' })[config.origin] || '设置记录';
    list.append(
      h('div', { class: 'chain-item' },
        h('div', { class: 'inline' },
          badge(origin, config.id === currentId ? 'ok' : 'info'),
          h('span', { text: `方案 ${index + 1}` }),
          config.id === currentId ? badge('当前', 'ok') : null,
          config.rollback_target_id ? badge('由历史设置恢复', 'warn') : null,
        ),
        h('div', { class: 'chain-diff', text: parent ? diffText(config, parent, settingNames) : '初始设置' }),
      ),
    );
  }
  return list;
}

function diffText(current, parent, settingNames) {
  if (!parent) return '无父版本';
  const keys = new Set([...Object.keys(parent.settings), ...Object.keys(current.settings)]);
  const changed = [...keys].filter((key) => parent.settings[key] !== current.settings[key]);
  if (!changed.length) return '无变化';
  return changed.map((key) => `${settingNames.get(key) || '设置项'}：${parent.settings[key]} → ${current.settings[key]}`).join('；');
}
